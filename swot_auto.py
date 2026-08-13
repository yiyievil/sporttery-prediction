#!/usr/bin/env python3
"""SWOT 全自动获取模块 (Ultra 6.5)

主流程: leisu.com 情报指南 (定性SWOT: 有利/不利情报)
  1. discover_leisu_guides(): 从 leisu.com/guide 列表页发现当日SWOT卡片
     (队名/时间/联赛/swot-ID), 纯requests + jsdom WAF求解, 无需人工提供URL
  2. 按多信号融合匹配到sporttery场次 (队名+时间+联赛, 复用 match_utils)
  3. 批量获取SWOT页并解析 (复用swot_fast_v3.parse_swot_from_html, cookie复用)

备用: 当leisu无该场卡片或解析为空时, 用已获取的500/nowscore统计数据
     生成"数据型情报" (近况/交锋/积分), 标记 source='stats', 保证每场都有SWOT输入

输出: predictions/swot_data_refreshed.json (swot_fusion_v3.py 的输入格式)

通用性: 全部纯requests + Node/jsdom(通用开源), 不依赖Kimi专属能力
"""

import os
import re
import json
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from leisu_session import leisu_get, HEADERS as LEISU_HEADERS
from match_utils import (
    MatchFingerprint, match_score, find_best_match, team_match_bool,
    LEAGUE_ALIASES,
)

_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
PREDICTIONS_DIR = os.path.join(_WORKSPACE, 'predictions')
SWOT_DATA_FILE = os.path.join(PREDICTIONS_DIR, 'swot_data_refreshed.json')

# 队名匹配: 复用nowscore_fetch的别名表与匹配逻辑
try:
    from swot_fast_v3 import parse_swot_from_html
except ImportError:
    parse_swot_from_html = None


# ============ leisu 发现与获取 ============

def discover_leisu_guides(session):
    """从leisu.com/guide发现当日SWOT卡片

    返回: [{gid, home, away, time, league, url}]
    """
    html, ok = leisu_get(session, 'https://www.leisu.com/guide')
    if not ok:
        print("  [SWOT] ⚠️ leisu /guide 获取失败, 无当日SWOT卡片 (页面结构可能变化, 请检查 https://www.leisu.com/guide)")
        return []

    cards = {}
    for m in re.finditer(r'/guide/swot-(\d+)', html):
        gid = m.group(1)
        if gid in cards:
            continue
        ctx = html[max(0, m.start() - 1500):m.start() + 1500]
        names = re.findall(r'class="match-(?:home|away)[^"]*"[^>]*>.*?>([^<>]{2,12})<', ctx, re.DOTALL)
        if len(names) < 2:
            continue
        t = re.search(r'(\d{2}:\d{2})', re.sub(r'<[^>]+>', ' ', ctx))
        league = re.search(r'comp-name[^>]*>([^<]+)<', ctx)
        cards[gid] = {
            'gid': gid,
            'home': names[0].strip(),
            'away': names[1].strip(),
            'time': t.group(1) if t else '',
            'league': league.group(1).strip() if league else '',
            'url': f'https://www.leisu.com/guide/swot-{gid}',
        }
    if not cards:
        print("  [SWOT] ⚠️ leisu /guide 解析不到SWOT卡片(0张), 页面结构可能已变化, 请检查 https://www.leisu.com/guide")
    return list(cards.values())


def _team_match(sp_name, leisu_name):
    """sporttery队名 vs leisu队名 模糊匹配 (委托 match_utils.team_match_bool)

    保留此函数以兼容旧调用路径, 实际逻辑已迁移到 match_utils。
    """
    return team_match_bool(sp_name, leisu_name)


def match_guides_to_sporttery(guides, matches):
    """将leisu卡片匹配到sporttery场次 (多信号融合: 队名+时间+联赛)

    匹配策略:
      1. 构建 sporttery 指纹列表 (MatchFingerprint)
      2. 对每张 leisu 卡片, 计算与所有 sporttery 场次的多信号评分
      3. 最高分 ≥ 0.55 且队名信号 ≥ 0.4 视为匹配

    返回: {match_key: guide}
    """
    result = {}

    # 构建 sporttery 指纹
    sp_fps = {}
    for key, mi in matches.items():
        sp_fps[key] = MatchFingerprint.from_sporttery(mi)

    # 构建 leisu 指纹
    lei_fps = [MatchFingerprint.from_leisu(g) for g in guides]

    for key, sp_fp in sp_fps.items():
        # 对每个 sporttery 场次, 找最佳 leisu 匹配
        best_idx, best_score = find_best_match(sp_fp, lei_fps, threshold=0.55)
        if best_idx is not None:
            # 额外验证: 队名信号必须 ≥ 0.4 (防止纯时间+联赛撞车)
            g = guides[best_idx]
            lei_fp = lei_fps[best_idx]
            name_score = match_score(sp_fp, lei_fp,
                                     weights={"team_name": 1.0, "league": 0.0, "time": 0.0, "date": 0.0})
            if name_score >= 0.4:
                result[key] = g
                if best_score < 0.8:  # 低于 0.8 的匹配打印评分供调试
                    print(f"  [SWOT] 🟡 {key} 匹配 {g['home']} vs {g['away']} "
                          f"(score={best_score:.2f}, name={name_score:.2f})")

    return result


