#!/usr/bin/env python3
import os
"""
体彩赔率变动特征分析 (Ultra 10.6)
===================================
分析体彩赔率从开盘到收盘的变动模式，以及与欧指变动的对比。

数据源:
  - odds_change_history (92,947条, 含HAD/HHAD/TTG/HAFU/CRS)
  - historical_matches (含sp_had终盘 + fc_ouzhi初/终盘)

核心问题:
  1. 体彩HAD赔率变动方向与赛果的关系
  2. 体彩变动 vs 欧指变动的一致性
  3. 变动幅度与赛果准确率
  4. 体彩HAD vs HHAD变动联动
  5. 变动方向组合模式的预测价值
"""

import json
import sqlite3
from datetime import datetime
from collections import defaultdict
_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_PATH = os.path.join(_WORKSPACE, 'predictions', 'historical_odds.db')
OUTPUT_PATH = os.path.join(_WORKSPACE, 'predictions', 'odds_change_analysis.json')


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


def classify_change(open_odds, close_odds, threshold=0.02):
    """判断赔率变动方向: 上升/下降/不变"""
    open_prob = 1.0 / open_odds
    close_prob = 1.0 / close_odds
    diff = close_prob - open_prob
    if diff > threshold:
        return '上升', diff
    elif diff < -threshold:
        return '下降', diff
    else:
        return '不变', diff


def get_open_close(c, odds_type):
    """从odds_change_history获取每场比赛的初盘(seq=0)和末盘(last seq)"""
    c.execute(f'''SELECT 
        och0.match_db_id as id,
        och0.h as open_h, och0.d as open_d, och0.a as open_a,
        och_last.h as close_h, och_last.d as close_d, och_last.a as close_a,
        och_last.seq as last_seq
    FROM odds_change_history och0
    JOIN odds_change_history och_last 
        ON och_last.match_db_id = och0.match_db_id 
        AND och_last.odds_type = och0.odds_type
        AND och_last.seq = (
            SELECT MAX(seq) FROM odds_change_history 
            WHERE match_db_id = och0.match_db_id AND odds_type = '{odds_type}'
        )
    WHERE och0.odds_type = '{odds_type}' AND och0.seq = 0
    ''')
    rows = c.fetchall()
    result = {}
    for r in rows:
        d = dict(r)
        result[d['id']] = d
    return result


