#!/usr/bin/env python3
"""SWOT融合脚本 v3 - 将SWOT数据融合到预测结果中

对于有SWOT数据的场次: 进行完整融合
对于无SWOT数据的场次: 标记为"无SWOT数据", 不调整置信度
"""

import json
import os
import re
from datetime import datetime

# Ultra-Opt: 通用路径 — 优先 SPORTTERY_WORKSPACE 环境变量, 缺省脚本所在目录
# (旧版硬编码 '/workspace/predictions' 为Linux路径, Windows上找不到文件)
_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
PREDICTIONS_DIR = os.path.join(_WORKSPACE, 'predictions')
SWOT_DATA_FILE = os.path.join(PREDICTIONS_DIR, 'swot_data_refreshed.json')

# ===== SWOT概率调整参数 (Ultra 6.4) =====
# SWOT不再只调置信度, 而是直接在胜/负之间迁移概率质量, 平局不受影响
# (本系统平局本就系统性低估, 不能让SWOT调整再侵蚀平局概率)
SWOT_SHIFT_PER_POINT = 0.01   # 每评分点 → 概率迁移1pp
SWOT_MAX_SHIFT = 0.20         # 迁移上限 ±20pp (联赛状态波动大, 12pp仍偏保守; 一蹶不振/触底反弹幅度大)
SWOT_MIN_DIFF = 2.0           # 评分差低于此值不调整 (噪音区)

# Ultra 13.3 (方向翻转 2026-08-14): SWOT作为最实时信息, 强信号时可翻转模型方向
# xG/排名等是常规判断(滞后/统计性), SWOT(伤停/状态/走势)是当下最直接的信号
# 当SWOT评分差足够大且方向与模型相反时, 直接翻转方向 — "该翻转时就翻转"
SWOT_FLIP_DIFF = 6.0     # 评分差≥6 视为强信号, 允许方向翻转 (约10%场次)
SWOT_FLIP_MARGIN = 0.05  # 翻转后反超安全余量 5pp
SWOT_FLIP_MAX = 0.35     # 翻转迁移上限 35pp (防止过度极端)

# Ultra 14.0 (2026-08-20): 翻转后一致性重算 — λ镜像增强参数
SWOT_LAMBDA_BOOST_PER_POINT = 0.04  # 镜像后方向仍不符时, 每评分点迁移λ 4%
SWOT_LAMBDA_BOOST_MAX = 0.25        # λ迁移上限 25% (防过度极端)

# ===== 改进#5 (2026-08-21): SWOT迁移参数化 + argmax不穿越 =====
# 依据: swot_calibration_analysis 审视版v1复盘 — 常规迁移(2≤|diff|<6)穿越argmax的
# 场次全部未命中, 且穿越使主推在非强信号下被动翻转。穿越权只保留给强信号分支。
SWOT_TIE_EPS = 0.001  # 常规迁移上限 = 当前argmax − 受益侧 − 此余量 (严格不追平)

# 学习版迁移参数 (learn_swot_shift.py 产出, 三道护栏; applied=false 时零行为变化)
def _load_swot_shift_params():
    """加载学习版迁移参数。仅 applied=true 才覆盖常量, 否则返回None(行为不变)。"""
    p = os.path.join(PREDICTIONS_DIR, 'swot_shift_params.json')
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        if not d.get('applied'):
            return None
        # 完整性校验: 缺一不用 (LRN-20260821-002: 防御坏文件)
        if not all(isinstance(d.get(k), (int, float)) for k in
                   ('k', 'max_shift', 'home_factor', 'away_factor')):
            return None
        return d
    except Exception:
        return None


_SHIFT_PARAMS = _load_swot_shift_params()
if _SHIFT_PARAMS:
    SWOT_SHIFT_PER_POINT = float(_SHIFT_PARAMS['k'])
    SWOT_MAX_SHIFT = float(_SHIFT_PARAMS['max_shift'])
    _SHIFT_HOME_FACTOR = float(_SHIFT_PARAMS['home_factor'])
    _SHIFT_AWAY_FACTOR = float(_SHIFT_PARAMS['away_factor'])
else:
    _SHIFT_HOME_FACTOR = 1.0   # 主客不对称因子 (学习版生效前恒等)
    _SHIFT_AWAY_FACTOR = 1.0


def _parse_pct(v):
    """健壮解析百分比字符串(如 '33%' / '33.5%'), 失败返回0"""
    try:
        return int(float(str(v).replace('%', '').strip()))
    except (ValueError, TypeError):
        return 0


