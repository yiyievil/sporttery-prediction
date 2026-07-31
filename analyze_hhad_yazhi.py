#!/usr/bin/env python3
"""
让球盘（HHAD）穿盘/输盘规律分析 + 亚盘变动分析
===========================================
利用历史数据库中让球赔率和亚盘数据，分析：
  Part 1: HHAD 穿盘/输盘规律
  Part 2: 亚盘变动分析

数据源: historical_matches.sp_hhad_h/d/a, sp_goal_line, sp_yazhi_init/final
"""

import json
import os
import sqlite3
import sys
from datetime import datetime
from collections import defaultdict

# ── 路径 ──────────────────────────────────────────────────────────
DB_PATH = '/workspace/sporttery/predictions/historical_odds.db'
OUTPUT_PATH = '/workspace/sporttery/predictions/hhad_yazhi_analysis.json'

# ── 导入 Shin method ──────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from v215_e2e import shin_method


def log(msg):
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {msg}")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── 辅助函数 ──────────────────────────────────────────────────────

def parse_goal_line(gl_str):
    """解析让球线, 返回让球数值 (float)

    格式:
      "-1"  -> 主队让1球 (返回 -1.0)
      "+1"  -> 主队受让1球 (返回 +1.0)
      "-2"  -> 主队让2球 (返回 -2.0)
    """
    if not gl_str:
        return None
    try:
        return float(gl_str)
    except (ValueError, TypeError):
        return None


def classify_hhad(home_score, away_score, goal_line):
    """根据赛果和让球线判断穿盘/走水/输盘

    让球后主队得分 = home_score + goal_line  (goal_line<0 表示主队让球, 如 -1 主队让1球)
    让球后客队得分 = away_score - goal_line  (goal_line>0 表示主队受让, 如 +1 主队受让1球)

    例: 主让1球 (goal_line=-1), 比分 1-0:
      adj_home = 1 + (-1) = 0
      adj_away = 0 - (-1) = 1
      adj_home(0) < adj_away(1) -> 输盘

    例: 主让1球 (goal_line=-1), 比分 2-0:
      adj_home = 2 + (-1) = 1
      adj_away = 0 - (-1) = 1
      adj_home(1) == adj_away(1) -> 走水

    返回: 'win' (主队穿盘/赢盘), 'push' (走水), 'loss' (主队输盘)
    """
    if goal_line is None:
        return None
    adj_home = home_score + goal_line
    adj_away = away_score - goal_line
    if adj_home > adj_away:
        return 'win'
    elif adj_home < adj_away:
        return 'loss'
    else:
        return 'push'


def parse_yazhi(yazhi_str):
    """解析亚盘字符串, 返回 (handicap, home_odds, away_odds)

    格式: "盘口|上盘赔率|下盘赔率|?"
    只取前3个字段
    """
    if not yazhi_str:
        return None, None, None
    parts = yazhi_str.split('|')
    if len(parts) < 3:
        return None, None, None
    try:
        handicap = float(parts[0])
        home_odds = float(parts[1])
        away_odds = float(parts[2])
        return handicap, home_odds, away_odds
    except (ValueError, TypeError):
        return None, None, None


def handicap_direction(init_h, final_h):
    """判断盘口变化方向

    盘口数值: 负值=主队让球, 正值=主队受让
    变深: 主队让球更多 (init_h > final_h, 如 -0.75 -> -1)
    变浅: 主队让球更少 (init_h < final_h, 如 -1 -> -0.75)
    不变: init_h == final_h
    """
    if init_h is None or final_h is None:
        return None
    # 主队让球更少 = 盘口变浅; 主队让球更多 = 盘口变深
    # 对于负值: -1 -> -0.75 是变浅 (init_h < final_h)
    # 对于负值: -1 -> -1.25 是变深 (init_h > final_h)
    # 对于正值: +0.75 -> +1 是变浅 (主队受让更多)
    # 对于正值: +1 -> +0.75 是变深 (主队受让更少)
    if abs(init_h - final_h) < 0.01:
        return 'unchanged'
    if init_h > final_h:
        return 'deepen'   # 盘口变深 (主队让球增加或受让减少)
    else:
        return 'shallow'  # 盘口变浅 (主队让球减少或受让增加)


# ── 数据加载 ──────────────────────────────────────────────────────

