#!/usr/bin/env python3
"""
体彩赔率变动记录采集器
从 sporttery.cn 的 getFixedBonusV1 API 采集每场比赛的赔率变动时间线

数据包括:
- HAD (胜平负): 每次赔率变动的胜/平/负值 + 时间戳
- HHAD (让球胜平负): 每次让球赔率变动 + 让球数
- CRS (比分): 每次比分赔率变动
- TTG (进球数): 每次进球数赔率变动
- HAFU (半全场): 每次半全场赔率变动

流程:
1. 为 historical_matches 表添加 sporttery_match_id 列
2. 从 matchResult API 回填所有比赛的 matchId
3. 创建 odds_change_history 表
4. 批量抓取赔率变动记录
"""

import os
import sqlite3
import time
import json
from datetime import datetime, timedelta
import requests

# ============================================================
# 配置
# ============================================================
_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE', os.path.dirname(os.path.abspath(__file__)))


def _safe_float(v):
    """健壮浮点转换: 非数字/None/空串返回 None, 避免 float() 抛 ValueError"""
    if v is None:
        return None
    try:
        f = float(v)
        return f
    except (ValueError, TypeError):
        return None
DB_PATH = os.path.join(_WORKSPACE, 'predictions', 'historical_odds.db')
RESULT_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry"
BONUS_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getFixedBonusV1.qry"

HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://www.lottery.gov.cn/jc/zqsgkj/',
    'Accept': 'application/json',
}

# 速率控制
_request_count = 0
_last_reset = time.time()

def rate_limited_get(url, params=None, max_per_sec=3):
    """速率限制的GET请求"""
    global _request_count, _last_reset
    now = time.time()
    if now - _last_reset >= 1.0:
        _request_count = 0
        _last_reset = now
    if _request_count >= max_per_sec:
        sleep_time = 1.0 - (now - _last_reset)
        if sleep_time > 0:
            time.sleep(sleep_time)
        _request_count = 0
        _last_reset = time.time()
    _request_count += 1
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        return r
    except Exception as e:
        print(f"  请求失败: {e}")
        return None


def normalize_team(name):
    """标准化球队名称用于匹配"""
    if not name:
        return ''
    # 去除常见后缀和空格
    name = name.strip()
    for suffix in ['FC', '队', 'CF']:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    return name.strip()


def teams_match(name1, name2):
    """模糊匹配球队名称"""
    if not name1 or not name2:
        return False
    n1 = normalize_team(name1)
    n2 = normalize_team(name2)
    if n1 == n2:
        return True
    # 包含关系
    if len(n1) >= 2 and n1 in n2:
        return True
    if len(n2) >= 2 and n2 in n1:
        return True
    # 前两个字匹配
    if len(n1) >= 2 and len(n2) >= 2 and n1[:2] == n2[:2]:
        return True
    return False