def determine_swot_lean_v3(swot_data):
    """v3: 优化SWOT倾向判断
    
    改进点:
    1. 加权评分: 优势条目数量+质量(关键情报权重更高)
    2. 走势数据作为强信号(>15%差异直接定向)
    3. 交锋记录作为关键因素
    4. 伤停信息作为重要权重
    """
    home_s = swot_data.get('home_strengths', [])
    home_w = swot_data.get('home_weaknesses', [])
    away_s = swot_data.get('away_strengths', [])
    away_w = swot_data.get('away_weaknesses', [])
    trend = swot_data.get('trend', {})
    
    home_pct = _parse_pct(trend.get('home_win_pct', '0%')) if trend else 0
    away_pct = _parse_pct(trend.get('away_win_pct', '0%')) if trend else 0
    
    # 评分系统
    home_score = 0
    away_score = 0
    
    # 1. 条目数量评分 (每条+1)
    home_score += len(home_s) * 1.0
    away_score += len(away_s) * 1.0
    home_score -= len(home_w) * 0.8
    away_score -= len(away_w) * 0.8
    
    # 2. 关键情报加权
    def check_key_intel(items):
        bonus = 0
        for item in items:
            # xG量化条目 (Ultra 15.9: xG多头信号接入) — 放在最前防误匹配
            # 中等权重1.2: λ建模已用xG(被贝叶斯收缩+市场锚稀释), 此处只对
            # 明显差距(≥0.4/0.3)补强, 与λ通道互补而非重复计分
            if 'xG进攻占优' in item:
                bonus += 1.2
            elif 'xG防守占优' in item:
                bonus += 1.2
            elif 'xG压迫占优' in item:
                bonus += 0.8
            # 联赛排名相关 (第1/第2 = 强信号)
            # 修复: 用负向前瞻排除 "第10"/"第20" 等双位数误匹配 (原 '第1' in 会命中'第10')
            elif re.search(r'排名联赛第[12](?!\d)', item):
                bonus += 2.0
            # 进攻/防守极端值
            elif '进球数第1多' in item or '进球数第2多' in item:
                bonus += 1.5
            elif '失球数第1少' in item or '失球数第2少' in item:
                bonus += 1.5
            elif '进球数第1少' in item or '失球数第1多' in item:
                bonus -= 1.5
            # 交锋记录
            elif '交锋' in item or '面对' in item:
                if '占优' in item or '优势' in item or '不败' in item:
                    bonus += 1.5
                elif '下风' in item or '劣势' in item:
                    bonus -= 1.0
            # 伤停信息 (Ultra 13.0: -1.0→-2.0, 核心球员缺阵直接改变进球预期, 场上最直接信号)
            elif '伤' in item or '缺阵' in item or '转会' in item or '流失' in item:
                bonus -= 2.0
            # 状态出色 (Ultra 13.0: +1.0→+1.5, 状态火热直接影响攻防效率)
            elif '出色' in item or '回升' in item or '不败' in item:
                bonus += 1.5
            # 状态低迷 (Ultra 13.0: -0.8→-1.5, 与状态出色对称)
            elif '低迷' in item or '下滑' in item or '一胜难求' in item:
                bonus -= 1.5
        return bonus
    
    home_score += check_key_intel(home_s)
    home_score += check_key_intel(home_w)
    away_score += check_key_intel(away_s)
    away_score += check_key_intel(away_w)
    
    # 3. 走势数据 (强信号)
    trend_diff = home_pct - away_pct
    if abs(trend_diff) > 15:
        if trend_diff > 0:
            home_score += 2.0
        else:
            away_score += 2.0
    elif abs(trend_diff) > 8:
        if trend_diff > 0:
            home_score += 1.0
        else:
            away_score += 1.0
    
    # 4. 最终判断
    diff = home_score - away_score
    
    # 修复: 阈值与 apply_swot_prob_shift 的 draw-boost (abs<SWOT_MIN_DIFF=2.0) 对齐,
    # 否则 1<|diff|<2 时显示"略占优"却把概率往平局调, 自相矛盾
    if diff > 3:
        lean = '主队占优'
    elif diff > 2:
        lean = '主队略占优'
    elif diff < -3:
        lean = '客队占优'
    elif diff < -2:
        lean = '客队略占优'
    else:
        lean = '势均力敌'
    
    return lean, home_score, away_score


def _shorten(text, max_len=25):
    """截断文本"""
    short = text.split('。')[0].split('，')[0]
    return short[:max_len]


# ============================================================
# Ultra 6.4: SWOT概率调整 (logit空间, 通用实现)
# ============================================================

def _parse_wdl_str(p_str):
    """解析 '32%/29%/38%' → [0.32, 0.29, 0.38], 失败返回None"""
    if not p_str or not isinstance(p_str, str):
        return None
    m = re.findall(r'([\d.]+)\s*%', p_str)
    if len(m) != 3:
        return None
    vals = [float(x) / 100 for x in m]
    if any(v <= 0 for v in vals):
        return None
    return vals


def apply_swot_prob_shift(wdl, home_score, away_score):
    """SWOT评分差 → HAD概率迁移 (Ultra 13.3: 支持方向翻转)

    原理: 
      - 评分差大(>=MIN_DIFF): 胜/负之间直接转移概率质量 s, 平局概率固定不变
      - 评分差极大(>=FLIP_DIFF)且方向相反: 翻转方向 — SWOT作为最实时信息
        直接覆盖常规模型(xG/排名)的方向判断, "该翻转时就翻转"
      - 评分差小(<MIN_DIFF): SWOT指向平局, 从胜/负各抽一半概率给平局
      - 有界(常规±20pp/翻转±35pp)、方向恒正确、概率和恒为1
      - 改进#5: 常规迁移不得穿越argmax(追平也不允许, 留1‰余量) — 方向翻转
        只由强信号分支触发; 常规迁移仅加强/削弱既有方向

    参数: wdl = [p_win, p_draw, p_lose]
    返回: (new_wdl, shift, applied)
        applied=False 表示评分差不足未调整, new_wdl原样返回
    """
    diff = home_score - away_score
    if abs(diff) < SWOT_MIN_DIFF:
        # Ultra 9.1: 评分差小 → SWOT指向平局, 微幅提升平局概率
        # 从胜/负各抽最多1.5pp给平局, 相当于平局置信度上调
        w, d, l = wdl
        DRAW_BOOST = 0.015  # 平局上调幅度上限 1.5pp
        max_from_w = max(0.0, w - 0.02)
        max_from_l = max(0.0, l - 0.02)
        boost = min(DRAW_BOOST, max_from_w, max_from_l)
        if boost >= 0.005:
            return [w - boost, d + 2 * boost, l - boost], boost, True
        return list(wdl), 0.0, False

    w, d, l = wdl

    # Ultra 13.3: 强信号方向翻转 (评分差≥FLIP_DIFF 且 SWOT方向与当前主导相反)
    if abs(diff) >= SWOT_FLIP_DIFF:
        cur_dir = _wdl_dir(wdl)
        swot_dir = '胜' if diff > 0 else '负'
        if cur_dir != swot_dir:
            # 从非SWOT方向的胜/负侧迁移, 使SWOT方向反超当前最大方向
            # 平局概率d保持不变 (系统平局系统性低估, 翻转不侵蚀平局)
            cur_max = max(w, d, l)
            if swot_dir == '胜':
                need = cur_max - w + SWOT_FLIP_MARGIN
                shift = min(need, SWOT_FLIP_MAX, max(0.0, l - 0.02))
            else:
                need = cur_max - l + SWOT_FLIP_MARGIN
                shift = min(need, SWOT_FLIP_MAX, max(0.0, w - 0.02))
            if shift >= 0.005:
                if swot_dir == '胜':
                    return [w + shift, d, l - shift], shift, True
                else:
                    return [w - shift, d, l + shift], -shift, True
            return list(wdl), 0.0, False

    # 常规迁移 (评分差在 MIN_DIFF~FLIP_DIFF 之间, 或方向一致时)
    # 改进#5: 主客不对称因子 (学习版, 默认1.0恒等) — 审视版v1发现客队评分有信号、
    # 主队评分近乎无信号(主队优势疑被Elo HFA/λ主场项双重计数), 因子由学习器标定
    _factor = _SHIFT_HOME_FACTOR if diff > 0 else _SHIFT_AWAY_FACTOR
    shift = max(-SWOT_MAX_SHIFT, min(SWOT_MAX_SHIFT, diff * _factor * SWOT_SHIFT_PER_POINT))
    # 边界保护: 任一侧概率不低于2%
    # 注意: max(0.0, ...) 防止 w/l 已低于2% 时 -(w-0.02) 为正导致方向翻转 (M19)
    if shift > 0:
        # 上迁移: 从负(lose)侧取概率, 负侧概率不低于2%
        max_shift_up = max(0.0, l - 0.02)
        shift = min(shift, max_shift_up)
        # 改进#5: argmax不穿越上限 — 胜侧未主导时, 迁至严格低于当前最大侧即止
        # (穿越权只属强信号分支 |diff|>=SWOT_FLIP_DIFF; 常规穿越历史0/6命中)
        cur_max = max(w, d, l)
        if w < cur_max:
            shift = min(shift, max(0.0, cur_max - w - SWOT_TIE_EPS))
        if abs(shift) < 0.005:
            return list(wdl), 0.0, False
    else:
        # 下迁移: 从胜(win)侧取概率, 胜侧概率不低于2%
        max_shift_down = max(0.0, w - 0.02)
        shift = max(shift, -max_shift_down)
        # 改进#5: argmax不穿越上限 (负侧对称)
        cur_max = max(w, d, l)
        if l < cur_max:
            shift = max(shift, -(max(0.0, cur_max - l - SWOT_TIE_EPS)))
        if abs(shift) < 0.005:
            return list(wdl), 0.0, False
    return [w + shift, d, l - shift], shift, True


