#!/usr/bin/env python3
import os
"""
体彩各玩法（TTG/HAFU/CRS）赔率偏差分析 (Ultra 10.2)
================================================
利用历史数据库中体彩盘口数据，量化分析各玩法：
1. 隐含概率 vs 实际结果的系统性偏差
2. 按联赛/赔率区间/选项特征的分层偏差
3. 生成校准因子，修正模型预测概率

数据源: historical_matches.sp_ttg_data/sp_hafu_data/sp_crs_data
样本量: TTG 4437场, HAFU 4413场, CRS 4436场 (均有比分)
"""

import json
import sqlite3
from datetime import datetime
from collections import defaultdict
_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_PATH = os.path.join(_WORKSPACE, 'predictions', 'historical_odds.db')
OUTPUT_PATH = os.path.join(_WORKSPACE, 'predictions', 'sporttery_pools_calibration.json')


def log(msg):
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {msg}")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def shin_method(odds):
    """Shin's method: 从赔率提取隐含概率, 修正favorite-longshot bias"""
    n = len(odds)
    # 迭代求解eta
    inv_odds = [1.0 / o for o in odds]
    s = sum(inv_odds)
    # 初始值: 取最小eta使得所有概率>0
    eta = 0.0
    for _ in range(100):
        denom = 1 + eta
        probs = [(1.0 / o) / denom for o in odds]
        c = sum(p * (1 - p) for p in probs)
        new_eta = (s - 1) / (1 - c / denom) if c > 0 else 0
        if abs(new_eta - eta) < 1e-8:
            break
        eta = new_eta
    denom = 1 + eta
    probs = [(1.0 / o) / denom for o in odds]
    return [p / sum(probs) for p in probs]


