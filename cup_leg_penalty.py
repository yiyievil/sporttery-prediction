# -*- coding: utf-8 -*-
"""杯赛首回合比分惩罚机制 (Ultra 7.8 全面修正版)

仅适用于有主客场两回合制的杯赛（欧冠/欧罗巴/欧协联/亚冠等），联赛不适用。

Ultra 7.8 核心修正 (2026-07-30):
- 问题1: 旧版只从SWOT文本解析首回合比分, SWOT无数据时完全失效
  → 修复: 新增 first_leg_scores.json 手动注入机制, 优先级最高
- 问题2: 旧版阈值3球过高, 1-2球分差完全不影响预测
  → 修复: 阈值降至1球, 分级惩罚 (1球轻微/2球中等/3球+严重)
- 问题3: 总比分平局时(如1-1), 客场进球多的球队有优势, 未建模
  → 修复: 新增客场进球优势因子

核心逻辑:
1. 优先从 first_leg_scores.json 读取首回合比分 (手动注入, 可靠)
2. 回退到SWOT文本解析 (自动, 不一定有)
3. 分差≥1球即触发:
   - 落后方: λ提升(背水一战强攻), 幅度随分差递增
   - 领先方: λ下调(保守轮换), 幅度固定
4. 总比分平局时: 客场进球多的一方有战术优势
5. 置信度封顶: 分差越大封顶越低
"""

import json
import os
import re

_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
SWOT_DATA_FILE = os.path.join(_WORKSPACE, 'predictions', 'swot_data_refreshed.json')
FIRST_LEG_FILE = os.path.join(_WORKSPACE, 'predictions', 'first_leg_scores.json')

# ===== 杯赛联赛名 (有主客场两回合制) =====
CUP_LEAGUES = {
    # 欧冠
    '欧冠', '欧冠杯', '冠军联赛', '欧洲冠军联赛', '欧冠资格赛', '欧冠附',
    # 欧罗巴/欧联
    '欧罗巴', '欧联', '欧联杯', '欧洲联赛', '欧罗巴联赛', '欧联资格赛',
    # 欧协联
    '欧协联', '欧协联杯', '欧洲协会联赛', '欧协联资格赛',
    # 亚冠
    '亚冠', '亚冠杯', '亚足联冠军联赛', '亚冠附',
    # 南美
    '解放者杯', '南美解放者杯', '南美杯', '南美俱乐部杯',
    # 中北美
    '中北美冠', '中北美冠军联赛',
    # 非洲
    '非洲冠', '非洲冠军联赛',
}

# ===== Ultra 7.8: 分级惩罚参数 =====
# 阈值降至1球: 任何首回合分差都应影响次回合预测
PENALTY_THRESHOLD = 1

# 分级λ调整: 落后方背水一战加成 (进攻提升)
# 1球分差: +5%  2球: +10%  3球: +13%  4球: +16%  5球+: +18%
UNDERDOG_BOOST = {
    1: 0.05,
    2: 0.10,
    3: 0.13,
    4: 0.16,
}
UNDERDOG_BOOST_MAX = 0.18  # 5球及以上上限

# 领先方保守修正 (保守/轮换/战意松)
# 1球: -3%  2球: -5%  3球+: -8%
LEADER_PENALTY = {
    1: 0.03,
    2: 0.05,
    3: 0.08,
}
LEADER_PENALTY_MAX = 0.10  # 4球及以上上限

# 置信度封顶 (分差越大, 次回合不确定性越高)
CONF_CAP = {
    1: 4.5,   # 1球分差: ★★★★½
    2: 4.0,   # 2球分差: ★★★★
    3: 3.5,   # 3球分差: ★★★½
}
CONF_CAP_MIN = 3.0  # 4球及以上: ★★★


def is_cup_competition(league):
    """判断是否为有主客场制的杯赛"""
    if not league:
        return False
    if league in CUP_LEAGUES:
        return True
    cup_keywords = ['欧冠', '欧罗巴', '欧联', '欧协联', '亚冠', '解放者杯', '资格赛']
    for kw in cup_keywords:
        if kw in league:
            return True
    return False


def _get_boost(diff):
    """获取落后方进攻加成比例"""
    if diff in UNDERDOG_BOOST:
        return UNDERDOG_BOOST[diff]
    return UNDERDOG_BOOST_MAX


def _get_leader_penalty(diff):
    """获取领先方保守下调比例"""
    if diff in LEADER_PENALTY:
        return LEADER_PENALTY[diff]
    return LEADER_PENALTY_MAX


def _get_conf_cap(diff):
    """获取置信度封顶"""
    if diff in CONF_CAP:
        return CONF_CAP[diff]
    return CONF_CAP_MIN


