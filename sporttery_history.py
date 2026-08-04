# -*- coding: utf-8 -*-
"""
sporttery_history.py — 竞彩历史赛果+赔率历史抓取 (通用, 纯requests)

数据源 (全部为体彩官方API, sporttery数据为主原则):
  1. getUniformMatchResultV1.qry  按日期扫描赛果 (matchId/比分/半场/终赔HAD)
  2. getFixedBonusV1.qry          按matchId取赔率历史 (hadList/hhadList首条=初赔,末条=终赔,
                                  crs/ttg/hafu 取终赔)

存储: predictions/sporttery_history.db (SQLite)
  matches(matchId PK, ...)  每场一行
  odds_had(matchId, pool, ...)  初赔/终赔展开列

用法:
  python sporttery_history.py scan           # 阶段A: 扫描日期范围, 入matches表 (增量, 可重跑)
  python sporttery_history.py odds [N]       # 阶段B: 为缺赔率的场次抓赔率历史, 每次最多N场(默认500)
  python sporttery_history.py stats          # 数据覆盖统计
默认日期范围 2025-01-01 ~ 今天; 联赛过滤: 瑞超/芬超/挪超/巴甲/韩职
"""
import os, sys, json, time, sqlite3, requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_WORKSPACE, 'predictions', 'sporttery_history.db')

RESULT_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry"
BONUS_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getFixedBonusV1.qry"
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.lottery.gov.cn/jc/zqsgkj/', 'Accept': 'application/json'}

LEAGUES = {'瑞超', '芬超', '挪超', '巴甲', '韩职'}
START_DATE = '2025-01-01'

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS matches(
        matchId INTEGER PRIMARY KEY, matchDate TEXT, matchNumStr TEXT,
        league TEXT, home TEXT, away TEXT, homeId INTEGER, awayId INTEGER,
        score TEXT, halfScore TEXT, winFlag TEXT, goalLine TEXT,
        close_h REAL, close_d REAL, close_a REAL, poolStatus TEXT,
        odds_fetched INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS odds(
        matchId INTEGER, pool TEXT,
        init_json TEXT, close_json TEXT,
        PRIMARY KEY(matchId, pool))""")
    conn.commit()
    return conn

def get_json(url, params, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return None

# ---------- 阶段A: 赛果扫描 ----------
def scan(only_league=None):
    """only_league: 单联赛补抓模式 — 强制按日期扫描, 仅插入该联赛 (用于后补联赛)。
    补抓进度记录在 scan_meta 表, 中断后重跑自动续扫。"""
    conn = db()
    conn.execute("CREATE TABLE IF NOT EXISTS scan_meta(league TEXT, matchDate TEXT, PRIMARY KEY(league, matchDate))")
    if only_league:
        have = {r[0] for r in conn.execute("SELECT matchDate FROM scan_meta WHERE league=?", (only_league,))}
    else:
        have = {r[0] for r in conn.execute("SELECT DISTINCT matchDate FROM matches")}
    start = datetime.strptime(START_DATE, '%Y-%m-%d')
    today = datetime.now()
    days = [(start + timedelta(days=i)).strftime('%Y-%m-%d')
            for i in range((today - start).days + 1)]
    todo = [d for d in days if d not in have]
    print(f"扫描范围 {days[0]} ~ {days[-1]}, 已有 {len(have)} 天, 待扫 {len(todo)} 天")
    n_new = 0
    for idx, d in enumerate(todo):
        page = 1
        while True:
            j = get_json(RESULT_URL, {'matchBeginDate': d, 'matchEndDate': d, 'leagueId': '',
                                      'pageSize': '100', 'pageNo': str(page), 'isFix': '0',
                                      'matchPage': '1', 'pcOrWap': '1'})
            if not j:
                print(f"  ⚠️ {d} page{page} 请求失败, 跳过(重跑可补)")
                break
            v = j.get('value') or {}
            ms = v.get('matchResult') or []
            for m in ms:
                if only_league:
                    if m.get('leagueNameAbbr') != only_league:
                        continue
                elif m.get('leagueNameAbbr') not in LEAGUES:
                    continue
                conn.execute("""INSERT OR REPLACE INTO matches
                    (matchId,matchDate,matchNumStr,league,home,away,homeId,awayId,
                     score,halfScore,winFlag,goalLine,close_h,close_d,close_a,poolStatus)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (m.get('matchId'), m.get('matchDate'), m.get('matchNumStr'),
                     m.get('leagueNameAbbr'), m.get('allHomeTeam') or m.get('homeTeam'),
                     m.get('allAwayTeam') or m.get('awayTeam'), m.get('homeTeamId'), m.get('awayTeamId'),
                     m.get('sectionsNo999'), m.get('sectionsNo1'), m.get('winFlag'), m.get('goalLine'),
                     _f(m.get('h')), _f(m.get('d')), _f(m.get('a')), m.get('poolStatus')))
                n_new += 1
            # S5: 兼容 totalPage/pages 两种字段名, 并int()保护
            if page >= int(v.get('totalPage') or v.get('pages') or 1):
                break
            page += 1
        if only_league:
            conn.execute("INSERT OR REPLACE INTO scan_meta VALUES(?,?)", (only_league, d))
        conn.commit()
        if (idx + 1) % 30 == 0:
            print(f"  进度 {idx+1}/{len(todo)} 天, 已入库 {n_new} 场")
        time.sleep(0.25)
    print(f"✅ 扫描完成: 新增/更新 {n_new} 场, 总计 {conn.execute('SELECT COUNT(*) FROM matches').fetchone()[0]} 场")
    conn.close()

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

