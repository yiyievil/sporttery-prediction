#!/usr/bin/env python3
"""
美职联 500.com 初赔/终赔采集器
- 通过 500.com 联赛页面获取 fid 列表
- 用 fid 获取欧赔/亚赔/大小球的初赔和终赔
- 匹配数据库中的美职联比赛并更新

500.com MLS page_id:
  2025赛季: 7394
  2026赛季: 19471 (jifen_id=26185)
"""

import os
import re
import sqlite3
import time
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
import requests

DB_PATH = '/workspace/sporttery/predictions/historical_odds.db'

HEADERS_500 = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}

# 500.com MLS 联赛配置
LEAGUE_CONFIG = {
    '美职联': {
        '2025': '7394',
        '2026': '19471',
    },
}

MAX_WORKERS = 8
REQUEST_INTERVAL = 0.3

_rate_lock = threading.Lock()
_last_request_time = [0.0]


def rate_limited_get(url, **kwargs):
    with _rate_lock:
        elapsed = time.time() - _last_request_time[0]
        if elapsed < REQUEST_INTERVAL:
            time.sleep(REQUEST_INTERVAL - elapsed)
        _last_request_time[0] = time.time()
    try:
        r = requests.get(url, headers=HEADERS_500, timeout=15, **kwargs)
        r.encoding = 'gb2312'
        return r
    except:
        return None


# ============================================================
# Phase 1: 收集 fid
# ============================================================
def get_jifen_id(page_id):
    r = rate_limited_get(f'https://liansai.500.com/zuqiu-{page_id}/')
    if not r:
        return None
    m = re.search(r'href="/zuqiu-\d+/jifen-(\d+)/"', r.text)
    return m.group(1) if m else None


def get_team_ids(page_id, jifen_id):
    if not jifen_id:
        return []
    r = rate_limited_get(f'https://liansai.500.com/zuqiu-{page_id}/jifen-{jifen_id}/')
    if not r:
        return []
    selects = re.findall(r'<select[^>]*>(.*?)</select>', r.text, re.DOTALL)
    for sel in selects:
        opts = re.findall(r'<option[^>]*value="(\d+)"[^>]*>([^<]+)</option>', sel)
        teams = [(v, t.strip()) for v, t in opts if v != '0' and len(t.strip()) >= 2]
        if 5 < len(teams) < 50:
            return teams
    team_links = re.findall(r'<a[^>]*href="[^"]*team/(\d+)/"[^>]*>([^<]+)</a>', r.text)
    seen = set()
    teams = []
    for tid, name in team_links:
        if tid not in seen and len(name.strip()) >= 2:
            seen.add(tid)
            teams.append((tid, name.strip()))
    return teams[:30]


def fetch_team_fixtures(team_id):
    r = rate_limited_get(f'https://liansai.500.com/team/{team_id}/teamfixture/')
    if not r:
        return []
    matches = []
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', r.text, re.DOTALL)
    for row in rows:
        if 'shuju-' not in row:
            continue
        fid_match = re.search(r'shuju-(\d+)\.shtml', row)
        date_match = re.search(r'(20\d{2}-\d{2}-\d{2})', row)
        if not (fid_match and date_match):
            continue
        fid = fid_match.group(1)
        date = date_match.group(1)
        team_links = re.findall(r'<a[^>]*href="[^"]*team/\d+/"[^>]*>([^<]+)</a>', row)
        home = team_links[0].strip() if len(team_links) >= 1 else ''
        away = team_links[1].strip() if len(team_links) >= 2 else ''
        if not home or not away:
            title_match = re.search(r'title="([^"]+)"', row)
            if title_match:
                title = title_match.group(1).replace('数据分析', '').replace('比分', '').strip()
                vs_match = re.match(r'(.+?)VS(.+)', title)
                if vs_match:
                    if not home: home = vs_match.group(1).strip()
                    if not away: away = vs_match.group(2).strip()
        if home and away:
            matches.append({'fid': fid, 'date': date, 'home': home, 'away': away})
    return matches