def fetch_leisu_swot(session, guide, retries=2):
    """获取并解析单个leisu SWOT页 (带重试, 应对WAF间歇性挑战失败)

    WAF cookie 可能对个别并发请求失效 (acw_sc__v2 有时效/按页面绑定),
    解析为空或获取失败时重试 retries 次, 每次重新求解WAF以提高成功率.
    """
    if parse_swot_from_html is None:
        return None
    for attempt in range(retries + 1):
        try:
            html, ok = leisu_get(session, guide['url'])
            if not ok:
                time.sleep(0.5 * (attempt + 1))
                continue
            swot = parse_swot_from_html(html, guide['url'])
            n = (len(swot.get('home_strengths', [])) + len(swot.get('home_weaknesses', [])) +
                 len(swot.get('away_strengths', [])) + len(swot.get('away_weaknesses', [])))
            if n > 0:
                swot['source'] = 'leisu'
                return swot
            time.sleep(0.5 * (attempt + 1))
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return None


# ============ 备用: 统计数据型情报 (500/nowscore shuju) ============

def build_stats_swot(match_data):
    """用已获取的统计数据生成数据型情报 (leisu不可用时降级)

    输入: 比赛数据字典 (含shuju: form_home/form_away/h2h/stats_队名 等)
    返回: SWOT格式字典, source='stats'; 数据不足返回None
    """
    shuju = (match_data or {}).get('shuju') or {}
    home = match_data.get('home', '主队')
    away = match_data.get('away', '客队')

    hs, hw, as_, aw = [], [], [], []

    # 近况: form字符串 (W/D/L序列, 最后字符=最近一场)
    for team, form, s_list, w_list in ((home, shuju.get('form_home', ''), hs, hw),
                                       (away, shuju.get('form_away', ''), as_, aw)):
        if form and len(form) >= 5:
            recent = form[-6:]
            w = recent.upper().count('W')
            d = recent.upper().count('D')
            l = recent.upper().count('L')
            if w >= 4:
                s_list.append(f"近况出色: 近{len(recent)}场{w}胜{d}平{l}负")
            elif l >= 4:
                w_list.append(f"近况低迷: 近{len(recent)}场{w}胜{d}平{l}负")
            elif w <= 1 and l >= 2:
                w_list.append(f"近况不佳: 近{len(recent)}场仅{w}胜")
            else:
                s_list.append(f"近况: 近{len(recent)}场{w}胜{d}平{l}负")

    # 交锋: 支持字符串("主队近40次交锋 14胜7平19负")或dict
    h2h = shuju.get('h2h')
    if isinstance(h2h, str):
        m = re.search(r'(\d+)胜(\d+)平(\d+)负', h2h)
        h2h = {'home_wins': int(m.group(1)), 'draws': int(m.group(2)), 'away_wins': int(m.group(3))} if m else None
    if h2h:
        hw_, d_, aw_ = h2h.get('home_wins'), h2h.get('draws'), h2h.get('away_wins')
        if hw_ is not None and d_ is not None and aw_ is not None and (hw_ + d_ + aw_) >= 3:
            if hw_ >= aw_ + 2:
                hs.append(f"交锋占优: 历史{hw_}胜{d_}平{aw_}负")
            elif aw_ >= hw_ + 2:
                as_.append(f"交锋占优: 历史{aw_}胜{d_}平{hw_}负")

    # 积分榜: stats_<队名> 含排名/场均进失
    for team, s_list, w_list in ((home, hs, hw), (away, as_, aw)):
        st = shuju.get(f'stats_{team}') or {}
        rank = st.get('rank')
        avg_gf, avg_ga = st.get('avg_gf'), st.get('avg_ga')
        if rank:
            try:
                r = int(rank)
                if r <= 3:
                    s_list.append(f"联赛排名第{r}, 处于争冠集团")
                elif r >= 15:
                    w_list.append(f"联赛排名第{r}, 深陷降级区")
            except (ValueError, TypeError):
                pass
        if avg_gf is not None and avg_ga is not None:
            try:
                gf, ga = float(avg_gf), float(avg_ga)
                if gf >= 2.0:
                    s_list.append(f"进攻火力强: 场均进{gf:.1f}球")
                if ga >= 2.0:
                    w_list.append(f"防守漏洞大: 场均失{ga:.1f}球")
                elif ga <= 0.8:
                    s_list.append(f"防守稳固: 场均仅失{ga:.1f}球")
            except (ValueError, TypeError):
                pass

    if not (hs or hw or as_ or aw):
        return None

    return {
        'home_strengths': hs, 'home_weaknesses': hw,
        'away_strengths': as_, 'away_weaknesses': aw,
        'trend': shuju.get('trend') or {},
        'swot_url': '',
        'source': 'stats',
    }