def load_data(c):
    """加载所有需要的数据"""
    log("加载 HHAD + 亚盘数据...")
    c.execute('''
        SELECT sp_hhad_h, sp_hhad_d, sp_hhad_a, sp_goal_line,
               sp_yazhi_init, sp_yazhi_final,
               home_score, away_score, half_home_score, half_away_score,
               league, result
        FROM historical_matches
        WHERE sp_hhad_h IS NOT NULL AND sp_hhad_h > 0
          AND home_score IS NOT NULL AND away_score IS NOT NULL
    ''')
    rows = c.fetchall()
    log(f"  原始行数: {len(rows)}")

    records = []
    for r in rows:
        try:
            # HHAD赔率
            odds_h = float(r['sp_hhad_h'])
            odds_d = float(r['sp_hhad_d'])
            odds_a = float(r['sp_hhad_a'])
            if odds_h <= 1 or odds_d <= 1 or odds_a <= 1:
                continue

            goal_line = parse_goal_line(r['sp_goal_line'])
            if goal_line is None:
                continue

            home_score = r['home_score']
            away_score = r['away_score']

            # 亚盘
            yazhi_init = parse_yazhi(r['sp_yazhi_init'])
            yazhi_final = parse_yazhi(r['sp_yazhi_final'])

            # 半场比分
            half_h = r['half_home_score']
            half_a = r['half_away_score']

            # 穿盘分类
            hhad_result = classify_hhad(home_score, away_score, goal_line)
            if hhad_result is None:
                continue

            records.append({
                'odds_h': odds_h,
                'odds_d': odds_d,
                'odds_a': odds_a,
                'goal_line': goal_line,
                'home_score': home_score,
                'away_score': away_score,
                'half_home_score': half_h if half_h is not None else 0,
                'half_away_score': half_a if half_a is not None else 0,
                'league': r['league'] or '未知',
                'result': r['result'] or '?',
                'hhad_result': hhad_result,
                'yazhi_init': yazhi_init,
                'yazhi_final': yazhi_final,
            })
        except (ValueError, TypeError):
            continue

    log(f"  有效记录: {len(records)} 条")
    return records


# ── Part 1: HHAD 穿盘/输盘规律 ─────────────────────────────────────

