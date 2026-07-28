#!/usr/bin/env python3
"""M串N 容错过关模拟器 (Ultra 6.5)

严格按竞彩官方规则:
  - 32种合法M串N组合 (不可自定义), 子注构成 C(M,k)
  - 单注奖金 = 2元 × 各场固定奖金(SP)连乘
  - 总奖金 = 中奖子注之和 × 倍数, 未中子注无奖金
  - 关数上限: 胜平负/让球8关, 总进球6关, 比分/半全场4关 (本模拟器用HAD, 8关内合法)

计算方法:
  - 每场取预测文件中的 HAD 主推方向 (SWOT融合后概率 × 官方SP)
  - 各场命中视为独立事件, 用泊松二项分布DP求"命中场数X"的精确分布
  - 每个M串N组合: 中奖概率=P(X≥最小关数), 期望奖金=Σ子注 P(子注全中)×子注奖金
  - EV/ROI = 期望奖金/成本 - 1, 成本=注数×2元×倍数

用法: python msn_simulator.py <pred文件> [--top N] [--unit 2]
"""

import json
import os
import re
import sys
from itertools import combinations

# ===== 竞彩官方 32 种 M串N 组合表 (JINGCAI_RULES.md) =====
# {M: {名称: [子注关数k列表]}}
def _build_combo_table():
    table = {}
    fixed = {
        3: {'3串3': [2, 2, 2], '3串4': [2, 2, 2, 3]},
        4: {'4串4': [3, 3, 3, 3], '4串5': [3, 3, 3, 3, 4], '4串6': [2] * 6,
            '4串11': [2] * 6 + [3] * 4 + [4]},
        5: {'5串5': [4] * 5, '5串6': [4] * 5 + [5], '5串10': [2] * 10,
            '5串16': [3] * 10 + [4] * 5 + [5], '5串20': [2] * 10 + [3] * 10,
            '5串26': [2] * 10 + [3] * 10 + [4] * 5 + [5]},
        6: {'6串6': [5] * 6, '6串7': [5] * 6 + [6], '6串15': [2] * 15,
            '6串20': [3] * 20, '6串22': [4] * 15 + [5] * 6 + [6],
            '6串35': [2] * 15 + [3] * 20, '6串42': [3] * 20 + [4] * 15 + [5] * 6 + [6],
            '6串50': [2] * 15 + [3] * 20 + [4] * 15,
            '6串57': [2] * 15 + [3] * 20 + [4] * 15 + [5] * 6 + [6]},
        7: {'7串7': [6] * 7, '7串8': [6] * 7 + [7], '7串21': [5] * 21,
            '7串35': [4] * 35, '7串120': [2] * 21 + [3] * 35 + [4] * 35 + [5] * 21 + [6] * 7 + [7]},
        8: {'8串8': [7] * 8, '8串9': [7] * 8 + [8], '8串28': [6] * 28,
            '8串56': [5] * 56, '8串70': [4] * 70,
            '8串247': [2] * 28 + [3] * 56 + [4] * 70 + [5] * 56 + [6] * 28 + [7] * 8 + [8]},
    }
    # 校验子注数 = 名称中的N
    for m, combos in fixed.items():
        for name, folds in combos.items():
            n_declared = int(name.split('串')[1])
            assert len(folds) == n_declared, f"{name} 子注数不符: {len(folds)} != {n_declared}"
    return fixed

COMBO_TABLE = _build_combo_table()


def poisson_binomial_probs(probs):
    """泊松二项分布: 各场独立命中概率 -> 命中场数X的分布 [P(X=0)..P(X=M)]"""
    dist = [1.0]
    for p in probs:
        new = [0.0] * (len(dist) + 1)
        for i, d in enumerate(dist):
            new[i] += d * (1 - p)
            new[i + 1] += d * p
        dist = new
    return dist


