"""football-prediction-pipeline · 集成预测层

EnsemblePredictor —— 融合 Poisson / XGBoost / Elo 三个基模型的预测结果:

    * 以 config.model_weights 为权重做加权平均, 产出统一的胜负概率与进球期望;
    * 基于融合后的进球期望 (λ) 解析计算大2.5球概率;
    * 度量三模型一致性 (1 - 归一化方差);
    * 提供蒙特卡洛比分模拟, 输出最可能比分与比分分布。

每个基模型需实现 predict(row) -> dict, 至少包含:
    p_home / p_draw / p_away                  胜平负概率 (求和为1)
    expected_goals_home / expected_goals_away 进球期望 λ (可选, 缺失时自动跳过)
"""
from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
import pandas as pd

from src.config import config

logger = logging.getLogger("ensemble")

# 概率值在 [0, 1] 区间内的最大总体方差, 用于一致性归一化
_MAX_PROB_VARIANCE = 0.25


def _poisson_pmf(k: int, lam: float) -> float:
    """泊松分布概率质量函数 P(X = k | λ)

    P(k; λ) = e^{-λ} · λ^k / k!
    """
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


class EnsemblePredictor:
    """集成预测器: 加权融合 Poisson / XGBoost / Elo 三模型"""

    def __init__(self, poisson: Any, xgboost: Any, elo: Any):
        """初始化集成预测器

        Parameters
        ----------
        poisson : 泊松回归模型 (需实现 predict(row) -> dict)
        xgboost : XGBoost 梯度提升模型 (需实现 predict(row) -> dict)
        elo     : Elo 评级模型 (需实现 predict(row) -> dict)
        """
        self.poisson = poisson
        self.xgboost = xgboost
        self.elo = elo
        # 模型权重, 取自 config: {"poisson": 0.35, "xgboost": 0.35, "elo": 0.30}
        self.weights: dict[str, float] = dict(config.model_weights)

    # ----------------------------------------------------------------
    # 内部工具
    # ----------------------------------------------------------------
    def _model_pairs(self) -> list[tuple[str, Any]]:
        """返回 (模型名, 模型对象) 列表, 顺序与权重键保持一致"""
        return [
            ("poisson", self.poisson),
            ("xgboost", self.xgboost),
            ("elo", self.elo),
        ]

    def _collect_predictions(self, row: Any) -> list[dict[str, Any]]:
        """依次调用三个基模型收集预测结果

        单个模型预测失败时跳过并记录警告, 不影响其余模型。
        """
        preds: list[dict[str, Any]] = []
        for name, model in self._model_pairs():
            try:
                raw = model.predict(row)
                pred: dict[str, Any] = dict(raw)
                pred["_model"] = name
                preds.append(pred)
            except Exception as exc:  # 基模型失败不应中断整体集成
                logger.warning("基模型 %s 预测失败, 已跳过: %s", name, exc)
        return preds

    def _weighted_avg(self, preds: list[dict[str, Any]], key: str) -> float:
        """对指定字段做加权平均

        自动跳过缺失该字段的模型, 并对参与模型的权重重归一化,
        保证各字段融合结果不受个别模型缺字段的影响。
        """
        acc = 0.0
        total_w = 0.0
        for pred in preds:
            value = pred.get(key)
            if value is None:
                continue
            w = self.weights.get(pred.get("_model"), 0.0)
            acc += w * float(value)
            total_w += w
        if total_w <= 0:
            return 0.0
        return acc / total_w

    @staticmethod
    def _compute_agreement(preds: list[dict[str, Any]]) -> float:
        """计算三模型一致性

        一致性 = 1 - 归一化方差
        其中归一化方差 = mean(各结果维度的跨模型方差) / 最大概率方差(0.25)

        * 模型预测完全一致时方差为 0, 一致性为 1.0;
        * 模型预测分歧最大时一致性趋近 0.0。
        """
        outcomes = ("p_home", "p_draw", "p_away")
        variances: list[float] = []
        for outcome in outcomes:
            values = [
                float(p[outcome])
                for p in preds
                if p.get(outcome) is not None
            ]
            if len(values) < 2:
                continue
            variances.append(float(np.var(values)))  # 总体方差 (ddof=0)
        if not variances:
            return 1.0
        mean_var = float(np.mean(variances))
        normalized = mean_var / _MAX_PROB_VARIANCE
        return float(max(0.0, min(1.0, 1.0 - normalized)))

    @staticmethod
    def _over_2_5_prob(lam_home: float, lam_away: float) -> float:
        """基于独立泊松进球计算大2.5球 (总进球≥3) 概率

        P(over 2.5) = 1 - Σ_{h + a ≤ 2} P(home=h) · P(away=a)
        """
        if lam_home <= 0 or lam_away <= 0:
            return 0.0
        p_under = 0.0
        for h in range(3):            # h = 0, 1, 2
            for a in range(3 - h):    # a 取值使 h + a ≤ 2
                p_under += _poisson_pmf(h, lam_home) * _poisson_pmf(a, lam_away)
        return float(max(0.0, min(1.0, 1.0 - p_under)))

    # ----------------------------------------------------------------
    # 对外接口
    # ----------------------------------------------------------------
    def predict(self, row: Any) -> dict[str, Any]:
        """对单场比赛融合三模型预测

        Parameters
        ----------
        row : 单场比赛特征 (pandas.Series 或 dict)

        Returns
        -------
        dict 包含:
            p_home / p_draw / p_away        融合后的胜平负概率 (归一化至求和为1)
            expected_goals_home / away      融合后的进球期望 λ
            expected_total                  进球期望总和
            p_over_2_5                      大2.5球概率
            model_agreement                 三模型一致性 ∈ [0, 1]
        """
        preds = self._collect_predictions(row)

        # 兜底: 所有基模型均失败时返回均势先验
        if not preds:
            logger.warning("所有基模型预测失败, 返回均势先验")
            return {
                "p_home": 1 / 3,
                "p_draw": 1 / 3,
                "p_away": 1 / 3,
                "expected_goals_home": 1.35,
                "expected_goals_away": 1.15,
                "expected_total": 2.50,
                "p_over_2_5": 0.50,
                "model_agreement": 0.0,
            }

        # 1) 加权融合胜平负概率
        p_home = self._weighted_avg(preds, "p_home")
        p_draw = self._weighted_avg(preds, "p_draw")
        p_away = self._weighted_avg(preds, "p_away")

        # 概率归一化 (保证求和为1, 容忍数值误差与个别模型缺字段)
        prob_sum = p_home + p_draw + p_away
        if prob_sum > 0:
            p_home /= prob_sum
            p_draw /= prob_sum
            p_away /= prob_sum

        # 2) 加权融合进球期望 (λ)
        exp_home = max(0.0, self._weighted_avg(preds, "expected_goals_home"))
        exp_away = max(0.0, self._weighted_avg(preds, "expected_goals_away"))
        exp_total = exp_home + exp_away

        # 3) 基于融合 λ 解析计算大2.5球概率
        p_over_2_5 = self._over_2_5_prob(exp_home, exp_away)

        # 4) 三模型一致性
        agreement = self._compute_agreement(preds)

        return {
            "p_home": float(p_home),
            "p_draw": float(p_draw),
            "p_away": float(p_away),
            "expected_goals_home": float(exp_home),
            "expected_goals_away": float(exp_away),
            "expected_total": float(exp_total),
            "p_over_2_5": float(p_over_2_5),
            "model_agreement": float(agreement),
        }

    def predict_batch(self, df: pd.DataFrame) -> pd.DataFrame:
        """批量预测: 对 DataFrame 每行调用 predict

        Returns
        -------
        pd.DataFrame 每行为一场比赛的融合预测结果, 索引与输入保持一致
        """
        if df is None or len(df) == 0:
            return pd.DataFrame()
        records = [self.predict(row) for _, row in df.iterrows()]
        result = pd.DataFrame(records)
        result.index = df.index
        return result

    def monte_carlo_simulation(self, row: Any, n: int = 10000) -> dict[str, Any]:
        """蒙特卡洛比分模拟

        基于融合后的进球期望 λ_home / λ_away, 用泊松分布独立采样 n 场比赛,
        统计最可能比分、平均总进球与比分分布。

        Parameters
        ----------
        row : 单场比赛特征
        n   : 模拟次数, 默认 10000

        Returns
        -------
        dict 包含:
            most_likely_score    出现次数最多的比分 (如 "2-1")
            avg_total_goals      模拟平均总进球
            score_distribution   出现频次前5的比分及其概率 (列表)
        """
        # 取融合后的进球期望作为泊松 λ
        ensemble = self.predict(row)
        lam_home = max(0.0, float(ensemble["expected_goals_home"]))
        lam_away = max(0.0, float(ensemble["expected_goals_away"]))

        # 可复现随机种子: 由进球期望确定性生成, 同一比赛多次模拟结果一致
        seed_base = (int(lam_home * 1_000_000) * 73856093) ^ (
            int(lam_away * 1_000_000) * 19349663
        )
        rng = np.random.default_rng(seed_base & 0x7FFFFFFF)

        # 独立泊松采样主客队进球
        home_goals = rng.poisson(lam_home, size=n)
        away_goals = rng.poisson(lam_away, size=n)
        total_goals = home_goals + away_goals

        # 统计比分频次并按频次降序排列
        scores = np.column_stack([home_goals, away_goals])
        unique_scores, counts = np.unique(scores, axis=0, return_counts=True)
        order = np.argsort(-counts)  # 频次降序

        # 频次前5的比分分布
        top_idx = order[:5]
        score_distribution: list[dict[str, Any]] = []
        for idx in top_idx:
            h = int(unique_scores[idx][0])
            a = int(unique_scores[idx][1])
            score_distribution.append({
                "score": f"{h}-{a}",
                "home": h,
                "away": a,
                "count": int(counts[idx]),
                "probability": float(counts[idx]) / n,
            })

        # 出现频次最高的比分
        best = order[0]
        most_likely_score = (
            f"{int(unique_scores[best][0])}-{int(unique_scores[best][1])}"
        )

        return {
            "most_likely_score": most_likely_score,
            "avg_total_goals": round(float(np.mean(total_goals)), 4),
            "score_distribution": score_distribution,
        }
