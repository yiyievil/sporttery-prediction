# -*- coding: utf-8 -*-
"""杯赛首回合大比分惩罚机制 (Ultra 7.7 实证修正版)

仅适用于有主客场两回合制的杯赛（欧冠/欧罗巴/欧协联/亚冠等），联赛不适用。

核心逻辑:
1. 从SWOT数据中解析首回合比分
2. 当首回合分差≥3球时，落后方λ提升(背水一战强攻)，领先方λ下调(保守轮换)
3. 置信度封顶★★★★ (4.0星)，防止过度自信

Ultra 7.7 实证修正 (2026-07-30, 波兹南0-3惨败案例):
- 旧逻辑(Ultra 7.6): 落后方 λ×0.90 (减少进攻) → 实际杯赛次回合落后方应强攻
- 实证案例: 波兹南首回合4-1领先, 次回合0-3惨败 → 落后方背水一战效应
- 新参数: 落后方 λ×1.10 (3球) 每球+3% 上限+15%; 领先方 ×0.95 (保守)
- 置信度封顶★★★★ 保留 (次回合战术不确定性难建模)

触发条件:
- 联赛名匹配杯赛列表
- SWOT文本中包含"首回合X-Y"比分信息
- 首回合分差≥3球
"""

import json
import os
import re

_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
SWOT_DATA_FILE = os.path.join(_WORKSPACE, 'predictions', 'swot_data_refreshed.json')

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

# ===== 惩罚参数 (Ultra 7.7 实证修正: 2026-07-30 杯赛次回合修正) =====
# 实证案例: 波兹南首回合4-1领先, 次回合0-3惨败 → 落后方背水一战进攻加成
# 旧逻辑(Ultra 7.6): 落后方λ×0.90(减少进攻) → 实际应增加进攻(绝境强攻)
# 新逻辑(Ultra 7.7): 落后方λ增加(背水一战), 领先方λ减少(保守轮换)
PENALTY_THRESHOLD = 3       # 首回合分差≥3球触发
UNDERDOG_BOOST_MIN = 0.10   # 3球分差: 落后方进攻提升10% (背水一战)
UNDERDOG_BOOST_PER_GOAL = 0.03  # 每多1球追加3%
UNDERDOG_BOOST_MAX = 0.15   # 落后方进攻提升上限15%
LEADER_PENALTY = 0.05       # 领先方下调5% (保守/轮换/战意松)
CONFIDENCE_CAP = 4.0        # 置信度封顶 ★★★★


def is_cup_competition(league):
    """判断是否为有主客场制的杯赛"""
    if not league:
        return False
    # 精确匹配
    if league in CUP_LEAGUES:
        return True
    # 模糊匹配 (联赛名包含杯赛关键词)
    cup_keywords = ['欧冠', '欧罗巴', '欧联', '欧协联', '亚冠', '解放者杯', '资格赛']
    for kw in cup_keywords:
        if kw in league:
            return True
    return False


def parse_first_leg_score(swot_texts):
    """从SWOT文本中解析首回合比分
    
    支持的文本模式:
    - "首回合0-4惨败" → home_scored=0, away_scored=4 (在home字段中)
    - "首回合1-0取胜" → away_scored=1, home_scored=0 (在away字段中)
    - "首回合0-1小负" → home_scored=0, away_scored=1 (在home字段中)
    - "首回合4-0完胜" → away_scored=4, home_scored=0 (在away字段中)
    - "首回合客场0-1" → with extra context
    
    注意: 需排除"角球11-1"、"控球57%"等非比分数据
    
    参数:
        swot_texts: list of (text, perspective) tuples
                    perspective: 'home' or 'away'
    
    返回:
        (first_leg_home_goals, first_leg_away_goals) 或 None
    """
    # 排除这些关键词后面的数字（不是比分）
    EXCLUDE_KEYWORDS = ['角球', '控球', '射门', '传球', '犯规', '越位', '黄牌', '红牌']
    
    # 策略1: "首回合"紧跟比分（最可靠，如"首回合0-4惨败"）
    pattern1 = re.compile(r'首回合\s*(\d+)\s*[-:：]\s*(\d+)')
    
    # 策略2: 比分+结果词（如"0-1惜败"、"4-0完胜"、"1-0取胜"）
    result_words = r'(?:惜败|惨败|完胜|取胜|领先|落后|小负|告负|战平|打平|逼平|大胜|告捷|落败|失利|惨胜|险胜|完败)'
    pattern2 = re.compile(r'(\d+)\s*[-:：]\s*(\d+)\s*' + result_words)
    
    def _is_excluded(text, match_start):
        """检查匹配位置前面是否有关键词（如"角球11-1"）"""
        prefix = text[max(0, match_start - 10):match_start]
        for kw in EXCLUDE_KEYWORDS:
            if kw in prefix:
                return True
        return False
    
    results = []
    for text, perspective in swot_texts:
        if not text:
            continue
        
        # 优先用 pattern1 (首回合紧跟比分)
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
        
        # 其次用 pattern2 (比分+结果词)
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
    
    # 取第一个有效结果 (首回合比分唯一)
    return results[0]


