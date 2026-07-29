#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ultra 6.11 场景修正系数实证标定 (与首回合惩罚相同的自身基线法)
数据: understat_matches 五大联赛 2023-24 ~ 2025-26 完整赛季 (约5200场)

标定目标:
1. 近况滑坡: 近3场 3L → λ×0.70, 2L → λ×0.80, DDD → ×0.88 (当前假设)
2. 交锋压制: h2h主胜率<35% → λ_h×0.85, λ_a×0.90 (当前假设)

方法:
- 对每场比赛的每个队, 用其本赛季此前比赛(排除近3场窗口)建立自身进球基线
- 期望进球 = 0.5*(自身场均进球基线 + 对手场均失球基线)
- 比较触发组 vs 对照组的 实际/期望 比值 → 得到实证修正系数
"""

import sqlite3
import os
from collections import defaultdict

_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE', os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_WORKSPACE, 'predictions', 'historical_odds.db')

LEAGUES = ['英超', '意甲', '西甲', '德甲', '法甲']
MIN_BASE_GAMES = 5   # 基线最少场次


def load_matches():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("""
        SELECT league_cn, season, match_date, home_team, away_team, home_goals, away_goals
        FROM understat_matches
        WHERE season IN ('2023-2024','2024-2025','2025-2026')
          AND league_cn IN ('英超','意甲','西甲','德甲','法甲')
        ORDER BY match_date, id
    """).fetchall()
    con.close()
    return rows


def build_team_histories(rows):
    """按 联赛+赛季 组建队时序: team -> [(date, gf, ga, result, opp, is_home)]"""
    hist = defaultdict(list)
    for lg, season, date, h, a, hg, ag in rows:
        if hg is None or ag is None:
            continue
        hr = 'W' if hg > ag else ('D' if hg == ag else 'L')
        ar = 'W' if ag > hg else ('D' if ag == hg else 'L')
        hist[(lg, season, h)].append((date, hg, ag, hr, a, True))
        hist[(lg, season, a)].append((date, ag, hg, ar, h, False))
    for k in hist:
        hist[k].sort(key=lambda x: x[0])
    return hist


def calibrate_form_slump(rows, hist):
    """近况滑坡: 触发组(近3场3L / 2L / DDD) vs 对照组 的实际/期望进球比"""
    groups = defaultdict(lambda: [0.0, 0.0, 0])  # key -> [actual_sum, expected_sum, n]

    for lg, season, date, h, a, hg, ag in rows:
        if hg is None or ag is None:
            continue
        for team, opp, gf, is_home in ((h, a, hg, True), (a, h, ag, False)):
            seq = hist.get((lg, season, team), [])
            opp_seq = hist.get((lg, season, opp), [])
            # 找到本场在时序中的位置
            idx = next((i for i, m in enumerate(seq) if m[0] == date and m[4] == opp), None)
            if idx is None or idx < 3:
                continue
            prev3 = seq[idx - 3:idx]
            base = seq[:idx - 3]
            opp_base = [m for m in opp_seq if m[0] < date]
            if len(base) < MIN_BASE_GAMES or len(opp_base) < MIN_BASE_GAMES:
                continue
            base_gf = sum(m[1] for m in base) / len(base)
            opp_ga = sum(m[2] for m in opp_base) / len(opp_base)
            expected = 0.5 * (base_gf + opp_ga)
            if expected <= 0:
                continue

            l3 = sum(1 for m in prev3 if m[3] == 'L')
            d3 = sum(1 for m in prev3 if m[3] == 'D')
            if l3 == 3:
                key = '3L (当前×0.70)'
            elif l3 == 2:
                key = '2L/3 (当前×0.80)'
            elif d3 == 3:
                key = 'DDD (当前×0.88)'
            else:
                key = '对照组 (无修正)'
            g = groups[key]
            g[0] += gf
            g[1] += expected
            g[2] += 1

    print('=' * 70)
    print('标定1: 近况滑坡 — 触发后实际进球 / 自身基线期望进球')
    print('=' * 70)
    ctrl_ratio = groups['对照组 (无修正)'][0] / groups['对照组 (无修正)'][1]
    print(f"{'分组':<22}{'n':>6}{'场均实际':>10}{'场均期望':>10}{'比值':>8}{'相对对照':>9}")
    for key in ['3L (当前×0.70)', '2L/3 (当前×0.80)', 'DDD (当前×0.88)', '对照组 (无修正)']:
        actual, expected, n = groups[key]
        if n == 0:
            continue
        ratio = actual / expected
        rel = ratio / ctrl_ratio
        print(f"{key:<22}{n:>6}{actual/n:>10.3f}{expected/n:>10.3f}{ratio:>8.3f}{rel:>9.3f}")
    print()
    print('解读: "相对对照"即为实证λ系数 (剔除对手强度与球队水平后)')
    print('当前假设: 3L→0.70, 2L→0.80, DDD→0.88')
    return groups


def calibrate_h2h_suppression(rows, hist, min_h2h=4):
    """交锋压制: h2h主胜率<35%时, 主队/客队 实际/期望进球比

    数据集3个赛季 → 赛季3的比赛有4场历史交锋可用 (min_h2h=4)
    """
    # 建交锋历史: (league, frozenset pair) -> [(date, home, away, hg, ag)]
    pair_hist = defaultdict(list)
    for lg, season, date, h, a, hg, ag in rows:
        if hg is None or ag is None:
            continue
        pair_hist[(lg, frozenset((h, a)))].append((date, h, a, hg, ag))
    for k in pair_hist:
        pair_hist[k].sort(key=lambda x: x[0])

    groups = defaultdict(lambda: [0.0, 0.0, 0])

    for lg, season, date, h, a, hg, ag in rows:
        if hg is None or ag is None:
            continue
        prior = [m for m in pair_hist.get((lg, frozenset((h, a))), []) if m[0] < date]
        if len(prior) < min_h2h:
            continue
        # 主队列视角胜率 (含主客场)
        hw = sum(1 for _, ph, pa, phg, pag in prior
                 if (ph == h and phg > pag) or (pa == h and pag > phg))
        rate = hw / len(prior)

        # 双方基线期望 (主客场拆分)
        def base_exp(team, opp, is_home):
            seq = [m for m in hist.get((lg, season, team), []) if m[0] < date and m[5] == is_home]
            opp_seq = [m for m in hist.get((lg, season, opp), []) if m[0] < date and m[5] != is_home]
            if len(seq) < 3 or len(opp_seq) < 3:
                return None
            gf = sum(m[1] for m in seq) / len(seq)
            ga = sum(m[2] for m in opp_seq) / len(opp_seq)
            return 0.5 * (gf + ga)

        eh = base_exp(h, a, True)
        ea = base_exp(a, h, False)
        if not eh or not ea or eh <= 0 or ea <= 0:
            continue

        key = 'h2h主胜率<35% (当前λ_h×0.85/λ_a×0.90)' if rate < 0.35 else 'h2h主胜率>=35% (对照)'
        g = groups[(key, 'home')]
        g[0] += hg; g[1] += eh; g[2] += 1
        g = groups[(key, 'away')]
        g[0] += ag; g[1] += ea; g[2] += 1

    print('=' * 70)
    print(f'标定2: 交锋压制 — h2h历史(n>={min_h2h})主胜率<35%时的进球抑制')
    print('=' * 70)
    print(f"{'分组':<42}{'方':>4}{'n':>6}{'实际':>8}{'期望':>8}{'比值':>8}{'相对对照':>9}")
    results = {}
    for key in ['h2h主胜率<35% (当前λ_h×0.85/λ_a×0.90)', 'h2h主胜率>=35% (对照)']:
        for side, label in (('home', '主'), ('away', '客')):
            actual, expected, n = groups[(key, side)]
            if n == 0:
                continue
            results[(key, side)] = actual / expected
    ctrl_h = results.get(('h2h主胜率>=35% (对照)', 'home'))
    ctrl_a = results.get(('h2h主胜率>=35% (对照)', 'away'))
    for key in ['h2h主胜率<35% (当前λ_h×0.85/λ_a×0.90)', 'h2h主胜率>=35% (对照)']:
        for side, label, ctrl in (('home', '主', ctrl_h), ('away', '客', ctrl_a)):
            actual, expected, n = groups[(key, side)]
            if n == 0:
                continue
            ratio = actual / expected
            rel = ratio / ctrl if ctrl else float('nan')
            print(f"{key:<42}{label:>4}{n:>6}{actual/n:>8.3f}{expected/n:>8.3f}{ratio:>8.3f}{rel:>9.3f}")
    print()
    print('解读: "相对对照"即为实证λ系数; 当前假设 λ_h×0.85, λ_a×0.90')


def calibrate_defensive_away(rows, hist):
    """修正5: 防守型客队 — 近4场3W+且赛季场均失球<1.0 → 当前假设双方λ×0.80(4W)/0.85(3W)

    分组: 4W+失球<1.0 / 3W+失球<1.0 / 对照
    分别测量主队λ与客队λ的 实际/期望 比值 (相对对照)
    """
    groups = defaultdict(lambda: [0.0, 0.0, 0])

    for lg, season, date, h, a, hg, ag in rows:
        if hg is None or ag is None:
            continue
        a_seq = [m for m in hist.get((lg, season, a), []) if m[0] < date]
        if len(a_seq) < 4 + MIN_BASE_GAMES:
            continue
        recent4 = a_seq[-4:]
        base = a_seq[:-4]
        w4 = sum(1 for m in recent4 if m[3] == 'W')
        avg_ga = sum(m[2] for m in base) / len(base)  # 赛季(窗口前)场均失球
        if w4 >= 3 and avg_ga < 1.0:
            key = '4W+失球<1.0 (当前×0.80)' if w4 == 4 else '3W+失球<1.0 (当前×0.85)'
        else:
            key = '对照组 (无修正)'

        # 期望: 主队 = 0.5*(主队主场进球基线 + 客队客场失球基线), 客队对称
        h_seq = [m for m in hist.get((lg, season, h), []) if m[0] < date]
        h_home = [m for m in h_seq if m[5]]
        a_away = [m for m in a_seq if not m[5]]
        h_awaybase = [m for m in a_seq if not m[5]]
        h_base_ha = [m for m in h_seq if not m[5]]
        if len(h_home) < 3 or len(a_away) < 3 or len(h_base_ha) < 3:
            continue
        eh = 0.5 * (sum(m[1] for m in h_home) / len(h_home) +
                    sum(m[2] for m in a_away) / len(a_away))
        ea = 0.5 * (sum(m[1] for m in h_awaybase) / len(h_awaybase) +
                    sum(m[2] for m in h_base_ha) / len(h_base_ha))
        if eh <= 0 or ea <= 0:
            continue
        g = groups[(key, 'home')]
        g[0] += hg; g[1] += eh; g[2] += 1
        g = groups[(key, 'away')]
        g[0] += ag; g[1] += ea; g[2] += 1

    print('=' * 70)
    print('标定3: 防守型客队 — 近4场3W+且场均失球<1.0时的进球抑制')
    print('=' * 70)
    order = ['4W+失球<1.0 (当前×0.80)', '3W+失球<1.0 (当前×0.85)', '对照组 (无修正)']
    ratios = {}
    for key in order:
        for side in ('home', 'away'):
            actual, expected, n = groups[(key, side)]
            if n:
                ratios[(key, side)] = (actual / expected, n, actual, expected)
    ctrl_h = ratios[('对照组 (无修正)', 'home')][0]
    ctrl_a = ratios[('对照组 (无修正)', 'away')][0]
    print(f"{'分组':<30}{'方':>4}{'n':>6}{'实际':>8}{'期望':>8}{'比值':>8}{'相对对照':>9}")
    for key in order:
        for side, label, ctrl in (('home', '主', ctrl_h), ('away', '客', ctrl_a)):
            r = ratios.get((key, side))
            if not r:
                continue
            ratio, n, actual, expected = r
            print(f"{key:<30}{label:>4}{n:>6}{actual/n:>8.3f}{expected/n:>8.3f}{ratio:>8.3f}{ratio/ctrl:>9.3f}")
    print()
    print('解读: "相对对照"即为实证λ系数; 当前假设 4W→×0.80, 3W→×0.85 (双方同罚)')


def main():
    rows = load_matches()
    print(f'样本: {len(rows)} 场 (五大联赛 2023-24 ~ 2025-26)')
    hist = build_team_histories(rows)
    print()
    calibrate_form_slump(rows, hist)
    print()
    calibrate_h2h_suppression(rows, hist)
    print()
    calibrate_defensive_away(rows, hist)


if __name__ == '__main__':
    main()
