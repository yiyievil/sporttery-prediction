#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
historical_odds.db.historical_matches 赛果/赔率空档补采器

数据源: 体彩 getUniformMatchResultV1 API
  - 返回任意联赛的 HAD 终赔 (h/d/a) + 全场/半场比分 (sectionsNo999/sectionsNo1)
    + 赛果 (winFlag: H/D/A) + 让球 (goalLine) + matchId
  - 不依赖仓库内缺失的多联赛采集脚本, 通用覆盖全部联赛

策略:
  - 按日期区间拉取 (默认 2026-07-27 ~ 2026-08-11)
  - leagueNameAbbr -> DB 联赛名映射 (仅 '美职'->'美职联', 其余同名)
  - 幂等 upsert: 主键 (match_date, home_team, away_team)
      * 不存在 -> INSERT
      * 已存在且旧比分 NULL、新有比分 -> UPDATE 比分/赔率/赛果/winFlag
      * 否则跳过 (避免重复/覆盖)

依赖: 仅标准库 (urllib), 任何 Python 可跑。可重复运行。

用法:
  python scripts/backfill_historical_odds.py
  python scripts/backfill_historical_odds.py --begin 2026-07-27 --end 2026-08-11
"""
import os
import sys
import json
import sqlite3
import time
import argparse
import datetime
import urllib.request
import urllib.parse

# 脚本位于 <workspace>/scripts/ 下, 仓库根为其父目录
_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE',
                            os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(_WORKSPACE, 'predictions', 'historical_odds.db')

RESULT_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry"
HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://www.lottery.gov.cn/jc/zqsgkj/',
    'Accept': 'application/json',
}

# 体彩联赛简称 -> DB historical_matches.league 名
LEAGUE_MAP = {
    '美职': '美职联',
}

INSERT_COLS = (
    'match_date', 'league', 'season', 'home_team', 'away_team',
    'home_score', 'away_score', 'half_home_score', 'half_away_score',
    'result', 'sp_had_h', 'sp_had_d', 'sp_had_a', 'sp_goal_line',
    'sp_match_num', 'source', 'sporttery_match_id', 'created_at',
)


def _safe_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def parse_score(s):
    """'0:3' -> (0, 3); 无效 -> (None, None)"""
    if not s or ':' not in str(s):
        return (None, None)
    a, b = str(s).split(':', 1)
    try:
        return (int(a.strip()), int(b.strip()))
    except (ValueError, TypeError):
        return (None, None)


def fetch_range(begin, end):
    """拉取 [begin, end] 区间内全部联赛比赛 (分页累积)"""
    out = []
    page = 1
    while True:
        params = {
            'matchBeginDate': begin,
            'matchEndDate': end,
            'leagueId': '',          # 空 = 全部联赛
            'pageSize': '100',
            'pageNo': str(page),
            'isFix': '0',
            'matchPage': '1',
            'pcOrWap': '1',
        }
        url = RESULT_URL + '?' + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            print(f"  [WARN] 请求失败(页{page}): {e}")
            break
        val = data.get('value', {})
        results = val.get('matchResult', [])
        if not results:
            break
        out.extend(results)
        total_pages = int(val.get('totalPage') or val.get('pages') or 1)
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.3)
    return out


def normalize(raw):
    """把 API 原始记录规范化为待入库 dict"""
    abbr = (raw.get('leagueNameAbbr') or '').strip()
    league = LEAGUE_MAP.get(abbr, abbr)
    if not league:
        return None
    match_date = (raw.get('matchDate') or '').strip()
    if not match_date:
        return None
    home = (raw.get('homeTeam') or '').strip()
    away = (raw.get('awayTeam') or '').strip()
    if not home or not away:
        return None

    hs, as_ = parse_score(raw.get('sectionsNo999'))
    hhs, has_ = parse_score(raw.get('sectionsNo1'))
    season = match_date[:4]  # 2026

    return {
        'match_date': match_date,
        'league': league,
        'season': season,
        'home_team': home,
        'away_team': away,
        'home_score': hs,
        'away_score': as_,
        'half_home_score': hhs,
        'half_away_score': has_,
        'result': (raw.get('winFlag') or '').strip(),
        'sp_had_h': _safe_float(raw.get('h')),
        'sp_had_d': _safe_float(raw.get('d')),
        'sp_had_a': _safe_float(raw.get('a')),
        'sp_goal_line': str(raw.get('goalLine', '') or ''),
        'sp_match_num': (raw.get('matchNumStr') or '').strip(),
        'source': 'sporttery',
        'sporttery_match_id': raw.get('matchId'),
        'created_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def backfill(begin, end):
    print(f"[拉取] {begin} ~ {end} ...")
    raw = fetch_range(begin, end)
    print(f"  API 返回 {len(raw)} 场")

    rows = [r for r in (normalize(m) for m in raw) if r]
    print(f"  规范化 {len(rows)} 场 (跳过缺失主客队/日期 {len(raw)-len(rows)} 场)")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    inserted = updated = skipped = 0
    league_new = {}

    for r in rows:
        ex = c.execute(
            'SELECT id, home_score, away_score FROM historical_matches '
            'WHERE match_date=? AND home_team=? AND away_team=?',
            (r['match_date'], r['home_team'], r['away_team'])
        ).fetchone()

        if ex is None:
            qmarks = ','.join(['?'] * len(INSERT_COLS))
            c.execute(
                f"INSERT INTO historical_matches ({','.join(INSERT_COLS)}) "
                f"VALUES ({qmarks})",
                tuple(r[col] for col in INSERT_COLS)
            )
            inserted += 1
            league_new[r['league']] = league_new.get(r['league'], 0) + 1
        else:
            # 已存在: 仅当旧比分缺失且新有比分时补更新
            if ex[1] is None and r['home_score'] is not None:
                c.execute(
                    '''UPDATE historical_matches SET
                         home_score=?, away_score=?, half_home_score=?, half_away_score=?,
                         result=?, sp_had_h=?, sp_had_d=?, sp_had_a=?,
                         sp_goal_line=?, sp_match_num=?, sporttery_match_id=?, source=?
                       WHERE id=?''',
                    (r['home_score'], r['away_score'], r['half_home_score'], r['half_away_score'],
                     r['result'], r['sp_had_h'], r['sp_had_d'], r['sp_had_a'],
                     r['sp_goal_line'], r['sp_match_num'], r['sporttery_match_id'], r['source'],
                     ex[0])
                )
                updated += 1
            else:
                skipped += 1

    conn.commit()
    conn.close()

    print(f"\n[结果] 新增={inserted}  补更新={updated}  跳过(已存在)={skipped}")
    if league_new:
        print("  各联赛新增:")
        for lg, n in sorted(league_new.items(), key=lambda x: -x[1]):
            print(f"    {lg}: +{n}")
    return inserted, updated, skipped


def main():
    ap = argparse.ArgumentParser(description="historical_matches 空档补采")
    ap.add_argument('--begin', default='2026-07-27')
    ap.add_argument('--end', default='2026-08-11')
    args = ap.parse_args()

    t0 = time.time()
    print("=" * 56)
    print("  historical_odds.db 赛果/赔率补采")
    print(f"  区间: {args.begin} ~ {args.end}")
    print("=" * 56)
    ins, upd, skp = backfill(args.begin, args.end)
    print(f"\n完成 (耗时 {time.time()-t0:.1f}s). 新增{ins}/更新{upd}/跳过{skp}")


if __name__ == '__main__':
    main()