def compute_ttg_analysis(c):
    """TTG (总进球数) 偏差分析
    
    体彩TTG: 0球,1球,2球,3球,4球,5球,6球,7+球
    数据: JSON格式 {"s0": odds, "s1": odds, ..., "s7": odds}
    """
    log("分析 TTG (总进球数) 赔率偏差...")
    
    c.execute('''SELECT sp_ttg_data, home_score, away_score, league, result
        FROM historical_matches 
        WHERE sp_ttg_data IS NOT NULL AND sp_ttg_data != "" 
        AND home_score IS NOT NULL AND away_score IS NOT NULL''')
    rows = c.fetchall()
    log(f"  TTG样本: {len(rows)} 场")
    
    # 每场比赛的: 各选项odds, 实际总进球
    all_data = []
    for r in rows:
        try:
            d = json.loads(r[0])
            odds = [float(d.get(f's{i}', 0)) for i in range(8)]  # s0~s7
            if any(o <= 1 for o in odds):
                continue
            actual_total = r[1] + r[2]
            # 实际总进球所属选项
            actual_opt = min(actual_total, 7)  # 7+视为7
            all_data.append({
                'odds': odds,
                'actual': actual_total,
                'actual_opt': actual_opt,
                'league': r[3],
                'result': r[4],
            })
        except (json.JSONDecodeError, ValueError, KeyError):
            continue
    
    log(f"  TTG有效样本: {len(all_data)} 场")
    if not all_data:
        return {'error': '无有效数据'}
    
    # 1. 整体偏差: 每个选项的隐含概率 vs 实际频率
    opt_labels = ['0球', '1球', '2球', '3球', '4球', '5球', '6球', '7+球']
    opt_counts = [0] * 8
    opt_implied = [0.0] * 8
    
    for d in all_data:
        opt_counts[d['actual_opt']] += 1
        probs = shin_method(d['odds'])
        for i in range(8):
            opt_implied[i] += probs[i]
    
    n = len(all_data)
    overall = {}
    margin_sum = 0.0
    for i in range(8):
        actual_freq = opt_counts[i] / n
        avg_implied = opt_implied[i] / n
        # 隐含概率的均值(从赔率)
        avg_odds = sum(d['odds'][i] for d in all_data) / n
        shin_implied = shin_method([avg_odds])[0] if avg_odds > 1 else 0
        bias = actual_freq - avg_implied
        overall[opt_labels[i]] = {
            'n': opt_counts[i],
            'actual_freq': round(actual_freq, 4),
            'avg_implied': round(avg_implied, 4),
            'bias_pp': round(bias * 100, 1),
            'avg_odds': round(avg_odds, 2),
        }
        # 赔率margin
        margin_sum += 1.0 / avg_odds if avg_odds > 1 else 0
    overall['margin'] = round((margin_sum - 1) * 100, 1)
    
    # 2. 按赔率区间分层 (以3球赔率为基准)
    by_odds_range = {}
    odds_ranges = [(1.0, 2.0, '1.0-2.0'), (2.0, 3.0, '2.0-3.0'),
                   (3.0, 5.0, '3.0-5.0'), (5.0, 10.0, '5.0-10.0'), (10.0, 99, '10.0+')]
    
    for lo, hi, label in odds_ranges:
        bin_data = [d for d in all_data if lo <= d['odds'][3] < hi]  # 3球赔率
        if len(bin_data) < 30:
            continue
        bin_counts = [0] * 8
        bin_implied = [0.0] * 8
        for d in bin_data:
            bin_counts[d['actual_opt']] += 1
            probs = shin_method(d['odds'])
            for i in range(8):
                bin_implied[i] += probs[i]
        bn = len(bin_data)
        by_odds_range[label] = {
            'n': bn,
            'bias_by_option': {}
        }
        for i in range(8):
            af = bin_counts[i] / bn
            ai = bin_implied[i] / bn
            by_odds_range[label]['bias_by_option'][opt_labels[i]] = {
                'actual_freq': round(af, 4),
                'avg_implied': round(ai, 4),
                'bias_pp': round((af - ai) * 100, 1),
            }
    
    # 3. 按联赛分层
    by_league = {}
    league_data = defaultdict(list)
    for d in all_data:
        league_data[d['league']].append(d)
    
    for league, data in sorted(league_data.items(), key=lambda x: -len(x[1])):
        if len(data) < 20:
            continue
        lc = [0] * 8
        li = [0.0] * 8
        for d in data:
            lc[d['actual_opt']] += 1
            probs = shin_method(d['odds'])
            for i in range(8):
                li[i] += probs[i]
        ln = len(data)
        entry = {'n': ln}
        # 主要偏差: 2球、3球、4球
        for opt_idx in [2, 3, 4]:
            af = lc[opt_idx] / ln
            ai = li[opt_idx] / ln
            entry[opt_labels[opt_idx]] = {
                'actual_freq': round(af, 4),
                'avg_implied': round(ai, 4),
                'bias_pp': round((af - ai) * 100, 1),
            }
        by_league[league] = entry
    
    # 4. 大/小球方向偏差
    # 定义: 0-2球=小, 3球=中, 4+球=大
    actual_small = sum(opt_counts[0:3])
    actual_mid = opt_counts[3]
    actual_big = sum(opt_counts[4:8])
    implied_small = sum(opt_implied[0:3]) / n
    implied_mid = opt_implied[3] / n
    implied_big = sum(opt_implied[4:8]) / n
    
    direction_bias = {
        '小(0-2球)': {
            'n': actual_small,
            'actual_freq': round(actual_small / n, 4),
            'avg_implied': round(implied_small, 4),
            'bias_pp': round((actual_small / n - implied_small) * 100, 1),
        },
        '中(3球)': {
            'n': actual_mid,
            'actual_freq': round(actual_mid / n, 4),
            'avg_implied': round(implied_mid, 4),
            'bias_pp': round((actual_mid / n - implied_mid) * 100, 1),
        },
        '大(4+球)': {
            'n': actual_big,
            'actual_freq': round(actual_big / n, 4),
            'avg_implied': round(implied_big, 4),
            'bias_pp': round((actual_big / n - implied_big) * 100, 1),
        },
    }
    
    return {
        'sample': n,
        'margin': overall['margin'],
        'overall': overall,
        'direction_bias': direction_bias,
        'by_odds_range': by_odds_range,
        'by_league': by_league,
    }