def compute_hhad_analysis(records):
    """Part 1: HHAD 穿盘/输盘规律分析"""
    log("=" * 50)
    log("Part 1: HHAD 穿盘/输盘规律分析")
    log("=" * 50)

    n = len(records)

    # 1. 整体穿盘/走水/输盘分布
    log("  1.1 整体穿盘/走水/输盘分布...")
    result_counts = {'win': 0, 'push': 0, 'loss': 0}
    for r in records:
        result_counts[r['hhad_result']] += 1

    overall_hhad = {
        'sample': n,
        'win': {
            'n': result_counts['win'],
            'pct': round(result_counts['win'] / n, 4),
        },
        'push': {
            'n': result_counts['push'],
            'pct': round(result_counts['push'] / n, 4),
        },
        'loss': {
            'n': result_counts['loss'],
            'pct': round(result_counts['loss'] / n, 4),
        },
    }

    # 2. 隐含概率 vs 实际穿盘率
    log("  1.2 隐含概率 vs 实际穿盘率...")
    sum_implied_win = 0.0
    sum_implied_push = 0.0
    sum_implied_loss = 0.0

    for r in records:
        probs = shin_method([r['odds_h'], r['odds_d'], r['odds_a']])
        # probs = [p_win(HHAD主胜=穿盘), p_push(HHAD平=走水), p_loss(HHAD客胜=输盘)]
        sum_implied_win += probs[0]
        sum_implied_push += probs[1]
        sum_implied_loss += probs[2]

    implied_vs_actual = {
        'win_cover': {
            'actual_freq': round(result_counts['win'] / n, 4),
            'avg_implied_prob': round(sum_implied_win / n, 4),
            'bias_pp': round((result_counts['win'] / n - sum_implied_win / n) * 100, 1),
        },
        'push': {
            'actual_freq': round(result_counts['push'] / n, 4),
            'avg_implied_prob': round(sum_implied_push / n, 4),
            'bias_pp': round((result_counts['push'] / n - sum_implied_push / n) * 100, 1),
        },
        'loss': {
            'actual_freq': round(result_counts['loss'] / n, 4),
            'avg_implied_prob': round(sum_implied_loss / n, 4),
            'bias_pp': round((result_counts['loss'] / n - sum_implied_loss / n) * 100, 1),
        },
    }

    # 3. 按让球数分层
    log("  1.3 按让球数分层...")
    by_goal_line = {}
    gl_groups = defaultdict(list)
    for r in records:
        gl_groups[r['goal_line']].append(r)

    for gl in sorted(gl_groups.keys(), key=lambda x: (abs(x), x)):
        data = gl_groups[gl]
        m = len(data)
        if m < 10:
            continue
        w = sum(1 for d in data if d['hhad_result'] == 'win')
        p = sum(1 for d in data if d['hhad_result'] == 'push')
        l = sum(1 for d in data if d['hhad_result'] == 'loss')

        # 让球描述
        if gl < 0:
            desc = f"主让{abs(int(gl))}球" if gl == int(gl) else f"主让{abs(gl):.2f}球"
        elif gl > 0:
            desc = f"主受让{int(gl)}球" if gl == int(gl) else f"主受让{gl:.2f}球"
        else:
            desc = "平手盘"

        by_goal_line[str(gl)] = {
            'desc': desc,
            'n': m,
            'win': {'n': w, 'pct': round(w / m, 4)},
            'push': {'n': p, 'pct': round(p / m, 4)},
            'loss': {'n': l, 'pct': round(l / m, 4)},
            'win_rate': round(w / m, 4),
        }

    # 4. 按联赛分层
    log("  1.4 按联赛分层...")
    by_league = {}
    league_groups = defaultdict(list)
    for r in records:
        league_groups[r['league']].append(r)

    for league in sorted(league_groups.keys(), key=lambda lg: -len(league_groups[lg])):
        data = league_groups[league]
        m = len(data)
        if m < 20:
            continue
        w = sum(1 for d in data if d['hhad_result'] == 'win')
        p = sum(1 for d in data if d['hhad_result'] == 'push')
        l = sum(1 for d in data if d['hhad_result'] == 'loss')

        by_league[league] = {
            'n': m,
            'win_rate': round(w / m, 4),
            'push_rate': round(p / m, 4),
            'loss_rate': round(l / m, 4),
        }

    # 5. 按赔率区间分层
    log("  1.5 按赔率区间分层...")
    by_odds_range = {}
    odds_ranges = [
        (1.0, 1.5, '1.0-1.5'),
        (1.5, 2.0, '1.5-2.0'),
        (2.0, 3.0, '2.0-3.0'),
        (3.0, 999, '3.0+'),
    ]

    for lo, hi, label in odds_ranges:
        bin_data = [r for r in records if lo <= r['odds_h'] < hi]
        if len(bin_data) < 30:
            continue
        m = len(bin_data)
        w = sum(1 for d in bin_data if d['hhad_result'] == 'win')
        p = sum(1 for d in bin_data if d['hhad_result'] == 'push')
        l = sum(1 for d in bin_data if d['hhad_result'] == 'loss')

        # 此区间的平均隐含概率
        avg_implied = [0.0, 0.0, 0.0]
        for d in bin_data:
            probs = shin_method([d['odds_h'], d['odds_d'], d['odds_a']])
            avg_implied[0] += probs[0]
            avg_implied[1] += probs[1]
            avg_implied[2] += probs[2]

        by_odds_range[label] = {
            'n': m,
            'avg_odds_h': round(sum(d['odds_h'] for d in bin_data) / m, 3),
            'win_rate': round(w / m, 4),
            'push_rate': round(p / m, 4),
            'loss_rate': round(l / m, 4),
            'avg_implied_win': round(avg_implied[0] / m, 4),
            'avg_implied_push': round(avg_implied[1] / m, 4),
            'avg_implied_loss': round(avg_implied[2] / m, 4),
            'bias_win_pp': round((w / m - avg_implied[0] / m) * 100, 1),
        }

    return {
        'overall_hhad': overall_hhad,
        'implied_vs_actual': implied_vs_actual,
        'by_goal_line': by_goal_line,
        'by_league': by_league,
        'by_odds_range': by_odds_range,
    }


# ── Part 2: 亚盘变动分析 ──────────────────────────────────────────

