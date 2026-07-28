#!/usr/bin/env python3
"""
校准函数回测验证脚本
对 calibrate_global_odds_bias 和 calibrate_odds_change_signal 进行回测

回测逻辑:
1. 从数据库加载所有有赛果+HAD赔率的历史比赛
2. 用Shin's method计算隐含概率 (与预测引擎一致)
3. 分别测试: 无校准 / 仅全局偏差校准 / 仅赔率变动校准 / 双重校准
4. 比较各方案的命中率、校准改善度、对数损失
"""

import sqlite3
import json
import math
import sys
import os
from collections import defaultdict

# ============================================================
# 加载校准数据
# ============================================================
CALIBRATION_PATH = '/workspace/sporttery/predictions/league_calibration.json'
DB_PATH = '/workspace/sporttery/predictions/historical_odds.db'

with open(CALIBRATION_PATH, 'r', encoding='utf-8') as f:
    CALIBRATION = json.load(f)

# ============================================================
# Shin's method (与 v215_e2e.py 一致)
# ============================================================
def shin_method(odds_list):
    """Shin's method: 修正 favorite-longshot bias"""
    n = len(odds_list)
    inv_odds = [1.0 / o for o in odds_list]
    sum_inv = sum(inv_odds)
    z = 0.92  # 默认 Shin z参数
    
    for _ in range(10):
        probs = []
        for i in range(n):
            p = inv_odds[i] / (z + (1 - z) * sum_inv)
            probs.append(p)
        # 更新 z: z = sum((1-p_i)^2 * p_i) / sum(p_i * (1-p_i))
        num = sum((1 - p) ** 2 * p for p in probs)
        den = sum(p * (1 - p) for p in probs)
        if den > 0:
            z = num / den
        z = max(0.01, min(0.99, z))
    
    # 归一化
    s = sum(probs)
    return [p / s for p in probs]


def normalize(pw, pd, pl):
    s = pw + pd + pl
    if s <= 0:
        return 0.33, 0.33, 0.34
    return pw / s, pd / s, pl / s


# ============================================================
# 校准函数 (从 v215_e2e.py 复制)
# ============================================================
def calibrate_global_odds_bias(probs, home_odds):
    """全局赔率区间偏差校准"""
    if not CALIBRATION or not home_odds or home_odds <= 1:
        return probs
    
    global_cal = CALIBRATION.get('global_odds_calibration', {})
    if not global_cal:
        return probs
    
    label = None
    for lo, hi, lbl in [(1.0, 1.5, '1.0-1.5'), (1.5, 2.0, '1.5-2.0'),
                         (2.0, 2.5, '2.0-2.5'), (2.5, 3.5, '2.5-3.5'), (3.5, 99, '3.5+')]:
        if lo <= home_odds < hi:
            label = lbl
            break
    if not label:
        return probs
    
    cal = global_cal.get(label)
    if not cal or cal.get('sample', 0) < 50:
        return probs
    
    bias = cal.get('bias', 0)
    if abs(bias) < 0.01:
        return probs
    
    bias = max(-0.06, min(0.06, bias))
    # 回测验证: 2.5-3.5区间过度修正→跳过; 其他区间应用50%
    if label == '2.5-3.5':
        return probs
    bias = bias * 0.5
    
    pw, pd, pl = probs
    pw_new = pw + bias
    if bias > 0:
        pd_new = pd - bias * 0.3
        pl_new = pl - bias * 0.7
    else:
        pd_new = pd - bias * 0.3
        pl_new = pl - bias * 0.7
    
    pw_new = max(0.05, min(0.90, pw_new))
    pd_new = max(0.05, min(0.60, pd_new))
    pl_new = max(0.05, min(0.90, pl_new))
    
    s = pw_new + pd_new + pl_new
    return [pw_new / s, pd_new / s, pl_new / s]


