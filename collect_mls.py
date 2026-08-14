#!/usr/bin/env python3
"""
美职联 (MLS) 历史数据采集器
- Sporttery API: 获取 MLS 2025+2026 赛季的 HAD/HHAD 赔率 + 赛果
- 500.com: 获取初赔/终赔 (通过 shuju 搜索匹配 fid)
- 入库到 predictions/historical_odds.db

Sporttery leagueId=50, leagueName="美国职业大联盟"
MLS 赛季: 2025 (2月-11月), 2026 (2月-至今)
"""

import os
import sqlite3
import time
from datetime import datetime, timedelta
import requests

# ============================================================
# 配置
# ============================================================
_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE', os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_WORKSPACE, 'predictions', 'historical_odds.db')
MLS_LEAGUE_ID = '50'
MLS_LEAGUE_NAME = '美职联'  # 体彩简称

HEADERS_SP = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://www.lottery.gov.cn/jc/zqsgkj/',
    'Accept': 'application/json',
}
HEADERS_500 = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}

RESULT_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry"

# MLS 赛季日期范围
SEASONS = [
    ('2025', '2025-02-01', '2025-12-31'),
    ('2026', '2026-01-01', '2026-07-28'),
]


def fetch_sporttery_mls_matches():
    """从 Sporttery matchResult API 获取美职联所有比赛"""
    all_matches = []
    
    for season, date_begin, date_end in SEASONS:
        print(f"\n[Sporttery] 获取美职联 {season} 赛季 ({date_begin} ~ {date_end})...")
        
        # 按月查询，避免单次查询数据过多
        current = datetime.strptime(date_begin, '%Y-%m-%d')
        end_dt = datetime.strptime(date_end, '%Y-%m-%d')
        season_matches = []
        
        while current < end_dt:
            month_end = min(current + timedelta(days=30), end_dt)
            cb = current.strftime('%Y-%m-%d')
            ce = month_end.strftime('%Y-%m-%d')
            
            page = 1
            while True:
                params = {
                    'matchBeginDate': cb,
                    'matchEndDate': ce,
                    'leagueId': MLS_LEAGUE_ID,
                    'pageSize': '30',
                    'pageNo': str(page),
                    'isFix': '0',
                    'matchPage': '1',
                    'pcOrWap': '1',
                }
                try:
                    # M10: 分页请求失败重试2次 (time.sleep(2)), 避免单月数据永久缺失
                    r = None
                    for _attempt in range(3):
                        try:
                            r = requests.get(RESULT_URL, headers=HEADERS_SP, params=params, timeout=15)
                            if r.status_code == 200:
                                break
                        except Exception:
                            r = None
                        if _attempt < 2:
                            time.sleep(2)
                    if r is None:
                        print(f"  [WARN] 请求失败(已重试2次), 跳过: {cb}~{ce} page{page}")
                        break
                    data = r.json()
                    val = data.get('value', {})
                    results = val.get('matchResult', [])
                    if not results:
                        break
                    
                    for m in results:
                        # 解析全场比分: sectionsNo999 = "4:0"
                        full_score = m.get('sectionsNo999', '')
                        home_score = away_score = None
                        if ':' in full_score:
                            parts = full_score.split(':')
                            home_score = int(parts[0]) if parts[0].strip().lstrip('-').isdigit() else None
                            away_score = int(parts[1]) if parts[1].strip().lstrip('-').isdigit() else None

                        # 解析半场比分: sectionsNo1 = "2:0"
                        half_score = m.get('sectionsNo1', '')
                        half_h = half_a = None
                        if ':' in half_score:
                            parts = half_score.split(':')
                            half_h = int(parts[0]) if parts[0].strip().lstrip('-').isdigit() else None
                            half_a = int(parts[1]) if parts[1].strip().lstrip('-').isdigit() else None

                        # HAD赔率
                        h_val = m.get('h')
                        d_val = m.get('d')
                        a_val = m.get('a')
                        try:
                            h_val = float(h_val) if h_val else None
                            d_val = float(d_val) if d_val else None
                            a_val = float(a_val) if a_val else None
                        except (ValueError, TypeError):
                            h_val = d_val = a_val = None

                        season_matches.append({
                            'match_date': m.get('matchDate', ''),
                            'league': MLS_LEAGUE_NAME,
                            'season': season,
                            'home_team': m.get('homeTeam', ''),
                            'away_team': m.get('awayTeam', ''),
                            'home_team_full': m.get('allHomeTeam', ''),
                            'away_team_full': m.get('allAwayTeam', ''),
                            'home_score': home_score,
                            'away_score': away_score,
                            'half_home_score': half_h,
                            'half_away_score': half_a,
                            'result': m.get('winFlag', ''),
                            'sp_had_h': h_val,
                            'sp_had_d': d_val,
                            'sp_had_a': a_val,
                            'sp_goal_line': str(m.get('goalLine', '')),
                            'sp_match_num': m.get('matchNumStr', ''),
                            'match_id': m.get('matchId'),
                            'league_id': m.get('leagueId'),
                        })
                    
                    # S5: 兼容 totalPage/pages 两种字段名, 并int()保护
                    total_pages = int(val.get('totalPage') or val.get('pages') or 1)
                    if page >= total_pages:
                        break
                    page += 1
                    time.sleep(0.3)
                except Exception as e:
                    print(f"  Error: {e}")
                    break
            
            current = month_end + timedelta(days=1)
        
        # 去重 (按日期+球队)
        seen = set()
        unique = []
        for m in season_matches:
            key = (m['match_date'], m['home_team'], m['away_team'])
            if key not in seen:
                seen.add(key)
                unique.append(m)
        
        print(f"  {season} 赛季: {len(unique)} 场比赛 (去重前 {len(season_matches)})")
        all_matches.extend(unique)
    
    return all_matches


