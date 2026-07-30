#!/usr/bin/env python3
"""优化版预测报告生成器 v2

核心改进:
1. 每场比赛明确标注【第一推荐】和【第二推荐】
2. 综合置信度、概率、SWOT一致性、EV进行排名
3. 直观的HTML报告输出
"""

import json
import os
import sys
from datetime import datetime

# Ultra-Opt: 通用路径 — 优先命令行参数, 缺省 SPORTTERY_WORKSPACE/脚本目录
_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
_PRED_DIR = os.path.join(_WORKSPACE, 'predictions')

# 用法: python gen_report_v2.py [pred文件] [输出html]
PRED_FILE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_PRED_DIR, 'pred_20260725_周六.json')
if len(sys.argv) > 2:
    OUTPUT_HTML = sys.argv[2]
else:
    _base = os.path.basename(PRED_FILE).replace('pred_', 'report_').replace('.json', '.html')
    OUTPUT_HTML = os.path.join(os.path.dirname(PRED_FILE) or _PRED_DIR, _base)

# 报告标题从文件名派生: pred_20260725_周六.json → 2026-07-25 周六
def _derive_title(pred_file):
    name = os.path.basename(pred_file).replace('pred_', '').replace('.json', '')
    parts = name.split('_')
    date_part = parts[0]
    wd_part = parts[1] if len(parts) > 1 else ''
    if len(date_part) == 8 and date_part.isdigit():
        return f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:]} {wd_part}".strip()
    return name

REPORT_TITLE = _derive_title(PRED_FILE)


def parse_conf_stars(conf_str):
    """解析置信度星号 -> 数值 (0-5)"""
    if not conf_str:
        return 0
    count = 0
    for c in conf_str:
        if c == '★':
            count += 1
        elif c == '½':
            count += 0.5
    return count


def parse_prob_from_p(p_str, direction):
    """从p字符串提取概率, 如 '30%/29%/41%' direction='负' -> 41"""
    if not p_str:
        return 0
    parts = p_str.split('/')
    if len(parts) != 3:
        return 0
    dir_map = {'胜': 0, '平': 1, '负': 2}
    idx = dir_map.get(direction, 0)
    try:
        return float(parts[idx].replace('%', ''))
    except:
        return 0


def score_option(name, market, direction, odds, conf_str, prob, swot_consistency, ev_pct, coverage_type='单选'):
    """综合评分一个投注选项
    
    评分 = 置信度×15 + 概率×0.4 + SWOT加成 + EV微调 + 覆盖加成
    """
    conf = parse_conf_stars(conf_str)
    
    # 置信度权重最高
    conf_score = conf * 15
    
    # 概率
    prob_score = prob * 0.4
    
    # SWOT一致性加成
    swot_bonus = 0
    if swot_consistency == '一致':
        swot_bonus = 15
    elif swot_consistency == '不一致':
        swot_bonus = -12
    elif swot_consistency == '部分一致':
        swot_bonus = 0
    
    # EV微调 (负EV轻微扣分)
    ev_penalty = 0
    if ev_pct is not None and ev_pct < 0:
        ev_penalty = -abs(ev_pct) * 0.15
    
    # 覆盖加成: 双选覆盖面广, 适合做第二推荐
    coverage_bonus = 0
    if '双选' in coverage_type:
        coverage_bonus = 8
    
    total = conf_score + prob_score + swot_bonus + ev_penalty + coverage_bonus
    
    return {
        'name': name,
        'market': market,
        'direction': direction,
        'odds': odds,
        'conf': conf_str,
        'conf_num': conf,
        'prob': prob,
        'swot_consistency': swot_consistency,
        'ev_pct': ev_pct,
        'coverage_type': coverage_type,
        'score': round(total, 1),
        'conf_score': round(conf_score, 1),
        'prob_score': round(prob_score, 1),
        'swot_bonus': swot_bonus,
        'ev_penalty': round(ev_penalty, 1),
        'coverage_bonus': coverage_bonus,
    }


