#!/usr/bin/env python3
"""验证美职联校准参数并输出分析结果"""
import sqlite3, json

DB_PATH = '/workspace/sporttery/predictions/historical_odds.db'
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

print("=" * 60)
print("  美职联校准分析")
print("=" * 60)

# 1. 联赛基本参数
c.execute('''SELECT 
    COUNT(*) as n,
    ROUND(SUM(CASE WHEN result='H' THEN 1.0 ELSE 0 END)/COUNT(*), 4) as h_rate,
    ROUND(SUM(CASE WHEN result='D' THEN 1.0 ELSE 0 END)/COUNT(*), 4) as d_rate,
    ROUND(SUM(CASE WHEN result='A' THEN 1.0 ELSE 0 END)/COUNT(*), 4) as a_rate,
    ROUND(AVG(home_score + away_score), 4) as avg_goals,
    ROUND(AVG(home_score), 4) as avg_home_goals,
    ROUND(AVG(away_score), 4) as avg_away_goals,
    ROUND(AVG(home_score - away_score), 4) as avg_gd
FROM historical_matches 
WHERE league='美职联' AND home_score IS NOT NULL AND result != '' ''')
row = c.fetchone()
if row:
    avg_hg = row[5] if row[5] and row[5] > 0 else 1.3
    avg_ag = row[6] if row[6] and row[6] > 0 else 1.3
    home_adv = round((avg_hg / avg_ag) ** 0.5, 3) if avg_ag > 0 else 1.15
    home_adv = max(1.05, min(1.35, home_adv))
    
    print(f"\n  样本数: {row[0]}")
    print(f"  主胜率: {row[1]:.1%} | 平局率: {row[2]:.1%} | 客胜率: {row[3]:.1%}")
    print(f"  均进球: {row[4]:.2f} | 主队均进球: {avg_hg:.2f} | 客队均进球: {avg_ag:.2f}")
    print(f"  主场优势: {home_adv:.3f}")
    print(f"  均净胜球: {row[7]:.2f}")

# 2. 赔率区间标定
print(f"\n  【主赔区间标定】")
bins = [(1.0, 1.5, '1.0-1.5'), (1.5, 2.0, '1.5-2.0'),
        (2.0, 2.5, '2.0-2.5'), (2.5, 3.5, '2.5-3.5'), (3.5, 99, '3.5+')]

c.execute('''SELECT sp_had_h, sp_had_d, sp_had_a, result 
             FROM historical_matches 
             WHERE league='美职联' AND sp_had_h IS NOT NULL AND sp_had_h > 1.0 AND result != '' ''')
mls_odds = c.fetchall()

for lo, hi, label in bins:
    bin_data = [(h,d,a,r) for h,d,a,r in mls_odds if lo <= h < hi]
    if len(bin_data) < 5:
        print(f"    {label}: 样本不足 ({len(bin_data)}场)")
        continue
    implied_h = sum(1/h / (1/h + 1/d + 1/a) for h,d,a,r in bin_data) / len(bin_data)
    actual_h = sum(1 for h,d,a,r in bin_data if r == 'H') / len(bin_data)
    actual_d = sum(1 for h,d,a,r in bin_data if r == 'D') / len(bin_data)
    bias = actual_h - implied_h
    print(f"    {label}: n={len(bin_data):3d} 隐含={implied_h:.1%} 实际主胜={actual_h:.1%} 偏差={bias:+.1%} 平局率={actual_d:.1%}")

# 3. 平赔区间平局率
print(f"\n  【平赔区间平局率】")
d_bins = [(2.5, 3.0, '2.5-3.0'), (3.0, 3.3, '3.0-3.3'),
          (3.3, 3.5, '3.3-3.5'), (3.5, 4.0, '3.5-4.0'), (4.0, 99, '4.0+')]
for lo, hi, label in d_bins:
    bin_data = [(h,d,a,r) for h,d,a,r in mls_odds if lo <= d < hi]
    if len(bin_data) < 5:
        print(f"    {label}: 样本不足 ({len(bin_data)}场)")
        continue
    actual_d = sum(1 for h,d,a,r in bin_data if r == 'D') / len(bin_data)
    implied_d = sum(1/d / (1/h + 1/d + 1/a) for h,d,a,r in bin_data) / len(bin_data)
    print(f"    {label}: n={len(bin_data):3d} 隐含={implied_d:.1%} 实际={actual_d:.1%} 偏差={actual_d-implied_d:+.1%}")

# 4. 让球盘口让平率
print(f"\n  【让球盘口让平率】")
hcap_configs = [('-1', 1, 15), ('-2', 2, 10), ('+1', -1, 15), ('+2', -2, 5)]
for gl_str, draw_diff, min_sample in hcap_configs:
    c.execute('''SELECT sp_had_h, sp_had_d, sp_had_a, home_score, away_score
                 FROM historical_matches
                 WHERE league='美职联' AND sp_goal_line = ?
                   AND home_score IS NOT NULL AND away_score IS NOT NULL
                   AND sp_had_h IS NOT NULL AND sp_had_h > 1.0''', (gl_str,))
    rows = c.fetchall()
    if not rows:
        print(f"    让{gl_str}球: 无数据")
        continue
    d_count = sum(1 for r in rows if r[3] - r[4] == draw_diff)
    d_rate = d_count / len(rows) if rows else 0
    print(f"    让{gl_str}球: n={len(rows)} 让平={d_count} 让平率={d_rate:.1%}")

