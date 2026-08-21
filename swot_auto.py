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
    MatchFingerprint, match_score, find_best_match, find_best_match_with_learning,
    team_match_bool, LEAGUE_ALIASES,
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

    # Ultra 13.6.1: 同联赛+同时刻的多场视为上下文歧义, 禁用上下文学习防误学
    # (如周六020吉达联合 vs 周六021利雅胜利, 同为沙职02:00)
    from collections import Counter
    _tk = Counter((mi.get('league', ''), (mi.get('match_time', '') or ''))
                  for mi in matches.values())

    for key, sp_fp in sp_fps.items():
        # 对每个 sporttery 场次, 找最佳 leisu 匹配 (Ultra 13.6: 匹配+自动学习别名)
        mi = matches.get(key, {})
        ambiguous = _tk[(mi.get('league', ''), (mi.get('match_time', '') or ''))] > 1
        best_idx, best_score, learned = find_best_match_with_learning(
            sp_fp, lei_fps, source='leisu', threshold=0.55,
            allow_context=not ambiguous)
        if best_idx is not None:
            # 额外验证: 队名信号必须 ≥ 0.4 (防止纯时间+联赛撞车);
            # 上下文强匹配学到新别名时(learned非空)则放宽
            g = guides[best_idx]
            lei_fp = lei_fps[best_idx]
            name_score = match_score(sp_fp, lei_fp,
                                     weights={"team_name": 1.0, "league": 0.0, "time": 0.0, "date": 0.0})
            if name_score >= 0.4 or learned:
                result[key] = g
                if learned:
                    _pairs = ', '.join(f'{a}→{b}' for a, b, _ in learned)
                    print(f"  [SWOT] 🧠 {key} 学习别名: {_pairs}")
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
    # Ultra 15.9: 小样本护栏 — 赛季初played<3时 场均进失/排名近乎随机
    # (260821实证: 马赛n=1"场均仅失0.0球"/敦刻尔克n=2"场均进3.5球"均为失真条目)
    for team, s_list, w_list in ((home, hs, hw), (away, as_, aw)):
        st = shuju.get(f'stats_{team}') or {}
        rank = st.get('rank')
        avg_gf, avg_ga = st.get('avg_gf'), st.get('avg_ga')
        try:
            played = int(st.get('played') or 0)
        except (ValueError, TypeError):
            played = 0
        small_sample = played < 3
        if rank:
            try:
                r = int(rank)
                if not small_sample:
                    if r <= 3:
                        s_list.append(f"联赛排名第{r}, 处于争冠集团")
                    elif r >= 15:
                        w_list.append(f"联赛排名第{r}, 深陷降级区")
            except (ValueError, TypeError):
                pass
        if avg_gf is not None and avg_ga is not None and not small_sample:
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


# ============ Ultra 15.9: xG量化情报 (方向2) + 多源合并 (方向1) ============