def compute_hafu_analysis(c):
    """HAFU (半全场) 偏差分析
    
    体彩HAFU: hh(胜胜),hd(胜平),ha(胜负),dh(平胜),dd(平平),
               da(平负),ah(负胜),ad(负平),aa(负负)
    数据: JSON格式 {"hh": odds, "hd": odds, ...}
    """
    log("分析 HAFU (半全场) 赔率偏差...")
    
    c.execute('''SELECT sp_hafu_data, home_score, away_score, league, result
        FROM historical_matches 
        WHERE sp_hafu_data IS NOT NULL AND sp_hafu_data != "" 
        AND home_score IS NOT NULL AND away_score IS NOT NULL''')
    rows = c.fetchall()
    log(f"  HAFU样本: {len(rows)} 场")
    
    # 注意: 数据库中的比分是全场比分, 我们没有半场比分
    # 因此HAFU分析只能分析"全场比分隐含的半全场方向", 这需要半场比分
    # 但我们没有半场比分, 所以只能分析"隐含概率"的分布特征, 而非实际命中率
    # 
    # 替代方案: 分析"体彩HAFU赔率 vs 模型推算的半全场概率"的偏差
    # 或者: 分析HAFU各选项赔率的边际分布特征
    
    # HAFU选项映射
    hafu_keys = ['hh', 'hd', 'ha', 'dh', 'dd', 'da', 'ah', 'ad', 'aa']
    hafu_labels = ['胜胜', '胜平', '胜负', '平胜', '平平', '平负', '负胜', '负平', '负负']
    key_to_label = dict(zip(hafu_keys, hafu_labels))
    label_to_key = dict(zip(hafu_labels, hafu_keys))
    
    all_data = []
    for r in rows:
        try:
            d = json.loads(r[0])
            odds = [float(d.get(k, 0)) for k in hafu_keys]
            if any(o <= 1 for o in odds):
                continue
            all_data.append({
                'odds': odds,
                'odds_dict': {k: float(d.get(k, 0)) for k in hafu_keys},
                'league': r[3],
                'result': r[4],
            })
        except (json.JSONDecodeError, ValueError, KeyError):
            continue
    
    log(f"  HAFU有效样本: {len(all_data)} 场")
    if not all_data:
        return {'error': '无有效数据'}
    
    n = len(all_data)
    
    # 1. 整体隐含概率分布 (无实际半场数据, 只能分析赔率本身特征)
    avg_implied = [0.0] * 9
    avg_odds = [0.0] * 9
    for d in all_data:
        probs = shin_method(d['odds'])
        for i in range(9):
            avg_implied[i] += probs[i]
            avg_odds[i] += d['odds'][i]
    
    # 2. 按主胜赔率区间分层
    by_odds_range = {}
    odds_ranges = [(1.0, 1.5, '1.0-1.5'), (1.5, 2.0, '1.5-2.0'),
                   (2.0, 3.0, '2.0-3.0'), (3.0, 99, '3.0+')]
    
    # 使用HAD主胜赔率做分层: 需要从 match_four_source 或 historical_matches 获取
    # 这里用HAFU中"胜胜"赔率近似
    for lo, hi, label in odds_ranges:
        bin_data = [d for d in all_data if lo <= d['odds_dict'].get('hh', 99) < hi]
        if len(bin_data) < 30:
            continue
        bin_implied = [0.0] * 9
        for d in bin_data:
            probs = shin_method(d['odds'])
            for i in range(9):
                bin_implied[i] += probs[i]
        bn = len(bin_data)
        by_odds_range[label] = {
            'n': bn,
            'avg_probs': {hafu_labels[i]: round(bin_implied[i] / bn, 4) for i in range(9)},
        }
    
    # 3. 按联赛分层
    by_league = {}
    league_data = defaultdict(list)
    for d in all_data:
        league_data[d['league']].append(d)
    
    for league, data in sorted(league_data.items(), key=lambda x: -len(x[1])):
        if len(data) < 20:
            continue
        li = [0.0] * 9
        for d in data:
            probs = shin_method(d['odds'])
            for i in range(9):
                li[i] += probs[i]
        ln = len(data)
        by_league[league] = {
            'n': ln,
            'avg_probs': {hafu_labels[i]: round(li[i] / ln, 4) for i in range(9)},
        }
    
    # 4. 赔率margin分析
    margin_sum = 0.0
    for d in all_data:
        margin_sum += sum(1.0 / o for o in d['odds'])
    avg_margin = round((margin_sum / n - 1) * 100, 1)
    
    # 5. 相关性分析: hafu选项之间的隐含概率关系
    # 例如: "胜胜"概率 vs "平平"概率的比值
    overall_avg = {hafu_labels[i]: round(avg_implied[i] / n, 4) for i in range(9)}
    overall_odds = {hafu_labels[i]: round(avg_odds[i] / n, 2) for i in range(9)}
    
    # 找到"胜胜"概率最高的场次, 看其他选项分布
    top_hh = sorted(all_data, key=lambda d: -shin_method(d['odds'])[0])[:100]
    top_hh_avg = [0.0] * 9
    for d in top_hh:
        probs = shin_method(d['odds'])
        for i in range(9):
            top_hh_avg[i] += probs[i]
    top_hh_probs = {hafu_labels[i]: round(top_hh_avg[i] / 100, 4) for i in range(9)}
    
    return {
        'sample': n,
        'margin': avg_margin,
        'overall_avg_probs': overall_avg,
        'overall_avg_odds': overall_odds,
        'by_odds_range': by_odds_range,
        'by_league': by_league,
        'top_hh_pattern': top_hh_probs,
    }


