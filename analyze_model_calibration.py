#!/usr/bin/env python3
"""
模型校准偏差分析 (闭环验证) — Ultra 10.3
========================================
对比模型预测概率 vs 实际赛果，验证校准质量：
1. 概率校准曲线 (Predicted vs Actual)
2. 置信度校准 (confidence stars vs accuracy)
3. 方向/联赛/赔率区间分层偏差
4. 生成模型偏差修正因子

数据源: historical_matches (有赔率+赛果的历史数据)
方法: 使用 Shin method + 各层校准计算模型概率，与实际结果对比
"""

import json
import os
import sqlite3
import math
from datetime import datetime
from collections import defaultdict

DB_PATH = '/workspace/sporttery/predictions/historical_odds.db'
OUTPUT_PATH = '/workspace/sporttery/predictions/model_calibration.json'
CALIB_PATH = '/workspace/sporttery/predictions/advanced_calibration.json'
ODDS_MOVEMENT_PATH = '/workspace/sporttery/predictions/odds_movement_calibration.json'
POOLS_PATH = '/workspace/sporttery/predictions/sporttery_pools_calibration.json'


def log(msg):
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {msg}")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def shin_method(odds):
    """Shin's method: 从赔率提取隐含概率, 修正favorite-longshot bias"""
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


def load_calibration():
    """加载各层校准因子"""
    cal = {'league': {}, 'global_odds': {}, 'odds_change': {}, 'pools': {}}
    
    # 从 advanced_calibration.json 加载联赛标定 + 全局赔率偏差
    if os.path.exists(CALIB_PATH):
        with open(CALIB_PATH) as f:
            ac = json.load(f)
        cal['league'] = ac.get('leagues', {})
        cal['odds_calibration'] = ac.get('odds_calibration', {})
        cal['global_odds_calibration'] = ac.get('global_odds_calibration', {})
        cal['odds_change_signal'] = ac.get('odds_change_signal', {})
        log(f"  加载 advanced_calibration: {len(cal['league'])}个联赛")
    
    # 从 odds_movement_calibration.json 加载赔率变动校准
    if os.path.exists(ODDS_MOVEMENT_PATH):
        with open(ODDS_MOVEMENT_PATH) as f:
            om = json.load(f)
        cal['odds_movement'] = {
            'by_league': om.get('by_league', {}),
            'by_odds_range': om.get('by_odds_range', {}),
            'overall': om.get('overall', {}),
        }
        log(f"  加载 odds_movement: {len(cal['odds_movement']['by_league'])}个联赛")
    
    # 从 sporttery_pools_calibration.json 加载体彩池校准
    if os.path.exists(POOLS_PATH):
        with open(POOLS_PATH) as f:
            pc = json.load(f)
        cal['pools']['ttg'] = pc.get('ttg', {})
        cal['pools']['hafu'] = pc.get('hafu', {})
        cal['pools']['crs'] = pc.get('crs', {})
        log(f"  加载 sporttery_pools: TTG/HAFU/CRS校准")
    
    return cal


