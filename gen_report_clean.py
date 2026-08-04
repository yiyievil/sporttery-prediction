#!/usr/bin/env python3
"""手机版预测报告PDF生成器

设计理念: 手机屏幕大小, 大字清晰, 一目了然
- A6竖版 (105x148mm), 适合手机阅读
- 大字号, 去除小字堆叠
- 汇总表: 推荐+赔率合并一格
- 次推荐也显示置信度星
"""

import json
import os
import sys
from datetime import datetime
from reportlab.lib.pagesizes import A6
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, LongTable,
)

from gen_report_v2 import rank_match, REPORT_TITLE
from pdf_fonts import register_cjk_font

_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
_PRED_DIR = os.path.join(_WORKSPACE, 'predictions')

PRED_FILE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_PRED_DIR, 'pred_20260725_周六.json')
if len(sys.argv) > 2:
    OUTPUT_PDF = sys.argv[2]
else:
    _base = os.path.basename(PRED_FILE).replace('pred_', 'report_').replace('.json', '_mobile.pdf')
    OUTPUT_PDF = os.path.join(os.path.dirname(PRED_FILE) or _PRED_DIR, _base)


# ============ 配色 (明亮) ============
C_BG          = HexColor('#ffffff')
C_HEADER_BG   = HexColor('#2563eb')
C_HEADER_TEXT = HexColor('#ffffff')
C_ROW_ALT1    = HexColor('#f0f4f8')
C_ROW_ALT2    = HexColor('#ffffff')
C_BORDER      = HexColor('#cbd5e1')
C_TEXT        = HexColor('#1e293b')
C_TEXT_LIGHT  = HexColor('#475569')
C_GREEN       = HexColor('#16a34a')
C_GREEN_BG    = HexColor('#dcfce7')
C_BLUE        = HexColor('#2563eb')
C_BLUE_BG     = HexColor('#dbeafe')
C_ORANGE      = HexColor('#ea580c')
C_RED         = HexColor('#dc2626')
C_CARD_BG     = HexColor('#f8fafc')
C_TITLE_BG    = HexColor('#1e40af')

# ============ 页面: A6竖版 (手机尺寸) ============
PAGE_SIZE = A6   # 105mm x 148mm
PAGE_W, PAGE_H = PAGE_SIZE
LM = 5 * mm
RM = 5 * mm
TM = 6 * mm
BM = 6 * mm
CW = PAGE_W - LM - RM   # ~95mm