def compute_crs_analysis(c):
    """CRS (正确比分) 偏差分析
    
    体彩CRS: 31个比分选项 + 3个"其他"选项
    比分键: s00s00, s00s01, ..., s05s02 (最多5-2)
    其他键: s-1sh(其他主胜), s-1sd(其他平局), s-1sa(其他客胜)
    f后缀: 标记字段(是否启用)
    """
    log("分析 CRS (正确比分) 赔率偏差...")
    
    c.execute('''SELECT sp_crs_data, home_score, away_score, league, result
        FROM historical_matches 
        WHERE sp_crs_data IS NOT NULL AND sp_crs_data != "" 
        AND home_score IS NOT NULL AND away_score IS NOT NULL''')
    rows = c.fetchall()
    log(f"  CRS样本: {len(rows)} 场")
    
    # 解析比分键
    # 格式: s{home_score}s{away_score}
    # 有效比分: 00-00, 00-01, ..., 05-02 (主0-5, 客0-3)
    score_keys = []
    for h in range(6):  # 0-5
        for a in range(4):  # 0-3
            score_keys.append(f's{h:02d}s{a:02d}')
    other_keys = ['s-1sh', 's-1sd', 's-1sa']
    all_keys = score_keys + other_keys
    
    # 比分映射: 键 -> (home_goals, away_goals)
    score_map = {}
    for k in score_keys:
        parts = k[1:].split('s')
        score_map[k] = (int(parts[0]), int(parts[1]))
    for k in other_keys:
        score_map[k] = None  # 其他选项
    
    # 比分标签
    score_labels = {}
    for h in range(6):
        for a in range(4):
            score_labels[f's{h:02d}s{a:02d}'] = f'{h}-{a}'
    score_labels['s-1sh'] = '其他主胜'
    score_labels['s-1sd'] = '其他平局'
    score_labels['s-1sa'] = '其他客胜'
    
    all_data = []
    for r in rows:
        try:
            d = json.loads(r[0])
            actual_h = r[1]
            actual_a = r[2]
            
            # 确定实际比分对应的键
            if actual_h <= 5 and actual_a <= 3:
                actual_key = f's{actual_h:02d}s{actual_a:02d}'
            else:
                # 其他比分
                if actual_h > actual_a:
                    actual_key = 's-1sh'
                elif actual_h == actual_a:
                    actual_key = 's-1sd'
                else:
                    actual_key = 's-1sa'
            
            # 提取所有有效赔率
            odds_list = []
            for k in all_keys:
                v = d.get(k)
                if v is not None:
                    try:
                        odds = float(v)
                        if odds > 1:
                            odds_list.append(odds)
                    except (ValueError, TypeError):
                        pass
            
            actual_odds = float(d.get(actual_key, 0))
            if actual_odds <= 0 or len(odds_list) < 5:
                continue
            
            all_data.append({
                'odds_dict': d,
                'odds_list': odds_list,
                'actual_key': actual_key,
                'actual_h': actual_h,
                'actual_a': actual_a,
                'actual_odds': actual_odds,
                'league': r[3],
                'result': r[4],
            })
        except (json.JSONDecodeError, ValueError, KeyError):
            continue
    
    log(f"  CRS有效样本: {len(all_data)} 场")
    if not all_data:
        return {'error': '无有效数据'}
    
    n = len(all_data)
    
    # 1. 整体偏差: 每个比分选项的隐含概率 vs 实际频率
    overall = {}
    key_counts = defaultdict(int)
    key_implied_sum = defaultdict(float)
    
    for d in all_data:
        key_counts[d['actual_key']] += 1
        # 用Shin method从所有有效赔率中提取概率
        probs = shin_method(d['odds_list'])
        # 找到实际比分键对应的概率索引
        # 由于每个场次的键集会变化, 需要按键匹配
        odds_keys = [k for k in all_keys if k in d['odds_dict'] and float(d['odds_dict'][k]) > 1]
        if d['actual_key'] in odds_keys:
            idx = odds_keys.index(d['actual_key'])
            if idx < len(probs):
                key_implied_sum[d['actual_key']] += probs[idx]
    
    for key in sorted(set(list(key_counts.keys()) + list(key_implied_sum.keys()))):
        label = score_labels.get(key, key)
        actual = key_counts.get(key, 0)
        implied = key_implied_sum.get(key, 0.0)
        if actual < 5:
            continue
        bias = (actual / n) - (implied / n)
        overall[label] = {
            'n': actual,
            'actual_freq': round(actual / n, 4),
            'avg_implied': round(implied / n, 4),
            'bias_pp': round(bias * 100, 1),
        }
    
    # 2. 按比分大类聚合
    # 主胜比分: 1-0, 2-0, 2-1, 3-0, 3-1, 3-2, 4-0, 4-1, 4-2, 5-0, 5-1, 5-2 + 其他主胜
    # 平局比分: 0-0, 1-1, 2-2, 3-3 + 其他平局
    # 客胜比分: 0-1, 0-2, 1-2, 0-3, 1-3, 2-3, 0-4, 1-4, 2-4, 0-5, 1-5, 2-5 + 其他客胜
    
    def classify_result(h, a):
        if h > a:
            return 'H', '主胜'
        elif h == a:
            return 'D', '平局'
        else:
            return 'A', '客胜'
    
    def classify_score_group(h, a):
        if h <= 5 and a <= 3:
            return f'{h}-{a}'
        elif h > a:
            return '其他主胜'
        elif h == a:
            return '其他平局'
        else:
            return '其他客胜'
    
    cat_actual = {'主胜': 0, '平局': 0, '客胜': 0}
    cat_implied = {'主胜': 0.0, '平局': 0.0, '客胜': 0.0}
    
    for d in all_data:
        _, cat = classify_result(d['actual_h'], d['actual_a'])
        cat_actual[cat] += 1
        odds_keys = [k for k in all_keys if k in d['odds_dict'] and float(d['odds_dict'][k]) > 1]
        probs = shin_method(d['odds_list'])
        # Shin概率已归一化(和为1), 按比分大类累加
        for i, k in enumerate(odds_keys):
            if i < len(probs):
                if k in score_keys:
                    h, a = score_map[k]
                    _, c = classify_result(h, a)
                    cat_implied[c] += probs[i]
                elif k == 's-1sh':
                    cat_implied['主胜'] += probs[i]
                elif k == 's-1sd':
                    cat_implied['平局'] += probs[i]
                elif k == 's-1sa':
                    cat_implied['客胜'] += probs[i]
    
    category_bias = {}
    for cat in ['主胜', '平局', '客胜']:
        af = cat_actual[cat] / n
        ai = cat_implied[cat] / n
        category_bias[cat] = {
            'n': cat_actual[cat],
            'actual_freq': round(af, 4),
            'avg_implied': round(ai, 4),
            'bias_pp': round((af - ai) * 100, 1),
        }
    
    # 3. 按联赛分层
    by_league = {}
    league_data = defaultdict(list)
    for d in all_data:
        league_data[d['league']].append(d)
    
    for league, data in sorted(league_data.items(), key=lambda x: -len(x[1])):
        if len(data) < 20:
            continue
        la = {'主胜': 0, '平局': 0, '客胜': 0}
        for d in data:
            _, c = classify_result(d['actual_h'], d['actual_a'])
            la[c] += 1
        ln = len(data)
        by_league[league] = {
            'n': ln,
            '主胜率': round(la['主胜'] / ln, 4),
            '平局率': round(la['平局'] / ln, 4),
            '客胜率': round(la['客胜'] / ln, 4),
        }
    
    # 4. 赔率margin分析
    margin_sum = 0.0
    for d in all_data:
        margin_sum += sum(1.0 / o for o in d['odds_list'])
    avg_margin = round((margin_sum / n - 1) * 100, 1)
    
    # 5. 热门比分偏差
    top_actual = sorted(overall.items(), key=lambda x: -x[1]['n'])[:10]
    
    return {
        'sample': n,
        'margin': avg_margin,
        'overall': overall,
        'category_bias': category_bias,
        'top_actual_bias': [(k, v) for k, v in top_actual],
        'by_league': by_league,
    }