def compute_model_prob(home_odds, draw_odds, away_odds, league, cal):
    """模拟模型概率计算流程 (简化版, 仅包含核心校准层)
    
    返回: [pw, pd, pl] 模型概率
    """
    if home_odds <= 1 or draw_odds <= 1 or away_odds <= 1:
        return None
    
    # Step 1: Shin method
    probs = shin_method([home_odds, draw_odds, away_odds])
    pw, pd, pl = probs[0], probs[1], probs[2]
    
    # Step 2: 联赛标定 (calibrate_shin_probs)
    league_cal = cal.get('odds_calibration', {}).get(league, {})
    if league_cal:
        label = None
        for lo, hi, lbl in [(1.0,1.5,'1.0-1.5'),(1.5,2.0,'1.5-2.0'),
                            (2.0,2.5,'2.0-2.5'),(2.5,3.5,'2.5-3.5'),(3.5,99,'3.5+')]:
            if lo <= home_odds < hi:
                label = lbl
                break
        if label:
            cal_entry = league_cal.get(label)
            if cal_entry and cal_entry.get('sample', 0) >= 5:
                bias = cal_entry.get('bias', 0)
                if abs(bias) >= 0.01:
                    pw_new = pw + bias
                    pd_new = pd - bias * 0.3
                    pl_new = pl - bias * 0.7
                    pw_new = max(0.05, min(0.90, pw_new))
                    pd_new = max(0.05, min(0.60, pd_new))
                    pl_new = max(0.05, min(0.90, pl_new))
                    s = pw_new + pd_new + pl_new
                    pw, pd, pl = pw_new/s, pd_new/s, pl_new/s
    
    # Step 3: 全局赔率区间偏差校准 (calibrate_global_odds_bias)
    global_cal = cal.get('global_odds_calibration', {})
    if global_cal:
        label = None
        for lo, hi, lbl in [(1.0,1.5,'1.0-1.5'),(1.5,2.0,'1.5-2.0'),
                            (2.0,2.5,'2.0-2.5'),(2.5,3.5,'2.5-3.5'),(3.5,99,'3.5+')]:
            if lo <= home_odds < hi:
                label = lbl
                break
        if label:
            cal_entry = global_cal.get(label)
            if cal_entry and cal_entry.get('sample', 0) >= 50:
                bias = cal_entry.get('bias', 0)
                if abs(bias) >= 0.01:
                    bias = max(-0.06, min(0.06, bias))
                    if label == '2.5-3.5':
                        pass  # 跳过此区间
                    else:
                        bias = bias * 0.5
                        pw_new = pw + bias
                        pd_new = pd - bias * 0.3
                        pl_new = pl - bias * 0.7
                        pw_new = max(0.05, min(0.90, pw_new))
                        pd_new = max(0.05, min(0.60, pd_new))
                        pl_new = max(0.05, min(0.90, pl_new))
                        s = pw_new + pd_new + pl_new
                        pw, pd, pl = pw_new/s, pd_new/s, pl_new/s
    
    # 注意: 赔率变动信号校准 (calibrate_odds_change_signal) 需要初赔和终赔
    # 此处仅用终赔模拟, 无法完整模拟该层
    
    return [pw, pd, pl]