def build_xg_swot(match_data, home_xg=None, away_xg=None):
    """用Understat滚动xG数据生成量化情报条目 (Ultra 15.9, 2026-08-21 用户裁决)

    背景: 昨日回测证明SWOT强翻转是核心胜负手, 但情报单源leisu覆盖不稳。
    xG滚动数据(Understat本地库)是独立量化源 — λ建模已用但其对WDL的影响被
    贝叶斯收缩(k=10/30)+市场锚大幅稀释; 此处只对【明显差距】打中等权重条目,
    作为SWOT评分的补强信号, 与λ通道互补而非重复计分。

    触发门槛 (双方都有数据才可比, 且样本/质量达标):
      · 进攻占优: 场均xG差 ≥ 0.40 (如 2.14 vs 1.42)
      · 防守占优: 场均xGA差 ≥ 0.30
      · 压迫优势: PPDA差 ≥ 2.0 (压迫更激进方, 仅双方有PPDA时)
    质量门槛: 双方 n_games ≥ 4 (小样本不产生条目, 与λ的小样本降权同哲学)

    参数: home_xg/away_xg 可外部传入(避免重复查库), 缺省按队名查库
    返回: SWOT格式dict(source='xg')或None
    """
    if home_xg is None or away_xg is None:
        home = match_data.get('home', '')
        away = match_data.get('away', '')
        if not home or not away:
            return None
        try:
            # 惰性导入引擎 (swot_auto被v215_e2e调用, 顶层import会循环依赖;
            # 同进程时sys.modules已有该模块, import即时返回 — swot_fusion_v3同模式)
            import v215_e2e as engine
            md = match_data.get('match_date', '9999')
            lg = match_data.get('league', '')
            if home_xg is None:
                home_xg = engine.fetch_xg_rolling_stats(home, md, lg)
            if away_xg is None:
                away_xg = engine.fetch_xg_rolling_stats(away, md, lg)
        except Exception:
            return None
    if not home_xg or not away_xg:
        return None
    if home_xg.get('n_games', 0) < 4 or away_xg.get('n_games', 0) < 4:
        return None  # 小样本不产生条目

    hs, hw, as_, aw = [], [], [], []
    h_att, a_att = home_xg['avg_xg_for'], away_xg['avg_xg_for']
    h_def, a_def = home_xg['avg_xg_against'], away_xg['avg_xg_against']

    # 进攻对比 (阈值0.40: 联赛平均xG约1.3, 0.4差≈30%火力差, 明显但非极端)
    if h_att - a_att >= 0.40:
        hs.append(f"xG进攻占优: 场均xG {h_att:.2f} vs 对手{a_att:.2f} (近{home_xg['n_games']}场)")
    elif a_att - h_att >= 0.40:
        as_.append(f"xG进攻占优: 场均xG {a_att:.2f} vs 对手{h_att:.2f} (近{away_xg['n_games']}场)")

    # 防守对比 (阈值0.30: 失球端差距通常小于进攻端)
    if a_def - h_def >= 0.30:
        hs.append(f"xG防守占优: 场均失xG仅{h_def:.2f}, 对手失{a_def:.2f}")
    elif h_def - a_def >= 0.30:
        as_.append(f"xG防守占优: 场均失xG仅{a_def:.2f}, 对手失{h_def:.2f}")

    # 压迫对比 (PPDA越低越激进, 仅双方有数据)
    h_ppda, a_ppda = home_xg.get('avg_ppda'), away_xg.get('avg_ppda')
    if h_ppda and a_ppda:
        if a_ppda - h_ppda >= 2.0:
            hs.append(f"xG压迫占优: PPDA {h_ppda:.1f} vs 对手{a_ppda:.1f}, 高位逼抢更激进")
        elif h_ppda - a_ppda >= 2.0:
            as_.append(f"xG压迫占优: PPDA {a_ppda:.1f} vs 对手{h_ppda:.1f}, 高位逼抢更激进")

    if not (hs or hw or as_ or aw):
        return None
    return {
        'home_strengths': hs, 'home_weaknesses': hw,
        'away_strengths': as_, 'away_weaknesses': aw,
        'trend': {},
        'swot_url': '',
        'source': 'xg',
    }


# 语义类别 (用于合并去重: leisu叙述已覆盖的类别不再叠加数字条目)
_DIR_S = ('出色', '占优', '优势', '稳固', '争冠', '不败', '火力强')
_DIR_W = ('低迷', '不佳', '漏洞', '深陷', '降级', '下风', '劣势')


def _item_category(text):
    """条目语义分类: form近况/h2h交锋/rank排名/firepower火力防守/xg量化/other"""
    t = str(text)
    if 'xG' in t or 'PPDA' in t:
        return 'xg'
    if '交锋' in t or '面对' in t:
        return 'h2h'
    if '排名' in t or '争冠' in t or '降级' in t:
        return 'rank'
    if any(k in t for k in ('场均', '火力', '失球', '零封', '防守', '进攻')):
        return 'firepower'
    # 近况类: leisu叙述格式多样 ("6场比赛5胜0平"/"近10场正赛"/"近况出色"/"连胜"),
    # 单靠'近'字漏判 → leisu已写近况时stats近况条目会重复并入 (单测#4实证)
    if ('近' in t and '场' in t) or '近况' in t or '连胜' in t or '连败' in t \
            or re.search(r'\d+场.{0,10}\d+[胜平负]', t):
        return 'form'
    return 'other'