# ============================================================
# Step 1: 添加 sporttery_match_id 列
# ============================================================
def add_match_id_column():
    """为 historical_matches 表添加 sporttery_match_id 列"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 检查列是否已存在
    c.execute("PRAGMA table_info(historical_matches)")
    columns = [col[1] for col in c.fetchall()]
    if 'sporttery_match_id' not in columns:
        c.execute("ALTER TABLE historical_matches ADD COLUMN sporttery_match_id INTEGER")
        conn.commit()
        print("  [OK] 添加 sporttery_match_id 列")
    else:
        print("  [SKIP] sporttery_match_id 列已存在")
    
    conn.close()


# ============================================================
# Step 2: 从 matchResult API 回填 matchId
# ============================================================
# 数据库联赛名 → 体彩leagueId 映射
LEAGUE_ID_MAP = {
    '美职联': '50',
    '挪超': '51',
    '瑞超': '58',
    '芬超': '2064839',
    '英超': '25',
    '欧冠': '69',
    '欧罗巴': '70',
    '欧协联': '1033103',
    '巴甲': '6',
}


def backfill_match_ids():
    """从 matchResult API 获取所有比赛的 matchId 并回填到数据库
    按联赛ID + 月度查询，避免API时间范围限制"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 获取还没有 matchId 的比赛，按联赛分组
    c.execute("""SELECT id, match_date, home_team, away_team, league 
                 FROM historical_matches 
                 WHERE sporttery_match_id IS NULL 
                 ORDER BY match_date""")
    pending = c.fetchall()
    print(f"  待回填比赛数: {len(pending)}")
    
    if not pending:
        conn.close()
        return 0
    
    # 按联赛分组
    pending_by_league = {}
    for row in pending:
        lg = row[4]
        if lg not in pending_by_league:
            pending_by_league[lg] = []
        pending_by_league[lg].append(row)
    
    print(f"  涉及联赛: {list(pending_by_league.keys())}")
    
    all_api_matches = []  # (matchId, matchDate, homeTeam, awayTeam, leagueAbbr)
    
    # 按联赛 + 月度查询
    for league_name, league_matches in pending_by_league.items():
        sporttery_lid = LEAGUE_ID_MAP.get(league_name)
        if not sporttery_lid:
            print(f"  [SKIP] {league_name}: 无leagueId映射, 跳过 {len(league_matches)} 场")
            continue
        
        # 获取该联赛的日期范围
        dates = [r[1][:10] for r in league_matches if r[1]]
        if not dates:
            continue
        date_begin = min(dates)
        date_end = max(dates)
        
        league_api_matches = []
        current = datetime.strptime(date_begin, '%Y-%m-%d')
        end_dt = datetime.strptime(date_end, '%Y-%m-%d')
        
        while current < end_dt:
            month_end = min(current + timedelta(days=30), end_dt)
            cb = current.strftime('%Y-%m-%d')
            ce = month_end.strftime('%Y-%m-%d')
            
            page = 1
            while True:
                params = {
                    'matchBeginDate': cb,
                    'matchEndDate': ce,
                    'leagueId': sporttery_lid,
                    'pageSize': '100',
                    'pageNo': str(page),
                    'isFix': '0',
                    'matchPage': '1',
                    'pcOrWap': '1',
                }
                r = rate_limited_get(RESULT_URL, params=params)
                if not r:
                    break
                try:
                    data = r.json()
                except:
                    break
                
                val = data.get('value')
                if not val:
                    break
                results = val.get('matchResult', [])
                if not results:
                    break
                
                for m in results:
                    league_api_matches.append((
                        m.get('matchId'),
                        m.get('matchDate', ''),
                        m.get('homeTeam', ''),
                        m.get('awayTeam', ''),
                        m.get('leagueNameAbbr', ''),
                    ))
                
                # S5: totalPage为主候选, 兼容pages字段回退, 并int()保护
                total_pages = int(val.get('totalPage') or val.get('pages') or 1)
                if page >= total_pages:
                    break
                page += 1
            
            current = month_end + timedelta(days=1)
        
        all_api_matches.extend(league_api_matches)
        print(f"  [{league_name}] API返回 {len(league_api_matches)} 场 (待匹配 {len(league_matches)} 场)")
    
    print(f"  API总返回比赛数: {len(all_api_matches)}")
    
    # 匹配并回填
    updated = 0
    for db_id, db_date, db_home, db_away, db_league in pending:
        db_date_short = db_date[:10] if db_date else ''
        best_match = None
        
        for api_mid, api_date, api_home, api_away, api_league in all_api_matches:
            if api_date != db_date_short:
                continue
            # 修复: 加入联赛匹配, 避免队名相似的不同联赛比赛(如"国际/纽约城")被错回填 matchId
            if db_league and api_league and db_league != api_league:
                continue
            if teams_match(db_home, api_home) and teams_match(db_away, api_away):
                best_match = api_mid
                break
        
        if best_match:
            c.execute("UPDATE historical_matches SET sporttery_match_id=? WHERE id=?",
                      (best_match, db_id))
            updated += 1
    
    conn.commit()
    print(f"  [OK] 回填 matchId: {updated}/{len(pending)}")
    
    # 统计回填率
    c.execute("SELECT COUNT(*) FROM historical_matches WHERE sporttery_match_id IS NOT NULL")
    total_with_id = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM historical_matches")
    total = c.fetchone()[0]
    # M10: total==0 时避免除零
    pct_str = f"{total_with_id*100//total}%" if total else "0%"
    print(f"  数据库总比赛: {total}, 有matchId: {total_with_id} ({pct_str})")
    
    # 按联赛统计
    c.execute('''SELECT league, 
        COUNT(*) as total,
        SUM(CASE WHEN sporttery_match_id IS NOT NULL THEN 1 ELSE 0 END) as matched
        FROM historical_matches GROUP BY league ORDER BY total DESC''')
    print(f"  各联赛回填情况:")
    for r in c.fetchall():
        pct = r[2]*100//r[1] if r[1] else 0
        print(f"    {r[0]}: {r[2]}/{r[1]} ({pct}%)")
    
    conn.close()
    return updated


