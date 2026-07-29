"""
football-prediction-pipeline · 主流程入口

管线: 原始数据层 → 特征工程层 → 预测模型层 → 集成层

用法:
  python run_pipeline.py                 # 完整跑通 (Understat 直接抓取)
  python run_pipeline.py --predict "Arsenal" "Chelsea"  # 单场预测
"""
from __future__ import annotations
import argparse
import logging
import sys

import pandas as pd
import numpy as np

from src.config import config, OUTPUT_DIR, LEAGUE_MAP, LEAGUE_MAP_REVERSE
from src.data_collectors import FBrefCollector, UnderstatCollector, EloBuilder
from src.features import FeatureBuilder
from src.models import PoissonModel, XGBoostModel, EloModel
from src.ensemble import EnsemblePredictor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pipeline")


def _understat_to_matches(understat_df: pd.DataFrame) -> pd.DataFrame:
    """将 Understat 数据转换为 EloBuilder/FeatureBuilder 可用的比赛格式

    Understat 赛程数据包含: home_team, away_team, home_goals, away_goals,
    home_xg, away_xg, date, league_cn 等。
    """
    if understat_df is None or understat_df.empty:
        return pd.DataFrame()

    df = understat_df.copy()
    # 标准化列名
    rename = {
        "home_team": "home_team",
        "away_team": "away_team",
        "league_cn": "league",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    # 确保日期列存在
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    elif df.index.name == "date":
        df["date"] = pd.to_datetime(df.index, errors="coerce")

    # 只保留已完赛的比赛 (有比分)
    if "home_goals" in df.columns and "away_goals" in df.columns:
        df = df.dropna(subset=["home_goals", "away_goals"])

    # 确保必要列存在
    for col in ["home_team", "away_team", "league"]:
        if col not in df.columns:
            df[col] = ""

    df = df.sort_values("date").reset_index(drop=True)
    return df


def run_full_pipeline() -> pd.DataFrame:
    """跑通完整管线, 返回预测结果

    数据流:
        1. Understat 采集 → 比赛数据 + xG 数据 (直接抓取, 无需浏览器)
        2. FeatureBuilder → 特征矩阵 (xG/xGA/PPDA/Elo/超额表现/交叉验证质量)
        3. 模型训练 → Poisson (xG驱动) + XGBoost + Elo
        4. 集成预测 → 加权融合 + 蒙特卡洛比分模拟
    """
    logger.info("=" * 60)
    logger.info("足球预测管线启动 (Understat 直接抓取模式)")
    logger.info("=" * 60)

    # ── 1. 原始数据层 ──────────────────────────────────
    logger.info("【第1层】原始数据采集 (Understat xG/xGA/PPDA)")

    understat = UnderstatCollector()
    understat_df = understat.collect()
    logger.info(f"  Understat xG 数据: {understat_df.shape}")

    if understat_df.empty:
        logger.error("Understat 数据采集失败, 管线终止")
        return pd.DataFrame()

    # 从 Understat 数据提取比赛信息 (用于 Elo 构建)
    matches = _understat_to_matches(understat_df)
    logger.info(f"  比赛数据 (从 Understat 提取): {matches.shape}")

    # 尝试 FBref (可选, 补充数据 — 默认跳过, Understat 已提供全部所需数据)
    if config.fbref_enabled:
        try:
            fbref = FBrefCollector()
            fbref_df = fbref.collect()
            if not fbref_df.empty:
                logger.info(f"  FBref 补充数据: {fbref_df.shape}")
        except Exception as e:
            logger.info(f"  FBref 不可用 (跳过): {e}")

    # 存储到数据库
    try:
        understat.store_to_db(understat_df)
    except Exception as e:
        logger.warning(f"  Understat 数据库存储失败: {e}")

    # ── 2. 特征工程层 ──────────────────────────────────
    logger.info("【第2层】特征工程 (Elo + xG + PPDA + 超额表现 + 交叉验证质量)")

    elo_builder = EloBuilder()
    elo_df = elo_builder.build(matches)
    logger.info(f"  Elo 评级: {elo_df.shape}")

    feat_builder = FeatureBuilder()
    features = feat_builder.build(matches, elo_df, understat_df=understat_df)
    logger.info(f"  特征矩阵: {features.shape}")

    if features.empty:
        logger.error("特征矩阵为空, 管线终止")
        return pd.DataFrame()

    # ── 3. 预测模型层 ──────────────────────────────────
    logger.info("【第3层】模型训练 (Poisson[xG驱动] + XGBoost + Elo)")

    poisson = PoissonModel()
    poisson.fit(features)

    # XGBoost 需要进球标签, 从原始比赛数据获取
    xgb = XGBoostModel()
    try:
        # 合并特征和进球数据用于训练
        train_df = features.copy()
        if "home_goals" not in train_df.columns and "home_goals" in matches.columns:
            # 通过队名+日期合并进球数据
            goal_cols = matches[["home_team", "away_team", "date", "home_goals", "away_goals"]].copy()
            goal_cols = goal_cols.rename(columns={"home_team": "team_home", "away_team": "team_away"})
            train_df = train_df.merge(
                goal_cols, on=["team_home", "team_away", "date"], how="left"
            )
        xgb_acc = xgb.fit(train_df)
        logger.info(f"  XGBoost 训练准确率: {xgb_acc:.4f}")
    except Exception as e:
        logger.warning(f"  XGBoost 训练失败 (跳过): {e}")
        xgb = None

    elo_model = EloModel()
    elo_model.fit(matches)

    # ── 4. 集成层 ──────────────────────────────────────
    logger.info("【第4层】集成预测 (加权融合 + 蒙特卡洛比分模拟)")
    ensemble = EnsemblePredictor(poisson, xgb, elo_model)

    # 对最近10场做集成预测
    recent = features.tail(10).copy()
    predictions = ensemble.predict_batch(recent)

    # 合并队名信息
    if "team_home" in recent.columns:
        predictions.insert(0, "team_home", recent["team_home"].values)
    if "team_away" in recent.columns:
        predictions.insert(1, "team_away", recent["team_away"].values)

    # 保存结果
    out_path = OUTPUT_DIR / "predictions.csv"
    predictions.to_csv(out_path, index=False)
    logger.info(f"预测结果已保存: {out_path}")

    # 特征重要性
    if xgb is not None and xgb.feature_importance is not None:
        imp_path = OUTPUT_DIR / "feature_importance.csv"
        xgb.feature_importance.to_csv(imp_path)
        logger.info(f"特征重要性已保存: {imp_path}")
        logger.info("Top 5 重要特征:\n" + str(xgb.feature_importance.head(5)))

    return predictions


def predict_single(home: str, away: str) -> dict:
    """单场预测 (用 Understat 历史数据推断)"""
    logger.info(f"单场预测: {home} vs {away}")

    # 采集数据
    understat = UnderstatCollector()
    understat_df = understat.collect()

    if understat_df.empty:
        logger.error("Understat 数据采集失败")
        return {}

    matches = _understat_to_matches(understat_df)

    elo_builder = EloBuilder()
    elo_df = elo_builder.build(matches)

    feat_builder = FeatureBuilder()
    features = feat_builder.build(matches, elo_df, understat_df=understat_df)

    poisson = PoissonModel()
    poisson.fit(features)
    elo_model = EloModel()
    elo_model.fit(matches)

    # XGBoost (可选)
    xgb = None
    try:
        train_df = features.copy()
        if "home_goals" not in train_df.columns and "home_goals" in matches.columns:
            goal_cols = matches[["home_team", "away_team", "date", "home_goals", "away_goals"]].copy()
            goal_cols = goal_cols.rename(columns={"home_team": "team_home", "away_team": "team_away"})
            train_df = train_df.merge(goal_cols, on=["team_home", "team_away", "date"], how="left")
        xgb = XGBoostModel()
        xgb_acc = xgb.fit(train_df)
        logger.info(f"  XGBoost 训练准确率: {xgb_acc:.4f}")
    except Exception as e:
        logger.warning(f"  XGBoost 训练失败 (跳过): {e}")
        xgb = None

    ensemble = EnsemblePredictor(poisson, xgb, elo_model)

    # 构造该对阵的特征行 (从历史推断)
    h_match = features[(features["team_home"] == home)].tail(1)
    if h_match.empty:
        # 尝试中文名
        from src.config import cn_to_en_team
        en_home = cn_to_en_team(home)
        if en_home:
            h_match = features[(features["team_home"] == en_home)].tail(1)

    if h_match.empty:
        logger.warning(f"未找到 {home} 的历史数据, 使用默认特征")
        row = features.iloc[-1].copy()
        row["team_home"], row["team_away"] = home, away
    else:
        row = h_match.iloc[0].copy()
        row["team_away"] = away

    result = ensemble.predict(row)
    mc = ensemble.monte_carlo_simulation(row)

    print(f"\n{'='*50}")
    print(f"  {home} vs {away}  预测结果")
    print(f"{'='*50}")
    print(f"  胜平负概率 (集成):")
    print(f"    {home} 胜: {result['p_home']:.1%}")
    print(f"    平局:     {result['p_draw']:.1%}")
    print(f"    {away} 胜: {result['p_away']:.1%}")
    print(f"  期望进球: {result['expected_goals_home']:.2f} : {result['expected_goals_away']:.2f}")
    print(f"  大小球 (2.5): 大球概率 {result['p_over_2_5']:.1%}")
    print(f"  模型一致性: {result['model_agreement']:.1%}")
    print(f"  蒙特卡洛模拟 (10000次):")
    print(f"    最可能比分: {mc['most_likely_score']}")
    print(f"    平均总进球: {mc['avg_total_goals']:.2f}")
    print(f"    Top 5 比分:")
    for s in mc['score_distribution']:
        print(f"      {s['score']}: {s['probability']:.1%}")
    print(f"{'='*50}")
    return result


def main():
    parser = argparse.ArgumentParser(description="足球体彩预测管线 (Understat 直接抓取)")
    parser.add_argument("--predict", nargs=2, metavar=("HOME", "AWAY"), help="单场预测")
    args = parser.parse_args()

    if args.predict:
        predict_single(args.predict[0], args.predict[1])
    else:
        preds = run_full_pipeline()
        if not preds.empty:
            print(f"\n管线完成, 共 {len(preds)} 条预测")
            display_cols = [c for c in ["team_home", "team_away", "p_home", "p_draw", "p_away",
                                        "expected_total", "p_over_2_5", "model_agreement"]
                           if c in preds.columns]
            print(preds[display_cols].to_string(index=False))
        else:
            print("\n管线完成, 无预测结果")


if __name__ == "__main__":
    main()
