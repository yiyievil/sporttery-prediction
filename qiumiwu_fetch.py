#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qiumiwu.com (球迷屋) 数据采集模块

覆盖维度 (远超 nowscore + 500.com):
  A. 比赛前瞻 (近况/交战/进失球分布/伤停/赛程) — 替代 nowscore 统计
  B. 球队赛季数据 (35+ 高级指标) — nowscore 完全不具备
  C. 联赛积分榜 (总/主/客) — 补充 nowscore 积分榜

数据流:
  qiumiwu 赛程页 (WebFetch → 一次性缓存) → 匹配 sporttery 场次
  → 请求比赛前瞻页 (HTTP 直连, SSR) → 解析近况/交战/进失球/伤停
  → 请求球队数据页 (可选) → 35+ 指标

设计原则:
  - 赛程页需要 JS 渲染 (WebFetch), 每日缓存一次
  - 前瞻页/球队页/积分榜均为 SSR, 用 requests 直连
  - 队名匹配使用 match_utils 多信号融合
"""

from __future__ import annotations
import json
import logging
import math
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import requests

from match_utils import MatchFingerprint, match_score, find_best_match, find_best_match_with_learning

logger = logging.getLogger("qiumiwu")

# ============================================================
# 配置
# ============================================================
BASE_URL = "https://m.qiumiwu.com"
SCHEDULE_CACHE = Path(__file__).parent / "predictions" / "qiumiwu_schedule.json"
SCHEDULE_CACHE_TTL = 3600  # 1小时

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
})


# ============================================================
# A. 赛程发现 (从 WebFetch 渲染后的 HTML 解析)
# ============================================================

def parse_schedule_html(html: str) -> list[dict]:
    """从 WebFetch 渲染的赛程 HTML 中提取比赛列表

    支持两种格式:
      格式A (旧, 已废弃): 纯文本排版
      格式B (新, SSR HTML): fixture__list 结构

    返回: [{game_id, home, away, time, league, date, status}, ...]
    """
    today = date.today()

    # 检测格式: 看是否有 fixture__list 结构
    if 'fixture__list' in html:
        return _parse_schedule_html_v2(html, today)

    # 旧格式: 回退
    return _parse_schedule_html_v1(html, today)


def _parse_schedule_html_v2(html: str, today: date) -> list[dict]:
    """解析 SSR HTML 格式 (https://m.qiumiwu.com/game/zuqiu)

    格式:
      <span>明天 08-14 星期五</span> <span>52场</span>
      <div ... data-path="/game/111391318499" ... status-alias="wait">
        <div class="fixture__list__header">
          <span>02:30</span>
          <span>欧联杯资格赛3</span>
        </div>
        <a ...>
          <div ...><span>流浪者</span></div>
          <div ...></div>
          <div ...><span>乔治罗尼亚</span></div>
        </a>
      </div>
    """
    matches = []

    # 1. 解析日期块
    date_sections = []
    for m in re.finditer(
        r'<span>(今天|明天|\d{2}-\d{2})\s+(\d{2}-\d{2})\s+星期[一二三四五六日]</span>\s*<span>\d+场</span>',
        html
    ):
        label = m.group(1)
        month_day = m.group(2)
        month, day = month_day.split('-')
        match_date = date(today.year, int(month), int(day))
        if match_date < today:
            match_date = match_date.replace(year=today.year + 1)
        date_sections.append({'start': m.end(), 'date': match_date})

    # 2. 为每个日期块分配结束位置
    for i, ds in enumerate(date_sections):
        if i + 1 < len(date_sections):
            ds['end'] = date_sections[i + 1]['start']
        else:
            ds['end'] = len(html)

    # 3. 解析每个日期块内的比赛
    seen_ids = set()
    for ds in date_sections:
        block = html[ds['start']:ds['end']]

        for m in re.finditer(
            r'data-path="/game/(\d{10,})".*?status-alias="(\w+)".*?'
            r'<span>(\d{2}:\d{2})</span>.*?'
            r'(?:<div[^>]*>\s*<span>([^<]*)</span>\s*</div>\s*)?'
            r'<span>([^<]{2,40})</span>\s*</div>\s*'
            r'<a[^>]*href="/game/\d+[^>]*>\s*'
            r'<div[^>]*>\s*<span>([^<]{2,30})</span>\s*</div>\s*'
            r'<div[^>]*></div>\s*'
            r'<div[^>]*>\s*<span>([^<]{2,30})</span>\s*</div>\s*'
            r'</a>',
            block, re.DOTALL
        ):
            gid = m.group(1)
            if gid in seen_ids:
                continue
            seen_ids.add(gid)

            status_alias = m.group(2)
            match_time = m.group(3)
            league = m.group(5).strip()
            home = m.group(6).strip()
            away = m.group(7).strip()

            # 状态映射
            status = "scheduled"
            if status_alias == 'live':
                status = "live"
            elif status_alias in ('finish', 'fin'):
                status = "finished"

            # 联赛名清理
            league = re.sub(r'第\d+轮\s*$', '', league)
            league = re.sub(r'\d+/\d+决赛$', '', league)
            league = re.sub(r'资格赛\d+', '', league)
            league = re.sub(r'\s+$', '', league)

            matches.append({
                "game_id": gid,
                "home": home,
                "away": away,
                "time": match_time,
                "league": league,
                "date": ds['date'].strftime("%Y-%m-%d"),
                "status": status,
            })

    return matches


def _parse_schedule_html_v1(html: str, today: date) -> list[dict]:
    """解析旧格式 (WebFetch 渲染的 schedule/zuqiu 页面)

    格式:
      00:15  高清直播  沙特联第1轮 联赛
      [艾卜哈](https://m.qiumiwu.com/game/111161528770)
      [哈森姆](https://m.qiumiwu.com/game/111161528770)
    """
    matches = []

    date_blocks = list(re.finditer(
        r'(今天|明天|\d{2}-\d{2})\s+\d{2}-\d{2}\s+星期[一二三四五六日]',
        html
    ))

    for bi, block in enumerate(date_blocks):
        date_str = re.search(r'(\d{2}-\d{2})', block.group())
        if date_str:
            month, day = date_str.group(1).split('-')
            match_date = date(today.year, int(month), int(day))
            if match_date < today:
                match_date = match_date.replace(year=today.year + 1)
        elif '今天' in block.group():
            match_date = today
        elif '明天' in block.group():
            match_date = today + timedelta(days=1)
        else:
            continue

        start = block.end()
        end = date_blocks[bi + 1].start() if bi + 1 < len(date_blocks) else len(html)
        block_html = html[start:end]

        for time_m in re.finditer(r'(\d{2}:\d{2})', block_html):
            time_str = time_m.group(1)
            ctx = block_html[time_m.start():time_m.start() + 500]

            league_match = re.search(
                r'\d{2}:\d{2}\s*\n\s*(?:完场|录像回放|高清直播|未赛|推迟|待定)?\s*\n\s*([^\n<]{2,30})',
                ctx
            )
            league = league_match.group(1).strip() if league_match else ""

            game_links = list(re.finditer(r'/game/(?:stat-)?(\d{10,})[\"\'][^>]*>([^<]{2,30})<', ctx))
            if len(game_links) >= 2:
                gid = game_links[0].group(1)
                home = game_links[0].group(2).strip()
                away = game_links[1].group(2).strip()

                status = "live"
                if '完场' in ctx[:100]:
                    status = "finished"
                elif '未赛' in ctx[:100]:
                    status = "scheduled"

                matches.append({
                    "game_id": gid,
                    "home": home,
                    "away": away,
                    "time": time_str,
                    "league": league,
                    "date": match_date.strftime("%Y-%m-%d"),
                    "status": status,
                })

    return matches


def load_schedule_cache() -> Optional[list[dict]]:
    """加载缓存的赛程数据"""
    if not SCHEDULE_CACHE.exists():
        return None
    try:
        data = json.loads(SCHEDULE_CACHE.read_text())
        if time.time() - data.get("ts", 0) < SCHEDULE_CACHE_TTL:
            return data.get("matches", [])
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def save_schedule_cache(matches: list[dict]):
    """保存赛程缓存"""
    SCHEDULE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULE_CACHE.write_text(json.dumps({
        "ts": time.time(),
        "matches": matches,
    }, ensure_ascii=False, indent=2))


def fetch_schedule_from_game_page(force_refresh: bool = False) -> Optional[list[dict]]:
    """从 https://m.qiumiwu.com/game/zuqiu 直接获取赛程 (SSR, 无需 JS)

    自动缓存, 默认1小时内不重复请求。
    """
    if not force_refresh:
        cached = load_schedule_cache()
        if cached:
            return cached

    url = f"{BASE_URL}/game/zuqiu"
    try:
        r = SESSION.get(url, timeout=20)
        if r.status_code != 200:
            logger.warning(f"qiumiwu schedule: HTTP {r.status_code}")
            return None
    except Exception as e:
        logger.warning(f"qiumiwu schedule: {e}")
        return None

    matches = parse_schedule_html(r.text)
    if matches:
        save_schedule_cache(matches)
        logger.info(f"qiumiwu: 赛程缓存已更新 ({len(matches)} 场)")

    return matches


# ============================================================
# B. 比赛前瞻页解析
# ============================================================

def fetch_match_preview(game_id: str) -> Optional[dict]:
    """获取比赛前瞻数据 (SSR, 纯 requests 直连)

    返回:
      {
        "game_id": str,
        "home": str, "away": str,
        "home_rank": str, "away_rank": str,
        "league": str, "match_time": str, "match_date": str,
        "h2h": [{home_team, away_team, score_h, score_a, date, league}, ...],
        "recent_home": [{opponent, score_h, score_a, date, league, result}, ...],
        "recent_away": [{opponent, score_h, score_a, date, league, result}, ...],
        "injuries_home": [{player, reason, status, return_date}, ...],
        "injuries_away": [{player, reason, status, return_date}, ...],
        "h2h_summary": str,  # 交战历史摘要
        "summary": str,       # 简介
      }
    """
    url = f"{BASE_URL}/game/{game_id}"
    try:
        r = SESSION.get(url, timeout=15)
        if r.status_code != 200:
            logger.warning(f"qiumiwu preview {game_id}: HTTP {r.status_code}")
            return None
    except Exception as e:
        logger.warning(f"qiumiwu preview {game_id}: {e}")
        return None

    html = r.text
    result = {"game_id": game_id}

    # --- 从 meta description 提取队名/排名/联赛/时间 ---
    desc_m = re.search(
        r'<meta\s+content="([^"]+)"\s+name="description"',
        html
    )
    if desc_m:
        desc = desc_m.group(1)
        result["summary"] = desc

        # 日期时间: 2026年08月14日00:15
        dt_m = re.search(r'(\d{4})年(\d{2})月(\d{2})日(\d{2}:\d{2})', desc)
        if dt_m:
            result["match_date"] = f"{dt_m.group(1)}-{dt_m.group(2)}-{dt_m.group(3)}"
            result["match_time"] = dt_m.group(4)

        # 联赛: 【沙特联联赛】
        lg_m = re.search(r'【([^】]+)】', desc)
        if lg_m:
            result["league"] = lg_m.group(1).replace("联赛", "")

        # 队名 + 排名: 艾卜哈排名沙特甲1，哈森姆排名沙特联9
        rank_m = re.search(r'赛前(\S+?)排名(\S+?)，(\S+?)排名(\S+?)[。，]', desc)
        if rank_m:
            result["home"] = rank_m.group(1)
            result["home_rank"] = rank_m.group(2)
            result["away"] = rank_m.group(3)
            result["away_rank"] = rank_m.group(4)
        else:
            # 备选: 从 title 提取
            vs_m = re.search(r'(\S{2,12})vs(\S{2,12})', desc)
            if vs_m:
                result["home"] = vs_m.group(1)
                result["away"] = vs_m.group(2)

    # --- 交战历史 ---
    result["h2h"] = []
    h2h_start = html.find('class="game__h2h__list"')
    if h2h_start > 0:
        h2h_html = html[h2h_start:h2h_start + 5000]
        # 每场比赛: 两个 team span + 两个 score span + extra span
        for block in re.finditer(
            r'<span>([^<]{2,15})</span>\s*</div>\s*<div[^>]*>\s*<span[^>]*>(\d+)</span>\s*<span[^>]*>(\d+)</span>'
            r'\s*</div>\s*<div[^>]*>\s*<img[^>]*>\s*<span>([^<]{2,15})</span>'
            r'\s*</div>\s*<div[^>]*>\s*<span>(\d{4}-\d{2}-\d{2})</span>\s*<span>([^<]{2,20})</span>',
            h2h_html
        ):
            result["h2h"].append({
                "away_team": block.group(1),
                "score_h": int(block.group(2)),
                "score_a": int(block.group(3)),
                "home_team": block.group(4),
                "date": block.group(5),
                "league": block.group(6),
            })

    # H2H 摘要
    h2h_summary_start = html.find("交战历史")
    if h2h_summary_start > 0:
        h2h_summ = html[h2h_summary_start:h2h_summary_start + 300]
        result["h2h_summary"] = re.sub(r'<[^>]+>', '', h2h_summ).strip()

    # --- 最近战绩 ---
    home_name = result.get("home", "")
    away_name = result.get("away", "")
    for side_label, key in [(home_name, "recent_home"), (away_name, "recent_away")]:
        # 动态定位: 根据主客队名找到对应的最近战绩区块
        # 先找最近战绩 section
        recent_start = html.find("最近战绩")
        if recent_start < 0:
            result[key] = []
            continue

        recent_html = html[recent_start:recent_start + 10000]

        # 在该 section 中找到对应球队的区块
        search_name = home_name if key == "recent_home" else away_name
        team_pos = recent_html.find(f'<span>{side_label}</span>')
        if team_pos < 0:
            # 尝试用 result 中的队名
            if search_name:
                team_pos = recent_html.find(f'<span>{search_name}</span>')

        if team_pos < 0:
            result[key] = []
            continue

        # 找到该球队区块中的所有比赛
        team_block = recent_html[team_pos:team_pos + 6000]

        matches = []
        for m in re.finditer(
            r'<span>(\d{4}-\d{2}-\d{2})</span>\s*<span>([^<]{2,20})</span>'
            r'\s*</div>\s*<span[^>]*>([^<]{2,15})</span>'
            r'\s*<div[^>]*>\s*<span[^>]*>\s*(\d+)\s*</span>\s*<span>\s*-\s*</span>\s*<span[^>]*>\s*(\d+)\s*</span>'
            r'\s*</div>\s*<span[^>]*>([^<]{2,15})</span>',
            team_block
        ):
            date_str = m.group(1)
            league = m.group(2)
            team1 = m.group(3).strip()
            score1 = int(m.group(4))
            score2 = int(m.group(5))
            team2 = m.group(6).strip()

            # 确定主客关系和结果
            result_val = "draw"
            if team1 == side_label or team1 == search_name:
                # team1 是目标队
                matches.append({
                    "date": date_str, "league": league,
                    "opponent": team2,
                    "score_h": score1, "score_a": score2,
                    "result": "win" if score1 > score2 else ("draw" if score1 == score2 else "lose"),
                })
            else:
                # team2 是目标队
                matches.append({
                    "date": date_str, "league": league,
                    "opponent": team1,
                    "score_h": score2, "score_a": score1,
                    "result": "win" if score2 > score1 else ("draw" if score2 == score1 else "lose"),
                })

        result[key] = matches[:10]

    # 如果上述动态匹配失败，回退到按顺序解析
    if not result.get("recent_home") and not result.get("recent_away"):
        recent_start = html.find("最近战绩")
        if recent_start > 0:
            recent_html = html[recent_start:recent_start + 10000]
            all_matches = list(re.finditer(
                r'<span>(\d{4}-\d{2}-\d{2})</span>\s*<span>([^<]{2,20})</span>'
                r'\s*</div>\s*<span[^>]*>([^<]{2,15})</span>'
                r'\s*<div[^>]*>\s*<span[^>]*>\s*(\d+)\s*</span>\s*<span>\s*-\s*</span>\s*<span[^>]*>\s*(\d+)\s*</span>'
                r'\s*</div>\s*<span[^>]*>([^<]{2,15})</span>',
                recent_html
            ))
            mid = len(all_matches) // 2
            for i, m in enumerate(all_matches):
                entry = {
                    "date": m.group(1), "league": m.group(2),
                    "opponent": m.group(6).strip(),
                    "score_h": int(m.group(4)), "score_a": int(m.group(5)),
                    "result": "win" if int(m.group(4)) > int(m.group(5)) else (
                        "draw" if int(m.group(4)) == int(m.group(5)) else "lose"),
                }
                if i < mid:
                    result.setdefault("recent_home", []).append(entry)
                else:
                    result.setdefault("recent_away", []).append(entry)

    # --- 伤停 ---
    result["injuries_home"] = []
    result["injuries_away"] = []
    injury_start = html.find("伤停球员")
    if injury_start > 0:
        injury_html = html[injury_start:injury_start + 3000]
        # 找两个 .game__injury__item 区块
        injury_blocks = list(re.finditer(
            r'<span>([^<]{2,15})</span>\s*</div>\s*<span>原因</span>\s*<span>状态</span>\s*<span>时间</span>'
            r'\s*</div>\s*(.*?)(?:</div>\s*</div>|$)',
            injury_html, re.DOTALL
        ))
        for bi, block in enumerate(injury_blocks):
            team_name = block.group(1)
            block_content = block.group(2)
            key = "injuries_home" if bi == 0 else "injuries_away"

            if "暂无数据" in block_content:
                continue

            # 解析伤停条目: 球员名 / 原因 / 状态 / 时间
            inj_items = re.findall(
                r'<span>([^<]{2,10})</span>\s*<span>([^<]{2,20})</span>\s*<span>([^<]{2,10})</span>\s*<span>([^<]{2,10})</span>',
                block_content
            )
            for inj in inj_items:
                result[key].append({
                    "player": inj[0], "reason": inj[1],
                    "status": inj[2], "return_date": inj[3],
                })

    return result


# ============================================================
# C. 球队赛季数据 (35+ 指标)
# ============================================================

def fetch_team_stats(team_slug: str) -> Optional[dict]:
    """获取球队赛季统计数据 (SSR)

    返回: {metric_name: {"value": float, "rank": int}, ...}
    指标包括: 进球, 失球, 净胜球, 角球, 场均进球, 场均失球, 场均角球,
             射门, 射正, 助攻, 传球, 点球, 犯规, 红牌, 黄牌, 控球率,
             解围, 抢断, 拦截, 有效阻挡, 越位, 被侵犯, 传球成功,
             关键传球, 传中球, 长传, 任意球, 过人, 1对1拼抢, 快攻, 丢失球权
    """
    url = f"{BASE_URL}/team/{team_slug}/stat"
    try:
        r = SESSION.get(url, timeout=15)
        if r.status_code != 200:
            return None
    except Exception:
        return None

    html = r.text
    result = {}

    # 解析指标: 格式 "5 进球" / "联赛第 1"
    for m in re.finditer(
        r'(\d+(?:\.\d+)?%?)\s+([^\s<]{2,8})\s*\n\s*\[?\s*联赛第\s*(\d+)',
        html
    ):
        value = m.group(1)
        metric = m.group(2)
        rank = int(m.group(3))
        # 解析数值
        try:
            if value.endswith('%'):
                num_val = float(value[:-1]) / 100
            else:
                num_val = float(value)
        except ValueError:
            num_val = 0
        result[metric] = {"value": num_val, "rank": rank}

    return result


# ============================================================
# D. 联赛积分榜
# ============================================================

def fetch_standings(league_slug: str) -> Optional[dict]:
    """获取联赛积分榜 (SSR)

    返回: {
      "total": [{team, rank, pts, played, w, d, l, gf, ga, gd, ...}, ...],
      "home": [...],
      "away": [...],
    }
    """
    url = f"{BASE_URL}/league/{league_slug}/standings"
    try:
        r = SESSION.get(url, timeout=15)
        if r.status_code != 200:
            return None
    except Exception:
        return None

    html = r.text
    result = {"total": [], "home": [], "away": []}

    # 解析积分榜 (总榜优先)
    # 格式: 排名 球队名 场次 积分 胜/平/负 进球 失球 净胜 ...
    table_start = 0
    for tab in ["total", "home", "away"]:
        if tab == "total":
            section = html
        elif tab == "home":
            sec = html.find("主场", table_start)
            section = html[sec:] if sec > 0 else ""
        else:
            sec = html.find("客场", table_start)
            section = html[sec:] if sec > 0 else ""

        rows = re.findall(
            r'(\d+)\s*\n.*?([^\s<]{2,15}).*?\n\s*(\d+)\s+(\d+)\s+(\d+)/(\d+)/(\d+)\s+(\d+)\s+(\d+)\s+([+-]?\d+)',
            section[:5000] if section else ""
        )
        for row in rows[:20]:
            result[tab].append({
                "rank": int(row[0]), "team": row[1],
                "played": int(row[2]), "pts": int(row[3]),
                "w": int(row[4]), "d": int(row[5]), "l": int(row[6]),
                "gf": int(row[7]), "ga": int(row[8]), "gd": int(row[9]),
            })
        if rows:
            table_start = section.find(rows[-1][1], section.find(rows[0][1])) + 100

    return result


# ============================================================
# E. 主入口: 匹配 sporttery 场次到 qiumiwu 并获取前瞻数据
# ============================================================

def match_and_fetch(sp_matches: dict, schedule_html: str = None,
                    force_refresh: bool = False) -> dict:
    """匹配 sporttery 场次到 qiumiwu 并获取前瞻数据

    Args:
      sp_matches: {match_key: {home, away, league, match_time, match_date, ...}}
      schedule_html: WebFetch 渲染的赛程 HTML (可选, 不传则自动从 game 页面获取)
      force_refresh: 强制刷新缓存

    Returns:
      {match_key: {preview_data, team_stats_home, team_stats_away, standings}}
    """
    # 1. 获取赛程列表
    if schedule_html:
        matches = parse_schedule_html(schedule_html)
        save_schedule_cache(matches)
    elif force_refresh:
        matches = fetch_schedule_from_game_page(force_refresh=True)
    else:
        matches = load_schedule_cache()
        if not matches:
            # 尝试从 game 页面获取
            matches = fetch_schedule_from_game_page()

    if not matches:
        logger.warning("qiumiwu: 无法获取赛程数据")
        return {}

    # 2. 构建 qiumiwu 指纹
    qm_fps = []
    for m in matches:
        qm_fps.append(MatchFingerprint(
            home=m["home"], away=m["away"],
            league=m["league"], match_time=m["time"],
            match_date=m["date"],
        ))

    # 3. 匹配 sporttery 场次 (Ultra 13.6: 匹配+自动学习别名)
    # 同联赛+同时刻+同日期多场 → 上下文歧义, 禁用上下文学习防误学
    from collections import Counter
    _tk = Counter((mi.get('league', ''), (mi.get('match_time', '') or ''), (mi.get('match_date', '') or ''))
                  for mi in sp_matches.values())
    result = {}
    for key, mi in sp_matches.items():
        sp_fp = MatchFingerprint.from_sporttery(mi)
        ambiguous = _tk[(mi.get('league', ''), (mi.get('match_time', '') or ''), (mi.get('match_date', '') or ''))] > 1
        best_idx, score, learned = find_best_match_with_learning(
            sp_fp, qm_fps, source='qiumiwu', threshold=0.55,
            allow_context=not ambiguous)
        if best_idx is not None:
            # Ultra 14.1: 队名信号校验 (与 swot_auto 对齐) — 上下文匹配(联赛+时间+日期)
            # 队名零证据时拒绝, 防止错配 game_id 导致前瞻数据污染 λ 链
            name_score = match_score(sp_fp, qm_fps[best_idx],
                                     weights={"team_name": 1.0, "league": 0.0, "time": 0.0, "date": 0.0})
            if name_score < 0.4 and not learned:
                logger.info(f"  qiumiwu 拒绝低队名分匹配: {key} (score={score:.2f}, name={name_score:.2f})")
                continue
            qm = matches[best_idx]
            gid = qm["game_id"]
            if learned:
                _pairs = ', '.join(f'{a}→{b}' for a, b, _ in learned)
                logger.info(f"  qiumiwu 学习别名: {_pairs}")
            logger.info(f"  qiumiwu 匹配: {key} → {gid} (score={score:.2f})")

            # 4. 获取前瞻数据
            preview = fetch_match_preview(gid)
            if preview:
                result[key] = {"preview": preview, "game_id": gid}

                # 5. 可选: 获取球队数据和积分榜 (通过 team slug)
                # team slug 可从比赛页面的链接中提取
                # 暂时跳过, 按需启用

    return result


# ============================================================
# F. 预测增强函数 (Ultra 6.12 — qiumiwu 数据 → 概率模型)
# ============================================================

def parse_rank_number(rank_str: str) -> tuple[int | None, int | None]:
    """从排名字符串中提取数字排名

    支持格式: "苏超9", "沙特甲1", "13", "波兰甲6"
    返回: (rank, total_teams) 或 (None, None)
    """
    if not rank_str:
        return None, None

    # 格式: "联赛名数字" 如 "苏超9"
    m = re.search(r'([^\d]*?)(\d+)$', str(rank_str).strip())
    if m:
        return int(m.group(2)), None

    # 纯数字
    try:
        return int(rank_str), None
    except (ValueError, TypeError):
        return None, None


# Ultra 13.2 (排名阶梯缩放 2026-08-14): 联赛排名影响随轮次阶梯放大
# 赛季初(第1轮)排名≈队名排序, 不代表真实实力; 随联赛进程逐步收敛到真实实力
# 每5轮调一次: 1-5轮 0.05 → 6-10轮 0.25 → 11-15轮 0.50 → 16-20轮 0.70 → 21-25轮 0.85 → 26+轮 1.00
# Ultra 13.10 (P0-2 2026-08-16 冒烟修正):
#   ① 轮次<6 或轮次未知(0) → 乘子完全禁用(0.0)。旧版轮次未知用满权重1.0是缺陷:
#      260816 葡超第1-2轮场次轮次解析失败时, 排名差把 λ_h 压到 0.30 下限、λ_a 顶到 2.5+,
#      直接造成 026法马利康/027布拉加 模型方向与欧指主胜完全反向。
#   ② 乘子边界收紧 [0.5, 2.0]: RANK_PENALTY_FLOOR 0.30→0.50, 加成上限 3.0→2.0
ROUND_SCALE_STEPS = [0.00, 0.25, 0.50, 0.70, 0.85, 1.00, 1.00]  # 索引=(轮-1)//5: 1-5轮禁用, 6-10轮0.25, 26+轮1.00
ROUND_STEP_SIZE = 5          # 每5轮上调一档
RANK_MIN_ROUNDS = 6          # 最少轮次门槛: 联赛<6轮排名≈队名字典序, 无信息量
RANK_MAX_IMPACT = 1.0        # 满权重最大影响: λ_h ×(1+1.0)=×2.0 (收紧自2.0/×3.0)
RANK_PENALTY_FLOOR = 0.50    # 弱队λ下限 0.50 (收紧自0.30, 防过度惩罚)
RANK_MULT_FLOOR = 0.50       # 乘子硬下限 (P0-2)
RANK_MULT_CAP = 2.00         # 乘子硬上限 (P0-2)


def extract_round_num(league_str: str) -> int:
    """从联赛名提取轮次, 如 '沙特联第1轮 联赛' → 1, 失败返回0"""
    if not league_str:
        return 0
    m = re.search(r'第(\d+)轮', str(league_str))
    return int(m.group(1)) if m else 0


def round_scale_factor(round_num: int) -> float:
    """轮次缩放系数: 排名权重随轮次阶梯放大 (每5轮调一次)

    第1-5轮   → 0.00 (P0-2: 排名≈队名字典序, 完全禁用; 旧版0.05仍引入噪声)
    第6-10轮  → 0.25
    第11-15轮 → 0.50
    第16-20轮 → 0.70
    第21-25轮 → 0.85
    第26轮+   → 1.00 (满权重, 漫长联赛30+轮排名已稳定)
    无轮次信息(round_num=0) → 0.00 (P0-2: 旧版1.0满权重是缺陷, 排名来源轮次不明
      时无法判断信息量, 宁可禁用也不用满权重压λ)
    """
    if round_num <= 0:
        return 0.0
    step = (round_num - 1) // ROUND_STEP_SIZE
    return ROUND_SCALE_STEPS[min(step, len(ROUND_SCALE_STEPS) - 1)]


def compute_rank_boost(home_rank_str: str, away_rank_str: str,
                       league_size: int = 16, round_num: int = 0) -> dict:
    """改进1: 排名差 → λ 调整因子

    排名差越大，实力差距越显著。使用 tanh 平滑函数避免极端值。

    Ultra 13.2 (排名阶梯缩放): 排名差影响乘以轮次缩放系数。
    赛季初排名≈队名排序, 随联赛进程每5轮阶梯放大至满权重。
    满权重最大加成 λ_h ×3.0 (顶级球队赛季末获得充分加成)。

    返回:
      {
        "home_rank": int, "away_rank": int,
        "rank_diff": int,  # 正=主队排名更高(实力更强)
        "boost_factor": float,  # λ_h 乘数
        "penalty_factor": float, # λ_a 乘数
        "round_num": int,  # 联赛轮次
        "round_scale": float,  # 轮次缩放系数
        "note": str,
      }
    """
    h_rank, _ = parse_rank_number(home_rank_str)
    a_rank, _ = parse_rank_number(away_rank_str)

    result = {
        "home_rank": h_rank, "away_rank": a_rank,
        "rank_diff": 0, "boost_factor": 1.0, "penalty_factor": 1.0,
        "round_num": round_num, "round_scale": 1.0,
        "note": "",
    }

    if h_rank is None or a_rank is None:
        return result

    # 排名差: 正数=主队排名更高(数字更小, 实力更强)
    rank_diff = a_rank - h_rank
    result["rank_diff"] = rank_diff

    if abs(rank_diff) < 2:
        return result

    # Ultra 13.1: 轮次缩放系数 (赛季初排名≈队名排序, 随轮次线性放大)
    r_scale = round_scale_factor(round_num)
    result["round_scale"] = r_scale

    # Ultra 13.2: 排名影响提升至满权重λ_h×3.0 (顶级球队赛季末应获充分加成)
    # tanh 平滑: 排名差 2→±37%, 5→±86%, 10→±135%, 15→±170% (×轮次阶梯缩放)
    norm_diff = rank_diff / league_size
    impact = math.tanh(abs(norm_diff) * 3) * RANK_MAX_IMPACT * r_scale

    # Ultra 13.10 (P0-2): r_scale=0 (轮次<6/未知) 时乘子已恒为1.0, 提前返回
    if r_scale <= 0:
        result["note"] = f"排名差{rank_diff:+d}(主{h_rank}vs客{a_rank}) 第{round_num or '?'}轮 (<{RANK_MIN_ROUNDS}轮/轮次未知, 乘子禁用)"
        return result

    if rank_diff > 0:
        # 主队排名更高(实力更强)
        result["boost_factor"] = min(RANK_MULT_CAP, 1.0 + impact)
        result["penalty_factor"] = max(RANK_MULT_FLOOR, 1.0 - impact * 0.7)
        result["note"] = f"排名差+{rank_diff}(主{h_rank}vs客{a_rank}), λ_h×{result['boost_factor']:.3f} λ_a×{result['penalty_factor']:.3f}"
        if round_num > 0 and r_scale < 1.0:
            result["note"] += f" [第{round_num}轮×{r_scale:.2f}缩放, 边界{RANK_MULT_FLOOR}~{RANK_MULT_CAP}]"
    else:
        result["boost_factor"] = max(RANK_MULT_FLOOR, 1.0 - impact * 0.7)
        result["penalty_factor"] = min(RANK_MULT_CAP, 1.0 + impact)
        result["note"] = f"排名差{rank_diff}(主{h_rank}vs客{a_rank}), λ_h×{result['boost_factor']:.3f} λ_a×{result['penalty_factor']:.3f}"
        if round_num > 0 and r_scale < 1.0:
            result["note"] += f" [第{round_num}轮×{r_scale:.2f}缩放, 边界{RANK_MULT_FLOOR}~{RANK_MULT_CAP}]"

    return result


def compute_weighted_form_score(recent_matches: list[dict],
                                n_weighted: int = 10) -> dict:
    """改进2: 近况比分 → 加权状态分

    对比旧版 _compute_form_score (仅 W/D/L, 无衰减):
      - 加入比分差 (净胜球)
      - 指数衰减权重 (0.85^场次)
      - 区分"1-0险胜"和"3-0大胜"

    返回:
      {
        "form_score": float,        # 加权状态分 (0~3+)
        "weighted_goals_for": float,  # 加权场均进球
        "weighted_goals_against": float, # 加权场均失球
        "recent_trend": float,      # 趋势分 (-1~+1, 正=上升)
        "form_str": str,            # W/D/L 字符串
      }
    """
    if not recent_matches:
        return {"form_score": 1.5, "weighted_goals_for": 1.0,
                "weighted_goals_against": 1.0, "recent_trend": 0.0, "form_str": ""}

    decay = 0.85
    total_weight = 0.0
    weighted_points = 0.0
    weighted_gf = 0.0
    weighted_ga = 0.0
    trend_scores = []

    form_chars = []
    for i, m in enumerate(recent_matches[:n_weighted]):
        w = decay ** i
        total_weight += w

        score_h = m.get("score_h", 0)
        score_a = m.get("score_a", 0)
        result = m.get("result", "draw")

        # 基础分: W=3, D=1, L=0
        if result == "win":
            base_points = 3.0
        elif result == "draw":
            base_points = 1.0
        else:
            base_points = 0.0

        # 比分加成: 净胜球/3 (大胜加分, 惨败扣分)
        goal_diff = (score_h - score_a) / 3.0
        match_score = base_points + goal_diff

        weighted_points += match_score * w
        weighted_gf += score_h * w
        weighted_ga += score_a * w

        # 趋势: 最近一半场次 vs 前一半场次
        if i < n_weighted // 2:
            trend_scores.append((match_score, w))
        else:
            trend_scores.append((match_score, -w))

        form_chars.append(result[0].upper() if result else "D")

    if total_weight > 0:
        form_score = weighted_points / total_weight
        avg_gf = weighted_gf / total_weight
        avg_ga = weighted_ga / total_weight
    else:
        form_score = 1.5
        avg_gf = 1.0
        avg_ga = 1.0

    # 趋势分: 近期 vs 远期
    if len(trend_scores) >= 4:
        mid = len(trend_scores) // 2
        recent_avg = sum(s[0] for s in trend_scores[:mid]) / max(mid, 1)
        older_avg = sum(s[0] for s in trend_scores[mid:]) / max(len(trend_scores) - mid, 1)
        recent_trend = math.tanh((recent_avg - older_avg) / 2.0)
    else:
        recent_trend = 0.0

    return {
        "form_score": round(form_score, 3),
        "weighted_goals_for": round(avg_gf, 3),
        "weighted_goals_against": round(avg_ga, 3),
        "recent_trend": round(recent_trend, 3),
        "form_str": "".join(form_chars),
    }


def compute_h2h_time_decay(h2h_matches: list[dict],
                           match_date: str = None) -> dict:
    """改进3: H2H 时间衰减权重

    对比旧版 (不加时间权重):
      - 3年前的交锋权重仅 0.9^3 ≈ 0.73
      - 3个月前的交锋权重约 0.97
      - 近期交锋自动获得更高权重

    返回:
      {
        "weighted_total": float,       # 加权总场次
        "weighted_home_wins": float,   # 加权主胜
        "weighted_draws": float,       # 加权平局
        "weighted_away_wins": float,   # 加权客胜
        "home_win_rate": float,        # 加权主胜率
        "raw_total": int,              # 原始总场次
        "note": str,
      }
    """
    if not h2h_matches:
        return {"weighted_total": 0, "weighted_home_wins": 0,
                "weighted_draws": 0, "weighted_away_wins": 0,
                "home_win_rate": 0.5, "raw_total": 0, "note": ""}

    from datetime import date as dt_date

    ref_date = None
    if match_date:
        try:
            ref_date = dt_date.fromisoformat(match_date[:10])
        except (ValueError, TypeError):
            pass
    if ref_date is None:
        ref_date = dt_date.today()

    year_decay = 0.85  # 每年衰减系数
    weighted_home_wins = 0.0
    weighted_draws = 0.0
    weighted_away_wins = 0.0
    weighted_total = 0.0

    for h in h2h_matches:
        h_date_str = h.get("date", "")
        w = 1.0
        if h_date_str:
            try:
                h_date = dt_date.fromisoformat(h_date_str[:10])
                years_ago = (ref_date - h_date).days / 365.25
                if years_ago > 0:
                    w = year_decay ** years_ago
            except (ValueError, TypeError):
                pass

        score_h = h.get("score_h", 0)
        score_a = h.get("score_a", 0)

        weighted_total += w
        if score_h > score_a:
            weighted_home_wins += w
        elif score_h == score_a:
            weighted_draws += w
        else:
            weighted_away_wins += w

    if weighted_total > 0:
        home_wr = weighted_home_wins / weighted_total
    else:
        home_wr = 0.5

    raw_total = len(h2h_matches)

    return {
        "weighted_total": round(weighted_total, 2),
        "weighted_home_wins": round(weighted_home_wins, 2),
        "weighted_draws": round(weighted_draws, 2),
        "weighted_away_wins": round(weighted_away_wins, 2),
        "home_win_rate": round(home_wr, 4),
        "raw_total": raw_total,
        "note": f"H2H(衰减): {weighted_home_wins:.1f}W-{weighted_draws:.1f}D-{weighted_away_wins:.1f}L(原始{raw_total}场)",
    }


def compute_injury_impact(injuries_home: list[dict],
                          injuries_away: list[dict]) -> dict:
    """改进4: 伤停量化 → λ 修正

    每名伤停球员对 λ 的影响:
      - 普通球员: -3% λ
      - 关键描述词(核心/主力/门将/队长): -5% λ
      - 上限: 最多 -15% (避免过度惩罚)

    返回:
      {
        "home_factor": float,   # λ_h 乘数
        "away_factor": float,   # λ_a 乘数
        "home_count": int,
        "away_count": int,
        "note": str,
      }
    """
    KEYWORDS = ["核心", "主力", "门将", "队长", "射手", "王牌", "大腿"]

    def count_impact(injuries):
        if not injuries:
            return 0, 0.0
        count = 0
        impact = 0.0
        for inj in injuries:
            reason = inj.get("reason", "")
            is_key = any(kw in reason for kw in KEYWORDS)
            impact += 0.05 if is_key else 0.03
            count += 1
        return count, min(impact, 0.15)  # 上限 15%

    h_count, h_impact = count_impact(injuries_home)
    a_count, a_impact = count_impact(injuries_away)

    note_parts = []
    if h_impact > 0:
        note_parts.append(f"主队{h_count}人伤停(λ_h×{1 - h_impact:.2f})")
    if a_impact > 0:
        note_parts.append(f"客队{a_count}人伤停(λ_a×{1 - a_impact:.2f})")

    return {
        "home_factor": round(1.0 - h_impact, 3),
        "away_factor": round(1.0 - a_impact, 3),
        "home_count": h_count,
        "away_count": a_count,
        "note": "; ".join(note_parts) if note_parts else "",
    }


def compute_ppg_strength(standings: dict, home_team: str,
                         away_team: str) -> dict:
    """改进5: 积分榜 PPG → 主客场实力分离

    从积分榜数据中提取主队主场PPG和客队客场PPG,
    计算相对于联赛平均的强度因子。

    返回:
      {
        "home_home_ppg": float,      # 主队主场场均积分
        "away_away_ppg": float,      # 客队客场场均积分
        "league_avg_home_ppg": float, # 联赛平均主场PPG
        "league_avg_away_ppg": float, # 联赛平均客场PPG
        "home_strength": float,       # 主队主场强度 (>1强, <1弱)
        "away_strength": float,       # 客队客场强度
        "strength_ratio": float,      # 主/客强度比
        "note": str,
      }
    """
    default = {
        "home_home_ppg": 1.5, "away_away_ppg": 1.2,
        "league_avg_home_ppg": 1.6, "league_avg_away_ppg": 1.1,
        "home_strength": 1.0, "away_strength": 1.0,
        "strength_ratio": 1.0, "note": "",
    }

    if not standings:
        return default

    home_table = standings.get("home", [])
    away_table = standings.get("away", [])
    total_table = standings.get("total", [])

    if not home_table or not away_table:
        return default

    # 计算联赛平均
    def calc_avg_ppg(table):
        if not table:
            return 1.5
        total_pts = sum(row.get("pts", 0) for row in table)
        total_played = sum(row.get("played", 0) for row in table)
        return total_pts / max(total_played, 1)

    league_avg_home_ppg = calc_avg_ppg(home_table)
    league_avg_away_ppg = calc_avg_ppg(away_table)

    # 查找球队
    def find_team_ppg(table, team_name):
        for row in table:
            if row.get("team", "") == team_name:
                played = row.get("played", 0)
                pts = row.get("pts", 0)
                return pts / max(played, 1) if played > 0 else None
        return None

    home_home_ppg = find_team_ppg(home_table, home_team)
    away_away_ppg = find_team_ppg(away_table, away_team)

    if home_home_ppg is None or away_away_ppg is None:
        return default

    # 强度因子
    home_strength = home_home_ppg / max(league_avg_home_ppg, 0.5)
    away_strength = away_away_ppg / max(league_avg_away_ppg, 0.5)
    strength_ratio = home_strength / max(away_strength, 0.3)

    note = (f"PPG: 主主场{home_home_ppg:.1f}(联赛均{league_avg_home_ppg:.1f}) "
            f"vs 客客场{away_away_ppg:.1f}(联赛均{league_avg_away_ppg:.1f}), "
            f"强度比{strength_ratio:.2f}")

    return {
        "home_home_ppg": round(home_home_ppg, 2),
        "away_away_ppg": round(away_away_ppg, 2),
        "league_avg_home_ppg": round(league_avg_home_ppg, 2),
        "league_avg_away_ppg": round(league_avg_away_ppg, 2),
        "home_strength": round(home_strength, 3),
        "away_strength": round(away_strength, 3),
        "strength_ratio": round(strength_ratio, 3),
        "note": note,
    }


def apply_qiumiwu_enhancements(qiumiwu_stats: dict,
                               qiumiwu_preview: dict = None) -> dict:
    """一键应用所有 qiumiwu 增强到 λ 修正因子

    调用所有 5 项改进, 返回统一的修正因子字典。

    Args:
      qiumiwu_stats: parse_preview_to_stats() 的输出
      qiumiwu_preview: 原始 preview 数据 (含完整 H2H/近况/伤停)

    Returns:
      {
        "rank_boost": dict,       # 排名差修正
        "form_enhanced": dict,    # 加权状态分
        "h2h_decayed": dict,      # H2H 时间衰减
        "injury_impact": dict,    # 伤停量化
        "ppg_strength": dict,     # PPG 强度
        "combined_lam_factor": float,  # 综合 λ 修正因子
        "notes": [str],           # 所有修正说明
      }
    """
    if not qiumiwu_stats:
        return {"combined_lam_factor": 1.0, "notes": []}

    notes = []

    # 1. 排名差 (Ultra 13.1: 轮次线性缩放 — 赛季初排名≈队名排序, 随轮次放大)
    round_num = extract_round_num((qiumiwu_preview or {}).get("league", ""))
    rank_boost = compute_rank_boost(
        qiumiwu_stats.get("home_rank", ""),
        qiumiwu_stats.get("away_rank", ""),
        round_num=round_num,
    )
    if rank_boost["note"]:
        notes.append(rank_boost["note"])

    # 2. 加权状态分 (从 preview 获取完整近况)
    preview = qiumiwu_preview or {}
    home_recent = preview.get("recent_home", [])
    away_recent = preview.get("recent_away", [])
    form_enhanced = {
        "home": compute_weighted_form_score(home_recent),
        "away": compute_weighted_form_score(away_recent),
    }
    hf = form_enhanced["home"]
    af = form_enhanced["away"]
    if hf["form_str"]:
        notes.append(
            f"加权状态: 主{hf['form_score']:.2f}({hf['form_str']}) "
            f"vs 客{af['form_score']:.2f}({af['form_str']}), "
            f"趋势主{hf['recent_trend']:+.2f}/客{af['recent_trend']:+.2f}"
        )

    # 3. H2H 时间衰减
    h2h_matches = preview.get("h2h", [])
    h2h_decayed = compute_h2h_time_decay(h2h_matches)
    if h2h_decayed["note"] and h2h_decayed["raw_total"] > 0:
        notes.append(h2h_decayed["note"])

    # 4. 伤停量化
    injuries_home = preview.get("injuries_home", [])
    injuries_away = preview.get("injuries_away", [])
    injury_impact = compute_injury_impact(injuries_home, injuries_away)
    if injury_impact["note"]:
        notes.append(injury_impact["note"])

    # 5. PPG 强度 (需要 standings 数据, 暂不在此计算)
    ppg_strength = {}

    # 综合 λ 修正因子
    lam_factor = 1.0
    # 排名差: 主队因子
    lam_factor *= rank_boost["boost_factor"]
    # 伤停: 主队因子
    lam_factor *= injury_impact["home_factor"]

    return {
        "rank_boost": rank_boost,
        "form_enhanced": form_enhanced,
        "h2h_decayed": h2h_decayed,
        "injury_impact": injury_impact,
        "ppg_strength": ppg_strength,
        "combined_lam_factor": round(lam_factor, 4),
        "notes": notes,
    }


# ============================================================
# G. 便捷函数
# ============================================================

def get_schedule_html_via_webfetch() -> Optional[str]:
    """获取赛程页 HTML (需在 WebFetch 环境中调用)

    使用方式: 在能调用 WebFetch 的环境中, 获取后传入 match_and_fetch()
    """
    return None  # 由外部调用者传入


def parse_preview_to_stats(preview: dict) -> dict:
    """将 qiumiwu 前瞻数据转换为 nowscore 兼容格式

    使现有的 Phase 2 统计消费代码无需修改即可使用 qiumiwu 数据。
    """
    if not preview:
        return {}

    result = {"source": "qiumiwu"}

    # 近况 (最近10场)
    recent_home = preview.get("recent_home", [])
    recent_away = preview.get("recent_away", [])
    if recent_home:
        result["home_recent_form"] = "".join(
            r.get("result", "D")[0].upper() for r in recent_home[:6]
        )
        result["home_recent_goals_for"] = sum(r.get("score_h", 0) for r in recent_home[:10])
        result["home_recent_goals_against"] = sum(r.get("score_a", 0) for r in recent_home[:10])
    if recent_away:
        result["away_recent_form"] = "".join(
            r.get("result", "D")[0].upper() for r in recent_away[:6]
        )
        result["away_recent_goals_for"] = sum(r.get("score_h", 0) for r in recent_away[:10])
        result["away_recent_goals_against"] = sum(r.get("score_a", 0) for r in recent_away[:10])

    # 交战历史
    h2h = preview.get("h2h", [])
    if h2h:
        result["h2h_matches"] = len(h2h)
        result["h2h_home_wins"] = sum(1 for h in h2h if h.get("score_h", 0) > h.get("score_a", 0))
        result["h2h_draws"] = sum(1 for h in h2h if h.get("score_h", 0) == h.get("score_a", 0))
        result["h2h_away_wins"] = sum(1 for h in h2h if h.get("score_h", 0) < h.get("score_a", 0))

    # 伤停
    result["injuries_home"] = len(preview.get("injuries_home", []))
    result["injuries_away"] = len(preview.get("injuries_away", []))

    # 排名
    result["home_rank"] = preview.get("home_rank", "")
    result["away_rank"] = preview.get("away_rank", "")

    return result