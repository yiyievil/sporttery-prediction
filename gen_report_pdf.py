#!/usr/bin/env python3
"""预测报告PDF生成器 (手机阅读优化版)

直接从预测JSON生成PDF报告，每场明确标注第一推荐和第二推荐。
复用 gen_report_v2.py 的 rank_match 逻辑。

v2: 手机阅读优化 — 放大字体、明亮高对比配色、宽行距
"""

import json
import os
import sys
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, LongTable,
)

# 复用 gen_report_v2 的排名逻辑与标题派生
from gen_report_v2 import rank_match, REPORT_TITLE
from pdf_fonts import register_cjk_font

# Ultra-Opt: 通用路径 — 优先命令行参数, 缺省 SPORTTERY_WORKSPACE/脚本目录
_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
_PRED_DIR = os.path.join(_WORKSPACE, 'predictions')

# 用法: python gen_report_pdf.py [pred文件] [输出pdf]
PRED_FILE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_PRED_DIR, 'pred_20260725_周六.json')
if len(sys.argv) > 2:
    OUTPUT_PDF = sys.argv[2]
else:
    _base = os.path.basename(PRED_FILE).replace('pred_', 'report_').replace('.json', '.pdf')
    OUTPUT_PDF = os.path.join(os.path.dirname(PRED_FILE) or _PRED_DIR, _base)

# ============ 页面常量 (手机优化: 减小边距, 增大内容区) ============
PAGE_SIZE = A4
PAGE_W, PAGE_H = PAGE_SIZE
LM = 10 * mm    # 左边距缩小 (15→10)
RM = 10 * mm    # 右边距缩小
TM = 12 * mm    # 上边距缩小 (18→12)
BM = 12 * mm    # 下边距缩小
CW = PAGE_W - LM - RM  # content width

# ============ 明亮配色方案 (手机阅读优化) ============
# 背景
BG_PAGE      = HexColor('#ffffff')   # 纯白页面背景
BG_HEADER    = HexColor('#1a56db')   # 明亮蓝标题栏
BG_CARD      = HexColor('#f0f5ff')   # 浅蓝卡片背景
BG_CARD_ALT  = HexColor('#e6effd')   # 浅蓝交替行
BG_SWOT      = HexColor('#fefce8')   # 浅黄SWOT栏
BG_EXTRA     = HexColor('#f8fafc')   # 极浅灰额外信息
BG_POOL      = HexColor('#fffbeb')   # 浅橙玩法区
BG_REC1      = HexColor('#ecfdf5')   # 浅绿第一推荐
BG_REC2      = HexColor('#eff6ff')   # 浅蓝第二推荐
BG_MSN_HEAD  = HexColor('#065f46')   # 深绿M串N表头
BG_MSN_HI    = HexColor('#d1fae5')   # 浅绿高亮行

# 文字
INK_DARK     = HexColor('#0f172a')   # 深黑 (标题/重要文字)
INK_BODY     = HexColor('#1e293b')   # 深灰 (正文)
INK_MUTED    = HexColor('#475569')   # 中灰 (次要文字)
INK_WHITE    = colors.white

# 强调色
ACCENT_BLUE  = HexColor('#1d4ed8')   # 明亮蓝
ACCENT_GREEN = HexColor('#059669')   # 明亮绿
ACCENT_RED   = HexColor('#dc2626')   # 明亮红
ACCENT_AMBER = HexColor('#d97706')   # 明亮橙
ACCENT_TEAL  = HexColor('#0891b2')   # 明亮青

# 边框
BORDER_LIGHT = HexColor('#cbd5e1')   # 浅灰边框
BORDER_BLUE  = HexColor('#3b82f6')   # 蓝色边框
BORDER_GREEN = HexColor('#10b981')   # 绿色边框