def fetch_jifen_fids(page_id, jifen_id):
    if not jifen_id:
        return []
    r = rate_limited_get(f'https://liansai.500.com/zuqiu-{page_id}/jifen-{jifen_id}/')
    if not r:
        return []
    matches = []
    fid_pattern = re.findall(r'data-fid="(\d+)"[^>]*>(.*?)</tr>', r.text, re.DOTALL)
    for fid, row_html in fid_pattern:
        time_match = re.search(r'data-time="(20\d{2}-\d{2}-\d{2})', row_html)
        date = time_match.group(1) if time_match else ''
        team_links = re.findall(r'<a[^>]*href="[^"]*team/\d+/"[^>]*>([^<]+)</a>', row_html)
        home = team_links[0].strip() if len(team_links) >= 1 else ''
        away = team_links[1].strip() if len(team_links) >= 2 else ''
        if home and away:
            matches.append({'fid': fid, 'date': date, 'home': home, 'away': away})
    if not matches:
        for fid in set(re.findall(r'shuju-(\d+)\.shtml', r.text)):
            matches.append({'fid': fid, 'date': '', 'home': '', 'away': ''})
    return matches


def collect_all_fids():
    all_fids = {}
    print('\n[Phase 1] 收集 fixture_id...')
    for league, seasons in LEAGUE_CONFIG.items():
        for season, page_id in seasons.items():
            print(f'  {league} {season} (page={page_id})...')
            jifen_id = get_jifen_id(page_id)
            if not jifen_id:
                print(f'    无法获取 jifen ID, 跳过')
                continue
            print(f'    jifen_id={jifen_id}')
            
            jifen_matches = fetch_jifen_fids(page_id, jifen_id)
            for m in jifen_matches:
                m['league'] = league; m['season'] = season
                if m['fid'] not in all_fids:
                    all_fids[m['fid']] = m
                elif not all_fids[m['fid']].get('home') and m.get('home'):
                    all_fids[m['fid']] = m
            print(f'    jifen: {len(jifen_matches)} 个')
            
            teams = get_team_ids(page_id, jifen_id)
            print(f'    球队: {len(teams)} 支')
            
            if teams:
                fixture_matches = []
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                    futures = {pool.submit(fetch_team_fixtures, tid): tid for tid, _ in teams}
                    for fut in as_completed(futures):
                        try:
                            fixture_matches.extend(fut.result())
                        except:
                            pass
                for m in fixture_matches:
                    m['league'] = league; m['season'] = season
                    if m['fid'] not in all_fids:
                        all_fids[m['fid']] = m
                    elif not all_fids[m['fid']].get('home') and m.get('home'):
                        all_fids[m['fid']] = m
                print(f'    teamfixture: {len(fixture_matches)} 个')
    
    print(f'\n  总计唯一 fid: {len(all_fids)} 个')
    return all_fids


# ============================================================
# Phase 2: 从 shuju 页面补充比赛信息
# ============================================================
def fetch_match_info_from_shuju(fid):
    r = rate_limited_get(f'https://odds.500.com/fenxi/shuju-{fid}.shtml')
    if not r:
        return None
    home, away = '', ''
    title_match = re.search(r'<title>([^<]+)</title>', r.text)
    if title_match:
        title = title_match.group(1)
        vs_match = re.match(r'([^(VS]+?)VS([^()（]+)', title)
        if vs_match:
            home = vs_match.group(1).strip()
            away = vs_match.group(2).strip()
    date_match = re.search(r'(?:比赛时间|开赛时间)[^0-9]*(20\d{2}-\d{2}-\d{2})', r.text)
    date = date_match.group(1) if date_match else ''
    if home and away:
        return {'home': home, 'away': away, 'date': date}
    return None


