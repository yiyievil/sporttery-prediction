#!/usr/bin/env python3
import os
"""
体彩赔率 vs 欧/亚指数对比分析 (Ultra 10.5)
===========================================
核心问题: 体彩的sp_had/sp_hhad vs 500.com欧指fc_ouzhi, 是高/低/相同?
结合赛果: 体彩赔率偏高时的赛果分布, 偏低时的赛果分布

数据源: historical_matches (sp_had + fc_ouzhi)
样本量: 345场同时有体彩HAD和500.com欧指终盘
"""

import json
import sqlite3
from datetime import datetime
from collections import defaultdict
_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_PATH = os.path.join(_WORKSPACE, 'predictions', 'historical_odds.db')
OUTPUT_PATH = os.path.join(_WORKSPACE, 'predictions', 'sporttery_vs_europe_analysis.json')


def log(msg):
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {msg}")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def shin_method(odds):
    """Shin's method: 从赔率提取隐含概率"""
    n = len(odds)
    inv_odds = [1.0 / o for o in odds]
    s = sum(inv_odds)
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
    s2 = sum(probs)
    return [p / s2 for p in probs]


def classify_deviation(sp_prob, fc_prob, threshold=0.02):
    """判断体彩赔率相对欧指是偏高/偏低/相同"""
    diff = sp_prob - fc_prob
    if diff > threshold:
        return '偏高', diff
    elif diff < -threshold:
        return '偏低', diff
    else:
        return '相同', diff


