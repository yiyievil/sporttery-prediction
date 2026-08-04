#!/usr/bin/env python3
"""
大小球盘口准确性分析 (Over/Under 2.5 Calibration)
===============================================
利用历史数据库中体彩大小球盘口数据, 量化分析:

1. 整体大小球命中率: 大球概率>50%时预测"大球", 对比实际总进球>=3
2. 按盘口(2.5/3/3.5)分层偏差
3. 按联赛分层大小球偏差
4. 初盘vs终盘变动分析: 隐含概率变化与实际赛果的关系

数据源: historical_matches.sp_daxiao_init / sp_daxiao_final
赔率格式: JSON {"s0":odds, "s1":odds, ..., "s7":odds} (s0=0球, ... s7=7+球)
"""

import json
import os
import sys
import sqlite3
from datetime import datetime
from collections import defaultdict

# 将 sporttery 根目录加入sys.path以支持从v215_e2e导入 (脚本位于 scripts/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from v215_e2e import shin_method

DB_PATH = '/workspace/sporttery/predictions/historical_odds.db'
OUTPUT_PATH = '/workspace/sporttery/predictions/over_under_analysis.json'


def log(msg):
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {msg}")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def parse_daxiao_odds(json_str):
    """解析sp_daxiao_init/final的JSON赔率, 返回有序odds列表[s0,s1,...,s7]"""
    if not json_str:
        return None
    try:
        d = json.loads(json_str)
        odds = [float(d.get(f's{i}', 0)) for i in range(8)]  # s0~s7
        if any(o <= 1 for o in odds):
            return None
        return odds
    except (json.JSONDecodeError, ValueError, KeyError, TypeError):
        return None


def compute_over_under_probs(odds):
    """从TTG赔率(s0~s7)计算大小球概率

    大球(Over 2.5) = P(3球) + P(4球) + ... + P(7+球)
    小球(Under 2.5) = P(0球) + P(1球) + P(2球)
    """
    probs = shin_method(odds)
    over_prob = sum(probs[3:8])  # s3~s7
    under_prob = sum(probs[0:3])  # s0~s2
    return over_prob, under_prob, probs


def get_handicap_label(odds_s3):
    """根据3球赔率确定盘口倾向

    3球赔率<3.5 = 偏向大球(大球热门)
    3球赔率3.5-4.5 = 中性盘口
    3球赔率>4.5 = 偏向小球(小球热门)
    """
    if odds_s3 < 3.5:
        return '大球热门(2.5)'
    elif odds_s3 <= 4.5:
        return '中性盘口(3.0)'
    else:
        return '小球热门(3.5)'


def compute_overall_accuracy(all_data):
    """1. 整体大小球命中率"""
    total = len(all_data)
    correct = 0
    over_pred = 0
    under_pred = 0
    actual_over = 0
    actual_under = 0

    # 累积概率和实际频率
    sum_over_prob = 0.0
    sum_under_prob = 0.0
    over_implied_all = []
    under_implied_all = []

    for d in all_data:
        over_p = d['over_prob']
        under_p = d['under_prob']
        actual_total = d['actual_total']
        is_over = actual_total >= 3

        sum_over_prob += over_p
        sum_under_prob += under_p
        over_implied_all.append(over_p)
        under_implied_all.append(under_p)

        if over_p > 0.5:
            over_pred += 1
            if is_over:
                correct += 1
        else:
            under_pred += 1
            if not is_over:
                correct += 1

        if is_over:
            actual_over += 1
        else:
            actual_under += 1

    accuracy = correct / total if total > 0 else 0
    avg_over_implied = sum_over_prob / total
    avg_under_implied = sum_under_prob / total
    actual_over_rate = actual_over / total
    actual_under_rate = actual_under / total

    # 偏差: 实际频率 - 平均隐含概率
    over_bias_pp = (actual_over_rate - avg_over_implied) * 100
    under_bias_pp = (actual_under_rate - avg_under_implied) * 100

    # Brier score
    brier = sum((d['over_prob'] - (1.0 if d['actual_total'] >= 3 else 0.0)) ** 2 for d in all_data) / total

    return {
        'total_matches': total,
        'accuracy': round(accuracy, 4),
        'correct': correct,
        'over_predictions': over_pred,
        'under_predictions': under_pred,
        'actual_over': actual_over,
        'actual_under': actual_under,
        'actual_over_rate': round(actual_over_rate, 4),
        'actual_under_rate': round(actual_under_rate, 4),
        'avg_over_implied': round(avg_over_implied, 4),
        'avg_under_implied': round(avg_under_implied, 4),
        'over_bias_pp': round(over_bias_pp, 1),
        'under_bias_pp': round(under_bias_pp, 1),
        'brier_score': round(brier, 4),
    }