def get_styles(cjk_font='CJK'):
    bold = 'CJK-Bold' if cjk_font == 'CJK' else cjk_font
    return {
        'title': ParagraphStyle('Title', fontName=bold, fontSize=14, leading=18,
                                 textColor=HexColor('#ffffff'), alignment=TA_CENTER, spaceAfter=2, wordWrap='CJK'),
        'subtitle': ParagraphStyle('Subtitle', fontName=cjk_font, fontSize=7, leading=9,
                                   textColor=HexColor('#bfdbfe'), alignment=TA_CENTER, wordWrap='CJK'),
        'section': ParagraphStyle('Section', fontName=bold, fontSize=10, leading=14,
                                   textColor=HexColor('#1e40af'), spaceBefore=8, spaceAfter=4, wordWrap='CJK'),
        # 汇总表
        'th': ParagraphStyle('Th', fontName=bold, fontSize=7.5, leading=10,
                              textColor=C_HEADER_TEXT, alignment=TA_CENTER, wordWrap='CJK'),
        'td': ParagraphStyle('Td', fontName=cjk_font, fontSize=8, leading=11,
                             textColor=C_TEXT, alignment=TA_CENTER, wordWrap='CJK'),
        'td_bold': ParagraphStyle('TdBold', fontName=bold, fontSize=8, leading=11,
                                    textColor=C_TEXT, alignment=TA_CENTER, wordWrap='CJK'),
        'td_green': ParagraphStyle('TdGreen', fontName=bold, fontSize=7.5, leading=10,
                                    textColor=C_GREEN, alignment=TA_CENTER, wordWrap='CJK'),
        'td_blue': ParagraphStyle('TdBlue', fontName=bold, fontSize=7.5, leading=10,
                                   textColor=C_BLUE, alignment=TA_CENTER, wordWrap='CJK'),
        # 卡片
        'match_title': ParagraphStyle('MatchTitle', fontName=bold, fontSize=10, leading=13,
                                       textColor=C_TEXT, wordWrap='CJK'),
        'match_info': ParagraphStyle('MatchInfo', fontName=cjk_font, fontSize=7, leading=9,
                                      textColor=C_TEXT_LIGHT, alignment=TA_LEFT, wordWrap='CJK'),
        'rec_label': ParagraphStyle('RecLabel', fontName=bold, fontSize=7, leading=9,
                                     textColor=C_TEXT_LIGHT, alignment=TA_CENTER, wordWrap='CJK'),
        'rec_name': ParagraphStyle('RecName', fontName=bold, fontSize=10, leading=13,
                                    textColor=C_TEXT, alignment=TA_CENTER, wordWrap='CJK'),
        'rec_odds': ParagraphStyle('RecOdds', fontName=bold, fontSize=14, leading=18,
                                    textColor=C_ORANGE, alignment=TA_CENTER, wordWrap='CJK'),
        'rec_detail': ParagraphStyle('RecDetail', fontName=cjk_font, fontSize=7.5, leading=10,
                                      textColor=C_TEXT_LIGHT, alignment=TA_CENTER, wordWrap='CJK'),
        # 关键数据行 — 加大到8.5pt
        'info': ParagraphStyle('Info', fontName=cjk_font, fontSize=8, leading=11,
                                textColor=C_TEXT_LIGHT, wordWrap='CJK'),
        'value': ParagraphStyle('Value', fontName=bold, fontSize=8, leading=11,
                                 textColor=C_GREEN, wordWrap='CJK'),
        # M串N
        'msn_small': ParagraphStyle('MsnSmall', fontName=cjk_font, fontSize=7, leading=9,
                                     textColor=C_TEXT_LIGHT, wordWrap='CJK'),
    }


def normalize_text(text):
    if not text:
        return ''
    replacements = {'\u2013': '-', '\u2014': '-', '\u2018': "'", '\u2019': "'",
                    '\u201c': '"', '\u201d': '"', '\u2212': '-'}
    for old, new in replacements.items():
        text = text.replace(old, new)
    return str(text)


