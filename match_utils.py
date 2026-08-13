#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨站比赛匹配引擎 — 多信号融合匹配 (队名 + 日期 + 时间 + 联赛)

设计原则:
  1. 队名匹配是核心 (权重最高), 日期/时间/联赛作辅助约束
  2. 多信号加权融合, 而非二值判断
  3. 所有数据源统一接口, 消除各模块碎片化匹配逻辑

用法:
  from match_utils import MatchFingerprint, match_score, find_best_match

  # 构建比赛指纹
  sp = MatchFingerprint.from_sporttery({"home": "艾卜哈", "away": "拉斯决心",
                                         "league": "沙职", "match_time": "00:15",
                                         "match_date": "2026-08-14"})
  lei = MatchFingerprint.from_leisu({"home": "艾卜哈", "away": "哈森姆",
                                      "league": "沙特联", "time": "00:15"})

  score = match_score(sp, lei)  # 0.85 → 高质量匹配
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import re

# ============================================================
# 联赛别名表 — 跨站联赛名统一映射
# ============================================================
LEAGUE_ALIASES: dict[str, list[str]] = {
    # 欧战
    "欧罗巴": ["欧罗巴", "欧联", "欧联杯", "欧罗巴联赛", "欧罗巴杯", "欧霸杯"],
    "欧冠": ["欧冠", "欧冠杯", "欧洲冠军联赛", "欧冠联赛"],
    "欧协联": ["欧协联", "欧协杯", "欧会杯", "欧会杯联赛"],
    # 亚洲
    "亚冠": ["亚冠", "亚冠杯", "亚洲冠军联赛", "亚冠联赛"],
    "亚协杯": ["亚协杯", "亚协联", "亚足联杯"],
    # 沙特
    "沙职": ["沙职", "沙特联", "沙特职业联赛", "沙超", "沙特超"],
    # 日韩
    "日职": ["日职", "日职联", "J联赛", "J1联赛", "日职联赛"],
    "韩职": ["韩职", "韩K联", "K联赛", "K1联赛", "韩K", "韩职联赛"],
    # 北欧
    "瑞超": ["瑞超", "瑞典超", "瑞典超联"],
    "挪超": ["挪超", "挪威超", "挪威超联"],
    "丹超": ["丹超", "丹麦超", "丹麦超联"],
    "芬超": ["芬超", "芬兰超", "芬兰超联"],
    # 东欧
    "俄超": ["俄超", "俄罗斯超", "俄罗斯超联"],
    "土超": ["土超", "土耳其超", "土耳其超联"],
    "荷甲": ["荷甲", "荷兰甲", "荷兰甲级"],
    "荷乙": ["荷乙", "荷兰乙", "荷兰乙级"],
    "葡超": ["葡超", "葡萄牙超", "葡萄牙超联"],
    "比甲": ["比甲", "比利时甲", "比利时甲级"],
    # 美洲
    "美职联": ["美职联", "美职", "MLS", "美国职业大联盟"],
    "巴甲": ["巴甲", "巴西甲", "巴西甲级"],
    "阿甲": ["阿甲", "阿根廷甲", "阿根廷甲级"],
    "解放者杯": ["解放者杯", "南美解放者杯", "自由杯", "南美自由杯"],
    # 英西德意法
    "英超": ["英超", "英格兰超级联赛", "英格超"],
    "西甲": ["西甲", "西班牙甲级联赛"],
    "德甲": ["德甲", "德国甲级联赛"],
    "意甲": ["意甲", "意大利甲级联赛"],
    "法甲": ["法甲", "法国甲级联赛"],
    "德乙": ["德乙", "德国乙级联赛"],
    "英冠": ["英冠", "英格兰冠军联赛"],
    # 中超
    "中超": ["中超", "中国超级联赛", "中国超"],
}

# 反向索引: 别名 → 标准名
_LEAGUE_ALIAS_REVERSE: dict[str, str] = {}
for _std, _aliases in LEAGUE_ALIASES.items():
    for _a in _aliases:
        _LEAGUE_ALIAS_REVERSE[_a] = _std


# ============================================================
# 队名别名表 — 从 nowscore_fetch 导入 (共享)
# ============================================================
try:
    from nowscore_fetch import TEAM_NAME_ALIASES
except ImportError:
    TEAM_NAME_ALIASES: dict[str, list[str]] = {}


