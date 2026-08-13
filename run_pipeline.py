#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Understat + FBref 数据采集器 — 为 v215_e2e.py 提供 xG/xGA/PPDA 数据

定位: 本脚本是 v215 预测引擎的数据前置管线, 只负责采集与落库,
      不做预测。预测统一由 v215_e2e.py 完成。

数据流:
  Understat  → predictions/historical_odds.db [understat_matches 表]
  FBref      → predictions/historical_odds.db [team_xg 表]
               source_type:
                 'fbref_direct' — cloudscraper/curl_cffi 直连成功
                 'fbref_proxy'  — proxy 兜底 (联赛均值)
  → v215_e2e.py fetch_xg_rolling_stats() 消费

用法:
  python run_pipeline.py                     # 全量采集 (Understat + FBref auto)
  python run_pipeline.py --update            # 增量模式: 仅当前赛季
  python run_pipeline.py --fbref-only        # 仅采集 FBref 数据
  python run_pipeline.py --fbref-only --mode cloudscraper  # 仅 cloudscraper
  python run_pipeline.py --fbref-only --mode proxy         # 仅 proxy
  python run_pipeline.py --understat-only    # 仅采集 Understat 数据
"""
from __future__ import annotations
import argparse
import logging
import sys

from src.config import config, DB_PATH, FBREF_LEAGUE_MAP, LEAGUE_MAP
from src.data_collectors import UnderstatCollector, FBrefCollector

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


def collect_fbref(update_only: bool = False,
                  fbref_mode: str = "auto") -> int:
    """采集 FBref 数据并写入 team_xg 表

    三层回退策略 (fbref_mode="auto"):
      1. cloudscraper 直连 → source_type='fbref_direct'
      2. curl_cffi 直连    → source_type='fbref_direct'
      3. proxy 兜底       → source_type='fbref_proxy'

    返回写入条数。
    """
    mode_label = "增量" if update_only else "全量"
    logger.info("=" * 60)
    logger.info(f"FBref 数据采集 [{mode_label}, mode={fbref_mode}] → {DB_PATH}")
    logger.info("=" * 60)

    seasons = [config.seasons[-1]] if update_only else config.seasons
    collector = FBrefCollector(
        seasons=seasons,
        leagues=FBREF_LEAGUE_MAP,
        mode=fbref_mode,
    )
    df = collector.collect()
    if df.empty:
        logger.error("FBref 采集失败")
        return 0

    inserted = collector.store_to_db(df)
    logger.info(f"完成: {inserted} 条记录写入 team_xg "
                f"(source_type={collector.source_type})")
    return inserted


def main():
    parser = argparse.ArgumentParser(
        description="Understat + FBref 数据采集器 (v215 数据前置)"
    )
    parser.add_argument("--update", action="store_true",
                        help="增量模式: 仅采集当前赛季 (日常刷新)")
    parser.add_argument("--fbref-only", action="store_true",
                        help="仅采集 FBref 数据")
    parser.add_argument("--understat-only", action="store_true",
                        help="仅采集 Understat 数据")
    parser.add_argument("--mode", type=str, default="auto",
                        choices=["auto", "cloudscraper", "curl_cffi", "proxy"],
                        help="FBref 采集模式: auto (默认, 自动回退) | "
                             "cloudscraper | curl_cffi | proxy")
    args = parser.parse_args()

    run_understat = not args.fbref_only
    run_fbref = not args.understat_only

    exit_code = 0

    if run_understat:
        n = collect_understat(update_only=args.update)
        if n <= 0:
            exit_code = 1

    if run_fbref:
        n = collect_fbref(update_only=args.update, fbref_mode=args.mode)
        if n <= 0:
            exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()