def compute_calibration_analysis(c, cal):
    """核心: 模型概率校准分析"""
    log("提取历史数据 (有赔率+赛果的市场)...")
    
    # 使用体彩赔率 (sp_had) 为主 + 500.com欧指为备选
    c.execute('''SELECT hm.league, hm.result, hm.home_score, hm.away_score,
                        hm.sp_had_h, hm.sp_had_d, hm.sp_had_a,
                        hm.fc_ouzhi_final_w, hm.fc_ouzhi_final_d, hm.fc_ouzhi_final_l,
                        hm.fc_ouzhi_init_w, hm.fc_ouzhi_init_d, hm.fc_ouzhi_init_l
        FROM historical_matches hm
        WHERE hm.result IN ('H','D','A')
          AND ( (hm.sp_had_h IS NOT NULL AND hm.sp_had_h > 1
                 AND hm.sp_had_d IS NOT NULL AND hm.sp_had_d > 1
                 AND hm.sp_had_a IS NOT NULL AND hm.sp_had_a > 1)
             OR (hm.fc_ouzhi_final_w IS NOT NULL AND hm.fc_ouzhi_final_w > 1
                 AND hm.fc_ouzhi_final_d IS NOT NULL AND hm.fc_ouzhi_final_d > 1
                 AND hm.fc_ouzhi_final_l IS NOT NULL AND hm.fc_ouzhi_final_l > 1) )
    ''')
    rows = c.fetchall()
    log(f"  提取 {len(rows)} 场有赔率+赛果的历史数据")
    
    # 构建分析数据集
    records = []
    for r in rows:
        league = r['league']
        result = r['result']
        
        # 优先使用体彩赔率, 备选500.com
        h_odds = r['sp_had_h'] or r['fc_ouzhi_final_w']
        d_odds = r['sp_had_d'] or r['fc_ouzhi_final_d']
        a_odds = r['sp_had_a'] or r['fc_ouzhi_final_l']
        
        if not h_odds or not d_odds or not a_odds:
            continue
        
        model_probs = compute_model_prob(h_odds, d_odds, a_odds, league, cal)
        if model_probs is None:
            continue
        
        pw, pd, pl = model_probs
        # 赔率隐含概率 (Shin, 无校准)
        raw_probs = shin_method([h_odds, d_odds, a_odds])
        
        # 判断模型方向
        max_idx = 0 if pw >= pd and pw >= pl else (1 if pd >= pw and pd >= pl else 2)
        model_dir = ['H', 'D', 'A'][max_idx]
        model_conf = max(pw, pd, pl)
        
        records.append({
            'league': league,
            'result': result,
            'model_dir': model_dir,
            'model_conf': model_conf,
            'pw': pw, 'pd': pd, 'pl': pl,
            'raw_pw': raw_probs[0],
            'raw_pd': raw_probs[1],
            'raw_pl': raw_probs[2],
            'h_odds': h_odds,
        })
    
    log(f"  有效记录: {len(records)} 场")
    n = len(records)
    if n == 0:
        return {'error': '无有效数据'}
    
    # ================================================================
    # 1. 概率校准曲线: 按预测概率分桶, 对比实际频率
    # ================================================================
    log("概率校准曲线分析...")
    
    # 按模型置信度 (最高概率) 分桶
    bins = [(0.0, 0.25, '0-25%'), (0.25, 0.35, '25-35%'), (0.35, 0.45, '35-45%'),
            (0.45, 0.55, '45-55%'), (0.55, 0.65, '55-65%'), (0.65, 0.75, '65-75%'),
            (0.75, 0.85, '75-85%'), (0.85, 1.0, '85-100%')]
    
    calibration_curve = {}
    for lo, hi, label in bins:
        bin_records = [r for r in records if lo <= r['model_conf'] < hi]
        if not bin_records:
            continue
        n_bin = len(bin_records)
        n_correct = sum(1 for r in bin_records if r['model_dir'] == r['result'])
        actual_rate = n_correct / n_bin
        avg_predicted = sum(r['model_conf'] for r in bin_records) / n_bin
        calibration_curve[label] = {
            'n': n_bin,
            'avg_predicted_pct': round(avg_predicted * 100, 1),
            'actual_rate_pct': round(actual_rate * 100, 1),
            'bias_pp': round((actual_rate - avg_predicted) * 100, 1),
            'n_correct': n_correct,
        }
    
    # 2. 整体方向命中率
    overall_hit = sum(1 for r in records if r['model_dir'] == r['result']) / n
    overall_raw_hit = 0
    raw_n = 0
    for r in records:
        max_idx = 0 if r['raw_pw'] >= r['raw_pd'] and r['raw_pw'] >= r['raw_pl'] else (1 if r['raw_pd'] >= r['raw_pw'] and r['raw_pd'] >= r['raw_pl'] else 2)
        raw_dir = ['H', 'D', 'A'][max_idx]
        if raw_dir == r['result']:
            overall_raw_hit += 1
        raw_n += 1
    overall_raw_hit_rate = overall_raw_hit / raw_n if raw_n > 0 else 0
    
    # 3. 主胜预测校准
    home_probs = [r for r in records if r['model_dir'] == 'H']
    home_hit = sum(1 for r in home_probs if r['result'] == 'H') / len(home_probs) if home_probs else 0
    home_avg_p = sum(r['pw'] for r in home_probs) / len(home_probs) if home_probs else 0
    
    # 4. 平局预测校准
    draw_probs = [r for r in records if r['model_dir'] == 'D']
    draw_hit = sum(1 for r in draw_probs if r['result'] == 'D') / len(draw_probs) if draw_probs else 0
    draw_avg_p = sum(r['pd'] for r in draw_probs) / len(draw_probs) if draw_probs else 0
    
    # 5. 客胜预测校准
    away_probs = [r for r in records if r['model_dir'] == 'A']
    away_hit = sum(1 for r in away_probs if r['result'] == 'A') / len(away_probs) if away_probs else 0
    away_avg_p = sum(r['pl'] for r in away_probs) / len(away_probs) if away_probs else 0
    
    direction_calibration = {
        '主胜': {
            'n': len(home_probs), 'avg_predicted': round(home_avg_p * 100, 1),
            'actual_rate': round(home_hit * 100, 1), 'bias_pp': round((home_hit - home_avg_p) * 100, 1),
        },
        '平局': {
            'n': len(draw_probs), 'avg_predicted': round(draw_avg_p * 100, 1),
            'actual_rate': round(draw_hit * 100, 1), 'bias_pp': round((draw_hit - draw_avg_p) * 100, 1),
        },
        '客胜': {
            'n': len(away_probs), 'avg_predicted': round(away_avg_p * 100, 1),
            'actual_rate': round(away_hit * 100, 1), 'bias_pp': round((away_hit - away_avg_p) * 100, 1),
        },
    }
    
    # ================================================================
    # 6. 按联赛分层命中率
    # ================================================================
    log("按联赛分层分析...")
    league_data = defaultdict(list)
    for r in records:
        league_data[r['league']].append(r)
    
    league_hit_rates = {}
    for league, data in sorted(league_data.items(), key=lambda x: -len(x[1])):
        if len(data) < 10:
            continue
        n_hit = sum(1 for r in data if r['model_dir'] == r['result'])
        ln = len(data)
        # 按方向统计
        dir_hit = {}
        for dir_name in ['H', 'D', 'A']:
            dir_data = [r for r in data if r['model_dir'] == dir_name]
            if dir_data:
                dir_hit[dir_name] = {
                    'n': len(dir_data),
                    'hit_rate': round(sum(1 for r in dir_data if r['result'] == dir_name) / len(dir_data), 3),
                }
        league_hit_rates[league] = {
            'n': ln,
            'hit_rate': round(n_hit / ln, 3),
            'by_direction': dir_hit,
        }
    
    # ================================================================
    # 7. 按赔率区间分层命中率
    # ================================================================
    log("按赔率区间分析...")
    odds_bins = [(1.0, 1.5, '1.0-1.5'), (1.5, 2.0, '1.5-2.0'),
                 (2.0, 2.5, '2.0-2.5'), (2.5, 3.5, '2.5-3.5'), (3.5, 99, '3.5+')]
    
    odds_hit_rates = {}
    for lo, hi, label in odds_bins:
        bin_data = [r for r in records if lo <= r['h_odds'] < hi]
        if len(bin_data) < 20:
            continue
        n_hit = sum(1 for r in bin_data if r['model_dir'] == r['result'])
        ln = len(bin_data)
        odds_hit_rates[label] = {
            'n': ln,
            'hit_rate': round(n_hit / ln, 3),
            'avg_model_conf': round(sum(r['model_conf'] for r in bin_data) / ln, 3),
        }
    
    # ================================================================
    # 8. 校准偏差摘要 (Brier Score, Log Loss 等)
    # ================================================================
    brier = 0.0
    for r in records:
        # 实际概率: 实际结果=1, 其他=0
        actual = [0, 0, 0]
        if r['result'] == 'H':
            actual[0] = 1
        elif r['result'] == 'D':
            actual[1] = 1
        else:
            actual[2] = 1
        brier += (r['pw'] - actual[0])**2 + (r['pd'] - actual[1])**2 + (r['pl'] - actual[2])**2
    
    brier_score = round(brier / n, 4)
    
    # 基准Brier (Shin, 无校准)
    raw_brier = 0.0
    for r in records:
        actual = [0, 0, 0]
        if r['result'] == 'H':
            actual[0] = 1
        elif r['result'] == 'D':
            actual[1] = 1
        else:
            actual[2] = 1
        raw_brier += (r['raw_pw'] - actual[0])**2 + (r['raw_pd'] - actual[1])**2 + (r['raw_pl'] - actual[2])**2
    raw_brier_score = round(raw_brier / n, 4)
    
    # ================================================================
    # 结果汇总
    # ================================================================
    result = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_matches': n,
        'overall': {
            'model_hit_rate': round(overall_hit * 100, 1),
            'raw_shin_hit_rate': round(overall_raw_hit_rate * 100, 1),
            'hit_rate_improvement_pp': round((overall_hit - overall_raw_hit_rate) * 100, 1),
            'model_brier_score': brier_score,
            'raw_shin_brier_score': raw_brier_score,
            'brier_improvement': round((raw_brier_score - brier_score) / raw_brier_score * 100, 1) if raw_brier_score > 0 else 0,
        },
        'calibration_curve': calibration_curve,
        'direction_calibration': direction_calibration,
        'by_league': league_hit_rates,
        'by_odds_range': odds_hit_rates,
    }
    
    return result