def calibrate_odds_change_signal(probs, init_odds, final_odds):
    """赔率变动信号校准"""
    if not CALIBRATION:
        return probs
    
    sig = CALIBRATION.get('odds_change_signal', {})
    if not sig:
        return probs
    
    if init_odds is None or final_odds is None or init_odds <= 1 or final_odds <= 1:
        return probs
    
    change = final_odds - init_odds
    abs_change = abs(change)
    
    if abs_change < 0.01:
        return probs
    
    pw, pd, pl = probs
    base_h_rate = 0.43
    
    if change < 0:
        if abs_change < 0.1:
            target_h = 0.552
        elif abs_change < 0.3:
            target_h = 0.477
        else:
            target_h = 0.211
    else:
        if abs_change < 0.1:
            target_h = 0.524
        elif abs_change < 0.3:
            target_h = 0.308
        else:
            target_h = 0.211
    
    delta = target_h - base_h_rate
    
    # 修正量: 回测验证优化参数
    if abs_change > 0.3:
        correction = delta * 0.10
    elif abs_change < 0.1:
        correction = delta * 0.15
    else:
        correction = delta * 0.20
    
    correction = max(-0.10, min(0.10, correction))
    
    if abs(correction) < 0.005:
        return probs
    
    pw_new = pw + correction
    if correction > 0:
        pd_new = pd - correction * 0.35
        pl_new = pl - correction * 0.65
    else:
        pd_new = pd - correction * 0.35
        pl_new = pl - correction * 0.65
    
    pw_new = max(0.05, min(0.90, pw_new))
    pd_new = max(0.05, min(0.60, pd_new))
    pl_new = max(0.05, min(0.90, pl_new))
    
    s = pw_new + pd_new + pl_new
    return [pw_new / s, pd_new / s, pl_new / s]