# ============ 主入口 ============

def fetch_swot_auto(matches, all_data=None):
    """全自动SWOT获取: leisu为主, 统计数据为备

    参数:
        matches: {key: match_info} sporttery场次
        all_data: {key: 完整比赛数据(含shuju)} 用于stats备用; 缺省用matches
    返回:
        {key: swot_dict} 并写入 predictions/swot_data_refreshed.json (合并已有)
    """
    data_map = all_data or matches
    results = {}

    # 1. leisu 发现
    session = requests.Session()
    session.headers.update(LEISU_HEADERS)
    print("  [SWOT] leisu 发现情报卡片...")
    try:
        guides = discover_leisu_guides(session)
    except Exception as ex:
        print(f"  [SWOT] leisu 发现失败: {ex}")
        guides = []
    print(f"  [SWOT] leisu 当日卡片 {len(guides)} 张")

    # 2. 匹配
    matched = match_guides_to_sporttery(guides, matches)
    print(f"  [SWOT] 匹配到 {len(matched)}/{len(matches)} 场")

    # 3. 获取leisu SWOT (Ultra-Opt: 并行获取, 每线程独立session复制cookies)
    #    旧版串行: N场 × (fetch 3s + sleep 0.8s) ≈ 35s/9场 → 并行后 ≈ 8s/9场
    def _fetch_one_leisu_swot(key, guide):
        """线程安全: 创建独立session, 复用主session的WAF cookies"""
        try:
            t_session = requests.Session()
            t_session.headers.update(LEISU_HEADERS)
            for c in session.cookies:
                t_session.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
            return key, guide, fetch_leisu_swot(t_session, guide)
        except Exception:
            return key, guide, None

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_fetch_one_leisu_swot, k, g) for k, g in matched.items()]
        for fut in as_completed(futures):
            key, guide, swot = fut.result()
            if swot:
                results[key] = swot
                n = (len(swot['home_strengths']) + len(swot['home_weaknesses']) +
                     len(swot['away_strengths']) + len(swot['away_weaknesses']))
                print(f"  [SWOT] ✅ {key} leisu {n}条 ({guide['home']} vs {guide['away']})")
            else:
                print(f"  [SWOT] ⚠️ {key} leisu页解析为空, 转stats备用")

    # 4. stats 备用兜底 (每场都保证有SWOT输入)
    for key in matches:
        if key not in results:
            swot = build_stats_swot(data_map.get(key))
            if swot:
                results[key] = swot
                print(f"  [SWOT] 📊 {key} stats备用 ({len(swot['home_strengths'])+len(swot['home_weaknesses'])+len(swot['away_strengths'])+len(swot['away_weaknesses'])}条)")

    # 5. 合并写入 swot_data_refreshed.json (保留历史场次的SWOT)
    # 格式约定: {'refreshed_at', 'source', 'matches': {key: swot}} (swot_fusion_v3读matches键)
    os.makedirs(PREDICTIONS_DIR, exist_ok=True)
    wrapper = {}
    if os.path.exists(SWOT_DATA_FILE):
        try:
            with open(SWOT_DATA_FILE, 'r', encoding='utf-8') as f:
                wrapper = json.load(f)
        except Exception:
            wrapper = {}
    matches_map = wrapper.get('matches', {}) if isinstance(wrapper.get('matches'), dict) else {}
    # 兼容旧版顶层散写格式: 把顶层的场次key并入matches
    for k, v in wrapper.items():
        if k not in ('refreshed_at', 'source', 'matches') and isinstance(v, dict) and 'home_strengths' in v:
            matches_map.setdefault(k, v)
    # 🔒 人工情报 (source 以 manual 开头, 如首回合赛果修正) 优先级最高,
    # 不被 leisu/stats 自动获取覆盖 (锁定规则)
    for k, v in results.items():
        existing = matches_map.get(k)
        if isinstance(existing, dict) and str(existing.get('source', '')).startswith('manual'):
            print(f"  [SWOT] 🔒 {k} 保留人工情报({existing.get('source')}), 跳过自动覆盖")
            continue
        matches_map[k] = v
    out = {
        'refreshed_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'swot_auto (leisu为主/stats备用)',
        'matches': matches_map,
    }
    with open(SWOT_DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"  [SWOT] 已写入 {SWOT_DATA_FILE} (本次{len(results)}场, 累计{len(matches_map)}场)")

    return results


if __name__ == '__main__':
    # 独立测试: 用当前sporttery场次
    import v215_e2e as e
    m = e.fetch_sporttery_matches(e.MATCH_NUMBERS, e.TARGET_DATE)
    fetch_swot_auto(m)
