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
import sys
import math
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.lib.colors import HexColor, white, black, Color
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether, Flowable, Frame, PageTemplate,
    BaseDocTemplate
)
from reportlab.lib import colors

# ── 字体注册 ──
# 霞鹜文楷 (LxgwWenKai)
pdfmetrics.registerFont(TTFont('CJK', '/usr/share/fonts/truetype/LXGWWenKai-Regular.ttf'))
pdfmetrics.registerFont(TTFont('CJKBold', '/usr/share/fonts/truetype/LXGWWenKai-Medium.ttf'))

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

    cm = res.get('cross_market', {})
    primary = cm.get('primary_bet', {})
    double_rec = cm.get('double_recommend', {})
    hhad_primary = cm.get('hhad_primary_bet', {})
    insight = cm.get('insight', '')
    margin_dist = cm.get('margin_dist', {})

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
        Spacer(1, 1 * mm),
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
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
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
    story.append(Spacer(1, 2.5 * mm))

    # ================================================================
    # 区域3: 核心数据表 (紧凑)
    # ================================================================
    def _make_row(label, value):
        return [Paragraph(label, S['table_key']), Paragraph(value, S['table_val'])]

    had_p_clean = m['had_p'].replace('%', '') if m['had_p'] else ''
    data_rows = [
        _make_row('胜平负', f'{m["had_dir"]}  @{m["had_odds"]}    {m["had_conf"]}  ({had_p_clean})'),
        _make_row('让球', f'{m["hhad_dir"]}  @{m["hhad_odds"]}    {m["hhad_conf"]}  (让{m["hhad_h"]}球)'),
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
        ('TOPPADDING', (0, 1), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 3),
        ('LEFTPADDING', (0, 1), (-1, -1), 10),
        ('RIGHTPADDING', (0, 1), (-1, -1), 10),
        ('LINEBELOW', (0, 1), (-1, -1), 0.3, C_BORDER_LT),
        ('LINEBELOW', (0, -1), (-1, -1), 0.5, C_BORDER),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 2 * mm))

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
        inner.append([Paragraph(f'{mk}  {opt}', S['rec_main_odds'])])
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
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
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
                [[Paragraph(f'{mk}  {opt}', S['rec_sub_odds']),
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
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
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

    story.append(Spacer(1, 1.5 * mm))

    # ================================================================
    # 区域5: 净胜分布 + 关键洞察 (完整显示)
    # ================================================================
    md = m['margin_dist']
    md_parts = []
    if md:
        for k, v in [('赢2+球', 'win_2plus'), ('赢1球', 'win_1'), ('平局', 'draw'), ('输球', 'lose')]:
            val = md.get(v, 0)
            if val:
                md_parts.append(f'{k} {val:.1f}%')

    insight_line = _detect_primary_insight(m)
    bottom_lines = []

    # 合并净胜分布 + 数据质量 + 难度为一行
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
    if data_line_parts:
        bottom_lines.append('  |  '.join(data_line_parts))
    if insight_line:
        bottom_lines.append(f'关键洞察: {insight_line}')

    if bottom_lines:
        insight_data = [[Paragraph(line, S['body_text'])] for line in bottom_lines]
        insight_table = Table(insight_data, colWidths=[full_w])
        insight_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), C_GOLD_PALE),
            ('BOX', (0, 0), (-1, -1), 0.5, C_BORDER),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ]))
        story.append(insight_table)
        story.append(Spacer(1, 1.5 * mm))

    # ================================================================
    # 区域6: 体彩玩法 EV 优选 (紧凑)
    # ================================================================
    sp = m['sporttery_pools'] or {}
    pool_lines = []
    for pool_key, pool_label in [('ttg', '总进球'), ('hafu', '半全场'), ('crs', '比分')]:
        items = sp.get(pool_key, [])
        if items:
            sorted_items = sorted(items, key=lambda x: x.get('ev_pct', -999), reverse=True)[:1]
            parts = []
            for it in sorted_items:
                opt = it.get('option', '?')
                odd = it.get('odds', '?')
                ev2 = it.get('ev_pct', 0)
                flag = ' ✓' if ev2 > 0 else ''
                parts.append(f'{opt} @{odd} (EV {ev2:+.0f}%){flag}')
            if parts:
                pool_lines.append(f'{pool_label}:  {" | ".join(parts)}')

    if pool_lines:
        story.extend(_section_header('竞彩玩法 EV 优选'))
        for line in pool_lines:
            story.append(Paragraph(line, S['pool_text']))
        story.append(Spacer(1, 1.5 * mm))

    # ================================================================
    # 区域7: 页脚
    # ================================================================
    story.append(DoubleGoldLine(full_w))
    story.append(Spacer(1, 1.5 * mm))

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    footer_text = f'生成 {now_str}  |  数据 500.com / Nowscore  |  仅供参考 理性购彩'
    story.append(Paragraph(footer_text, S['footer']))
    story.append(Spacer(1, 0.5 * mm))
    story.append(Paragraph(f'— 第 {page_num} 页 / 共 {total} 页 —', S['page_num']))

    return story


# ====================================================================
# 封面页
# ====================================================================
def build_cover_story(matches, match_date, total):
    """构建封面内容"""
    story = []
    story.append(Spacer(1, 50 * mm))
    story.append(Paragraph('竞彩足球预测', S['cover_title']))
    story.append(Spacer(1, 3 * mm))

    gold_line_tbl = Table([['']], colWidths=[CW * 0.35])
    gold_line_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), C_GOLD),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))
    gl_wrap = Table([[gold_line_tbl]], colWidths=[CW])
    gl_wrap.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
    story.append(gl_wrap)
    story.append(Spacer(1, 6 * mm))

    story.append(Paragraph(f'{match_date}', S['cover_sub']))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(f'共 {total} 场比赛', S['cover_date']))
    story.append(Spacer(1, 10 * mm))

    for i, m in enumerate(matches):
        line = f'{m["id"]:>8s}    {m["home"]}  vs  {m["away"]}    —  {m["league"]}'
        story.append(Paragraph(line, S['cover_match']))
        story.append(Spacer(1, 1.5 * mm))

    story.append(Spacer(1, 35 * mm))
    story.append(Paragraph(f'生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}', S['cover_footer']))
    story.append(Paragraph('数据来源: 500.com / Nowscore  |  仅供参考 理性购彩', S['cover_footer']))
    return story


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

    # 封面
    cover_story = build_cover_story(matches, match_date, total)

    # 正文
    full_story = []
    full_story.extend(cover_story)

    for i, m in enumerate(matches):
        full_story.append(PageBreak())
        card = build_match_page(m, i + 1, total)
        full_story.extend(card)

    doc.build(
        full_story,
        onFirstPage=draw_cover_bg,
        onLaterPages=draw_page_bg,
    )
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