def compute_analysis(c):
    """核心分析"""
    log("提取同时有体彩HAD和500.com欧指的数据...")

    # ================================================================
    # Part 1: 体彩HAD vs 欧指HAD
    # ================================================================
    c.execute('''SELECT hm.id, hm.match_date, hm.league, hm.home_team, hm.away_team,
                        hm.result, hm.home_score, hm.away_score,
                        hm.sp_had_h, hm.sp_had_d, hm.sp_had_a,
                        hm.fc_ouzhi_final_w, hm.fc_ouzhi_final_d, hm.fc_ouzhi_final_l,
                        hm.fc_ouzhi_init_w, hm.fc_ouzhi_init_d, hm.fc_ouzhi_init_l
                FROM historical_matches hm
                WHERE hm.result IN ('H','D','A')
                  AND hm.sp_had_h IS NOT NULL AND hm.sp_had_h > 1
                  AND hm.sp_had_d IS NOT NULL AND hm.sp_had_d > 1
                  AND hm.sp_had_a IS NOT NULL AND hm.sp_had_a > 1
                  AND hm.fc_ouzhi_final_w IS NOT NULL AND hm.fc_ouzhi_final_w > 1
                  AND hm.fc_ouzhi_final_d IS NOT NULL AND hm.fc_ouzhi_final_d > 1
                  AND hm.fc_ouzhi_final_l IS NOT NULL AND hm.fc_ouzhi_final_l > 1
            ''')
    rows = c.fetchall()
    log(f"  提取 {len(rows)} 场同时有体彩HAD+欧指终盘的数据")

    # 构建分析数据集
    records = []
    for r in rows:
        sp_odds = [r['sp_had_h'], r['sp_had_d'], r['sp_had_a']]
        fc_odds = [r['fc_ouzhi_final_w'], r['fc_ouzhi_final_d'], r['fc_ouzhi_final_l']]
        fc_init_odds = [r['fc_ouzhi_init_w'], r['fc_ouzhi_init_d'], r['fc_ouzhi_init_l']]

        sp_probs = shin_method(sp_odds)
        fc_probs = shin_method(fc_odds)
        fc_init_probs = shin_method(fc_init_odds)

        # 三个方向各自的偏差
        deviations = {}
        for i, dir_name in enumerate(['主胜', '平局', '客胜']):
            label, diff = classify_deviation(sp_probs[i], fc_probs[i])
            deviations[dir_name] = {
                'sp_prob': round(sp_probs[i], 4),
                'fc_prob': round(fc_probs[i], 4),
                'diff_pp': round(diff * 100, 1),
                'label': label,
            }

        # 模型方向: 体彩最高概率 vs 欧指最高概率
        sp_max_dir = ['H', 'D', 'A'][max(range(3), key=lambda i: sp_probs[i])]
        fc_max_dir = ['H', 'D', 'A'][max(range(3), key=lambda i: fc_probs[i])]
        direction_agreement = (sp_max_dir == fc_max_dir)

        # 体彩赔率 vs 欧指: 体彩整体溢价(overround)
        sp_margin = sum(1.0 / o for o in sp_odds) - 1
        fc_margin = sum(1.0 / o for o in fc_odds) - 1

        records.append({
            'id': r['id'],
            'date': r['match_date'],
            'league': r['league'],
            'home': r['home_team'],
            'away': r['away_team'],
            'result': r['result'],
            'home_score': r['home_score'],
            'away_score': r['away_score'],
            'sp_odds': sp_odds,
            'fc_odds': fc_odds,
            'fc_init_odds': fc_init_odds,
            'sp_probs': [round(p, 4) for p in sp_probs],
            'fc_probs': [round(p, 4) for p in fc_probs],
            'fc_init_probs': [round(p, 4) for p in fc_init_probs],
            'deviations': deviations,
            'sp_max_dir': sp_max_dir,
            'fc_max_dir': fc_max_dir,
            'direction_agreement': direction_agreement,
            'sp_margin': round(sp_margin * 100, 1),
            'fc_margin': round(fc_margin * 100, 1),
        })

    n = len(records)
    log(f"  有效记录: {n} 场")

    # ================================================================
    # 1. 整体偏差概览
    # ================================================================
    log("分析整体偏差...")

    # 各方向平均偏差
    avg_dev = {}
    for dir_name in ['主胜', '平局', '客胜']:
        diffs = [r['deviations'][dir_name]['diff_pp'] for r in records]
        avg_dev[dir_name] = {
            'avg_diff_pp': round(sum(diffs) / len(diffs), 1),
            'min_diff_pp': round(min(diffs), 1),
            'max_diff_pp': round(max(diffs), 1),
            'std_diff_pp': round((sum(d * d for d in diffs) / len(diffs) - (sum(diffs) / len(diffs)) ** 2) ** 0.5, 1),
        }

    # 体彩偏高/偏低/相同的样本占比
    deviation_counts = {'偏高': 0, '偏低': 0, '相同': 0}
    for r in records:
        # 取主胜方向的偏差
        deviation_counts[r['deviations']['主胜']['label']] += 1

    # 体彩整体溢价(overround) vs 欧指
    avg_sp_margin = sum(r['sp_margin'] for r in records) / n
    avg_fc_margin = sum(r['fc_margin'] for r in records) / n

    overall = {
        'sample': n,
        'avg_deviation_pp': avg_dev,
        'deviation_distribution': {
            label: {
                'n': cnt,
                'pct': round(cnt / n * 100, 1),
            }
            for label, cnt in deviation_counts.items()
        },
        'avg_margin': {
            'sporttery': round(avg_sp_margin, 1),
            'europe': round(avg_fc_margin, 1),
            'diff_pp': round(avg_sp_margin - avg_fc_margin, 1),
        },
    }

    # ================================================================
    # 2. 体彩偏高/偏低时的赛果分布 (核心)
    # ================================================================
    log("分析体彩赔率偏差方向与赛果的关系...")

    # 对每个方向(主胜/平局/客胜), 按体彩偏高/偏低/相同分组, 统计赛果
    result_by_deviation = {}
    for dir_name in ['主胜', '平局', '客胜']:
        dir_result = {}
        for label in ['偏高', '偏低', '相同']:
            subset = [r for r in records if r['deviations'][dir_name]['label'] == label]
            if not subset:
                continue
            sn = len(subset)
            result_counts = {'H': 0, 'D': 0, 'A': 0}
            for r in subset:
                result_counts[r['result']] += 1
            dir_result[label] = {
                'n': sn,
                'result_dist': {
                    '主胜': {'n': result_counts['H'], 'pct': round(result_counts['H'] / sn * 100, 1)},
                    '平局': {'n': result_counts['D'], 'pct': round(result_counts['D'] / sn * 100, 1)},
                    '客胜': {'n': result_counts['A'], 'pct': round(result_counts['A'] / sn * 100, 1)},
                },
            }
        result_by_deviation[dir_name] = dir_result

    # ================================================================
    # 3. 体彩vs欧指方向一致性与赛果准确率
    # ================================================================
    log("分析方向一致性...")

    agreement_analysis = {
        '方向一致': {
            'n': sum(1 for r in records if r['direction_agreement']),
            'correct_rate': round(
                sum(1 for r in records if r['direction_agreement'] and r['sp_max_dir'] == r['result']) /
                sum(1 for r in records if r['direction_agreement']) * 100, 1
            ) if sum(1 for r in records if r['direction_agreement']) > 0 else 0,
        },
        '方向不一致': {
            'n': sum(1 for r in records if not r['direction_agreement']),
            '体彩正确率': round(
                sum(1 for r in records if not r['direction_agreement'] and r['sp_max_dir'] == r['result']) /
                sum(1 for r in records if not r['direction_agreement']) * 100, 1
            ) if sum(1 for r in records if not r['direction_agreement']) > 0 else 0,
            '欧指正确率': round(
                sum(1 for r in records if not r['direction_agreement'] and r['fc_max_dir'] == r['result']) /
                sum(1 for r in records if not r['direction_agreement']) * 100, 1
            ) if sum(1 for r in records if not r['direction_agreement']) > 0 else 0,
        },
    }

    # 体彩整体方向准确率
    sp_accuracy = sum(1 for r in records if r['sp_max_dir'] == r['result']) / n
    fc_accuracy = sum(1 for r in records if r['fc_max_dir'] == r['result']) / n
    agreement_analysis['总体准确率'] = {
        'sporttery': round(sp_accuracy * 100, 1),
        'europe': round(fc_accuracy * 100, 1),
    }

    # ================================================================
    # 4. 按赔率区间分层: 体彩vs欧指偏差
    # ================================================================
    log("按赔率区间分层分析...")

    odds_bins = [(1.0, 1.5, '1.0-1.5'), (1.5, 2.0, '1.5-2.0'),
                 (2.0, 2.5, '2.0-2.5'), (2.5, 3.5, '2.5-3.5'), (3.5, 99, '3.5+')]

    by_odds_range = {}
    for lo, hi, label in odds_bins:
        bin_data = [r for r in records if lo <= r['sp_odds'][0] < hi]
        if len(bin_data) < 10:
            continue
        bn = len(bin_data)
        # 平均偏差
        avg_diff = sum(r['deviations']['主胜']['diff_pp'] for r in bin_data) / bn
        # 偏差方向分布
        high_cnt = sum(1 for r in bin_data if r['deviations']['主胜']['label'] == '偏高')
        low_cnt = sum(1 for r in bin_data if r['deviations']['主胜']['label'] == '偏低')
        # 赛果分布
        result_cnt = {'H': 0, 'D': 0, 'A': 0}
        for r in bin_data:
            result_cnt[r['result']] += 1
        by_odds_range[label] = {
            'n': bn,
            'avg_diff_pp': round(avg_diff, 1),
            'high_pct': round(high_cnt / bn * 100, 1),
            'low_pct': round(low_cnt / bn * 100, 1),
            'result_dist': {
                '主胜': round(result_cnt['H'] / bn * 100, 1),
                '平局': round(result_cnt['D'] / bn * 100, 1),
                '客胜': round(result_cnt['A'] / bn * 100, 1),
            },
        }

    # ================================================================
    # 5. 按联赛分层: 体彩vs欧指偏差
    # ================================================================
    log("按联赛分层分析...")

    league_data = defaultdict(list)
    for r in records:
        league_data[r['league']].append(r)

    by_league = {}
    for league, data in sorted(league_data.items(), key=lambda x: -len(x[1])):
        if len(data) < 10:
            continue
        ln = len(data)
        avg_diff = sum(r['deviations']['主胜']['diff_pp'] for r in data) / ln
        # 体彩准确率
        sp_hit = sum(1 for r in data if r['sp_max_dir'] == r['result']) / ln
        # 欧指准确率
        fc_hit = sum(1 for r in data if r['fc_max_dir'] == r['result']) / ln
        # 偏差方向趋势
        high_cnt = sum(1 for r in data if r['deviations']['主胜']['label'] == '偏高')
        low_cnt = sum(1 for r in data if r['deviations']['主胜']['label'] == '偏低')
        by_league[league] = {
            'n': ln,
            'avg_diff_pp': round(avg_diff, 1),
            'high_pct': round(high_cnt / ln * 100, 1),
            'low_pct': round(low_cnt / ln * 100, 1),
            'sp_accuracy': round(sp_hit * 100, 1),
            'fc_accuracy': round(fc_hit * 100, 1),
            'accuracy_diff_pp': round((sp_hit - fc_hit) * 100, 1),
        }

    # ================================================================
    # 6. 体彩溢价(overround) vs 欧指溢价
    # ================================================================
    log("分析体彩vs欧指溢价关系...")

    # 体彩margin比欧指高多少
    margin_diff = [r['sp_margin'] - r['fc_margin'] for r in records]
    avg_margin_diff = sum(margin_diff) / n

    # 体彩margin偏高时赛果分布
    sp_higher_margin = [r for r in records if r['sp_margin'] > r['fc_margin']]
    sp_lower_margin = [r for r in records if r['sp_margin'] <= r['fc_margin']]

    margin_analysis = {
        'avg_sp_margin': round(avg_sp_margin, 1),
        'avg_fc_margin': round(avg_fc_margin, 1),
        'avg_margin_diff_pp': round(avg_margin_diff, 1),
        '体彩margin更高时': {
            'n': len(sp_higher_margin),
            'sp_accuracy': round(
                sum(1 for r in sp_higher_margin if r['sp_max_dir'] == r['result']) / len(sp_higher_margin) * 100, 1
            ) if sp_higher_margin else 0,
        },
        '体彩margin更低时': {
            'n': len(sp_lower_margin),
            'sp_accuracy': round(
                sum(1 for r in sp_lower_margin if r['sp_max_dir'] == r['result']) / len(sp_lower_margin) * 100, 1
            ) if sp_lower_margin else 0,
        },
    }

    # ================================================================
    # 7. 体彩概率 vs 欧指概率的差值分布
    # ================================================================
    log("分析概率差值分布...")

    # 对主胜: 按差值大小分组看赛果
    diff_bins = [(-99, -10, '<-10pp'), (-10, -5, '-10~-5pp'), (-5, -2, '-5~-2pp'),
                 (-2, 2, '±2pp'), (2, 5, '2~5pp'), (5, 10, '5~10pp'), (10, 99, '>10pp')]

    diff_analysis = {}
    for lo, hi, label in diff_bins:
        bin_data = [r for r in records if lo <= r['deviations']['主胜']['diff_pp'] < hi]
        if not bin_data:
            continue
        bn = len(bin_data)
        result_cnt = {'H': 0, 'D': 0, 'A': 0}
        for r in bin_data:
            result_cnt[r['result']] += 1
        diff_analysis[label] = {
            'n': bn,
            'result_dist': {
                '主胜': {'n': result_cnt['H'], 'pct': round(result_cnt['H'] / bn * 100, 1)},
                '平局': {'n': result_cnt['D'], 'pct': round(result_cnt['D'] / bn * 100, 1)},
                '客胜': {'n': result_cnt['A'], 'pct': round(result_cnt['A'] / bn * 100, 1)},
            },
        }

    # ================================================================
    # 8. 方向偏差组合: 三个方向同时偏高/同时偏低等
    # ================================================================
    log("分析偏差组合模式...")

    # 三个方向中几个偏高
    high_count_dist = defaultdict(int)
    for r in records:
        hc = sum(1 for d in ['主胜', '平局', '客胜'] if r['deviations'][d]['label'] == '偏高')
        high_count_dist[hc] += 1

    # 体彩三个方向都偏高时的赛果
    all_high = [r for r in records if all(r['deviations'][d]['label'] == '偏高' for d in ['主胜', '平局', '客胜'])]
    all_low = [r for r in records if all(r['deviations'][d]['label'] == '偏低' for d in ['主胜', '平局', '客胜'])]

    combo_analysis = {
        'high_count_distribution': {
            str(k): {'n': v, 'pct': round(v / n * 100, 1)}
            for k, v in sorted(high_count_dist.items())
        },
        '三方向都偏高': {
            'n': len(all_high),
            'sp_accuracy': round(
                sum(1 for r in all_high if r['sp_max_dir'] == r['result']) / len(all_high) * 100, 1
            ) if all_high else 0,
        } if all_high else None,
        '三方向都偏低': {
            'n': len(all_low),
            'sp_accuracy': round(
                sum(1 for r in all_low if r['sp_max_dir'] == r['result']) / len(all_low) * 100, 1
            ) if all_low else 0,
        } if all_low else None,
    }

    # ================================================================
    # 9. 欧指初盘→终盘变动方向
    # ================================================================
    log("分析欧指初盘→终盘变动方向...")

    change_analysis = {}
    for dir_name, dir_idx in [('主胜', 0), ('平局', 1), ('客胜', 2)]:
        up = 0
        down = 0
        for r in records:
            try:
                # 使用初盘概率 vs 终盘概率
                fc_init_prob = r['fc_init_probs'][dir_idx]
                fc_final_prob = r['fc_probs'][dir_idx]
                if fc_final_prob > fc_init_prob:
                    up += 1
                elif fc_final_prob < fc_init_prob:
                    down += 1
            except (IndexError, ZeroDivisionError):
                continue
        change_analysis[dir_name] = {
            '欧指上涨': up,
            '欧指下跌': down,
        }

    # ================================================================
    # Part 2: 体彩内部玩法对比 (HAD vs HHAD vs 亚盘)
    # ================================================================
    # 注意: 欧指数据只有343场, 而体彩内部数据有4400+场
    # 这部分分析体彩各玩法之间的内在关系

    log("\nPart 2: 体彩内部玩法对比...")

    # 提取所有有HAD+HHAD+亚盘数据的比赛 (用于内部对比)
    c.execute('''SELECT hm.id, hm.match_date, hm.league, hm.home_team, hm.away_team,
                        hm.result, hm.home_score, hm.away_score,
                        hm.sp_had_h, hm.sp_had_d, hm.sp_had_a,
                        hm.sp_hhad_h, hm.sp_hhad_d, hm.sp_hhad_a, hm.sp_goal_line,
                        hm.sp_yazhi_init, hm.sp_yazhi_final
                FROM historical_matches hm
                WHERE hm.result IN ('H','D','A')
                  AND hm.sp_had_h IS NOT NULL AND hm.sp_had_h > 1
                  AND hm.sp_hhad_h IS NOT NULL AND hm.sp_hhad_h > 1
                  AND hm.sp_yazhi_init IS NOT NULL
            ''')
    internal_rows = c.fetchall()
    log(f"  提取 {len(internal_rows)} 场有完整体彩赔率的数据")

    internal_records = []
    for r in internal_rows:
        sp_odds = [r['sp_had_h'], r['sp_had_d'], r['sp_had_a']]
        sp_probs = shin_method(sp_odds)
        sp_max_dir = ['H', 'D', 'A'][max(range(3), key=lambda i: sp_probs[i])]

        # 解析HHAD
        hhad_odds = [r['sp_hhad_h'], r['sp_hhad_d'], r['sp_hhad_a']]
        hhad_probs = shin_method(hhad_odds)
        hhad_max_dir = ['H', 'D', 'A'][max(range(3), key=lambda i: hhad_probs[i])]

        # 解析亚盘 (格式: "goal_line|h_odds|d_odds|a_odds")
        yazhi_init = str(r['sp_yazhi_init']).split('|')
        yazhi_final = str(r['sp_yazhi_final']).split('|') if r['sp_yazhi_final'] else None
        goal_line = float(yazhi_init[0]) if len(yazhi_init) >= 1 else None
        yazhi_odds = [float(yazhi_init[1]), float(yazhi_init[2]), float(yazhi_init[3])] if len(yazhi_init) >= 4 else None
        yazhi_final_odds = [float(yazhi_final[1]), float(yazhi_final[2]), float(yazhi_final[3])] if yazhi_final and len(yazhi_final) >= 4 else None

        yazhi_probs = shin_method(yazhi_odds) if yazhi_odds and all(o > 1 for o in yazhi_odds) else None
        yazhi_max_dir = ['H', 'D', 'A'][max(range(3), key=lambda i: yazhi_probs[i])] if yazhi_probs else None

        internal_records.append({
            'id': r['id'],
            'league': r['league'],
            'result': r['result'],
            'sp_max_dir': sp_max_dir,
            'hhad_max_dir': hhad_max_dir,
            'yazhi_max_dir': yazhi_max_dir,
            'goal_line': goal_line,
            'sp_had_probs': [round(p, 4) for p in sp_probs],
            'hhad_probs': [round(p, 4) for p in hhad_probs],
            'yazhi_probs': [round(p, 4) for p in yazhi_probs] if yazhi_probs else None,
            'had_vs_hhad_agree': (sp_max_dir == hhad_max_dir),
            'had_vs_yazhi_agree': (sp_max_dir == yazhi_max_dir) if yazhi_max_dir else None,
        })

    in_n = len(internal_records)
    log(f"  有效内部记录: {in_n} 场")

    # 2a. HAD vs HHAD 方向一致性
    had_hhad_agree = sum(1 for r in internal_records if r['had_vs_hhad_agree'])
    log(f"  HAD-HHAD方向一致: {had_hhad_agree}/{in_n} ({had_hhad_agree/in_n*100:.1f}%)")

    # 按让球盘口分层
    by_goal_line = defaultdict(list)
    for r in internal_records:
        gl = r['goal_line']
        by_goal_line[gl].append(r)

    hhad_consistency = {}
    for gl in sorted(by_goal_line.keys(), key=lambda x: (abs(x), x)):
        data = by_goal_line[gl]
        ln = len(data)
        agree = sum(1 for r in data if r['had_vs_hhad_agree'])
        sp_hit = sum(1 for r in data if r['sp_max_dir'] == r['result'])
        hhad_hit = sum(1 for r in data if r['hhad_max_dir'] == r['result'])
        hhad_consistency[str(gl)] = {
            'n': ln,
            'had_hhad_agree_pct': round(agree / ln * 100, 1),
            'had_accuracy': round(sp_hit / ln * 100, 1),
            'hhad_accuracy': round(hhad_hit / ln * 100, 1),
        }

    # 2b. HAD vs 亚盘 方向一致性
    had_yazhi_agree = sum(1 for r in internal_records if r['had_vs_yazhi_agree'] is True)
    had_yazhi_total = sum(1 for r in internal_records if r['had_vs_yazhi_agree'] is not None)
    log(f"  HAD-亚盘方向一致: {had_yazhi_agree}/{had_yazhi_total} ({had_yazhi_agree/had_yazhi_total*100:.1f}%)")

    # 2c. 体彩各玩法准确率对比
    sp_accuracy_internal = sum(1 for r in internal_records if r['sp_max_dir'] == r['result']) / in_n
    hhad_accuracy_internal = sum(1 for r in internal_records if r['hhad_max_dir'] == r['result']) / in_n
    yazhi_accuracy = 0
    yazhi_n = 0
    for r in internal_records:
        if r['yazhi_max_dir'] is not None:
            yazhi_n += 1
            if r['yazhi_max_dir'] == r['result']:
                yazhi_accuracy += 1
    yazhi_accuracy = yazhi_accuracy / yazhi_n if yazhi_n > 0 else 0

    internal_analysis = {
        'sample': in_n,
        'had_vs_hhad': {
            'consistency_pct': round(had_hhad_agree / in_n * 100, 1),
            'by_goal_line': hhad_consistency,
        },
        'had_vs_yazhi': {
            'consistency_pct': round(had_yazhi_agree / had_yazhi_total * 100, 1) if had_yazhi_total > 0 else 0,
        },
        'accuracy_comparison': {
            'had': round(sp_accuracy_internal * 100, 1),
            'hhad': round(hhad_accuracy_internal * 100, 1),
            'yazhi': round(yazhi_accuracy * 100, 1),
        },
    }

    # ================================================================
    # 构建最终输出
    # ================================================================
    result = {
        'metadata': {
            'version': 'Ultra 10.5',
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'description': '体彩vs欧指偏差分析 — 体彩sp_had vs 500.com欧指fc_ouzhi',
            'total_sample': n,
        },
        'part1_had_vs_europe': {
            'overall': overall,
            'result_by_deviation': result_by_deviation,
            'agreement_analysis': agreement_analysis,
            'by_odds_range': by_odds_range,
            'by_league': by_league,
            'margin_analysis': margin_analysis,
            'diff_analysis': diff_analysis,
            'combo_analysis': combo_analysis,
        },
        'part2_internal_comparison': internal_analysis,
    }

    return result