def _fmt_wdl(wdl):
    return f"{wdl[0]*100:.0f}%/{wdl[1]*100:.0f}%/{wdl[2]*100:.0f}%"


def _wdl_dir(wdl):
    m = max(wdl)
    return '胜' if m == wdl[0] else ('平' if m == wdl[1] else '负')


def _conf_to_score(conf_str):
    """'★★★½' → 3.5"""
    if not conf_str:
        return None
    return conf_str.count('★') + (0.5 if '½' in conf_str else 0)


def _score_to_conf(score):
    """3.5 → '★★★½'"""
    score = max(0.5, min(5.0, round(score * 2) / 2))
    full = int(score)
    return '★' * full + ('½' if score > full else '')


def _propagate_hhad_same_source(result, new_wdl):
    """Ultra 15.3 (2026-08-20): SWOT迁移同源传导 — 用新HAD锚重导出HHAD展示概率

    一个体系铁律: HHAD = HAD锚 × 净胜球条件形状 (margin_shape, 预测时已存)。
    SWOT改写了HAD锚, HHAD若不跟着重导出就退回"两个世界"(HAD改了HHAD没改的旧病,
    实例: 260820周四007 HAD胜53.8%→60% 而HHAD纹丝不动, 同场两套口径)。
    翻转路径已由 _recompute_after_flip 整体重算(λ镜像+全量重导出), 本函数只管非翻转。
    陷阱校准同规则复推 (15.2), 保持降级语义连续; 传导失败静默保持原展示。

    Ultra 15.7: 返回新HHAD概率list[3] (未传导/失败返回None) — 供主推同源重选取用,
    保证重选基准与展示概率严格同源; 陷阱复推优先用 resel_ctx 真实赔率(EV基准,
    与e2e同口径), 旧批次无ctx时退回"-5pp基准+仅e2e触发过才复推"的保守行为。
    """
    _hh = result.get('HHAD') or {}
    if not _hh.get('same_source'):
        return None  # 非同源批次(旧文件/对齐失败): 保持原行为, 不强改
    _ms = _hh.get('margin_shape')
    try:
        _hcap = float(_hh.get('handicap'))
    except (TypeError, ValueError):
        return None
    if not _ms:
        return None
    try:
        import v215_e2e as engine
        _new_hhad, _ok = engine.align_hhad_to_had(new_wdl, None, _hcap, _ms)
        if not _ok:
            return None
        # Ultra 15.7: 陷阱校准 — 有resel_ctx真实赔率按当前条件判定(与e2e同口径);
        # 无ctx(旧批次)仅当预测时触发过才复推(保守, 退化-5pp基准)
        _ctx_odds = ((result.get('cross_market') or {}).get('resel_ctx') or {}).get('hhad_odds')
        if _ctx_odds or _hh.get('trap_cal_note'):
            _new_hhad, _tn = engine.hhad_trap_downgrade(_new_hhad, _ctx_odds, _hcap)
            _hh['trap_cal_note'] = _tn  # None=本次未触发, 诚实清掉旧注记
        _old_p = _hh.get('p', '')
        _hh['p'] = f"{_new_hhad[0]:.0%}/{_new_hhad[1]:.0%}/{_new_hhad[2]:.0%}"
        if 'p_pre_swot' not in _hh:  # 幂等: 重跑不覆盖真原值
            _hh['p_pre_swot'] = _old_p
        _idx = _new_hhad.index(max(_new_hhad))
        _base = ['让胜', '让平', '让负']
        _hh['dir'] = engine._hhad_display_label(_base[_idx], _hcap)
        return list(_new_hhad)
    except Exception:
        return None  # 传导失败保持原展示, 不影响主链路