def draw_bg(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(C_BG)
    canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    canvas.restoreState()


def conf_to_color(conf_str):
    if not conf_str:
        return C_TEXT_LIGHT
    count = 0
    for c in conf_str:
        if c == '★':
            count += 1
        elif c == '½':
            count += 0.5
    if count >= 4.5:
        return C_GREEN
    elif count >= 3.5:
        return C_BLUE
    elif count >= 2.5:
        return C_ORANGE
    return C_TEXT_LIGHT


# ============ 汇总表 (推荐+赔率合并一格, Ultra 9.0: 加EV/价值标签) ============
def build_summary_table(matches, styles):
    """汇总表: 推荐选项和赔率放在同一个格子里, 更直观"""
    data = [[
        Paragraph('场次', styles['th']),
        Paragraph('对阵', styles['th']),
        Paragraph('主推荐', styles['th']),
        Paragraph('次推荐', styles['th']),
    ]]

    for m in matches:
        first = m['first']
        second = m['second']

        # 场次
        key_text = normalize_text(m['key'])

        # 对阵 (主队 vs 客队换行)
        vs_text = f'{normalize_text(m["home"])}<br/>vs<br/>{normalize_text(m["away"])}'
        vs_style = ParagraphStyle('vs', parent=styles['td'], fontSize=7.5, leading=10)

        # 主推荐: 选项 + 赔率 + 概率 + EV + 星 合并一格
        if first:
            first_conf = normalize_text(first.get('conf', ''))
            first_conf_color = conf_to_color(first_conf)
            first_ev = first.get('ev_pct', 0)
            first_ev_color = '#16a34a' if first_ev > 0 else '#dc2626'
            first_html = (
                f'<font name="CJK-Bold" color="#16a34a" size="8">{normalize_text(first["name"])}</font><br/>'
                f'<font name="CJK-Bold" color="#ea580c" size="11">@{first["odds"]}</font> '
                f'<font color="#475569" size="7">P{first["prob"]:.0f}%</font> '
                f'<font color="{first_ev_color}" size="7">EV{first_ev:+.0f}%</font><br/>'
                f'<font color="#{first_conf_color.hexval()[2:]}" size="7">{first_conf}</font>'
            )
            first_cell = Paragraph(first_html, styles['td'])
        else:
            first_cell = Paragraph('-', styles['td'])

        # 次推荐: 同样合并, 也加EV
        if second:
            second_conf = normalize_text(second.get('conf', ''))
            second_conf_color = conf_to_color(second_conf)
            second_ev = second.get('ev_pct', 0)
            second_ev_color = '#16a34a' if second_ev > 0 else '#475569'
            second_html = (
                f'<font name="CJK-Bold" color="#2563eb" size="8">{normalize_text(second["name"])}</font><br/>'
                f'<font name="CJK-Bold" color="#ea580c" size="11">@{second["odds"]}</font> '
                f'<font color="#475569" size="7">P{second["prob"]:.0f}%</font> '
                f'<font color="{second_ev_color}" size="7">EV{second_ev:+.0f}%</font><br/>'
                f'<font color="#{second_conf_color.hexval()[2:]}" size="7">{second_conf}</font>'
            )
            second_cell = Paragraph(second_html, styles['td'])
        else:
            second_cell = Paragraph('-', styles['td'])

        data.append([
            Paragraph(key_text, styles['td_bold']),
            Paragraph(vs_text, vs_style),
            first_cell,
            second_cell,
        ])

    # A6宽度 ~95mm: 场次18mm, 对阵24mm, 主推28mm, 次推29mm (EV挤一挤)
    col_widths = [16*mm, 22*mm, 29*mm, 28*mm]
    table = LongTable(data, colWidths=col_widths, repeatRows=1)

    table.setStyle([
        ('BACKGROUND', (0, 0), (-1, 0), C_HEADER_BG),
        ('TOPPADDING', (0, 0), (-1, 0), 5),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [C_ROW_ALT1, C_ROW_ALT2]),
        ('GRID', (0, 0), (-1, -1), 0.5, C_BORDER),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 1), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 3),
        ('RIGHTPADDING', (0, 0), (-1, -1), 3),
    ])
    return table


