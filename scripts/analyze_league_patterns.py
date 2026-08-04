#!/usr/bin/env python3
"""
联赛进球分布特征 + 主客场优势量化 + 比分分布特征 (分析项#5/#6/#7)
====================================================================
基于历史赛果数据，从三个维度量化联赛特征：
1. 联赛进球分布特征 (场均进球、进球数分布、进球稳定性)
2. 主客场优势量化 (主胜率、进球差、时间趋势)
3. 比分分布特征 (常见比分、半全场模式、逆转率)

数据源: historical_matches (home_score, away_score, half_home_score, half_away_score, league, result)
输出:   league_patterns_analysis.json + 控制台报告
"""

import json
import os
import sqlite3
from datetime import datetime
from collections import defaultdict

DB_PATH = '/workspace/sporttery/predictions/historical_odds.db'
OUTPUT_PATH = '/workspace/sporttery/predictions/league_patterns_analysis.json'


def log(msg):
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {msg}")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_result(result_str, home_score, away_score):
    """统一 result 字段: 返回 H(主胜)/D(平)/A(客胜)"""
    if result_str in ('H', 'W'):
        return 'H'
    if result_str in ('A', 'L'):
        return 'A'
    if result_str == 'D':
        return 'D'
    # 根据比分推断
    if home_score > away_score:
        return 'H'
    elif home_score == away_score:
        return 'D'
    else:
        return 'A'


def load_all_matches(c):
    """加载所有有效比赛数据"""
    c.execute('''SELECT league, home_score, away_score,
                        half_home_score, half_away_score,
                        result, home_team, away_team, match_date
                 FROM historical_matches
                 WHERE home_score IS NOT NULL
                   AND away_score IS NOT NULL''')
    rows = c.fetchall()
    log(f"加载 {len(rows)} 场有效比赛")

    matches = []
    for r in rows:
        d = dict(r)
        d['total_goals'] = d['home_score'] + d['away_score']
        d['half_total'] = (d['half_home_score'] or 0) + (d['half_away_score'] or 0)
        d['norm_result'] = normalize_result(d['result'], d['home_score'], d['away_score'])
        d['year'] = d['match_date'][:4] if d['match_date'] else 'unknown'
        matches.append(d)
    return matches


# ============================================================
# Part 1: 联赛进球分布特征
# ============================================================

def compute_goal_distribution(matches):
    """各联赛进球分布特征"""
    log("分析联赛进球分布特征...")

    # 按联赛分组
    league_data = defaultdict(list)
    for m in matches:
        league_data[m['league']].append(m)

    results = {}
    for league, data in sorted(league_data.items(), key=lambda x: -len(x[1])):
        if len(data) < 15:
            continue
        n = len(data)

        # 场均进球
        home_goals = [m['home_score'] for m in data]
        away_goals = [m['away_score'] for m in data]
        total_goals = [m['total_goals'] for m in data]

        avg_home = sum(home_goals) / n
        avg_away = sum(away_goals) / n
        avg_total = sum(total_goals) / n

        # 方差
        var_home = sum((g - avg_home) ** 2 for g in home_goals) / n
        var_away = sum((g - avg_away) ** 2 for g in away_goals) / n
        var_total = sum((g - avg_total) ** 2 for g in total_goals) / n

        # 标准差
        std_home = var_home ** 0.5
        std_away = var_away ** 0.5
        std_total = var_total ** 0.5

        # 进球数分布 (0-7+)
        goal_dist = {str(i): 0 for i in range(8)}
        for tg in total_goals:
            idx = min(tg, 7)
            goal_dist[str(idx)] += 1
        goal_dist_pct = {k: round(v / n, 4) for k, v in goal_dist.items()}

        # 小球率 (0-2球) / 大球率 (3+球)
        small_rate = sum(goal_dist[str(i)] for i in range(3)) / n
        big_rate = sum(goal_dist[str(i)] for i in range(3, 8)) / n

        # 主客进球平衡性: 主队进球占比
        home_goal_share = sum(home_goals) / (sum(total_goals) + 1e-9)

        results[league] = {
            'n': n,
            'avg_home_goals': round(avg_home, 3),
            'avg_away_goals': round(avg_away, 3),
            'avg_total_goals': round(avg_total, 3),
            'var_total_goals': round(var_total, 3),
            'std_total_goals': round(std_total, 3),
            'std_home_goals': round(std_home, 3),
            'std_away_goals': round(std_away, 3),
            'goal_distribution': goal_dist_pct,
            'small_rate_0_2': round(small_rate, 4),
            'big_rate_3plus': round(big_rate, 4),
            'home_goal_share': round(home_goal_share, 4),
        }

    log(f"  完成 {len(results)} 个联赛的进球分布分析")
    return results