def _recompute_after_flip(result, swot_dir, home_score, away_score, league=None, had_anchor=None):
    """Ultra 14.0 (2026-08-20): SWOT翻转后 λ/比分Top3/HHAD/半全场 一致性重算

    修复已知瑕疵 (260819复盘#002): SWOT强信号翻转HAD方向后, lam/score.top3/HHAD/
    half_full 仍是旧方向产物, 展示层出现"主推负但比分Top3全偏主队"的自相矛盾。

    重算口径 (复用引擎 compute_scores/compute_half_full, 与主链路同源):
      1. λ 镜像交换 — SWOT判定攻防强弱互换, 总量级保留
         残差增强: 镜像后泊松argmax仍≠SWOT方向时, 每轮按评分差×4%迁移λ (上限25%)
      2. score.top3/high_top3/wdl/main_dir/high_dir/over_*: 新λ重算
      3. HHAD poisson/p/dir: 新hhad_wdl argmax (让球线=玩法定义不变, 受让标签映射同引擎)
      4. half_full main/top3: 新λ重算 (半全场方向同样需对齐)

    返回重算明细dict (写入prob_adjust.recomputed供展示); 解析失败返回None回退旧逻辑。
    """
    # --- 解析原λ ("1.6/1.8") ---
    lam_str = result.get('lam', '')
    try:
        lam_h, lam_a = [float(x) for x in str(lam_str).split('/')]
    except (ValueError, TypeError):
        return None
    if lam_h <= 0 or lam_a <= 0:
        return None

    # --- 让球线 (玩法定义, 翻转不改盘口) ---
    hhad = result.get('HHAD', {}) or {}
    try:
        goal_line = float(hhad.get('handicap'))
    except (TypeError, ValueError):
        goal_line = 0.0

    # --- 主盘口还原: market_gl_str "3/3.5" → 3.25 (fmt_gl逆运算=两段平均) ---
    market_gl = 2.5
    gl_str = (result.get('score', {}) or {}).get('market_gl_str', '')
    try:
        parts = [float(x) for x in str(gl_str).split('/') if x.strip()]
        if parts:
            market_gl = sum(parts) / len(parts)
    except ValueError:
        pass

    # 惰性导入引擎 (仅真翻转时加载; v215_e2e以__main__运行时import为独立实例,
    # 与 swot_auto.py 同模式, 先例验证安全)
    import v215_e2e as engine

    # --- 1. λ镜像交换 + 残差增强 (确保新λ隐含方向与SWOT一致) ---
    new_h, new_a = lam_a, lam_h
    diff = abs(home_score - away_score)
    boosted = False
    scores = None
    for _ in range(3):
        scores = engine.compute_scores(new_h, new_a, goal_line=goal_line,
                                       market_goal_line=market_gl, top_n=3, league=league)
        pw, pd, pl = [v / 100.0 for v in scores['poisson_wdl']]
        _mx = max(pw, pd, pl)
        _cur = '胜' if _mx == pw else ('平' if _mx == pd else '负')
        if _cur == swot_dir:
            break
        adj = min(SWOT_LAMBDA_BOOST_MAX, SWOT_LAMBDA_BOOST_PER_POINT * diff)
        if swot_dir == '胜':
            new_h *= (1 + adj)
            new_a *= (1 - adj)
        else:
            new_a *= (1 + adj)
            new_h *= (1 - adj)
        boosted = True
    if scores is None:
        return None

    # --- 2. score 重写 (口径同引擎第8588行组装) ---
    sc = result.get('score', {})
    if isinstance(sc, dict):
        sc['top3'] = ' '.join(f"{s}:{p}" for s, p in scores['top3_filtered'][:3])
        sc['high_top3'] = ' '.join(f"{s}:{p}" for s, p in scores['high_top3'][:3])
        sc['wdl'] = '/'.join(f"{v}" for v in scores['poisson_wdl'])
        sc['main_dir'] = scores['main_dir']
        sc['high_dir'] = scores.get('high_dir', '')
        sc['over_main'] = scores['over_main']
        sc['over_low'] = scores['over_low']
        sc['over_high'] = scores['over_high']

    # --- 3. HHAD 重算 (让球线不变, 概率随新λ; 受让标签映射同引擎铁律) ---
    # Ultra 15.3: 重算后同样走同源对齐 — HHAD = 翻转后HAD锚 × 新margin_shape,
    # 不再直接用裸Poisson hhad_wdl (避免翻转路径回到双体系)。
    hhad_wdl = scores['hhad_wdl']  # [让胜/让平/让负] 百分数
    hhad_probs = [v / 100.0 for v in hhad_wdl]
    _ss_ok, _al_note, _tc_note = False, None, None
    if had_anchor:
        try:
            _ap, _ss_ok = engine.align_hhad_to_had(had_anchor, hhad_probs, goal_line,
                                                   scores.get('margin_shape'))
            if _ss_ok:
                _al_note = (f"同源对齐: 顶概率{max(hhad_probs)*100:.0f}%→{max(_ap)*100:.0f}%")
                hhad_probs = list(_ap)
                _tp2, _tc_note = engine.hhad_trap_downgrade(hhad_probs, None, goal_line)
                if _tc_note:
                    hhad_probs = list(_tp2)
        except Exception:
            _ss_ok = False
    idx = hhad_probs.index(max(hhad_probs))
    hhad_base_dirs = ['让胜', '让平', '让负']
    new_hhad_dir = engine._hhad_display_label(hhad_base_dirs[idx], goal_line)
    result['HHAD'] = {
        'dir': new_hhad_dir,
        'handicap': hhad.get('handicap'),
        'odds': None,  # 旧odds对应旧方向, 翻转后失效 (与HAD翻转处理一致)
        'odds_note': 'SWOT翻转重算, 赔率未同步, 以体彩当前盘口为准',
        'conf': hhad.get('conf', ''),
        'conf_hit_rate': hhad.get('conf_hit_rate'),
        'p': f"{hhad_probs[0]:.0%}/{hhad_probs[1]:.0%}/{hhad_probs[2]:.0%}",
        'poisson': '/'.join(f"{v}" for v in hhad_wdl),
        # Ultra 15.2/15.3: 同源体系痕迹 (与主链路字段口径一致)
        'same_source': _ss_ok,
        'align_note': _al_note,
        'trap_cal_note': _tc_note,
        'margin_shape': scores.get('margin_shape'),
    }

    # --- 4. 半全场重算 (方向类玩法一并对齐) ---
    try:
        hf = engine.compute_half_full(new_h, new_a, league=league)
        result['half_full'] = {
            'main': hf.get('main', ''),
            'top3': hf.get('top3', ''),
            'recalibrated': True,
        }
    except Exception:
        pass  # 半全场重算失败不影响主链路

    # --- 5. λ 更新 + 痕迹标记 ---
    old_lam = f"{lam_h:.1f}/{lam_a:.1f}"
    result['lam'] = f"{new_h:.1f}/{new_a:.1f}"
    result['lam_calibration'] = {
        'recalibrated': True,
        'original': old_lam,
        'calibrated': f"{new_h:.2f}/{new_a:.2f}",
        'reason': f"SWOT翻转镜像交换{'+残差增强' if boosted else ''}",
    }
    result.setdefault('v611_flags', {})['swot_flip_recomputed'] = True

    return {
        'lam': f"{old_lam}→{result['lam']}",
        'score_top3': sc.get('top3', '') if isinstance(sc, dict) else '',
        'hhad_dir': new_hhad_dir,
        'boosted': boosted,
        # Ultra 15.7: 翻转路径重选基准 — 同源对齐+陷阱校准后的HHAD概率
        # (与result['HHAD']['p']展示严格同源; 对齐失败为None → 跳过主推重选)
        'hhad_probs': list(hhad_probs) if _ss_ok else None,
        'same_source': _ss_ok,
    }


