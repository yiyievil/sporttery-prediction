#!/usr/bin/env python3
"""
竞彩足球预测 PDF 生成器 — 至尊版 (每场一整页)
=============================================
设计理念: 顶级金融杂志风格, 每场比赛独占一页
  - 深蓝+金色奢华配色
  - 全幅跨页头图式标题栏
  - 卡片式信息区块 + 精致装饰线
  - 大标题、大留白、清晰视觉流(但每场严格1页)
  - 手机 / 平板阅读优化

用法: python3 gen_pred_pdf.py <json_path> [output_path]
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

# ── 北京时间 (UTC+8): 系统时区为UTC, 生成时间显示给用户必须换算北京时间 ──
_BEIJING_TZ = timezone(timedelta(hours=8))
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, Flowable,
)

from pdf_fonts import register_cjk_font

# ── 字体注册 ──
# 复用公共模块 pdf_fonts (优先霞鹜文楷, 缺失回退其他CJK字体), 避免 import 崩溃
register_cjk_font(bold_name='CJKBold')

# ====================================================================
# 配色方案
# ====================================================================
C_NAVY       = HexColor('#0a1628')
C_NAVY_MID   = HexColor('#112240')
C_DARK_BLUE  = HexColor('#0f2b4a')
C_GOLD       = HexColor('#c7922e')
C_GOLD_LIGHT = HexColor('#f5e7c8')
C_GOLD_PALE  = HexColor('#faf3e0')
C_WHITE      = HexColor('#ffffff')
C_CREAM      = HexColor('#faf8f5')
C_CARD       = HexColor('#ffffff')
C_BORDER     = HexColor('#e0dcd3')
C_BORDER_LT  = HexColor('#f0ede6')
C_TEXT       = HexColor('#1a1a2e')
C_TEXT_LIGHT = HexColor('#4a4a5e')
C_MUTED      = HexColor('#8a8a9a')
C_GREEN      = HexColor('#0d9488')
C_GREEN_BG   = HexColor('#ecfdf5')
C_GREEN_DARK = HexColor('#065f46')
C_AMBER      = HexColor('#b45309')
C_AMBER_BG   = HexColor('#fffbeb')
C_RED        = HexColor('#dc2626')
C_TEAL       = HexColor('#0e7490')
C_GOLD_STAR  = HexColor('#c7922e')
C_GOLD_DARK  = HexColor('#8b6914')
C_HEADER_BG  = HexColor('#0a1628')
C_FOOTER_BG  = HexColor('#f0ede6')
C_PAGE_BG    = HexColor('#f7f5f0')
C_COVER_BG   = HexColor('#0a1628')

# ====================================================================
# 页面设置 — 紧凑但保留高端感
# ====================================================================
PAGE_W, PAGE_H = A4
LM = 11 * mm
RM = 11 * mm
TM = 8 * mm
BM = 8 * mm
CW = PAGE_W - LM - RM  # ≈ 188mm
CONTENT_H = PAGE_H - TM - BM  # ≈ 281mm

# ====================================================================
# 装饰性 Flowable 元素
# ====================================================================
class GoldLine(Flowable):
    def __init__(self, width, height=1.0):
        Flowable.__init__(self)
        self.width = width
        self.height = height
    def draw(self):
        self.canv.setStrokeColor(C_GOLD)
        self.canv.setLineWidth(self.height)
        self.canv.line(0, 0, self.width, 0)

class DoubleGoldLine(Flowable):
    def __init__(self, width):
        Flowable.__init__(self)
        self.width = width
        self.height = 3.5
    def draw(self):
        c = self.canv
        c.setStrokeColor(C_GOLD)
        c.setLineWidth(1.2)
        c.line(0, 2.2, self.width, 2.2)
        c.line(0, 0, self.width, 0)

# ====================================================================
# 样式系统 — 稍紧凑但保留高品质感
# ====================================================================
def make_style(name, font='CJK', size=12, leading=None, color=C_TEXT, align=TA_LEFT,
               space_before=0, space_after=2, bold=False):
    return ParagraphStyle(
        name, fontName='CJKBold' if bold else font, fontSize=size,
        leading=leading or size * 1.55, textColor=color, alignment=align,
        spaceBefore=space_before, spaceAfter=space_after, wordWrap='CJK',
    )

S = {
    # 封面
    'cover_title': make_style('CoverTitle', bold=True, size=42, leading=52,
                               color=C_GOLD, align=TA_CENTER, space_after=0),
    'cover_sub': make_style('CoverSub', bold=True, size=18, leading=26,
                             color=HexColor('#8899bb'), align=TA_CENTER, space_after=0),
    'cover_date': make_style('CoverDate', size=16, leading=24,
                              color=HexColor('#aabbcc'), align=TA_CENTER, space_after=0),
    'cover_match': make_style('CoverMatch', size=14, leading=22,
                               color=HexColor('#ccddee'), align=TA_CENTER, space_after=0),
    'cover_footer': make_style('CoverFooter', size=10, leading=15,
                                color=HexColor('#667788'), align=TA_CENTER, space_after=0),

    # 比赛标识 (无标题栏后直接用)
    'match_badge_title': make_style('MatchBadgeTitle', bold=True, size=14, leading=20,
                                     color=C_GOLD, align=TA_CENTER, space_after=0),
    'match_title': make_style('MatchTitle', bold=True, size=28, leading=36,
                               color=C_NAVY, align=TA_CENTER, space_after=0),
    'match_info': make_style('MatchInfo', size=13, leading=19,
                              color=C_MUTED, align=TA_CENTER, space_after=0),

    # 区块标签
    'section_label': make_style('SectionLabel', bold=True, size=13, leading=18,
                                 color=C_NAVY, space_before=0, space_after=0),

    # 表格
    'table_header': make_style('TableHeader', bold=True, size=11, leading=15, color=white),
    'table_key': make_style('TableKey', bold=True, size=12, leading=17, color=C_NAVY),
    'table_val': make_style('TableVal', size=12, leading=17, color=C_TEXT),

    # 推荐
    'rec_main': make_style('RecMain', bold=True, size=17, leading=24,
                            color=C_GREEN, space_before=0, space_after=0),
    'rec_main_odds': make_style('RecMainOdds', bold=True, size=19, leading=26,
                                 color=C_TEXT, space_before=0, space_after=0),
    'rec_main_detail': make_style('RecMainDetail', size=12, leading=17,
                                   color=C_TEXT_LIGHT, space_after=0),
    'rec_sub': make_style('RecSub', bold=True, size=15, leading=22,
                           color=C_AMBER, space_before=0, space_after=0),
    'rec_sub_odds': make_style('RecSubOdds', bold=True, size=17, leading=24,
                                color=C_TEXT, space_before=0, space_after=0),
    'rec_detail': make_style('RecDetail', size=11, leading=16, color=C_MUTED, space_after=0),

    # 醒目提示行 (Ultra 11.18) — 让平直推 / 平局关注
    'alert_line': make_style('AlertLine', bold=True, size=13, leading=19,
                             color=C_RED, space_before=0, space_after=0),
    'alert_line_sub': make_style('AlertLineSub', bold=True, size=12, leading=18,
                                  color=C_GOLD_DARK, space_before=0, space_after=0),
    'alert_line_draw': make_style('AlertLineDraw', bold=True, size=12, leading=18,
                                   color=C_TEAL, space_before=0, space_after=0),

    # 正文/辅助
    'body_text': make_style('BodyText', size=12, leading=17, color=C_TEXT_LIGHT, space_after=0),
    'pool_text': make_style('PoolText', size=11, leading=16, color=C_TEXT, space_after=0),
    'footer': make_style('Footer', size=8, leading=12, color=C_MUTED, align=TA_CENTER, space_after=0),
    'page_num': make_style('PageNum', size=9, leading=13, color=C_MUTED, align=TA_CENTER, space_after=0),
}


# ====================================================================
# 数据提取
# ====================================================================
def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def extract_match(data, match_id):
    """从JSON中提取一场比赛的结构化数据"""
    meta = data.get('meta', {}).get(match_id, {})
    res = data.get('results', {}).get(match_id, {})
    if not meta or not res:
        return None

    home = meta.get('home', '?')
    away = meta.get('away', '?')
    league = meta.get('league', '?')
    match_time = meta.get('match_time', '?')
    match_date = meta.get('match_date', '?')
    weekday = meta.get('weekday', '?')
    rank_h = meta.get('home_rank', '')
    rank_a = meta.get('away_rank', '')
    data_source = meta.get('data_source', '')
    betting_single = meta.get('betting_single', False)

    had = res.get('HAD', {})
    had_dir = had.get('dir', '?')
    had_odds = had.get('odds', '?')
    had_conf = had.get('conf', '')
    had_p = had.get('p', '')
    if isinstance(had_p, str):
        had_p = had_p.replace('%', '')

    hhad = res.get('HHAD', {})
    hhad_dir = hhad.get('dir', '?')
    hhad_odds = hhad.get('odds', '?')
    hhad_conf = hhad.get('conf', '')
    hhad_h = hhad.get('handicap', '?')
    hhad_p = hhad.get('p', '')

    score = res.get('score', {})
    top3 = score.get('top3', '')
    wdl = score.get('wdl', '')
    over_main = score.get('over_main', '')
    market_gl = score.get('market_gl_str', '')
    high_top3 = score.get('high_top3', '')
    high_dir = score.get('high_dir', '')

    tg = res.get('total_goals', {})
    tg_main = tg.get('main', '')
    tg_top3 = tg.get('top3', '')

    hf = res.get('half_full', {})
    hf_main = hf.get('main', '')
    hf_top3 = hf.get('top3', '')

    goals = res.get('goals', {})
    home_xg = goals.get('home_xg', '')
    away_xg = goals.get('away_xg', '')
    total_exp = goals.get('total_expected', '')
    key_insight = goals.get('key_insight', '')

    cmb = res.get('cross_market', {})
    primary = cmb.get('primary_bet', {})
    double_rec = cmb.get('double_recommend', {})
    hhad_primary = cmb.get('hhad_primary_bet', {})
    wdl_picks = cmb.get('wdl_picks', [])  # Ultra 11.30: 胜平负共斥双推
    let_draw_rec = cmb.get('let_draw_rec', {})  # Ultra 11.17: 让平直推
    draw_attention = cmb.get('draw_attention', {})  # Ultra 11.18: 平局关注
    draw_window_priority = bool(cmb.get('draw_window_hhad_priority', False))  # Ultra 11.19: 平局窗口HHAD优先
    insight = cmb.get('insight', '')
    margin_dist = cmb.get('margin_dist', {})

    sp = res.get('sporttery_pools', {})
    dq = res.get('data_quality', {})
    quality = dq.get('quality', '?')
    quality_score = dq.get('score', '')
    difficulty = res.get('difficulty', '?')
    ev_list = res.get('ev', [])
    initial = res.get('initial', {})
    had_trend = meta.get('had_trend', '')

    return {
        'id': match_id,
        'home': home, 'away': away,
        'league': league, 'time': match_time,
        'date': match_date, 'weekday': weekday,
        'rank_h': rank_h, 'rank_a': rank_a,
        'data_source': data_source,
        'betting_single': betting_single,
        'had_dir': had_dir, 'had_odds': had_odds,
        'had_conf': had_conf, 'had_p': had_p,
        'hhad_dir': hhad_dir, 'hhad_odds': hhad_odds,
        'hhad_conf': hhad_conf, 'hhad_h': hhad_h,
        'hhad_p': hhad_p,
        'top3_score': top3, 'wdl': wdl,
        'over_main': over_main, 'market_gl': market_gl,
        'high_top3': high_top3, 'high_dir': high_dir,
        'tg_main': tg_main, 'tg_top3': tg_top3,
        'hf_main': hf_main, 'hf_top3': hf_top3,
        'home_xg': home_xg, 'away_xg': away_xg,
        'total_exp': total_exp, 'key_insight': key_insight,
        'primary': primary, 'double_rec': double_rec,
        'hhad_primary': hhad_primary,
        'wdl_picks': wdl_picks,
        'let_draw_rec': let_draw_rec,
        'draw_attention': draw_attention,
        'draw_window_priority': draw_window_priority,
        'insight': insight, 'margin_dist': margin_dist,
        'sporttery_pools': sp,
        'quality': quality, 'quality_score': quality_score,
        'difficulty': difficulty,
        'ev_list': ev_list,
        'initial': initial,
        'had_trend': had_trend,
    }


# ====================================================================
# 辅助构建函数
# ====================================================================
def _section_header(label):
    """区块标题 + 金色装饰线 (紧凑版)"""
    return [
        Paragraph(label, S['section_label']),
        GoldLine(CW, 0.8),
        Spacer(1, 0.5 * mm),
    ]


def _detect_primary_insight(m):
    """从洞察中提取一句关键推荐语 — 完整显示"""
    ins = m['insight']
    if not ins:
        return ''
    # 不再截断，直接显示完整的第一段
    for prefix in ['主推', '混合', 'HHAD主推']:
        idx = ins.find(prefix)
        if idx >= 0:
            return ins[idx:]
    return ins


def _build_initial_odds_block(initial):
    """构建初赔信息"""
    if not initial:
        return ''
    parts = []
    ouzhi = initial.get('ouzhi_now', '')
    yazhi = initial.get('yazhi_now', '')
    if ouzhi:
        parts.append(f'欧指 {ouzhi}')
    if yazhi:
        parts.append(f'亚指 {yazhi}')
    return '  |  '.join(parts) if parts else ''


def _hhad_option_label(option, handicap):
    """让球玩法选项术语规范化 (Ultra 11.10 铁律)

    - 负盘(≤-1)=让球: 让胜/让负/让平 不变
    - 正盘(≥+1)=受让: 让胜→受让胜, 让负→受让负, 让平→受让平
    - 0=平盘: 保持让X不变
    只处理 option 含 '让胜'/'让负'/'让平' 的 HHAD 选项, 其余原样返回。
    """
    if not option:
        return option
    try:
        hcap = float(handicap)
    except (TypeError, ValueError):
        return option
    if hcap <= 0:
        return option  # 让球盘或平盘, 术语不变
    # 幂等替换 (ERR-20260809-001): 用负向后瞻 (?<!受) 避免已含"受让X"的文本二次替换成"受受让X"
    for src, dst in [('让胜', '受让胜'), ('让负', '受让负'), ('让平', '受让平')]:
        option = re.sub(r'(?<!受)' + src, dst, option)
    return option


# ====================================================================
# 核心: 构建单场比赛页面 (严格1页)
# ====================================================================
def build_match_page(m, page_num, total):
    """构建单场比赛的完整页面内容 — 严格控制在1页内"""
    story = []
    full_w = CW

    # ================================================================
    # 区域1: 比赛对阵 (无联赛/数据源标签)
    # ================================================================
    # 仅保留单关标识
    if m['betting_single']:
        sd_data = [[Paragraph('单关', S['match_badge_title'])]]
        sd_table = Table(sd_data, colWidths=[full_w * 0.25])
        sd_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), C_GOLD_PALE),
            ('BOX', (0, 0), (-1, -1), 0.5, C_GOLD_LIGHT),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('RIGHTPADDING', (0, 0), (-1, -1), 10),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ]))
        sd_wrap = Table([[sd_table]], colWidths=[full_w])
        sd_wrap.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        story.append(sd_wrap)
        story.append(Spacer(1, 2 * mm))

    # 主队 vs 客队 (赛号+球队名+排名 — 同一行，排名缩小)
    rank_h = m['rank_h'] if m['rank_h'] else ''
    rank_a = m['rank_a'] if m['rank_a'] else ''

    # 用富文本 <font> 标签实现不同字号，不用表格
    big_sz = '28'
    small_sz = '13'
    rank_h_str = f'<font size="{small_sz}" color="#ffffff">{rank_h}</font>' if rank_h else ''
    rank_a_str = f'<font size="{small_sz}" color="#ffffff">{rank_a}</font>' if rank_a else ''
    title_text = (
        f'<font size="{big_sz}">{m["id"]} {m["home"]}</font>{rank_h_str}'
        f'<font size="{big_sz}"> vs {m["away"]}</font>{rank_a_str}'
    )
    # 金色背景横幅
    title_para = Paragraph(title_text, make_style('TitleGold', bold=True, size=28, leading=36,
                                                   color=white, align=TA_CENTER, space_after=0))
    title_wrap = Table([[title_para]], colWidths=[full_w])
    title_wrap.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), C_GOLD),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(title_wrap)
    story.append(Spacer(1, 0.5 * mm))

    # 时间信息
    info_parts = [f'⏱ {m["time"]}']
    if m['league']:
        info_parts.append(m['league'])
    story.append(Paragraph('  |  '.join(info_parts), S['match_info']))
    story.append(Spacer(1, 2 * mm))

    # ================================================================
    # 区域3: 核心数据表 (紧凑)
    # ================================================================
    def _make_row(label, value):
        return [Paragraph(label, S['table_key']), Paragraph(value, S['table_val'])]

    had_p_clean = m['had_p'].replace('%', '') if m['had_p'] else ''
    # 让球标识: 负盘(-1,-2,...)=让球, 正盘(+1,+2,...)=受让 (Ultra 11.10规范)
    _hcap = m['hhad_h']
    try:
        _hcap_n = float(_hcap) if str(_hcap).replace('.','').replace('-','').isdigit() else None
    except:
        _hcap_n = None
    if _hcap_n is not None and _hcap_n != 0:
        _hcap_label = f'受让{abs(_hcap_n):g}球' if _hcap_n > 0 else f'让{abs(_hcap_n):g}球'
    elif _hcap_n is not None:
        _hcap_label = '平盘(0球)'
    else:
        _hcap_label = f'盘口{_hcap}'
    data_rows = [
        _make_row('胜平负', f'{m["had_dir"]}  @{m["had_odds"]}    {m["had_conf"]}  ({had_p_clean})'),
        _make_row('让球', f'{m["hhad_dir"]}  @{m["hhad_odds"]}    {m["hhad_conf"]}  ({_hcap_label})'),
    ]
    if m['wdl']:
        wdl_clean = m['wdl'].replace('%', '')
        data_rows.append(_make_row('概率分布', f'胜 {wdl_clean}'))

    score_parts = [m['top3_score']]
    if m['market_gl']:
        over_str = f'大 {m["over_main"]}%' if m['over_main'] else ''
        score_parts.append(f'盘口 {m["market_gl"]} {over_str}')
    data_rows.append(_make_row('比分 / 盘口', '  |  '.join(score_parts)))

    tg_val = m['tg_top3'] if m['tg_top3'] else m['tg_main']
    data_rows.append(_make_row('总进球', tg_val))

    hf_val = m['hf_top3'] if m['hf_top3'] else m['hf_main']
    data_rows.append(_make_row('半全场', hf_val))

    xg_val = f'主 {m["home_xg"]}  /  客 {m["away_xg"]}    总 {m["total_exp"]} 球'
    data_rows.append(_make_row('预期进球', xg_val))

    init_block = _build_initial_odds_block(m['initial'])
    if init_block:
        data_rows.append(_make_row('初赔参考', init_block))

    header_row = [Paragraph('数据分析', S['table_header']), Paragraph('核心数据', S['table_header'])]
    tbl_data = [header_row] + data_rows

    col_w = full_w * 0.20, full_w * 0.80
    tbl = Table(tbl_data, colWidths=col_w)
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), C_NAVY),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('LINEBELOW', (0, 0), (-1, 0), 1.2, C_GOLD),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [C_WHITE, C_GOLD_PALE]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, 0), 5),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
        ('LEFTPADDING', (0, 0), (-1, 0), 10),
        ('RIGHTPADDING', (0, 0), (-1, 0), 10),
        ('TOPPADDING', (0, 1), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 2),
        ('LEFTPADDING', (0, 1), (-1, -1), 10),
        ('RIGHTPADDING', (0, 1), (-1, -1), 10),
        ('LINEBELOW', (0, 1), (-1, -1), 0.3, C_BORDER_LT),
        ('LINEBELOW', (0, -1), (-1, -1), 0.5, C_BORDER),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 1.5 * mm))

    # ================================================================
    # 区域4: 推荐方案 — 双卡片
    # ================================================================
    rec_blocks = []

    # 主推卡片
    pri = m['primary']
    if pri and pri.get('option'):
        opt = pri.get('option', '')
        mk = pri.get('market', '')
        prob = pri.get('prob', 0)
        odds = pri.get('odds', 0)
        ev = pri.get('ev_pct', 0)
        val = '★ 价值之选' if pri.get('value') else ''
        cov = pri.get('coverage', '')

        inner = []
        title_row = Table(
            [[Paragraph('● 主推推荐', S['rec_main']), Paragraph(f'@{odds}', S['rec_main_odds'])]],
            colWidths=[full_w * 0.32, full_w * 0.14]
        )
        title_row.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ]))
        inner.append([title_row])
        # option 已含市场前缀(HHAD让负/HAD胜平双选), 不再重复拼 mk
        inner.append([Paragraph(f'{_hhad_option_label(opt, m["hhad_h"])}', S['rec_main_odds'])])
        detail_parts = [f'P = {prob:.1f}%', f'EV = {ev:+.1f}%']
        if val:
            detail_parts.append(val)
        # 添加概率说明
        if mk == 'HAD' and had_p_clean:
            detail_parts.append(f'(模型原始P↑ {had_p_clean.split("/")[0]}%)')
        inner.append([Paragraph('  |  '.join(detail_parts), S['rec_main_detail'])])
        if cov:
            inner.append([Paragraph(f'覆盖: {cov}', S['rec_main_detail'])])

        card = Table(inner, colWidths=[full_w * 0.46])
        card.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), C_GREEN_BG),
            ('BOX', (0, 0), (-1, -1), 1.5, C_GREEN),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ]))
        rec_blocks.append(card)
    else:
        rec_blocks.append(Spacer(1, 1))

    # 次推卡片
    dr = m['double_rec']
    hp = m['hhad_primary']
    sub_entries = []
    if dr and dr.get('option'):
        sub_entries.append(('double', dr))
    if hp and hp.get('option') and (not pri or hp.get('option') != pri.get('option')):
        sub_entries.append(('hhad', hp))

    if sub_entries:
        inner = []
        inner.append([Paragraph('● 次选方案', S['rec_sub'])])
        for entry_type, entry in sub_entries:
            opt = entry.get('option', '')
            mk = entry.get('market', '')
            prob = entry.get('prob', 0)
            odds = entry.get('odds', 0)
            ev = entry.get('ev_pct', 0)
            val = '★ 价值之选' if entry.get('value') else ''
            dir_ = entry.get('direction', '')

            odds_row = Table(
                [[Paragraph(f'{_hhad_option_label(opt, m["hhad_h"])}', S['rec_sub_odds']),
                  Paragraph(f'@{odds}', S['rec_sub_odds'])]],
                colWidths=[full_w * 0.32, full_w * 0.14]
            )
            odds_row.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 0),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
            ]))
            inner.append([odds_row])
            detail_parts = [f'P = {prob:.1f}%', f'EV = {ev:+.1f}%']
            if val:
                detail_parts.append(val)
            if dir_:
                detail_parts.append(f'方向: {dir_}')
            inner.append([Paragraph('  |  '.join(detail_parts), S['rec_detail'])])

        card = Table(inner, colWidths=[full_w * 0.46])
        card.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), C_AMBER_BG),
            ('BOX', (0, 0), (-1, -1), 1.5, C_AMBER),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ]))
        rec_blocks.append(card)
    else:
        rec_blocks.append(Spacer(1, 1))

    # 并排双卡片
    if len(rec_blocks) >= 2:
        rec_row = Table([rec_blocks], colWidths=[full_w * 0.5, full_w * 0.5])
        rec_row.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (0, 0), 0),
            ('RIGHTPADDING', (1, 0), (1, 0), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        story.extend(_section_header('推荐方案'))
        story.append(rec_row)
    elif len(rec_blocks) == 1:
        story.extend(_section_header('推荐方案'))
        story.append(rec_blocks[0])

    # 醒目提示行 (Ultra 11.18) — 让平直推 / 平局关注 (一行文字, 非卡片)
    alert_rows = []

    # 让平直推行
    ldr = m['let_draw_rec']
    if ldr and ldr.get('option') and ldr.get('let_draw_direct'):
        _ldr_prob = ldr.get('prob', 0)
        _ldr_odds = ldr.get('odds', 0)
        _ldr_ev = ldr.get('ev_pct', 0)
        _ldr_label = _hhad_option_label(ldr.get('option', ''), m['hhad_h'])
        alert_rows.append(Paragraph(
            f'▲ 让平直推  {_ldr_label} @{_ldr_odds}  |  P = {_ldr_prob:.1f}%  |  EV = {_ldr_ev:+.1f}%',
            S['alert_line']))

    # 平局关注行 (Ultra 11.18)
    dar = m['draw_attention']
    if dar and dar.get('option') and dar.get('draw_attention'):
        _dar_prob = dar.get('prob', 0)
        _dar_odds = dar.get('odds', 0)
        _dar_ev = dar.get('ev_pct', 0)
        alert_rows.append(Paragraph(
            f'▲ 平局关注  HAD平 @{_dar_odds}  |  P = {_dar_prob:.1f}%  |  EV = {_dar_ev:+.1f}%  |  历史平局率≈25%',
            S['alert_line_sub']))

    # 平局窗口HHAD优先行 (Ultra 11.19) — 平局场次让球盘判别力更强
    if m.get('draw_window_priority'):
        _hhd = m.get('hhad_primary') or {}
        _hhd_dir = _hhd.get('option', '')
        _hhd_odds = _hhd.get('odds', 0)
        if _hhd_dir and _hhd_odds and _hhd_odds > 0:
            _hhd_dir_label = _hhad_option_label(_hhd_dir, m['hhad_h'])
            _dw_line = f'HHAD参考{_hhd_dir_label}@{_hhd_odds}'
        else:
            # HHAD主推缺失(未开盘)时, 给出通用让球盘优先提示
            _dw_line = '让球盘未开盘, 平局概率偏高, 注意提防平局'
        alert_rows.append(Paragraph(
            f'◆ 平局窗口HHAD优先  HAD平局P≥30%  |  让球盘更稳  |  {_dw_line}',
            S['alert_line_draw']))

    if alert_rows:
        alert_data = [[row] for row in alert_rows]
        alert_table = Table(alert_data, colWidths=[full_w])
        alert_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), C_GOLD_PALE),
            ('BOX', (0, 0), (-1, -1), 1.0, C_RED),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ]))
        story.append(Spacer(1, 0.5 * mm))
        story.append(alert_table)

    story.append(Spacer(1, 1 * mm))

    # ================================================================
    # 区域5: 净胜分布 + 关键结论 (Ultra 11.21 表格化)
    #   用户要求: 用"项目+结论"简洁表格, 替代超长洞察段落。
    #   关键结论从结构化字段(主推/次选/让平直推/双选)构建, 与对话一致。
    # ================================================================
    md = m['margin_dist']
    md_parts = []
    if md:
        for k, v in [('赢2+球', 'win_2plus'), ('赢1球', 'win_1'), ('平局', 'draw'), ('输球', 'lose')]:
            val = md.get(v, 0)
            if val:
                md_parts.append(f'{k} {val:.1f}%')

    # 数据质量/难度一行
    data_line_parts = []
    if md_parts:
        data_line_parts.append('净胜分布:  ' + '  |  '.join(md_parts))
    if m['quality']:
        q_label = f'数据质量: {m["quality"]}'
        if m['quality_score']:
            q_label += f' ({m["quality_score"]}分)'
        data_line_parts.append(q_label)
    if m['difficulty']:
        data_line_parts.append(f'难度: {m["difficulty"]}')

    # 从结构化字段构建"项目+结论"表格行 (与对话中的"项目/结论"一致)
    verdict_rows = []

    # 主推
    _pri = m['primary']
    if _pri and _pri.get('option'):
        _opt = _hhad_option_label(_pri.get('option', ''), m['hhad_h'])
        _mk = _pri.get('market', '')
        _tag = '主推' + (f'[{_mk}]' if _mk else '')
        _desc = f'{_opt}@{_pri.get("odds",0)}  P={_pri.get("prob",0):.1f}%  EV={_pri.get("ev_pct",0):+.1f}%'
        if _pri.get('value'):
            _desc += ' ★价值'
        verdict_rows.append((_tag, _desc))

    # 次选(双选/次推)
    _dr = m['double_rec']
    if _dr and _dr.get('option'):
        _opt = _hhad_option_label(_dr.get('option', ''), m['hhad_h'])
        _tag = '双选'
        _desc = f'{_opt}@{_dr.get("odds",0)}  P={_dr.get("prob",0):.1f}%  EV={_dr.get("ev_pct",0):+.1f}%'
        if _dr.get('value'):
            _desc += ' ★价值'
        if _dr.get('direction'):
            _desc += f'  ({_dr["direction"]})'
        verdict_rows.append((_tag, _desc))

    # HHAD主推 (与主推不同时才单独列)
    _hp = m['hhad_primary']
    if _hp and _hp.get('option') and (not _pri or _hp.get('option') != _pri.get('option')):
        _opt = _hhad_option_label(_hp.get('option', ''), m['hhad_h'])
        _desc = f'{_opt}@{_hp.get("odds",0)}  P={_hp.get("prob",0):.1f}%  EV={_hp.get("ev_pct",0):+.1f}%'
        if _hp.get('value'):
            _desc += ' ★价值'
        verdict_rows.append(('HHAD主推', _desc))

    # 让平直推
    _ldr = m['let_draw_rec']
    if _ldr and _ldr.get('option') and _ldr.get('let_draw_direct'):
        _opt = _hhad_option_label(_ldr.get('option', ''), m['hhad_h'])
        verdict_rows.append((
            '让平直推',
            f'{_opt}@{_ldr.get("odds",0)}  P={_ldr.get("prob",0):.1f}%  EV={_ldr.get("ev_pct",0):+.1f}%'
        ))

    # 平局关注
    _dar = m['draw_attention']
    if _dar and _dar.get('option') and _dar.get('draw_attention'):
        verdict_rows.append((
            '平局关注',
            f'{_hhad_option_label(_dar.get("option",""), m["hhad_h"])}@{_dar.get("odds",0)}  '
            f'P={_dar.get("prob",0):.1f}%  EV={_dar.get("ev_pct",0):+.1f}%'
        ))

    # 平局窗口HHAD优先
    if m.get('draw_window_priority'):
        _hhd = m.get('hhad_primary') or {}
        _hhd_dir = _hhd.get('option', '')
        _hhd_odds = _hhd.get('odds', 0)
        if _hhd_dir and _hhd_odds and _hhd_odds > 0:
            _dw_desc = f'HHAD参考{_hhad_option_label(_hhd_dir, m["hhad_h"])}@{_hhd_odds}'
        else:
            _dw_desc = '让球盘未开盘, 平局概率偏高, 注意提防平局'
        verdict_rows.append(('平局窗口', f'HHAD优先  {_dw_desc}'))

    # 渲染: 数据质量一行 (若有) + "项目/结论"表格
    bottom_blocks = []
    if data_line_parts:
        bottom_blocks.append(Paragraph('  |  '.join(data_line_parts), S['body_text']))

    if verdict_rows:
        v_data = [[Paragraph(k, S['table_key']), Paragraph(v, S['table_val'])] for k, v in verdict_rows]
        v_tbl = Table(v_data, colWidths=[full_w * 0.24, full_w * 0.76])
        v_tbl.setStyle(TableStyle([
            ('ROWBACKGROUNDS', (0, 0), (-1, -1), [C_WHITE, C_GOLD_PALE]),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('RIGHTPADDING', (0, 0), (-1, -1), 10),
            ('LINEBELOW', (0, 0), (-1, -1), 0.3, C_BORDER_LT),
            ('LINEBELOW', (0, -1), (-1, -1), 0.5, C_BORDER),
        ]))
        bottom_blocks.append(v_tbl)

    if bottom_blocks:
        insight_data = [[b] for b in bottom_blocks]
        insight_table = Table(insight_data, colWidths=[full_w])
        insight_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), C_GOLD_PALE),
            ('BOX', (0, 0), (-1, -1), 0.5, C_BORDER),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ]))
        story.append(insight_table)
        story.append(Spacer(1, 1 * mm))

    # ================================================================
    # 区域6: 体彩玩法 命中率优选 (Ultra 11.24 简洁表格化)
    #   用户要求: 5大玩法均命中率第一优先, EV仅展示参考。
    #   每个玩法取命中率最高(prob)的选项(最多2个), 展示prob+EV作参考。
    #   → 版面像对话中的"项目/结论"表格一样直观, 不再堆大段文字。
    # ================================================================
    sp = m['sporttery_pools'] or {}
    pool_rows = []
    for pool_key, pool_label in [('ttg', '竞彩总进球'), ('hafu', '竞彩半全场'), ('crs', '竞彩比分')]:
        items = sp.get(pool_key, [])
        if not items:
            continue
        # Ultra 11.24: 按命中率(prob)优先排序, 不再按EV
        sorted_items = sorted(items, key=lambda x: x.get('prob', -999), reverse=True)
        # 命中率最高的选项
        pos = sorted_items[:2]
        parts = []
        for it in pos:
            opt = it.get('option', '?')
            odd = it.get('odds', '?')
            p2 = it.get('prob', 0)
            ev2 = it.get('ev_pct', 0)
            parts.append(f'{opt}@{odd}  P{p2:.0f}%  EV{ev2:+.0f}%')
        if parts:
            pool_rows.append([
                Paragraph(pool_label, S['table_key']),
                Paragraph(' | '.join(parts), S['table_val']),
            ])

    if pool_rows:
        story.extend(_section_header('竞彩玩法 命中率优选'))
        pool_header = [Paragraph('玩法', S['table_header']), Paragraph('命中率优选选项', S['table_header'])]
        pool_tbl = Table([pool_header] + pool_rows, colWidths=[full_w * 0.28, full_w * 0.72])
        pool_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), C_NAVY),
            ('TEXTCOLOR', (0, 0), (-1, 0), white),
            ('LINEBELOW', (0, 0), (-1, 0), 1.2, C_GOLD),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [C_WHITE, C_GOLD_PALE]),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, 0), 5),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
            ('LEFTPADDING', (0, 0), (-1, 0), 10),
            ('RIGHTPADDING', (0, 0), (-1, 0), 10),
            ('TOPPADDING', (0, 1), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 1), (-1, -1), 3),
            ('LEFTPADDING', (0, 1), (-1, -1), 10),
            ('RIGHTPADDING', (0, 1), (-1, -1), 10),
            ('LINEBELOW', (0, 1), (-1, -1), 0.3, C_BORDER_LT),
            ('LINEBELOW', (0, -1), (-1, -1), 0.5, C_BORDER),
        ]))
        story.append(pool_tbl)
        story.append(Spacer(1, 1 * mm))

    # ================================================================
    # 区域7: 页脚
    # ================================================================
    story.append(DoubleGoldLine(full_w))
    story.append(Spacer(1, 1.5 * mm))

    now_str = datetime.now(_BEIJING_TZ).strftime('%Y-%m-%d %H:%M')
    footer_text = f'生成 {now_str}  |  数据 500.com / Nowscore  |  仅供参考 理性购彩'
    story.append(Paragraph(footer_text, S['footer']))
    story.append(Spacer(1, 0.5 * mm))
    story.append(Paragraph(f'— 第 {page_num} 页 / 共 {total} 页 —', S['page_num']))

    return story


# ====================================================================
# 封面页
# ====================================================================
# ====================================================================
# 汇总页: 总进球 / 比分概率最高选项 (Ultra 11.19)
#   用户要求: 单独两页, 每页一个玩法, 汇总所有场次概率最高的选项
# ====================================================================
def build_summary_pages(matches):
    """构建两页汇总: 页1=总进球概率最高, 页2=比分概率最高。返回 story"""
    story = []
    full_w = CW

    # 收集两玩法每场概率最高的选项
    pools = {'ttg': [], 'crs': []}  # pool_key -> [(mid, home, away, best_opt, ...)]
    for m in matches:
        sp = m['sporttery_pools'] or {}
        for pool_key in ('ttg', 'crs'):
            items = sp.get(pool_key, [])
            if items:
                best = max(items, key=lambda x: x.get('prob', 0))
                pools[pool_key].append({
                    'mid': m['id'], 'home': m['home'], 'away': m['away'],
                    'opt': best.get('option', '?'),
                    'odds': best.get('odds', '?'),
                    'prob': best.get('prob', 0),
                    'ev': best.get('ev_pct', 0),
                })
            else:
                pools[pool_key].append({
                    'mid': m['id'], 'home': m['home'], 'away': m['away'],
                    'opt': '-', 'odds': '-', 'prob': 0, 'ev': 0,
                })

    # 标红样式 (Ultra 11.19): 每页概率最高的场次用红色高亮
    _red_val = make_style('TableValRed', bold=True, size=12, leading=17, color=C_RED)
    _red_key = make_style('TableKeyRed', bold=True, size=12, leading=17, color=C_RED)

    for idx, (pool_key, page_title, sub) in enumerate([
        ('ttg', '总进球概率最高汇总', 'Total Goals — 每场概率最高的总进球选项'),
        ('crs', '比分概率最高汇总', 'Correct Score — 每场概率最高的比分选项'),
    ]):
        # 页首 (两页之间用 PageBreak 分隔)
        if idx > 0:
            story.append(PageBreak())
        story.append(Spacer(1, 8 * mm))
        story.append(Paragraph(page_title, S['match_title']))
        story.append(Spacer(1, 1.5 * mm))
        story.append(Paragraph(sub, S['match_info']))
        story.append(Spacer(1, 3 * mm))

        # 找出本页所有场次里的概率最高值 (只统计有数据的场次)
        valid = [e['prob'] for e in pools[pool_key] if e['prob'] > 0]
        max_prob = max(valid) if valid else 0

        # 表头
        header = [
            Paragraph('场次', S['table_header']),
            Paragraph('对阵', S['table_header']),
            Paragraph('概率最高', S['table_header']),
            Paragraph('赔率', S['table_header']),
            Paragraph('P', S['table_header']),
            Paragraph('EV', S['table_header']),
        ]
        rows = [header]
        for entry in pools[pool_key]:
            is_hi = entry['prob'] > 0 and abs(entry['prob'] - max_prob) < 1e-9
            pv = _red_val if is_hi else S['table_val']
            pk = _red_key if is_hi else S['table_val']
            rows.append([
                Paragraph(str(entry['mid']), pk),
                Paragraph(f'{entry["home"]} vs {entry["away"]}', pv),
                Paragraph(str(entry['opt']), pv),
                Paragraph(str(entry['odds']), pv),
                Paragraph(f'{entry["prob"]:.1f}%', pv),
                Paragraph(f'{entry["ev"]:+.1f}%', pv),
            ])

        col_w = [full_w * 0.11, full_w * 0.33, full_w * 0.17, full_w * 0.13, full_w * 0.13, full_w * 0.13]
        tbl = Table(rows, colWidths=col_w, repeatRows=1)
        tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), C_NAVY),
            ('TEXTCOLOR', (0, 0), (-1, 0), white),
            ('LINEBELOW', (0, 0), (-1, 0), 1.2, C_GOLD),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [C_WHITE, C_GOLD_PALE]),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (0, -1), 'CENTER'),
            ('ALIGN', (3, 0), (5, -1), 'CENTER'),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('LINEBELOW', (0, 1), (-1, -1), 0.3, C_BORDER_LT),
            ('LINEBELOW', (0, -1), (-1, -1), 0.5, C_BORDER),
        ]))
        story.append(tbl)

    return story


# ====================================================================
# Ultra 11.25: 8列总览表 (用户要求: 紧凑直观, 一场一行)
#   列: 1编号 2时间 3赛事 4主队VS客队 5胜平负 6总进球 7半全场 8比分
#   胜平负: HAD + HHAD 各取命中率最高1项
#   总进球/半全场/比分: 取命中率最高1-2项 (sporttery_pools 已按prob降序)
#   全部命中率优先, EV仅作参考 (用户铁律: 足球非抛硬币, 每场独立)
# ====================================================================
def _strip_market(opt):
    """去掉 HAD/HHAD 市场前缀: 'HAD胜'→'胜', 'HHAD让胜'→'让胜'"""
    for p in ('HAD', 'HHAD'):
        if str(opt).startswith(p):
            return str(opt)[len(p):]
    return str(opt)


def _wdl_cell(m):
    """胜平负列: 优先取共斥双推 wdl_picks (Ultra 11.30: 净胜球互斥Top2, 不重叠不冗余)
    字段缺失/为空时回退旧口径 HAD主推 + HHAD主推.
    单行空格分隔, 不强制分行 (Ultra 11.28: 消除大片留空)"""
    wdl = m.get('wdl_picks') or []
    if wdl:
        parts = [_strip_market(it.get('option', '?')) for it in wdl]
        return ' '.join(parts) if parts else '-'
    parts = []
    pb = m.get('primary') or {}
    if pb.get('option'):
        parts.append(_strip_market(pb.get('option')))
    hpb = m.get('hhad_primary') or {}
    if hpb.get('option'):
        parts.append(_strip_market(hpb.get('option')))
    return ' '.join(parts) if parts else '-'


def _pool_cell(items, max_n=2):
    """玩法池列: 取命中率最高前max_n个, 仅输出选项, 单行空格分隔
    内部按prob降序排序, 不依赖JSON存储顺序 (数据可能为改版前旧排序)"""
    if not items:
        return '-'
    items = sorted(items, key=lambda x: x.get('prob', 0), reverse=True)
    parts = []
    for it in items[:max_n]:
        parts.append(_strip_market(it.get('option', '?')))
    return ' '.join(parts)


def build_overview_pages(matches, match_date=''):
    """构建 8 列总览表 (Ultra 11.25): 一场一行, 紧凑直观, 替代每场一整页+旧汇总页
    Ultra 11.27: 标题加日期, 去掉副标题行, 配色更醒目高大上"""
    story = []
    full_w = CW

    # 标题 + 金色装饰线 (高大上风格)
    story.append(Spacer(1, 6 * mm))
    title = f'竞彩预测  {match_date}' if match_date else '竞彩预测'
    _title_style = make_style('OverviewTitle', bold=True, size=28, leading=36,
                              color=C_NAVY, align=TA_CENTER, space_after=0)
    story.append(Paragraph(title, _title_style))
    story.append(Spacer(1, 2 * mm))
    gold_line_tbl = Table([['']], colWidths=[full_w])
    gold_line_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), C_GOLD),
        ('TOPPADDING', (0, 0), (-1, -1), 1.2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1.2),
    ]))
    gold_wrap = Table([[gold_line_tbl]], colWidths=[full_w])
    gold_wrap.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
    story.append(gold_wrap)
    story.append(Spacer(1, 3 * mm))

    _ov_header = make_style('OverviewHeader', bold=True, size=14, leading=19,
                               color=white, align=TA_CENTER)
    header = [
        Paragraph('编号', _ov_header),
        Paragraph('时间', _ov_header),
        Paragraph('赛事', _ov_header),
        Paragraph('主队VS客队', _ov_header),
        Paragraph('胜平负', _ov_header),
        Paragraph('总进球', _ov_header),
        Paragraph('半全场', _ov_header),
        Paragraph('比分', _ov_header),
    ]
    rows = [header]

    _cell = make_style('OverviewCell', size=13.5, leading=18, color=C_TEXT)
    _cell_bold = make_style('OverviewCellBold', bold=True, size=13.5, leading=18, color=C_NAVY)
    _cell_id = make_style('OverviewCellId', bold=True, size=13.5, leading=18, color=C_GOLD_DARK)

    for m in matches:
        sp = m['sporttery_pools'] or {}
        rows.append([
            Paragraph(m.get('id', ''), _cell_id),
            Paragraph(m.get('time', ''), _cell),
            Paragraph(m.get('league', ''), _cell),
            Paragraph(f"{m['home']} vs<br/>{m['away']}", _cell_bold),
            Paragraph(_wdl_cell(m), _cell),
            Paragraph(_pool_cell(sp.get('ttg')), _cell),
            Paragraph(_pool_cell(sp.get('hafu')), _cell),
            Paragraph(_pool_cell(sp.get('crs')), _cell),
        ])

    col_w = [
        full_w * 0.13,   # 编号 周一001(5全角)
        full_w * 0.085,  # 时间 01:00
        full_w * 0.07,   # 赛事 瑞超(2全角, 单行)
        full_w * 0.165,  # 主队VS客队 (两行, 收窄消除右侧留白)
        full_w * 0.135,  # 胜平负 胜 让胜
        full_w * 0.135,  # 总进球 2球 1球
        full_w * 0.135,  # 半全场 负负 平负
        full_w * 0.125,  # 比分 1-1 1-0
    ]
    tbl = Table(rows, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), C_NAVY),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('LINEBELOW', (0, 0), (-1, 0), 2.0, C_GOLD),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [C_WHITE, C_GOLD_PALE]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 3),
        ('RIGHTPADDING', (0, 0), (-1, -1), 3),
        ('LINEBELOW', (0, -1), (-1, -1), 1.2, C_GOLD),
        ('LINEABOVE', (0, 1), (-1, 1), 0.5, C_GOLD),
    ]))
    story.append(tbl)
    return story


def _measure_cover_pages(elements):
    """测量封面元素实际占用页数。

    注意: reportlab 的 build() 会消费(清空)传入的 story, 故测量一律传 deepcopy 副本。
    """
    import copy as _copy
    _buff = BytesIO()
    _tdoc = SimpleDocTemplate(
        _buff, pagesize=A4,
        leftMargin=LM, rightMargin=RM,
        topMargin=TM, bottomMargin=BM,
    )
    _p = [1]
    def _cb(_c, _d):
        if _d.page > _p[0]:
            _p[0] = _d.page
    _tdoc.build(_copy.deepcopy(elements), onFirstPage=_cb, onLaterPages=_cb)
    return _p[0]


def build_cover_story(matches, match_date, total):
    """构建封面内容, 返回 (story, cover_pages)。

    封面不设显式 PageBreak, 比赛多时会自动分页溢出到第2+/N页。
    为保证**每个封面页顶部都有留白**(避免续页第一行压到顶部金线),
    这里逐行测量、手动在每页开头插入 50mm 顶部 Spacer 后分页。
    """
    # 第一页头部块 (标题/日期/共N场)
    head = []
    head.append(Paragraph('竞彩足球预测', S['cover_title']))
    head.append(Spacer(1, 3 * mm))

    gold_line_tbl = Table([['']], colWidths=[CW * 0.35])
    gold_line_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), C_GOLD),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))
    gl_wrap = Table([[gold_line_tbl]], colWidths=[CW])
    gl_wrap.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
    head.append(gl_wrap)
    head.append(Spacer(1, 6 * mm))

    head.append(Paragraph(f'{match_date}', S['cover_sub']))
    head.append(Spacer(1, 2 * mm))
    head.append(Paragraph(f'共 {total} 场比赛', S['cover_date']))
    head.append(Spacer(1, 10 * mm))

    # 比赛行
    rows = []
    for m in matches:
        line = f'{m["id"]:>8s}    {m["home"]}  vs  {m["away"]}    —  {m["league"]}'
        rows.append(Paragraph(line, S['cover_match']))
        rows.append(Spacer(1, 1.5 * mm))

    # 页脚 (仅最后一页)
    foot = []
    foot.append(Spacer(1, 35 * mm))
    foot.append(Paragraph(f'生成时间: {datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M")}', S['cover_footer']))
    foot.append(Paragraph('数据来源: 500.com / Nowscore  |  仅供参考 理性购彩', S['cover_footer']))

    # 逐页组装: 每页顶部统一 50mm 留白, 避免续页第一行压到金线
    story = []
    current = [Spacer(1, 50 * mm)] + head
    cur_page = 1
    for row in rows:
        trial = current + [row]
        if _measure_cover_pages(trial) > cur_page:
            # 当前页放不下此行 → 收尾当前页, 开新页(也带顶部留白)
            story.extend(current)
            story.append(PageBreak())
            current = [Spacer(1, 50 * mm), row]
            cur_page += 1
        else:
            current = trial

    story.extend(current)
    story.extend(foot)

    # 修正 cover_pages (页脚可能使末页溢出)
    cover_pages = _measure_cover_pages(story)
    return story, cover_pages


# ====================================================================
# 页面背景绘制
# ====================================================================
def draw_page_bg(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(C_PAGE_BG)
    canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    canvas.restoreState()


def draw_cover_bg(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(C_COVER_BG)
    canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    canvas.setStrokeColor(C_GOLD)
    canvas.setLineWidth(0.5)
    canvas.line(20, PAGE_H - 25, PAGE_W - 20, PAGE_H - 25)
    canvas.line(20, PAGE_H - 30, PAGE_W - 20, PAGE_H - 30)
    canvas.line(20, 20, PAGE_W - 20, 20)
    canvas.line(20, 25, PAGE_W - 20, 25)
    canvas.restoreState()


# ====================================================================
# 主生成函数
# ====================================================================
def generate_pdf(data, output_path):
    """生成完整的预测 PDF (每场一页)"""
    match_date = ''
    matches = []
    for mid in sorted(data.get('meta', {}).keys()):
        m = extract_match(data, mid)
        if m:
            matches.append(m)
            if not match_date:
                match_date = m['date']

    if not match_date:
        match_date = '待定'

    total = len(matches)

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=LM, rightMargin=RM,
        topMargin=TM, bottomMargin=BM,
    )

    # Ultra 11.26: 去掉封面, 只输出8列总览表 (用户要求: 简洁直观)
    full_story = []
    full_story.extend(build_overview_pages(matches, match_date))

    doc.build(full_story, onFirstPage=draw_page_bg, onLaterPages=draw_page_bg)
    return output_path


# ====================================================================
# 入口
# ====================================================================
def main():
    if len(sys.argv) < 2:
        print("用法: python3 gen_pred_pdf.py <json_path> [output_path]")
        sys.exit(1)

    json_path = sys.argv[1]
    if not os.path.exists(json_path):
        print(f"错误: 文件不存在 {json_path}")
        sys.exit(1)

    if len(sys.argv) >= 3:
        pdf_path = sys.argv[2]
    else:
        base = os.path.splitext(json_path)[0]
        pdf_path = base + '.pdf'

    data = load_json(json_path)
    generate_pdf(data, pdf_path)
    print(f"✅ PDF 已生成: {pdf_path}")


if __name__ == '__main__':
    main()