# ============================================================
# Part 2: 主客场优势量化
# ============================================================

def compute_home_away_advantage(matches):
    """主客场优势量化"""
    log("分析主客场优势...")

    league_data = defaultdict(list)
    for m in matches:
        league_data[m['league']].append(m)

    results = {}
    for league, data in sorted(league_data.items(), key=lambda x: -len(x[1])):
        if len(data) < 15:
            continue
        n = len(data)

        # 主胜/平/客胜率
        home_wins = sum(1 for m in data if m['norm_result'] == 'H')
        draws = sum(1 for m in data if m['norm_result'] == 'D')
        away_wins = sum(1 for m in data if m['norm_result'] == 'A')

        home_win_rate = home_wins / n
        draw_rate = draws / n
        away_win_rate = away_wins / n

        # 主场优势因子 = 主胜率 / (1 - 平率 - 主胜率) = 主胜率 / 客胜率
        # 这个比值 > 1 表示主队胜率高于客队
        home_advantage_factor = home_win_rate / (away_win_rate + 1e-9)

        # 主客场进球差
        avg_home_goals = sum(m['home_score'] for m in data) / n
        avg_away_goals = sum(m['away_score'] for m in data) / n
        goal_diff = avg_home_goals - avg_away_goals

        # 主场进球增益 = 主队场均进球 / 客队场均进球 (相对值)
        home_goal_boost = avg_home_goals / (avg_away_goals + 1e-9)

        # 平局率占比特征
        draw_share = draw_rate / (1 - draw_rate + 1e-9)

        results[league] = {
            'n': n,
            'home_win_rate': round(home_win_rate, 4),
            'draw_rate': round(draw_rate, 4),
            'away_win_rate': round(away_win_rate, 4),
            'home_advantage_factor': round(home_advantage_factor, 3),
            'avg_home_goals': round(avg_home_goals, 3),
            'avg_away_goals': round(avg_away_goals, 3),
            'goal_diff_home_away': round(goal_diff, 3),
            'home_goal_boost': round(home_goal_boost, 3),
            'draw_share': round(draw_share, 3),
        }

    # --- 主客场优势随时间变化 ---
    log("  分析主场优势时间趋势...")
    year_data = defaultdict(lambda: {'total': 0, 'home_win': 0, 'draw': 0, 'away_win': 0,
                                      'home_goals': 0, 'away_goals': 0})

    for m in matches:
        yr = m['year']
        year_data[yr]['total'] += 1
        if m['norm_result'] == 'H':
            year_data[yr]['home_win'] += 1
        elif m['norm_result'] == 'D':
            year_data[yr]['draw'] += 1
        else:
            year_data[yr]['away_win'] += 1
        year_data[yr]['home_goals'] += m['home_score']
        year_data[yr]['away_goals'] += m['away_score']

    yearly_trend = {}
    for yr in sorted(year_data.keys()):
        yd = year_data[yr]
        t = yd['total']
        hwr = yd['home_win'] / t
        dr = yd['draw'] / t
        awr = yd['away_win'] / t
        yearly_trend[str(yr)] = {
            'n': t,
            'home_win_rate': round(hwr, 4),
            'draw_rate': round(dr, 4),
            'away_win_rate': round(awr, 4),
            'home_advantage_factor': round(hwr / (awr + 1e-9), 3),
            'avg_home_goals': round(yd['home_goals'] / t, 3),
            'avg_away_goals': round(yd['away_goals'] / t, 3),
        }

    log(f"  完成 {len(results)} 个联赛的主客场优势量化")
    return {
        'by_league': results,
        'yearly_trend': yearly_trend,
    }


# ============================================================
# Part 3: 比分分布特征
# ============================================================