# 5. 500.com初赔/终赔对比
print(f"\n  【500.com初赔→终赔变动分析】")
c.execute('''SELECT COUNT(*) FROM historical_matches 
             WHERE league='美职联' AND fc_ouzhi_init_w IS NOT NULL''')
has_odds = c.fetchone()[0]
if has_odds > 0:
    c.execute('''SELECT 
        ROUND(AVG(fc_ouzhi_final_w - fc_ouzhi_init_w), 3) as avg_w_change,
        ROUND(AVG(fc_ouzhi_final_d - fc_ouzhi_init_d), 3) as avg_d_change,
        ROUND(AVG(fc_ouzhi_final_l - fc_ouzhi_init_l), 3) as avg_l_change
        FROM historical_matches 
        WHERE league='美职联' AND fc_ouzhi_init_w IS NOT NULL''')
    row = c.fetchone()
    print(f"    场次: {has_odds}")
    print(f"    胜赔变动: {row[0]:+.3f}")
    print(f"    平赔变动: {row[1]:+.3f}")
    print(f"    负赔变动: {row[2]:+.3f}")
else:
    print(f"    无500.com初赔/终赔数据")

# 6. 与其他联赛对比
print(f"\n  【与其他联赛对比】")
c.execute('''SELECT league, COUNT(*) as n,
    ROUND(SUM(CASE WHEN result='H' THEN 1.0 ELSE 0 END)/COUNT(*), 3) as h_rate,
    ROUND(SUM(CASE WHEN result='D' THEN 1.0 ELSE 0 END)/COUNT(*), 3) as d_rate,
    ROUND(AVG(home_score + away_score), 2) as avg_goals
    FROM historical_matches 
    WHERE home_score IS NOT NULL AND result != ''
    GROUP BY league HAVING COUNT(*) >= 10
    ORDER BY league''')
print(f"    {'联赛':<8} {'场次':<6} {'主胜率':<8} {'平局率':<8} {'均进球':<8}")
for row in c.fetchall():
    marker = " ◀◀◀" if row[0] == '美职联' else ""
    print(f"    {row[0]:<8} {row[1]:<6} {row[2]:<8.1%} {row[3]:<8.1%} {row[4]:<8.2f}{marker}")

# 7. 更新 league_calibration.json
print(f"\n  【更新 league_calibration.json】")
# 读取现有的校准文件
import os
cal_path = '/workspace/sporttery/predictions/league_calibration.json'
if os.path.exists(cal_path):
    with open(cal_path, 'r', encoding='utf-8') as f:
        cal_data = json.load(f)
    print(f"  现有联赛: {list(cal_data.get('leagues', {}).keys())}")
    
    # 添加美职联参数
    if row:  # 使用最后一次查询的美职联数据
        c.execute('''SELECT 
            COUNT(*) as n,
            ROUND(SUM(CASE WHEN result='H' THEN 1.0 ELSE 0 END)/COUNT(*), 4) as h_rate,
            ROUND(SUM(CASE WHEN result='D' THEN 1.0 ELSE 0 END)/COUNT(*), 4) as d_rate,
            ROUND(SUM(CASE WHEN result='A' THEN 1.0 ELSE 0 END)/COUNT(*), 4) as a_rate,
            ROUND(AVG(home_score + away_score), 4) as avg_goals,
            ROUND(AVG(home_score), 4) as avg_home_goals,
            ROUND(AVG(away_score), 4) as avg_away_goals,
            ROUND(AVG(home_score - away_score), 4) as avg_gd
            FROM historical_matches 
            WHERE league='美职联' AND home_score IS NOT NULL AND result != '' ''')
        r = c.fetchone()
        if r and r[0] >= 10:
            avg_hg = r[5] if r[5] and r[5] > 0 else 1.3
            avg_ag = r[6] if r[6] and r[6] > 0 else 1.3
            ha = round((avg_hg / avg_ag) ** 0.5, 3) if avg_ag > 0 else 1.15
            ha = max(1.05, min(1.35, ha))
            
            cal_data.setdefault('leagues', {})['美职联'] = {
                'h_rate': r[1], 'd_rate': r[2], 'a_rate': r[3],
                'avg_goals': r[4],
                'avg_home_goals': avg_hg, 'avg_away_goals': avg_ag,
                'home_adv': ha,
                'avg_gd': r[7],
                'sample_size': r[0],
            }
            with open(cal_path, 'w', encoding='utf-8') as f:
                json.dump(cal_data, f, ensure_ascii=False, indent=2)
            print(f"  ✅ 美职联参数已写入 league_calibration.json")
            print(f"     主胜率={r[1]:.1%} 平局率={r[2]:.1%} 客胜率={r[3]:.1%}")
            print(f"     均进球={r[4]:.2f} 主场优势={ha:.3f}")
else:
    print(f"  league_calibration.json 不存在")

conn.close()
print(f"\n{'=' * 60}")
print(f"  校准分析完成!")
print(f"{'=' * 60}")
