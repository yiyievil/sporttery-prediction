#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
260813周四 比赛预测补丁 — sporttery API 被 WAF 拦截, 手动构造数据
"""
import json, sys, os, time, re
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

# 切换到项目目录
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ============================================================
# 手动构造的 260813周四 001-010 比赛数据
# 来源: lottery.gov.cn 竞彩足球胜平负计算器 (2026-08-13 11:55:04)
# ============================================================
MANUAL_MATCHES = {
    "周四001": {
        "match_num": "001", "full_num": "260813001", "weekday": "周四",
        "key": "周四001", "match_id": 2040832,
        "league": "沙职", "home": "艾卜哈", "away": "拉斯决心",
        "match_date": "2026-08-14", "match_time": "00:15",
        "HAD": {"h": 2.32, "d": 3.15, "a": 2.63},
        "HHAD": {"h": 5.20, "d": 4.00, "a": 1.46, "goalLine": -1},
        "had_in_list": True, "data_source": "sporttery",
        "betting_single": False, "home_rank": "16", "away_rank": "9",
    },
    "周四002": {
        "match_num": "002", "full_num": "260813002", "weekday": "周四",
        "key": "周四002", "match_id": 2040821,
        "league": "欧罗巴", "home": "克拉约瓦", "away": "库奥皮奥",
        "match_date": "2026-08-14", "match_time": "01:00",
        "HAD": {"h": 1.32, "d": 4.25, "a": 7.40},
        "HHAD": {"h": 2.16, "d": 3.25, "a": 2.80, "goalLine": -1},
        "had_in_list": True, "data_source": "sporttery",
        "betting_single": False, "home_rank": "", "away_rank": "",
    },
    "周四003": {
        "match_num": "003", "full_num": "260813003", "weekday": "周四",
        "key": "周四003", "match_id": 2040822,
        "league": "欧罗巴", "home": "帕福斯", "away": "萨尔茨堡",
        "match_date": "2026-08-14", "match_time": "01:00",
        "HAD": {"h": 3.05, "d": 3.45, "a": 1.96},
        "HHAD": {"h": 1.65, "d": 3.70, "a": 3.95, "goalLine": 1},
        "had_in_list": True, "data_source": "sporttery",
        "betting_single": False, "home_rank": "", "away_rank": "",
    },
    "周四004": {
        "match_num": "004", "full_num": "260813004", "weekday": "周四",
        "key": "周四004", "match_id": 2040833,
        "league": "欧罗巴", "home": "雷克维京", "away": "图恩",
        "match_date": "2026-08-14", "match_time": "01:30",
        "HAD": {"h": 2.28, "d": 3.65, "a": 2.40},
        "HHAD": {"h": 4.70, "d": 4.15, "a": 1.48, "goalLine": -1},
        "had_in_list": True, "data_source": "sporttery",
        "betting_single": False, "home_rank": "", "away_rank": "",
    },
    "周四005": {
        "match_num": "005", "full_num": "260813005", "weekday": "周四",
        "key": "周四005", "match_id": 2040823,
        "league": "沙职", "home": "利雅青年", "away": "胡巴卡德",
        "match_date": "2026-08-14", "match_time": "02:00",
        "HAD": {"h": 4.45, "d": 4.20, "a": 1.50},
        "HHAD": {"h": 2.25, "d": 3.60, "a": 2.46, "goalLine": 1},
        "had_in_list": True, "data_source": "sporttery",
        "betting_single": False, "home_rank": "13", "away_rank": "4",
    },
    "周四006": {
        "match_num": "006", "full_num": "260813006", "weekday": "周四",
        "key": "周四006", "match_id": 2040824,
        "league": "欧罗巴", "home": "流浪者", "away": "比亚韦",
        "match_date": "2026-08-14", "match_time": "02:30",
        "HAD": {"h": 1.54, "d": 3.90, "a": 4.50},
        "HHAD": {"h": 2.70, "d": 3.35, "a": 2.17, "goalLine": -1},
        "had_in_list": True, "data_source": "sporttery",
        "betting_single": False, "home_rank": "", "away_rank": "",
    },
    "周四007": {
        "match_num": "007", "full_num": "260813007", "weekday": "周四",
        "key": "周四007", "match_id": 2040825,
        "league": "欧罗巴", "home": "安德莱", "away": "塞萨洛",
        "match_date": "2026-08-14", "match_time": "02:30",
        "HAD": {"h": 2.75, "d": 3.30, "a": 2.17},
        "HHAD": {"h": 1.54, "d": 3.75, "a": 4.70, "goalLine": 1},
        "had_in_list": True, "data_source": "sporttery",
        "betting_single": False, "home_rank": "", "away_rank": "",
    },
    "周四008": {
        "match_num": "008", "full_num": "260813008", "weekday": "周四",
        "key": "周四008", "match_id": 2040826,
        "league": "欧罗巴", "home": "哈茨", "away": "本菲卡",
        "match_date": "2026-08-14", "match_time": "02:45",
        "HAD": {"h": 5.90, "d": 4.80, "a": 1.33},
        "HHAD": {"h": 2.78, "d": 3.55, "a": 2.05, "goalLine": 1},
        "had_in_list": True, "data_source": "sporttery",
        "betting_single": False, "home_rank": "", "away_rank": "",
    },
    "周四009": {
        "match_num": "009", "full_num": "260813009", "weekday": "周四",
        "key": "周四009", "match_id": 2040827,
        "league": "解放者杯", "home": "米拉索尔", "away": "基多体大",
        "match_date": "2026-08-14", "match_time": "06:00",
        "HAD": {"h": 1.49, "d": 3.60, "a": 5.60},
        "HHAD": {"h": 2.74, "d": 3.10, "a": 2.27, "goalLine": -1},
        "had_in_list": True, "data_source": "sporttery",
        "betting_single": False, "home_rank": "2", "away_rank": "1",
    },
    "周四010": {
        "match_num": "010", "full_num": "260813010", "weekday": "周四",
        "key": "周四010", "match_id": 2040828,
        "league": "解放者杯", "home": "罗萨里奥", "away": "科林蒂安",
        "match_date": "2026-08-14", "match_time": "08:30",
        "HAD": {"h": 2.02, "d": 2.70, "a": 3.80},
        "HHAD": {"h": 4.90, "d": 3.30, "a": 1.61, "goalLine": -1},
        "had_in_list": True, "data_source": "sporttery",
        "betting_single": False, "home_rank": "2", "away_rank": "1",
    },
}

# ============================================================
# Monkey-patch: 替换 sporttery API 调用, 注入手动数据
# ============================================================
import v215_e2e as engine

# 保存原始函数
_orig_fetch_sporttery_matches = engine.fetch_sporttery_matches
_orig_enrich_sporttery_extra = engine.enrich_sporttery_extra
_orig_fetch_sporttery_fixed_bonus = engine.fetch_sporttery_fixed_bonus
_orig_fetch_sporttery_matches_from_results = engine.fetch_sporttery_matches_from_results

def patched_fetch_sporttery_matches(match_numbers, target_date=None):
    """返回手动构造的比赛数据"""
    print(f"  [补丁] 手动注入 260813周四 比赛数据 (API被WAF拦截)")
    result = {}
    for key, m in MANUAL_MATCHES.items():
        if m["match_num"] in match_numbers:
            result[key] = dict(m)  # 深拷贝
    print(f"  [补丁] 匹配到 {len(result)} 场比赛")
    return result

def patched_enrich_sporttery_extra(matches):
    """空操作 — 手动数据已含 rank/betting_single"""
    if matches:
        print(f"  [补丁] 跳过 enrich_sporttery_extra (手动数据已含)")

def patched_fetch_sporttery_fixed_bonus(match_id):
    """空操作 — 固定奖金API也被拦截"""
    return None

def patched_fetch_sporttery_matches_from_results(match_numbers, target_date=None):
    """空操作"""
    return {}

# 应用补丁
engine.fetch_sporttery_matches = patched_fetch_sporttery_matches
engine.enrich_sporttery_extra = patched_enrich_sporttery_extra
engine.fetch_sporttery_fixed_bonus = patched_fetch_sporttery_fixed_bonus
engine.fetch_sporttery_matches_from_results = patched_fetch_sporttery_matches_from_results

# 设置目标参数
engine.TARGET_WEEKDAY = "周四"
engine.MATCH_NUMBERS = [f"{i:03d}" for i in range(1, 11)]
engine.TARGET_DATE = None
engine.PRED_MODE = "update"

# 运行主流程
print("=" * 60)
print("260813周四 001-010 预测 (手动数据注入)")
print("=" * 60)
engine.main()