def print_report(results):
    """打印分析报告"""
    print("")
    print("=" * 60)
    print("  体彩各玩法赔率偏差分析报告")
    print("=" * 60)
    
    # ===== TTG =====
    ttg = results.get('ttg', {})
    if ttg and 'error' not in ttg:
        print(f"\n【1. TTG (总进球数)】样本: {ttg['sample']}场, 平均margin: {ttg['margin']}%")
        print(f"{'选项':>8} | {'样本':>6} | {'实际频率':>10} | {'隐含概率':>10} | {'偏差(pp)':>10}")
        print("-" * 55)
        for opt, data in ttg['overall'].items():
            if isinstance(data, dict) and 'actual_freq' in data:
                print(f"{opt:>8} | {data['n']:>6} | {data['actual_freq']:>8.1%} | {data['avg_implied']:>8.1%} | {data['bias_pp']:>+8.1f}")
        
        print(f"\n  方向偏差:")
        for dir_name, dir_data in ttg.get('direction_bias', {}).items():
            print(f"    {dir_name}: 实际{dir_data['actual_freq']:.1%} vs 隐含{dir_data['avg_implied']:.1%} = {dir_data['bias_pp']:+.1f}pp")
        
        print(f"\n  按赔率区间(3球)分层:")
        for rng, rng_data in sorted(ttg.get('by_odds_range', {}).items()):
            opt_biases = rng_data.get('bias_by_option', {})
            b3 = opt_biases.get('3球', {})
            b2 = opt_biases.get('2球', {})
            b4 = opt_biases.get('4球', {})
            print(f"    初赔{rng} ({rng_data['n']}场): 2球{b2.get('bias_pp',0):+5.1f}pp, "
                  f"3球{b3.get('bias_pp',0):+5.1f}pp, 4球{b4.get('bias_pp',0):+5.1f}pp")
        
        print(f"\n  按联赛分层 (Top 10):")
        for league, lg_data in sorted(ttg.get('by_league', {}).items(), key=lambda x: -x[1]['n'])[:10]:
            b2 = lg_data.get('2球', {}).get('bias_pp', 0)
            b3 = lg_data.get('3球', {}).get('bias_pp', 0)
            b4 = lg_data.get('4球', {}).get('bias_pp', 0)
            print(f"    {league}: {lg_data['n']}场 | 2球{b2:+5.1f}pp, 3球{b3:+5.1f}pp, 4球{b4:+5.1f}pp")
    
    # ===== HAFU =====
    hafu = results.get('hafu', {})
    if hafu and 'error' not in hafu:
        print(f"\n【2. HAFU (半全场)】样本: {hafu['sample']}场, 平均margin: {hafu['margin']}%")
        print(f"{'选项':>8} | {'平均赔率':>10} | {'隐含概率':>10}")
        print("-" * 35)
        for opt in ['胜胜', '胜平', '胜负', '平胜', '平平', '平负', '负胜', '负平', '负负']:
            odds = hafu.get('overall_avg_odds', {}).get(opt, 0)
            prob = hafu.get('overall_avg_probs', {}).get(opt, 0)
            print(f"{opt:>8} | {odds:>8.2f} | {prob:>8.1%}")
        
        print(f"\n  胜胜高概率场次其他选项分布:")
        for opt, prob in hafu.get('top_hh_pattern', {}).items():
            val = prob * 100
            if val > 1:
                print(f"    {opt}: {val:.1f}%")
    
    # ===== CRS =====
    crs = results.get('crs', {})
    if crs and 'error' not in crs:
        print(f"\n【3. CRS (正确比分)】样本: {crs['sample']}场, 平均margin: {crs['margin']}%")
        
        print(f"  比分大类偏差:")
        for cat, cat_data in crs.get('category_bias', {}).items():
            print(f"    {cat}: 实际{cat_data['actual_freq']:.1%} vs 隐含{cat_data['avg_implied']:.1%} = {cat_data['bias_pp']:+.1f}pp")
        
        print(f"\n  热门比分偏差 (Top 10):")
        for label, data in crs.get('top_actual_bias', []):
            print(f"    {label}: {data['n']}场, 实际{data['actual_freq']:.1%}, 隐含{data['avg_implied']:.1%}, 偏差{data['bias_pp']:+.1f}pp")
    
    print("")
    print("=" * 60)