def enrich_fid_info(all_fids):
    need_enrich = [(fid, info) for fid, info in all_fids.items()
                   if not info.get('home') or not info.get('date')]
    if not need_enrich:
        return
    print(f'\n[Phase 2] 从 shuju 页面补充 {len(need_enrich)} 个 fid...')
    enriched = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_match_info_from_shuju, fid): fid for fid, _ in need_enrich}
        for i, fut in enumerate(as_completed(futures)):
            try:
                result = fut.result()
                if result:
                    fid = futures[fut]
                    if result.get('home'): all_fids[fid]['home'] = result['home']
                    if result.get('away'): all_fids[fid]['away'] = result['away']
                    if result.get('date'): all_fids[fid]['date'] = result['date']
                    enriched += 1
            except:
                pass
            if (i + 1) % 50 == 0:
                print(f'    进度: {i+1}/{len(need_enrich)}')
    print(f'  补充成功: {enriched} 个')


# ============================================================
# Phase 3: 获取初赔/终赔
# ============================================================
def fetch_ouzhi_odds(fid):
    r = rate_limited_get(
        f'https://odds.500.com/fenxi1/ouzhi.php?id={fid}'
        f'&chupan=1&ctype=0&start=0&r=1&style=0&guojia=0&currentIndex=0'
    )
    if not r:
        return None
    html = r.text
    company_count = re.search(r'共<span[^>]*>(\d+)</span>家', html)
    if not company_count or int(company_count.group(1)) == 0:
        return None
    quancheng_positions = [
        (m.start(), m.group(1).strip())
        for m in re.finditer(r'<span class="quancheng"[^>]*>([^<]+)</span>', html)
    ]
    table_positions = [
        (m.start(), m.group(1))
        for m in re.finditer(r'<table[^>]*class="pl_table_data"[^>]*>(.*?)</table>', html, re.DOTALL)
    ]
    companies = []
    for i, (q_pos, name) in enumerate(quancheng_positions):
        next_q = quancheng_positions[i + 1][0] if i + 1 < len(quancheng_positions) else len(html)
        for t_pos, t_html in table_positions:
            if t_pos > q_pos and t_pos < next_q:
                trs = re.findall(r'<tr[^>]*>(.*?)</tr>', t_html, re.DOTALL)
                if len(trs) >= 2:
                    final_vals = re.findall(r'<td[^>]*>\s*(\d+\.\d{2})\s*</td>', trs[0])
                    init_vals = re.findall(r'<td[^>]*>\s*(\d+\.\d{2})\s*</td>', trs[1])
                    if len(final_vals) == 3 and len(init_vals) == 3:
                        inst = tuple(float(v) for v in final_vals)
                        init = tuple(float(v) for v in init_vals)
                        if all(v > 1.0 for v in inst + init):
                            companies.append({'instant': inst, 'initial': init})
                break
    if not companies:
        return None
    n = len(companies)
    return {
        'fc_fid': fid,
        'fc_ouzhi_init_w': round(sum(c['initial'][0] for c in companies) / n, 3),
        'fc_ouzhi_init_d': round(sum(c['initial'][1] for c in companies) / n, 3),
        'fc_ouzhi_init_l': round(sum(c['initial'][2] for c in companies) / n, 3),
        'fc_ouzhi_final_w': round(sum(c['instant'][0] for c in companies) / n, 3),
        'fc_ouzhi_final_d': round(sum(c['instant'][1] for c in companies) / n, 3),
        'fc_ouzhi_final_l': round(sum(c['instant'][2] for c in companies) / n, 3),
        'fc_num_bookmakers': n,
    }