def compute_by_handicap(all_data):
    """2. 按大小球盘口(2.5/3/3.5)分层"""
    bins = defaultdict(list)

    for d in all_data:
        # 使用3球赔率作为盘口标识
        s3_odds = d['odds'][3]
        label = get_handicap_label(s3_odds)
        bins[label].append(d)

    result = {}
    for label, data in sorted(bins.items()):
        n = len(data)
        correct = 0
        over_pred = 0
        under_pred = 0
        actual_over = 0
        sum_over_prob = 0.0
        sum_under_prob = 0.0

        for d in data:
            over_p = d['over_prob']
            under_p = d['under_prob']
            is_over = d['actual_total'] >= 3

            sum_over_prob += over_p
            sum_under_prob += under_p

            if over_p > 0.5:
                over_pred += 1
                if is_over:
                    correct += 1
            else:
                under_pred += 1
                if not is_over:
                    correct += 1

            if is_over:
                actual_over += 1

        # 平均3球赔率
        avg_s3_odds = sum(d['odds'][3] for d in data) / n

        result[label] = {
            'n': n,
            'accuracy': round(correct / n, 4),
            'correct': correct,
            'over_predictions': over_pred,
            'under_predictions': under_pred,
            'actual_over_rate': round(actual_over / n, 4),
            'avg_over_implied': round(sum_over_prob / n, 4),
            'avg_under_implied': round(sum_under_prob / n, 4),
            'over_bias_pp': round((actual_over / n - sum_over_prob / n) * 100, 1),
            'under_bias_pp': round(((n - actual_over) / n - sum_under_prob / n) * 100, 1),
            'avg_s3_odds': round(avg_s3_odds, 2),
        }

    return result


def compute_by_league(all_data):
    """3. 按联赛分层大小球偏差"""
    league_data = defaultdict(list)
    for d in all_data:
        league_data[d['league']].append(d)

    result = {}
    for league, data in sorted(league_data.items(), key=lambda x: -len(x[1])):
        if len(data) < 20:
            continue
        n = len(data)
        correct = 0
        actual_over = 0
        sum_over_prob = 0.0
        sum_under_prob = 0.0

        for d in data:
            over_p = d['over_prob']
            is_over = d['actual_total'] >= 3
            sum_over_prob += over_p
            sum_under_prob += d['under_prob']
            if is_over:
                actual_over += 1
            if (over_p > 0.5 and is_over) or (over_p <= 0.5 and not is_over):
                correct += 1

        avg_over_implied = sum_over_prob / n
        avg_under_implied = sum_under_prob / n
        actual_over_rate = actual_over / n

        result[league] = {
            'n': n,
            'accuracy': round(correct / n, 4),
            'actual_over_rate': round(actual_over_rate, 4),
            'avg_over_implied': round(avg_over_implied, 4),
            'avg_under_implied': round(avg_under_implied, 4),
            'over_bias_pp': round((actual_over_rate - avg_over_implied) * 100, 1),
            'under_bias_pp': round(((1 - actual_over_rate) - avg_under_implied) * 100, 1),
        }

    return result


