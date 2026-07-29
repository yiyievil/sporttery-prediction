#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Understat 数据采集器 — 为 v215_e2e.py 提供 xG/xGA/PPDA 数据

定位: 本脚本是 v215 预测引擎的数据前置管线, 只负责采集与落库,
      不做预测。预测统一由 v215_e2e.py 完成。
      (原模型层已隔离至 lab/, 仅供实验对照)

数据流: Understat → predictions/historical_odds.db [understat_matches 表]
        → v215_e2e.py _compute_xg_stats() 消费

用法:
  python run_pipeline.py            # 全量采集 (config.seasons 全部赛季)
  python run_pipeline.py --update   # 增量模式: 仅当前赛季 (每日刷新用)
"""
from __future__ import annotations
import argparse
import logging
import sys

from src.config import config, DB_PATH
from src.data_collectors import UnderstatCollector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pipeline")


def collect_understat(update_only: bool = False) -> int:
    """采集 Understat 数据并写入 historical_odds.db

    update_only=True 时仅抓 config.seasons 最后一个赛季 (增量刷新),
    store_to_db 使用 INSERT OR REPLACE (game_id 唯一), 重复运行安全。
    返回写入条数。
    """
    seasons = [config.seasons[-1]] if update_only else config.seasons
    mode = "增量 (仅当前赛季)" if update_only else f"全量 ({len(seasons)}赛季)"
    logger.info("=" * 60)
    logger.info(f"Understat 数据采集 [{mode}] → {DB_PATH}")
    logger.info("=" * 60)

    collector = UnderstatCollector(seasons=seasons)
    df = collector.collect()
    if df.empty:
        logger.error("Understat 采集失败")
        return 0

    inserted = collector.store_to_db(df)
    logger.info(f"完成: {inserted} 条记录写入 understat_matches")
    return inserted


def main():
    parser = argparse.ArgumentParser(description="Understat 数据采集器 (v215 数据前置)")
    parser.add_argument("--update", action="store_true",
                        help="增量模式: 仅采集当前赛季 (日常刷新)")
    args = parser.parse_args()

    n = collect_understat(update_only=args.update)
    if n <= 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