def fetch_yazhi_daxiao_odds(fid):
    """获取亚指和大小球初赔/终赔"""
    # 亚指
    r = rate_limited_get(
        f'https://odds.500.com/fenxi1/yazhi.php?id={fid}'
        f'&chupan=1&ctype=0&start=0&r=1&style=0&guojia=0&currentIndex=0'
    )
    yazhi_init = yazhi_final = None
    if r:
        html = r.text
        quancheng_positions = [
            (m.start(), m.group(1).strip())
            for m in re.finditer(r'<span class="quancheng"[^>]*>([^<]+)</span>', html)
        ]
        table_positions = [
            (m.start(), m.group(1))
            for m in re.finditer(r'<table[^>]*class="pl_table_data"[^>]*>(.*?)</table>', html, re.DOTALL)
        ]
        init_handicaps = []
        final_handicaps = []
        for i, (q_pos, name) in enumerate(quancheng_positions):
            next_q = quancheng_positions[i + 1][0] if i + 1 < len(quancheng_positions) else len(html)
            for t_pos, t_html in table_positions:
                if t_pos > q_pos and t_pos < next_q:
                    trs = re.findall(r'<tr[^>]*>(.*?)</tr>', t_html, re.DOTALL)
                    if len(trs) >= 2:
                        for ti, tr in enumerate(trs[:2]):
                            tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.DOTALL)
                            clean = [re.sub(r'<[^>]+>', '', td).strip() for td in tds]
                            for v in clean:
                                v = v.strip()
                                if re.match(r'^[+\-]?\d+(\.\d+)?$', v) and abs(float(v)) < 10:
                                    if ti == 0: final_handicaps.append(float(v))
                                    else: init_handicaps.append(float(v))
                                    break
                    break
        yazhi_init = round(sum(init_handicaps) / len(init_handicaps), 2) if init_handicaps else None
        yazhi_final = round(sum(final_handicaps) / len(final_handicaps), 2) if final_handicaps else None

    # 大小球
    r2 = rate_limited_get(
        f'https://odds.500.com/fenxi1/daxiao.php?id={fid}'
        f'&chupan=1&ctype=0&start=0&r=1&style=0&guojia=0&currentIndex=0'
    )
    daxiao_init = daxiao_final = None
    if r2:
        html = r2.text
        quancheng_positions = [
            (m.start(), m.group(1).strip())
            for m in re.finditer(r'<span class="quancheng"[^>]*>([^<]+)</span>', html)
        ]
        table_positions = [
            (m.start(), m.group(1))
            for m in re.finditer(r'<table[^>]*class="pl_table_data"[^>]*>(.*?)</table>', html, re.DOTALL)
        ]
        init_lines = []
        final_lines = []
        for i, (q_pos, name) in enumerate(quancheng_positions):
            next_q = quancheng_positions[i + 1][0] if i + 1 < len(quancheng_positions) else len(html)
            for t_pos, t_html in table_positions:
                if t_pos > q_pos and t_pos < next_q:
                    trs = re.findall(r'<tr[^>]*>(.*?)</tr>', t_html, re.DOTALL)
                    if len(trs) >= 2:
                        for ti, tr in enumerate(trs[:2]):
                            tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.DOTALL)
                            clean = [re.sub(r'<[^>]+>', '', td).strip() for td in tds]
                            for v in clean:
                                v = v.strip()
                                # 大小球盘口: 2, 2.5, 3, 3.5 等
                                if re.match(r'^\d+(\.\d+)?$', v) and 0.5 <= float(v) <= 10:
                                    if ti == 0: final_lines.append(float(v))
                                    else: init_lines.append(float(v))
                                    break
                    break
        daxiao_init = round(sum(init_lines) / len(init_lines), 2) if init_lines else None
        daxiao_final = round(sum(final_lines) / len(final_lines), 2) if final_lines else None

    return yazhi_init, yazhi_final, daxiao_init, daxiao_final