# ============ 精简比赛卡片 ============
def build_match_card(m, styles):
    elements = []

    # 标题栏
    header_data = [[
        Paragraph(f'{m["key"]} {normalize_text(m["home"])} vs {normalize_text(m["away"])}', styles['match_title']),
        Paragraph(f'{normalize_text(m["league"])} {m["match_time"]}', styles['match_info']),
    ]]
    header = Table(header_data, colWidths=[CW * 0.62, CW * 0.38])
    header.setStyle([
        ('BACKGROUND', (0, 0), (-1, -1), C_CARD_BG),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('LINEBELOW', (0, 0), (-1, -1), 1.5, C_HEADER_BG),
    ])
    elements.append(header)

    # 两推荐并排
    first = m['first']
    second = m['second']

    def make_rec_cell(rec, label, bg_color, border_color):
        if not rec:
            return Paragraph('-', styles['td'])

        name = normalize_text(rec['name'])
        odds = rec['odds']
        conf = normalize_text(rec.get('conf', ''))
        prob = rec.get('prob', 0)
        ev = rec.get('ev_pct', 0)
        implied = rec.get('implied_prob', 0)

        # Ultra 9.0: EV为正显示绿色价值标签, 为负显示红色
        if ev > 0:
            ev_label = '✓价值'
            ev_color = '#16a34a'
        else:
            ev_label = f'EV{ev:+.0f}%'
            ev_color = '#dc2626'

        conf_color = conf_to_color(conf)
        conf_html = f'<font color="#{conf_color.hexval()[2:]}">{conf}</font>'

        # Edge: 模型概率 - 隐含概率
        edge = prob - implied
        edge_color = '#16a34a' if edge > 0 else '#475569'

        inner = [
            [Paragraph(label, styles['rec_label'])],
            [Paragraph(name, styles['rec_name'])],
            [Paragraph(f'@{odds}', styles['rec_odds'])],
            [Paragraph(f'P{prob:.0f}%  {conf_html}', styles['rec_detail'])],
            [Paragraph(f'<font color="{ev_color}">{ev_label}</font> | '
                       f'<font color="{edge_color}">优势{edge:+.0f}%</font>',
                       styles['rec_detail'])],
        ]
        inner_table = Table(inner, colWidths=[CW * 0.48])
        inner_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), bg_color),
            ('BOX', (0, 0), (-1, -1), 1, border_color),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ])
        return inner_table

    rec_row = [[
        make_rec_cell(first, '主推荐', C_GREEN_BG, C_GREEN),
        make_rec_cell(second, '次推荐', C_BLUE_BG, C_BLUE),
    ]]
    rec_table = Table(rec_row, colWidths=[CW * 0.5, CW * 0.5])
    rec_table.setStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ])
    elements.append(rec_table)

    # 关键数据行 — 精简, 只保留最核心的
    goals = m.get('goals', {})
    score_info = m.get('score_info', {})
    half_full = m.get('half_full', {})
    total_goals = m.get('total_goals', {})
    data_source = m.get('data_source', '')

    parts = []
    if goals:
        te = goals.get('total_expected', '')
        ou = goals.get('over_under', '')
        if te:
            parts.append(f'预期{te}球({ou})')
    if half_full.get('main'):
        parts.append(f'半全场 {normalize_text(half_full["main"])}')
    if total_goals.get('main'):
        parts.append(f'总进球 {normalize_text(total_goals["main"])}')
    if score_info.get('top3'):
        # 只取第一个比分
        top1 = normalize_text(score_info["top3"]).split()[0]
        parts.append(f'比分 {top1}')
    if data_source:
        ds_label = '500.com' if '500' in data_source else ('nowscore' if 'nowscore' in data_source else '体彩')
        parts.append(ds_label)

    if parts:
        info_text = '  |  '.join(parts)
        info_data = [[Paragraph(info_text, styles['info'])]]
        info_table = Table(info_data, colWidths=[CW])
        info_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), C_CARD_BG),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ])
        elements.append(info_table)

    # 价值投注 (仅EV>0, 精简显示)
    sp_pools = m.get('sporttery_pools') or {}
    value_parts = []

    hafu_odds = {p['option']: p for p in (sp_pools.get('hafu') or [])}
    hf_top3 = half_full.get('top3', '')
    if hf_top3:
        for tok in hf_top3.split():
            if ':' in tok:
                name, pct = tok.rsplit(':', 1)
                o = hafu_odds.get(name)
                if o and o['ev_pct'] > 0:
                    value_parts.append(f'{name}@{o["odds"]} +{o["ev_pct"]:.0f}%')

    if sp_pools.get('ttg'):
        for p in sp_pools['ttg'][:2]:
            if p['ev_pct'] > 0:
                value_parts.append(f'{p["option"]}@{p["odds"]} +{p["ev_pct"]:.0f}%')

    if sp_pools.get('crs'):
        for p in sp_pools['crs'][:2]:
            if p['ev_pct'] > 0:
                value_parts.append(f'{p["option"]}@{p["odds"]} +{p["ev_pct"]:.0f}%')

    if value_parts:
        # 最多显示3个, 避免太长
        val_text = '  '.join(value_parts[:3])
        val_data = [[Paragraph(f'★ {val_text}', styles['value'])]]
        val_table = Table(val_data, colWidths=[CW])
        val_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), C_GREEN_BG),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('LINEBELOW', (0, 0), (-1, -1), 0.5, C_GREEN),
        ])
        elements.append(val_table)

    elements.append(Spacer(1, 4))
    return elements