# ============================================================
# MatchFingerprint — 比赛指纹
# ============================================================
@dataclass
class MatchFingerprint:
    """比赛指纹 — 从任意数据源提取的统一结构

    所有字段均为可选, 匹配时按可用字段评分。
    """
    home: str = ""
    away: str = ""
    league: str = ""          # 联赛名 (原始, 匹配时走别名表)
    match_date: str = ""      # "YYYY-MM-DD"
    match_time: str = ""      # "HH:MM" 或 "HH:MM:SS"
    weekday: str = ""         # "周X"

    def __post_init__(self):
        self.match_time = self._normalize_time(self.match_time)

    @staticmethod
    def _normalize_time(t: str) -> str:
        """统一时间格式: HH:MM (去掉秒)"""
        if not t:
            return ""
        # "00:15:00" → "00:15"
        m = re.match(r'^(\d{2}:\d{2})', t.strip())
        return m.group(1) if m else t.strip()

    @classmethod
    def from_sporttery(cls, match_info: dict) -> "MatchFingerprint":
        """从 sporttery API 返回的 match_info 构建指纹"""
        return cls(
            home=match_info.get("home", ""),
            away=match_info.get("away", ""),
            league=match_info.get("league", ""),
            match_date=match_info.get("match_date", ""),
            match_time=match_info.get("match_time", ""),
            weekday=match_info.get("weekday", ""),
        )

    @classmethod
    def from_leisu(cls, guide: dict) -> "MatchFingerprint":
        """从 leisu 卡片构建指纹"""
        return cls(
            home=guide.get("home", ""),
            away=guide.get("away", ""),
            league=guide.get("league", ""),
            match_time=guide.get("time", ""),
        )

    @classmethod
    def from_nowscore(cls, match_info: dict) -> "MatchFingerprint":
        """从 nowscore 数据构建指纹"""
        return cls(
            home=match_info.get("home", ""),
            away=match_info.get("away", ""),
            league=match_info.get("league", ""),
            match_time=match_info.get("match_time", ""),
            match_date=match_info.get("match_date", ""),
        )


# ============================================================
# 评分函数
# ============================================================

def _team_name_similarity(a: str, b: str) -> float:
    """队名相似度 (0.0 ~ 1.0)

    策略:
      1. 别名表双向包含 → 1.0
      2. 原始名直接包含   → 0.9
      3. 字符重叠率       → 0.4~0.8
      4. 英文名匹配       → 0.9
    """
    if not a or not b:
        return 0.0

    # L1: 别名表
    aliases = TEAM_NAME_ALIASES.get(a, [a])
    for alias in aliases + [a]:
        if alias and (alias in b or b in alias):
            return 1.0

    # L2: 原始名直接包含 (不需别名表)
    if a in b or b in a:
        return 0.9

    # L3: 字符重叠 (中文队名)
    set_a = set(a)
    set_b = set(b)
    a_is_cn = any('\u4e00' <= c <= '\u9fff' for c in a)
    b_is_cn = any('\u4e00' <= c <= '\u9fff' for c in b)
    if a_is_cn and b_is_cn and len(set_a) >= 2:
        overlap = set_a & set_b
        shorter = min(len(set_a), len(set_b))
        if shorter > 0:
            ratio = len(overlap) / shorter
            if ratio >= 0.4:
                return 0.4 + 0.6 * (ratio - 0.4) / 0.6  # 0.4~1.0 线性映射

    # L4: 英文名子串匹配
    if a_is_cn and not b_is_cn:
        return 0.0
    if not a_is_cn and not b_is_cn:
        if a.lower() in b.lower() or b.lower() in a.lower():
            return 0.9

    return 0.0


def _league_similarity(a: str, b: str) -> float:
    """联赛名相似度 (0.0 ~ 1.0)"""
    if not a or not b:
        return 0.5  # 无联赛信息时不惩罚也不奖励

    # 走别名表
    a_std = _LEAGUE_ALIAS_REVERSE.get(a, a)
    b_std = _LEAGUE_ALIAS_REVERSE.get(b, b)
    if a_std == b_std:
        return 1.0

    # 子串包含
    if a in b or b in a:
        return 0.8

    # 字符重叠 (中文)
    set_a = set(a)
    set_b = set(b)
    if set_a and set_b:
        overlap = set_a & set_b
        shorter = min(len(set_a), len(set_b))
        if shorter > 0 and len(overlap) / shorter >= 0.5:
            return 0.6

    return 0.0