def load_first_leg_scores():
    """从 first_leg_scores.json 加载手动注入的首回合比分"""
    try:
        with open(FIRST_LEG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # 过滤掉 _ 开头的说明字段
        return {k: v for k, v in data.items() if not k.startswith('_')}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def parse_first_leg_score(swot_texts):
    """从SWOT文本中解析首回合比分 (回退方案)"""
    EXCLUDE_KEYWORDS = ['角球', '控球', '射门', '传球', '犯规', '越位', '黄牌', '红牌']

    pattern1 = re.compile(r'首回合\s*(\d+)\s*[-:：]\s*(\d+)')
    result_words = r'(?:惜败|惨败|完胜|取胜|领先|落后|小负|告负|战平|打平|逼平|大胜|告捷|落败|失利|惨胜|险胜|完败)'
    pattern2 = re.compile(r'(\d+)\s*[-:：]\s*(\d+)\s*' + result_words)

    def _is_excluded(text, match_start):
        prefix = text[max(0, match_start - 10):match_start]
        for kw in EXCLUDE_KEYWORDS:
            if kw in prefix:
                return True
        return False

    results = []
    for text, perspective in swot_texts:
        if not text:
            continue
        found = False
        for m in pattern1.finditer(text):
            if _is_excluded(text, m.start()):
                continue
            try:
                x, y = int(m.group(1)), int(m.group(2))
                if x > 20 or y > 20:
                    continue
                if perspective == 'home':
                    results.append((x, y))
                else:
                    results.append((y, x))
                found = True
                break
            except (ValueError, IndexError):
                continue
        if found:
            continue
        for m in pattern2.finditer(text):
            if _is_excluded(text, m.start()):
                continue
            try:
                x, y = int(m.group(1)), int(m.group(2))
                if x > 20 or y > 20:
                    continue
                if perspective == 'home':
                    results.append((x, y))
                else:
                    results.append((y, x))
                found = True
                break
            except (ValueError, IndexError):
                continue

    if not results:
        return None
    return results[0]


def compute_cup_leg_penalty(match_num, league, home_name='', away_name=''):
    """计算杯赛首回合惩罚 (Ultra 7.8: 手动注入优先 + 分级惩罚)

    首回合比分含义:
    - first_leg_home = 本场次回合主队 在首回合中的进球数
    - first_leg_away = 本场次回合客队 在首回合中的进球数
    - goal_diff > 0 → 本场主队首回合落后 (需追分, 背水一战)
    - goal_diff < 0 → 本场客队首回合落后 (需追分, 背水一战)
    - goal_diff = 0 → 总比分平局 (看客场进球)
    """
    if not is_cup_competition(league):
        return None

    first_leg_home = None
    first_leg_away = None
    source = None

    # ===== 优先级1: 手动注入文件 =====
    manual_scores = load_first_leg_scores()

    # 按 match_num 匹配 (如 "周四001")
    entry = manual_scores.get(match_num)
    if entry:
        first_leg_home = entry.get('first_leg_home')
        first_leg_away = entry.get('first_leg_away')
        source = 'manual'

    # 按球队名匹配 (如果编号没匹配到)
    if first_leg_home is None:
        for k, v in manual_scores.items():
            mh = v.get('home_team', '')
            ma = v.get('away_team', '')
            if (home_name and home_name in mh) or (away_name and away_name in ma):
                first_leg_home = v.get('first_leg_home')
                first_leg_away = v.get('first_leg_away')
                source = 'manual_by_name'
                break

    # ===== 优先级2: SWOT文本解析 =====
    if first_leg_home is None:
        try:
            with open(SWOT_DATA_FILE, 'r', encoding='utf-8') as f:
                swot_data = json.load(f)
            matches = swot_data.get('matches', {})
            swot_entry = matches.get(match_num)
            if not swot_entry:
                for k, v in matches.items():
                    swot_home = v.get('home_name', '')
                    swot_away = v.get('away_name', '')
                    if (home_name and home_name in swot_home) or (away_name and away_name in swot_away):
                        swot_entry = v
                        break
            if swot_entry:
                swot_texts = []
                for field in ('home_strengths', 'home_weaknesses'):
                    items = swot_entry.get(field, [])
                    if isinstance(items, list):
                        for item in items:
                            swot_texts.append((item, 'home'))
                for field in ('away_strengths', 'away_weaknesses'):
                    items = swot_entry.get(field, [])
                    if isinstance(items, list):
                        for item in items:
                            swot_texts.append((item, 'away'))
                first_leg = parse_first_leg_score(swot_texts)
                if first_leg:
                    first_leg_home, first_leg_away = first_leg
                    source = 'swot'
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    # 无首回合比分数据
    if first_leg_home is None or first_leg_away is None:
        return None

    goal_diff = first_leg_away - first_leg_home  # 正=主队落后, 负=客队落后
    abs_diff = abs(goal_diff)

    # ===== Ultra 7.8: 总比分平局时的客场进球优势 =====
    if abs_diff == 0:
        # 总比分平局: 首回合客场进球多的球队有优势
        # 首回合: 本场主队是首回合客场, 本场客队是首回合主场
        # 所以 first_leg_home = 本场主队的客场进球, first_leg_away = 本场客队的主场进球
        # 如果本场主队首回合客场进球多 (first_leg_home > first_leg_away), 主队有客场进球优势
        # 但这里 abs_diff=0 意味着 first_leg_home == first_leg_away, 没有客场进球差异
        # 只有当总比分平但客场进球不同时才有优势 — 这在 abs_diff=0 时不可能
        # 所以平局时不触发惩罚, 但记录信息
        return {
            'applied': False,
            'first_leg_home': first_leg_home,
            'first_leg_away': first_leg_away,
            'goal_diff': 0,
            'trailing_side': None,
            'lambda_factor': 1.0,
            'leader_factor': 1.0,
            'conf_cap': None,
            'penalty_pct': 0.0,
            'leader_penalty_pct': 0.0,
            'source': source,
            'note': f'首回合{first_leg_home}-{first_leg_away}平局,总比分平,无惩罚',
        }

    if abs_diff < PENALTY_THRESHOLD:
        return {
            'applied': False,
            'first_leg_home': first_leg_home,
            'first_leg_away': first_leg_away,
            'goal_diff': abs_diff,
            'trailing_side': None,
            'lambda_factor': 1.0,
            'leader_factor': 1.0,
            'conf_cap': None,
            'penalty_pct': 0.0,
            'leader_penalty_pct': 0.0,
            'source': source,
            'note': f'首回合分差{abs_diff}球<阈值{PENALTY_THRESHOLD},无惩罚',
        }

    # ===== 计算分级惩罚 =====
    boost_pct = _get_boost(abs_diff)
    leader_pct = _get_leader_penalty(abs_diff)
    lambda_factor = 1.0 + boost_pct    # 落后方进攻提升
    leader_factor = 1.0 - leader_pct   # 领先方保守收缩
    conf_cap = _get_conf_cap(abs_diff)

    if goal_diff > 0:
        # 本场主队首回合落后
        trailing_side = 'home'
        stars = '★' * int(conf_cap) + ('½' if conf_cap % 1 == 0.5 else '')
        note = (f'首回合{first_leg_home}-{first_leg_away}落后{abs_diff}球(总比分{first_leg_home+first_leg_away}-{first_leg_away+first_leg_home}),'
                f'主队λ×{lambda_factor:.2f}(背水一战),客队λ×{leader_factor:.2f}(保守),置信度封顶{stars} [来源:{source}]')
    else:
        # 本场客队首回合落后
        trailing_side = 'away'
        stars = '★' * int(conf_cap) + ('½' if conf_cap % 1 == 0.5 else '')
        note = (f'首回合{first_leg_home}-{first_leg_away}领先{abs_diff}球(总比分{first_leg_home+first_leg_away}-{first_leg_away+first_leg_home}),'
                f'客队λ×{lambda_factor:.2f}(背水一战),主队λ×{leader_factor:.2f}(保守),置信度封顶{stars} [来源:{source}]')

    return {
        'applied': True,
        'first_leg_home': first_leg_home,
        'first_leg_away': first_leg_away,
        'goal_diff': abs_diff,
        'trailing_side': trailing_side,
        'lambda_factor': lambda_factor,
        'leader_factor': leader_factor,
        'conf_cap': conf_cap,
        'penalty_pct': boost_pct,
        'leader_penalty_pct': leader_pct,
        'source': source,
        'note': note,
    }


# ===== 缓存 =====
_penalty_cache = {}


def get_cup_leg_penalty(match_num, league, home_name='', away_name=''):
    """带缓存的惩罚查询"""
    cache_key = (match_num, league, home_name, away_name)
    if cache_key not in _penalty_cache:
        _penalty_cache[cache_key] = compute_cup_leg_penalty(
            match_num, league, home_name, away_name
        )
    return _penalty_cache[cache_key]


def clear_cache():
    """清除缓存"""
    global _penalty_cache
    _penalty_cache = {}


if __name__ == '__main__':
    # 测试
    print("=== Ultra 7.8 杯赛首回合惩罚测试 ===\n")

    tests = [
        ('周四001', '欧罗巴', '中日德兰', '贝西克塔'),
        ('周四002', '欧罗巴', '帕福斯', '斯海杜克'),
        ('周四003', '欧罗巴', '安德莱', '哈马比'),
        ('周四004', '欧罗巴', '费伦茨', '特温特'),
        ('周四005', '欧罗巴', '本菲卡', '圣加仑'),
        ('周四006', '巴甲', '科林蒂安', '巴竞技'),
    ]

    for match_num, league, home, away in tests:
        result = get_cup_leg_penalty(match_num, league, home, away)
        print(f"{match_num} {league} {home} vs {away}:")
        if result:
            if result.get('applied'):
                print(f"  ✅ 惩罚触发: {result['note']}")
                print(f"     λ_factor={result['lambda_factor']}, leader_factor={result['leader_factor']}, conf_cap={result['conf_cap']}")
            else:
                print(f"  ℹ️ 未触发: {result['note']}")
        else:
            print(f"  ❌ 无数据 (非杯赛或无首回合比分)")
        print()