def print_report(results):
    """打印分析报告"""
    print("")
    print("=" * 60)
    print("  模型校准偏差分析报告 (闭环验证)")
    print("=" * 60)
    
    r = results
    print(f"\n📊 验证样本: {r['total_matches']} 场")
    print(f"  Shin命中率: {r['overall']['raw_shin_hit_rate']}%")
    print(f"  模型命中率: {r['overall']['model_hit_rate']}%")
    print(f"  改进: {r['overall']['hit_rate_improvement_pp']:+.1f}pp")
    print(f"  Brier Score: 模型{r['overall']['model_brier_score']} vs Shin{r['overall']['raw_shin_brier_score']} ({r['overall']['brier_improvement']:+.1f}%)")
    
    print(f"\n【1. 概率校准曲线】")
    print(f"{'概率区间':>12} | {'样本':>6} | {'平均预测':>10} | {'实际胜率':>10} | {'偏差(pp)':>10}")
    print("-" * 55)
    for label, data in r['calibration_curve'].items():
        print(f"{label:>12} | {data['n']:>6} | {data['avg_predicted_pct']:>7.1f}% | {data['actual_rate_pct']:>7.1f}% | {data['bias_pp']:>+8.1f}")
    
    print(f"\n【2. 方向校准】")
    print(f"{'方向':>8} | {'样本':>6} | {'平均预测':>10} | {'实际胜率':>10} | {'偏差(pp)':>10}")
    print("-" * 45)
    for dir_name, data in r['direction_calibration'].items():
        print(f"{dir_name:>8} | {data['n']:>6} | {data['avg_predicted']:>7.1f}% | {data['actual_rate']:>7.1f}% | {data['bias_pp']:>+8.1f}")
    
    print(f"\n【3. 按联赛命中率 (Top 15)】")
    for league, data in sorted(r['by_league'].items(), key=lambda x: -x[1]['n'])[:15]:
        hr = data['hit_rate'] * 100
        print(f"  {league}: {data['n']}场, 命中率{hr:.1f}%")
    
    print(f"\n【4. 按赔率区间命中率】")
    for label, data in r['by_odds_range'].items():
        print(f"  初赔{label}: {data['n']}场, 命中率{data['hit_rate']*100:.1f}%, 平均置信度{data['avg_model_conf']*100:.1f}%")
    
    print("")
    print("=" * 60)