def rank_match(key, meta, result):
    """对一场比赛的所有投注选项进行排名
    
    改进: 
    1. 去重 - HAD方向和pure_direction_bet相同则合并
    2. 第一推荐和第二推荐必须不同类型(市场不同或单选/双选不同)
    """
    home = meta.get('home', '')
    away = meta.get('away', '')
    league = meta.get('league', '')
    match_time = meta.get('match_time', '')
    
    had = result.get('HAD', {})
    hhad = result.get('HHAD', {})
    cm = result.get('cross_market', {})
    swot = result.get('swot', {})
    swot_consistency = swot.get('consistency', 'N/A')
    swot_lean = swot.get('swot_lean', '无SWOT数据')
    swot_adjust = swot.get('conf_adjust', '无调整')
    swot_key_factor = swot.get('key_factor', '')
    swot_prob_adjust = swot.get('prob_adjust')  # Ultra 6.4: SWOT概率调整记录
    data_source = meta.get('data_source', '')   # nowscore / 500.com
    
    # 调整后的置信度 (考虑SWOT调整)
    swot_adjust_val = 0
    if '+1★' in swot_adjust:
        swot_adjust_val = 1
    elif '+0.5★' in swot_adjust:
        swot_adjust_val = 0.5
    elif '-0.5★' in swot_adjust:
        swot_adjust_val = -0.5
    
    def apply_swot_adj(conf_str, conf_old_present):
        """应用SWOT调整到置信度星号
        Ultra 6.4: 融合时已把调整写回conf(并存conf_old), 此处不得重复调整;
        仅旧版融合(仅建议文案未写回)的文件才在此处应用"""
        if swot_adjust_val == 0 or conf_old_present:
            return conf_str
        base = parse_conf_stars(conf_str)
        adjusted = max(0, min(5, base + swot_adjust_val))
        return '★' * int(adjusted) + ('½' if adjusted % 1 == 0.5 else '')
    
    options = []
    seen_keys = set()  # 去重用
    
    # 1. HAD纯方向 (含cross_market的pure_direction_bet, 合并去重)
    had_dir = had.get('dir', '')
    had_odds = had.get('odds', 0)
    had_conf = had.get('conf', '')
    had_p = had.get('p', '')
    had_prob = parse_prob_from_p(had_p, had_dir)
    had_ev = result.get('kelly', {}).get('HAD', {}).get('ev', 0)
    had_conf_adj = apply_swot_adj(had_conf, 'conf_old' in had)
    
    # 检查pure_direction_bet是否和HAD方向相同
    pdb = cm.get('pure_direction_bet', {})
    pdb_option = pdb.get('option', '')
    pdb_is_same = False
    if pdb_option:
        # 去除空格比较: "HAD负" vs "HAD 负"
        norm_pdb = pdb_option.replace(' ', '')
        norm_had = f'HAD{had_dir}'
        if norm_pdb == norm_had:
            pdb_is_same = True
    
    had_name = f'HAD {had_dir}'
    had_key = f'HAD_{had_dir}'
    if had_key not in seen_keys:
        seen_keys.add(had_key)
        # 如果pure_direction_bet和HAD相同, 用概率更高的那个
        best_prob = had_prob
        if pdb_is_same:
            pdb_prob = pdb.get('prob', 0)
            if pdb_prob > best_prob:
                best_prob = pdb_prob
        options.append(score_option(
            had_name, '胜平负', had_dir, had_odds,
            had_conf_adj, best_prob, swot_consistency, had_ev, '单选'
        ))
    
    # 2. HHAD让球方向
    hhad_dir = hhad.get('dir', '')
    hhad_odds = hhad.get('odds', 0)
    hhad_conf = hhad.get('conf', '')
    hhad_p = hhad.get('p', '')
    hhad_prob = parse_prob_from_p(hhad_p, hhad_dir)
    hhad_ev = result.get('kelly', {}).get('HHAD', {}).get('ev', 0)
    hhad_handicap = hhad.get('handicap', 0)
    hhad_conf_adj = apply_swot_adj(hhad_conf, 'conf_old' in hhad)
    
    hhad_key = f'HHAD_{hhad_dir}'
    if hhad_key not in seen_keys:
        seen_keys.add(hhad_key)
        options.append(score_option(
            f'HHAD {hhad_dir} (让{hhad_handicap})', '让球胜平负', hhad_dir, hhad_odds,
            hhad_conf_adj, hhad_prob, swot_consistency, hhad_ev, '单选'
        ))
    
    # 3. 双选保险
    dr = cm.get('double_recommend') or {}
    dr_option = dr.get('option', '') if dr else ''
    dr_odds = dr.get('odds', 0) if dr else 0
    dr_prob = dr.get('prob', 0) if dr else 0
    dr_ev = dr.get('ev_pct', 0) if dr else 0
    dr_dir = dr.get('direction', '') if dr else ''
    
    if dr_option:
        dr_key = f'DOUBLE_{dr_dir}'
        if dr_key not in seen_keys:
            seen_keys.add(dr_key)
            options.append(score_option(
                dr_option, 'HAD双选', dr_dir, dr_odds,
                '★★★', dr_prob, swot_consistency, dr_ev, '双选'
            ))
    
    # 4. 纯方向投注 (仅当与HAD方向不同时才添加)
    if pdb_option and not pdb_is_same:
        pdb_odds = pdb.get('odds', 0)
        pdb_prob = pdb.get('prob', 0)
        pdb_ev = pdb.get('ev_pct', 0)
        pdb_sel_type = pdb.get('selection_type', '')
        pdb_key = f'PDB_{pdb_option}'
        if pdb_key not in seen_keys:
            seen_keys.add(pdb_key)
            options.append(score_option(
                pdb_option, 'HAD方向', had_dir, pdb_odds,
                had_conf_adj, pdb_prob, swot_consistency, pdb_ev,
                '真单选' if '真' in pdb_sel_type else '单选'
            ))
    
    # 按分数排序
    options.sort(key=lambda x: x['score'], reverse=True)
    
    # Ultra 6.5: 推荐规则改为纯概率排序 (用户决策)
    # 第一推 = HAD/HHAD单选中概率最高, 第二推 = 概率第二高
    # 不再强制不同类型/不同市场 — 概率是唯一标准 (EV仅供参考展示)
    single_opts = [o for o in options if '双选' not in o.get('coverage_type', '')]
    single_opts.sort(key=lambda x: x.get('prob', 0), reverse=True)
    first = single_opts[0] if single_opts else (options[0] if options else None)
    second = single_opts[1] if len(single_opts) > 1 else (options[1] if len(options) > 1 else None)
    
    # 额外信息
    goals = result.get('goals', {})
    score_info = result.get('score', {})
    half_full = result.get('half_full', {})
    total_goals = result.get('total_goals', {})
    data_quality = result.get('data_quality', {})
    
    return {
        'key': key,
        'home': home,
        'away': away,
        'league': league,
        'match_time': match_time,
        'first': first,
        'second': second,
        'all_options': options,
        'swot_lean': swot_lean,
        'swot_adjust': swot_adjust,
        'swot_key_factor': swot_key_factor,
        'swot_prob_adjust': swot_prob_adjust,
        'data_source': data_source,
        'goals': goals,
        'score_info': score_info,
        'half_full': half_full,
        'total_goals': total_goals,
        'sporttery_pools': result.get('sporttery_pools'),  # Ultra 6.5: 竞彩官方玩法EV
        'data_quality': data_quality,
        'difficulty': result.get('difficulty', 0),
        'insight': cm.get('insight', ''),
    }