def compute_analysis(c):
    """核心分析"""

    # ================================================================
    # Part 1: 体彩HAD赔率变动与赛果的关系 (4247场比赛)
    # ================================================================
    log("Part 1: 体彩HAD赔率变动分析...")

    had_open_close = get_open_close(c, 'had')
    log(f"  提取 {len(had_open_close)} 场HAD开盘-收盘数据")

    # 关联赛果
    c.execute('''SELECT hm.id, hm.result, hm.league, hm.home_score, hm.away_score,
                        hm.sp_had_h as final_h
                FROM historical_matches hm
                WHERE hm.result IN ('H','D','A')''')
    match_results = {}
    for r in c.fetchall():
        match_results[r['id']] = dict(r)

    # 构建分析数据集
    records = []
    for mid, oc in had_open_close.items():
        if mid not in match_results:
            continue
        mr = match_results[mid]

        # 开盘概率 (Shin)
        open_odds = [oc['open_h'], oc['open_d'], oc['open_a']]
        close_odds = [oc['close_h'], oc['close_d'], oc['close_a']]
        if any(o <= 1 for o in open_odds) or any(o <= 1 for o in close_odds):
            continue

        open_probs = shin_method(open_odds)
        close_probs = shin_method(close_odds)

        # 各方向变动
        h_change, h_diff = classify_change(oc['open_h'], oc['close_h'])
        d_change, d_diff = classify_change(oc['open_d'], oc['close_d'])
        a_change, a_diff = classify_change(oc['open_a'], oc['close_a'])

        # 开盘方向 vs 收盘方向
        open_dir = ['H', 'D', 'A'][max(range(3), key=lambda i: open_probs[i])]
        close_dir = ['H', 'D', 'A'][max(range(3), key=lambda i: close_probs[i])]
        dir_changed = (open_dir != close_dir)

        # 变动幅度(主胜)
        h_prob_diff = close_probs[0] - open_probs[0]

        records.append({
            'id': mid,
            'league': mr['league'],
            'result': mr['result'],
            'open_h': oc['open_h'], 'close_h': oc['close_h'],
            'open_d': oc['open_d'], 'close_d': oc['close_d'],
            'open_a': oc['open_a'], 'close_a': oc['close_a'],
            'open_probs': [round(p, 4) for p in open_probs],
            'close_probs': [round(p, 4) for p in close_probs],
            'h_change': h_change, 'h_diff_pp': round(h_diff * 100, 1),
            'd_change': d_change, 'd_diff_pp': round(d_diff * 100, 1),
            'a_change': a_change, 'a_diff_pp': round(a_diff * 100, 1),
            'h_prob_diff_pp': round(h_prob_diff * 100, 1),
            'open_dir': open_dir, 'close_dir': close_dir,
            'dir_changed': dir_changed,
            'final_h': mr['final_h'],
        })

    n = len(records)
    log(f"  有效记录: {n} 场")

    # --- 1a. 主胜变动方向与赛果 ---
    log("  分析主胜变动方向与赛果...")

    change_result = {}
    for change_label in ['上升', '下降', '不变']:
        subset = [r for r in records if r['h_change'] == change_label]
        if not subset:
            continue
        sn = len(subset)
        result_cnt = {'H': 0, 'D': 0, 'A': 0}
        for r in subset:
            result_cnt[r['result']] += 1
        change_result[change_label] = {
            'n': sn,
            'pct': round(sn / n * 100, 1),
            'result_dist': {
                '主胜': {'n': result_cnt['H'], 'pct': round(result_cnt['H'] / sn * 100, 1)},
                '平局': {'n': result_cnt['D'], 'pct': round(result_cnt['D'] / sn * 100, 1)},
                '客胜': {'n': result_cnt['A'], 'pct': round(result_cnt['A'] / sn * 100, 1)},
            },
            'avg_h_prob_diff_pp': round(sum(r['h_prob_diff_pp'] for r in subset) / sn, 1),
        }

    # --- 1b. 变动幅度分层 ---
    log("  分析变动幅度分层...")

    diff_bins = [(-99, -5, '<-5pp'), (-5, -2, '-5~-2pp'), (-2, -0.5, '-2~-0.5pp'),
                 (-0.5, 0.5, '±0.5pp'), (0.5, 2, '0.5~2pp'), (2, 5, '2~5pp'), (5, 99, '>5pp')]

    diff_analysis = {}
    for lo, hi, label in diff_bins:
        bin_data = [r for r in records if lo <= r['h_prob_diff_pp'] < hi]
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

    # --- 1c. 方向转变(开盘→收盘)与赛果 ---
    log("  分析方向转变...")

    # 开盘方向不变 vs 改变
    unchanged_dir = [r for r in records if not r['dir_changed']]
    changed_dir = [r for r in records if r['dir_changed']]

    dir_change_analysis = {
        '方向不变': {
            'n': len(unchanged_dir),
            'accuracy': round(
                sum(1 for r in unchanged_dir if r['close_dir'] == r['result']) / len(unchanged_dir) * 100, 1
            ) if unchanged_dir else 0,
        },
        '方向转变': {
            'n': len(changed_dir),
            'accuracy': round(
                sum(1 for r in changed_dir if r['close_dir'] == r['result']) / len(changed_dir) * 100, 1
            ) if changed_dir else 0,
            '开盘方向准确率': round(
                sum(1 for r in changed_dir if r['open_dir'] == r['result']) / len(changed_dir) * 100, 1
            ) if changed_dir else 0,
        },
    }

    # --- 1d. 按联赛分层: 变动方向与赛果 ---
    log("  按联赛分层...")

    league_data = defaultdict(list)
    for r in records:
        league_data[r['league']].append(r)

    by_league = {}
    for league, data in sorted(league_data.items(), key=lambda x: -len(x[1])):
        if len(data) < 30:
            continue
        ln = len(data)
        # 主胜赔率上升/下降的场次
        up = len([r for r in data if r['h_change'] == '上升'])
        down = len([r for r in data if r['h_change'] == '下降'])
        # 整体准确率
        accuracy = sum(1 for r in data if r['close_dir'] == r['result']) / ln
        # 下降时的准确率(主胜赔率下降→看好客胜)
        down_data = [r for r in data if r['h_change'] == '下降']
        down_accuracy = sum(1 for r in down_data if r['close_dir'] == r['result']) / len(down_data) if down_data else 0
        by_league[league] = {
            'n': ln,
            'up_pct': round(up / ln * 100, 1),
            'down_pct': round(down / ln * 100, 1),
            'accuracy': round(accuracy * 100, 1),
            'down_accuracy': round(down_accuracy * 100, 1),
        }

    # --- 1e. 变动组合模式 ---
    log("  分析变动组合模式...")

    # 主胜上升+客胜下降(看好主胜) vs 主胜下降+客胜上升(看好客胜)
    combo_result = {}
    # 模式1: 主胜上升+客胜下降
    pat1 = [r for r in records if r['h_change'] == '上升' and r['a_change'] == '下降']
    # 模式2: 主胜下降+客胜上升
    pat2 = [r for r in records if r['h_change'] == '下降' and r['a_change'] == '上升']
    # 模式3: 主胜上升+客胜上升(全面上升)
    pat3 = [r for r in records if r['h_change'] == '上升' and r['a_change'] == '上升']
    # 模式4: 主胜下降+客胜下降(全面下降)
    pat4 = [r for r in records if r['h_change'] == '下降' and r['a_change'] == '下降']

    for name, data in [('主胜↑客胜↓(看好主队)', pat1), ('主胜↓客胜↑(看好客队)', pat2),
                        ('主胜↑客胜↑(全面上升)', pat3), ('主胜↓客胜↓(全面下降)', pat4)]:
        if not data:
            continue
        dn = len(data)
        rc = {'H': 0, 'D': 0, 'A': 0}
        for r in data:
            rc[r['result']] += 1
        combo_result[name] = {
            'n': dn,
            'result_dist': {
                '主胜': {'n': rc['H'], 'pct': round(rc['H'] / dn * 100, 1)},
                '平局': {'n': rc['D'], 'pct': round(rc['D'] / dn * 100, 1)},
                '客胜': {'n': rc['A'], 'pct': round(rc['A'] / dn * 100, 1)},
            },
        }

    # ================================================================
    # Part 2: 体彩变动 vs 欧指变动 (345场有欧指数据的比赛)
    # ================================================================
    log("\nPart 2: 体彩变动 vs 欧指变动对比...")

    c.execute('''SELECT hm.id, hm.result, hm.league,
                        hm.fc_ouzhi_init_w, hm.fc_ouzhi_init_d, hm.fc_ouzhi_init_l,
                        hm.fc_ouzhi_final_w, hm.fc_ouzhi_final_d, hm.fc_ouzhi_final_l
                FROM historical_matches hm
                WHERE hm.result IN ('H','D','A')
                  AND hm.fc_ouzhi_init_w IS NOT NULL AND hm.fc_ouzhi_init_w > 1
                  AND hm.fc_ouzhi_final_w IS NOT NULL AND hm.fc_ouzhi_final_w > 1''')
    europe_rows = {r['id']: dict(r) for r in c.fetchall()}
    log(f"  有欧指初/终盘数据的比赛: {len(europe_rows)} 场")

    # 与体彩变动数据合并
    eur_records = []
    for r in records:
        if r['id'] not in europe_rows:
            continue
        er = europe_rows[r['id']]

        # 欧指初盘→终盘概率
        fc_init = [er['fc_ouzhi_init_w'], er['fc_ouzhi_init_d'], er['fc_ouzhi_init_l']]
        fc_final = [er['fc_ouzhi_final_w'], er['fc_ouzhi_final_d'], er['fc_ouzhi_final_l']]
        if any(o <= 1 for o in fc_init) or any(o <= 1 for o in fc_final):
            continue

        fc_init_probs = shin_method(fc_init)
        fc_final_probs = shin_method(fc_final)

        fc_h_change, fc_h_diff = classify_change(fc_init[0], fc_final[0])
        fc_h_prob_diff = fc_final_probs[0] - fc_init_probs[0]

        # 体彩vs欧指变动方向是否一致
        same_direction = (r['h_change'] == fc_h_change)

        eur_records.append({
            'id': r['id'],
            'result': r['result'],
            'league': r['league'],
            'sp_h_change': r['h_change'],
            'sp_h_diff_pp': r['h_diff_pp'],
            'fc_h_change': fc_h_change,
            'fc_h_diff_pp': round(fc_h_diff * 100, 1),
            'same_direction': same_direction,
            'sp_h_prob_diff_pp': r['h_prob_diff_pp'],
            'fc_h_prob_diff_pp': round(fc_h_prob_diff * 100, 1),
            'sp_close_dir': r['close_dir'],
            'fc_close_dir': ['H', 'D', 'A'][max(range(3), key=lambda i: fc_final_probs[i])],
        })

    en = len(eur_records)
    log(f"  有效对比记录: {en} 场")

    # --- 2a. 体彩vs欧指变动方向一致性 ---
    same = sum(1 for r in eur_records if r['same_direction'])
    diff = en - same
    log(f"  变动方向一致: {same}/{en} ({same/en*100:.1f}%)")

    # 一致时的赛果准确率
    same_correct = sum(1 for r in eur_records if r['same_direction'] and r['sp_close_dir'] == r['result'])
    diff_correct_sp = sum(1 for r in eur_records if not r['same_direction'] and r['sp_close_dir'] == r['result'])
    diff_correct_fc = sum(1 for r in eur_records if not r['same_direction'] and r['fc_close_dir'] == r['result'])

    # 体彩上升vs欧指下降(背离)时
    diverge = [r for r in eur_records if r['sp_h_change'] == '上升' and r['fc_h_change'] == '下降']
    converge = [r for r in eur_records if r['sp_h_change'] == '上升' and r['fc_h_change'] == '上升']

    eur_agreement = {
        'sample': en,
        'consistency_pct': round(same / en * 100, 1),
        '一致时准确率': {
            'n': same,
            'sp_accuracy': round(same_correct / same * 100, 1) if same > 0 else 0,
        },
        '不一致时准确率': {
            'n': diff,
            'sp_accuracy': round(diff_correct_sp / diff * 100, 1) if diff > 0 else 0,
            'fc_accuracy': round(diff_correct_fc / diff * 100, 1) if diff > 0 else 0,
        },
        '体彩↑欧指↓(背离)': {
            'n': len(diverge),
            'sp_accuracy': round(
                sum(1 for r in diverge if r['sp_close_dir'] == r['result']) / len(diverge) * 100, 1
            ) if diverge else 0,
        } if diverge else None,
        '体彩↑欧指↑(同向)': {
            'n': len(converge),
            'sp_accuracy': round(
                sum(1 for r in converge if r['sp_close_dir'] == r['result']) / len(converge) * 100, 1
            ) if converge else 0,
        } if converge else None,
    }

    # --- 2b. 体彩vs欧指变动幅度对比 ---
    log("  分析变动幅度对比...")

    # 体彩变幅 > 欧指变幅
    sp_larger = [r for r in eur_records if abs(r['sp_h_prob_diff_pp']) > abs(r['fc_h_prob_diff_pp'])]
    fc_larger = [r for r in eur_records if abs(r['sp_h_prob_diff_pp']) < abs(r['fc_h_prob_diff_pp'])]

    magnitude_analysis = {
        '体彩变幅更大': {
            'n': len(sp_larger),
            'sp_accuracy': round(
                sum(1 for r in sp_larger if r['sp_close_dir'] == r['result']) / len(sp_larger) * 100, 1
            ) if sp_larger else 0,
        },
        '欧指变幅更大': {
            'n': len(fc_larger),
            'fc_accuracy': round(
                sum(1 for r in fc_larger if r['fc_close_dir'] == r['result']) / len(fc_larger) * 100, 1
            ) if fc_larger else 0,
        },
    }

    # ================================================================
    # Part 3: 体彩HHAD赔率变动 (让球盘)
    # ================================================================
    log("\nPart 3: 体彩HHAD赔率变动分析...")

    hhad_open_close = get_open_close(c, 'hhad')
    log(f"  提取 {len(hhad_open_close)} 场HHAD开盘-收盘数据")

    hhad_records = []
    for mid, oc in hhad_open_close.items():
        if mid not in match_results:
            continue
        mr = match_results[mid]

        open_odds = [oc['open_h'], oc['open_d'], oc['open_a']]
        close_odds = [oc['close_h'], oc['close_d'], oc['close_a']]
        if any(o <= 1 for o in open_odds) or any(o <= 1 for o in close_odds):
            continue

        h_hchange, _ = classify_change(oc['open_h'], oc['close_h'])
        h_dchange, _ = classify_change(oc['open_d'], oc['close_d'])
        h_achange, _ = classify_change(oc['open_a'], oc['close_a'])

        hhad_records.append({
            'id': mid,
            'result': mr['result'],
            'h_change': h_hchange,
            'd_change': h_dchange,
            'a_change': h_achange,
        })

    hhad_n = len(hhad_records)
    log(f"  有效HHAD记录: {hhad_n} 场")

    # HHAD主胜变动方向与赛果
    hhad_change_result = {}
    for change_label in ['上升', '下降', '不变']:
        subset = [r for r in hhad_records if r['h_change'] == change_label]
        if not subset:
            continue
        sn = len(subset)
        rc = {'H': 0, 'D': 0, 'A': 0}
        for r in subset:
            rc[r['result']] += 1
        hhad_change_result[change_label] = {
            'n': sn,
            'pct': round(sn / hhad_n * 100, 1),
            'result_dist': {
                '主胜': {'n': rc['H'], 'pct': round(rc['H'] / sn * 100, 1)},
                '平局': {'n': rc['D'], 'pct': round(rc['D'] / sn * 100, 1)},
                '客胜': {'n': rc['A'], 'pct': round(rc['A'] / sn * 100, 1)},
            },
        }

    # ================================================================
    # Part 4: HAD vs HHAD变动联动分析
    # ================================================================
    log("\nPart 4: HAD vs HHAD变动联动...")

    # 合并HAD和HHAD变动
    had_hhad = []
    for r in records:
        hh = next((h for h in hhad_records if h['id'] == r['id']), None)
        if not hh:
            continue
        had_hhad.append({
            'id': r['id'],
            'result': r['result'],
            'had_h': r['h_change'],
            'hhad_h': hh['h_change'],
            'had_hhad_agree_h': (r['h_change'] == hh['h_change']),
            'had_d': r['d_change'],
            'hhad_d': hh['d_change'],
            'had_hhad_agree_d': (r['d_change'] == hh['d_change']),
        })

    hh_n = len(had_hhad)
    log(f"  联动记录: {hh_n} 场")

    # HAD与HHAD主胜同时上升 vs 同时下降
    both_up = [r for r in had_hhad if r['had_h'] == '上升' and r['hhad_h'] == '上升']
    both_down = [r for r in had_hhad if r['had_h'] == '下降' and r['hhad_h'] == '下降']
    hup_hhad_down = [r for r in had_hhad if r['had_h'] == '上升' and r['hhad_h'] == '下降']
    hdown_hhad_up = [r for r in had_hhad if r['had_h'] == '下降' and r['hhad_h'] == '上升']

    linkage_analysis = {}
    for name, data in [('HAD+HHAD主胜都上升', both_up), ('HAD+HHAD主胜都下降', both_down),
                        ('HAD↑但HHAD↓', hup_hhad_down), ('HAD↓但HHAD↑', hdown_hhad_up)]:
        if not data:
            continue
        dn = len(data)
        rc = {'H': 0, 'D': 0, 'A': 0}
        for r in data:
            rc[r['result']] += 1
        linkage_analysis[name] = {
            'n': dn,
            'result_dist': {
                '主胜': {'n': rc['H'], 'pct': round(rc['H'] / dn * 100, 1)},
                '平局': {'n': rc['D'], 'pct': round(rc['D'] / dn * 100, 1)},
                '客胜': {'n': rc['A'], 'pct': round(rc['A'] / dn * 100, 1)},
            },
        }

    # ================================================================
    # Part 5: 体彩平局赔率变动与赛果
    # ================================================================
    log("\nPart 5: 平局赔率变动分析...")

    draw_change = {}
    for change_label in ['上升', '下降', '不变']:
        subset = [r for r in records if r['d_change'] == change_label]
        if not subset:
            continue
        sn = len(subset)
        rc = {'H': 0, 'D': 0, 'A': 0}
        for r in subset:
            rc[r['result']] += 1
        draw_change[change_label] = {
            'n': sn,
            'pct': round(sn / n * 100, 1),
            'result_dist': {
                '主胜': {'n': rc['H'], 'pct': round(rc['H'] / sn * 100, 1)},
                '平局': {'n': rc['D'], 'pct': round(rc['D'] / sn * 100, 1)},
                '客胜': {'n': rc['A'], 'pct': round(rc['A'] / sn * 100, 1)},
            },
        }

    # ================================================================
    # Part 6: 体彩各玩法矛盾信号 (HAD vs HHAD vs 亚盘)
    # ================================================================
    log("\nPart 6: 体彩各玩法矛盾信号...")

    # 提取HAD+HHAD+亚盘数据, 对比三者方向
    c.execute('''SELECT hm.id, hm.result,
                        hm.sp_had_h, hm.sp_had_d, hm.sp_had_a,
                        hm.sp_hhad_h, hm.sp_hhad_d, hm.sp_hhad_a, hm.sp_goal_line,
                        hm.sp_yazhi_init
                FROM historical_matches hm
                WHERE hm.result IN ('H','D','A')
                  AND hm.sp_had_h IS NOT NULL AND hm.sp_had_h > 1
                  AND hm.sp_hhad_h IS NOT NULL AND hm.sp_hhad_h > 1
                  AND hm.sp_yazhi_init IS NOT NULL
            ''')
    conflict_rows = c.fetchall()
    log(f"  提取 {len(conflict_rows)} 场有HAD+HHAD+亚盘的数据")

    conflict_records = []
    for r in conflict_rows:
        # HAD方向
        sp_odds = [r['sp_had_h'], r['sp_had_d'], r['sp_had_a']]
        sp_probs = shin_method(sp_odds)
        sp_dir = ['H', 'D', 'A'][max(range(3), key=lambda i: sp_probs[i])]
        sp_conf = max(sp_probs)  # 置信度

        # HHAD方向
        hhad_odds = [r['sp_hhad_h'], r['sp_hhad_d'], r['sp_hhad_a']]
        hhad_probs = shin_method(hhad_odds)
        hhad_dir = ['H', 'D', 'A'][max(range(3), key=lambda i: hhad_probs[i])]
        hhad_conf = max(hhad_probs)

        # 亚盘方向 (格式: "goal_line|h|d|a")
        yazhi_parts = str(r['sp_yazhi_init']).split('|')
        if len(yazhi_parts) >= 4:
            yazhi_odds = [float(yazhi_parts[1]), float(yazhi_parts[2]), float(yazhi_parts[3])]
            if all(o > 1 for o in yazhi_odds):
                yazhi_probs = shin_method(yazhi_odds)
                yazhi_dir = ['H', 'D', 'A'][max(range(3), key=lambda i: yazhi_probs[i])]
                yazhi_conf = max(yazhi_probs)
            else:
                yazhi_dir = None
                yazhi_conf = 0
        else:
            yazhi_dir = None
            yazhi_conf = 0

        # 矛盾类型
        # conflict_level = len(dirs) - len(unique_dirs)
        #   2 → 三者一致 (e.g. H,H,H)
        #   1 → 部分矛盾 (e.g. H,H,A)
        #   0 → 三者全不同 (e.g. H,D,A)
        dirs = [sp_dir, hhad_dir, yazhi_dir] if yazhi_dir else [sp_dir, hhad_dir]
        unique_dirs = set(dirs)
        conflict_level = len(dirs) - len(unique_dirs)

        # 具体矛盾类型
        conflict_type = '一致'
        if conflict_level == 2:
            conflict_type = '三方向一致' if len(dirs) == 3 else 'HAD-HHAD一致'
        elif conflict_level == 1:
            if sp_dir == hhad_dir and yazhi_dir and hhad_dir != yazhi_dir:
                conflict_type = 'HAD-HHAD vs 亚盘'
            elif hhad_dir == yazhi_dir and sp_dir != hhad_dir:
                conflict_type = '亚盘-HHAD vs HAD'
            elif sp_dir == yazhi_dir and hhad_dir != sp_dir:
                conflict_type = 'HAD-亚盘 vs HHAD'
            elif len(dirs) == 2:
                conflict_type = 'HAD-HHAD一致'
            else:
                conflict_type = '部分矛盾'
        else:  # conflict_level == 0
            conflict_type = '三方向全不同'

        # 哪个方向更可信: 看谁最终正确
        sp_correct = (sp_dir == r['result'])
        hhad_correct = (hhad_dir == r['result'])
        yazhi_correct = (yazhi_dir == r['result']) if yazhi_dir else None

        # 置信度差: HAD vs HHAD
        conf_diff = sp_conf - hhad_conf

        conflict_records.append({
            'id': r['id'],
            'result': r['result'],
            'sp_dir': sp_dir, 'sp_conf': round(sp_conf, 4),
            'hhad_dir': hhad_dir, 'hhad_conf': round(hhad_conf, 4),
            'yazhi_dir': yazhi_dir, 'yazhi_conf': round(yazhi_conf, 4) if yazhi_dir else None,
            'conflict_level': conflict_level,
            'conflict_type': conflict_type,
            'sp_correct': sp_correct,
            'hhad_correct': hhad_correct,
            'yazhi_correct': yazhi_correct,
            'conf_diff_pp': round(conf_diff * 100, 1),
        })

    cf_n = len(conflict_records)
    log(f"  有效记录: {cf_n} 场")

    # --- 6a. 矛盾类型分布 ---
    conflict_dist = defaultdict(list)
    for r in conflict_records:
        conflict_dist[r['conflict_type']].append(r)

    conflict_distribution = {}
    for ctype, data in sorted(conflict_dist.items(), key=lambda x: -len(x[1])):
        cn = len(data)
        sp_hit = sum(1 for r in data if r['sp_correct'])
        hhad_hit = sum(1 for r in data if r['hhad_correct'])
        conflict_distribution[ctype] = {
            'n': cn,
            'pct': round(cn / cf_n * 100, 1),
            'had_accuracy': round(sp_hit / cn * 100, 1),
            'hhad_accuracy': round(hhad_hit / cn * 100, 1),
        }

    # --- 6b. HAD-HHAD方向矛盾时的赛果 ---
    # HAD看好H, HHAD看好A
    had_h_hhad_a = [r for r in conflict_records if r['sp_dir'] == 'H' and r['hhad_dir'] == 'A']
    had_a_hhad_h = [r for r in conflict_records if r['sp_dir'] == 'A' and r['hhad_dir'] == 'H']
    had_d_hhad_not_d = [r for r in conflict_records if r['sp_dir'] == 'D' and r['hhad_dir'] != 'D']
    had_not_d_hhad_d = [r for r in conflict_records if r['sp_dir'] != 'D' and r['hhad_dir'] == 'D']

    conflict_patterns = {}
    for name, data in [('HAD主胜, HHAD客胜', had_h_hhad_a),
                        ('HAD客胜, HHAD主胜', had_a_hhad_h),
                        ('HAD平局, HHAD不平局', had_d_hhad_not_d),
                        ('HAD不平局, HHAD平局', had_not_d_hhad_d)]:
        if not data:
            continue
        dn = len(data)
        rc = {'H': 0, 'D': 0, 'A': 0}
        sp_hit = 0
        hhad_hit = 0
        for r in data:
            rc[r['result']] += 1
            if r['sp_correct']:
                sp_hit += 1
            if r['hhad_correct']:
                hhad_hit += 1
        conflict_patterns[name] = {
            'n': dn,
            'result_dist': {
                '主胜': {'n': rc['H'], 'pct': round(rc['H'] / dn * 100, 1)},
                '平局': {'n': rc['D'], 'pct': round(rc['D'] / dn * 100, 1)},
                '客胜': {'n': rc['A'], 'pct': round(rc['A'] / dn * 100, 1)},
            },
            'had_accuracy': round(sp_hit / dn * 100, 1),
            'hhad_accuracy': round(hhad_hit / dn * 100, 1),
        }

    # --- 6c. 置信度差信号: HAD vs HHAD谁更自信 ---
    conf_diff_analysis = {}
    for label, lo, hi in [('HAD自信度更高(>5pp)', 5, 99),
                           ('HAD略高(2~5pp)', 2, 5),
                           ('HAD-HHAD接近(±2pp)', -2, 2),
                           ('HHAD略高(-5~-2pp)', -5, -2),
                           ('HHAD自信度更高(<-5pp)', -99, -5)]:
        bin_data = [r for r in conflict_records if lo <= r['conf_diff_pp'] < hi]
        if not bin_data:
            continue
        bn = len(bin_data)
        rc = {'H': 0, 'D': 0, 'A': 0}
        sp_hit = 0
        hhad_hit = 0
        for r in bin_data:
            rc[r['result']] += 1
            if r['sp_correct']:
                sp_hit += 1
            if r['hhad_correct']:
                hhad_hit += 1
        conf_diff_analysis[label] = {
            'n': bn,
            'result_dist': {
                '主胜': {'n': rc['H'], 'pct': round(rc['H'] / bn * 100, 1)},
                '平局': {'n': rc['D'], 'pct': round(rc['D'] / bn * 100, 1)},
                '客胜': {'n': rc['A'], 'pct': round(rc['A'] / bn * 100, 1)},
            },
            'had_accuracy': round(sp_hit / bn * 100, 1),
            'hhad_accuracy': round(hhad_hit / bn * 100, 1),
        }

    # --- 6d. 三方向一致性 vs 准确率 ---
    # 三者一致 vs 部分一致 vs 全部不一致
    all_agree = [r for r in conflict_records if r['sp_dir'] == r['hhad_dir'] == r['yazhi_dir']]
    all_disagree = [r for r in conflict_records if r['yazhi_dir'] is not None
                    and len({r['sp_dir'], r['hhad_dir'], r['yazhi_dir']}) == 3]

    unanimity_analysis = {
        '三者方向一致': {
            'n': len(all_agree),
            'had_accuracy': round(
                sum(1 for r in all_agree if r['sp_correct']) / len(all_agree) * 100, 1
            ) if all_agree else 0,
        },
        '三者方向全不同': {
            'n': len(all_disagree),
            'had_accuracy': round(
                sum(1 for r in all_disagree if r['sp_correct']) / len(all_disagree) * 100, 1
            ) if all_disagree else 0,
        } if all_disagree else None,
    }

    conflict_analysis = {
        'sample': cf_n,
        'conflict_distribution': conflict_distribution,
        'conflict_patterns': conflict_patterns,
        'conf_diff_analysis': conf_diff_analysis,
        'unanimity_analysis': unanimity_analysis,
    }

    # ================================================================
    # 构建最终输出
    # ================================================================
    result = {
        'metadata': {
            'version': 'Ultra 10.6',
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'description': '体彩赔率变动特征 + 玩法矛盾信号分析',
            'total_had_sample': n,
            'total_hhad_sample': hhad_n,
            'total_europe_sample': en,
            'total_conflict_sample': cf_n,
        },
        'part1_had_change': {
            'change_vs_result': change_result,
            'diff_analysis': diff_analysis,
            'dir_change_analysis': dir_change_analysis,
            'by_league': by_league,
            'combo_analysis': combo_result,
        },
        'part2_vs_europe': {
            'agreement': eur_agreement,
            'magnitude': magnitude_analysis,
        },
        'part3_hhad_change': {
            'change_vs_result': hhad_change_result,
        },
        'part4_had_hhad_linkage': linkage_analysis,
        'part5_draw_change': draw_change,
        'part6_conflict_signal': conflict_analysis,
    }

    return result


