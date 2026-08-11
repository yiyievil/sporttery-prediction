#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cloud_daily.py — 云端/无人值守调度入口 (GitHub Actions / crontab)

功能: 自动检测当日(北京时间)体彩开盘场次, 驱动 预测/更新/验证 全流程,
     无需手动指定编号, 电脑关机也可在云端运行。

用法:
  python cloud_daily.py                          # 预测今日全部开盘场次 + PDF报告
  python cloud_daily.py --mode update            # 更新今日场次即时赔率+趋势警报
  python cloud_daily.py --mode verify            # 验证昨日场次赛果+结算模拟投注
  python cloud_daily.py --date 260811 --numbers 001,002   # 手动指定日期/场次
  python cloud_daily.py --dry-run                # 只检测场次, 不执行

环境: 时区一律按北京时间(UTC+8)计算, 与运行机器时区无关。
"""

import argparse
import glob
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import requests

SPORTTERY_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getMatchListV1.qry?clientCode=3001"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
BJT = timezone(timedelta(hours=8))  # 北京时间

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def now_bjt():
    return datetime.now(BJT)


def fetch_today_matches(target_date=None):
    """从体彩API获取指定日期(缺省=今日, 北京时间)的开盘场次

    返回: (code_date '260811', weekday '周二', numbers ['001','002',...], match_date 'YYYY-MM-DD')
    无开盘场次时返回 None
    """
    day = target_date or now_bjt().date()
    day_str = day.strftime("%Y-%m-%d")
    try:
        r = requests.get(SPORTTERY_URL, headers=HEADERS, timeout=20)
        data = r.json()
    except Exception as e:
        print(f"[cloud] sporttery API 请求失败: {e}")
        return None

    for mi in (data.get("value") or {}).get("matchInfoList", []) or []:
        if mi.get("businessDate") != day_str:
            continue
        subs = mi.get("subMatchList", []) or []
        numbers = sorted({str(s.get("matchNum", ""))[-3:] for s in subs if s.get("matchNum")})
        if not numbers:
            return None
        return day.strftime("%y%m%d"), mi.get("weekday", ""), numbers, day_str
    return None


def numbers_from_pred_file(target_date):
    """verify/update 回退: 从当日预测文件提取场次编号
    (体彩 match list API 只覆盖在售后场次, 历史场次无法检测)"""
    pattern = os.path.join(SCRIPT_DIR, "predictions", f"pred_{target_date.strftime('%Y%m%d')}_*.json")
    hits = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not hits:
        return []
    import json
    try:
        with open(hits[-1], encoding="utf-8") as f:
            data = json.load(f)
        return sorted({k[-3:] for k in (data.get("results") or {})})
    except Exception as e:
        print(f"[cloud] ⚠️ 读取预测文件失败: {e}")
        return []


def run(cmd):
    print(f"[cloud] $ {' '.join(cmd)}", flush=True)
    p = subprocess.run(cmd, cwd=SCRIPT_DIR)
    return p.returncode


def gen_pdf_for(code_date, weekday):
    """为新产出的预测JSON生成PDF报告"""
    pattern = os.path.join(SCRIPT_DIR, "predictions", f"pred_20{code_date}_{weekday}.json")
    hits = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not hits:
        # 兼容无周几后缀的命名
        hits = sorted(glob.glob(os.path.join(SCRIPT_DIR, "predictions", f"pred_20{code_date}*.json")),
                      key=os.path.getmtime)
    if not hits:
        print("[cloud] ⚠️ 未找到预测JSON, 跳过PDF生成")
        return 1
    out_pdf = os.path.join(SCRIPT_DIR, "predictions", os.path.basename(hits[-1]).replace(".json", ".pdf"))
    return run([sys.executable, "gen_pred_pdf.py", hits[-1], out_pdf])


def main():
    ap = argparse.ArgumentParser(description="云端无人值守竞彩预测调度")
    ap.add_argument("--mode", choices=["predict", "update", "verify"], default="predict")
    ap.add_argument("--date", default="", help="编号日期 260811 或 2026-08-11 (缺省: predict/update=今日, verify=昨日)")
    ap.add_argument("--numbers", default="", help="场次编号, 如 001,002 (缺省=当日全部)")
    ap.add_argument("--dry-run", action="store_true", help="只检测场次不执行")
    args = ap.parse_args()

    # 解析目标日期
    target_date = None
    if args.date:
        d = args.date.strip()
        try:
            target_date = datetime.strptime(d, "%y%m%d").date() if len(d) == 6 \
                else datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            print(f"[cloud] ⚠️ 无法解析日期 '{args.date}', 使用缺省日期")
    if target_date is None:
        target_date = now_bjt().date() - timedelta(days=1) if args.mode == "verify" else now_bjt().date()

    manual_numbers = [x.strip()[-3:] for x in args.numbers.replace("，", ",").split(",") if x.strip()]

    # verify/update 需要定位场次: 手动编号优先, 否则从当日开盘检测
    detected = fetch_today_matches(target_date)
    if detected:
        code_date, weekday, numbers, day_str = detected
        print(f"[cloud] 检测到 {day_str} ({weekday}) 开盘 {len(numbers)} 场: {','.join(numbers)}")
    else:
        code_date, weekday, day_str = target_date.strftime("%y%m%d"), "", target_date.strftime("%Y-%m-%d")
        numbers = []
        print(f"[cloud] {day_str} 无开盘场次检测不到(verify/update 可用手动编号)")

    if manual_numbers:
        numbers = manual_numbers
    if not numbers:
        numbers = numbers_from_pred_file(target_date)
        if numbers:
            print(f"[cloud] 从预测文件回退提取 {len(numbers)} 场: {','.join(numbers)}")
    if not numbers:
        print(f"[cloud] {day_str} 无场次可处理, 退出")
        return 0

    print(f"[cloud] 模式={args.mode} 日期={day_str} 场次={','.join(numbers)}")
    if args.dry_run:
        print("[cloud] dry-run, 不执行")
        return 0

    if args.mode == "predict":
        rc = run([sys.executable, "v215_e2e.py", code_date, ",".join(numbers)])
        if rc == 0:
            gen_pdf_for(code_date, weekday)
        return rc
    if args.mode == "update":
        return run([sys.executable, "v215_update.py", day_str, ",".join(numbers)])
    # verify
    return run([sys.executable, "v215_verify.py", f"{day_str} {','.join(numbers)}"])


if __name__ == "__main__":
    sys.exit(main())