def generate_html_report(matches):
    """生成HTML报告"""
    
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>预测报告 {REPORT_TITLE}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif; background: #0f1117; color: #e0e0e0; padding: 20px; }}
.header {{ text-align: center; padding: 30px 0; border-bottom: 2px solid #2a2d35; margin-bottom: 30px; }}
.header h1 {{ font-size: 28px; color: #fff; margin-bottom: 8px; }}
.header .meta {{ color: #888; font-size: 14px; }}
.match-card {{ background: #1a1d24; border-radius: 12px; margin-bottom: 20px; overflow: hidden; border: 1px solid #2a2d35; }}
.match-header {{ display: flex; justify-content: space-between; align-items: center; padding: 16px 24px; background: #222631; border-bottom: 1px solid #2a2d35; }}
.match-title {{ font-size: 18px; font-weight: bold; color: #fff; }}
.match-info {{ font-size: 13px; color: #888; }}
.match-body {{ padding: 20px 24px; }}
.rec-row {{ display: flex; gap: 16px; margin-bottom: 16px; }}
.rec-box {{ flex: 1; border-radius: 10px; padding: 16px; position: relative; }}
.rec-first {{ background: linear-gradient(135deg, #1a3a1a, #0d2818); border: 2px solid #2ecc71; }}
.rec-second {{ background: linear-gradient(135deg, #1a2a3a, #0d1d28); border: 2px solid #3498db; }}
.rec-label {{ font-size: 12px; font-weight: bold; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }}
.rec-first .rec-label {{ color: #2ecc71; }}
.rec-second .rec-label {{ color: #3498db; }}
.rec-main {{ display: flex; align-items: baseline; gap: 12px; margin-bottom: 8px; }}
.rec-direction {{ font-size: 24px; font-weight: bold; color: #fff; }}
.rec-odds {{ font-size: 18px; color: #f39c12; }}
.rec-conf {{ font-size: 16px; }}
.rec-details {{ display: flex; gap: 16px; font-size: 13px; color: #aaa; flex-wrap: wrap; }}
.rec-detail-item {{ display: flex; gap: 4px; }}
.rec-detail-label {{ color: #666; }}
.rec-detail-value {{ color: #ccc; }}
.rec-score {{ position: absolute; top: 8px; right: 12px; font-size: 11px; color: #555; }}
.swot-bar {{ background: #161821; border-radius: 8px; padding: 12px 16px; margin-bottom: 12px; font-size: 13px; display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }}
.swot-tag {{ padding: 3px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }}
.swot-consistent {{ background: #1a3a1a; color: #2ecc71; }}
.swot-inconsistent {{ background: #3a1a1a; color: #e74c3c; }}
.swot-partial {{ background: #2a2a1a; color: #f1c40f; }}
.swot-none {{ background: #222; color: #888; }}
.extra-info {{ display: flex; gap: 12px; font-size: 12px; color: #888; flex-wrap: wrap; margin-top: 8px; padding-top: 8px; border-top: 1px solid #2a2d35; }}
.extra-item {{ background: #161821; padding: 4px 10px; border-radius: 6px; }}
.summary-table {{ background: #1a1d24; border-radius: 12px; margin-bottom: 20px; overflow: hidden; border: 1px solid #2a2d35; }}
.summary-table table {{ width: 100%; border-collapse: collapse; }}
.summary-table th {{ background: #222631; padding: 12px 16px; text-align: left; font-size: 13px; color: #888; border-bottom: 1px solid #2a2d35; }}
.summary-table td {{ padding: 12px 16px; font-size: 14px; border-bottom: 1px solid #1e2128; }}
.summary-table tr:hover td {{ background: #1e2128; }}
.dir-tag {{ padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; }}
.dir-win {{ background: #1a3a1a; color: #2ecc71; }}
.dir-draw {{ background: #2a2a1a; color: #f1c40f; }}
.dir-lose {{ background: #3a1a1a; color: #e74c3c; }}
.section-title {{ font-size: 20px; color: #fff; margin: 30px 0 16px; padding-left: 12px; border-left: 4px solid #3498db; }}
</style>
</head>
<body>
<div class="header">
<h1>⚽ 预测报告 · {REPORT_TITLE}</h1>
<div class="meta">共 {len(matches)} 场 · 生成时间 {now_str} · 数据源: nowscore/500.com + SWOT融合</div>
</div>
"""
    
    # 汇总表
    html += '<div class="section-title">📊 全场汇总</div>\n'
    html += '<div class="summary-table"><table>\n'
    html += '<tr><th>场次</th><th>联赛</th><th>主队</th><th>客队</th><th>第一推荐</th><th>赔率</th><th>第二推荐</th><th>赔率</th><th>SWOT</th></tr>\n'
    
    for m in matches:
        first = m['first']
        second = m['second']
        
        dir_class = ''
        if first:
            d = first['direction']
            if '胜' in d: dir_class = 'dir-win'
            elif '平' in d: dir_class = 'dir-draw'
            elif '负' in d: dir_class = 'dir-lose'
        
        swot_class = 'swot-none'
        swot_text = m['swot_lean']
        if m['swot_lean'] == '无SWOT数据':
            swot_class = 'swot-none'
        elif '一致' in m.get('swot_adjust', ''):
            if '+' in m.get('swot_adjust', ''):
                swot_class = 'swot-consistent'
        
        html += f'<tr>'
        html += f'<td>{m["key"]}</td>'
        html += f'<td>{m["league"]}</td>'
        html += f'<td>{m["home"]}</td>'
        html += f'<td>{m["away"]}</td>'
        if first:
            html += f'<td><span class="dir-tag {dir_class}">{first["name"]}</span></td>'
            html += f'<td style="color:#f39c12">{first["odds"]}</td>'
        else:
            html += '<td>-</td><td>-</td>'
        if second:
            html += f'<td><span class="dir-tag" style="background:#1a2a3a;color:#3498db">{second["name"]}</span></td>'
            html += f'<td style="color:#f39c12">{second["odds"]}</td>'
        else:
            html += '<td>-</td><td>-</td>'
        html += f'<td><span class="swot-tag {swot_class}">{swot_text}</span></td>'
        html += '</tr>\n'
    
    html += '</table></div>\n'
    
    # 详细卡片
    html += '<div class="section-title">🎯 逐场推荐详情</div>\n'
    
    for m in matches:
        first = m['first']
        second = m['second']
        
        # SWOT tag class
        swot_class = 'swot-none'
        if m['swot_lean'] == '无SWOT数据':
            swot_class = 'swot-none'
        elif '不一致' in m.get('swot_adjust', ''):
            swot_class = 'swot-inconsistent'
        elif '一致' in m.get('swot_adjust', '') and '+' in m.get('swot_adjust', ''):
            swot_class = 'swot-consistent'
        elif '部分' in m.get('swot_adjust', ''):
            swot_class = 'swot-partial'
        else:
            swot_class = 'swot-consistent'
        
        html += f'''<div class="match-card">
<div class="match-header">
<div class="match-title">{m["key"]} · {m["home"]} vs {m["away"]}</div>
<div class="match-info">{m["league"]} · {m["match_time"]} · {m["data_source"] or "?"}</div>
</div>
<div class="match-body">
<div class="swot-bar">
<span class="swot-tag {swot_class}">SWOT: {m["swot_lean"]}</span>
<span style="color:#888">置信调整: {m["swot_adjust"]}</span>
'''
        
        # Ultra 6.4: SWOT概率调整徽标
        pa = m.get('swot_prob_adjust')
        if pa:
            if pa.get('flipped'):
                html += f'<span class="swot-tag" style="background:#3a2a1a;color:#f39c12">🔄 SWOT翻转: {pa["old_dir"]}→{pa["new_dir"]} ({pa["old_p"]}→{pa["new_p"]})</span>\n'
            else:
                html += f'<span style="color:#777">概率调整: {pa["old_p"]}→{pa["new_p"]}</span>\n'
        
        if m['swot_key_factor']:
            html += f'<span style="color:#999">关键因素: {m["swot_key_factor"]}</span>\n'
        
        html += '</div>\n'
        
        # 推荐框
        html += '<div class="rec-row">\n'
        
        # 第一推荐
        if first:
            html += f'''<div class="rec-box rec-first">
<div class="rec-label">🥇 第一推荐</div>
<div class="rec-main">
<span class="rec-direction">{first["name"]}</span>
<span class="rec-odds">@{first["odds"]}</span>
<span class="rec-conf">{first["conf"]}</span>
</div>
<div class="rec-details">
<span class="rec-detail-item"><span class="rec-detail-label">概率:</span><span class="rec-detail-value">{first["prob"]:.1f}%</span></span>
<span class="rec-detail-item"><span class="rec-detail-label">市场:</span><span class="rec-detail-value">{first["market"]}</span></span>
<span class="rec-detail-item"><span class="rec-detail-label">类型:</span><span class="rec-detail-value">{first["coverage_type"]}</span></span>
<span class="rec-detail-item"><span class="rec-detail-label">EV:</span><span class="rec-detail-value">{first["ev_pct"]:.1f}%</span></span>
</div>
<div class="rec-score">评分 {first["score"]}</div>
</div>
'''
        
        # 第二推荐
        if second:
            html += f'''<div class="rec-box rec-second">
<div class="rec-label">🥈 第二推荐</div>
<div class="rec-main">
<span class="rec-direction">{second["name"]}</span>
<span class="rec-odds">@{second["odds"]}</span>
<span class="rec-conf">{second["conf"]}</span>
</div>
<div class="rec-details">
<span class="rec-detail-item"><span class="rec-detail-label">概率:</span><span class="rec-detail-value">{second["prob"]:.1f}%</span></span>
<span class="rec-detail-item"><span class="rec-detail-label">市场:</span><span class="rec-detail-value">{second["market"]}</span></span>
<span class="rec-detail-item"><span class="rec-detail-label">类型:</span><span class="rec-detail-value">{second["coverage_type"]}</span></span>
<span class="rec-detail-item"><span class="rec-detail-label">EV:</span><span class="rec-detail-value">{second["ev_pct"]:.1f}%</span></span>
</div>
<div class="rec-score">评分 {second["score"]}</div>
</div>
'''
        
        html += '</div>\n'  # end rec-row
        
        # 额外信息
        goals = m.get('goals', {})
        score_info = m.get('score_info', {})
        half_full = m.get('half_full', {})
        total_goals = m.get('total_goals', {})
        data_quality = m.get('data_quality', {})
        
        html += '<div class="extra-info">\n'
        if goals:
            html += f'<span class="extra-item">🎯 预期进球: {goals.get("home_expected","")}-{goals.get("away_expected","")} (总{goals.get("total_expected","")})</span>\n'
            html += f'<span class="extra-item">📊 大小: {goals.get("over_under","")} {score_info.get("market_gl_str","")}</span>\n'
        if half_full:
            html += f'<span class="extra-item">半全场: {half_full.get("main","")}</span>\n'
        if total_goals:
            html += f'<span class="extra-item">总进球: {total_goals.get("main","")}</span>\n'
        if data_quality:
            html += f'<span class="extra-item">数据质量: {data_quality.get("quality","")} ({data_quality.get("score","")})</span>\n'
        html += f'<span class="extra-item">难度: {m.get("difficulty",0):.1f}</span>\n'
        html += '</div>\n'
        
        # 所有选项对比
        html += '<div style="margin-top:12px;font-size:12px;color:#666;">\n'
        html += '所有选项评分: '
        for opt in m['all_options']:
            html += f'{opt["name"]}@{opt["odds"]}({opt["score"]}) | '
        html += '</div>\n'
        
        html += '</div>\n'  # end match-body
        html += '</div>\n'  # end match-card
    
    html += '</body>\n</html>\n'
    
    return html


def main():
    with open(PRED_FILE, 'r', encoding='utf-8') as f:
        pred_data = json.load(f)
    
    results = pred_data.get('results', {})
    meta = pred_data.get('meta', {})
    
    matches = []
    for key in sorted(results.keys()):
        m = rank_match(key, meta.get(key, {}), results[key])
        matches.append(m)
    
    # 打印排名结果
    print("=" * 80)
    print("预测报告 v2 - 第一推荐/第二推荐排名")
    print("=" * 80)
    for m in matches:
        print(f"\n{m['key']} {m['home']} vs {m['away']} [{m['league']}]")
        print(f"  SWOT: {m['swot_lean']} 调整:{m['swot_adjust']}")
        if m['first']:
            f = m['first']
            print(f"  🥇 第一: {f['name']} @{f['odds']} {f['conf']} P={f['prob']:.1f}% 评分={f['score']}")
        if m['second']:
            s = m['second']
            print(f"  🥈 第二: {s['name']} @{s['odds']} {s['conf']} P={s['prob']:.1f}% 评分={s['score']}")
        all_opts = ' | '.join('{}@{}({})'.format(o['name'], o['odds'], o['score']) for o in m['all_options'])
        print(f"  全部选项: {all_opts}")
    
    # 生成HTML
    html = generate_html_report(matches)
    with open(OUTPUT_HTML, 'w', encoding='utf-8') as f:
        f.write(html)
    
    print(f"\n✅ HTML报告已生成: {OUTPUT_HTML}")


if __name__ == '__main__':
    main()