def _reselect_cross_market(result, new_wdl, new_hhad_probs):
    """Ultra 15.7 (2026-08-20 用户裁决): SWOT迁移后主推同源重选 — 消灭选推/展示两时点分裂

    根因: e2e在SWOT迁移【前】完成cross_market选推(主推/双选/纯方向/洞察), 融合改写
    HAD/HHAD展示概率后, 主推仍是旧概率世界的产物 — 同一份报告里 "主推76.6%" 与
    "让球盘72%" 并存 (实例260820周四005); 严重时主推选项本身已不是迁移后体系的
    argmax (周四007: HAD胜53.8%→60% 后仍主推HHAD让负54.8%→40%)。

    方案 (轻量·零逻辑分叉): e2e把重选上下文(两盘h/d/a赔率+λ)存入 cross_market.resel_ctx,
    融合侧调用【同一个引擎函数】compute_cross_market_value 以迁移后概率整体重算 —
    排序规则/陷阱护栏/双选对齐/洞察文案与e2e完全同源, 不存在第二套选推逻辑;
    重算后的cross_market自带新resel_ctx (重选自身可同源重跑; 注意整体重跑融合脚本
    会对HAD p二次迁移, 手动重融合应以未融合的预测文件为基准)。

    守卫(任一不满足→返回None, 保持原推荐, 与旧行为一致):
      · 无 resel_ctx (旧批次文件)
      · HHAD非同源 (对齐失败/未传导 — 概率两世界, 重选无意义)
      · 引擎调用异常

    返回重选痕迹dict {from, to, changed, note} 或 None。
    """
    cm = result.get('cross_market') or {}
    ctx = cm.get('resel_ctx')
    if not ctx or not new_wdl or not new_hhad_probs:
        return None
    hh = result.get('HHAD') or {}
    if not hh.get('same_source'):
        return None
    try:
        import v215_e2e as engine
        # λ: 翻转路径已被 _recompute_after_flip 更新, 优先读 result 当前值
        lam_h, lam_a = (ctx.get('lam') or [None, None])
        try:
            _lh, _la = [float(x) for x in str(result.get('lam', '')).split('/')]
            lam_h, lam_a = _lh, _la
        except (ValueError, TypeError):
            pass
        # handicap 归一化: JSON里的 goalLine 可能为字符串, 引擎 is_integer_handicap
        # 判定要求数值 — 字符串会 TypeError 被 except 吞掉导致重选静默失效
        _hcap = float(hh.get('handicap'))
        new_cm = engine.compute_cross_market_value(
            list(new_wdl), ctx.get('had_odds'), list(new_hhad_probs),
            ctx.get('hhad_odds'), _hcap, lam_h, lam_a,
            mode='prob', difficulty=result.get('difficulty'))
    except Exception:
        return None
    if not isinstance(new_cm, dict) or not new_cm.get('primary_bet'):
        return None
    old_pb = cm.get('primary_bet') or {}
    new_pb = new_cm.get('primary_bet') or {}
    # Ultra 15.8-C (2026-08-21): EV未校准标注跨SWOT重选传递 —
    # ev_uncalibrated/ev_calib_n 由e2e在compute_cross_market_value【之后】单独注入,
    # 重算的new_cm不含这两键; 不回填则SWOT重选过的场次渲染层丢失"⚠未校准"警示
    # (实证260821周五: 11场仅1场带标注, 其余均为SWOT重选清除了标志)。
    for _k in ('ev_uncalibrated', 'ev_calib_n'):
        if _k in cm:
            new_cm[_k] = cm[_k]
    # 痕迹: 预迁移主推存档 (审计/渲染层可追溯"重选前后")
    new_cm['pre_swot_primary'] = {
        'market': old_pb.get('market'), 'option': old_pb.get('option'),
        'prob': old_pb.get('prob')}
    new_cm['swot_resel'] = True
    result['cross_market'] = new_cm
    changed = (old_pb.get('option') != new_pb.get('option'))
    _op, _np = old_pb.get('prob'), new_pb.get('prob')
    if changed:
        note = (f"主推重选: {old_pb.get('option','')}({_op}%)→"
                f"{new_pb.get('option','')}({_np}%)")
    elif (isinstance(_op, (int, float)) and isinstance(_np, (int, float))
            and abs(_np - _op) >= 0.5):
        note = f"主推概率同步: {_op}%→{_np}%"
    else:
        note = ''
    return {'from': f"{old_pb.get('option','')}@{_op}%",
            'to': f"{new_pb.get('option','')}@{_np}%",
            'changed': changed, 'note': note}