# ============ 样式 (手机优化: 放大字体 + 宽行距) ============
def get_styles(cjk_font='CJK'):
    """获取样式字典 — 手机阅读优化版"""
    bold_font = 'CJK-Bold' if cjk_font == 'CJK' else cjk_font
    return {
        'title': ParagraphStyle('Title', fontName=bold_font, fontSize=28, leading=36,
                                 textColor=INK_WHITE, alignment=TA_CENTER, spaceAfter=8, wordWrap='CJK'),
        'subtitle': ParagraphStyle('Subtitle', fontName=cjk_font, fontSize=14, leading=20,
                                   textColor=HexColor('#bfdbfe'), alignment=TA_CENTER, spaceAfter=6, wordWrap='CJK'),
        'section': ParagraphStyle('Section', fontName=bold_font, fontSize=20, leading=28,
                                   textColor=ACCENT_BLUE, spaceBefore=24, spaceAfter=12, wordWrap='CJK'),
        'match_title': ParagraphStyle('MatchTitle', fontName=bold_font, fontSize=17, leading=24,
                                       textColor=INK_DARK, wordWrap='CJK'),
        'match_info': ParagraphStyle('MatchInfo', fontName=cjk_font, fontSize=13, leading=18,
                                      textColor=INK_MUTED, alignment=2, wordWrap='CJK'),
        'label': ParagraphStyle('Label', fontName=bold_font, fontSize=14, leading=20,
                                 textColor=ACCENT_GREEN, wordWrap='CJK'),
        'label2': ParagraphStyle('Label2', fontName=bold_font, fontSize=14, leading=20,
                                 textColor=ACCENT_BLUE, wordWrap='CJK'),
        'rec_main': ParagraphStyle('RecMain', fontName=bold_font, fontSize=16, leading=24,
                                    textColor=INK_DARK, wordWrap='CJK'),
        'body': ParagraphStyle('Body', fontName=cjk_font, fontSize=13, leading=20,
                               textColor=INK_BODY, wordWrap='CJK'),
        'small': ParagraphStyle('Small', fontName=cjk_font, fontSize=12, leading=18,
                                 textColor=INK_MUTED, wordWrap='CJK'),
        'th': ParagraphStyle('Th', fontName=bold_font, fontSize=12, leading=16,
                              textColor=INK_WHITE, alignment=TA_CENTER, wordWrap='CJK'),
        'td': ParagraphStyle('Td', fontName=cjk_font, fontSize=12, leading=16,
                             textColor=INK_BODY, alignment=TA_CENTER, wordWrap='CJK'),
        'td_first': ParagraphStyle('TdFirst', fontName=bold_font, fontSize=12, leading=16,
                                    textColor=ACCENT_GREEN, alignment=TA_CENTER, wordWrap='CJK'),
        'td_second': ParagraphStyle('TdSecond', fontName=bold_font, fontSize=12, leading=16,
                                     textColor=ACCENT_BLUE, alignment=TA_CENTER, wordWrap='CJK'),
        'td_score': ParagraphStyle('TdScore', fontName=bold_font, fontSize=12, leading=16,
                                    textColor=ACCENT_AMBER, alignment=TA_CENTER, wordWrap='CJK'),
    }


def normalize_text(text):
    """规范化文本"""
    if not text:
        return ''
    replacements = {'\u2013': '-', '\u2014': '-', '\u2018': "'", '\u2019': "'",
                    '\u201c': '"', '\u201d': '"', '\u2212': '-'}
    for old, new in replacements.items():
        text = text.replace(old, new)
    return str(text)


# ============ 背景色绘制 (明亮白底) ============
def draw_bg(canvas, doc):
    """绘制明亮白色背景"""
    canvas.saveState()
    canvas.setFillColor(BG_PAGE)
    canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    canvas.restoreState()


