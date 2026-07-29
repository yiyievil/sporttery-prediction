"""football-prediction-pipeline · 预测模型层

三个独立预测模型, 最终通过 config.model_weights 加权融合:
  PoissonModel  — 基于 xG/xGA 的泊松模型 + Dixon-Coles 低比分修正
  XGBoostModel  — 基于 xgboost 梯度提升的多分类模型
  EloModel      — 基于 Elo 评级差的胜平负概率模型

所有模型的 predict() 统一返回胜平负概率, 键名:
  "win"  -> 主队胜 (胜)
  "draw" -> 平局   (平)
  "lose" -> 主队负 (客队胜, 负)
"""
from __future__ import annotations

import math
import logging

import numpy as np
import pandas as pd

from .config import config

logger = logging.getLogger("models")

# 泊松比分矩阵枚举上限 (0..N 球)
_MAX_GOALS = 10
# 阶乘查表 (加速向量化泊松概率计算)
_FACT = np.array([math.factorial(i) for i in range(_MAX_GOALS + 1)], dtype=float)


# ============================================================
# 1. 泊松模型 (Poisson + Dixon-Coles)
# ============================================================
class PoissonModel:
    """基于 xG/xGA 的泊松得分模型

    用预期进球 (xG) / 预期失球 (xGA) 计算主客队进球强度 lambda:
        lam_h = (home_xg_for + away_xg_against) / 2
        lam_a = (away_xg_for + home_xg_against) / 2
    其中 home_xg_for 为主队场均预期进球, away_xg_against 为客队场均预期失球。
    当 xG 不可用时, 回退到实际进球 (goals_for / goals_against)。

    使用 Dixon-Coles 修正项 tau 对低比分 (0-0/1-0/0-1/1-1) 概率进行校正,
    参数 rho 在 fit() 中由历史数据网格搜索最大化对数似然估计得到。
    """

    def __init__(self, max_goals: int = _MAX_GOALS):
        self.max_goals = max_goals
        # Dixon-Coles rho 参数 (低比分相关性), fit 后更新
        self.rho: float = 0.0
        # 主场进攻加成倍数 (fit 后更新)
        self.home_advantage: float = 1.0
        # 球队近期统计: {team: {xg_for, xg_against, goals_for, goals_against}}
        self.team_stats: dict[str, dict[str, float]] = {}
        # 联赛场均进球 (回退用)
        self.league_avg: float = 1.35

    # ---------- 泊松概率 ----------
    @staticmethod
    def _poisson_pmf(k: int, lam: float) -> float:
        """单点泊松概率 P(X=k; lam) = e^-lam * lam^k / k!"""
        if lam <= 0:
            return 1.0 if k == 0 else 0.0
        return math.exp(-lam) * (lam ** k) / math.factorial(k)

    @staticmethod
    def _poisson_pmf_vec(k: np.ndarray, lam: np.ndarray) -> np.ndarray:
        """向量化泊松概率 (k 为整数数组)"""
        k = np.clip(k.astype(int), 0, _MAX_GOALS)
        lam = np.asarray(lam, dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            pmf = np.exp(-lam) * np.power(lam, k) / _FACT[k]
        pmf = np.where(lam <= 0, (k == 0).astype(float), pmf)
        return pmf

    # ---------- Dixon-Coles 修正 ----------
    @staticmethod
    def _dc_tau(i: int, j: int, lam_h: float, lam_a: float, rho: float) -> float:
        """Dixon-Coles 低比分修正系数 tau(i, j)"""
        if i == 0 and j == 0:
            return 1.0 - lam_h * lam_a * rho
        if i == 0 and j == 1:
            return 1.0 + lam_h * rho
        if i == 1 and j == 0:
            return 1.0 + lam_a * rho
        if i == 1 and j == 1:
            return 1.0 - rho
        return 1.0

    @staticmethod
    def _dc_tau_vec(i: np.ndarray, j: np.ndarray,
                    lam_h: np.ndarray, lam_a: np.ndarray, rho: float) -> np.ndarray:
        """向量化 Dixon-Coles 修正系数"""
        tau = np.ones_like(lam_h, dtype=float)
        m00 = (i == 0) & (j == 0)
        m01 = (i == 0) & (j == 1)
        m10 = (i == 1) & (j == 0)
        m11 = (i == 1) & (j == 1)
        tau[m00] = 1.0 - lam_h[m00] * lam_a[m00] * rho
        tau[m01] = 1.0 + lam_h[m01] * rho
        tau[m10] = 1.0 + lam_a[m10] * rho
        tau[m11] = 1.0 - rho
        return tau

    def _prob_matrix(self, lam_h: float, lam_a: float, rho: float) -> np.ndarray:
        """构建 (max_goals+1)x(max_goals+1) 比分概率矩阵并归一化

        mat[i, j] = tau(i,j) * P(主队进 i 球) * P(客队进 j 球)
        """
        n = self.max_goals + 1
        pmf_h = np.array([self._poisson_pmf(i, lam_h) for i in range(n)])
        pmf_a = np.array([self._poisson_pmf(j, lam_a) for j in range(n)])
        mat = np.outer(pmf_h, pmf_a)
        for i in range(n):
            for j in range(n):
                mat[i, j] *= self._dc_tau(i, j, lam_h, lam_a, rho)
        total = mat.sum()
        if total > 0:
            mat /= total
        return mat

    @staticmethod
    def _dc_score_prob_vec(gh: np.ndarray, ga: np.ndarray,
                           lam_h: np.ndarray, lam_a: np.ndarray, rho: float) -> np.ndarray:
        """向量化计算实际比分 (gh, ga) 的发生概率"""
        gh = np.clip(gh.astype(int), 0, _MAX_GOALS)
        ga = np.clip(ga.astype(int), 0, _MAX_GOALS)
        pmf_h = PoissonModel._poisson_pmf_vec(gh, lam_h)
        pmf_a = PoissonModel._poisson_pmf_vec(ga, lam_a)
        tau = PoissonModel._dc_tau_vec(gh, ga, lam_h, lam_a, rho)
        return tau * pmf_h * pmf_a

    # ---------- 特征工程 ----------
    def _engineer_features(self, matches: pd.DataFrame) -> pd.DataFrame:
        """计算每场比赛的赛前滚动 xG/进球统计 (shift 防泄漏)

        返回列:
            home_xg_for, home_xg_against, away_xg_for, away_xg_against,
            home_goals_for, home_goals_against, away_goals_for, away_goals_against,
            home_goals, away_goals
        """
        df = matches.copy().reset_index(drop=True)
        df["_mid"] = np.arange(len(df))
        if "date" in df.columns:
            df = df.sort_values("date").reset_index(drop=True)
            df["_mid"] = np.arange(len(df))

        has_team = "home_team" in df.columns and "away_team" in df.columns
        if not has_team:
            # 无球队维度, 直接用单场 xG/进球
            out = pd.DataFrame({
                "home_xg_for": df.get("home_xg", np.nan),
                "home_xg_against": df.get("away_xg", np.nan),
                "away_xg_for": df.get("away_xg", np.nan),
                "away_xg_against": df.get("home_xg", np.nan),
                "home_goals_for": df.get("home_goals", np.nan),
                "home_goals_against": df.get("away_goals", np.nan),
                "away_goals_for": df.get("away_goals", np.nan),
                "away_goals_against": df.get("home_goals", np.nan),
                "home_goals": df.get("home_goals", np.nan),
                "away_goals": df.get("away_goals", np.nan),
            }, index=df.index)
            return out

        # 主队 / 客队视角的长表
        h = pd.DataFrame({
            "date": df.get("date", pd.NaT),
            "_mid": df["_mid"],
            "is_home": True,
            "team": df["home_team"],
            "xg_for": df.get("home_xg", np.nan),
            "xg_against": df.get("away_xg", np.nan),
            "goals_for": df.get("home_goals", np.nan),
            "goals_against": df.get("away_goals", np.nan),
        })
        a = pd.DataFrame({
            "date": df.get("date", pd.NaT),
            "_mid": df["_mid"],
            "is_home": False,
            "team": df["away_team"],
            "xg_for": df.get("away_xg", np.nan),
            "xg_against": df.get("home_xg", np.nan),
            "goals_for": df.get("away_goals", np.nan),
            "goals_against": df.get("home_goals", np.nan),
        })
        long = pd.concat([h, a], ignore_index=True)
        long = long.sort_values(["team", "date"]).reset_index(drop=True)

        w = config.rolling_window
        g = long.groupby("team", sort=False)
        for col in ["xg_for", "xg_against", "goals_for", "goals_against"]:
            # shift(1) 保证只用历史比赛, 无泄漏
            long[col + "_roll"] = g[col].transform(
                lambda s: s.shift(1).rolling(w, min_periods=1).mean()
            )

        home_stats = long[long["is_home"]].set_index("_mid")[
            ["xg_for_roll", "xg_against_roll", "goals_for_roll", "goals_against_roll"]
        ].rename(columns={
            "xg_for_roll": "home_xg_for",
            "xg_against_roll": "home_xg_against",
            "goals_for_roll": "home_goals_for",
            "goals_against_roll": "home_goals_against",
        })
        away_stats = long[~long["is_home"]].set_index("_mid")[
            ["xg_for_roll", "xg_against_roll", "goals_for_roll", "goals_against_roll"]
        ].rename(columns={
            "xg_for_roll": "away_xg_for",
            "xg_against_roll": "away_xg_against",
            "goals_for_roll": "away_goals_for",
            "goals_against_roll": "away_goals_against",
        })

        out = df[["_mid"]].join(home_stats, on="_mid").join(away_stats, on="_mid")
        out["home_goals"] = df.get("home_goals", np.nan)
        out["away_goals"] = df.get("away_goals", np.nan)
        out = out.drop(columns=["_mid"])
        return out

    # ---------- 训练 ----------
    def fit(self, matches: pd.DataFrame) -> "PoissonModel":
        """拟合模型: 估计 Dixon-Coles rho 与主场加成, 计算球队近期统计

        Args:
            matches: 比赛数据, 支持两种格式:
                     A) FeatureBuilder 输出 (含 home_xg_for, home_xg_against 等)
                     B) 原始比赛数据 (含 home_xg, away_xg, home_goals, away_goals, date)
        """
        # 判断输入格式: FeatureBuilder 输出已有 home_xg_for 列
        is_feature_matrix = "home_xg_for" in matches.columns

        if is_feature_matrix:
            # FeatureBuilder 输出: 直接使用已计算的滚动统计
            feats = matches.copy()
            # 确保有 home_goals / away_goals (用于标签)
            if "home_goals" not in feats.columns:
                feats["home_goals"] = np.nan
            if "away_goals" not in feats.columns:
                feats["away_goals"] = np.nan
        else:
            # 原始比赛数据: 计算滚动统计
            feats = self._engineer_features(matches)

        # 联赛场均进球 (xG 优先, 回退到进球)
        # 处理两种输入格式: FeatureBuilder 输出 (home_xg_for) 或原始数据 (home_xg)
        if "home_xg" in matches.columns:
            xg_all = pd.concat([matches.get("home_xg"), matches.get("away_xg")]).dropna()
        elif "home_xg_for" in matches.columns:
            xg_all = pd.concat([matches.get("home_xg_for"), matches.get("away_xg_for")]).dropna()
        else:
            xg_all = pd.Series(dtype=float)
        gl_all = pd.concat([matches.get("home_goals"), matches.get("away_goals")]).dropna() if "home_goals" in matches.columns else pd.Series(dtype=float)
        if len(xg_all) > 0:
            self.league_avg = float(xg_all.mean())
        elif len(gl_all) > 0:
            self.league_avg = float(gl_all.mean())

        # 每场基础 lambda (不含主场加成), xG 优先, 回退进球, 再回退联赛均值
        hxf = feats["home_xg_for"].fillna(feats.get("home_goals_for", pd.Series(np.nan, index=feats.index)))
        axf = feats["away_xg_for"].fillna(feats.get("away_goals_for", pd.Series(np.nan, index=feats.index)))
        hxa = feats["home_xg_against"].fillna(feats.get("home_goals_against", pd.Series(np.nan, index=feats.index)))
        axa = feats["away_xg_against"].fillna(feats.get("away_goals_against", pd.Series(np.nan, index=feats.index)))
        hxf = hxf.fillna(self.league_avg)
        axf = axf.fillna(self.league_avg)
        hxa = hxa.fillna(self.league_avg)
        axa = axa.fillna(self.league_avg)

        lam_h_base = ((hxf + axa) / 2.0).to_numpy(dtype=float)
        lam_a_base = ((axf + hxa) / 2.0).to_numpy(dtype=float)
        gh = feats["home_goals"].to_numpy(dtype=float) if "home_goals" in feats.columns else np.array([])
        ga = feats["away_goals"].to_numpy(dtype=float) if "away_goals" in feats.columns else np.array([])

        if len(gh) > 0 and len(ga) > 0:
            valid = ~(np.isnan(gh) | np.isnan(ga) | np.isnan(lam_h_base) | np.isnan(lam_a_base))
            gh, ga = gh[valid], ga[valid]
            lam_h_base, lam_a_base = lam_h_base[valid], lam_a_base[valid]
        else:
            gh, ga = np.array([]), np.array([])

        # 网格搜索 rho (低比分相关) 与 home_advantage (主场进攻加成)
        best_ll, best_rho, best_hfa = -np.inf, 0.0, 1.0
        if len(gh) > 0:
            for hfa in (1.0, 1.1, 1.2, 1.3, 1.4):
                lam_h = lam_h_base * hfa
                for rho in np.linspace(-0.2, 0.2, 21):
                    p = self._dc_score_prob_vec(gh, ga, lam_h, lam_a_base, rho)
                    p = np.clip(p, 1e-12, None)
                    ll = float(np.sum(np.log(p)))
                    if ll > best_ll:
                        best_ll, best_rho, best_hfa = ll, rho, hfa

        self.rho = float(best_rho)
        self.home_advantage = float(best_hfa)

        # 球队当前统计 (含全部历史, 非 shift, 作为预测用近期状态)
        if is_feature_matrix:
            # FeatureBuilder 输出: 从特征列提取球队统计
            self.team_stats = self._extract_stats_from_features(matches)
        else:
            self.team_stats = self._compute_current_stats(matches)

        logger.info(
            "PoissonModel 拟合完成: rho=%.4f, home_advantage=%.2f, 球队数=%d, 样本=%d, loglik=%.2f",
            self.rho, self.home_advantage, len(self.team_stats), int(len(gh)), best_ll,
        )
        return self

    def _extract_stats_from_features(self, features: pd.DataFrame) -> dict[str, dict[str, float]]:
        """从 FeatureBuilder 输出提取每支球队最新统计"""
        stats: dict[str, dict[str, float]] = {}
        for _, row in features.iterrows():
            home = row.get("team_home")
            away = row.get("team_away")
            if home and pd.notna(home):
                hxf = row.get("home_xg_for")
                hxa = row.get("home_xg_against")
                if pd.notna(hxf) and pd.notna(hxa):
                    stats[str(home)] = {
                        "xg_for": float(hxf),
                        "xg_against": float(hxa),
                        "goals_for": float(hxf),
                        "goals_against": float(hxa),
                    }
            if away and pd.notna(away):
                axf = row.get("away_xg_for")
                axa = row.get("away_xg_against")
                if pd.notna(axf) and pd.notna(axa):
                    stats[str(away)] = {
                        "xg_for": float(axf),
                        "xg_against": float(axa),
                        "goals_for": float(axf),
                        "goals_against": float(axa),
                    }
        return stats

    def _compute_current_stats(self, matches: pd.DataFrame) -> dict[str, dict[str, float]]:
        """计算每支球队含全部历史的滚动均值 (当前状态)"""
        stats: dict[str, dict[str, float]] = {}
        if "home_team" not in matches.columns or "away_team" not in matches.columns:
            return stats
        df = matches.copy()
        if "date" in df.columns:
            df = df.sort_values("date")
        h = pd.DataFrame({
            "team": df["home_team"],
            "xg_for": df.get("home_xg", np.nan),
            "xg_against": df.get("away_xg", np.nan),
            "goals_for": df.get("home_goals", np.nan),
            "goals_against": df.get("away_goals", np.nan),
        })
        a = pd.DataFrame({
            "team": df["away_team"],
            "xg_for": df.get("away_xg", np.nan),
            "xg_against": df.get("home_xg", np.nan),
            "goals_for": df.get("away_goals", np.nan),
            "goals_against": df.get("home_goals", np.nan),
        })
        long = pd.concat([h, a], ignore_index=True).sort_values("team").reset_index(drop=True)
        w = config.rolling_window
        g = long.groupby("team", sort=False)
        for col in ["xg_for", "xg_against", "goals_for", "goals_against"]:
            long[col + "_roll"] = g[col].transform(lambda s: s.rolling(w, min_periods=1).mean())
        latest = long.groupby("team", sort=False).last()
        for team, row in latest.iterrows():
            xg_for = row["xg_for_roll"]
            xg_against = row["xg_against_roll"]
            stats[team] = {
                "xg_for": float(xg_for) if not pd.isna(xg_for) else float(row["goals_for_roll"]),
                "xg_against": float(xg_against) if not pd.isna(xg_against) else float(row["goals_against_roll"]),
                "goals_for": float(row["goals_for_roll"]) if not pd.isna(row["goals_for_roll"]) else self.league_avg,
                "goals_against": float(row["goals_against_roll"]) if not pd.isna(row["goals_against_roll"]) else self.league_avg,
            }
        return stats

    # ---------- 预测 ----------
    def _get_lambdas(self, row) -> tuple[float, float]:
        """从单场比赛取出 (lam_h, lam_a), 依次回退:
        显式 xG 特征 -> 球队统计 -> 单场 xG -> 实际进球 -> 联赛均值
        """
        def g(key, default=np.nan):
            if isinstance(row, dict):
                return row.get(key, default)
            if isinstance(row, pd.Series):
                return row.get(key, default)
            return getattr(row, key, default)

        def ok(v):
            return v is not None and not (isinstance(v, float) and math.isnan(v))

        home_team = g("home_team", g("team_home", None))
        away_team = g("away_team", g("team_away", None))

        hxf, axf, hxa, axa = np.nan, np.nan, np.nan, np.nan

        # 1) 显式 xG 特征
        if ok(g("home_xg_for", np.nan)):
            hxf = float(g("home_xg_for"))
        if ok(g("away_xg_for", np.nan)):
            axf = float(g("away_xg_for"))
        if ok(g("home_xg_against", np.nan)):
            hxa = float(g("home_xg_against"))
        if ok(g("away_xg_against", np.nan)):
            axa = float(g("away_xg_against"))

        # 2) 球队统计查表
        if not ok(hxf) and home_team in self.team_stats:
            hxf = self.team_stats[home_team]["xg_for"]
            hxa = self.team_stats[home_team]["xg_against"]
        if not ok(axf) and away_team in self.team_stats:
            axf = self.team_stats[away_team]["xg_for"]
            axa = self.team_stats[away_team]["xg_against"]

        # 3) 单场 xG 回退 (主队 xG_for=home_xg, 主队 xG_against=away_xg)
        if not ok(hxf):
            if ok(g("home_xg", np.nan)):
                hxf = float(g("home_xg"))
            if ok(g("away_xg", np.nan)):
                hxa = float(g("away_xg"))
        if not ok(axf):
            if ok(g("away_xg", np.nan)):
                axf = float(g("away_xg"))
            if ok(g("home_xg", np.nan)):
                axa = float(g("home_xg"))

        # 4) 实际进球回退
        if not ok(hxf):
            if ok(g("home_goals", np.nan)):
                hxf = float(g("home_goals"))
            if ok(g("away_goals", np.nan)):
                hxa = float(g("away_goals"))
        if not ok(axf):
            if ok(g("away_goals", np.nan)):
                axf = float(g("away_goals"))
            if ok(g("home_goals", np.nan)):
                axa = float(g("home_goals"))

        # 5) 联赛均值回退
        if not ok(hxf):
            hxf = hxa = self.league_avg
        if not ok(axf):
            axf = axa = self.league_avg

        lam_h = (hxf + axa) / 2.0 * self.home_advantage
        lam_a = (axf + hxa) / 2.0
        # 防止 lambda 过大或为负
        lam_h = max(min(lam_h, 10.0), 0.0)
        lam_a = max(min(lam_a, 10.0), 0.0)
        return lam_h, lam_a

    def predict(self, match) -> dict | pd.DataFrame:
        """预测胜平负概率

        Args:
            match: 单场 (dict/Series) 或多场 (DataFrame)
        Returns:
            单场 -> {"p_home", "p_draw", "p_away", "expected_goals_home", "expected_goals_away"};
            多场 -> DataFrame (列同上, 索引对齐输入)
        """
        single = not isinstance(match, pd.DataFrame)
        rows = [match] if single else [row for _, row in match.iterrows()]
        out = []
        for r in rows:
            lam_h, lam_a = self._get_lambdas(r)
            mat = self._prob_matrix(lam_h, lam_a, self.rho)
            win = float(np.tril(mat, -1).sum())   # 主队进球 > 客队
            draw = float(np.trace(mat))           # 进球相等
            lose = float(np.triu(mat, 1).sum())   # 客队进球 > 主队
            out.append({
                "p_home": win,
                "p_draw": draw,
                "p_away": lose,
                "expected_goals_home": float(lam_h),
                "expected_goals_away": float(lam_a),
            })
        if single:
            return out[0]
        return pd.DataFrame(out, index=match.index)


# ============================================================
# 2. XGBoost 梯度提升模型
# ============================================================
class XGBoostModel:
    """基于 xgboost 的多分类 (胜/平/负) 梯度提升模型

    特征: xg_for, xg_against, xg_diff, ppda, ppda_diff, pressure_index,
          pressure_xg_interaction, elo_diff, xg_overperformance
    fit() 返回训练准确率, 并将 feature_importance 存为 DataFrame。
    """

    FEATURES = [
        "xg_for", "xg_against", "xg_diff",
        "ppda", "ppda_diff", "pressure_index", "pressure_xg_interaction",
        "elo_diff", "xg_overperformance",
    ]

    # 标签编码: 0=主胜, 1=平, 2=客胜 (主负)
    LABEL_WIN, LABEL_DRAW, LABEL_LOSE = 0, 1, 2

    def __init__(self, **params):
        self.model = None
        self.feature_importance: pd.DataFrame | None = None
        self.params: dict = {
            "n_estimators": 200,
            "max_depth": 4,
            "learning_rate": 0.1,
            "subsample": 0.9,
            "colsample_bytree": 0.9,
            "objective": "multi:softprob",
            "num_class": 3,
            "eval_metric": "mlogloss",
            "tree_method": "hist",
            "random_state": 42,
            "n_jobs": -1,
        }
        self.params.update(params)

    # ---------- 特征构建 ----------
    def _build_features(self, matches) -> pd.DataFrame:
        """从原始比赛数据或 FeatureBuilder 输出构建特征矩阵 (列顺序 = FEATURES)

        支持两种输入格式:
            1. FeatureBuilder 输出: home_xg_for, home_xg_against, home_xg_diff,
               home_ppda, away_ppda, elo_diff, home_xg_overperformance, away_xg_overperformance
            2. 原始比赛数据: home_xg, away_xg, home_goals, away_goals, home_ppda, away_ppda
        """
        df = matches if isinstance(matches, pd.DataFrame) else pd.DataFrame([matches])
        X = pd.DataFrame(index=df.index)

        # xg_for: 主队预期进球
        if "xg_for" in df.columns:
            X["xg_for"] = df["xg_for"]
        elif "home_xg_for" in df.columns:
            X["xg_for"] = df["home_xg_for"]
        elif "home_xg" in df.columns:
            X["xg_for"] = df["home_xg"]
        else:
            X["xg_for"] = np.nan

        # xg_against: 主队预期失球 (= 客队 xG)
        if "xg_against" in df.columns:
            X["xg_against"] = df["xg_against"]
        elif "home_xg_against" in df.columns:
            X["xg_against"] = df["home_xg_against"]
        elif "away_xg" in df.columns:
            X["xg_against"] = df["away_xg"]
        else:
            X["xg_against"] = np.nan

        # xg_diff: 预期进球差 (主 - 客)
        if "xg_diff" in df.columns:
            X["xg_diff"] = df["xg_diff"]
        elif "home_xg_diff" in df.columns:
            X["xg_diff"] = df["home_xg_diff"]
        else:
            X["xg_diff"] = X["xg_for"] - X["xg_against"]

        # ppda: PPDA 差 (主 - 客, 越低越激进)
        if "ppda" in df.columns:
            X["ppda"] = df["ppda"]
        elif "home_ppda" in df.columns and "away_ppda" in df.columns:
            X["ppda"] = df["home_ppda"] - df["away_ppda"]
        elif "home_ppda" in df.columns:
            X["ppda"] = df["home_ppda"]
        else:
            X["ppda"] = 0.0

        # ppda_diff: PPDA压迫优势差 (主队压迫优势 - 客队压迫优势)
        # 正值=主队压迫更强, 负值=客队压迫更强
        if "home_ppda_diff" in df.columns and "away_ppda_diff" in df.columns:
            X["ppda_diff"] = df["home_ppda_diff"] - df["away_ppda_diff"]
        elif "home_ppda_diff" in df.columns:
            X["ppda_diff"] = df["home_ppda_diff"]
        else:
            X["ppda_diff"] = 0.0

        # pressure_index: 压迫强度指数差 (主 - 客, 0-1范围)
        if "home_pressure_index" in df.columns and "away_pressure_index" in df.columns:
            X["pressure_index"] = df["home_pressure_index"] - df["away_pressure_index"]
        elif "home_pressure_index" in df.columns:
            X["pressure_index"] = df["home_pressure_index"]
        else:
            X["pressure_index"] = 0.0

        # pressure_xg_interaction: 压迫与xG的交互效应
        if "pressure_xg_interaction" in df.columns:
            X["pressure_xg_interaction"] = df["pressure_xg_interaction"]
        else:
            X["pressure_xg_interaction"] = X["ppda_diff"] * X["xg_diff"]

        # elo_diff: Elo 评级差 (主 - 客)
        if "elo_diff" in df.columns:
            X["elo_diff"] = df["elo_diff"]
        elif "elo_home_pre" in df.columns and "elo_away_pre" in df.columns:
            X["elo_diff"] = df["elo_home_pre"] - df["elo_away_pre"]
        elif "elo_home" in df.columns and "elo_away" in df.columns:
            X["elo_diff"] = df["elo_home"] - df["elo_away"]
        else:
            X["elo_diff"] = 0.0

        # xg_overperformance: xG 超额表现差 = (进球-xG)主 - (进球-xG)客
        if "xg_overperformance" in df.columns:
            X["xg_overperformance"] = df["xg_overperformance"]
        elif "home_xg_overperformance" in df.columns and "away_xg_overperformance" in df.columns:
            X["xg_overperformance"] = df["home_xg_overperformance"] - df["away_xg_overperformance"]
        elif "home_xg_overperformance" in df.columns:
            X["xg_overperformance"] = df["home_xg_overperformance"]
        elif all(c in df.columns for c in ["home_goals", "home_xg", "away_goals", "away_xg"]):
            X["xg_overperformance"] = (
                (df["home_goals"] - df["home_xg"]) - (df["away_goals"] - df["away_xg"])
            )
        else:
            X["xg_overperformance"] = 0.0

        # 填充 NaN 为 0 (XGBoost 不接受 NaN)
        X = X.fillna(0.0)
        return X[self.FEATURES]

    def _build_label(self, matches: pd.DataFrame) -> np.ndarray:
        """构建标签: 0=主胜, 1=平, 2=客胜"""
        if "home_goals" not in matches.columns or "away_goals" not in matches.columns:
            raise ValueError("XGBoostModel 需要 home_goals 与 away_goals 列以构建标签")
        hg = matches["home_goals"].to_numpy(dtype=float)
        ag = matches["away_goals"].to_numpy(dtype=float)
        y = np.where(hg > ag, self.LABEL_WIN, np.where(hg == ag, self.LABEL_DRAW, self.LABEL_LOSE))
        return y.astype(int)

    # ---------- 训练 ----------
    def fit(self, matches: pd.DataFrame, **kwargs) -> float:
        """训练 xgboost 模型

        Returns:
            训练集准确率 (accuracy)
        """
        import xgboost as xgb

        self.params.update(kwargs)
        X = self._build_features(matches)
        y = self._build_label(matches)

        mask = ~pd.isna(y)
        X, y = X.loc[mask], y[mask]
        if len(X) == 0:
            raise ValueError("XGBoostModel 无有效训练样本")

        self.model = xgb.XGBClassifier(**self.params)
        self.model.fit(X, y)

        pred = self.model.predict(X)
        acc = float(np.mean(pred == y))

        # 特征重要性存为 DataFrame
        importances = self.model.feature_importances_
        self.feature_importance = (
            pd.DataFrame({"feature": self.FEATURES, "importance": importances})
            .sort_values("importance", ascending=False)
            .reset_index(drop=True)
        )

        logger.info(
            "XGBoostModel 训练完成: 样本=%d, 准确率=%.4f, 重要特征=%s",
            len(X), acc,
            ", ".join(self.feature_importance.head(3)["feature"].tolist()),
        )
        return acc

    # ---------- 预测 ----------
    def predict(self, matches) -> dict | pd.DataFrame:
        """预测胜平负概率

        Returns:
            单场 -> {"win", "draw", "lose"};
            多场 -> DataFrame (列: win/draw/lose, 索引对齐输入)
        """
        if self.model is None:
            raise RuntimeError("XGBoostModel 尚未训练, 请先调用 fit()")
        single = not isinstance(matches, pd.DataFrame)
        X = self._build_features(matches)

        proba = self.model.predict_proba(X)  # (n, n_classes_seen)
        classes = list(self.model.classes_)  # 可能是 [0,1,2] 的子集
        full = np.zeros((len(X), 3), dtype=float)
        for idx, c in enumerate(classes):
            full[:, int(c)] = proba[:, idx]
        # 归一化 (防御性, 确保三列和为 1)
        s = full.sum(axis=1, keepdims=True)
        s[s == 0] = 1.0
        full = full / s

        if single:
            return {"p_home": float(full[0, 0]), "p_draw": float(full[0, 1]), "p_away": float(full[0, 2])}
        return pd.DataFrame(
            {"p_home": full[:, 0], "p_draw": full[:, 1], "p_away": full[:, 2]},
            index=matches.index,
        )


# ============================================================
# 3. Elo 评级模型
# ============================================================
class EloModel:
    """基于 Elo 评级差的胜平负概率模型

    主场胜率 (原始):
        P(home_win) = 1 / (1 + 10 ^ (-(R_h - R_a + HFA) / 400))
    平局概率:
        P(draw) = 0.28 - 0.10 * |P_win - P_lose|
    其中 P_win / P_lose 为非平局结果中主胜/客胜的相对份额, 最终归一化使三者和为 1。
    队伍势均力敌时平局概率最高 (0.28), 实力悬殊时最低 (0.18)。

    参数与 EloBuilder 保持一致: HFA=65, 初始评级=1500, K=20(联赛)/30(杯赛)。
    """

    HFA = 65                 # 主场优势 (Elo 点)
    INIT_RATING = 1500.0     # 初始评级
    K_LEAGUE = 20            # 联赛 K 因子
    K_CUP = 30               # 杯赛 K 因子

    CUP_LEAGUES = {
        "欧冠", "欧冠杯", "冠军联赛", "欧洲冠军联赛", "欧冠资格赛", "欧冠附",
        "欧罗巴", "欧联", "欧联杯", "欧洲联赛", "欧罗巴联赛",
        "欧协联", "欧协联杯", "欧洲协会联赛",
        "亚冠", "亚冠杯", "亚足联冠军联赛",
        "解放者杯", "南美解放者杯",
    }

    def __init__(self, home_field_advantage: float = HFA):
        self.HFA = home_field_advantage
        # 球队当前 Elo 评级: {team: rating}
        self.ratings: dict[str, float] = {}

    # ---------- 训练 ----------
    def fit(self, matches: pd.DataFrame) -> "EloModel":
        """从比赛历史构建 Elo 评级

        Args:
            matches: 比赛数据, 需含 home_team, away_team, home_goals, away_goals,
                     date (可选), league (可选, 用于区分杯赛 K 因子)
        """
        df = matches.copy()
        if "date" in df.columns:
            df = df.sort_values("date").reset_index(drop=True)

        self.ratings = {}
        for _, m in df.iterrows():
            home = m.get("home_team", "")
            away = m.get("away_team", "")
            hg = m.get("home_goals")
            ag = m.get("away_goals")
            league = m.get("league", "") or m.get("league_cn", "")

            if hg is None or ag is None or not home or not away:
                continue

            r_home = self.ratings.get(home, self.INIT_RATING)
            r_away = self.ratings.get(away, self.INIT_RATING)

            diff = r_home - r_away + self.HFA
            e_home = 1.0 / (1.0 + 10.0 ** (-diff / 400.0))

            # 实际结果: 1=主胜, 0.5=平, 0=主负
            if hg > ag:
                actual = 1.0
            elif hg == ag:
                actual = 0.5
            else:
                actual = 0.0

            k = self.K_CUP if league in self.CUP_LEAGUES else self.K_LEAGUE
            delta = k * (actual - e_home)
            self.ratings[home] = r_home + delta
            self.ratings[away] = r_away - delta

        logger.info("EloModel 拟合完成: 球队数=%d, HFA=%d", len(self.ratings), self.HFA)
        return self

    # ---------- 评级取值 ----------
    def _get_rating_diff(self, row) -> float:
        """获取主客队评级差 (R_h - R_a), 依次回退:
        elo_diff -> elo_home_pre/elo_away_pre -> 球队评级表 -> 0
        """
        def g(key, default=None):
            if isinstance(row, dict):
                return row.get(key, default)
            if isinstance(row, pd.Series):
                return row.get(key, default)
            return getattr(row, key, default)

        def ok(v):
            return v is not None and not (isinstance(v, float) and math.isnan(v))

        if ok(g("elo_diff", None)):
            return float(g("elo_diff"))
        eh = g("elo_home_pre", g("home_elo", None))
        ea = g("elo_away_pre", g("away_elo", None))
        if ok(eh) and ok(ea):
            return float(eh) - float(ea)

        home = g("home_team", g("team_home", None))
        away = g("away_team", g("team_away", None))
        r_home = self.ratings.get(home, self.INIT_RATING) if home else self.INIT_RATING
        r_away = self.ratings.get(away, self.INIT_RATING) if away else self.INIT_RATING
        return r_home - r_away

    # ---------- 预测 ----------
    def _probs_from_diff(self, diff: float) -> dict:
        """由评级差计算胜平负概率"""
        # 原始主胜份额 (非平局结果中主队的相对胜率)
        p_win_raw = 1.0 / (1.0 + 10.0 ** (-(diff + self.HFA) / 400.0))
        p_lose_raw = 1.0 - p_win_raw

        # 平局概率: 势均力敌 -> 0.28, 悬殊 -> 0.18
        p_draw = 0.28 - 0.10 * abs(p_win_raw - p_lose_raw)
        p_draw = max(0.0, min(p_draw, 1.0))

        # 主胜/客胜按原始份额瓜分剩余概率, 保证三者和为 1
        remaining = 1.0 - p_draw
        p_win = p_win_raw * remaining
        p_lose = p_lose_raw * remaining
        return {"p_home": float(p_win), "p_draw": float(p_draw), "p_away": float(p_lose)}

    def predict(self, match) -> dict | pd.DataFrame:
        """预测胜平负概率

        Args:
            match: 单场 (dict/Series) 或多场 (DataFrame), 可直接提供 elo_diff
        Returns:
            单场 -> {"p_home", "p_draw", "p_away"};
            多场 -> DataFrame (列: p_home/p_draw/p_away, 索引对齐输入)
        """
        if isinstance(match, pd.DataFrame):
            diffs = np.array([self._get_rating_diff(row) for _, row in match.iterrows()])
            p_win_raw = 1.0 / (1.0 + 10.0 ** (-(diffs + self.HFA) / 400.0))
            p_lose_raw = 1.0 - p_win_raw
            p_draw = 0.28 - 0.10 * np.abs(p_win_raw - p_lose_raw)
            p_draw = np.clip(p_draw, 0.0, 1.0)
            remaining = 1.0 - p_draw
            p_win = p_win_raw * remaining
            p_lose = p_lose_raw * remaining
            return pd.DataFrame(
                {"p_home": p_win, "p_draw": p_draw, "p_away": p_lose},
                index=match.index,
            )
        return self._probs_from_diff(self._get_rating_diff(match))


__all__ = ["PoissonModel", "XGBoostModel", "EloModel"]