def main():
    log("=" * 60)
    log("体彩赔率变动特征分析 (Ultra 10.6)")
    log("=" * 60)

    conn = get_conn()
    c = conn.cursor()

    result = compute_analysis(c)

    # 输出到文件
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log(f"分析结果已保存到: {OUTPUT_PATH}")

    # 打印关键发现
    print()
    print("=" * 60)
    print("关键发现 - Part 1: 体彩HAD赔率变动")
    print("=" * 60)

    p1 = result['part1_had_change']
    print(f"\n📊 HAD主胜赔率变动方向与赛果 ({result['metadata']['total_had_sample']}场):")
    for label, data in p1['change_vs_result'].items():
        rd = data['result_dist']
        print(f"  {label:8s} ({data['n']:4d}场, {data['pct']:5.1f}%): 主胜{rd['主胜']['pct']:5.1f}% 平局{rd['平局']['pct']:5.1f}% 客胜{rd['客胜']['pct']:5.1f}%  (均变幅{data['avg_h_prob_diff_pp']:+.1f}pp)")

    print(f"\n📊 变动幅度分层赛果:")
    for label, data in p1['diff_analysis'].items():
        rd = data['result_dist']
        if data['n'] >= 30:
            print(f"  {label:12s} ({data['n']:4d}场): 主胜{rd['主胜']['pct']:5.1f}% 平局{rd['平局']['pct']:5.1f}% 客胜{rd['客胜']['pct']:5.1f}%")

    print(f"\n📊 方向转变:")
    print(f"  {p1['dir_change_analysis']['方向不变']['n']}场方向不变, 准确率{p1['dir_change_analysis']['方向不变']['accuracy']}%")
    print(f"  {p1['dir_change_analysis']['方向转变']['n']}场方向转变, 收盘准确率{p1['dir_change_analysis']['方向转变']['accuracy']}% (开盘仅{p1['dir_change_analysis']['方向转变']['开盘方向准确率']}%)")

    print(f"\n📊 变动组合模式:")
    for name, data in p1['combo_analysis'].items():
        rd = data['result_dist']
        print(f"  {name:20s} ({data['n']:4d}场): 主胜{rd['主胜']['pct']:5.1f}% 平局{rd['平局']['pct']:5.1f}% 客胜{rd['客胜']['pct']:5.1f}%")

    print()
    print("=" * 60)
    print("关键发现 - Part 2: 体彩vs欧指变动对比")
    print("=" * 60)

    p2 = result['part2_vs_europe']
    a = p2['agreement']
    print(f"\n📊 变动方向一致性 ({a['sample']}场):")
    print(f"  一致: {a['consistency_pct']}% (准确率{a['一致时准确率']['sp_accuracy']}%)")
    print(f"  不一致: {100-a['consistency_pct']:.1f}% (体彩准确率{a['不一致时准确率']['sp_accuracy']}%, 欧指准确率{a['不一致时准确率']['fc_accuracy']}%)")
    if a['体彩↑欧指↓(背离)']:
        print(f"  体彩↑欧指↓(背离): {a['体彩↑欧指↓(背离)']['n']}场, 准确率{a['体彩↑欧指↓(背离)']['sp_accuracy']}%")
    if a['体彩↑欧指↑(同向)']:
        print(f"  体彩↑欧指↑(同向): {a['体彩↑欧指↑(同向)']['n']}场, 准确率{a['体彩↑欧指↑(同向)']['sp_accuracy']}%")

    print()
    print("=" * 60)
    print("关键发现 - Part 3: 体彩各玩法联动")
    print("=" * 60)

    p4 = result['part4_had_hhad_linkage']
    print(f"\n📊 HAD vs HHAD变动联动:")
    for name, data in p4.items():
        rd = data['result_dist']
        print(f"  {name:25s} ({data['n']:4d}场): 主胜{rd['主胜']['pct']:5.1f}% 平局{rd['平局']['pct']:5.1f}% 客胜{rd['客胜']['pct']:5.1f}%")

    conn.close()


if __name__ == '__main__':
    main()