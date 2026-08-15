#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模型重标定 (Ultra 13.5): 基于 regression.db verify_history 重新生成校准因子

触发依据: CUSUM 检测到模型漂移, 漂移点从 predictions/drift_state.json 动态读取
  (由 gen_drift_state.py 基于 CUSUM 检出写入, 保证三脚本口径一致)。
  2026-08-15 跨周污染修复后: 漂移点 2026-08-09, 51.5% → 38.2% (13.2pp)。
重标定策略:
  1. 概率修正 (probability_correction): 按置信度区间, 模型标称 vs 实际命中
     - 漂移后样本 (≥漂移点) 权重 ×2.5 (近期漂移状态更能反映当前模型行为)
  2. 方向修正 (direction_correction): 各方向漂移后命中率
  3. 联赛修正 (league_correction): 各联赛漂移后表现 vs 总体

输出: predictions/model_calibration.json (覆盖)
"""

import json
import os
import re
import sqlite3
from datetime import datetime

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(WORKSPACE, 'predictions', 'regression.db')
OUT = os.path.join(WORKSPACE, 'predictions', 'model_calibration.json')
DRIFT_STATE_FILE = os.path.join(WORKSPACE, 'predictions', 'drift_state.json')

# Ultra 13.5: 漂移点改为从 drift_state.json 读取 (与 CUSUM 检出/gen_drift_state 一致)
DRIFT_POINT = '2026-08-09'   # 兜底值 (drift_state.json 缺失/损坏时)
try:
    with open(DRIFT_STATE_FILE, encoding='utf-8') as _f:
        _ds = json.load(_f)
    if _ds.get('drift_detected') and _ds.get('drift_point'):
        DRIFT_POINT = _ds['drift_point']
except Exception:
    pass

RECENT_WEIGHT = 2.5   # 漂移后样本权重倍数

CONF_BINS = [(0.0, 0.25, '0-25%'), (0.25, 0.35, '25-35%'), (0.35, 0.45, '35-45%'),
             (0.45, 0.55, '45-55%'), (0.55, 0.65, '55-65%'), (0.65, 0.75, '65-75%'),
             (0.75, 0.85, '75-85%'), (0.85, 1.01, '85-100%')]


def parse_probs(s):
    """'62%/31%/6%' → (62, 31, 6)"""
    if not s:
        return None
    m = re.match(r'(\d+)%/(\d+)%/(\d+)%', str(s))
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def main():
    conn = sqlite3.connect(DB)
    rows = conn.execute('''
        SELECT match_date, pred_had_probs, pred_had_dir, actual_had, had_hit, league
        FROM verify_history
        WHERE pred_had_probs IS NOT NULL AND actual_had IN ('胜','平','负')
    ''').fetchall()
    print(f'[重标定] 加载 {len(rows)} 场验证数据')

    old = {}
    if os.path.exists(OUT):
        with open(OUT, encoding='utf-8') as f:
            old = json.load(f)

    # ===== 1. 概率修正 (置信度区间) =====
    # 每个bin: 加权命中数 / 加权总数 → 实际命中率; 模型标称 = bin中点附近的平均conf
    bins_data = {}   # lbl → [w_hit, w_n, conf_sum_w, raw_n]
    for md, probs, pred_dir, actual, hit, lg in rows:
        p = parse_probs(probs)
        if not p:
            continue
        conf = max(p) / 100.0
        for lo, hi, lbl in CONF_BINS:
            if lo <= conf < hi:
                w = RECENT_WEIGHT if (md or '') >= DRIFT_POINT else 1.0
                b = bins_data.setdefault(lbl, [0.0, 0.0, 0.0, 0])
                b[0] += (hit or 0) * w
                b[1] += w
                b[2] += conf * w
                b[3] += 1
                break

    prob_corr = {}
    for lbl in ['0-25%', '25-35%', '35-45%', '45-55%', '55-65%', '65-75%', '75-85%', '85-100%']:
        if lbl not in bins_data or bins_data[lbl][1] < 5:
            # 样本不足, 保留旧因子
            if 'probability_correction' in old and lbl in old['probability_correction']:
                prob_corr[lbl] = old['probability_correction'][lbl]
            continue
        w_hit, w_n, conf_sum, raw_n = bins_data[lbl]
        actual_rate = w_hit / w_n
        nominal = conf_sum / w_n
        bias_pp = (actual_rate - nominal) * 100   # 负=模型高估
        prob_corr[lbl] = {
            'n': raw_n,
            'nominal_pct': round(nominal * 100, 1),
            'actual_pct': round(actual_rate * 100, 1),
            'bias_pp': round(bias_pp, 1),
            'correction_factor': round(actual_rate / nominal, 3) if nominal > 0.05 else 1.0,
        }

    # ===== 2. 方向修正 =====
    dir_data = {}
    for md, probs, pred_dir, actual, hit, lg in rows:
        if not pred_dir or hit is None:
            continue
        w = RECENT_WEIGHT if (md or '') >= DRIFT_POINT else 1.0
        d = dir_data.setdefault(pred_dir, [0.0, 0.0])
        d[0] += hit * w
        d[1] += w
    total_n = sum(d[1] for d in dir_data.values())
    overall = sum(d[0] for d in dir_data.values()) / total_n if total_n else 0.45

    dir_corr = {}
    for d_ in ['胜', '平', '负']:
        if d_ not in dir_data or dir_data[d_][1] < 4:
            continue
        h, n = dir_data[d_]
        rate = h / n
        dir_corr[f'预测{d_}'] = {
            'n': int(round(n)),
            'hit_rate': round(rate * 100, 1),
            'vs_overall_pp': round((rate - overall) * 100, 1),
            'bias_pp': round((rate - 0.45) * 100, 1),   # 相对45%基准
        }

    # ===== 3. 联赛修正 =====
    lg_data = {}
    for md, probs, pred_dir, actual, hit, lg in rows:
        if not lg or hit is None:
            continue
        w = RECENT_WEIGHT if (md or '') >= DRIFT_POINT else 1.0
        d = lg_data.setdefault(lg, [0.0, 0.0])
        d[0] += hit * w
        d[1] += w
    league_corr = {}
    for lg, (h, n) in sorted(lg_data.items(), key=lambda x: -x[1][1]):
        if n < 6:   # 样本不足不计
            continue
        rate = h / n
        league_corr[lg] = {
            'n': int(round(n)),
            'hit_rate': round(rate * 100, 1),
            'vs_overall_pp': round((rate - overall) * 100, 1),
        }

    # ===== 汇总 =====
    all_hits = sum(1 for _, _, _, _, h, _ in rows if h)
    out = {
        'version': 'Ultra 13.5',
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'description': ('模型校准偏差修正因子 — 漂移重标定版(清洁数据)。'
                        f'基于{len(rows)}场验证数据, {DRIFT_POINT}起样本×{RECENT_WEIGHT}权重。'
                        f'漂移点由 drift_state.json 提供 (CUSUM检出)'),
        'drift_recalibration': {
            'triggered_by': 'CUSUM drift detection (v215_verify Ultra 8.0)',
            'drift_point': DRIFT_POINT,
            'recent_weight': RECENT_WEIGHT,
            'samples_total': len(rows),
            'data_note': '2026-08-15 跨周污染修复后重标定 (verify_history 001-010 重配)',
        },
        'overall': {
            'model_hit_rate': round(all_hits / len(rows) * 100, 1) if rows else 0,
        },
        'probability_correction': prob_corr,
        'direction_correction': dir_corr,
        'league_correction': league_corr,
    }

    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f'[重标定] 已写入 {OUT}')
    print(f"  总体命中率: {out['overall']['model_hit_rate']}%")
    print('\n  概率修正 (置信度区间):')
    for lbl, v in prob_corr.items():
        print(f"    {lbl}: 标称{v.get('nominal_pct','?')}% 实际{v.get('actual_pct','?')}% "
              f"偏差{v['bias_pp']:+.1f}pp (n={v.get('n','?')})")
    print('\n  方向修正:')
    for k, v in dir_corr.items():
        print(f"    {k}: 命中{v['hit_rate']}% vs总体{v['vs_overall_pp']:+.1f}pp (n={v['n']})")
    print(f'\n  联赛修正: {len(league_corr)} 个联赛 (样本≥6)')


if __name__ == '__main__':
    main()