# ============ M串N精简 ============
def build_msn_section(pred_file, styles):
    try:
        from msn_simulator import extract_had_bets, simulate_combo, COMBO_TABLE
    except ImportError:
        return None
    try:
        bets = extract_had_bets(pred_file)
        M = len(bets)
        if M < 3:
            return None
        if M > 8:
            bets = sorted(bets, key=lambda b: b['prob'], reverse=True)[:8]
            M = 8

        combos = COMBO_TABLE.get(M, {})
        rows = [(f'{M}串1', simulate_combo(bets, [M]))]
        for name, folds in combos.items():
            rows.append((name, simulate_combo(bets, folds)))
        rows.sort(key=lambda x: x[1]['roi'], reverse=True)

        # 只显示Top5
        rows = rows[:5]

        els = [Paragraph('M串N 推荐Top5', styles['section'])]

        # 精简: 只显示 过关/中奖率/ROI
        header = [Paragraph('过关', styles['th']), Paragraph('中奖率', styles['th']), Paragraph('ROI', styles['th'])]
        tbl = [header]
        for name, r in rows:
            roi_color = C_GREEN if r['roi'] > -10 else (C_ORANGE if r['roi'] > -20 else C_RED)
            roi_style = ParagraphStyle('roi', parent=styles['td_bold'], textColor=roi_color)
            tbl.append([
                Paragraph(name, styles['td_bold']),
                Paragraph(f"{r['p_any_win']:.0%}", styles['td']),
                Paragraph(f"{r['roi']:+.1f}%", roi_style),
            ])
        t = Table(tbl, colWidths=[CW * 0.35, CW * 0.32, CW * 0.33])
        t.setStyle([
            ('BACKGROUND', (0, 0), (-1, 0), C_HEADER_BG),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [C_ROW_ALT1, C_ROW_ALT2]),
            ('GRID', (0, 0), (-1, -1), 0.5, C_BORDER),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ])
        # 高亮第1名
        if len(tbl) > 1:
            t.setStyle([('BACKGROUND', (0, 1), (-1, 1), C_GREEN_BG)])
        els.append(t)

        best = rows[0]
        els.append(Paragraph(f"推荐: {best[0]} (中奖{best[1]['p_any_win']:.0%}/ROI{best[1]['roi']:+.1f}%)", styles['msn_small']))
        return els
    except Exception as ex:
        print(f'  [M串N] 生成失败: {ex}')
        return None


# ============ 主函数 ============
def main():
    cjk = register_cjk_font()
    styles = get_styles(cjk)

    with open(PRED_FILE, 'r', encoding='utf-8') as f:
        pred_data = json.load(f)

    results = pred_data.get('results', {})
    meta = pred_data.get('meta', {})

    matches = []
    for key in sorted(results.keys()):
        m = rank_match(key, meta.get(key, {}), results[key])
        matches.append(m)

    print("=" * 50)
    print("手机版预测报告")
    print("=" * 50)
    for m in matches:
        f_str = f'{m["first"]["name"]}@{m["first"]["odds"]}' if m['first'] else 'N/A'
        s_str = f'{m["second"]["name"]}@{m["second"]["odds"]}' if m['second'] else 'N/A'
        print(f'{m["key"]} {m["home"]}vs{m["away"]} | 主推:{f_str} | 次推:{s_str}')

    doc = SimpleDocTemplate(
        OUTPUT_PDF, pagesize=PAGE_SIZE,
        leftMargin=LM, rightMargin=RM, topMargin=TM, bottomMargin=BM,
        title=f'预测报告 {REPORT_TITLE}',
    )

    story = []

    # 标题栏
    now_str = datetime.now().strftime('%m-%d %H:%M')
    title_data = [[
        Paragraph('竞彩预测报告', styles['title']),
    ]]
    title_table = Table(title_data, colWidths=[CW])
    title_table.setStyle([
        ('BACKGROUND', (0, 0), (-1, -1), C_TITLE_BG),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ])
    story.append(title_table)

    meta_data = [[Paragraph(f'{REPORT_TITLE} | {len(matches)}场 | {now_str}', styles['subtitle'])]]
    meta_table = Table(meta_data, colWidths=[CW])
    meta_table.setStyle([
        ('BACKGROUND', (0, 0), (-1, -1), C_TITLE_BG),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ])
    story.append(meta_table)
    story.append(Spacer(1, 4))

    # 汇总表
    story.append(Paragraph('全场汇总', styles['section']))
    story.append(build_summary_table(matches, styles))

    # 逐场卡片
    story.append(Spacer(1, 4))
    story.append(Paragraph('逐场推荐', styles['section']))

    for m in matches:
        card = build_match_card(m, styles)
        story.extend(card)

    # M串N
    msn_section = build_msn_section(PRED_FILE, styles)
    if msn_section:
        story.append(Spacer(1, 2))
        story.extend(msn_section)

    doc.build(story, onFirstPage=draw_bg, onLaterPages=draw_bg)
    print(f'\nPDF报告已生成: {OUTPUT_PDF}')


if __name__ == '__main__':
    main()