def compute_init_final_movement(all_data_with_init_final):
    """4. 初盘vs终盘变动分析"""
    result = {
        'total_with_both': len(all_data_with_init_final),
        'movement_direction_analysis': {},
        'signal_strength': {},
        'by_handicap_movement': {},
    }

    if not all_data_with_init_final:
        return result

    # 分析初终盘变化方向
    # 初盘大球概率 -> 终盘大球概率
    init_over_probs = [d['init_over_prob'] for d in all_data_with_init_final]
    final_over_probs = [d['final_over_prob'] for d in all_data_with_init_final]
    actual_overs = [d['actual_total'] >= 3 for d in all_data_with_init_final]

    # 平均隐含概率变化
    avg_init_over = sum(init_over_probs) / len(init_over_probs)
    avg_final_over = sum(final_over_probs) / len(final_over_probs)

    # 变动方向分类
    # 升盘(大球概率上升): 初盘->终盘 大球概率增加 > 1pp
    # 降盘(大球概率下降): 初盘->终盘 大球概率减少 > 1pp
    # 基本不变: 变化在 +/-1pp 以内
    up_count = 0
    down_count = 0
    stable_count = 0
    up_correct = 0  # 升盘且实际大球
    down_correct = 0  # 降盘且实际小球
    up_wrong = 0
    down_wrong = 0

    for init_p, final_p, is_over in zip(init_over_probs, final_over_probs, actual_overs):
        change = final_p - init_p
        if change > 0.01:
            up_count += 1
            if is_over:
                up_correct += 1
            else:
                up_wrong += 1
        elif change < -0.01:
            down_count += 1
            if not is_over:
                down_correct += 1
            else:
                down_wrong += 1
        else:
            stable_count += 1

    result['movement_direction_analysis'] = {
        'avg_init_over_prob': round(avg_init_over, 4),
        'avg_final_over_prob': round(avg_final_over, 4),
        'avg_change_pp': round((avg_final_over - avg_init_over) * 100, 2),
        'up_cases': {
            'count': up_count,
            'pct': round(up_count / len(all_data_with_init_final) * 100, 1),
            'correct_when_over': up_correct,
            'wrong_when_under': up_wrong,
            'accuracy': round(up_correct / up_count * 100, 1) if up_count > 0 else 0,
        },
        'down_cases': {
            'count': down_count,
            'pct': round(down_count / len(all_data_with_init_final) * 100, 1),
            'correct_when_under': down_correct,
            'wrong_when_over': down_wrong,
            'accuracy': round(down_correct / down_count * 100, 1) if down_count > 0 else 0,
        },
        'stable_cases': {
            'count': stable_count,
            'pct': round(stable_count / len(all_data_with_init_final) * 100, 1),
        },
    }

    # 信号强度: 按变动幅度分层
    magnitude_bins = [
        (0.01, 0.03, '1-3pp'),
        (0.03, 0.05, '3-5pp'),
        (0.05, 0.10, '5-10pp'),
        (0.10, 1.0, '10pp+'),
    ]
    mag_result = {}
    for lo, hi, label in magnitude_bins:
        mag_data = [(init_p, final_p, is_over)
                    for init_p, final_p, is_over in zip(init_over_probs, final_over_probs, actual_overs)
                    if lo <= abs(final_p - init_p) < hi]
        if not mag_data:
            continue
        mn = len(mag_data)
        mag_correct = sum(1 for init_p, final_p, is_over in mag_data
                          if (final_p > init_p and is_over) or (final_p < init_p and not is_over))
        mag_result[label] = {
            'n': mn,
            'accuracy': round(mag_correct / mn * 100, 1) if mn > 0 else 0,
        }
    result['signal_strength'] = mag_result

    # 按盘口区间看初终盘变动
    by_hc = defaultdict(lambda: {'init_over': [], 'final_over': [], 'actual': []})
    for d in all_data_with_init_final:
        label = get_handicap_label(d['init_odds'][3])
        by_hc[label]['init_over'].append(d['init_over_prob'])
        by_hc[label]['final_over'].append(d['final_over_prob'])
        by_hc[label]['actual'].append(d['actual_total'] >= 3)

    hc_movement = {}
    for label, data in sorted(by_hc.items()):
        n = len(data['init_over'])
        avg_init = sum(data['init_over']) / n
        avg_final = sum(data['final_over']) / n
        actual_over_rate = sum(data['actual']) / n
        hc_movement[label] = {
            'n': n,
            'avg_init_over': round(avg_init, 4),
            'avg_final_over': round(avg_final, 4),
            'avg_change_pp': round((avg_final - avg_init) * 100, 2),
            'actual_over_rate': round(actual_over_rate, 4),
        }
    result['by_handicap_movement'] = hc_movement

    # 终盘与初盘分别的准确性对比
    init_correct = 0
    final_correct = 0
    for d in all_data_with_init_final:
        is_over = d['actual_total'] >= 3
        if (d['init_over_prob'] > 0.5 and is_over) or (d['init_over_prob'] <= 0.5 and not is_over):
            init_correct += 1
        if (d['final_over_prob'] > 0.5 and is_over) or (d['final_over_prob'] <= 0.5 and not is_over):
            final_correct += 1
    total = len(all_data_with_init_final)
    result['init_vs_final_accuracy'] = {
        'init_accuracy': round(init_correct / total, 4),
        'final_accuracy': round(final_correct / total, 4),
        'improvement_pp': round((final_correct - init_correct) / total * 100, 2),
    }

    return result