def compute_score_distribution(matches):
    """比分分布特征分析"""
    log("分析比分分布特征...")

    league_data = defaultdict(list)
    for m in matches:
        league_data[m['league']].append(m)

    # 1. 整体比分频率
    overall_score_freq = defaultdict(int)
    for m in matches:
        score_key = f"{m['home_score']}-{m['away_score']}"
        overall_score_freq[score_key] += 1

    n_total = len(matches)
    overall_score_pct = {k: {'n': v, 'pct': round(v / n_total, 4)}
                         for k, v in sorted(overall_score_freq.items(), key=lambda x: -x[1])}

    # 2. 各联赛最常出现的比分
    league_top_scores = {}
    for league, data in sorted(league_data.items(), key=lambda x: -len(x[1])):
        if len(data) < 15:
            continue
        n = len(data)
        score_freq = defaultdict(int)
        for m in data:
            score_key = f"{m['home_score']}-{m['away_score']}"
            score_freq[score_key] += 1
        top = sorted(score_freq.items(), key=lambda x: -x[1])[:10]
        league_top_scores[league] = {
            'n': n,
            'top_scores': [{'score': k, 'n': v, 'pct': round(v / n, 4)} for k, v in top]
        }

    # 3. 半场比分分布 + 半全场模式
    log("  分析半全场模式...")
    half_data = [m for m in matches
                 if m['half_home_score'] is not None and m['half_away_score'] is not None]

    # 半场领先 vs 全场结果
    ht_ft_patterns = defaultdict(lambda: {'ht': '', 'ft': '', 'count': 0})
    half_result_counts = defaultdict(int)
    ft_given_ht = defaultdict(lambda: {'H': 0, 'D': 0, 'A': 0, 'total': 0})

    for m in half_data:
        hh, ha = m['half_home_score'], m['half_away_score']
        if hh > ha:
            ht_res = 'H'  # 半场主队领先
        elif hh == ha:
            ht_res = 'D'  # 半场平
        else:
            ht_res = 'A'  # 半场客队领先

        ft_res = m['norm_result']
        pattern_key = f"{ht_res}{ft_res}"
        half_result_counts[ht_res] += 1
        ft_given_ht[ht_res]['total'] += 1
        ft_given_ht[ht_res][ft_res] += 1

    # 半场领先率 (各联赛)
    league_half_lead = {}
    for league, data in sorted(league_data.items(), key=lambda x: -len(x[1])):
        ld = [m for m in data if m['half_home_score'] is not None and m['half_away_score'] is not None]
        if len(ld) < 15:
            continue
        n = len(ld)
        ht_h = sum(1 for m in ld if m['half_home_score'] > m['half_away_score'])
        ht_d = sum(1 for m in ld if m['half_home_score'] == m['half_away_score'])
        ht_a = sum(1 for m in ld if m['half_home_score'] < m['half_away_score'])
        league_half_lead[league] = {
            'n': n,
            'half_home_lead_rate': round(ht_h / n, 4),
            'half_draw_rate': round(ht_d / n, 4),
            'half_away_lead_rate': round(ht_a / n, 4),
        }

    # 半全场模式分布
    ht_ft_counts = defaultdict(int)
    for m in half_data:
        hh, ha = m['half_home_score'], m['half_away_score']
        ht_res = 'H' if hh > ha else ('D' if hh == ha else 'A')
        ft_res = m['norm_result']
        ht_ft_counts[f"{ht_res}{ft_res}"] += 1

    half_total = len(half_data)
    ht_ft_patterns = {k: {'n': v, 'pct': round(v / half_total, 4)}
                      for k, v in sorted(ht_ft_counts.items(), key=lambda x: -x[1])}

    # 半场领先时的全场结果 (条件概率)
    ht_conditional = {}
    for ht_res in ['H', 'D', 'A']:
        info = ft_given_ht[ht_res]
        if info['total'] == 0:
            continue
        ht_conditional[ht_res] = {
            'total': info['total'],
            'to_H': round(info['H'] / info['total'], 4),
            'to_D': round(info['D'] / info['total'], 4),
            'to_A': round(info['A'] / info['total'], 4),
        }

    # 4. 逆转/追平统计
    log("  分析逆转率...")
    reversal_stats = {}

    # 半场落后但最终不败 (赢或平)
    # 半场落后指: ht_res = A 且主队落后, 或 ht_res = H 且客队落后
    # 这里我们统一: 半场落后方最终不败

    # 主队半场落后 -> 最终不败
    home_trailing = [m for m in half_data
                     if m['half_home_score'] < m['half_away_score']]
    home_comeback = sum(1 for m in home_trailing if m['norm_result'] in ('H', 'D'))
    home_reversal = sum(1 for m in home_trailing if m['norm_result'] == 'H')

    # 客队半场落后 -> 最终不败
    away_trailing = [m for m in half_data
                     if m['half_home_score'] > m['half_away_score']]
    away_comeback = sum(1 for m in away_trailing if m['norm_result'] in ('A', 'D'))
    away_reversal = sum(1 for m in away_trailing if m['norm_result'] == 'A')

    # 整体逆转率
    all_trailing = len(home_trailing) + len(away_trailing)
    all_unbeaten = home_comeback + away_comeback
    all_reversal = home_reversal + away_reversal

    overall_reversal = {
        'total_trailing': all_trailing,
        'unbeaten_rate': round(all_unbeaten / all_trailing, 4) if all_trailing > 0 else 0,
        'full_reversal_rate': round(all_reversal / all_trailing, 4) if all_trailing > 0 else 0,
        'home_trailing': {
            'n': len(home_trailing),
            'unbeaten_rate': round(home_comeback / len(home_trailing), 4) if home_trailing else 0,
            'reversal_rate': round(home_reversal / len(home_trailing), 4) if home_trailing else 0,
        },
        'away_trailing': {
            'n': len(away_trailing),
            'unbeaten_rate': round(away_comeback / len(away_trailing), 4) if away_trailing else 0,
            'reversal_rate': round(away_reversal / len(away_trailing), 4) if away_trailing else 0,
        },
    }

    # 按联赛分层的逆转率
    league_reversal = {}
    for league, data in sorted(league_data.items(), key=lambda x: -len(x[1])):
        ld = [m for m in data if m['half_home_score'] is not None and m['half_away_score'] is not None]
        if len(ld) < 15:
            continue

        # 主队半场落后
        ht = [m for m in ld if m['half_home_score'] < m['half_away_score']]
        ht_unbeaten = sum(1 for m in ht if m['norm_result'] in ('H', 'D'))
        ht_reversal = sum(1 for m in ht if m['norm_result'] == 'H')

        # 客队半场落后
        at = [m for m in ld if m['half_home_score'] > m['half_away_score']]
        at_unbeaten = sum(1 for m in at if m['norm_result'] in ('A', 'D'))
        at_reversal = sum(1 for m in at if m['norm_result'] == 'A')

        total_t = len(ht) + len(at)
        total_ub = ht_unbeaten + at_unbeaten
        total_rev = ht_reversal + at_reversal

        league_reversal[league] = {
            'n': len(ld),
            'total_trailing': total_t,
            'overall_unbeaten_rate': round(total_ub / total_t, 4) if total_t > 0 else 0,
            'overall_reversal_rate': round(total_rev / total_t, 4) if total_t > 0 else 0,
            'home_trailing': {
                'n': len(ht),
                'unbeaten_rate': round(ht_unbeaten / len(ht), 4) if ht else 0,
                'reversal_rate': round(ht_reversal / len(ht), 4) if ht else 0,
            },
            'away_trailing': {
                'n': len(at),
                'unbeaten_rate': round(at_unbeaten / len(at), 4) if at else 0,
                'reversal_rate': round(at_reversal / len(at), 4) if at else 0,
            },
        }

    log(f"  完成比分分布分析, 整体逆转率: {overall_reversal['full_reversal_rate']:.1%}")
    return {
        'overall_score_freq': dict(list(overall_score_pct.items())[:30]),
        'league_top_scores': league_top_scores,
        'half_time_stats': {
            'overall_half_lead': {
                'home_lead': round(half_result_counts.get('H', 0) / half_total, 4),
                'draw': round(half_result_counts.get('D', 0) / half_total, 4),
                'away_lead': round(half_result_counts.get('A', 0) / half_total, 4),
            },
            'ht_ft_patterns': ht_ft_patterns,
            'ht_conditional': ht_conditional,
            'league_half_lead': league_half_lead,
        },
        'reversal_stats': {
            'overall': overall_reversal,
            'by_league': league_reversal,
        },
    }