def insert_into_db(matches):
    """将比赛数据插入 historical_matches 表"""
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()

        inserted = 0
        skipped = 0
        for m in matches:
            try:
                c.execute('''INSERT OR IGNORE INTO historical_matches
                    (match_date, league, season, home_team, away_team,
                     home_score, away_score, half_home_score, half_away_score, result,
                     sp_had_h, sp_had_d, sp_had_a, sp_goal_line, sp_match_num,
                     source, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sporttery', ?)''',
                    (m['match_date'], m['league'], m['season'],
                     m['home_team'], m['away_team'],
                     m['home_score'], m['away_score'],
                     m['half_home_score'], m['half_away_score'], m['result'],
                     m['sp_had_h'], m['sp_had_d'], m['sp_had_a'],
                     m['sp_goal_line'], m['sp_match_num'],
                     datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
                if c.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as e:
                print(f"  INSERT error: {e}")
                skipped += 1

        conn.commit()
    finally:
        conn.close()
    print(f"\n[DB] 插入: {inserted}, 跳过(已存在): {skipped}")
    return inserted


def print_db_stats():
    """打印数据库统计"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    print("\n" + "=" * 60)
    print("  【数据库统计】")
    print("=" * 60)
    
    c.execute('SELECT COUNT(*) FROM historical_matches')
    total = c.fetchone()[0]
    print(f"  总比赛数: {total}")
    
    c.execute('''SELECT league, season, COUNT(*) as n,
                  SUM(CASE WHEN sp_had_h IS NOT NULL THEN 1 ELSE 0 END) as has_had,
                  SUM(CASE WHEN home_score IS NOT NULL THEN 1 ELSE 0 END) as has_score,
                  SUM(CASE WHEN result != '' THEN 1 ELSE 0 END) as has_result
               FROM historical_matches
               GROUP BY league, season ORDER BY league, season''')
    print(f"\n  {'联赛':<8} {'赛季':<10} {'场次':<6} {'有HAD':<6} {'有比分':<6} {'有结果':<6}")
    print(f"  {'-'*50}")
    for row in c.fetchall():
        print(f"  {row[0]:<8} {row[1]:<10} {row[2]:<6} {row[3]:<6} {row[4]:<6} {row[5]:<6}")
    
    # 美职联详细统计
    c.execute('''SELECT season, COUNT(*) as n,
                  ROUND(AVG(home_score), 2) as avg_h,
                  ROUND(AVG(away_score), 2) as avg_a,
                  ROUND(AVG(CASE WHEN result='H' THEN 1.0 ELSE 0 END), 3) as h_rate,
                  ROUND(AVG(CASE WHEN result='D' THEN 1.0 ELSE 0 END), 3) as d_rate,
                  ROUND(AVG(CASE WHEN result='A' THEN 1.0 ELSE 0 END), 3) as a_rate
               FROM historical_matches
               WHERE league='美职联' AND home_score IS NOT NULL
               GROUP BY season''')
    rows = c.fetchall()
    if rows:
        print(f"\n  【美职联详细统计】")
        print(f"  {'赛季':<8} {'场次':<6} {'均主进球':<8} {'均客进球':<8} {'主胜率':<8} {'平局率':<8} {'客胜率':<8}")
        for row in rows:
            print(f"  {row[0]:<8} {row[1]:<6} {row[2]:<8} {row[3]:<8} {row[4]:<8} {row[5]:<8} {row[6]:<8}")
    
    conn.close()


def main():
    t0 = time.time()
    print("=" * 60)
    print("  美职联 (MLS) 历史数据采集器")
    print(f"  Sporttery leagueId={MLS_LEAGUE_ID}")
    print(f"  赛季: 2025 + 2026")
    print("=" * 60)
    
    # Phase 1: 从 Sporttery 获取比赛数据
    print("\n[Phase 1] Sporttery API 采集...")
    matches = fetch_sporttery_mls_matches()
    print(f"\n  总计: {len(matches)} 场美职联比赛")
    
    # Phase 2: 入库
    print("\n[Phase 2] 入库...")
    inserted = insert_into_db(matches)
    
    # Phase 3: 统计
    print_db_stats()
    
    total_time = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"  采集完成! 总耗时: {total_time:.1f}s ({total_time/60:.1f}min)")
    print(f"  新增: {inserted} 场")
    print(f"{'=' * 60}")


if __name__ == '__main__':
    main()
