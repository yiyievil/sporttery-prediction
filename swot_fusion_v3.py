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
SWOT_MAX_SHIFT = 0.08         # 迁移上限 ±8pp
SWOT_MIN_DIFF = 2.0           # 评分差低于此值不调整 (噪音区)


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
    
    home_pct = int(trend.get('home_win_pct', '0%').replace('%', '')) if trend else 0
    away_pct = int(trend.get('away_win_pct', '0%').replace('%', '')) if trend else 0
    
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
            # 联赛排名相关 (第1/第2 = 强信号)
            if '排名联赛第1' in item or '排名联赛第2' in item:
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
            # 伤停信息
            elif '伤' in item or '缺阵' in item or '转会' in item or '流失' in item:
                bonus -= 1.0
            # 状态出色
            elif '出色' in item or '回升' in item or '不败' in item:
                bonus += 1.0
            # 状态低迷
            elif '低迷' in item or '下滑' in item or '一胜难求' in item:
                bonus -= 0.8
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
    
    if diff > 3:
        lean = '主队占优'
    elif diff > 1:
        lean = '主队略占优'
    elif diff < -3:
        lean = '客队占优'
    elif diff < -1:
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
    """SWOT评分差 → HAD概率迁移 (Ultra 9.1: 支持平局调整)

    原理: 
      - 评分差大(>=MIN_DIFF): 胜/负之间直接转移概率质量 s, 平局概率固定不变
      - 评分差小(<MIN_DIFF): SWOT指向平局, 从胜/负各抽一半概率给平局
      - 有界(±8pp)、方向恒正确、概率和恒为1

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
    shift = max(-SWOT_MAX_SHIFT, min(SWOT_MAX_SHIFT, diff * SWOT_SHIFT_PER_POINT))
    w, d, l = wdl
    # 边界保护: 任一侧概率不低于2%
    # 注意: max(0.0, ...) 防止 w/l 已低于2% 时 -(w-0.02) 为正导致方向翻转 (M19)
    if shift > 0:
        # 上迁移: 从负(lose)侧取概率, 负侧概率不低于2%
        max_shift_up = max(0.0, l - 0.02)
        shift = min(shift, max_shift_up)
        if abs(shift) < 0.005:
            return list(wdl), 0.0, False
    else:
        # 下迁移: 从胜(win)侧取概率, 胜侧概率不低于2%
        max_shift_down = max(0.0, w - 0.02)
        shift = max(shift, -max_shift_down)
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
        
        # 如果key没匹配到, 尝试用球队名匹配
        if not swot_data:
            for swot_key, swot_val in swot_all.items():
                swot_home = swot_val.get('home_name', '')
                swot_away = swot_val.get('away_name', '')
                # 模糊匹配球队名
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
                    # 方向翻转时更新赔率为新方向的赔率
                    meta_had = m.get('HAD', {})
                    odds_map = {'胜': 'h', '平': 'd', '负': 'a'}
                    if isinstance(meta_had, dict) and odds_map.get(new_dir) in meta_had:
                        had['odds'] = meta_had[odds_map[new_dir]]
                # 同步调整比分矩阵的wdl汇总 (保持顶层一致)
                sc = result.get('score', {})
                if isinstance(sc, dict) and sc.get('wdl'):
                    nums = re.findall(r'([\d.]+)', str(sc['wdl']))
                    if len(nums) == 3:
                        sc_vals = [float(x) / 100 for x in nums]
                        if all(v > 0 for v in sc_vals):
                            new_sc, _, _ = apply_swot_prob_shift(sc_vals, home_score, away_score)
                            sc['wdl'] = f"{new_sc[0]*100:.1f}/{new_sc[1]*100:.1f}/{new_sc[2]*100:.1f}"
        
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
        if swot_dir == orig_model_dir:
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
        
        result['swot'] = {
            'home_strengths': swot_data.get('home_strengths', []),
            'home_weaknesses': swot_data.get('home_weaknesses', []),
            'away_strengths': swot_data.get('away_strengths', []),
            'away_weaknesses': swot_data.get('away_weaknesses', []),
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
            'fused_at': now_str,
            'trend': trend,
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
