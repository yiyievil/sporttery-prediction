#!/usr/bin/env python3
"""
赔率变动方向与幅度的预测价值量化分析 (Ultra 10.1)
===============================================
利用 odds_change_history (92947条) 分析:
1. 赔率变动方向 vs 实际结果的映射
2. 赔率变动幅度 vs 实际结果的映射
3. 按联赛/赔率区间的分层分析
4. 亚指/大小球赔率变动的辅助信号

输出: odds_movement_calibration.json ← 供预测流程使用
"""

import json
import sqlite3
from datetime import datetime

DB_PATH = '/workspace/sporttery/predictions/historical_odds.db'
OUTPUT_PATH = '/workspace/sporttery/predictions/odds_movement_calibration.json'

def log(msg):
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {msg}")

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def compute_movement_analysis():
    """核心分析: 赔率变动特征 vs 实际结果"""
    conn = get_conn()
    c = conn.cursor()
    
    results = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_matches_analyzed': 0,
        'overall': {},
        'by_league': {},
        'by_odds_range': {},
        'by_magnitude': {},
        'by_trend': {},
        'yazhi': {},
        'daxiao': {},
    }
    
    # ===== 1. HAD 赔率变动方向分析 =====
    # 每场比赛: 首次赔率(seq=0) vs 末次赔率(seq=max), 对比实际结果
    log("分析 HAD 赔率变动方向...")
    
    c.execute("""
        SELECT hm.id as match_id, hm.league, hm.result,
               hm.fc_ouzhi_init_w, hm.fc_ouzhi_final_w,
               hm.fc_ouzhi_init_d, hm.fc_ouzhi_final_d,
               hm.fc_ouzhi_init_l, hm.fc_ouzhi_final_l,
               hm.fc_yazhi_init, hm.fc_yazhi_final,
               hm.fc_daxiao_init, hm.fc_daxiao_final,
               hm.home_score, hm.away_score
        FROM historical_matches hm
        WHERE hm.result IN ('H','D','A')
          AND hm.fc_ouzhi_init_w IS NOT NULL AND hm.fc_ouzhi_final_w IS NOT NULL
          AND hm.fc_ouzhi_init_w > 1 AND hm.fc_ouzhi_final_w > 1
    """)
    rows_500 = c.fetchall()
    log(f"  500.com 初终赔匹配: {len(rows_500)} 场")
    
    # ===== 2. odds_change_history 时序分析 (更丰富) =====
    log("分析 odds_change_history 时序数据...")
    
    c.execute("""
        SELECT och.match_db_id, hm.league, hm.result,
               MIN(CASE WHEN och.seq=0 THEN och.h END) as h_first,
               MAX(CASE WHEN och.seq=(SELECT MAX(seq) FROM odds_change_history o2 
                                       WHERE o2.match_db_id=och.match_db_id AND o2.odds_type='had') 
                        THEN och.h END) as h_last,
               MIN(CASE WHEN och.seq=0 THEN och.d END) as d_first,
               MAX(CASE WHEN och.seq=(SELECT MAX(seq) FROM odds_change_history o2 
                                       WHERE o2.match_db_id=och.match_db_id AND o2.odds_type='had') 
                        THEN och.d END) as d_last,
               MIN(CASE WHEN och.seq=0 THEN och.a END) as a_first,
               MAX(CASE WHEN och.seq=(SELECT MAX(seq) FROM odds_change_history o2 
                                       WHERE o2.match_db_id=och.match_db_id AND o2.odds_type='had') 
                        THEN och.a END) as a_last,
               MAX(och.seq) as n_snapshots
        FROM odds_change_history och
        JOIN historical_matches hm ON hm.id = och.match_db_id
        WHERE och.odds_type='had' AND och.h IS NOT NULL
          AND hm.result IN ('H','D','A')
        GROUP BY och.match_db_id
        HAVING h_first IS NOT NULL AND h_last IS NOT NULL
    """)
    och_rows = c.fetchall()
    log(f"  odds_change_history 时序匹配: {len(och_rows)} 场")
    
    # ===== 3. 整体方向分析 (500.com) =====
    down_h = up_h = same_h = 0
    down_h_win = up_h_win = same_h_win = 0
    down_a_win = up_a_win = 0
    
    for row in rows_500:
        change = row['fc_ouzhi_final_w'] - row['fc_ouzhi_init_w']
        result = row['result']
        
        if change < -0.02:  # 主胜赔下降(看好主队)
            down_h += 1
            if result == 'H': down_h_win += 1
            elif result == 'A': down_a_win += 1
        elif change > 0.02:  # 主胜赔上升(不看好主队)
            up_h += 1
            if result == 'H': up_h_win += 1
            elif result == 'A': up_a_win += 1
        else:
            same_h += 1
            if result == 'H': same_h_win += 1
    
    results['overall']['500_had_direction'] = {
        '主胜赔↓(看好)': {
            'n': down_h, 'home_win': round(down_h_win/down_h, 3) if down_h else 0,
            'away_win': round(down_a_win/down_h, 3) if down_h else 0,
        },
        '主胜赔↑(不看好)': {
            'n': up_h, 'home_win': round(up_h_win/up_h, 3) if up_h else 0,
            'away_win': round(up_a_win/up_h, 3) if up_h else 0,
        },
        '持平': {
            'n': same_h, 'home_win': round(same_h_win/same_h, 3) if same_h else 0,
        },
        '信号强度': round((down_h_win/down_h - up_h_win/up_h)*100, 1) if down_h and up_h else 0,
    }
    
    # ===== 4. 整体方向分析 (odds_change_history) =====
    och_down_h = och_up_h = 0
    och_down_h_win = och_up_h_win = 0
    och_down_a_win = och_up_a_win = 0
    
    for row in och_rows:
        h_first = row['h_first']
        h_last = row['h_last']
        result = row['result']
        
        change = h_last - h_first
        
        if change < -0.01:
            och_down_h += 1
            if result == 'H': och_down_h_win += 1
            elif result == 'A': och_down_a_win += 1
        elif change > 0.01:
            och_up_h += 1
            if result == 'H': och_up_h_win += 1
            elif result == 'A': och_up_a_win += 1
    
    results['overall']['och_had_direction'] = {
        '主胜赔↓(看好)': {
            'n': och_down_h, 'home_win': round(och_down_h_win/och_down_h, 3) if och_down_h else 0,
            'away_win': round(och_down_a_win/och_down_h, 3) if och_down_h else 0,
        },
        '主胜赔↑(不看好)': {
            'n': och_up_h, 'home_win': round(och_up_h_win/och_up_h, 3) if och_up_h else 0,
            'away_win': round(och_up_a_win/och_up_h, 3) if och_up_h else 0,
        },
        '信号强度(pp)': round((och_up_h_win/och_up_h - och_down_h_win/och_down_h)*100, 1) if och_down_h and och_up_h else 0,
    }
    
    # ===== 5. 按赔率区间分层 =====
    log("按赔率区间分层分析...")
    odds_ranges = [(1.0, 1.5, '1.0-1.5'), (1.5, 2.0, '1.5-2.0'), 
                   (2.0, 3.0, '2.0-3.0'), (3.0, 5.0, '3.0-5.0'), (5.0, 99, '5.0+')]
    
    for lo, hi, label in odds_ranges:
        bin_down = bin_up = 0
        bin_down_h = bin_up_h = 0
        bin_down_a = bin_up_a = 0
        
        for row in och_rows:
            h_first = row['h_first']
            h_last = row['h_last']
            result = row['result']
            
            if not (lo <= h_first < hi):
                continue
            
            change = h_last - h_first
            if change < -0.01:
                bin_down += 1
                if result == 'H': bin_down_h += 1
                elif result == 'A': bin_down_a += 1
            elif change > 0.01:
                bin_up += 1
                if result == 'H': bin_up_h += 1
                elif result == 'A': bin_up_a += 1
        
        results['by_odds_range'][label] = {
            '赔率降(样本/主胜)': {'n': bin_down, 'home_win': round(bin_down_h/bin_down, 3) if bin_down else 0},
            '赔率升(样本/主胜)': {'n': bin_up, 'home_win': round(bin_up_h/bin_up, 3) if bin_up else 0},
            '信号强度(pp)': round((bin_up_h/bin_up - bin_down_h/bin_down)*100, 1) if bin_down and bin_up else 0,
        }
    
    # ===== 6. 按变动幅度分层 =====
    log("按变动幅度分层分析...")
    magnitude_ranges = [(0.01, 0.05, '微幅(0.01-0.05)'), (0.05, 0.15, '中幅(0.05-0.15)'),
                        (0.15, 0.5, '大幅(0.15-0.5)'), (0.5, 99, '巨幅(0.5+)')]
    
    for lo, hi, label in magnitude_ranges:
        mag_down = mag_up = 0
        mag_down_h = mag_up_h = 0
        mag_down_a = mag_up_a = 0
        
        for row in och_rows:
            h_first = row['h_first']
            h_last = row['h_last']
            result = row['result']
            change = abs(h_last - h_first)
            
            if not (lo <= change < hi):
                continue
            
            if h_last < h_first:  # 下降
                mag_down += 1
                if result == 'H': mag_down_h += 1
                elif result == 'A': mag_down_a += 1
            else:  # 上升
                mag_up += 1
                if result == 'H': mag_up_h += 1
                elif result == 'A': mag_up_a += 1
        
        results['by_magnitude'][label] = {
            '降(样本/主胜)': {'n': mag_down, 'pct': round(mag_down_h/mag_down, 3) if mag_down else 0},
            '升(样本/主胜)': {'n': mag_up, 'pct': round(mag_up_h/mag_up, 3) if mag_up else 0},
            '信号强度(pp)': round((mag_up_h/mag_up - mag_down_h/mag_down)*100, 1) if mag_down and mag_up else 0,
        }
    
    # ===== 7. 按联赛分层 =====
    log("按联赛分层分析...")
    league_stats = {}
    for row in och_rows:
        league = row['league']
        if league not in league_stats:
            league_stats[league] = {'down': 0, 'up': 0, 'down_h': 0, 'up_h': 0, 'down_a': 0, 'up_a': 0}
        
        change = row['h_last'] - row['h_first']
        result = row['result']
        
        if change < -0.01:
            league_stats[league]['down'] += 1
            if result == 'H': league_stats[league]['down_h'] += 1
            elif result == 'A': league_stats[league]['down_a'] += 1
        elif change > 0.01:
            league_stats[league]['up'] += 1
            if result == 'H': league_stats[league]['up_h'] += 1
            elif result == 'A': league_stats[league]['up_a'] += 1
    
    for league, stats in sorted(league_stats.items(), key=lambda x: x[1]['down']+x[1]['up'], reverse=True):
        total = stats['down'] + stats['up']
        if total < 15:
            continue
        down_h = round(stats['down_h']/stats['down'], 3) if stats['down'] else 0
        up_h = round(stats['up_h']/stats['up'], 3) if stats['up'] else 0
        signal = round((up_h - down_h)*100, 1)
        
        results['by_league'][league] = {
            'n': total,
            '降赔主胜': down_h, '升赔主胜': up_h,
            '信号强度': signal,
            '降赔客胜': round(stats['down_a']/stats['down'], 3) if stats['down'] else 0,
            '升赔客胜': round(stats['up_a']/stats['up'], 3) if stats['up'] else 0,
        }
    
    # ===== 8. 亚指变动分析 =====
    log("分析亚指变动...")
    yazhi_down = yazhi_up = 0
    yazhi_down_h = yazhi_up_h = 0
    
    for row in rows_500:
        if row['fc_yazhi_init'] is None or row['fc_yazhi_final'] is None:
            continue
        change = row['fc_yazhi_final'] - row['fc_yazhi_init']
        result = row['result']
        
        if change < -0.1:
            yazhi_down += 1
            if result == 'H': yazhi_down_h += 1
        elif change > 0.1:
            yazhi_up += 1
            if result == 'H': yazhi_up_h += 1
    
    results['yazhi'] = {
        '亚指↓(主队让球减少)': {'n': yazhi_down, 'home_win': round(yazhi_down_h/yazhi_down, 3) if yazhi_down else 0},
        '亚指↑(主队让球增加)': {'n': yazhi_up, 'home_win': round(yazhi_up_h/yazhi_up, 3) if yazhi_up else 0},
    }
    
    # ===== 9. 大小球变动分析 =====
    log("分析大小球变动...")
    dx_down = dx_up = 0
    dx_down_over = dx_up_over = 0
    
    for row in rows_500:
        if row['fc_daxiao_init'] is None or row['fc_daxiao_final'] is None:
            continue
        if row['home_score'] is None or row['away_score'] is None:
            continue
        actual_goals = row['home_score'] + row['away_score']
        change = row['fc_daxiao_final'] - row['fc_daxiao_init']
        
        if change < -0.1:
            dx_down += 1
            if actual_goals > abs(row['fc_daxiao_final']): dx_down_over += 1
        elif change > 0.1:
            dx_up += 1
            if actual_goals > abs(row['fc_daxiao_final']): dx_up_over += 1
    
    results['daxiao'] = {
        '大小↓(盘口降低)': {'n': dx_down, 'over_rate': round(dx_down_over/dx_down, 3) if dx_down else 0},
        '大小↑(盘口升高)': {'n': dx_up, 'over_rate': round(dx_up_over/dx_up, 3) if dx_up else 0},
    }
    
    results['total_matches_analyzed'] = len(och_rows)
    
    # ===== 10. 生成校准因子 =====
    log("生成赔率变动校准因子...")
    
    # 核心因子: 赔率升→主胜率偏差 (相对于市场隐含概率)
    calibration = {
        'version': 'Ultra 10.1',
        'description': '赔率变动方向校准因子 — 用于修正融合概率',
        'method': '赔率升(↑)时主胜率 vs 赔率降(↓)时主胜率的差值, 正值表示"诱盘信号"',
        'overall_bias': results['overall']['och_had_direction']['信号强度(pp)'],
        'by_league': {},
        'by_odds_range': {},
    }
    
    # 按联赛的校准因子
    for league, stats in results['by_league'].items():
        if stats['n'] >= 20:
            calibration['by_league'][league] = {
                'bias_pp': stats['信号强度'],
                '样本量': stats['n'],
                '升赔主胜率': stats['升赔主胜'],
                '降赔主胜率': stats['降赔主胜'],
            }
    
    # 按赔率区间的校准因子
    for rng, stats in results['by_odds_range'].items():
        if stats['赔率升(样本/主胜)']['n'] >= 10 and stats['赔率降(样本/主胜)']['n'] >= 10:
            calibration['by_odds_range'][rng] = {
                'bias_pp': stats['信号强度(pp)'],
                '升赔主胜率': stats['赔率升(样本/主胜)']['home_win'],
                '降赔主胜率': stats['赔率降(样本/主胜)']['home_win'],
            }
    
    results['calibration'] = calibration
    
    # 保存结果
    conn.close()
    return results