# ============================================================
# Step 3: 创建 odds_change_history 表
# ============================================================
def create_odds_history_table():
    """创建赔率变动记录表"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS odds_change_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_db_id INTEGER,
        sporttery_match_id INTEGER,
        match_date TEXT,
        league TEXT,
        home_team TEXT,
        away_team TEXT,
        odds_type TEXT,
        update_time TEXT,
        seq INTEGER,
        h REAL,
        d REAL,
        a REAL,
        goal_line TEXT,
        crs_data TEXT,
        ttg_data TEXT,
        hafu_data TEXT,
        created_at TEXT,
        UNIQUE(sporttery_match_id, odds_type, update_time)
    )''')
    
    c.execute('''CREATE INDEX IF NOT EXISTS idx_och_match_id 
                 ON odds_change_history(sporttery_match_id)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_och_type 
                 ON odds_change_history(odds_type)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_och_match_db 
                 ON odds_change_history(match_db_id)''')
    
    conn.commit()
    conn.close()
    print("  [OK] 创建 odds_change_history 表")


# ============================================================
# Step 4: 抓取赔率变动记录
# ============================================================
def fetch_odds_history(match_id):
    """从 getFixedBonusV1 API 获取单场比赛的赔率变动记录"""
    r = rate_limited_get(BONUS_URL, params={
        'clientCode': '3001',
        'matchId': str(match_id),
    })
    if not r:
        return None
    try:
        data = r.json()
    except:
        return None
    
    if data.get('errorCode') != '0':
        return None
    
    val = data.get('value', {})
    oh = val.get('oddsHistory', {})
    
    if not oh:
        return None
    
    result = {
        'match_id': match_id,
        'home_team': oh.get('homeTeamAbbName', ''),
        'away_team': oh.get('awayTeamAbbName', ''),
        'league_name': oh.get('leagueAbbName', ''),
        'had': [],
        'hhad': [],
        'crs': [],
        'ttg': [],
        'hafu': [],
    }
    
    # HAD (胜平负)
    for item in oh.get('hadList', []):
        result['had'].append({
            'time': f"{item.get('updateDate', '')} {item.get('updateTime', '')}".strip(),
            'h': _safe_float(item.get('h')),
            'd': _safe_float(item.get('d')),
            'a': _safe_float(item.get('a')),
        })
    
    # HHAD (让球胜平负)
    for item in oh.get('hhadList', []):
        result['hhad'].append({
            'time': f"{item.get('updateDate', '')} {item.get('updateTime', '')}".strip(),
            'h': _safe_float(item.get('h')),
            'd': _safe_float(item.get('d')),
            'a': _safe_float(item.get('a')),
            'goal_line': str(item.get('goalLine', '')),
        })
    
    # CRS (比分) - 存为JSON
    for item in oh.get('crsList', []):
        time_str = f"{item.get('updateDate', '')} {item.get('updateTime', '')}".strip()
        crs_data = {k: v for k, v in item.items() 
                    if k not in ('updateDate', 'updateTime') and v and v != '0'}
        result['crs'].append({
            'time': time_str,
            'crs_data': json.dumps(crs_data, ensure_ascii=False),
        })
    
    # TTG (进球数)
    for item in oh.get('ttgList', []):
        time_str = f"{item.get('updateDate', '')} {item.get('updateTime', '')}".strip()
        ttg_data = {}
        for k, v in item.items():
            if k.startswith('s') and not k.endswith('f') and v and v != '0':
                ttg_data[k] = float(v)
        result['ttg'].append({
            'time': time_str,
            'ttg_data': json.dumps(ttg_data, ensure_ascii=False),
        })
    
    # HAFU (半全场)
    for item in oh.get('hafuList', []):
        time_str = f"{item.get('updateDate', '')} {item.get('updateTime', '')}".strip()
        hafu_data = {}
        hafu_keys = ['hh', 'hd', 'ha', 'dh', 'dd', 'da', 'ah', 'ad', 'aa']
        for k in hafu_keys:
            v = item.get(k)
            if v and v != '0':
                hafu_data[k] = float(v)
        result['hafu'].append({
            'time': time_str,
            'hafu_data': json.dumps(hafu_data, ensure_ascii=False),
        })
    
    return result


def save_odds_history(match_db_id, sporttery_match_id, match_date, league, home_team, away_team, odds_data):
    """保存赔率变动记录到数据库"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    inserted = 0
    
    for odds_type in ['had', 'hhad', 'crs', 'ttg', 'hafu']:
        records = odds_data.get(odds_type, [])
        for seq, rec in enumerate(records):
            try:
                c.execute('''INSERT OR IGNORE INTO odds_change_history 
                    (match_db_id, sporttery_match_id, match_date, league, home_team, away_team,
                     odds_type, update_time, seq, h, d, a, goal_line, crs_data, ttg_data, hafu_data, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (match_db_id, sporttery_match_id, match_date, league, home_team, away_team,
                     odds_type, rec['time'], seq,
                     rec.get('h'), rec.get('d'), rec.get('a'),
                     rec.get('goal_line'),
                     rec.get('crs_data'), rec.get('ttg_data'), rec.get('hafu_data'),
                     now))
                if c.rowcount > 0:
                    inserted += 1
            except Exception as e:
                pass
    
    conn.commit()
    conn.close()
    return inserted


def batch_fetch_odds_history():
    """批量抓取所有比赛的赔率变动记录"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 获取需要抓取的比赛 (有matchId但还没有赔率变动记录的)
    c.execute('''SELECT h.id, h.sporttery_match_id, h.match_date, h.league, h.home_team, h.away_team
                 FROM historical_matches h
                 WHERE h.sporttery_match_id IS NOT NULL
                   AND h.sporttery_match_id NOT IN (
                       SELECT DISTINCT sporttery_match_id FROM odds_change_history
                   )
                 ORDER BY h.match_date DESC''')
    pending = c.fetchall()
    conn.close()
    
    print(f"  待抓取比赛数: {len(pending)}")
    if not pending:
        return 0
    
    total_inserted = 0
    success = 0
    fail = 0
    
    for i, (db_id, mid, mdate, league, home, away) in enumerate(pending):
        if i % 50 == 0:
            print(f"  进度: {i}/{len(pending)} (成功={success}, 失败={fail}, 已插入={total_inserted})")
        
        odds_data = fetch_odds_history(mid)
        if odds_data:
            inserted = save_odds_history(db_id, mid, mdate, league, home, away, odds_data)
            total_inserted += inserted
            success += 1
        else:
            fail += 1
        
        # 每100场打印一次详情
        if i > 0 and i % 100 == 0:
            print(f"  [OK] 已处理 {i} 场, 成功={success}, 失败={fail}, 插入记录={total_inserted}")
    
    print(f"\n  [完成] 总计: 成功={success}, 失败={fail}, 插入赔率变动记录={total_inserted}")
    return total_inserted


# ============================================================
# 主流程
# ============================================================
def main():
    print("=" * 60)
    print("  体彩赔率变动记录采集器")
    print("=" * 60)
    
    # Step 1: 添加 sporttery_match_id 列
    print("\n[Step 1] 添加 sporttery_match_id 列...")
    add_match_id_column()
    
    # Step 2: 回填 matchId
    print("\n[Step 2] 回填 sporttery_match_id...")
    backfill_match_ids()
    
    # Step 3: 创建 odds_change_history 表
    print("\n[Step 3] 创建 odds_change_history 表...")
    create_odds_history_table()
    
    # Step 4: 批量抓取赔率变动记录
    print("\n[Step 4] 批量抓取赔率变动记录...")
    batch_fetch_odds_history()
    
    # 统计
    print("\n[统计]")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM historical_matches WHERE sporttery_match_id IS NOT NULL")
    print(f"  有matchId的比赛: {c.fetchone()[0]}")
    
    c.execute("SELECT COUNT(*) FROM odds_change_history")
    print(f"  赔率变动记录总数: {c.fetchone()[0]}")
    
    c.execute('''SELECT odds_type, COUNT(*), COUNT(DISTINCT sporttery_match_id) 
                 FROM odds_change_history GROUP BY odds_type''')
    print(f"\n  各类型赔率变动记录:")
    for r in c.fetchall():
        type_name = {'had':'胜平负', 'hhad':'让球胜平负', 'crs':'比分', 'ttg':'进球数', 'hafu':'半全场'}.get(r[0], r[0])
        print(f"    {type_name}: {r[1]} 条 ({r[2]} 场)")
    
    c.execute('''SELECT league, COUNT(DISTINCT sporttery_match_id) as matches,
                 COUNT(*) as records
                 FROM odds_change_history GROUP BY league ORDER BY records DESC''')
    print(f"\n  各联赛覆盖:")
    for r in c.fetchall():
        print(f"    {r[0]}: {r[1]} 场, {r[2]} 条记录")
    
    conn.close()
    
    print(f"\n{'=' * 60}")
    print(f"  采集完成!")
    print(f"{'=' * 60}")


if __name__ == '__main__':
    main()