def _time_similarity(a: str, b: str) -> float:
    """开赛时间相似度 (0.0 ~ 1.0)

    策略:
      - 精确匹配 → 1.0
      - 差 ≤15分钟 → 0.8
      - 差 ≤30分钟 → 0.6
      - 差 ≤60分钟 → 0.3
      - 差 >60分钟  → 0.0
    """
    if not a or not b:
        return 0.5  # 无时间信息时不惩罚

    try:
        h1, m1 = map(int, a.split(":")[:2])
        h2, m2 = map(int, b.split(":")[:2])
        diff = abs((h1 * 60 + m1) - (h2 * 60 + m2))
        if diff == 0:
            return 1.0
        if diff <= 15:
            return 0.8
        if diff <= 30:
            return 0.6
        if diff <= 60:
            return 0.3
        return 0.0
    except (ValueError, IndexError):
        return 0.5


def _date_similarity(a: str, b: str) -> float:
    """日期相似度 (0.0 ~ 1.0)

    策略:
      - 精确匹配 → 1.0
      - 差 ±1天    → 0.7
      - 差 ±2天    → 0.3
      - 差 >2天    → 0.0
    """
    if not a or not b:
        return 0.5
    try:
        from datetime import date
        da = date.fromisoformat(a)
        db = date.fromisoformat(b)
        diff = abs((da - db).days)
        if diff == 0:
            return 1.0
        if diff == 1:
            return 0.7
        if diff == 2:
            return 0.3
        return 0.0
    except (ValueError, TypeError):
        return 0.5


def match_score(a: MatchFingerprint, b: MatchFingerprint,
                weights: dict | None = None) -> float:
    """多信号融合匹配评分 (0.0 ~ 1.0)

    默认权重 (可覆盖):
      team_name: 0.55  — 队名匹配 (核心)
      league:    0.15  — 联赛匹配
      time:      0.15  — 开赛时间匹配
      date:      0.15  — 日期匹配

    返回: 0.0 ~ 1.0, 建议阈值 ≥0.6 为可信匹配

    注意: 正向匹配 (home↔home, away↔away) 和反向匹配 (home↔away, away↔home)
    取最高分, 因为不同数据源可能主客队顺序不同。
    """
    w = weights or {"team_name": 0.55, "league": 0.15, "time": 0.15, "date": 0.15}

    # 正向匹配
    home_score = _team_name_similarity(a.home, b.home)
    away_score = _team_name_similarity(a.away, b.away)
    team_forward = (home_score + away_score) / 2.0

    # 反向匹配 (主客颠倒)
    home_rev = _team_name_similarity(a.home, b.away)
    away_rev = _team_name_similarity(a.away, b.home)
    team_reverse = (home_rev + away_rev) / 2.0

    team = max(team_forward, team_reverse)

    league = _league_similarity(a.league, b.league)
    time = _time_similarity(a.match_time, b.match_time)
    date = _date_similarity(a.match_date, b.match_date)

    score = (w["team_name"] * team +
             w["league"] * league +
             w["time"] * time +
             w["date"] * date)

    return round(score, 4)


def find_best_match(target: MatchFingerprint, candidates: list[MatchFingerprint],
                    threshold: float = 0.55,
                    weights: dict | None = None) -> tuple[int | None, float]:
    """从候选列表中找最佳匹配

    返回: (index, score) 或 (None, 0.0)
    """
    best_idx, best_score = None, 0.0
    for i, c in enumerate(candidates):
        s = match_score(target, c, weights)
        if s > best_score:
            best_score = s
            best_idx = i
    if best_score >= threshold:
        return best_idx, best_score
    return None, 0.0


# ============================================================
# 便捷方法: 兼容旧接口
# ============================================================

def team_match(sp_name: str, other_name: str) -> float:
    """单队名匹配 (兼容旧 _team_match 接口, 返回 float 而非 bool)

    use: score = team_match("艾卜哈", "艾卜哈")  # 1.0
    """
    return _team_name_similarity(sp_name, other_name)


def team_match_bool(sp_name: str, other_name: str, threshold: float = 0.4) -> bool:
    """单队名匹配 (兼容旧 _team_match 接口, 返回 bool)"""
    return _team_name_similarity(sp_name, other_name) >= threshold