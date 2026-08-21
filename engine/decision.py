#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine.decision — 决策/展示层纯函数 (改进#1 第一增量, 2026-08-21)

自 v215_e2e.py 原样迁移 (行为不变): 四函数均为零模块状态依赖的纯函数。
原位置改为 import 绑定, 全部内部/外部调用方 (v215_update/v215_verify/
swot_fusion_v3 等) 无感知。
"""
import re


def format_stars(score):
    """将1.0-5.0的分数转为5星制字符串(含半星)

    例如:
      5.0 → ★★★★★    4.5 → ★★★★½
      4.0 → ★★★★      3.5 → ★★★½
      3.0 → ★★★        2.5 → ★★½
      2.0 → ★★          1.5 → ★½
      1.0 → ★
    """
    score = round(score * 2) / 2  # 取最近的0.5
    score = max(1.0, min(5.0, score))
    full = int(score)
    half = 1 if (score - full) >= 0.5 else 0
    return '★' * full + ('½' if half else '')


def stars_to_score(stars_str):
    """将星级字符串转回分数 (用于对比/校准)

    ★★★★★ → 5.0, ★★★★½ → 4.5, ★★★ → 3.0, ★ → 1.0
    """
    if not stars_str:
        return 0.0
    full_count = stars_str.count('★')
    has_half = '½' in stars_str
    return full_count + (0.5 if has_half else 0.0)


def kelly_criterion(prob, odds, margin=0.0):
    """四分之一Kelly投注比例 + margin加权value判定
    f* = (bp - q) / b, 实际使用 f* / 4
    value: f>0 且 EV >= margin/2 (抽水越深的玩法要求越高边际, 默认margin=0保持旧行为)
    返回: {'stake_pct': float, 'ev': float, 'value': bool}
    """
    b = odds - 1
    if b <= 0:
        return {'stake_pct': 0, 'ev': 0, 'value': False}
    f = (b * prob - (1 - prob)) / b
    ev = prob * odds - 1  # EV = P×赔率 - 1
    stake = max(0, f * 0.25) * 100
    # Optimize: margin/2 阈值偏保守, 改为 margin*0.3
    # 实证: 比分玩法margin高达30-40%, margin/2=15-20%阈值过高,
    # 很多略高于0的正EV被忽略; margin*0.3在保守和敏感之间折中
    value_threshold = margin * 0.3
    return {'stake_pct': round(stake, 1), 'ev': round(ev * 100, 1),
            'value': f > 0 and ev >= value_threshold}


def _hhad_display_label(option, handicap):
    """HHAD选项/洞察文案术语规范化 (Ultra 11.10 铁律, 与 gen_pred_pdf._hhad_option_label 一致)

    - 负盘(≤-1)=让球: 让胜/让负/让平 不变
    - 正盘(≥+1)=受让: 让胜→受让胜, 让负→受让负, 让平→受让平
    - 0=平盘: 保持让X不变
    只处理含 '让胜'/'让负'/'让平' 的文本, 其余原样返回。
    """
    if not option:
        return option
    try:
        hcap = float(handicap)
    except (TypeError, ValueError):
        return option
    if hcap <= 0:
        return option  # 让球盘或平盘, 术语不变
    # 幂等替换 (ERR-20260809-001): 用负向后瞻 (?<!受) 避免对已含"受让X"的文本二次替换成"受受让X"
    # 例如传入"HHAD受让胜"时, "让胜"前已有"受", 不再替换 → 不会变成"HHAD受受让胜"
    for src, dst in [('让胜', '受让胜'), ('让负', '受让负'), ('让平', '受让平')]:
        option = re.sub(r'(?<!受)' + src, dst, option)
    return option
