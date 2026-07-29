"""football-prediction-pipeline · 特征工程

计算滚动 xG/xGA 统计、PPDA 防守压迫指标、xG 超额表现、
交叉验证质量等特征, 支持贝叶斯收缩处理小样本。

数据来源优先级:
    1. Understat  — 真实 xG / PPDA / 进球 (首选)
    2. 比赛结果    — 实际进球 (Understat 缺失时回退, 标记 has_xg_data=False)
    3. 联赛先验    — 无任何历史时使用 (最大贝叶斯收缩)

核心特征列:
    team_home, team_away, date, league
    home_xg_for, home_xg_against, home_xg_diff
    away_xg_for, away_xg_against, away_xg_diff
    home_ppda, away_ppda
    home_xg_overperformance, away_xg_overperformance
    xg_cv_quality (0-1), has_xg_data (bool)
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from src.config import config, TEAM_NAME_MAP, cn_to_en_team, is_big5_league

logger = logging.getLogger("features")


class FeatureBuilder:
    """特征构建器

    从比赛数据、Elo 评级和 Understat xG 数据构建预测特征。

    核心特征:
        - 滚动 xG/xGA (默认窗口=10场):
            avg_xg_for      球队近N场平均预期进球
            avg_xg_against  球队近N场平均预期失球
            avg_xg_diff     预期进球差 (= for - against)
        - 滚动 PPDA (每次防守动作允许传球数, 越低越激进)
        - xG 超额表现 = 实际进球 - xG  (正值=超额/运气好)
        - 交叉验证质量 (xG 与实际进球偏离度, 0-1)
        - 贝叶斯收缩 (小样本向联赛均值收缩, k=10)

    无 Understat 数据时, 回退到实际进球作为 xG 代理, 并标记 has_xg_data=False。
    """

    def __init__(self, window: int | None = None, bayes_k: int | None = None):
        """初始化特征构建器

        Args:
            window:  滚动统计窗口 (场), 默认取 config.rolling_window = 10
            bayes_k: 贝叶斯收缩强度 (伪观测数), 默认取 config.bayes_k = 10
        """
        self.window = window if window is not None else config.rolling_window
        self.bayes_k = bayes_k if bayes_k is not None else config.bayes_k
        # 球队历史缓存: {球队名 -> 按日期排序的 DataFrame}
        self._team_history: dict[str, pd.DataFrame] = {}
        # 联赛先验 (贝叶斯收缩用)
        self._priors: dict[str, float] = {}

    # ================================================================
    # 公开接口
    # ================================================================
    def build(
        self,
        matches: pd.DataFrame,
        elo_df: pd.DataFrame | None,
        understat_df: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        """构建特征矩阵

        Args:
            matches:      目标比赛 DataFrame
                          需含队名 (team_home/team_away 或 home_team/away_team)、
                          日期 (date/match_date)、联赛 (league/league_cn)
            elo_df:       Elo 评级 DataFrame
                          (含 home_team, away_team, date, elo_home_pre 等)
            understat_df: Understat xG 数据 (可选)
                          列: home_team, away_team, home_team_cn, away_team_cn,
                              home_xg, away_xg, home_goals, away_goals,
                              home_ppda, away_ppda, match_date, league_cn, date

        Returns:
            特征 DataFrame, 每行对应一场比赛
        """
        if matches is None or matches.empty:
            logger.warning("特征构建: 输入比赛数据为空")
            return pd.DataFrame()

        logger.info(
            f"特征构建: 开始处理 {len(matches)} 场比赛, "
            f"Understat 数据={'有' if understat_df is not None else '无'}, "
            f"Elo 数据={'有' if elo_df is not None else '无'}"
        )

        # 1. 标准化比赛列名
        df = self._normalize_matches(matches)

        # 2. 构建球队历史 (用于滚动统计)
        history = self._build_team_history(understat_df, df)

        # 3. 计算联赛先验 (贝叶斯收缩用)
        self._priors = self._compute_league_priors(history)

        # 4. 预排序球队历史, 构建查找索引
        self._index_team_history(history)

        # 5. 逐场计算特征
        features = self._compute_features(df)

        # 6. 合并 Elo 特征
        if elo_df is not None and not elo_df.empty:
            features = self._merge_elo(features, elo_df)
        else:
            logger.warning("特征构建: 无 Elo 数据, 跳过 Elo 特征")
            features["elo_home"] = np.nan
            features["elo_away"] = np.nan
            features["elo_diff"] = np.nan

        # 7. 后处理: 列顺序、类型、清理
        features = self._finalize(features)

        logger.info(
            f"特征构建: 完成, 输出 {len(features)} 行 × {len(features.columns)} 列, "
            f"有xG数据占比: {features['has_xg_data'].mean():.1%}"
            if not features.empty and "has_xg_data" in features.columns
            else f"特征构建: 完成, 输出 {len(features)} 行"
        )
        return features

    # ================================================================
    # 列名标准化
    # ================================================================
    def _normalize_matches(self, matches: pd.DataFrame) -> pd.DataFrame:
        """标准化比赛 DataFrame 列名, 统一为内部格式

        内部格式:
            team_home, team_away — 队名 (中/英文均可)
            date                 — 比赛日期 (datetime)
            league               — 联赛 (中文)
        """
        df = matches.copy()

        # 列名映射: 支持多种命名约定
        # 注意: home_team 优先于 home_team_cn (英文队名是标准格式, 用于历史查找)
        col_map = {}
        if "home_team" in df.columns:
            col_map["home_team"] = "team_home"
        elif "home_team_cn" in df.columns:
            col_map["home_team_cn"] = "team_home"
        if "away_team" in df.columns:
            col_map["away_team"] = "team_away"
        elif "away_team_cn" in df.columns:
            col_map["away_team_cn"] = "team_away"
        if "league_cn" in df.columns and "league" not in df.columns:
            col_map["league_cn"] = "league"
        if "league_en" in df.columns:
            col_map["league_en"] = "league_en"
        # match_date → date (仅当 date 列不存在时, 避免重复列)
        if "match_date" in df.columns and "date" not in df.columns:
            col_map["match_date"] = "date"
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

        # 日期解析 (处理 index 中的 date 和 column 中的 date)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
        elif df.index.name == "date":
            df["date"] = pd.to_datetime(df.index, errors="coerce")
        else:
            # 尝试从 index 中提取 date (Understat 多级索引含 date)
            if isinstance(df.index, pd.MultiIndex) and "date" in df.index.names:
                df["date"] = pd.to_datetime(df.index.get_level_values("date"), errors="coerce")
            else:
                df["date"] = pd.NaT

        # 确保必要列存在
        for col in ["team_home", "team_away", "league"]:
            if col not in df.columns:
                df[col] = None

        # 大五联赛标记
        if "league" in df.columns:
            df["is_big5"] = df["league"].apply(
                lambda x: is_big5_league(x) if pd.notna(x) else False
            )
        else:
            df["is_big5"] = False

        return df

    # ================================================================
    # 球队历史构建
    # ================================================================
    def _build_team_history(
        self,
        understat_df: pd.DataFrame | None,
        matches: pd.DataFrame,
    ) -> pd.DataFrame:
        """构建统一的球队比赛历史 (每支球队每场比赛一条记录)

        数据优先级:
            1. Understat (真实 xG / PPDA) — 首选
            2. 比赛结果 (实际进球, 作为 xG 回退) — 仅补充 Understat 未覆盖的球队

        Returns:
            DataFrame, 列: team, team_cn, date, xg_for, xg_against,
                          goals_for, goals_against, ppda, has_xg, league, is_home
        """
        frames: list[pd.DataFrame] = []

        # --- 1. 从 Understat 构建 (首选, 含真实 xG) ---
        if understat_df is not None and not understat_df.empty:
            us = understat_df.copy()

            # 确保必要列存在
            for col in [
                "home_team", "away_team", "home_xg", "away_xg",
                "home_goals", "away_goals", "home_ppda", "away_ppda",
            ]:
                if col not in us:
                    us[col] = np.nan
            if "home_team_cn" not in us:
                us["home_team_cn"] = None
            if "away_team_cn" not in us:
                us["away_team_cn"] = None
            if "league_cn" not in us:
                us["league_cn"] = ""

            # 解析日期 (优先 date 列, 其次 match_date)
            if "date" in us:
                dates = pd.to_datetime(us["date"], errors="coerce")
            elif "match_date" in us:
                dates = pd.to_datetime(us["match_date"], errors="coerce")
            else:
                dates = pd.Series(pd.NaT, index=us.index)

            # xG 有效性标记
            has_xg_mask = us["home_xg"].notna() & us["away_xg"].notna()

            # 主队视角记录
            home_df = pd.DataFrame({
                "team": us["home_team"].values,
                "team_cn": us["home_team_cn"].values,
                "date": dates.values,
                "xg_for": us["home_xg"].values,
                "xg_against": us["away_xg"].values,
                "goals_for": us["home_goals"].values,
                "goals_against": us["away_goals"].values,
                "ppda": us["home_ppda"].values,
                "opp_ppda": us["away_ppda"].values,
                "has_xg": has_xg_mask.values,
                "league": us["league_cn"].values,
                "is_home": True,
            })

            # 客队视角记录
            away_df = pd.DataFrame({
                "team": us["away_team"].values,
                "team_cn": us["away_team_cn"].values,
                "date": dates.values,
                "xg_for": us["away_xg"].values,
                "xg_against": us["home_xg"].values,
                "goals_for": us["away_goals"].values,
                "goals_against": us["home_goals"].values,
                "ppda": us["away_ppda"].values,
                "opp_ppda": us["home_ppda"].values,
                "has_xg": has_xg_mask.values,
                "league": us["league_cn"].values,
                "is_home": False,
            })

            frames = [home_df, away_df]
            logger.info(
                f"  球队历史: 从 Understat 构建 "
                f"{len(home_df) + len(away_df)} 条记录 "
                f"({has_xg_mask.sum()} 场有真实 xG)"
            )

        # --- 2. 从比赛结果补充 (Understat 无数据时回退到进球) ---
        # 收集已有球队 (Understat 英文名)
        existing_teams: set[str] = set()
        for f in frames:
            existing_teams.update(f["team"].dropna().astype(str).unique())

        if matches is not None and not matches.empty:
            # 重置索引, 确保迭代安全
            matches_flat = matches.reset_index(drop=True)
            # 仅当比赛数据含进球列时才补充
            has_goals = "home_goals" in matches_flat.columns and "away_goals" in matches_flat.columns
            if has_goals:
                supp_records = []
                for idx in range(len(matches_flat)):
                    row_data = matches_flat.iloc[idx].to_dict()
                    date = row_data.get("date")
                    _ht = row_data.get("team_home")
                    _at = row_data.get("team_away")
                    home_raw = str(_ht).strip() if _ht is not None and str(_ht) != 'nan' else ""
                    away_raw = str(_at).strip() if _at is not None and str(_at) != 'nan' else ""
                    home = self._resolve_team_name(home_raw, existing_teams)
                    away = self._resolve_team_name(away_raw, existing_teams)
                    hg = row_data.get("home_goals")
                    ag = row_data.get("away_goals")
                    league = row_data.get("league", "")
                    if league is None or str(league) == 'nan':
                        league = ""

                    # 主队: 不在 Understat 历史中, 且有进球数据 → 用进球回退
                    if home and home not in existing_teams and pd.notna(hg):
                        supp_records.append({
                            "team": home,
                            "team_cn": home_raw,
                            "date": date,
                            "xg_for": hg,       # 回退: 用进球代替 xG
                            "xg_against": ag,
                            "goals_for": hg,
                            "goals_against": ag,
                            "ppda": np.nan,     # 无 PPDA 数据
                            "opp_ppda": np.nan,
                            "has_xg": False,
                            "league": league,
                            "is_home": True,
                        })
                    # 客队: 同理
                    if away and away not in existing_teams and pd.notna(ag):
                        supp_records.append({
                            "team": away,
                            "team_cn": away_raw,
                            "date": date,
                            "xg_for": ag,       # 回退: 用进球代替 xG
                            "xg_against": hg,
                            "goals_for": ag,
                            "goals_against": hg,
                            "ppda": np.nan,
                            "opp_ppda": np.nan,
                            "has_xg": False,
                            "league": league,
                            "is_home": False,
                        })

                if supp_records:
                    frames.append(pd.DataFrame(supp_records))
                    logger.info(
                        f"  球队历史: 从比赛结果补充 {len(supp_records)} 条记录 "
                        f"(进球回退, 无 xG)"
                    )

        if not frames:
            logger.warning("  球队历史: 无可用历史数据")
            return pd.DataFrame()

        # 合并、去重、排序
        history = pd.concat(frames, ignore_index=True)
        history["date"] = pd.to_datetime(history["date"], errors="coerce")
        history = history.dropna(subset=["date", "team"])
        history["team"] = history["team"].astype(str)
        # 同队同日仅保留一条 (优先保留有 xG 的)
        history = history.sort_values(
            ["team", "date", "has_xg"], ascending=[True, True, False]
        )
        history = history.drop_duplicates(subset=["team", "date"], keep="first")
        history = history.sort_values(["team", "date"]).reset_index(drop=True)

        n_xg = history["has_xg"].sum()
        logger.info(
            f"  球队历史: 共 {len(history)} 条, "
            f"{history['team'].nunique()} 支球队, "
            f"有xG: {n_xg} 条 ({n_xg / len(history):.1%})"
        )
        return history

    # ================================================================
    # 联赛先验计算 (贝叶斯收缩用)
    # ================================================================
    def _compute_league_priors(self, history: pd.DataFrame) -> dict[str, float]:
        """计算联赛级先验均值, 用于贝叶斯收缩

        先验代表 "联赛平均水平的球队" 的各项统计期望值。
        小样本球队的估计会向先验收缩。

        Returns:
            dict: {
                'xg_for':          联赛平均预期进球,
                'xg_against':      联赛平均预期失球 (= xg_for, 对称性),
                'ppda':            联赛平均 PPDA,
                'overperformance': 联赛平均超额表现 (≈0, 回归均值),
            }
        """
        # 默认先验 (五大联赛经验值)
        priors = {
            "xg_for": 1.35,          # 五大联赛场均约 1.35 球
            "xg_against": 1.35,
            "ppda": 10.0,            # 典型 PPDA 约 8-12
            "overperformance": 0.0,  # 长期超额表现回归 0
        }

        if history is None or history.empty:
            return priors

        # 优先使用有真实 xG 的记录计算先验
        has_xg_hist = history[history["has_xg"]]
        if not has_xg_hist.empty:
            priors["xg_for"] = float(has_xg_hist["xg_for"].mean())
            priors["xg_against"] = float(has_xg_hist["xg_against"].mean())
            priors["ppda"] = float(has_xg_hist["ppda"].mean())
            # 超额表现先验 = 平均(进球 - xG)
            overperf = has_xg_hist["goals_for"] - has_xg_hist["xg_for"]
            priors["overperformance"] = float(overperf.mean())
        else:
            # 无 xG 数据, 用进球数据计算先验
            priors["xg_for"] = float(history["goals_for"].mean())
            priors["xg_against"] = float(history["goals_against"].mean())

        # 处理 NaN
        for key, val in priors.items():
            if pd.isna(val):
                if key == "overperformance":
                    priors[key] = 0.0
                elif key == "ppda":
                    priors[key] = 10.0
                else:
                    priors[key] = 1.35

        logger.info(
            f"  联赛先验: xg_for={priors['xg_for']:.3f}, "
            f"xg_against={priors['xg_against']:.3f}, "
            f"ppda={priors['ppda']:.2f}, "
            f"overperf={priors['overperformance']:.3f}"
        )
        return priors

    # ================================================================
    # 球队历史索引 (加速查找)
    # ================================================================
    def _index_team_history(self, history: pd.DataFrame):
        """按球队分组并预排序, 构建查找用的字典索引"""
        self._team_history = {}
        if history is None or history.empty:
            return
        for team, grp in history.groupby("team"):
            self._team_history[team] = grp.sort_values("date").reset_index(drop=True)

    # ================================================================
    # 滚动统计计算
    # ================================================================
    def _get_recent_games(
        self, team: str, target_date, n: int
    ) -> pd.DataFrame:
        """获取球队在目标日期前的最近 n 场比赛

        严格使用 date < target_date 过滤, 防止数据泄露 (不包含当天比赛)。

        Args:
            team:        球队名 (Understat 英文格式)
            target_date: 目标日期
            n:           取最近 n 场
        Returns:
            该球队最近的比赛记录 DataFrame (可能不足 n 场)
        """
        hist = self._team_history.get(team)
        if hist is None or hist.empty:
            return pd.DataFrame()

        if pd.isna(target_date):
            return pd.DataFrame()

        # 仅取目标日期之前的比赛 (严格小于, 避免泄露)
        past = hist[hist["date"] < target_date]
        if past.empty:
            return pd.DataFrame()

        return past.tail(n)

    def _compute_rolling_stats(self, team: str, target_date) -> dict:
        """计算球队在目标日期前的滚动统计

        流程:
            1. 获取近 window 场历史
            2. 计算样本均值 (xG/xGA/PPDA/对手PPDA/超额表现)
            3. 贝叶斯收缩: shrunken = (n * sample + k * prior) / (n + k)
            4. 交叉验证质量: xG 与进球偏离度
            5. PPDA压迫强度衍生特征 (压迫指数/稳定性/优势差)

        Returns:
            dict: {
                'xg_for', 'xg_against', 'xg_diff',
                'ppda', 'opp_ppda', 'ppda_diff',
                'pressure_index', 'ppda_stability',
                'overperformance',
                'cv_quality', 'has_xg', 'n_games'
            }
        """
        result = {
            "xg_for": np.nan,
            "xg_against": np.nan,
            "xg_diff": np.nan,
            "ppda": np.nan,
            "opp_ppda": np.nan,
            "ppda_diff": np.nan,
            "pressure_index": np.nan,
            "ppda_stability": 0.5,
            "overperformance": np.nan,
            "cv_quality": 0.0,
            "has_xg": False,
            "n_games": 0,
        }

        # 检查球队是否有 Understat xG 数据 (查全部历史, 非仅窗口内)
        team_hist = self._team_history.get(team)
        if team_hist is not None and not team_hist.empty:
            result["has_xg"] = bool(team_hist["has_xg"].any())

        recent = self._get_recent_games(team, target_date, self.window)

        # --- 无历史: 使用联赛先验 (最大收缩) ---
        if recent.empty:
            result["xg_for"] = self._priors.get("xg_for", 1.35)
            result["xg_against"] = self._priors.get("xg_against", 1.35)
            result["xg_diff"] = result["xg_for"] - result["xg_against"]
            result["ppda"] = self._priors.get("ppda", 10.0)
            result["opp_ppda"] = self._priors.get("ppda", 10.0)
            result["ppda_diff"] = 0.0
            result["pressure_index"] = 0.5  # 中等压迫
            result["overperformance"] = 0.0
            return result

        n = len(recent)
        result["n_games"] = n
        k = self.bayes_k  # 贝叶斯收缩强度 (伪观测数)

        # --- 样本均值 (向量化计算) ---
        # xG: 优先用真实 xG, 缺失则回退到实际进球
        xg_for_vals = recent["xg_for"].fillna(recent["goals_for"])
        xg_against_vals = recent["xg_against"].fillna(recent["goals_against"])

        sample_xg_for = float(xg_for_vals.mean())
        sample_xg_against = float(xg_against_vals.mean())

        # PPDA (自身压迫强度)
        ppda_vals = recent["ppda"].dropna()
        sample_ppda = float(ppda_vals.mean()) if not ppda_vals.empty else np.nan

        # 对手PPDA (对方压迫强度)
        if "opp_ppda" in recent.columns:
            opp_ppda_vals = recent["opp_ppda"].dropna()
            sample_opp_ppda = float(opp_ppda_vals.mean()) if not opp_ppda_vals.empty else np.nan
        else:
            sample_opp_ppda = np.nan

        # PPDA稳定性: 基于标准差的归一化
        if len(ppda_vals) >= 3:
            ppda_mean = float(ppda_vals.mean())
            ppda_std = float(ppda_vals.std())
            cv = ppda_std / ppda_mean if ppda_mean > 0 else 1.0
            result["ppda_stability"] = float(np.clip(1.0 / (1.0 + cv), 0.0, 1.0))
        else:
            result["ppda_stability"] = 0.5

        # 超额表现 = 实际进球 - xG (正值=超额/运气好)
        overperf_vals = (recent["goals_for"] - recent["xg_for"]).fillna(0.0)
        sample_overperf = float(overperf_vals.mean())

        # --- 贝叶斯收缩 ---
        # 公式: shrunken = (n * sample_mean + k * prior) / (n + k)
        # n 小时向先验收缩, n 大时趋向样本均值
        prior_xg_for = self._priors.get("xg_for", 1.35)
        prior_xg_against = self._priors.get("xg_against", 1.35)
        prior_ppda = self._priors.get("ppda", 10.0)
        prior_overperf = self._priors.get("overperformance", 0.0)

        result["xg_for"] = (n * sample_xg_for + k * prior_xg_for) / (n + k)
        result["xg_against"] = (
            n * sample_xg_against + k * prior_xg_against
        ) / (n + k)
        result["xg_diff"] = result["xg_for"] - result["xg_against"]

        if not np.isnan(sample_ppda):
            result["ppda"] = (n * sample_ppda + k * prior_ppda) / (n + k)
        else:
            result["ppda"] = prior_ppda

        if not np.isnan(sample_opp_ppda):
            result["opp_ppda"] = (n * sample_opp_ppda + k * prior_ppda) / (n + k)
        else:
            result["opp_ppda"] = prior_ppda

        # PPDA优势差: 对手PPDA - 自身PPDA (正值=自身压迫更强)
        result["ppda_diff"] = result["opp_ppda"] - result["ppda"]

        # 压迫强度指数 (0-1, 1=最激进)
        # sigmoid映射: pressure = 1 / (1 + exp((ppda - 11) / 3))
        import math
        ppda_val = result["ppda"]
        if not np.isnan(ppda_val):
            result["pressure_index"] = float(
                1.0 / (1.0 + math.exp((ppda_val - 11.0) / 3.0))
            )

        result["overperformance"] = (
            n * sample_overperf + k * prior_overperf
        ) / (n + k)

        # --- 交叉验证质量 ---
        result["cv_quality"] = self._compute_cv_quality(recent)

        return result

    # ================================================================
    # 交叉验证质量
    # ================================================================
    def _compute_cv_quality(self, recent: pd.DataFrame) -> float:
        """计算 xG 交叉验证质量 (0-1)

        比较 Understat xG 与实际进球的偏离程度。
        偏离越小 → 质量越高 (接近 1); 偏离越大 → 质量越低 (接近 0)。

        方法: 基于平均绝对误差 (MAE) 归一化
            quality = 1 / (1 + mae)

        足球中 xG 与进球 MAE 通常 0.3-1.5:
            MAE=0.3 → quality≈0.77 (高质量)
            MAE=1.0 → quality≈0.50 (中等)
            MAE=1.5 → quality≈0.40 (低质量)

        无 xG 数据时返回 0。

        Args:
            recent: 球队最近比赛记录
        Returns:
            质量 (0-1)
        """
        if recent is None or recent.empty:
            return 0.0

        # 仅使用有真实 xG 的记录
        has_xg = recent[recent["has_xg"]]
        if has_xg.empty:
            return 0.0

        xg = has_xg["xg_for"].values
        goals = has_xg["goals_for"].values

        # 过滤 NaN
        mask = ~(np.isnan(xg) | np.isnan(goals))
        xg = xg[mask]
        goals = goals[mask]

        if len(xg) == 0:
            return 0.0

        # 平均绝对误差: |实际进球 - xG|
        mae = float(np.mean(np.abs(goals - xg)))

        # 归一化到 0-1: MAE 越小质量越高
        quality = 1.0 / (1.0 + mae)

        return float(np.clip(quality, 0.0, 1.0))

    # ================================================================
    # 逐场特征计算
    # ================================================================
    def _compute_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """对每场比赛计算特征

        遍历目标比赛, 分别计算主队和客队的滚动统计,
        汇总为特征行。
        """
        rows = []

        # 收集历史中已有的球队名 (用于解析)
        known_teams = set(self._team_history.keys())

        df_flat = df.reset_index(drop=True)
        for idx in range(len(df_flat)):
            m = df_flat.iloc[idx].to_dict()
            target_date = m.get("date")
            _ht = m.get("team_home")
            _at = m.get("team_away")
            home_raw = str(_ht).strip() if _ht is not None and str(_ht) != 'nan' else ""
            away_raw = str(_at).strip() if _at is not None and str(_at) != 'nan' else ""

            # 解析为 Understat 英文队名 (用于历史查找)
            home_team = self._resolve_team_name(home_raw, known_teams)
            away_team = self._resolve_team_name(away_raw, known_teams)

            # 兜底: 若无法解析, 保留原始名
            if not home_team:
                home_team = home_raw or None
            if not away_team:
                away_team = away_raw or None

            _lg = m.get("league", "")
            if _lg is None or str(_lg) == 'nan':
                _lg = ""

            row = {
                "team_home": home_raw,
                "team_away": away_raw,
                "date": target_date,
                "league": _lg,
            }

            # 保留原始比赛数据 (进球/xG, 用于模型训练标签和 Poisson 校准)
            for src_key, dst_key in [
                ("home_goals", "home_goals"), ("away_goals", "away_goals"),
                ("home_xg", "home_xg"), ("away_xg", "away_xg"),
            ]:
                val = m.get(src_key)
                if val is not None and str(val) != 'nan':
                    row[dst_key] = val

            # 大五联赛标记
            row["is_big5"] = is_big5_league(_lg) if _lg else False

            # --- 计算主队滚动统计 ---
            if home_team and pd.notna(target_date):
                home_stats = self._compute_rolling_stats(home_team, target_date)
            else:
                home_stats = self._empty_stats()

            # --- 计算客队滚动统计 ---
            if away_team and pd.notna(target_date):
                away_stats = self._compute_rolling_stats(away_team, target_date)
            else:
                away_stats = self._empty_stats()

            # --- 填充特征列 ---
            # 滚动 xG/xGA
            row["home_xg_for"] = home_stats["xg_for"]
            row["home_xg_against"] = home_stats["xg_against"]
            row["home_xg_diff"] = home_stats["xg_diff"]
            row["away_xg_for"] = away_stats["xg_for"]
            row["away_xg_against"] = away_stats["xg_against"]
            row["away_xg_diff"] = away_stats["xg_diff"]

            # 滚动 PPDA
            row["home_ppda"] = home_stats["ppda"]
            row["away_ppda"] = away_stats["ppda"]

            # PPDA压迫强度衍生特征
            row["home_opp_ppda"] = home_stats["opp_ppda"]
            row["away_opp_ppda"] = away_stats["opp_ppda"]
            row["home_ppda_diff"] = home_stats["ppda_diff"]
            row["away_ppda_diff"] = away_stats["ppda_diff"]
            row["home_pressure_index"] = home_stats["pressure_index"]
            row["away_pressure_index"] = away_stats["pressure_index"]
            row["home_ppda_stability"] = home_stats["ppda_stability"]
            row["away_ppda_stability"] = away_stats["ppda_stability"]
            # 压迫强度交互项: PPDA差 × xG差 (压迫优势与进攻优势的协同效应)
            row["pressure_xg_interaction"] = (
                (home_stats["ppda_diff"] or 0) * (home_stats["xg_diff"] or 0)
            )

            # xG 超额表现 = 实际进球 - xG
            row["home_xg_overperformance"] = home_stats["overperformance"]
            row["away_xg_overperformance"] = away_stats["overperformance"]

            # 交叉验证质量: 取两队平均
            row["xg_cv_quality"] = (
                home_stats["cv_quality"] + away_stats["cv_quality"]
            ) / 2.0

            # 是否有真实 xG 数据: 两队均有才为 True
            row["has_xg_data"] = bool(
                home_stats["has_xg"] and away_stats["has_xg"]
            )

            # 辅助信息 (透明度)
            row["home_n_games"] = home_stats["n_games"]
            row["away_n_games"] = away_stats["n_games"]
            row["home_has_xg"] = bool(home_stats["has_xg"])
            row["away_has_xg"] = bool(away_stats["has_xg"])

            rows.append(row)

        return pd.DataFrame(rows)

    def _empty_stats(self) -> dict:
        """返回空统计 (无历史数据时使用联赛先验)"""
        return {
            "xg_for": self._priors.get("xg_for", 1.35),
            "xg_against": self._priors.get("xg_against", 1.35),
            "xg_diff": 0.0,
            "ppda": self._priors.get("ppda", 10.0),
            "opp_ppda": self._priors.get("ppda", 10.0),
            "ppda_diff": 0.0,
            "pressure_index": 0.5,
            "ppda_stability": 0.5,
            "overperformance": 0.0,
            "cv_quality": 0.0,
            "has_xg": False,
            "n_games": 0,
        }

    # ================================================================
    # Elo 特征合并
    # ================================================================
    def _merge_elo(
        self, features: pd.DataFrame, elo_df: pd.DataFrame
    ) -> pd.DataFrame:
        """合并 Elo 评级特征

        elo_df 列 (来自 EloBuilder.build):
            home_team, away_team, date, league,
            elo_home_pre, elo_away_pre, elo_diff,
            elo_home_post, elo_away_post

        合并策略:
            1. 精确匹配 (队名 + 日期)
            2. 匹配率低时回退到模糊匹配 (队名 + 最近日期)
        """
        elo = elo_df.copy()
        elo["date"] = pd.to_datetime(elo["date"], errors="coerce")

        # 标准化队名列名
        if "home_team" in elo.columns and "team_home" not in elo.columns:
            elo = elo.rename(
                columns={"home_team": "team_home", "away_team": "team_away"}
            )

        # 选择 Elo 相关列
        elo_keep = ["team_home", "team_away", "date"]
        rename_map = {}
        for c in ["elo_home_pre", "elo_away_pre", "elo_diff"]:
            if c in elo.columns:
                elo_keep.append(c)
        # 重命名为简短形式
        rename_map = {
            "elo_home_pre": "elo_home",
            "elo_away_pre": "elo_away",
        }

        if not all(c in elo.columns for c in ["team_home", "team_away", "date"]):
            logger.warning("  Elo 合并: 列名不匹配, 跳过")
            features["elo_home"] = np.nan
            features["elo_away"] = np.nan
            features["elo_diff"] = np.nan
            return features

        elo_sel = elo[elo_keep].rename(columns=rename_map).copy()
        # 若 elo_diff 不在原数据中, 用 pre 差值计算
        if "elo_diff" not in elo_sel.columns and "elo_home" in elo_sel.columns:
            elo_sel["elo_diff"] = elo_sel["elo_home"] - elo_sel["elo_away"]

        # 确保特征 date 为 datetime
        features["date"] = pd.to_datetime(features["date"], errors="coerce")

        # --- 1. 精确匹配 ---
        merged = features.merge(
            elo_sel,
            on=["team_home", "team_away", "date"],
            how="left",
            suffixes=("", "_elo"),
        )

        match_rate = merged["elo_home"].notna().mean() if "elo_home" in merged.columns else 0.0

        # --- 2. 匹配率低时模糊匹配 ---
        if match_rate < 0.5:
            logger.info(f"  Elo 精确匹配率 {match_rate:.1%}, 尝试模糊匹配")
            merged = self._merge_elo_fuzzy(features, elo_sel)
            match_rate = (
                merged["elo_home"].notna().mean()
                if "elo_home" in merged.columns
                else 0.0
            )

        logger.info(f"  Elo 合并: 匹配率 {match_rate:.1%}")
        return merged

    def _merge_elo_fuzzy(
        self, features: pd.DataFrame, elo_sel: pd.DataFrame
    ) -> pd.DataFrame:
        """模糊匹配 Elo (按队名 + 最近日期)

        当精确日期匹配失败时, 找同队名组合中最近的不超过目标日期的 Elo 记录。
        """
        result = features.copy()
        for c in ["elo_home", "elo_away", "elo_diff"]:
            if c not in result.columns:
                result[c] = np.nan
            else:
                result[c] = np.nan  # 清空, 重新填充

        elo_sorted = elo_sel.sort_values("date")

        for idx, row in result.iterrows():
            date = row.get("date")
            home = row.get("team_home")
            away = row.get("team_away")
            if pd.isna(date) or not home or not away:
                continue

            # 同队名组合的候选记录
            mask = (elo_sorted["team_home"] == home) & (
                elo_sorted["team_away"] == away
            )
            candidates = elo_sorted[mask]
            if candidates.empty:
                continue

            # 最近的不超过目标日期的记录
            past = candidates[candidates["date"] <= date]
            if not past.empty:
                best = past.iloc[-1]
            else:
                # 若无历史记录, 取最近的未来记录 (兜底)
                best = candidates.iloc[0]

            result.at[idx, "elo_home"] = best.get("elo_home")
            result.at[idx, "elo_away"] = best.get("elo_away")
            if "elo_diff" in best.index:
                result.at[idx, "elo_diff"] = best.get("elo_diff")

        return result

    # ================================================================
    # 辅助函数
    # ================================================================
    def _resolve_team_name(
        self, name: str, known_teams: set
    ) -> str | None:
        """解析球队名 → Understat 英文名

        依次尝试:
            1. 名称本身已是 Understat 英文名 (在 known_teams 中)
            2. 中文名 → 英文名 (通过 TEAM_NAME_MAP / cn_to_en_team)
            3. 模糊匹配 known_teams 中的球队名
            4. 返回中→英映射结果或原始名

        Args:
            name:         球队名 (中文或英文)
            known_teams:  已知球队名集合 (Understat 英文名)
        Returns:
            解析后的球队名, 或 None
        """
        if not name or (isinstance(name, float) and np.isnan(name)):
            return None

        name = str(name).strip()

        # 1. 直接匹配已知球队
        if name in known_teams:
            return name

        # 2. 中文 → 英文 (通过配置映射表)
        en = cn_to_en_team(name)
        if en and en in known_teams:
            return en

        # 3. 模糊匹配 (子串包含)
        for t in known_teams:
            if name and (name in t or t in name):
                return t

        # 4. 返回映射结果 (即使不在 known_teams 中, 仍可能是有效队名)
        if en:
            return en

        return name

    def _finalize(self, features: pd.DataFrame) -> pd.DataFrame:
        """最终处理: 列顺序、类型、数值精度

        确保输出包含所有核心特征列, 并按规范顺序排列。
        """
        if features.empty:
            return features

        # 核心输出列顺序
        core_cols = [
            "team_home", "team_away", "date", "league",
            "home_xg_for", "home_xg_against", "home_xg_diff",
            "away_xg_for", "away_xg_against", "away_xg_diff",
            "home_ppda", "away_ppda",
            "home_opp_ppda", "away_opp_ppda",
            "home_ppda_diff", "away_ppda_diff",
            "home_pressure_index", "away_pressure_index",
            "home_ppda_stability", "away_ppda_stability",
            "pressure_xg_interaction",
            "home_xg_overperformance", "away_xg_overperformance",
            "xg_cv_quality", "has_xg_data",
            "home_n_games", "away_n_games",
            "home_has_xg", "away_has_xg",
            "is_big5",
        ]

        # Elo 列 (若存在)
        elo_cols = [
            c for c in ["elo_home", "elo_away", "elo_diff"]
            if c in features.columns
        ]

        # 其他列 (排除内部临时列)
        other_cols = [
            c for c in features.columns
            if c not in core_cols
            and c not in elo_cols
            and not c.startswith("_")
        ]

        ordered = core_cols + elo_cols + other_cols
        # 只保留实际存在的列
        ordered = [c for c in ordered if c in features.columns]
        # 补充可能遗漏的列
        for c in features.columns:
            if c not in ordered and not c.startswith("_"):
                ordered.append(c)

        features = features[ordered]

        # 类型转换
        if "has_xg_data" in features.columns:
            features["has_xg_data"] = features["has_xg_data"].astype(bool)
        if "home_has_xg" in features.columns:
            features["home_has_xg"] = features["home_has_xg"].astype(bool)
        if "away_has_xg" in features.columns:
            features["away_has_xg"] = features["away_has_xg"].astype(bool)
        if "xg_cv_quality" in features.columns:
            features["xg_cv_quality"] = (
                features["xg_cv_quality"].clip(0, 1).astype(float)
            )

        # 数值列四舍五入 (保留4位小数)
        numeric_cols = features.select_dtypes(include=[np.number]).columns
        features[numeric_cols] = features[numeric_cols].round(4)

        return features.reset_index(drop=True)