def print_report(results):
    """打印格式化的分析报告"""
    print("")
    print("=" * 70)
    print("  大小球盘口准确性分析报告 (Over/Under 2.5 Calibration)")
    print("=" * 70)

    # ---- 1. 整体大小球命中率 ----
    overall = results.get('overall', {})
    if overall:
        print(f"\n【1. 整体大小球命中率】")
        print(f"  总样本: {overall['total_matches']} 场")
        print(f"  命中率: {overall['accuracy']:.1%} ({overall['correct']}/{overall['total_matches']})")
        print(f"  Brier Score: {overall['brier_score']:.4f}")
        print(f"")
        print(f"  {'指标':>20} | {'实际频率':>10} | {'平均隐含概率':>12} | {'偏差(pp)':>10}")
        print(f"  " + "-" * 60)
        print(f"  {'大球(Over 2.5)':>20} | {overall['actual_over_rate']:>8.1%} | {overall['avg_over_implied']:>10.1%} | {overall['over_bias_pp']:>+8.1f}")
        print(f"  {'小球(Under 2.5)':>20} | {overall['actual_under_rate']:>8.1%} | {overall['avg_under_implied']:>10.1%} | {overall['under_bias_pp']:>+8.1f}")
        print(f"")
        print(f"  预测分布: 大球预测 {overall['over_predictions']} 场, 小球预测 {overall['under_predictions']} 场")
        print(f"  实际分布: 大球 {overall['actual_over']} 场 ({overall['actual_over_rate']:.1%}), 小球 {overall['actual_under']} 场 ({overall['actual_under_rate']:.1%})")

    # ---- 2. 按盘口区间偏差 ----
    by_handicap = results.get('by_handicap', {})
    if by_handicap:
        print(f"\n【2. 按盘口区间偏差表】")
        print(f"  {'盘口区间':>20} | {'样本':>6} | {'命中率':>8} | {'实际大球率':>10} | {'隐含大球率':>10} | {'偏差(pp)':>10} | {'3球均赔':>8}")
        print(f"  " + "-" * 80)
        for label, data in sorted(by_handicap.items(), key=lambda x: x[0]):
            print(f"  {label:>20} | {data['n']:>6} | {data['accuracy']:>6.1%} | "
                  f"{data['actual_over_rate']:>8.1%} | {data['avg_over_implied']:>8.1%} | "
                  f"{data['over_bias_pp']:>+8.1f} | {data['avg_s3_odds']:>6.2f}")

    # ---- 3. 按联赛分层 ----
    by_league = results.get('by_league', {})
    if by_league:
        print(f"\n【3. 按联赛分层大小球偏差 (Top 15)】")
        print(f"  {'联赛':>20} | {'样本':>6} | {'命中率':>8} | {'实际大球率':>10} | {'隐含大球率':>10} | {'偏差(pp)':>10}")
        print(f"  " + "-" * 70)
        for league, data in sorted(by_league.items(), key=lambda x: -x[1]['n'])[:15]:
            print(f"  {league:>20} | {data['n']:>6} | {data['accuracy']:>6.1%} | "
                  f"{data['actual_over_rate']:>8.1%} | {data['avg_over_implied']:>8.1%} | "
                  f"{data['over_bias_pp']:>+8.1f}")

    # ---- 4. 初终盘变动分析 ----
    init_final = results.get('init_final_movement', {})
    if init_final and init_final.get('total_with_both', 0) > 0:
        print(f"\n【4. 初盘vs终盘变动分析】")
        print(f"  同时有初终盘数据的场次: {init_final['total_with_both']} 场")

        mov = init_final.get('movement_direction_analysis', {})
        if mov:
            print(f"\n  平均隐含大球概率变化: 初盘 {mov['avg_init_over_prob']:.1%} -> 终盘 {mov['avg_final_over_prob']:.1%} ({mov['avg_change_pp']:+.2f}pp)")

            print(f"\n  变动方向分类:")
            up = mov.get('up_cases', {})
            down = mov.get('down_cases', {})
            stable = mov.get('stable_cases', {})
            print(f"    升盘(大球概率上升>1pp): {up['count']}场 ({up['pct']}%), "
                  f"实际大球率 {up['accuracy']:.1f}% (正确{up['correct_when_over']}场)")
            print(f"    降盘(大球概率下降>1pp): {down['count']}场 ({down['pct']}%), "
                  f"实际小球率 {down['accuracy']:.1f}% (正确{down['correct_when_under']}场)")
            print(f"    基本不变(+/-1pp内):  {stable['count']}场 ({stable['pct']}%)")

        sig = init_final.get('signal_strength', {})
        if sig:
            print(f"\n  变动幅度信号强度:")
            for label, data in sorted(sig.items()):
                print(f"    变动{label}: {data['n']}场, 方向准确率 {data['accuracy']:.1f}%")

        acc = init_final.get('init_vs_final_accuracy', {})
        if acc:
            print(f"\n  初盘vs终盘命中率对比:")
            print(f"    初盘命中率: {acc['init_accuracy']:.1%}")
            print(f"    终盘命中率: {acc['final_accuracy']:.1%}")
            print(f"    提升: {acc['improvement_pp']:+.2f}pp")

        hc_mov = init_final.get('by_handicap_movement', {})
        if hc_mov:
            print(f"\n  按盘口区间初终盘变动:")
            print(f"  {'盘口区间':>20} | {'样本':>6} | {'初盘大球':>10} | {'终盘大球':>10} | {'变化(pp)':>10} | {'实际大球率':>10}")
            print(f"  " + "-" * 70)
            for label, data in sorted(hc_mov.items()):
                print(f"  {label:>20} | {data['n']:>6} | {data['avg_init_over']:>8.1%} | "
                      f"{data['avg_final_over']:>8.1%} | {data['avg_change_pp']:>+8.2f} | "
                      f"{data['actual_over_rate']:>8.1%}")

    print("")
    print("=" * 70)