def extract_had_bets(pred_file):
    """从预测文件提取每场 HAD 主推 (SWOT融合后概率 × 官方SP)"""
    with open(pred_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    bets = []
    for key, r in data.get('results', {}).items():
        had = r.get('HAD', {})
        if not had.get('dir') or not had.get('odds'):
            continue
        m = re.findall(r'(\d+(?:\.\d+)?)%', had.get('p', ''))
        if len(m) != 3:
            continue
        idx = {'胜': 0, '平': 1, '负': 2}.get(had['dir'])
        if idx is None:
            continue
        meta = data.get('meta', {}).get(key, {})
        bets.append({
            'key': key,
            'home': meta.get('home', ''),
            'away': meta.get('away', ''),
            'dir': had['dir'],
            'prob': float(m[idx]) / 100.0,
            'odds': float(had['odds']),
            'conf': had.get('conf', ''),
        })
    return bets


def simulate_combo(bets, folds, unit=2.0):
    """模拟一个M串N组合

    bets: 每场的 {prob, odds}
    folds: 子注关数列表 (如 [2,2,2,3])
    返回: dict(注数/成本/中奖概率/期望奖金/期望盈利/ROI/保底回收分布)
    """
    M = len(bets)
    probs = [b['prob'] for b in bets]
    odds = [b['odds'] for b in bets]
    dist = poisson_binomial_probs(probs)  # P(X=x)

    n_bets = len(folds)
    cost = n_bets * unit

    # 期望奖金: 枚举每个子注 (fold k -> C(M,k) 个组合)
    exp_return = 0.0
    for k in set(folds):
        # combinations(range(M), k) 已枚举全部 C(M,k) 个子注, 与组合表注数一致, 无需再乘
        for idxs in combinations(range(M), k):
            p_sub = 1.0
            o_sub = 1.0
            for i in idxs:
                p_sub *= probs[i]
                o_sub *= odds[i]
            exp_return += p_sub * o_sub * unit

    min_fold = min(folds)
    p_any_win = sum(dist[x] for x in range(min_fold, M + 1))

    # 期望回收 (含未中小于min_fold时=0): exp_return已含全部子注期望
    roi = exp_return / cost - 1.0 if cost > 0 else 0.0

    return {
        'n_bets': n_bets,
        'cost': cost,
        'min_fold': min_fold,
        'p_any_win': p_any_win,
        'exp_return': exp_return,
        'exp_profit': exp_return - cost,
        'roi': roi,
        'dist': dist,
    }


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    pred_file = args[0] if args else None
    if not pred_file or not os.path.exists(pred_file):
        print("用法: python msn_simulator.py <pred文件> [--top N]")
        return
    top_n = 10
    for i, a in enumerate(sys.argv):
        if a == '--top' and i + 1 < len(sys.argv):
            top_n = int(sys.argv[i + 1])

    bets = extract_had_bets(pred_file)
    M = len(bets)
    print(f"场次: {M}场 (HAD主推, 概率=SWOT融合后, SP=官方固定奖金)")
    for b in bets:
        print(f"  {b['key']} {b['home']} vs {b['away']} | {b['dir']}@{b['odds']} P={b['prob']:.0%} {b['conf']}")

    if M < 3:
        print("场次不足3场, M串N至少需3场")
        return
    if M > 8:
        print(f"⚠️ {M}场超过8关上限, 取置信度前8场")
        bets = sorted(bets, key=lambda b: b['prob'], reverse=True)[:8]
        M = 8

    combos = COMBO_TABLE.get(M, {})
    print(f"\n{'=' * 78}")
    print(f"{'过关方式':<8} {'注数':>4} {'成本':>6} {'中奖条件':>8} {'中奖概率':>8} {'期望奖金':>8} {'期望盈亏':>8} {'ROI':>8}")
    print(f"{'=' * 78}")

    rows = []
    # M串1 基线
    r1 = simulate_combo(bets, [M])
    rows.append((f'{M}串1', r1))
    for name, folds in combos.items():
        r = simulate_combo(bets, folds)
        rows.append((name, r))

    # 按ROI排序输出
    rows.sort(key=lambda x: x[1]['roi'], reverse=True)
    for name, r in rows:
        print(f"{name:<8} {r['n_bets']:>4} {r['cost']:>6.0f} 中≥{r['min_fold']}场   "
              f"{r['p_any_win']:>7.1%} {r['exp_return']:>8.1f} {r['exp_profit']:>+8.1f} {r['roi']:>+7.1%}")

    print(f"{'=' * 78}")
    # 命中场数分布
    dist = poisson_binomial_probs([b['prob'] for b in bets])
    print("命中场数分布: " + ' '.join(f"{x}场:{dist[x]:.0%}" for x in range(M + 1)))

    best_roi = rows[0]
    best_hit = max(rows, key=lambda x: x[1]['p_any_win'])
    print(f"\n按目标选择:")
    print(f"  最高ROI:   {best_roi[0]} | 中奖概率{best_roi[1]['p_any_win']:.0%} | 期望盈亏{best_roi[1]['exp_profit']:+.1f}元 | ROI{best_roi[1]['roi']:+.1%}")
    print(f"  最高命中率: {best_hit[0]} | 中奖概率{best_hit[1]['p_any_win']:.0%} | 期望盈亏{best_hit[1]['exp_profit']:+.1f}元 | ROI{best_hit[1]['roi']:+.1%}")
    print(f"  均衡(容错1): 选 {M}串{M+1} 或 {M}串{M} (错1场仍保大部分奖金)")
    print("注: EV线性原理下各组合ROI接近, 选择本质是[命中率 vs 单注奖金]的权衡;")
    print("    期望值为模型概率下的长期均值, 实际奖金以出票时刻SP为准")


if __name__ == '__main__':
    main()