# ============ 汇总表 ============
def build_summary_table(matches, styles):
    """构建汇总表 — 明亮配色"""
    data = [[
        Paragraph('场次', styles['th']),
        Paragraph('联赛', styles['th']),
        Paragraph('主队', styles['th']),
        Paragraph('客队', styles['th']),
        Paragraph('第一推荐', styles['th']),
        Paragraph('赔率', styles['th']),
        Paragraph('第二推荐', styles['th']),
        Paragraph('赔率', styles['th']),
        Paragraph('SWOT', styles['th']),
    ]]

    for m in matches:
        first = m['first']
        second = m['second']

        row = [
            Paragraph(normalize_text(m['key']), styles['td']),
            Paragraph(normalize_text(m['league']), styles['td']),
            Paragraph(normalize_text(m['home']), styles['td']),
            Paragraph(normalize_text(m['away']), styles['td']),
        ]

        if first:
            row.append(Paragraph(normalize_text(first['name']), styles['td_first']))
            row.append(Paragraph(f'{first["odds"]}', styles['td_score']))
        else:
            row.extend([Paragraph('-', styles['td']), Paragraph('-', styles['td'])])

        if second:
            row.append(Paragraph(normalize_text(second['name']), styles['td_second']))
            row.append(Paragraph(f'{second["odds"]}', styles['td_score']))
        else:
            row.extend([Paragraph('-', styles['td']), Paragraph('-', styles['td'])])

        swot_color = INK_MUTED
        if '占优' in m['swot_lean']:
            if '主' in m['swot_lean']:
                swot_color = ACCENT_GREEN
            else:
                swot_color = ACCENT_RED
        swot_style = ParagraphStyle('swot', parent=styles['td'],
                                     textColor=swot_color, fontName='CJK-Bold')
        row.append(Paragraph(normalize_text(m['swot_lean']), swot_style))

        data.append(row)

    col_widths = [28*mm, 16*mm, 24*mm, 24*mm, 32*mm, 12*mm, 32*mm, 12*mm, 18*mm]
    table = LongTable(data, colWidths=col_widths, repeatRows=1)

    table.setStyle([
        # 表头: 明亮蓝底白字
        ('BACKGROUND', (0, 0), (-1, 0), BG_HEADER),
        # 数据行: 白色/浅蓝交替
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [HexColor('#ffffff'), BG_CARD_ALT]),
        # 网格线
        ('GRID', (0, 0), (-1, -1), 0.5, BORDER_LIGHT),
        ('LINEBELOW', (0, 0), (-1, 0), 1.5, ACCENT_BLUE),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        # 内边距加大 (手机阅读更舒适)
        ('TOPPADDING', (0, 0), (-1, 0), 8),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('TOPPADDING', (0, 1), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 7),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
    ])

    return table