def fetch_all_odds(all_fids):
    print(f'\n[Phase 3] 获取赔率数据 (并行 max_workers={MAX_WORKERS})...')
    
    # 只获取美职联相关日期的 fid
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT match_date FROM historical_matches WHERE league='美职联' AND fc_fid IS NULL")
    db_dates = set()
    for row in c.fetchall():
        d = row[0][:10] if row[0] else ''
        if d: db_dates.add(d)
    conn.close()
    
    # 日期过滤
    filtered = {}
    for fid, info in all_fids.items():
        date = info.get('date', '')
        if not date:
            filtered[fid] = info
            continue
        try:
            d = datetime.strptime(date, '%Y-%m-%d')
            for delta in range(-1, 2):
                if (d + timedelta(days=delta)).strftime('%Y-%m-%d') in db_dates:
                    filtered[fid] = info
                    break
        except:
            filtered[fid] = info
    
    fids = list(filtered.keys())
    print(f'  日期过滤: {len(all_fids)} → {len(fids)} 个 fid')
    
    odds_results = {}
    yazhi_results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_ouzhi_odds, fid): fid for fid in fids}
        for i, fut in enumerate(as_completed(futures)):
            fid = futures[fut]
            try:
                odds = fut.result()
                if odds and odds.get('fc_ouzhi_final_w'):
                    odds_results[fid] = odds
            except:
                pass
            if (i + 1) % 50 == 0:
                print(f'    欧赔进度: {i+1}/{len(fids)}, 有赔率: {len(odds_results)}')
    
    print(f'  欧赔获取成功: {len(odds_results)} / {len(fids)}')
    
    # 获取亚指和大小球 (只对有欧赔的fid)
    print(f'  获取亚指+大小球...')
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_yazhi_daxiao_odds, fid): fid for fid in odds_results.keys()}
        for i, fut in enumerate(as_completed(futures)):
            fid = futures[fut]
            try:
                yazhi_results[fid] = fut.result()
            except:
                pass
            if (i + 1) % 50 == 0:
                print(f'    亚指+大小球进度: {i+1}/{len(odds_results)}')
    
    return odds_results, yazhi_results


# ============================================================
# Phase 4: 匹配并更新数据库
# ============================================================
def normalize_team_name(name):
    if not name: return ''
    name = name.strip()
    for suffix in ['足球俱乐部', '足球会', '队', 'FC', 'fc', '队队']:
        name = name.replace(suffix, '')
    return name.strip()


def fuzzy_match_team(name1, name2):
    if not name1 or not name2: return False
    n1 = normalize_team_name(name1)
    n2 = normalize_team_name(name2)
    if not n1 or not n2: return False
    if n1 == n2: return True
    if len(n1) >= 2 and len(n2) >= 2:
        if n1 in n2 or n2 in n1: return True
    min_len = min(len(n1), len(n2))
    if min_len >= 3 and n1[:3] == n2[:3]: return True
    if min_len >= 4 and n1[:4] == n2[:4]: return True
    set1, set2 = set(n1), set(n2)
    common = set1 & set2
    shorter_len = min(len(n1), len(n2))
    if shorter_len >= 2 and len(common) / shorter_len >= 0.6: return True
    if SequenceMatcher(None, n1, n2).ratio() >= 0.6: return True
    return False