def compute_yazhi_analysis(records):
    """Part 2: 亚盘变动分析"""
    log("=" * 50)
    log("Part 2: 亚盘变动分析")
    log("=" * 50)

    # 筛选有亚盘数据的记录
    yazhi_records = [
        r for r in records
        if r['yazhi_init'][0] is not None and r['yazhi_final'][0] is not None
    ]
    log(f"  有亚盘数据的记录: {len(yazhi_records)} 条")

    if not yazhi_records:
        return {'error': '无亚盘数据'}

    n = len(yazhi_records)

    # 6. 盘口变化分类
    log("  2.1 盘口变化方向统计...")
    direction_counts = {'deepen': 0, 'shallow': 0, 'unchanged': 0}
    for r in yazhi_records:
        init_h = r['yazhi_init'][0]
        final_h = r['yazhi_final'][0]
        direction = handicap_direction(init_h, final_h)
        if direction:
            direction_counts[direction] += 1

    # 7. 盘口变化方向 vs 主队赢盘率
    log("  2.2 盘口变化方向 vs 主队赢盘率...")
    direction_results = defaultdict(lambda: {'win': 0, 'push': 0, 'loss': 0, 'total': 0})
    for r in yazhi_records:
        init_h = r['yazhi_init'][0]
        final_h = r['yazhi_final'][0]
        direction = handicap_direction(init_h, final_h)
        if direction is None:
            continue
        direction_results[direction]['total'] += 1
        direction_results[direction][r['hhad_result']] += 1

    direction_analysis = {}
    for d in ['deepen', 'shallow', 'unchanged']:
        dr = direction_results[d]
        t = dr['total']
        if t == 0:
            continue
        direction_analysis[d] = {
            'n': t,
            'win': {'n': dr['win'], 'pct': round(dr['win'] / t, 4)},
            'push': {'n': dr['push'], 'pct': round(dr['push'] / t, 4)},
            'loss': {'n': dr['loss'], 'pct': round(dr['loss'] / t, 4)},
            'win_rate': round(dr['win'] / t, 4),
        }

    # 盘口变化幅度细分
    log("  2.3 盘口变化幅度细分...")
    change_magnitude = defaultdict(lambda: {'win': 0, 'push': 0, 'loss': 0, 'total': 0})
    for r in yazhi_records:
        init_h = r['yazhi_init'][0]
        final_h = r['yazhi_final'][0]
        if init_h is None or final_h is None:
            continue
        change = abs(init_h - final_h)
        # 按变化幅度分桶
        if change < 0.01:
            bucket = '不变'
        elif change < 0.26:
            bucket = '微调 (<0.25)'
        elif change < 0.51:
            bucket = '小调 (0.25-0.5)'
        elif change < 1.01:
            bucket = '中调 (0.5-1.0)'
        else:
            bucket = '大调 (>1.0)'
        change_magnitude[bucket]['total'] += 1
        change_magnitude[bucket][r['hhad_result']] += 1

    magnitude_analysis = {}
    for bucket in ['不变', '微调 (<0.25)', '小调 (0.25-0.5)', '中调 (0.5-1.0)', '大调 (>1.0)']:
        cm = change_magnitude[bucket]
        t = cm['total']
        if t == 0:
            continue
        magnitude_analysis[bucket] = {
            'n': t,
            'win_rate': round(cm['win'] / t, 4),
            'push_rate': round(cm['push'] / t, 4),
            'loss_rate': round(cm['loss'] / t, 4),
        }

    # 初盘 vs 终盘 赔率变化
    log("  2.4 初盘 vs 终盘 赔率变化...")
    odds_change = {'home_up': 0, 'home_down': 0, 'away_up': 0, 'away_down': 0}
    for r in yazhi_records:
        init_h_odds = r['yazhi_init'][1]
        final_h_odds = r['yazhi_final'][1]
        init_a_odds = r['yazhi_init'][2]
        final_a_odds = r['yazhi_final'][2]
        if init_h_odds and final_h_odds:
            if final_h_odds > init_h_odds:
                odds_change['home_up'] += 1
            elif final_h_odds < init_h_odds:
                odds_change['home_down'] += 1
        if init_a_odds and final_a_odds:
            if final_a_odds > init_a_odds:
                odds_change['away_up'] += 1
            elif final_a_odds < init_a_odds:
                odds_change['away_down'] += 1

    # 盘口变化 + 赔率变化 联合分析
    log("  2.5 盘口变化 + 主队赔率变化联合分析...")
    combo = defaultdict(lambda: {'win': 0, 'push': 0, 'loss': 0, 'total': 0})
    for r in yazhi_records:
        init_h = r['yazhi_init'][0]
        final_h = r['yazhi_final'][0]
        init_h_odds = r['yazhi_init'][1]
        final_h_odds = r['yazhi_final'][1]
        if None in (init_h, final_h, init_h_odds, final_h_odds):
            continue

        direction = handicap_direction(init_h, final_h)
        if direction is None:
            continue

        # 主队赔率变化方向
        if abs(final_h_odds - init_h_odds) < 0.01:
            odds_dir = 'odds_unchanged'
        elif final_h_odds > init_h_odds:
            odds_dir = 'odds_up'
        else:
            odds_dir = 'odds_down'

        key = f"{direction}_{odds_dir}"
        combo[key]['total'] += 1
        combo[key][r['hhad_result']] += 1

    combo_analysis = {}
    for key, data in sorted(combo.items()):
        t = data['total']
        if t < 10:
            continue
        combo_analysis[key] = {
            'n': t,
            'win_rate': round(data['win'] / t, 4),
            'push_rate': round(data['push'] / t, 4),
            'loss_rate': round(data['loss'] / t, 4),
        }

    # 盘口与赛果关系的汇总
    # 变深时主队赢盘率, 变浅时主队赢盘率
    deepen_win_rate = direction_analysis.get('deepen', {}).get('win_rate', 0)
    shallow_win_rate = direction_analysis.get('shallow', {}).get('win_rate', 0)
    unchanged_win_rate = direction_analysis.get('unchanged', {}).get('win_rate', 0)

    overall_direction = {
        'deepen_win_rate': deepen_win_rate,
        'shallow_win_rate': shallow_win_rate,
        'unchanged_win_rate': unchanged_win_rate,
        'deepen_advantage_pp': round((deepen_win_rate - shallow_win_rate) * 100, 1),
    }

    return {
        'sample': n,
        'direction_counts': direction_counts,
        'direction_analysis': direction_analysis,
        'magnitude_analysis': magnitude_analysis,
        'odds_change': odds_change,
        'combo_analysis': combo_analysis,
        'overall_direction': overall_direction,
    }