def generate_correction_factors(results):
    """生成模型偏差修正因子"""
    cf = {
        'version': 'Ultra 10.3',
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'description': '模型校准偏差修正因子 — 修正模型概率为实际校准概率',
        'overall': {
            'model_hit_rate': results['overall']['model_hit_rate'],
            'brier_score': results['overall']['model_brier_score'],
        },
        'probability_correction': {},
        'direction_correction': {},
        'league_correction': {},
    }
    
    # 概率校准修正: 每个区间的偏差
    for label, data in results['calibration_curve'].items():
        if abs(data['bias_pp']) > 1:
            cf['probability_correction'][label] = {
                'bias_pp': data['bias_pp'],
                'correction_factor': round(1 + data['bias_pp'] / 100 / (data['avg_predicted_pct'] / 100 + 0.001), 3),
            }
    
    # 方向校准修正
    for dir_name, data in results['direction_calibration'].items():
        if abs(data['bias_pp']) > 2:
            cf['direction_correction'][dir_name] = {
                'bias_pp': data['bias_pp'],
            }
    
    # 联赛命中率修正 (低于整体命中率的联赛需要额外关注)
    overall_hr = results['overall']['model_hit_rate'] / 100
    for league, data in results['by_league'].items():
        if data['n'] >= 20:
            hr_diff = data['hit_rate'] - overall_hr
            if abs(hr_diff) > 0.03:  # 偏差>3pp
                cf['league_correction'][league] = {
                    'n': data['n'],
                    'hit_rate': round(data['hit_rate'] * 100, 1),
                    'vs_overall_pp': round(hr_diff * 100, 1),
                }
    
    return cf


def main():
    print("\n🚀 模型校准偏差分析 (闭环验证) — Ultra 10.3")
    print("=" * 60)
    
    conn = get_conn()
    c = conn.cursor()
    
    cal = load_calibration()
    results = compute_calibration_analysis(c, cal)
    
    conn.close()
    
    if 'error' in results:
        print(f"❌ 分析失败: {results['error']}")
        return
    
    print_report(results)
    
    correction = generate_correction_factors(results)
    
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(correction, f, ensure_ascii=False, indent=2)
    print(f"✅ 校准修正因子已保存: {OUTPUT_PATH}")
    
    # 关键发现
    print("\n🔥 关键发现:")
    cc = results['calibration_curve']
    for label, data in cc.items():
        if abs(data['bias_pp']) > 2:
            print(f"  {label}: 偏差{data['bias_pp']:+.1f}pp ({data['n']}场)")
    
    dc = results['direction_calibration']
    for dir_name, data in dc.items():
        if abs(data['bias_pp']) > 2:
            print(f"  {dir_name}方向: 偏差{data['bias_pp']:+.1f}pp ({data['n']}场)")


if __name__ == '__main__':
    main()