def fuse_swot_into_predictions(pred_file):
    """将SWOT数据融合到预测文件中"""
    if not os.path.exists(pred_file):
        print(f"  ❌ 预测文件不存在: {pred_file}")
        return
    
    # 加载预测文件
    with open(pred_file, 'r', encoding='utf-8') as f:
        pred_data = json.load(f)
    
    results = pred_data.get('results', {})
    meta = pred_data.get('meta', {})
    
    # 加载SWOT数据
    swot_all = {}
    if os.path.exists(SWOT_DATA_FILE):
        with open(SWOT_DATA_FILE, 'r', encoding='utf-8') as f:
            swot_raw = json.load(f)
        swot_all = swot_raw.get('matches', {})
    
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    fused_count = 0
    no_swot_count = 0
    
    for key, result in results.items():
        m = meta.get(key, {})
        home = m.get('home', '')
        away = m.get('away', '')
        
        # 尝试匹配SWOT数据
        swot_data = swot_all.get(key, None)
        
        # 如果key没匹配到, 尝试用球队名匹配 (多信号融合)
        if not swot_data:
            try:
                from match_utils import MatchFingerprint, find_best_match
                sp_fp = MatchFingerprint(home=home, away=away)
                candidates = []
                candidate_keys = []
                for swot_key, swot_val in swot_all.items():
                    candidates.append(MatchFingerprint(
                        home=swot_val.get('home_name', ''),
                        away=swot_val.get('away_name', ''),
                    ))
                    candidate_keys.append(swot_key)
                if candidates:
                    best_idx, score = find_best_match(sp_fp, candidates, threshold=0.55)
                    if best_idx is not None:
                        swot_data = swot_all[candidate_keys[best_idx]]
            except ImportError:
                # 回退到旧逻辑: 子串包含
                for swot_key, swot_val in swot_all.items():
                    swot_home = swot_val.get('home_name', '')
                    swot_away = swot_val.get('away_name', '')
                    if (home and swot_home and (home in swot_home or swot_home in home)) and \
                       (away and swot_away and (away in swot_away or swot_away in away)):
                        swot_data = swot_val
                        break
        
        if not swot_data:
            # 无SWOT数据, 标记但不调整
            result['swot'] = {
                'swot_lean': '无SWOT数据',
                'swot_score': 'N/A',
                'consistency': 'N/A',
                'conf_adjust': '无调整',
                'fusion_advice': '⏸ 无SWOT数据, 置信度不调整',
                'source_url': '',
                'fused_at': now_str,
            }
            no_swot_count += 1
            continue
        
        # 计算SWOT倾向
        lean, home_score, away_score = determine_swot_lean_v3(swot_data)
        
        # ===== Ultra 6.4: SWOT直接调整HAD概率 (logit空间) =====
        had = result.get('HAD', {})
        orig_model_dir = had.get('dir', '')  # 一致性判断必须对比调整前的原始方向
        prob_adjust = None
        wdl = _parse_wdl_str(had.get('p', ''))
        if wdl:
            new_wdl, delta, applied = apply_swot_prob_shift(wdl, home_score, away_score)
            # ===== Ultra 13.16 (2026-08-16): 市场优先护栏 — SWOT写回不得推翻四策主推 =====
            # 45场官方核对实证: 与热门分歧且模型领先<15pp时命中仅20%(同场热门40%)。
            # 四策可能把主推拉回热门(偏离门槛/禁平), 而融合P的argmax仍在原方向;
            # SWOT写回时按P argmax重推dir会把主推重新翻离热门, 复活"低门槛偏离"失败模式。
            # 规则: SWOT迁移后的argmax方向 ≠ 当前主推dir 时:
            #   market_first启用 且 该方向≠热门 → 整体弃用(保留原P与dir), 标注拦截
            #   否则(该方向=热门 或 无市场锚) → 照常写回
            if applied:
                _cur_dir = had.get('dir', '')
                _new_d = _wdl_dir(new_wdl)
                if _new_d != _cur_dir:
                    _mf = had.get('market_first') or {}
                    if _mf.get('enabled') and _new_d != _mf.get('fav_dir'):
                        applied = False
                        had['swot_flip_blocked'] = (
                            f"SWOT后P_argmax={_new_d}≠主推{_cur_dir}且非热门{_mf.get('fav_dir')}"
                            f"(λ={_mf.get('lambda_model')}), 按市场优先四策保留主推与原P")
                        result.setdefault('v611_flags', {})['swot_flip_gated'] = True
            if applied:
                old_dir = _wdl_dir(wdl)
                new_dir = _wdl_dir(new_wdl)
                prob_adjust = {
                    'delta': round(delta, 3),
                    'old_p': _fmt_wdl(wdl),
                    'new_p': _fmt_wdl(new_wdl),
                    'old_dir': old_dir,
                    'new_dir': new_dir,
                    'flipped': old_dir != new_dir,
                }
                # 写回HAD概率与方向
                had['p'] = _fmt_wdl(new_wdl)
                had['dir'] = new_dir
                if prob_adjust['flipped']:
                    # 修复(P1-4, Ultra 13.10): 原用 m.get('HAD') 取分项赔率是死代码 (meta 无 'HAD' 键,
                    # 且 HAD 结构只有主推 odds 无 h/d/a 分项), 导致翻转后 odds 仍为旧方向赔率。
                    # 旧方案置 0 → 全部展示层渲染成"胜@0"像坏数据;
                    # 现改为 None + odds_note 说明, 展示层统一渲染"以当前盘口为准"
                    had['odds'] = None
                    had['odds_note'] = 'SWOT翻转方向, 赔率未同步, 以体彩当前盘口为准'
                    # ===== Ultra 14.0 (2026-08-20): 翻转后一致性重算 =====
                    # 修复260819#002瑕疵: 翻转后 λ/比分Top3/HHAD/半全场仍是旧方向产物
                    # ("主推负但比分Top3全偏主队"自相矛盾)。重算失败回退旧的wdl数值同步。
                    _flip_dir = '胜' if home_score > away_score else '负'
                    try:
                        _rc = _recompute_after_flip(
                            result, _flip_dir, home_score, away_score,
                            m.get('league', ''), had_anchor=new_wdl)
                    except Exception:
                        _rc = None
                    if _rc:
                        prob_adjust['recomputed'] = _rc
                # ===== Ultra 15.3 (2026-08-20): SWOT迁移同源传导 =====
                # 非翻转路径: HAD锚迁移后, HHAD按 margin_shape 同源重导出, 消灭
                # "HAD改了HHAD没改"的双体系展示。翻转路径已由 _recompute_after_flip
                # 整体重算(λ镜像+HHAD重导出), 不走此分支。
                # Ultra 15.7: 捕获重导出后的HHAD概率(None=未传导/失败/对齐失败) —
                # 主推重选的基准, 与展示概率严格同源。
                _resel_hhad = None
                if not prob_adjust['flipped']:
                    _resel_hhad = _propagate_hhad_same_source(result, new_wdl)
                else:
                    _resel_hhad = (prob_adjust.get('recomputed') or {}).get('hhad_probs')
                # 同步调整比分矩阵的wdl汇总 (保持顶层一致)
                # Ultra 14.0: 翻转且重算成功时 score 已整体重算(含wdl), 此处仅未翻转/
                # 重算失败时做旧式数值同步
                if not prob_adjust.get('recomputed'):
                    sc = result.get('score', {})
                    if isinstance(sc, dict) and sc.get('wdl'):
                        nums = re.findall(r'([\d.]+)', str(sc['wdl']))
                        if len(nums) == 3:
                            sc_vals = [float(x) / 100 for x in nums]
                            if all(v > 0 for v in sc_vals):
                                new_sc, _, _ = apply_swot_prob_shift(sc_vals, home_score, away_score)
                                sc['wdl'] = f"{new_sc[0]*100:.1f}/{new_sc[1]*100:.1f}/{new_sc[2]*100:.1f}"
                # ===== Ultra 15.7 (2026-08-20 用户裁决): SWOT迁移后主推同源重选 =====
                # 消灭选推/展示两时点分裂: 主推以迁移后(HAD, HHAD)概率用同一引擎函数
                # 整体重算 — 选项可能变(周四007型: HAD胜53.8%→60%后仍主推HHAD让负
                # 54.8%→40%), 也可能只同步概率(周四005型: 主推76.6% vs 展示72%)。
                # 任一守卫不满足 → 保持原推荐, 与旧行为完全一致。
                try:
                    _resel = _reselect_cross_market(result, new_wdl, _resel_hhad)
                except Exception:
                    _resel = None
                if _resel:
                    prob_adjust['primary_reselect'] = _resel
                    result.setdefault('v611_flags', {})['swot_primary_reselected'] = True
        
        # 当前模型方向 (概率调整后)
        model_dir = had.get('dir', '')
        
        # SWOT方向
        if '主队' in lean and '占优' in lean:
            swot_dir = '胜'
        elif '客队' in lean and '占优' in lean:
            swot_dir = '负'
        elif '主队' in lean:
            swot_dir = '胜'
        elif '客队' in lean:
            swot_dir = '负'
        else:
            swot_dir = '平'
        
        # 一致性判断 (对比调整前的原始模型方向, 避免"自己跟自己比")
        # Ultra 13.3: 仅当评分差达到翻转阈值且方向翻转, 才标记"SWOT翻转"
        # (平局提升分支也可能让方向微变, 但那是评分差小<FLIP_DIFF, 不算强信号翻转)
        if (prob_adjust and prob_adjust.get('flipped')
                and abs(home_score - away_score) >= SWOT_FLIP_DIFF):
            consistency = 'SWOT翻转'
            conf_adjust = '+1★'
            fusion_advice = f'🔄 SWOT强信号翻转方向: 模型判{orig_model_dir}→SWOT判{swot_dir}, 置信度上调+1★'
            # Ultra 14.0: 翻转重算详情透出 (λ/比分/HHAD已按新方向对齐)
            _rc = (prob_adjust or {}).get('recomputed')
            if _rc:
                fusion_advice += (f"; λ/比分/HHAD/半全场已重算对齐"
                                  f"(λ{_rc.get('lam','')}, HHAD→{_rc.get('hhad_dir','')})")
        elif swot_dir == orig_model_dir:
            consistency = '一致'
            # 信号强度
            diff = abs(home_score - away_score)
            if diff > 10:
                conf_adjust = '+1★'
                fusion_advice = '✅ SWOT与模型方向一致(信号强), 置信度上调+1★'
            else:
                conf_adjust = '+0.5★'
                fusion_advice = '✅ SWOT与模型方向一致, 置信度上调+0.5★'
        elif orig_model_dir == '平' or swot_dir == '平':
            # Ultra 7.7: 部分一致(含平局)也降级 — SWOT判断平局意味着方向不确定
            # 实证案例: 波兹南SWOT判"势均力敌", 模型判"胜", 实际0-3惨败
            consistency = '部分一致'
            conf_adjust = '-0.5★'
            fusion_advice = '◐ SWOT与模型方向部分一致(含平局分歧), 置信度降低-0.5★'
        else:
            consistency = '不一致'
            conf_adjust = '-0.5★'
            fusion_advice = '⚠️ SWOT与模型方向不一致, 置信度降低-0.5★'
        
        # Ultra 15.7: 主推重选痕迹透出 (选推/展示两时点分裂修复的可见性 —
        # 选项变更与概率同步两种场景都让用户在融合建议里看得到)
        _rs_note = ((prob_adjust or {}).get('primary_reselect') or {}).get('note')
        if _rs_note:
            fusion_advice += f" [{_rs_note}]"
        
        # Ultra 6.4: 置信度调整实际生效 (不再只是建议文案)
        conf_score = _conf_to_score(had.get('conf', ''))
        if conf_score is not None and conf_adjust not in ('无调整',):
            delta_star = float(conf_adjust.replace('★', '').replace('+', '') or 0)
            new_conf_score = max(0.5, min(5.0, conf_score + delta_star))
            if new_conf_score != conf_score:
                had['conf_old'] = had.get('conf')
                had['conf'] = _score_to_conf(new_conf_score)
        
        # Ultra 7.4: 杯赛首回合大比分惩罚 — SWOT融合后仍需遵守置信度封顶
        cup_penalty = result.get('cup_leg_penalty')
        if cup_penalty and cup_penalty.get('applied'):
            conf_cap = cup_penalty.get('conf_cap', 4.0)
            post_conf_score = _conf_to_score(had.get('conf', ''))
            if post_conf_score is not None and post_conf_score > conf_cap:
                had['conf'] = _score_to_conf(conf_cap)
                fusion_advice += f' [杯赛惩罚封顶★★★★]'
        
        # 生成关键因素
        trend = swot_data.get('trend', {})
        trend_str = ''
        if trend:
            trend_str = f"走势{trend.get('total',0)}次主胜{trend.get('home_win_pct','0%')}/客胜{trend.get('away_win_pct','0%')}"
        
        home_key = _shorten(swot_data.get('home_weaknesses', [''])[0]) if swot_data.get('home_weaknesses') else ''
        away_key = _shorten(swot_data.get('away_weaknesses', [''])[0]) if swot_data.get('away_weaknesses') else ''
        
        key_factor = f"{home}{home_key}，{away}{away_key}，{trend_str}"

        # 优化② (Ultra 13.11, 2026-08-16): SWOT小样本标注 — 球队xG样本n<3时,
        # 对该队SWOT条目中的统计性表述(场均/失球/进球/xG)追加"(小样本n=X)"
        # 260816实证: 026马里迪莫"防守稳固: 场均仅失0.0球"来自仅1场数据,
        # 读者会误以为是赛季稳定属性; 标注后信息透明, 由用户自行权衡
        def _annotate_small_sample(items, n_games):
            """给样本敏感条目追加小样本标注, 返回(新列表, 标注数)"""
            if not items or not isinstance(n_games, int) or n_games >= 3:
                return items, 0
            _kw = ('场均', '失球', '进球', 'xG', 'XG')
            out, cnt = [], 0
            for it in items:
                if isinstance(it, str) and any(k in it for k in _kw) and '小样本' not in it:
                    out.append(f"{it}(小样本n={n_games})")
                    cnt += 1
                else:
                    out.append(it)
            return out, cnt

        _xg_res = result.get('xg_data') or {}
        _n_h = (_xg_res.get('home') or {}).get('n_games', 99)
        _n_a = (_xg_res.get('away') or {}).get('n_games', 99)
        _swot_out = {
            'home_strengths': swot_data.get('home_strengths', []),
            'home_weaknesses': swot_data.get('home_weaknesses', []),
            'away_strengths': swot_data.get('away_strengths', []),
            'away_weaknesses': swot_data.get('away_weaknesses', []),
        }
        _annotated = 0
        if isinstance(_n_h, int) and _n_h < 3:
            _swot_out['home_strengths'], _c1 = _annotate_small_sample(_swot_out['home_strengths'], _n_h)
            _swot_out['home_weaknesses'], _c2 = _annotate_small_sample(_swot_out['home_weaknesses'], _n_h)
            _annotated += _c1 + _c2
        if isinstance(_n_a, int) and _n_a < 3:
            _swot_out['away_strengths'], _c3 = _annotate_small_sample(_swot_out['away_strengths'], _n_a)
            _swot_out['away_weaknesses'], _c4 = _annotate_small_sample(_swot_out['away_weaknesses'], _n_a)
            _annotated += _c3 + _c4
        sample_warning = None
        if _annotated > 0:
            _ss_teams = []
            if isinstance(_n_h, int) and _n_h < 3:
                _ss_teams.append(f"{home}(n={_n_h})")
            if isinstance(_n_a, int) and _n_a < 3:
                _ss_teams.append(f"{away}(n={_n_a})")
            sample_warning = (f"⚠️ 小样本警示: {'、'.join(_ss_teams)} xG样本不足3场, "
                              f"其统计性优劣势已标注(共{_annotated}条), 参考价值有限")

        result['swot'] = {
            'home_strengths': _swot_out['home_strengths'],
            'home_weaknesses': _swot_out['home_weaknesses'],
            'away_strengths': _swot_out['away_strengths'],
            'away_weaknesses': _swot_out['away_weaknesses'],
            'swot_lean': lean,
            'swot_score': f'主{home_score:.1f}/客{away_score:.1f}',
            'key_factor': key_factor,
            'swot_dir': swot_dir,
            'model_dir': model_dir,
            'model_dir_orig': orig_model_dir,
            'prob_adjust': prob_adjust,
            'consistency': consistency,
            'conf_adjust': conf_adjust,
            'fusion_advice': fusion_advice,
            'source_url': swot_data.get('swot_url', ''),
            # Ultra 15.9: 情报源构成 (leisu+stats+xg 多源交叉 / 单源) —
            # 多源共识的倾向比单源叙述更可信, 报告层展示
            'intel_source': swot_data.get('source', ''),
            'intel_items': (len(_swot_out['home_strengths']) + len(_swot_out['home_weaknesses']) +
                            len(_swot_out['away_strengths']) + len(_swot_out['away_weaknesses'])),
            'fused_at': now_str,
            'trend': trend,
            'sample_warning': sample_warning,  # 优化②: 小样本警示(None=样本充足)
        }
        fused_count += 1
    
    # 保存
    pred_data['results'] = results
    pred_data['swot_fused_at'] = now_str
    
    with open(pred_file, 'w', encoding='utf-8') as f:
        json.dump(pred_data, f, ensure_ascii=False, indent=1)
    
    print(f"  ✅ SWOT融合完成: {fused_count}场有SWOT数据, {no_swot_count}场无SWOT数据")
    return fused_count, no_swot_count


if __name__ == '__main__':
    import sys
    # 用法: python swot_fusion_v3.py [pred文件路径]
    pred_file = sys.argv[1] if len(sys.argv) > 1 else os.path.join(PREDICTIONS_DIR, 'pred_20260725_周六.json')
    print(f"SWOT融合: {pred_file}")
    fuse_swot_into_predictions(pred_file)