# ── 报告打印 ──────────────────────────────────────────────────────

def print_report(hhad, yazhi):
    """打印格式化分析报告"""
    print("")
    print("=" * 70)
    print("  让球盘（HHAD）穿盘/输盘规律 + 亚盘变动分析报告")
    print("=" * 70)

    # ── Part 1: HHAD ──
    if hhad and 'error' not in hhad:
        print(f"\n【Part 1: HHAD 穿盘/输盘规律】")
        oh = hhad.get('overall_hhad', {})
        print(f"  样本总量: {oh.get('sample', 0)} 场")
        print(f"  {'分类':>8} | {'场次':>6} | {'占比':>8}")
        print("  " + "-" * 30)
        for label, key in [('穿盘(赢)', 'win'), ('走水', 'push'), ('输盘', 'loss')]:
            d = oh.get(key, {})
            print(f"  {label:>8} | {d.get('n', 0):>6} | {d.get('pct', 0):>7.1%}")

        # 隐含概率 vs 实际
        iva = hhad.get('implied_vs_actual', {})
        print(f"\n  隐含概率 vs 实际频率:")
        print(f"  {'分类':>10} | {'实际频率':>10} | {'隐含概率':>10} | {'偏差(pp)':>10}")
        print("  " + "-" * 50)
        for label, key in [('穿盘(赢)', 'win_cover'), ('走水', 'push'), ('输盘', 'loss')]:
            d = iva.get(key, {})
            print(f"  {label:>10} | {d.get('actual_freq', 0):>8.1%} | {d.get('avg_implied_prob', 0):>8.1%} | {d.get('bias_pp', 0):>+8.1f}")

        # 按让球数
        bgl = hhad.get('by_goal_line', {})
        print(f"\n  按让球数分层:")
        print(f"  {'让球数':>8} | {'场次':>6} | {'穿盘率':>8} | {'走水率':>8} | {'输盘率':>8}")
        print("  " + "-" * 50)
        for gl_key in sorted(bgl.keys(), key=lambda x: (abs(float(x)), float(x))):
            d = bgl[gl_key]
            print(f"  {d.get('desc', gl_key):>8} | {d['n']:>6} | {d['win_rate']:>7.1%} | {d['push']['pct']:>7.1%} | {d['loss']['pct']:>7.1%}")

        # 按联赛
        bl = hhad.get('by_league', {})
        print(f"\n  按联赛分层 (Top 15):")
        print(f"  {'联赛':>12} | {'场次':>6} | {'穿盘率':>8} | {'走水率':>8} | {'输盘率':>8}")
        print("  " + "-" * 55)
        for league in sorted(bl.keys(), key=lambda lg: -bl[lg]['n'])[:15]:
            d = bl[league]
            print(f"  {league:>12} | {d['n']:>6} | {d['win_rate']:>7.1%} | {d['push_rate']:>7.1%} | {d['loss_rate']:>7.1%}")

        # 按赔率区间
        bor = hhad.get('by_odds_range', {})
        print(f"\n  按 HHAD 主胜赔率区间分层:")
        print(f"  {'赔率区间':>12} | {'场次':>6} | {'平均赔率':>8} | {'穿盘率':>8} | {'隐含概率':>8} | {'偏差(pp)':>8}")
        print("  " + "-" * 60)
        for label in ['1.0-1.5', '1.5-2.0', '2.0-3.0', '3.0+']:
            d = bor.get(label)
            if d is None:
                continue
            print(f"  {label:>12} | {d['n']:>6} | {d['avg_odds_h']:>7.2f} | {d['win_rate']:>7.1%} | "
                  f"{d['avg_implied_win']:>7.1%} | {d['bias_win_pp']:>+7.1f}")

    # ── Part 2: 亚盘 ──
    if yazhi and 'error' not in yazhi:
        print(f"\n【Part 2: 亚盘变动分析】")
        print(f"  样本: {yazhi.get('sample', 0)} 场")

        dc = yazhi.get('direction_counts', {})
        total_dir = sum(dc.values()) or 1
        print(f"\n  盘口变化方向:")
        for d_label, d_key in [('变深', 'deepen'), ('变浅', 'shallow'), ('不变', 'unchanged')]:
            cnt = dc.get(d_key, 0)
            print(f"    {d_label}: {cnt} 场 ({cnt/total_dir:.1%})")

        da = yazhi.get('direction_analysis', {})
        print(f"\n  盘口变化方向 vs 主队赢盘率:")
        print(f"  {'方向':>8} | {'场次':>6} | {'穿盘率':>8} | {'走水率':>8} | {'输盘率':>8}")
        print("  " + "-" * 50)
        for d_label, d_key in [('变深', 'deepen'), ('变浅', 'shallow'), ('不变', 'unchanged')]:
            d = da.get(d_key, {})
            if d:
                print(f"  {d_label:>8} | {d['n']:>6} | {d['win_rate']:>7.1%} | {d['push']['pct']:>7.1%} | {d['loss']['pct']:>7.1%}")

        od = yazhi.get('overall_direction', {})
        print(f"\n  盘口方向总结:")
        print(f"    变深时主队赢盘率: {od.get('deepen_win_rate', 0):.1%}")
        print(f"    变浅时主队赢盘率: {od.get('shallow_win_rate', 0):.1%}")
        print(f"    不变时主队赢盘率: {od.get('unchanged_win_rate', 0):.1%}")
        print(f"    变深-变浅赢盘率差: {od.get('deepen_advantage_pp', 0):+.1f}pp")

        ma = yazhi.get('magnitude_analysis', {})
        print(f"\n  盘口变化幅度 vs 主队赢盘率:")
        print(f"  {'幅度':>16} | {'场次':>6} | {'穿盘率':>8} | {'走水率':>8} | {'输盘率':>8}")
        print("  " + "-" * 58)
        for bucket in ['不变', '微调 (<0.25)', '小调 (0.25-0.5)', '中调 (0.5-1.0)', '大调 (>1.0)']:
            d = ma.get(bucket)
            if d:
                print(f"  {bucket:>16} | {d['n']:>6} | {d['win_rate']:>7.1%} | {d['push_rate']:>7.1%} | {d['loss_rate']:>7.1%}")

        oc = yazhi.get('odds_change', {})
        total_oc = sum(oc.values()) or 1
        print(f"\n  主客赔率变化方向:")
        print(f"    主队赔率上升: {oc.get('home_up', 0)} | 主队赔率下降: {oc.get('home_down', 0)}")
        print(f"    客队赔率上升: {oc.get('away_up', 0)} | 客队赔率下降: {oc.get('away_down', 0)}")

        # 组合分析
        combo = yazhi.get('combo_analysis', {})
        if combo:
            print(f"\n  盘口变化 + 赔率变化 联合分析:")
            print(f"  {'组合':>30} | {'场次':>6} | {'穿盘率':>8}")
            print("  " + "-" * 52)
            for key in sorted(combo.keys(), key=lambda k: -combo[k]['n']):
                d = combo[key]
                label_map = {
                    'deepen_odds_up': '变深+主赔升',
                    'deepen_odds_down': '变深+主赔降',
                    'deepen_odds_unchanged': '变深+主赔不变',
                    'shallow_odds_up': '变浅+主赔升',
                    'shallow_odds_down': '变浅+主赔降',
                    'shallow_odds_unchanged': '变浅+主赔不变',
                    'unchanged_odds_up': '不变+主赔升',
                    'unchanged_odds_down': '不变+主赔降',
                    'unchanged_odds_unchanged': '不变+主赔不变',
                }
                lbl = label_map.get(key, key)
                print(f"  {lbl:>30} | {d['n']:>6} | {d['win_rate']:>7.1%}")

    print("")
    print("=" * 70)