# ============================================================
# 打印报告
# ============================================================

def print_report(goal_dist, home_away, score_dist):
    """打印格式化的分析报告"""
    print("")
    print("=" * 70)
    print("  联赛进球分布特征 + 主客场优势量化 + 比分分布特征 分析报告")
    print("=" * 70)

    # ===== Part 1: 进球分布 =====
    print("\n" + "=" * 70)
    print("【Part 1: 联赛进球分布特征】")
    print("=" * 70)

    # 按场均总进球排序
    sorted_by_goals = sorted(goal_dist.items(), key=lambda x: -x[1]['avg_total_goals'])

    print(f"\n  各联赛场均进球排名 (Top 20):")
    print(f"  {'排名':>4} | {'联赛':<10} | {'场次':>6} | {'总进球':>8} | {'主队':>6} | {'客队':>6} | {'标准差':>8} | {'大球率':>8}")
    print(f"  " + "-" * 70)
    for rank, (league, data) in enumerate(sorted_by_goals[:20], 1):
        print(f"  {rank:>4} | {league:<10} | {data['n']:>6} | {data['avg_total_goals']:>8.2f} | "
              f"{data['avg_home_goals']:>6.2f} | {data['avg_away_goals']:>6.2f} | "
              f"{data['std_total_goals']:>8.2f} | {data['big_rate_3plus']:>7.1%}")

    # 大球率排名
    print(f"\n  大球率 (3+球) 排名 Top 10:")
    sorted_big = sorted(goal_dist.items(), key=lambda x: -x[1]['big_rate_3plus'])
    for rank, (league, data) in enumerate(sorted_big[:10], 1):
        print(f"    {rank}. {league:<12} {data['big_rate_3plus']:>6.1%} (场均{data['avg_total_goals']:.2f}球, {data['n']}场)")

    print(f"\n  小球率 (0-2球) 排名 Top 10:")
    sorted_small = sorted(goal_dist.items(), key=lambda x: -x[1]['small_rate_0_2'])
    for rank, (league, data) in enumerate(sorted_small[:10], 1):
        print(f"    {rank}. {league:<12} {data['small_rate_0_2']:>6.1%} (场均{data['avg_total_goals']:.2f}球, {data['n']}场)")

    # 进球稳定性: 标准差最低的最稳定
    sorted_stable = sorted(goal_dist.items(), key=lambda x: x[1]['std_total_goals'])
    print(f"\n  进球稳定性排名 (标准差最低 = 最稳定):")
    for rank, (league, data) in enumerate(sorted_stable[:10], 1):
        print(f"    {rank}. {league:<12} 标准差{data['std_total_goals']:.2f} (场均{data['avg_total_goals']:.2f}球)")

    # ===== Part 2: 主客场优势 =====
    print("\n" + "=" * 70)
    print("【Part 2: 主客场优势量化】")
    print("=" * 70)

    ha_leagues = home_away.get('by_league', {})
    sorted_hfa = sorted(ha_leagues.items(), key=lambda x: -x[1]['home_advantage_factor'])

    print(f"\n  主场优势排名 (Top 15):")
    print(f"  {'排名':>4} | {'联赛':<10} | {'场次':>6} | {'主胜率':>8} | {'平率':>8} | {'客胜率':>8} | {'优势因子':>10} | {'进球差':>8}")
    print(f"  " + "-" * 70)
    for rank, (league, data) in enumerate(sorted_hfa[:15], 1):
        print(f"  {rank:>4} | {league:<10} | {data['n']:>6} | {data['home_win_rate']:>7.1%} | "
              f"{data['draw_rate']:>7.1%} | {data['away_win_rate']:>7.1%} | "
              f"{data['home_advantage_factor']:>8.2f}x | {data['goal_diff_home_away']:>+7.2f}")

    # 主客进球增益排名
    sorted_boost = sorted(ha_leagues.items(), key=lambda x: -x[1]['home_goal_boost'])
    print(f"\n  主队进球增益排名 (Top 10):")
    for rank, (league, data) in enumerate(sorted_boost[:10], 1):
        print(f"    {rank}. {league:<12} 主队{data['avg_home_goals']:.2f} / 客队{data['avg_away_goals']:.2f} = {data['home_goal_boost']:.2f}x")

    # 主场优势随时间变化
    yearly = home_away.get('yearly_trend', {})
    print(f"\n  主场优势随时间变化:")
    print(f"  {'年份':>6} | {'场次':>6} | {'主胜率':>8} | {'平率':>8} | {'客胜率':>8} | {'优势因子':>10} | {'主队进球':>8} | {'客队进球':>8}")
    print(f"  " + "-" * 70)
    for yr in sorted(yearly.keys()):
        yd = yearly[yr]
        print(f"  {yr:>6} | {yd['n']:>6} | {yd['home_win_rate']:>7.1%} | {yd['draw_rate']:>7.1%} | "
              f"{yd['away_win_rate']:>7.1%} | {yd['home_advantage_factor']:>8.2f}x | "
              f"{yd['avg_home_goals']:>7.2f} | {yd['avg_away_goals']:>7.2f}")

    # ===== Part 3: 比分分布 =====
    print("\n" + "=" * 70)
    print("【Part 3: 比分分布特征】")
    print("=" * 70)

    # 整体常见比分
    score_freq = score_dist.get('overall_score_freq', {})
    sorted_scores = sorted(score_freq.items(), key=lambda x: -x[1]['n'])[:15]
    print(f"\n  整体最常出现的比分 (Top 15):")
    print(f"  {'排名':>4} | {'比分':>8} | {'场次':>6} | {'占比':>8}")
    print(f"  " + "-" * 30)
    for rank, (score, info) in enumerate(sorted_scores, 1):
        print(f"  {rank:>4} | {score:>8} | {info['n']:>6} | {info['pct']:>7.1%}")

    # 各联赛最常见比分
    league_top = score_dist.get('league_top_scores', {})
    print(f"\n  各联赛最常见比分 (Top 3):")
    top_leagues = sorted(league_top.items(), key=lambda x: -x[1]['n'])[:12]
    for league, info in top_leagues:
        top3 = info['top_scores'][:3]
        scores_str = ', '.join([f"{s['score']}({s['pct']:.0%})" for s in top3])
        print(f"    {league:<10} ({info['n']}场): {scores_str}")

    # 半全场模式
    ht_stats = score_dist.get('half_time_stats', {})
    print(f"\n  半场领先分布:")
    hl = ht_stats.get('overall_half_lead', {})
    print(f"    主队领先: {hl.get('home_lead',0):.1%}  平局: {hl.get('draw',0):.1%}  客队领先: {hl.get('away_lead',0):.1%}")

    print(f"\n  半全场模式 (Top 10):")
    ht_ft = ht_stats.get('ht_ft_patterns', {})
    sorted_htft = sorted(ht_ft.items(), key=lambda x: -x[1]['n'])[:10]
    label_map = {'HH': '胜胜', 'HD': '胜平', 'HA': '胜负',
                 'DH': '平胜', 'DD': '平平', 'DA': '平负',
                 'AH': '负胜', 'AD': '负平', 'AA': '负负'}
    for pattern, info in sorted_htft:
        label = label_map.get(pattern, pattern)
        print(f"    {label:<8} ({pattern}): {info['n']:>5}场, {info['pct']:.1%}")

    print(f"\n  半场领先 -> 全场结果 (条件概率):")
    ht_cond = ht_stats.get('ht_conditional', {})
    for ht_res, info in ht_cond.items():
        label = {'H': '半场主队领先', 'D': '半场平局', 'A': '半场客队领先'}.get(ht_res, ht_res)
        print(f"    {label}: 主场胜{info['to_H']:.0%} / 平{info['to_D']:.0%} / 客胜{info['to_A']:.0%} ({info['total']}场)")

    # 逆转率
    reversal = score_dist.get('reversal_stats', {}).get('overall', {})
    print(f"\n  逆转/追平统计:")
    print(f"    半场落后总场次: {reversal.get('total_trailing', 0)}")
    print(f"    最终不败率: {reversal.get('unbeaten_rate', 0):.1%}")
    print(f"    完全逆转率: {reversal.get('full_reversal_rate', 0):.1%}")

    ht = reversal.get('home_trailing', {})
    at = reversal.get('away_trailing', {})
    print(f"    主队半场落后->不败: {ht.get('unbeaten_rate',0):.1%} / 逆转: {ht.get('reversal_rate',0):.1%} ({ht.get('n',0)}场)")
    print(f"    客队半场落后->不败: {at.get('unbeaten_rate',0):.1%} / 逆转: {at.get('reversal_rate',0):.1%} ({at.get('n',0)}场)")

    # 按联赛逆转率
    league_rev = score_dist.get('reversal_stats', {}).get('by_league', {})
    sorted_rev = sorted(league_rev.items(), key=lambda x: -x[1].get('overall_reversal_rate', 0))
    print(f"\n  联赛逆转率排名 (Top 10):")
    print(f"  {'排名':>4} | {'联赛':<10} | {'场次':>6} | {'落后场':>6} | {'逆转率':>8} | {'不败率':>8} | {'主逆':>8} | {'客逆':>8}")
    print(f"  " + "-" * 65)
    for rank, (league, data) in enumerate(sorted_rev[:10], 1):
        print(f"  {rank:>4} | {league:<10} | {data['n']:>6} | {data['total_trailing']:>6} | "
              f"{data['overall_reversal_rate']:>7.1%} | {data['overall_unbeaten_rate']:>7.1%} | "
              f"{data['home_trailing']['reversal_rate']:>7.1%} | {data['away_trailing']['reversal_rate']:>7.1%}")

    print("")
    print("=" * 70)
    log("报告打印完成")