# ============================================================
# 回测主逻辑
# ============================================================
def load_matches():
    """加载所有有赛果+HAD赔率的比赛"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''SELECT id, league, home_team, away_team, result,
                        sp_had_h, sp_had_d, sp_had_a,
                        fc_ouzhi_init_w, fc_ouzhi_final_w
                 FROM historical_matches
                 WHERE result IS NOT NULL AND result != ''
                 AND sp_had_h IS NOT NULL AND sp_had_h > 0
                 AND sp_had_d IS NOT NULL AND sp_had_d > 0
                 AND sp_had_a IS NOT NULL AND sp_had_a > 0
                 ORDER BY match_date''')
    
    matches = []
    for row in c.fetchall():
        matches.append({
            'id': row[0],
            'league': row[1],
            'home_team': row[2],
            'away_team': row[3],
            'result': row[4],  # H/D/A
            'had_h': row[5],
            'had_d': row[6],
            'had_a': row[7],
            'init_w': row[8],  # 500.com initial home odds
            'final_w': row[9],  # 500.com final home odds
        })
    
    # 加载体彩赔率变动历史 (从 odds_change_history)
    c.execute('''SELECT match_db_id, 
                 MIN(CASE WHEN seq=0 THEN h END) as init_h,
                 MAX(CASE WHEN seq=(SELECT MAX(seq) FROM odds_change_history o2 
                                   WHERE o2.match_db_id = odds_change_history.match_db_id 
                                   AND o2.odds_type='had') THEN h END) as final_h
                 FROM odds_change_history 
                 WHERE odds_type='had' AND match_db_id IS NOT NULL
                 GROUP BY match_db_id''')
    
    odds_changes = {}
    for row in c.fetchall():
        if row[1] and row[2] and row[1] > 0 and row[2] > 0:
            odds_changes[row[0]] = {'init_h': row[1], 'final_h': row[2]}
    
    # 合并到 matches
    for m in matches:
        if m['id'] in odds_changes:
            if not m['init_w'] or m['init_w'] <= 0:
                m['init_w'] = odds_changes[m['id']]['init_h']
                m['final_w'] = odds_changes[m['id']]['final_h']
            # 同时保留体彩变动数据用于单独测试
            m['sp_init_h'] = odds_changes[m['id']]['init_h']
            m['sp_final_h'] = odds_changes[m['id']]['final_h']
        else:
            m['sp_init_h'] = None
            m['sp_final_h'] = None
    
    conn.close()
    return matches


def predict_outcome(probs):
    """返回概率最高的结果: H/D/A"""
    labels = ['H', 'D', 'A']
    idx = probs.index(max(probs))
    return labels[idx]


def log_loss(probs, actual_result):
    """计算对数损失 (越低越好)"""
    label_map = {'H': 0, 'D': 1, 'A': 2}
    idx = label_map.get(actual_result, 0)
    p = max(probs[idx], 1e-10)
    return -math.log(p)


def run_backtest(matches):
    """运行回测, 比较四种方案"""
    results = {
        'baseline': {'correct': 0, 'total': 0, 'log_loss': 0, 'by_odds_range': defaultdict(lambda: {'correct': 0, 'total': 0})},
        'global_bias': {'correct': 0, 'total': 0, 'log_loss': 0, 'by_odds_range': defaultdict(lambda: {'correct': 0, 'total': 0})},
        'odds_change': {'correct': 0, 'total': 0, 'log_loss': 0, 'by_odds_range': defaultdict(lambda: {'correct': 0, 'total': 0})},
        'combined': {'correct': 0, 'total': 0, 'log_loss': 0, 'by_odds_range': defaultdict(lambda: {'correct': 0, 'total': 0})},
    }
    
    # 统计有赔率变动数据的比赛
    has_change_data = 0
    
    for m in matches:
        had_h = m['had_h']
        had_d = m['had_d']
        had_a = m['had_a']
        result = m['result']
        
        # 1. Baseline: Shin's method 无校准
        base_probs = shin_method([had_h, had_d, had_a])
        
        # 2. Global bias calibration
        gb_probs = calibrate_global_odds_bias(list(base_probs), had_h)
        
        # 3. Odds change signal calibration (优先使用体彩变动, 其次500.com)
        init_odds = m.get('sp_init_h') or m.get('init_w')
        final_odds = m.get('sp_final_h') or m.get('final_w')
        
        has_change = init_odds and final_odds and init_odds > 1 and final_odds > 1
        if has_change:
            has_change_data += 1
            oc_probs = calibrate_odds_change_signal(list(base_probs), init_odds, final_odds)
        else:
            oc_probs = list(base_probs)  # 无变动数据, 不校准
        
        # 4. Combined: 先全局偏差, 再赔率变动
        if has_change:
            comb_probs = calibrate_odds_change_signal(list(gb_probs), init_odds, final_odds)
        else:
            comb_probs = list(gb_probs)
        
        # 确定赔率区间标签
        odds_label = None
        for lo, hi, lbl in [(1.0, 1.5, '1.0-1.5'), (1.5, 2.0, '1.5-2.0'),
                             (2.0, 2.5, '2.0-2.5'), (2.5, 3.5, '2.5-3.5'), (3.5, 99, '3.5+')]:
            if lo <= had_h < hi:
                odds_label = lbl
                break
        if not odds_label:
            odds_label = 'other'
        
        # 评估各方案
        for scheme, probs in [('baseline', base_probs), ('global_bias', gb_probs),
                               ('odds_change', oc_probs), ('combined', comb_probs)]:
            pred = predict_outcome(probs)
            ll = log_loss(probs, result)
            results[scheme]['total'] += 1
            results[scheme]['correct'] += (1 if pred == result else 0)
            results[scheme]['log_loss'] += ll
            results[scheme]['by_odds_range'][odds_label]['total'] += 1
            results[scheme]['by_odds_range'][odds_label]['correct'] += (1 if pred == result else 0)
    
    return results, has_change_data


def run_odds_change_only_backtest(matches):
    """仅对有赔率变动数据的比赛进行回测"""
    results = {
        'baseline': {'correct': 0, 'total': 0, 'log_loss': 0},
        'odds_change': {'correct': 0, 'total': 0, 'log_loss': 0},
        'combined': {'correct': 0, 'total': 0, 'log_loss': 0},
        'by_change_category': defaultdict(lambda: {
            'baseline_correct': 0, 'oc_correct': 0, 'comb_correct': 0, 'total': 0
        }),
    }
    
    for m in matches:
        init_odds = m.get('sp_init_h') or m.get('init_w')
        final_odds = m.get('sp_final_h') or m.get('final_w')
        
        if not init_odds or not final_odds or init_odds <= 1 or final_odds <= 1:
            continue
        
        had_h = m['had_h']
        had_d = m['had_d']
        had_a = m['had_a']
        result = m['result']
        
        base_probs = shin_method([had_h, had_d, had_a])
        gb_probs = calibrate_global_odds_bias(list(base_probs), had_h)
        oc_probs = calibrate_odds_change_signal(list(base_probs), init_odds, final_odds)
        comb_probs = calibrate_odds_change_signal(list(gb_probs), init_odds, final_odds)
        
        # 确定变动类别
        change = final_odds - init_odds
        abs_change = abs(change)
        if abs_change < 0.01:
            cat = 'unchanged'
        elif change < 0:
            if abs_change < 0.1:
                cat = 'drop_small (<0.1)'
            elif abs_change < 0.3:
                cat = 'drop_medium (0.1-0.3)'
            else:
                cat = 'drop_large (>0.3)'
        else:
            if abs_change < 0.1:
                cat = 'rise_small (<0.1)'
            elif abs_change < 0.3:
                cat = 'rise_medium (0.1-0.3)'
            else:
                cat = 'rise_large (>0.3)'
        
        for scheme, probs in [('baseline', base_probs), ('odds_change', oc_probs), ('combined', comb_probs)]:
            pred = predict_outcome(probs)
            ll = log_loss(probs, result)
            results[scheme]['total'] += 1
            results[scheme]['correct'] += (1 if pred == result else 0)
            results[scheme]['log_loss'] += ll
        
        results['by_change_category'][cat]['total'] += 1
        results['by_change_category'][cat]['baseline_correct'] += (1 if predict_outcome(base_probs) == result else 0)
        results['by_change_category'][cat]['oc_correct'] += (1 if predict_outcome(oc_probs) == result else 0)
        results['by_change_category'][cat]['comb_correct'] += (1 if predict_outcome(comb_probs) == result else 0)
    
    return results


def print_report(results, has_change_data, total_matches, oc_results):
    """打印回测报告"""
    print("\n" + "=" * 80)
    print("  校准函数回测验证报告")
    print("=" * 80)
    
    print(f"\n  总比赛数: {total_matches}")
    print(f"  有赔率变动数据: {has_change_data}")
    
    # ---- 全量回测 ----
    print("\n" + "-" * 80)
    print("  【全量回测】所有有HAD赔率+赛果的比赛")
    print("-" * 80)
    
    print(f"\n  {'方案':<20} {'命中率':<10} {'正确/总数':<15} {'对数损失':<10} {'改善':<10}")
    print(f"  {'-'*65}")
    
    base_hit = results['baseline']['correct'] / results['baseline']['total'] if results['baseline']['total'] else 0
    base_ll = results['baseline']['log_loss'] / results['baseline']['total'] if results['baseline']['total'] else 0
    
    for scheme in ['baseline', 'global_bias', 'odds_change', 'combined']:
        r = results[scheme]
        hit = r['correct'] / r['total'] if r['total'] else 0
        ll = r['log_loss'] / r['total'] if r['total'] else 0
        delta = hit - base_hit
        delta_str = f"{delta:+.2%}" if scheme != 'baseline' else '-'
        ll_delta = ll - base_ll
        ll_delta_str = f"{ll_delta:+.4f}" if scheme != 'baseline' else '-'
        
        scheme_name = {
            'baseline': '基线 (Shin无校准)',
            'global_bias': '全局偏差校准',
            'odds_change': '赔率变动校准',
            'combined': '双重校准'
        }[scheme]
        
        print(f"  {scheme_name:<20} {hit:<10.2%} {r['correct']}/{r['total']:<12} {ll:<10.4f} {delta_str}")
    
    # ---- 按赔率区间 ----
    print("\n" + "-" * 80)
    print("  【按赔率区间命中率对比】(全局偏差校准 vs 基线)")
    print("-" * 80)
    
    print(f"\n  {'赔率区间':<12} {'样本':<6} {'基线命中率':<12} {'校准命中率':<12} {'改善':<10}")
    print(f"  {'-'*55}")
    
    for label in ['1.0-1.5', '1.5-2.0', '2.0-2.5', '2.5-3.5', '3.5+', 'other']:
        base = results['baseline']['by_odds_range'].get(label, {'correct': 0, 'total': 0})
        gb = results['global_bias']['by_odds_range'].get(label, {'correct': 0, 'total': 0})
        if base['total'] == 0:
            continue
        base_hit = base['correct'] / base['total']
        gb_hit = gb['correct'] / gb['total']
        delta = gb_hit - base_hit
        print(f"  {label:<12} {base['total']:<6} {base_hit:<12.2%} {gb_hit:<12.2%} {delta:+.2%}")
    
    # ---- 仅有赔率变动的比赛 ----
    if oc_results['baseline']['total'] > 0:
        print("\n" + "-" * 80)
        print("  【仅赔率变动比赛回测】有初赔→终赔变动数据的比赛")
        print("-" * 80)
        
        total_oc = oc_results['baseline']['total']
        print(f"\n  样本数: {total_oc}")
        
        print(f"\n  {'方案':<20} {'命中率':<10} {'正确/总数':<15} {'对数损失':<10} {'改善':<10}")
        print(f"  {'-'*65}")
        
        base_hit = oc_results['baseline']['correct'] / total_oc
        base_ll = oc_results['baseline']['log_loss'] / total_oc
        
        for scheme in ['baseline', 'odds_change', 'combined']:
            r = oc_results[scheme]
            hit = r['correct'] / r['total'] if r['total'] else 0
            ll = r['log_loss'] / r['total'] if r['total'] else 0
            delta = hit - base_hit
            delta_str = f"{delta:+.2%}" if scheme != 'baseline' else '-'
            
            scheme_name = {
                'baseline': '基线 (Shin无校准)',
                'odds_change': '赔率变动校准',
                'combined': '双重校准'
            }[scheme]
            
            print(f"  {scheme_name:<20} {hit:<10.2%} {r['correct']}/{r['total']:<12} {ll:<10.4f} {delta_str}")
        
        # 按变动类别
        print(f"\n  {'变动类别':<25} {'样本':<6} {'基线命中率':<12} {'变动校准':<12} {'双重校准':<12}")
        print(f"  {'-'*70}")
        
        for cat in ['drop_small (<0.1)', 'drop_medium (0.1-0.3)', 'drop_large (>0.3)',
                     'unchanged', 'rise_small (<0.1)', 'rise_medium (0.1-0.3)', 'rise_large (>0.3)']:
            d = oc_results['by_change_category'].get(cat)
            if not d or d['total'] == 0:
                continue
            base_h = d['baseline_correct'] / d['total']
            oc_h = d['oc_correct'] / d['total']
            comb_h = d['comb_correct'] / d['total']
            print(f"  {cat:<25} {d['total']:<6} {base_h:<12.2%} {oc_h:<12.2%} {comb_h:<12.2%}")
    
    # ---- 结论 ----
    print("\n" + "=" * 80)
    print("  【结论】")
    print("=" * 80)
    
    base_hit = results['baseline']['correct'] / results['baseline']['total']
    gb_hit = results['global_bias']['correct'] / results['global_bias']['total']
    comb_hit = results['combined']['correct'] / results['combined']['total']
    
    print(f"\n  全局偏差校准命中率: {base_hit:.2%} → {gb_hit:.2%} ({gb_hit-base_hit:+.2%})")
    if oc_results['baseline']['total'] > 0:
        oc_base = oc_results['baseline']['correct'] / oc_results['baseline']['total']
        oc_comb = oc_results['combined']['correct'] / oc_results['combined']['total']
        print(f"  双重校准(有变动数据): {oc_base:.2%} → {oc_comb:.2%} ({oc_comb-oc_base:+.2%})")
    print(f"  全量双重校准命中率: {base_hit:.2%} → {comb_hit:.2%} ({comb_hit-base_hit:+.2%})")
    
    # 判定
    if comb_hit > base_hit:
        print(f"\n  ✅ 校准函数有效! 综合命中率提升 {comb_hit-base_hit:.2%}")
    elif gb_hit > base_hit:
        print(f"\n  ⚠️ 全局偏差校准有效, 但赔率变动校准可能过度修正")
    else:
        print(f"\n  ❌ 校准函数未提升命中率, 需调整参数")


def main():
    print("=" * 80)
    print("  校准函数回测验证")
    print("=" * 80)
    
    # 加载数据
    print("\n[1] 加载历史比赛数据...")
    matches = load_matches()
    print(f"  加载 {len(matches)} 场比赛 (有HAD+赛果)")
    
    # 全量回测
    print("\n[2] 运行全量回测...")
    results, has_change_data = run_backtest(matches)
    
    # 赔率变动回测
    print("\n[3] 运行赔率变动回测...")
    oc_results = run_odds_change_only_backtest(matches)
    
    # 打印报告
    print_report(results, has_change_data, len(matches), oc_results)
    
    # 保存JSON结果
    output = {
        'total_matches': len(matches),
        'has_change_data': has_change_data,
        'baseline_hit_rate': results['baseline']['correct'] / results['baseline']['total'],
        'global_bias_hit_rate': results['global_bias']['correct'] / results['global_bias']['total'],
        'combined_hit_rate': results['combined']['correct'] / results['combined']['total'],
    }
    
    output_path = '/workspace/sporttery/predictions/backtest_results.json'
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n  结果已保存到: {output_path}")


if __name__ == '__main__':
    main()