def print_report(results):
    """打印分析报告"""
    print("")
    print("=" * 60)
    print("  赔率变动预测价值分析报告")
    print("=" * 60)
    
    print(f"\n📊 分析样本: {results['total_matches_analyzed']} 场比赛")
    
    print("\n【1. 整体方向信号 (500.com初终赔)】")
    for k, v in results['overall']['500_had_direction'].items():
        if isinstance(v, dict):
            print(f"  {k}: {v['n']}场, 主胜率{v['home_win']}, 客胜率{v.get('away_win', '?')}")
        else:
            print(f"  {k}: {v}")
    
    print("\n【2. 整体方向信号 (odds_change_history)】")
    for k, v in results['overall']['och_had_direction'].items():
        if isinstance(v, dict):
            print(f"  {k}: {v['n']}场, 主胜率{v['home_win']}, 客胜率{v.get('away_win', '?')}")
        else:
            print(f"  {k}: {v}")
    
    print("\n【3. 按赔率区间分层】")
    for rng, stats in results['by_odds_range'].items():
        down = stats['赔率降(样本/主胜)']
        up = stats['赔率升(样本/主胜)']
        print(f"  初赔{rng}: 降赔{down['n']}场主胜{down['home_win']:.1%} | "
              f"升赔{up['n']}场主胜{up['home_win']:.1%} | "
              f"信号{stats['信号强度(pp)']:+.1f}pp")
    
    print("\n【4. 按变动幅度分层】")
    for mag, stats in sorted(results['by_magnitude'].items()):
        s = stats
        print(f"  {mag}: 降{s['降(样本/主胜)']['n']}场主胜{s['降(样本/主胜)']['pct']:.1%} | "
              f"升{s['升(样本/主胜)']['n']}场主胜{s['升(样本/主胜)']['pct']:.1%} | "
              f"信号{s['信号强度(pp)']:+.1f}pp")
    
    print("\n【5. 按联赛分层 (Top 15)】")
    for league, stats in sorted(results['by_league'].items(), key=lambda x: x[1]['n'], reverse=True)[:15]:
        print(f"  {league}: {stats['n']}场 | "
              f"降赔主胜{stats['降赔主胜']:.1%} → 升赔主胜{stats['升赔主胜']:.1%} | "
              f"信号{stats['信号强度']:+.1f}pp")
    
    print("\n【6. 亚指变动】")
    for k, v in results['yazhi'].items():
        print(f"  {k}: {v['n']}场, 主胜率{v['home_win']}")
    
    print("\n" + "=" * 60)


