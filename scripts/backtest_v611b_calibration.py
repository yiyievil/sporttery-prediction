#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ultra 6.11 修正3/4 实证标定
数据: odds_change_history (4437场竞彩赔率变动) + historical_matches (赛果)

修正3: 跨盘口诱大 (大球升盘+平赔下降 → λ×0.85~1.0)
  代理构造: TTG隐含期望进球上升 = 大球升盘; HAD平赔隐含概率上升 = 平赔下降
  验证假设: 触发后实际总进球是否低于市场预期 (诱大=是)

修正4: 0-0校准 (模型0-0 < 市场隐含50% → 上调低进球比分)
  验证: (A) 市场0-0赔率本身是否校准; (B) 模型/市场严重分歧时谁更准,
        最优混合权重 → 标定 adjustment 幅度
"""

import sqlite3
import json
import math
import os
from collections import defaultdict

_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(_WORKSPACE, 'predictions', 'historical_odds.db')

TTG_VALUES = {'s0': 0, 's1': 1, 's2': 2, 's3': 3, 's4': 4, 's5': 5, 's6': 6, 's7': 7.3}


def norm3(h, d, a):
    inv = [1.0 / h, 1.0 / d, 1.0 / a]
    s = sum(inv)
    return [x / s for x in inv]


def ttg_expected_goals(ttg_json):
    try:
        d = json.loads(ttg_json)
        inv = {k: 1.0 / float(v) for k, v in d.items() if k in TTG_VALUES and float(v) > 1}
        s = sum(inv.values())
        if s <= 0 or len(inv) < 6:
            return None
        return sum(TTG_VALUES[k] * (v / s) for k, v in inv.items())
    except Exception:
        return None


def crs_score_probs(crs_json):
    """解析crs → {score: prob} (去水归一化), 返回 None 若数据不全"""
    try:
        d = json.loads(crs_json)
        probs = {}
        for k, v in d.items():
            o = float(v)
            if o <= 1:
                continue
            if k.startswith('s') and 's' in k[1:]:
                parts = k[1:].split('s')
                if len(parts) == 2 and parts[0].lstrip('-').isdigit() and parts[1].isdigit():
                    hg, ag = int(parts[0]), int(parts[1])
                    if hg >= 0:
                        probs[f'{hg}-{ag}'] = 1.0 / o
            elif k == 's-1sh':
                probs['胜其他'] = 1.0 / o
            elif k == 's-1sd':
                probs['平其他'] = 1.0 / o
            elif k == 's-1sa':
                probs['负其他'] = 1.0 / o
        s = sum(probs.values())
        if s <= 0:
            return None
        return {k: v / s for k, v in probs.items()}
    except Exception:
        return None


def load_data():
    con = sqlite3.connect(DB_PATH)
    matches = {}
    for r in con.execute("SELECT id, home_score, away_score FROM historical_matches WHERE home_score IS NOT NULL"):
        matches[r[0]] = (r[1], r[2])

    # 每场: had/ttg 初盘(seq最小)与临场(seq最大), crs 临场
    snaps = defaultdict(dict)
    for ot in ('had', 'ttg', 'crs'):
        rows = con.execute("""
            SELECT match_db_id, seq, h, d, a, ttg_data, crs_data
            FROM odds_change_history WHERE odds_type=? ORDER BY match_db_id, seq
        """, (ot,)).fetchall()
        per = defaultdict(list)
        for mid, seq, h, d, a, ttg, crs in rows:
            per[mid].append((seq, h, d, a, ttg, crs))
        for mid, lst in per.items():
            key_init, key_close = f'{ot}_init', f'{ot}_close'
            snaps[mid][key_init] = lst[0]
            snaps[mid][key_close] = lst[-1]
    con.close()
    return matches, snaps


def main():
    matches, snaps = load_data()
    print(f'样本: {len(matches)} 场有赛果, {len(snaps)} 场有赔率快照')

    # ============================================================
    # 修正3: 诱大信号
    # ============================================================
    groups = defaultdict(lambda: [0.0, 0.0, 0])  # key -> [actual_total, expected_close, n]
    n_trigger = 0
    for mid, sc in matches.items():
        s = snaps.get(mid, {})
        had_i, had_c = s.get('had_init'), s.get('had_close')
        ttg_i, ttg_c = s.get('ttg_init'), s.get('ttg_close')
        if not all([had_i, had_c, ttg_i, ttg_c]) or not had_i[1] or not had_c[1]:
            continue
        e_i = ttg_expected_goals(ttg_i[4])
        e_c = ttg_expected_goals(ttg_c[4])
        if not e_i or not e_c or not had_i[2] or not had_c[2]:
            continue
        pd_i = norm3(had_i[1], had_i[2], had_i[3])[1]
        pd_c = norm3(had_c[1], had_c[2], had_c[3])[1]
        total = sc[0] + sc[1]

        ou_up = e_c > e_i + 0.08          # 期望进球显著上升 = 大球升盘
        draw_up = pd_c > pd_i + 0.004     # 平赔概率上升 = 平赔下降
        if ou_up and draw_up:
            key = '诱大触发 (升盘+平赔降)'
            n_trigger += 1
        elif ou_up and not draw_up:
            key = '仅升盘 (对照A)'
        else:
            key = '其余 (对照B)'
        g = groups[key]
        g[0] += total
        g[1] += e_c
        g[2] += 1

    print()
    print('=' * 70)
    print('标定4: 跨盘口诱大 — TTG期望进球代理 (触发=期望升+平赔升)')
    print('=' * 70)
    ctrl = groups['其余 (对照B)']
    ctrl_ratio = ctrl[0] / ctrl[1] if ctrl[1] else 1
    print(f"{'分组':<24}{'n':>6}{'场均进球':>10}{'临场期望':>10}{'比值':>8}{'相对对照':>9}")
    for key in ['诱大触发 (升盘+平赔降)', '仅升盘 (对照A)', '其余 (对照B)']:
        actual, expected, n = groups[key]
        if not n:
            continue
        ratio = actual / expected
        print(f"{key:<24}{n:>6}{actual/n:>10.3f}{expected/n:>10.3f}{ratio:>8.3f}{ratio/ctrl_ratio:>9.3f}")
    print()
    print('解读: 触发组"相对对照"显著<1 → 诱大假设成立, 该值即实证λ系数')

    # ============================================================
    # 修正4: 0-0 校准
    # ============================================================
    bins = defaultdict(lambda: [0, 0, 0.0])       # bin -> [n, n00, mkt_prob_sum]
    disagree = []  # (model_00, mkt_00_adj, actual00)
    for mid, sc in matches.items():
        s = snaps.get(mid, {})
        ttg_c = s.get('ttg_close')
        crs_c = s.get('crs_close')
        if not ttg_c or not crs_c or not crs_c[5]:
            continue
        e_c = ttg_expected_goals(ttg_c[4])
        probs = crs_score_probs(crs_c[5])
        if not e_c or not probs or '0-0' not in probs:
            continue
        mkt_00 = probs['0-0']                     # 已去水归一化
        model_00 = math.exp(-e_c)                 # Poisson: P(0-0)=e^-(λh+λa)
        actual00 = 1 if (sc[0] == 0 and sc[1] == 0) else 0

        b = round(min(0.20, max(0.04, mkt_00)), 2)
        g = bins[b]
        g[0] += 1
        g[1] += actual00
        g[2] += mkt_00
        if model_00 < mkt_00 * 0.5:
            disagree.append((model_00, mkt_00, actual00))

    print()
    print('=' * 70)
    print('标定5A: 市场0-0赔率校准度 (去水后概率 vs 实际频率)')
    print('=' * 70)
    print(f"{'市场概率档':>10}{'n':>7}{'实际0-0率':>10}{'市场均值':>10}{'偏差':>8}")
    for b in sorted(bins):
        n, n00, ps = bins[b]
        if n < 20:
            continue
        print(f"{b:>10.2f}{n:>7}{n00/n:>10.3f}{ps/n:>10.3f}{n00/n-ps/n:>8.3f}")

    print()
    print('=' * 70)
    print(f'标定5B: 模型严重低估0-0时 (model < 50%×market, n={len(disagree)}) 谁更准')
    print('=' * 70)
    if disagree:
        n00 = sum(x[2] for x in disagree)
        mm = sum(x[0] for x in disagree) / len(disagree)
        mk = sum(x[1] for x in disagree) / len(disagree)
        print(f"实际0-0率: {n00/len(disagree):.3f} | 模型均值: {mm:.3f} | 市场均值: {mk:.3f}")

        def logloss(w):
            ll = 0.0
            for m, k, y in disagree:
                p = max(1e-9, min(1 - 1e-9, w * k + (1 - w) * m))
                ll -= y * math.log(p) + (1 - y) * math.log(1 - p)
            return ll / len(disagree)

        best_w, best_ll = min(((w / 20, logloss(w / 20)) for w in range(21)), key=lambda x: x[1])
        print(f"logloss: 纯模型={logloss(0):.4f} | 纯市场={logloss(1):.4f} | 最优w(市场权重)={best_w:.2f} → {best_ll:.4f}")
        print('解读: w越接近1 → 市场越可信, 上调0-0方向正确; w为最优混合比')


if __name__ == '__main__':
    main()