# ── 主函数 ────────────────────────────────────────────────────────

def main():
    print(f"\n{'=' * 60}")
    print(f"  让球盘（HHAD）穿盘/输盘规律 + 亚盘变动分析")
    print(f"  启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 60}")

    conn = get_conn()
    c = conn.cursor()

    # 加载数据
    records = load_data(c)
    conn.close()

    if not records:
        log("ERROR: 无有效数据, 退出")
        return

    # Part 1: HHAD分析
    log("")
    hhad = compute_hhad_analysis(records)

    # Part 2: 亚盘分析
    log("")
    yazhi = compute_yazhi_analysis(records)

    # 汇总结果
    results = {
        'metadata': {
            'version': '1.0',
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'description': '让球盘（HHAD）穿盘/输盘规律 + 亚盘变动分析',
            'total_sample': len(records),
        },
        'hhad_analysis': hhad,
        'yazhi_analysis': yazhi,
    }

    # 打印报告
    print_report(hhad, yazhi)

    # 保存JSON
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log(f"结果已保存: {OUTPUT_PATH}")

    # 关键发现
    print(f"\n{'=' * 60}")
    print("  KEY FINDINGS")
    print(f"{'=' * 60}")

    # HHAD关键发现
    if hhad and 'error' not in hhad:
        oh = hhad.get('overall_hhad', {})
        wr = oh.get('win', {}).get('pct', 0)
        lr = oh.get('loss', {}).get('pct', 0)
        print(f"  HHAD整体: 主队穿盘率 {wr:.1%}, 输盘率 {lr:.1%}")

        iva = hhad.get('implied_vs_actual', {})
        for label, key in [('穿盘', 'win_cover'), ('走水', 'push'), ('输盘', 'loss')]:
            d = iva.get(key, {})
            bp = d.get('bias_pp', 0)
            if abs(bp) > 1:
                print(f"  HHAD {label}偏差: {bp:+.1f}pp ({d.get('actual_freq', 0):.1%} vs {d.get('avg_implied_prob', 0):.1%})")

        # 赔率区间偏差
        bor = hhad.get('by_odds_range', {})
        for label, d in bor.items():
            bp = d.get('bias_win_pp', 0)
            if abs(bp) > 2:
                print(f"  主胜赔率{label}: 穿盘偏差 {bp:+.1f}pp ({d['n']}场)")

    # 亚盘关键发现
    if yazhi and 'error' not in yazhi:
        od = yazhi.get('overall_direction', {})
        dap = od.get('deepen_advantage_pp', 0)
        if abs(dap) > 2:
            print(f"  亚盘: 变深 vs 变浅 赢盘率差 {dap:+.1f}pp")
        print(f"  变深穿盘率: {od.get('deepen_win_rate', 0):.1%}, 变浅穿盘率: {od.get('shallow_win_rate', 0):.1%}")

    print(f"\n{'=' * 60}")
    log("分析完成!")


if __name__ == '__main__':
    main()