# ============ 比赛卡片 ============
def build_match_card(m, styles):
    """构建单场比赛的推荐卡片 — 明亮配色"""
    elements = []

    # 比赛标题行
    header_data = [[
        Paragraph(f'{m["key"]} · {normalize_text(m["home"])} vs {normalize_text(m["away"])}', styles['match_title']),
        Paragraph(f'{normalize_text(m["league"])} · {m["match_time"]}', styles['match_info']),
    ]]
    header = Table(header_data, colWidths=[CW * 0.65, CW * 0.35])
    header.setStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BG_CARD),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING', (0, 0), (-1, -1), 14),
        ('RIGHTPADDING', (0, 0), (-1, -1), 14),
        ('LINEBELOW', (0, 0), (-1, -1), 2, ACCENT_BLUE),
    ])
    elements.append(header)

    # SWOT信息栏
    swot_text = normalize_text(m['swot_lean'])
    swot_adj = normalize_text(m.get('swot_adjust', ''))
    swot_color = INK_MUTED
    if '占优' in swot_text:
        if '主' in swot_text:
            swot_color = ACCENT_GREEN
        else:
            swot_color = ACCENT_RED

    swot_data = [[
        Paragraph(f'<font color="#{swot_color.hexval()[2:]}"><b>SWOT: {swot_text}</b></font>', styles['body']),
        Paragraph(f'置信调整: <b>{swot_adj}</b>', styles['body']),
    ]]
    if m.get('swot_key_factor'):
        kf = normalize_text(m['swot_key_factor'])[:80]
        swot_data[0].append(Paragraph(f'关键: {kf}...', styles['small']))

    swot_bar = Table(swot_data, colWidths=[CW * 0.25, CW * 0.2, CW * 0.55])
    swot_bar.setStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BG_SWOT),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ('LEFTPADDING', (0, 0), (-1, -1), 14),
    ])
    elements.append(swot_bar)

    # 第一推荐 + 第二推荐 并排
    first = m['first']
    second = m['second']

    def make_rec_cell(rec, label, label_style, bg_color, border_color):
        """构建推荐单元格"""
        if not rec:
            return Paragraph('-', styles['body'])

        name = normalize_text(rec['name'])
        odds = rec['odds']
        conf = normalize_text(rec['conf'])
        prob = rec.get('prob', 0)
        market = normalize_text(rec.get('market', ''))
        cov = normalize_text(rec.get('coverage_type', ''))
        ev = rec.get('ev_pct', 0)
        score = rec.get('score', 0)

        inner = [
            [Paragraph(label, label_style)],
            [Paragraph(f'{name} @{odds} {conf}', styles['rec_main'])],
            [Paragraph(f'概率: {prob:.1f}% | 市场: {market} | {cov} | EV: {ev:.1f}%', styles['body'])],
            [Paragraph(f'综合评分: {score}', styles['small'])],
        ]
        inner_table = Table(inner, colWidths=[CW * 0.48])
        inner_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), bg_color),
            ('BOX', (0, 0), (-1, -1), 2, border_color),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ])
        return inner_table

    rec_row = [[
        make_rec_cell(first, '第一推荐', styles['label'], BG_REC1, BORDER_GREEN),
        make_rec_cell(second, '第二推荐', styles['label2'], BG_REC2, BORDER_BLUE),
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

    # 额外信息
    goals = m.get('goals', {})
    score_info = m.get('score_info', {})
    half_full = m.get('half_full', {})
    total_goals = m.get('total_goals', {})
    data_quality = m.get('data_quality', {})

    extra_parts = []
    if goals:
        he = goals.get('home_expected', '')
        ae = goals.get('away_expected', '')
        te = goals.get('total_expected', '')
        ou = goals.get('over_under', '')
        gl = score_info.get('market_gl_str', '')
        extra_parts.append(f'预期进球: {he}-{ae} (总{te}) | 大小: {ou} {gl}')
    if half_full and half_full.get('main'):
        extra_parts.append(f'半全场: {normalize_text(half_full["main"])}')
    if total_goals and total_goals.get('main'):
        extra_parts.append(f'总进球: {normalize_text(total_goals["main"])}')
    if data_quality and data_quality.get('quality'):
        extra_parts.append(f'数据质量: {data_quality["quality"]}({data_quality.get("score","")})')
    extra_parts.append(f'难度: {m.get("difficulty",0):.1f}')

    extra_text = '  |  '.join(extra_parts)
    extra_data = [[Paragraph(extra_text, styles['small'])]]
    extra_table = Table(extra_data, colWidths=[CW])
    extra_table.setStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BG_EXTRA),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 14),
    ])
    elements.append(extra_table)

    # Ultra 6.5: 竞彩官方玩法区块 (半全场/总进球/比分 — 官方赔率 × 模型概率 EV)
    sp_pools = m.get('sporttery_pools') or {}
    pool_lines = []
    hf_top3 = half_full.get('top3', '')
    if hf_top3:
        hafu_odds = {p['option']: p for p in (sp_pools.get('hafu') or [])}
        parts = []
        for tok in hf_top3.split():
            if ':' in tok:
                name, pct = tok.rsplit(':', 1)
                o = hafu_odds.get(name)
                if o:
                    flag = '[值]' if o['ev_pct'] > 0 else ''
                    parts.append(f"{name} {pct}%(@{o['odds']} EV{o['ev_pct']:+.0f}%){flag}")
                else:
                    parts.append(f"{name} {pct}%")
        if parts:
            pool_lines.append('半全场: ' + ' | '.join(parts))
    if sp_pools.get('ttg'):
        parts = []
        for p in sp_pools['ttg'][:3]:
            flag = '[值]' if p['ev_pct'] > 0 else ''
            parts.append(f"{p['option']} {p['prob']}%(@{p['odds']} EV{p['ev_pct']:+.0f}%){flag}")
        pool_lines.append('总进球: ' + ' | '.join(parts))
    elif total_goals.get('top3'):
        pool_lines.append('总进球(模型): ' + normalize_text(total_goals['top3']))
    if sp_pools.get('crs'):
        parts = []
        for p in sp_pools['crs'][:3]:
            flag = '[值]' if p['ev_pct'] > 0 else ''
            parts.append(f"{p['option']} {p['prob']}%(@{p['odds']} EV{p['ev_pct']:+.0f}%){flag}")
        pool_lines.append('比分: ' + ' | '.join(parts))
    elif score_info.get('top3'):
        pool_lines.append('比分(模型): ' + normalize_text(score_info['top3']))

    if pool_lines:
        for line in pool_lines:
            pl_data = [[Paragraph(normalize_text(line), styles['small'])]]
            pl_table = Table(pl_data, colWidths=[CW])
            pl_table.setStyle([
                ('BACKGROUND', (0, 0), (-1, -1), BG_POOL),
                ('TOPPADDING', (0, 0), (-1, -1), 5),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                ('LEFTPADDING', (0, 0), (-1, -1), 14),
            ])
            elements.append(pl_table)

    # 所有选项评分
    if m.get('all_options'):
        all_opts = ' | '.join(f'{normalize_text(o["name"])}@{o["odds"]}({o["score"]})' for o in m['all_options'])
        opts_data = [[Paragraph(f'全部选项: {all_opts}', styles['small'])]]
        opts_table = Table(opts_data, colWidths=[CW])
        opts_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), BG_EXTRA),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 14),
        ])
        elements.append(opts_table)

    elements.append(Spacer(1, 10))
    return elements


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

    # 控制台输出
    print("=" * 60)
    print("预测报告 PDF (手机阅读优化版) - 第一/第二推荐")
    print("=" * 60)
    for m in matches:
        f_str = f'{m["first"]["name"]}@{m["first"]["odds"]}({m["first"]["score"]})' if m['first'] else 'N/A'
        s_str = f'{m["second"]["name"]}@{m["second"]["odds"]}({m["second"]["score"]})' if m['second'] else 'N/A'
        print(f'{m["key"]} {m["home"]}vs{m["away"]} | 1:{f_str} | 2:{s_str}')

    # 生成PDF
    doc = SimpleDocTemplate(
        OUTPUT_PDF, pagesize=PAGE_SIZE,
        leftMargin=LM, rightMargin=RM, topMargin=TM, bottomMargin=BM,
        title=f'预测报告 {REPORT_TITLE}',
    )

    story = []

    # 标题 (明亮蓝底白字)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    title_data = [[
        Paragraph(f'预测报告 · {REPORT_TITLE}', styles['title']),
    ]]
    title_table = Table(title_data, colWidths=[CW])
    title_table.setStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BG_HEADER),
        ('TOPPADDING', (0, 0), (-1, -1), 16),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 16),
    ])
    story.append(title_table)

    meta_data = [[Paragraph(f'共 {len(matches)} 场 | 生成时间 {now_str} | nowscore/500.com + SWOT融合', styles['subtitle'])]]
    meta_table = Table(meta_data, colWidths=[CW])
    meta_table.setStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BG_HEADER),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ])
    story.append(meta_table)
    story.append(Spacer(1, 14))

    # 汇总表
    story.append(Paragraph('全场汇总', styles['section']))
    story.append(build_summary_table(matches, styles))

    # 逐场详情
    story.append(Spacer(1, 18))
    story.append(Paragraph('逐场推荐详情', styles['section']))

    for m in matches:
        card = build_match_card(m, styles)
        story.extend(card)

    # Ultra 6.5: M串N 容错过关推荐
    msn_section = build_msn_section(PRED_FILE, styles)
    if msn_section:
        story.append(Spacer(1, 10))
        story.extend(msn_section)

    doc.build(story, onFirstPage=draw_bg, onLaterPages=draw_bg)
    print(f'\nPDF报告已生成 (手机阅读优化版): {OUTPUT_PDF}')