def main():
    log("=" * 60)
    log("体彩vs欧指偏差分析 (Ultra 10.5)")
    log("=" * 60)

    conn = get_conn()
    c = conn.cursor()

    # 先检查数据量
    c.execute('''SELECT COUNT(*) FROM historical_matches 
        WHERE sp_had_h IS NOT NULL AND sp_had_h > 1
        AND fc_ouzhi_final_w IS NOT NULL AND fc_ouzhi_final_w > 1''')
    total = c.fetchone()[0]
    log(f"同时有体彩HAD+欧指终盘的数据: {total} 条")

    # 执行分析
    result = compute_analysis(c)

    # 输出到文件
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log(f"分析结果已保存到: {OUTPUT_PATH}")

    # 打印关键发现
    print()
    print("=" * 60)
    print("关键发现 - Part 1: 体彩HAD vs 欧指")
    print("=" * 60)

    p1 = result['part1_had_vs_europe']
    o = p1['overall']
    print(f"\n📊 整体概况 ({o['sample']}场)")
    print(f"  体彩平均溢价: {o['avg_margin']['sporttery']}%")
    print(f"  欧指平均溢价: {o['avg_margin']['europe']}%")
    print(f"  溢价差: {o['avg_margin']['diff_pp']}pp")

    print(f"\n📊 体彩vs欧指偏差方向分布:")
    for label, data in o['deviation_distribution'].items():
        print(f"  {label}: {data['n']}场 ({data['pct']}%)")

    print(f"\n📊 体彩vs欧指方向准确率:")
    a = p1['agreement_analysis']
    print(f"  体彩: {a['总体准确率']['sporttery']}%")
    print(f"  欧指: {a['总体准确率']['europe']}%")
    print(f"  方向一致: {a['方向一致']['n']}场 (准确率{a['方向一致']['correct_rate']}%)")

    print(f"\n📊 体彩赔率偏高/偏低时的赛果分布 (主胜方向):")
    for label, data in p1['result_by_deviation']['主胜'].items():
        rd = data['result_dist']
        print(f"  {label} ({data['n']}场): 主胜{rd['主胜']['pct']}% 平局{rd['平局']['pct']}% 客胜{rd['客胜']['pct']}%")

    print(f"\n📊 概率差值分层赛果 (主胜):")
    for label, data in p1['diff_analysis'].items():
        rd = data['result_dist']
        if data['n'] >= 10:
            print(f"  {label:12s} ({data['n']:3d}场): 主胜{rd['主胜']['pct']:5.1f}% 平局{rd['平局']['pct']:5.1f}% 客胜{rd['客胜']['pct']:5.1f}%")

    print()
    print("=" * 60)
    print("关键发现 - Part 2: 体彩内部玩法对比")
    print("=" * 60)

    p2 = result['part2_internal_comparison']
    print(f"\n📊 体彩各玩法准确率 ({p2['sample']}场):")
    print(f"  HAD:   {p2['accuracy_comparison']['had']}%")
    print(f"  HHAD:  {p2['accuracy_comparison']['hhad']}%")
    print(f"  亚盘:  {p2['accuracy_comparison']['yazhi']}%")

    print(f"\n📊 HAD vs HHAD 方向一致性: {p2['had_vs_hhad']['consistency_pct']}%")
    print(f"📊 HAD vs 亚盘 方向一致性: {p2['had_vs_yazhi']['consistency_pct']}%")

    print(f"\n📊 按让球盘口分层的HAD/HHAD准确率:")
    for gl, data in sorted(p2['had_vs_hhad']['by_goal_line'].items(), key=lambda x: (abs(float(x[0])), float(x[0]))):
        if data['n'] >= 100:
            print(f"  让{gl}球 ({data['n']:4d}场): HAD准确率{data['had_accuracy']:5.1f}% HHAD准确率{data['hhad_accuracy']:5.1f}% 一致率{data['had_hhad_agree_pct']:5.1f}%")

    conn.close()


if __name__ == '__main__':
    main()