# ---------- 阶段B: 赔率历史 ----------
def fetch_one(mid):
    j = get_json(BONUS_URL, {'clientCode': '3001', 'matchId': str(mid)}, retries=2)
    if not j:
        return mid, None
    oh = (j.get('value') or {}).get('oddsHistory') or {}
    rows = []
    for pool, key in (('HAD', 'hadList'), ('HHAD', 'hhadList'), ('CRS', 'crsList'),
                      ('TTG', 'ttgList'), ('HAFU', 'hafuList')):
        arr = oh.get(key) or []
        if not arr:
            continue
        rows.append((mid, pool, json.dumps(arr[0], ensure_ascii=False),
                     json.dumps(arr[-1], ensure_ascii=False)))
    # 无任何玩法赔率数据 → 返回 None (与"抓取失败"同语义), 避免被标记为 odds_fetched=1 无法重跑补抓
    if not rows:
        return mid, None
    return mid, rows

def odds(limit=500):
    conn = db()
    todo = [r[0] for r in conn.execute(
        "SELECT matchId FROM matches WHERE odds_fetched=0 ORDER BY matchDate LIMIT ?", (limit,))]
    total_left = conn.execute("SELECT COUNT(*) FROM matches WHERE odds_fetched=0").fetchone()[0]
    print(f"待抓赔率 {total_left} 场, 本次 {len(todo)} 场")
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for mid, rows in ex.map(fetch_one, todo):
            if rows is None:
                fail += 1
                continue
            conn.executemany("INSERT OR REPLACE INTO odds VALUES(?,?,?,?)", rows)
            conn.execute("UPDATE matches SET odds_fetched=1 WHERE matchId=?", (mid,))
            ok += 1
            if ok % 100 == 0:
                conn.commit()
                print(f"  进度 {ok}/{len(todo)}")
    conn.commit()
    print(f"✅ 赔率抓取: 成功 {ok}, 失败 {fail} (重跑可补)")
    conn.close()

# ---------- 统计 ----------
def stats():
    conn = db()
    print("按联赛:")
    for lg, n, no in conn.execute("""SELECT league, COUNT(*),
        SUM(CASE WHEN odds_fetched=1 THEN 1 ELSE 0 END)
        FROM matches GROUP BY league ORDER BY 1"""):
        print(f"  {lg}: {n} 场 (赔率已抓 {no})")
    print("按赛季:")
    for yr, n in conn.execute("SELECT substr(matchDate,1,4), COUNT(*) FROM matches GROUP BY 1"):
        print(f"  {yr}: {n} 场")
    print("赔率池覆盖:")
    for pool, n in conn.execute("SELECT pool, COUNT(*) FROM odds GROUP BY pool"):
        print(f"  {pool}: {n} 场")
    conn.close()

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'stats'
    if cmd == 'scan':
        scan()
    elif cmd == 'scan_league':   # 单联赛补抓: python sporttery_history.py scan_league 英超
        scan(only_league=sys.argv[2])
    elif cmd == 'odds':
        odds(int(sys.argv[2]) if len(sys.argv) > 2 else 500)
    elif cmd == 'stats':
        stats()
    else:
        print(__doc__)