def match_and_update(all_fids, odds_results, yazhi_results):
    print(f'\n[Phase 4] 匹配并更新数据库...')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT id, match_date, league, season, home_team, away_team
                 FROM historical_matches WHERE league='美职联' AND fc_fid IS NULL''')
    
    by_date = {}
    all_records = []
    for row in c.fetchall():
        rec = {'id': row[0], 'date': row[1], 'league': row[2], 'season': row[3],
               'home': row[4], 'away': row[5]}
        all_records.append(rec)
        d = row[1][:10] if row[1] else ''
        if d not in by_date: by_date[d] = []
        by_date[d].append(rec)
    
    print(f'  数据库中待匹配记录: {len(all_records)} 条')
    
    matched = 0
    for fid, info in all_fids.items():
        odds = odds_results.get(fid)
        if not odds: continue
        
        home = info.get('home', '')
        away = info.get('away', '')
        date = info.get('date', '')
        if not home or not away or not date: continue
        
        candidates = []
        try:
            d = datetime.strptime(date, '%Y-%m-%d')
            for delta in range(-1, 2):
                d_str = (d + timedelta(days=delta)).strftime('%Y-%m-%d')
                candidates.extend(by_date.get(d_str, []))
        except:
            candidates = by_date.get(date, [])
        
        best_match = None
        best_score = 0
        for rec in candidates:
            if fuzzy_match_team(home, rec['home']) and fuzzy_match_team(away, rec['away']):
                score = 1.0
                try:
                    rd = datetime.strptime(rec['date'][:10], '%Y-%m-%d')
                    md = datetime.strptime(date, '%Y-%m-%d')
                    diff = abs((rd - md).days)
                    score = 1.0 / (1 + diff)
                except:
                    pass
                if score > best_score:
                    best_score = score
                    best_match = rec
        
        if best_match:
            yazhi = yazhi_results.get(fid, (None, None, None, None))
            c.execute('''UPDATE historical_matches SET
                fc_fid=?, fc_ouzhi_init_w=?, fc_ouzhi_init_d=?, fc_ouzhi_init_l=?,
                fc_ouzhi_final_w=?, fc_ouzhi_final_d=?, fc_ouzhi_final_l=?,
                fc_yazhi_init=?, fc_yazhi_final=?,
                fc_daxiao_init=?, fc_daxiao_final=?,
                fc_num_bookmakers=?
                WHERE id=? AND fc_fid IS NULL''',
                (int(fid),
                 odds['fc_ouzhi_init_w'], odds['fc_ouzhi_init_d'], odds['fc_ouzhi_init_l'],
                 odds['fc_ouzhi_final_w'], odds['fc_ouzhi_final_d'], odds['fc_ouzhi_final_l'],
                 yazhi[0], yazhi[1],
                 yazhi[2], yazhi[3],
                 odds['fc_num_bookmakers'],
                 best_match['id']))
            if c.rowcount > 0:
                matched += 1
    
    conn.commit()
    conn.close()
    print(f'  匹配并更新: {matched} 条')
    return matched


# ============================================================
# 主流程
# ============================================================
def main():
    t0 = time.time()
    print('=' * 60)
    print('  美职联 500.com 初赔/终赔采集器')
    print('=' * 60)
    
    all_fids = collect_all_fids()
    enrich_fid_info(all_fids)
    odds_results, yazhi_results = fetch_all_odds(all_fids)
    matched = match_and_update(all_fids, odds_results, yazhi_results)
    
    # 统计
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM historical_matches WHERE league='美职联'")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM historical_matches WHERE league='美职联' AND fc_fid IS NOT NULL")
    has_fid = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM historical_matches WHERE league='美职联' AND fc_ouzhi_init_w IS NOT NULL")
    has_init = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM historical_matches WHERE league='美职联' AND fc_yazhi_init IS NOT NULL")
    has_yazhi = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM historical_matches WHERE league='美职联' AND fc_daxiao_init IS NOT NULL")
    has_daxiao = c.fetchone()[0]
    conn.close()
    
    print(f'\n{"=" * 60}')
    print(f'  美职联数据统计:')
    print(f'  总比赛: {total}')
    print(f'  有fc_fid: {has_fid} ({has_fid/total*100:.0f}%)')
    print(f'  有初赔: {has_init} ({has_init/total*100:.0f}%)')
    print(f'  有亚指: {has_yazhi} ({has_yazhi/total*100:.0f}%)')
    print(f'  有大小球: {has_daxiao} ({has_daxiao/total*100:.0f}%)')
    
    total_time = time.time() - t0
    print(f'\n  采集完成! 总耗时: {total_time:.1f}s ({total_time/60:.1f}min)')
    print(f'  成功更新: {matched} 场')
    print(f'{"=" * 60}')


if __name__ == '__main__':
    main()