def generate_calibration(results):
    """生成校准因子, 供预测流程使用"""
    calibration = {
        'version': 'Ultra 10.2',
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'description': '体彩各玩法赔率偏差校准因子 — 修正模型预测概率为体彩真实概率',
        'ttg': {},
        'hafu': {},
        'crs': {},
    }
    
    # TTG校准
    ttg = results.get('ttg', {})
    if ttg and 'error' not in ttg:
        # 方向偏差校准
        dir_bias = ttg.get('direction_bias', {})
        calibration['ttg']['direction_calibration'] = {}
        for dir_name, dir_data in dir_bias.items():
            calibration['ttg']['direction_calibration'][dir_name] = {
                'bias_pp': dir_data['bias_pp'],
                'sample': dir_data['n'],
            }
        
        # 选项偏差校准
        calibration['ttg']['option_calibration'] = {}
        for opt, data in ttg['overall'].items():
            if isinstance(data, dict) and 'bias_pp' in data:
                calibration['ttg']['option_calibration'][opt] = {
                    'bias_pp': data['bias_pp'],
                    'sample': data['n'],
                }
        
        # 按赔率区间的校准
        calibration['ttg']['by_odds_range'] = {}
        for rng, rng_data in ttg.get('by_odds_range', {}).items():
            calibration['ttg']['by_odds_range'][rng] = {}
            for opt, opt_data in rng_data.get('bias_by_option', {}).items():
                if abs(opt_data['bias_pp']) > 1:
                    calibration['ttg']['by_odds_range'][rng][opt] = {
                        'bias_pp': opt_data['bias_pp'],
                    }
        
        # 按联赛的校准
        calibration['ttg']['by_league'] = {}
        for league, lg_data in ttg.get('by_league', {}).items():
            entry = {}
            for opt_key in ['2球', '3球', '4球']:
                if opt_key in lg_data and abs(lg_data[opt_key]['bias_pp']) > 2:
                    entry[opt_key] = {'bias_pp': lg_data[opt_key]['bias_pp']}
            if entry:
                calibration['ttg']['by_league'][league] = entry
    
    # HAFU校准 (仅平均概率分布, 无实际半场数据)
    hafu = results.get('hafu', {})
    if hafu and 'error' not in hafu:
        calibration['hafu']['avg_probs'] = hafu.get('overall_avg_probs', {})
        calibration['hafu']['margin'] = hafu.get('margin', 0)
        calibration['hafu']['by_league'] = {}
        for league, lg_data in hafu.get('by_league', {}).items():
            calibration['hafu']['by_league'][league] = lg_data.get('avg_probs', {})
    
    # CRS校准
    crs = results.get('crs', {})
    if crs and 'error' not in crs:
        calibration['crs']['category_bias'] = crs.get('category_bias', {})
        calibration['crs']['margin'] = crs.get('margin', 0)
        calibration['crs']['by_league'] = {}
        for league, lg_data in crs.get('by_league', {}).items():
            calibration['crs']['by_league'][league] = {
                '主胜率': lg_data['主胜率'],
                '平局率': lg_data['平局率'],
                '客胜率': lg_data['客胜率'],
            }
    
    return calibration