def main():
    print("\n🚀 赔率变动预测价值量化分析 (Ultra 10.1)")
    print("=" * 60)
    
    results = compute_movement_analysis()
    print_report(results)
    
    # 保存校准因子
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"✅ 分析结果已保存: {OUTPUT_PATH}")
    print(f"✅ 校准因子已保存 (共{len(results['calibration']['by_league'])}个联赛, "
          f"{len(results['calibration']['by_odds_range'])}个赔率区间)")
    
    # 输出关键发现
    print("\n🔥 关键发现:")
    overall = results['overall']['och_had_direction']
    signal = overall['信号强度(pp)']
    if signal > 5:
        print(f"  1. 赔率上升时主胜率比赔率下降时高出{signal}pp — 强烈诱盘信号!")
    elif signal > 0:
        print(f"  1. 赔率上升时主胜率比赔率下降时高出{signal}pp — 存在诱盘信号")
    else:
        print(f"  1. 赔率上升时主胜率比赔率下降时低{abs(signal)}pp — 正常市场信号")
    
    # 找出信号最强的联赛
    strongest = max(results['by_league'].items(), key=lambda x: abs(x[1]['信号强度']))
    print(f"  2. 信号最强联赛: {strongest[0]} ({strongest[1]['信号强度']:+.1f}pp, {strongest[1]['n']}场)")
    
    # 找出信号最强的赔率区间
    strongest_rng = max(results['by_odds_range'].items(), key=lambda x: abs(x[1]['信号强度(pp)']))
    print(f"  3. 信号最强赔率区间: {strongest_rng[0]} ({strongest_rng[1]['信号强度(pp)']:+.1f}pp)")


if __name__ == '__main__':
    main()