def compute_cup_leg_penalty(match_num, league, home_name='', away_name=''):
    """计算杯赛首回合大比分惩罚
    
    参数:
        match_num: 比赛编号 (如 "周二001")
        league: 联赛名
        home_name: 主队名
        away_name: 客队名
    
    返回:
        dict 或 None:
        {
            'applied': True,
            'first_leg_home': 0,     # 首回合主队进球
            'first_leg_away': 4,     # 首回合客队进球
            'goal_diff': 4,          # 首回合分差
            'trailing_side': 'home', # 落后方: 'home' 或 'away'
            'lambda_factor': 0.6,    # 落后方λ乘数 (1-惩罚比例)
            'leader_factor': 0.95,   # 领先方λ乘数 (Ultra 7.6 对称修正)
            'conf_cap': 4.0,         # 置信度封顶
            'penalty_pct': 0.40,     # 惩罚比例
            'note': '首回合0-4落后,主队λ×0.60,客队λ×0.95,置信度封顶★★★★'
        }
    """
    # 1. 检查是否为杯赛
    if not is_cup_competition(league):
        return None
    
    # 2. 加载SWOT数据
    try:
        with open(SWOT_DATA_FILE, 'r', encoding='utf-8') as f:
            swot_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    
    # 3. 匹配比赛
    matches = swot_data.get('matches', {})
    swot_entry = matches.get(match_num)
    
    if not swot_entry:
        # 尝试按球队名匹配
        for k, v in matches.items():
            swot_home = v.get('home_name', '')
            swot_away = v.get('away_name', '')
            if (home_name and home_name in swot_home) or (away_name and away_name in swot_away):
                swot_entry = v
                break
    
    if not swot_entry:
        return None
    
    # 4. 收集所有SWOT文本
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
    
    # 5. 解析首回合比分
    first_leg = parse_first_leg_score(swot_texts)
    if first_leg is None:
        return None
    
    first_leg_home, first_leg_away = first_leg
    goal_diff = first_leg_away - first_leg_home  # 正数=主队落后, 负数=客队落后
    
    abs_diff = abs(goal_diff)
    if abs_diff < PENALTY_THRESHOLD:
        return None
    
    # 6. 计算惩罚 (Ultra 7.7: 落后方背水一战加成 + 领先方保守修正)
    penalty_pct = min(
        UNDERDOG_BOOST_MIN + (abs_diff - PENALTY_THRESHOLD) * UNDERDOG_BOOST_PER_GOAL,
        UNDERDOG_BOOST_MAX
    )
    lambda_factor = 1.0 + penalty_pct   # 落后方进攻提升 (背水一战)
    leader_factor = 1.0 - LEADER_PENALTY  # 领先方保守收缩

    if goal_diff > 0:
        # 主队落后
        trailing_side = 'home'
        note = (f'首回合{first_leg_home}-{first_leg_away}落后{abs_diff}球,'
                f'主队λ×{lambda_factor:.2f}(背水一战),客队λ×{leader_factor:.2f}(保守),置信度封顶★★★★')
    else:
        # 客队落后
        trailing_side = 'away'
        note = (f'首回合{first_leg_home}-{first_leg_away}领先{abs_diff}球,'
                f'客队λ×{lambda_factor:.2f}(背水一战),主队λ×{leader_factor:.2f}(保守),置信度封顶★★★★')

    return {
        'applied': True,
        'first_leg_home': first_leg_home,
        'first_leg_away': first_leg_away,
        'goal_diff': abs_diff,
        'trailing_side': trailing_side,
        'lambda_factor': lambda_factor,
        'leader_factor': leader_factor,
        'conf_cap': CONFIDENCE_CAP,
        'penalty_pct': penalty_pct,
        'leader_penalty_pct': LEADER_PENALTY,
        'note': note,
    }


# ===== 缓存 (避免重复加载SWOT文件) =====
_penalty_cache = {}


def get_cup_leg_penalty(match_num, league, home_name='', away_name=''):
    """带缓存的惩罚查询 (同一场比赛只计算一次)"""
    cache_key = (match_num, league)
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
    result = get_cup_leg_penalty('周二002', '欧冠', '哈茨', '格风暴')
    if result:
        print('✅ 惩罚触发:')
        for k, v in result.items():
            print(f'  {k}: {v}')
    else:
        print('❌ 未触发惩罚')
    
    print()
    result2 = get_cup_leg_penalty('周二001', '欧冠', '库奥皮奥', '萨巴赫')
    if result2:
        print('✅ 惩罚触发:')
        for k, v in result2.items():
            print(f'  {k}: {v}')
    else:
        print('✅ 未触发惩罚 (分差<3, 正确)')