def main():
    print("\n🚀 体彩各玩法赔率偏差分析 (Ultra 10.2)")
    print("=" * 60)
    
    conn = get_conn()
    c = conn.cursor()
    
    results = {}
    results['ttg'] = compute_ttg_analysis(c)
    results['hafu'] = compute_hafu_analysis(c)
    results['crs'] = compute_crs_analysis(c)
    
    conn.close()
    
    print_report(results)
    
    calibration = generate_calibration(results)
    
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(calibration, f, ensure_ascii=False, indent=2)
    print(f"✅ 校准因子已保存: {OUTPUT_PATH}")
    
    # 关键发现
    print("\n🔥 关键发现:")
    ttg = results.get('ttg', {})
    if ttg and 'error' not in ttg:
        db = ttg.get('direction_bias', {})
        for dir_name, dir_data in db.items():
            print(f"  TTG {dir_name}: 偏差{dir_data['bias_pp']:+.1f}pp ({dir_data['n']}场)")
    
    hafu = results.get('hafu', {})
    if hafu and 'error' not in hafu:
        print(f"  HAFU: 平均margin {hafu['margin']}%, 样本{hafu['sample']}场")
    
    crs = results.get('crs', {})
    if crs and 'error' not in crs:
        cb = crs.get('category_bias', {})
        for cat, cat_data in cb.items():
            print(f"  CRS {cat}: 偏差{cat_data['bias_pp']:+.1f}pp ({cat_data['n']}场)")


if __name__ == '__main__':
    main()