# ============================================================
# 主函数
# ============================================================

def main():
    print("\n联赛进球分布特征 + 主客场优势量化 + 比分分布特征 分析")
    print("=" * 70)

    conn = get_conn()
    c = conn.cursor()

    # 加载数据
    matches = load_all_matches(c)
    conn.close()

    if not matches:
        log("错误: 无有效比赛数据!")
        return

    # Part 1: 进球分布特征
    goal_dist = compute_goal_distribution(matches)

    # Part 2: 主客场优势量化
    home_away = compute_home_away_advantage(matches)

    # Part 3: 比分分布特征
    score_dist = compute_score_distribution(matches)

    # 汇总结果
    results = {
        'analysis_meta': {
            'title': '联赛进球分布特征 + 主客场优势量化 + 比分分布特征分析',
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total_matches': len(matches),
            'total_leagues': len(goal_dist),
            'date_range': {
                'start': min(m['match_date'] for m in matches if m['match_date']),
                'end': max(m['match_date'] for m in matches if m['match_date']),
            },
        },
        'part1_goal_distribution': goal_dist,
        'part2_home_away_advantage': home_away,
        'part3_score_distribution': score_dist,
    }

    # 保存JSON
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log(f"分析结果已保存: {OUTPUT_PATH}")

    # 打印报告
    print_report(goal_dist, home_away, score_dist)

    # 关键发现总结
    print("\n" + "=" * 70)
    print("  [关键发现]")
    print("=" * 70)

    # 最高/最低进球联赛
    sorted_by_goals = sorted(goal_dist.items(), key=lambda x: -x[1]['avg_total_goals'])
    print(f"  最高进球联赛: {sorted_by_goals[0][0]} ({sorted_by_goals[0][1]['avg_total_goals']:.2f}球/场)")
    print(f"  最低进球联赛: {sorted_by_goals[-1][0]} ({sorted_by_goals[-1][1]['avg_total_goals']:.2f}球/场)")

    # 最强/弱主场优势
    ha_leagues = home_away.get('by_league', {})
    sorted_hfa = sorted(ha_leagues.items(), key=lambda x: -x[1]['home_advantage_factor'])
    print(f"  最强主场优势: {sorted_hfa[0][0]} (因子{sorted_hfa[0][1]['home_advantage_factor']:.2f}x, 主胜{sorted_hfa[0][1]['home_win_rate']:.0%})")
    weakest_hfa = sorted_hfa[-1]
    print(f"  最弱主场优势: {weakest_hfa[0]} (因子{weakest_hfa[1]['home_advantage_factor']:.2f}x, 主胜{weakest_hfa[1]['home_win_rate']:.0%})")

    # 最常见比分
    score_freq = score_dist.get('overall_score_freq', {})
    sorted_scores = sorted(score_freq.items(), key=lambda x: -x[1]['n'])
    top_score = sorted_scores[0] if sorted_scores else ('N/A', {})
    print(f"  最常见比分: {top_score[0]} ({top_score[1].get('pct', 0):.1%})")

    # 逆转率
    reversal = score_dist.get('reversal_stats', {}).get('overall', {})
    print(f"  半场落后逆转率: {reversal.get('full_reversal_rate', 0):.1%}")
    print(f"  半场落后不败率: {reversal.get('unbeaten_rate', 0):.1%}")

    # 半场领先 -> 胜率
    ht_cond = score_dist.get('half_time_stats', {}).get('ht_conditional', {})
    if 'H' in ht_cond:
        print(f"  半场主队领先 -> 最终胜率: {ht_cond['H']['to_H']:.0%}")


if __name__ == '__main__':
    main()