def main():
    print("\n大小球盘口准确性分析 (Over/Under Calibration)")
    print("=" * 60)

    conn = get_conn()
    c = conn.cursor()

    # 查询有sp_daxiao_init数据和实际比分的记录
    log("查询 sp_daxiao_init 数据...")
    c.execute('''SELECT sp_daxiao_init, sp_daxiao_final, home_score, away_score, league, result
        FROM historical_matches
        WHERE sp_daxiao_init IS NOT NULL AND sp_daxiao_init != ""
        AND home_score IS NOT NULL AND away_score IS NOT NULL''')
    rows = c.fetchall()
    log(f"  原始样本: {len(rows)} 场")

    # 解析数据
    all_data = []  # 有初盘的数据
    all_data_with_both = []  # 同时有初盘和终盘的数据

    for r in rows:
        init_odds = parse_daxiao_odds(r[0])
        if init_odds is None:
            continue

        actual_total = int(r[2]) + int(r[3])  # home_score + away_score
        over_prob, under_prob, raw_probs = compute_over_under_probs(init_odds)

        entry = {
            'odds': init_odds,
            'over_prob': over_prob,
            'under_prob': under_prob,
            'raw_probs': raw_probs,
            'actual_total': actual_total,
            'league': r[4],
            'result': r[5],
        }
        all_data.append(entry)

        # 终盘数据
        final_odds = parse_daxiao_odds(r[1])
        if final_odds is not None:
            final_over_prob, final_under_prob, _ = compute_over_under_probs(final_odds)
            entry_with_both = entry.copy()
            entry_with_both.update({
                'init_odds': init_odds,
                'final_odds': final_odds,
                'init_over_prob': over_prob,
                'final_over_prob': final_over_prob,
                'init_under_prob': under_prob,
                'final_under_prob': final_under_prob,
            })
            all_data_with_both.append(entry_with_both)

    log(f"  有效初盘样本: {len(all_data)} 场")
    log(f"  同时有初终盘: {len(all_data_with_both)} 场")

    if not all_data:
        log("错误: 无有效数据!")
        conn.close()
        return

    # 执行各项分析
    results = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'description': '大小球盘口(Over/Under 2.5)准确性分析',
        'data_source': 'historical_matches.sp_daxiao_init / sp_daxiao_final',
    }

    log("计算整体大小球命中率...")
    results['overall'] = compute_overall_accuracy(all_data)

    log("按盘口区间分层分析...")
    results['by_handicap'] = compute_by_handicap(all_data)

    log("按联赛分层分析...")
    results['by_league'] = compute_by_league(all_data)

    log("初终盘变动分析...")
    results['init_final_movement'] = compute_init_final_movement(all_data_with_both)

    # 保存JSON输出
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log(f"分析结果已保存: {OUTPUT_PATH}")

    # 打印报告
    print_report(results)

    # 关键发现摘要
    print("\n🔥 关键发现:")
    overall = results.get('overall', {})
    if overall:
        print(f"  整体大小球命中率: {overall['accuracy']:.1%} ({overall['correct']}/{overall['total_matches']})")
        print(f"  大球偏差: {overall['over_bias_pp']:+.1f}pp, 小球偏差: {overall['under_bias_pp']:+.1f}pp")

    by_handicap = results.get('by_handicap', {})
    if by_handicap:
        for label, data in sorted(by_handicap.items(), key=lambda x: -x[1]['n']):
            print(f"  {label}: 命中率{data['accuracy']:.1%}, 偏差{data['over_bias_pp']:+.1f}pp ({data['n']}场)")

    by_league = results.get('by_league', {})
    if by_league:
        top_leagues = sorted(by_league.items(), key=lambda x: -x[1]['n'])[:5]
        for league, data in top_leagues:
            print(f"  {league}: 命中率{data['accuracy']:.1%}, 大球偏差{data['over_bias_pp']:+.1f}pp ({data['n']}场)")

    init_final = results.get('init_final_movement', {})
    if init_final and init_final.get('total_with_both', 0) > 0:
        acc = init_final.get('init_vs_final_accuracy', {})
        sig = init_final.get('signal_strength', {})
        if acc:
            print(f"  初盘命中率: {acc['init_accuracy']:.1%} -> 终盘命中率: {acc['final_accuracy']:.1%} (提升{acc['improvement_pp']:+.2f}pp)")
        if sig:
            best = max(sig.items(), key=lambda x: x[1].get('accuracy', 0))
            print(f"  最佳信号: 变动{best[0]}, 方向准确率{best[1]['accuracy']:.1f}% ({best[1]['n']}场)")

    conn.close()
    print(f"\n完整分析结果已保存至: {OUTPUT_PATH}")


if __name__ == '__main__':
    main()