def _is_directional(item):
    """条目是否携带明确方向信号 (中性描述条目合并时跳过 — 纯上下文无方向,
    进条目列表只会+1计分噪音; fallback独用时保留完整上下文)"""
    t = str(item)
    return any(k in t for k in _DIR_S) or any(k in t for k in _DIR_W)


def merge_extra_swot(base, extra):
    """把数据型条目(extra)合并进leisu条目(base) — 类别去重 + 方向过滤

    规则:
      1. extra条目须携带方向信号 (中性"近况: 3胜2平1负"类不并入)
      2. 类别去重: leisu同侧已覆盖该类别 → 跳过 (叙述+数字各一份=重复计分)
      3. xG类别例外: 量化源与叙述天然无重叠, 永远并入
    返回合并后的base (原地修改); extra为空返回base原样
    """
    if not extra:
        return base
    if not base:
        return extra
    for side in ('home_strengths', 'home_weaknesses', 'away_strengths', 'away_weaknesses'):
        base_items = base.setdefault(side, [])
        base_cats = {_item_category(it) for it in base_items}
        for it in extra.get(side, []):
            cat = _item_category(it)
            if cat != 'xg' and (cat in base_cats or not _is_directional(it)):
                continue
            if it in base_items:
                continue
            base_items.append(it)
            base_cats.add(cat)
    _src = base.get('source', 'leisu')
    _esrc = extra.get('source', 'stats')
    if _esrc not in str(_src):
        base['source'] = f"{_src}+{_esrc}"
    return base


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

    # 4. Ultra 15.9: 数据型情报每场都构建并合并 (stats+xg → leisu)
    # 旧版: stats仅在leisu失败时兜底, 情报单源leisu, 覆盖不稳
    # 新版: leisu为主源, stats(近况/交锋/排名/火力)+xG(量化对比)按类别去重后
    #       并入 — 每场情报从"一家叙述"变成"多源交叉验证"
    n_merged = 0
    for key in matches:
        md = data_map.get(key) or {}
        # 4a. 数据型条目 (stats + xg 独立构建)
        stats_swot = build_stats_swot(md)
        xg_swot = None
        try:
            xg_swot = build_xg_swot(md)
        except Exception:
            xg_swot = None

        if key in results:
            # 4b. leisu已有 → 类别去重合并 (leisu叙述覆盖的类别不叠加数字条目)
            base = results[key]
            _n0 = (len(base.get('home_strengths', [])) + len(base.get('home_weaknesses', [])) +
                   len(base.get('away_strengths', [])) + len(base.get('away_weaknesses', [])))
            if stats_swot:
                merge_extra_swot(base, stats_swot)
            if xg_swot:
                merge_extra_swot(base, xg_swot)
            _n1 = (len(base.get('home_strengths', [])) + len(base.get('home_weaknesses', [])) +
                   len(base.get('away_strengths', [])) + len(base.get('away_weaknesses', [])))
            if _n1 > _n0:
                n_merged += 1
                print(f"  [SWOT] 🔗 {key} 多源合并 {_n0}→{_n1}条 (source={base.get('source')})")
        else:
            # 4c. leisu缺失 → stats+xg 合成完整情报 (原兜底路径增强)
            fused = None
            if stats_swot:
                fused = stats_swot
            if xg_swot:
                fused = merge_extra_swot(fused, xg_swot) if fused else xg_swot
            if fused:
                results[key] = fused
                _n = (len(fused['home_strengths']) + len(fused['home_weaknesses']) +
                      len(fused['away_strengths']) + len(fused['away_weaknesses']))
                print(f"  [SWOT] 📊 {key} stats+xg合成 ({_n}条, source={fused.get('source')})")

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
    print(f"  [SWOT] 已写入 {SWOT_DATA_FILE} (本次{len(results)}场, 累计{len(matches_map)}场, 多源合并{n_merged}场)")

    return results


if __name__ == '__main__':
    # 独立测试: 用当前sporttery场次
    import v215_e2e as e
    m = e.fetch_sporttery_matches(e.MATCH_NUMBERS, e.TARGET_DATE)
    fetch_swot_auto(m)