def build_msn_section(pred_file, styles):
    """M串N 容错过关推荐区块 — 明亮配色"""
    try:
        from msn_simulator import extract_had_bets, simulate_combo, poisson_binomial_probs, COMBO_TABLE
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

        dist = poisson_binomial_probs([b['prob'] for b in bets])

        els = [Paragraph('M串N 容错过关推荐 (官方32种组合)', styles['section'])]

        # 场次明细 — 显示市场类型(HAD/HHAD)和让球数
        leg_parts = []
        for b in bets:
            mkt = b.get('market', 'HAD')
            hc = f"让{b['handicap']}" if 'handicap' in b else ''
            leg_parts.append(f"{b['key']}[{mkt}]{hc} {b['dir']}@{b['odds']}(P{b['prob']:.0%})")
        legs = ' | '.join(leg_parts)
        els.append(Paragraph(normalize_text(f'串关场次({M}场HAD/HHAD最优): {legs}'), styles['small']))
        dist_str = ' '.join(f"{x}场:{dist[x]:.0%}" for x in range(1, M + 1) if dist[x] >= 0.01)
        els.append(Paragraph(normalize_text(f'命中场数分布: {dist_str}'), styles['small']))

        # 推荐表: 全部组合 (按ROI排序)
        header = ['过关方式', '注数', '成本', '中奖条件', '中奖概率', '期望盈亏', 'ROI']
        tbl = [header]
        for name, r in rows:
            tbl.append([name, str(r['n_bets']), f"{r['cost']:.0f}元", f"中≥{r['min_fold']}场",
                        f"{r['p_any_win']:.0%}", f"{r['exp_profit']:+.1f}元", f"{r['roi']:+.1%}"])
        t = Table(tbl, colWidths=[CW * 0.16, CW * 0.08, CW * 0.12, CW * 0.16, CW * 0.14, CW * 0.17, CW * 0.17])
        t.setStyle([
            # 明亮绿表头
            ('BACKGROUND', (0, 0), (-1, 0), BG_MSN_HEAD),
            ('TEXTCOLOR', (0, 0), (-1, 0), INK_WHITE),
            ('GRID', (0, 0), (-1, -1), 0.5, BORDER_LIGHT),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('FONTNAME', (0, 0), (-1, -1), styles['small'].fontName),
            ('FONTSIZE', (0, 0), (-1, -1), styles['small'].fontSize),
            ('TEXTCOLOR', (0, 1), (-1, -1), INK_BODY),
            # 数据行交替色
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [HexColor('#ffffff'), HexColor('#f0fdf4')]),
        ])
        # 高亮前3名
        for i in range(1, min(4, len(tbl))):
            t.setStyle([('BACKGROUND', (0, i), (-1, i), BG_MSN_HI)])
        els.append(t)

        best_roi = rows[0]
        best_hit = max(rows, key=lambda x: x[1]['p_any_win'])
        els.append(Paragraph(normalize_text(
            f"按目标: 最高ROI {best_roi[0]}(中奖{best_roi[1]['p_any_win']:.0%}/ROI{best_roi[1]['roi']:+.1%}) | "
            f"最高命中率 {best_hit[0]}(中奖{best_hit[1]['p_any_win']:.0%}/ROI{best_hit[1]['roi']:+.1%}) | "
            f"均衡容错1场 选{M}串{M}或{M}串{M + 1}"), styles['small']))
        els.append(Paragraph(normalize_text(
            '注: EV线性原理下各组合ROI接近, 选择本质是命中率与单注奖金的权衡; 实际奖金以出票时刻SP为准'), styles['small']))
        return els
    except Exception as ex:
        print(f'  [M串N] 推荐区块生成失败(不影响报告): {ex}')
        return None


if __name__ == '__main__':
    main()
