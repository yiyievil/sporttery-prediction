#!/usr/bin/env python3
"""验证报告PDF生成器 (手机阅读优化版)

从验证HTML报告生成PDF，采用与预测报告相同的明亮配色和手机优化布局。
复用 gen_report_pdf.py 的字体注册和配色方案。

用法: python3 gen_verify_pdf.py [verify_html] [output_pdf]
"""

import re
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
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ============================================================
# 路径配置
# ============================================================
_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))

# 命令行参数
VERIFY_HTML = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_WORKSPACE, 'verify_20260729.html')
if len(sys.argv) > 2:
    OUTPUT_PDF = sys.argv[2]
else:
    _base = os.path.basename(VERIFY_HTML).replace('.html', '.pdf')
    OUTPUT_PDF = os.path.join(os.path.dirname(VERIFY_HTML) or _WORKSPACE, _base)

# ============================================================
# 字体注册 (复用 gen_report_pdf 逻辑)
# ============================================================
def register_cjk_font():
    # 霞鹜文楷 (LxgwWenKai) — 与预测PDF保持一致
    font_reg = '/usr/share/fonts/truetype/LXGWWenKai-Regular.ttf'
    font_bold = '/usr/share/fonts/truetype/LXGWWenKai-Medium.ttf'
    if os.path.exists(font_reg) and os.path.exists(font_bold):
        pdfmetrics.registerFont(TTFont('CJK', font_reg))
        pdfmetrics.registerFont(TTFont('CJK-Bold', font_bold))
        return 'CJK'

    # 回退: 系统字体查找
    from pathlib import Path
    candidates = []
    env_dir = os.environ.get('SPORTTERY_FONT_DIR')
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path(os.path.dirname(os.path.abspath(__file__))) / 'fonts')
    candidates.append(Path(sys.executable).parent / 'fonts')
    candidates.append(Path(sys.executable).parent.parent / 'fonts')

    names = [
        ('NotoSansSC-Regular.ttf', 'NotoSansSC-Bold.ttf'),
        ('NotoSansCJKsc-Regular.ttf', 'NotoSansCJKsc-Bold.ttf'),
    ]
    for d in candidates:
        for reg, bold in names:
            if (d / reg).exists() and (d / bold).exists():
                pdfmetrics.registerFont(TTFont('CJK', str(d / reg)))
                pdfmetrics.registerFont(TTFont('CJK-Bold', str(d / bold)))
                return 'CJK'

    os_fonts = [
        ('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'),
        ('/usr/share/fonts/truetype/wqy/wqy-microhei.ttc', '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'),
        ('/data/user/work/NotoSansCJKsc-Regular.ttf', '/data/user/work/NotoSansCJKsc-Bold.ttf'),
        ('C:/Windows/Fonts/simhei.ttf', 'C:/Windows/Fonts/simhei.ttf'),
        ('C:/Windows/Fonts/msyh.ttf', 'C:/Windows/Fonts/msyhbd.ttf'),
    ]
    for reg, bold in os_fonts:
        if os.path.exists(reg):
            try:
                pdfmetrics.registerFont(TTFont('CJK', reg, subfontIndex=0))
                pdfmetrics.registerFont(TTFont('CJK-Bold', bold if os.path.exists(bold) else reg, subfontIndex=0))
                return 'CJK'
            except Exception:
                continue
    raise RuntimeError('未找到可用CJK字体, 请设置 SPORTTERY_FONT_DIR 或将字体放入 ./fonts/')

# ============================================================
# 页面常量 (手机优化: 减小边距, 增大内容区)
# ============================================================
PAGE_SIZE = A4
PAGE_W, PAGE_H = PAGE_SIZE
LM = 10 * mm
RM = 10 * mm
TM = 12 * mm
BM = 12 * mm
CW = PAGE_W - LM - RM

# ============================================================
# 明亮配色方案 (与预测报告一致)
# ============================================================
BG_PAGE      = HexColor('#ffffff')
BG_HEADER    = HexColor('#1a56db')   # 明亮蓝标题栏
BG_CARD      = HexColor('#f0f5ff')   # 浅蓝卡片背景
BG_CARD_ALT  = HexColor('#e6effd')   # 浅蓝交替行
BG_SWOT      = HexColor('#fefce8')   # 浅黄
BG_EXTRA     = HexColor('#f8fafc')   # 极浅灰
BG_POOL      = HexColor('#fffbeb')   # 浅橙
BG_REC1      = HexColor('#ecfdf5')   # 浅绿命中
BG_REC2      = HexColor('#eff6ff')   # 浅蓝第二
BG_MSN_HEAD  = HexColor('#065f46')   # 深绿表头
BG_MSN_HI    = HexColor('#d1fae5')   # 浅绿高亮
BG_WARN      = HexColor('#fef3c7')   # 浅黄警告
BG_HIT       = HexColor('#d1fae5')   # 命中背景
BG_MISS      = HexColor('#fee2e2')   # 未中背景

INK_DARK     = HexColor('#0f172a')
INK_BODY     = HexColor('#1e293b')
INK_MUTED    = HexColor('#475569')
INK_WHITE    = colors.white

ACCENT_BLUE  = HexColor('#1d4ed8')
ACCENT_GREEN = HexColor('#059669')
ACCENT_RED   = HexColor('#dc2626')
ACCENT_AMBER = HexColor('#d97706')
ACCENT_TEAL  = HexColor('#0891b2')

BORDER_LIGHT = HexColor('#cbd5e1')
BORDER_BLUE  = HexColor('#3b82f6')
BORDER_GREEN = HexColor('#10b981')

# ============================================================
# 样式 (手机优化: 放大字体 + 宽行距)
# ============================================================
def get_styles(cjk_font='CJK'):
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
        'td_hit': ParagraphStyle('TdHit', fontName=bold_font, fontSize=12, leading=16,
                                  textColor=ACCENT_GREEN, alignment=TA_CENTER, wordWrap='CJK'),
        'td_miss': ParagraphStyle('TdMiss', fontName=bold_font, fontSize=12, leading=16,
                                   textColor=ACCENT_RED, alignment=TA_CENTER, wordWrap='CJK'),
        'td_score': ParagraphStyle('TdScore', fontName=bold_font, fontSize=12, leading=16,
                                    textColor=ACCENT_AMBER, alignment=TA_CENTER, wordWrap='CJK'),
        'stat_value': ParagraphStyle('StatVal', fontName=bold_font, fontSize=24, leading=30,
                                      textColor=ACCENT_BLUE, alignment=TA_CENTER, wordWrap='CJK'),
        'stat_label': ParagraphStyle('StatLabel', fontName=cjk_font, fontSize=11, leading=16,
                                      textColor=INK_MUTED, alignment=TA_CENTER, wordWrap='CJK'),
        'callout': ParagraphStyle('Callout', fontName=cjk_font, fontSize=13, leading=20,
                                   textColor=INK_BODY, wordWrap='CJK'),
    }


def normalize_text(text):
    if not text:
        return ''
    replacements = {'\u2013': '-', '\u2014': '-', '\u2018': "'", '\u2019': "'",
                    '\u201c': '"', '\u201d': '"', '\u2212': '-'}
    for old, new in replacements.items():
        text = text.replace(old, new)
    return str(text).strip()


# ============================================================
# HTML 解析器
# ============================================================
def parse_verify_html(html_path_or_content):
    """解析验证HTML报告，提取结构化数据

    Args:
        html_path_or_content: HTML文件路径 或 HTML内容字符串
    """
    # 判断是文件路径还是HTML内容
    if '<html' in html_path_or_content[:500].lower() or '<h1>' in html_path_or_content[:500]:
        html = html_path_or_content
    elif os.path.exists(html_path_or_content):
        with open(html_path_or_content, 'r', encoding='utf-8') as f:
            html = f.read()
    else:
        # 内容本身就是HTML (没有文件头标志但也不是文件路径)
        html = html_path_or_content

    data = {'stats': {}, 'matches': [], 'calibration': {}, 'confusion': {},
            'lessons': [], 'history': {}, 'advanced': {}}

    # 提取标题信息
    title_m = re.search(r'<h1>(.*?)</h1>', html)
    data['title'] = normalize_text(title_m.group(1)) if title_m else '赛果验证报告'

    subtitle_m = re.search(r'<div class="subtitle">(.*?)</div>', html)
    data['subtitle'] = normalize_text(subtitle_m.group(1)) if subtitle_m else ''

    badge_m = re.search(r'<div class="badge">(.*?)</div>', html)
    data['badge'] = normalize_text(badge_m.group(1)) if badge_m else ''

    # 提取统计卡片
    stat_cards = re.findall(r'<div class="stat-card[^"]*">\s*<div class="value">(.*?)</div>\s*<div class="label">(.*?)</div>\s*</div>', html)
    for val, label in stat_cards:
        data['stats'][normalize_text(label)] = normalize_text(val)

    # 提取汇总表行
    table_rows = re.findall(r'<tr>\s*<td><span class="tag[^"]*">(.*?)</span></td>\s*<td>(.*?)</td>\s*<td class="score">(.*?)</td>\s*(.*?)</tr>', html, re.DOTALL)
    for key, teams, score, rest in table_rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', rest)
        # cells: [实际HAD, 预测HAD, 验证, 实际HHAD, 预测HHAD, 验证, 实际总进球, 预测总进球, 验证, 实际半全场, 预测半全场, 验证, 可预测性, 主推]
        match = {
            'key': normalize_text(key),
            'teams': normalize_text(teams),
            'score': normalize_text(score),
        }
        if len(cells) >= 14:
            match['actual_had'] = normalize_text(cells[0])
            match['pred_had'] = normalize_text(cells[1])
            match['had_verify'] = normalize_text(cells[2])
            match['actual_hhad'] = normalize_text(cells[3])
            match['pred_hhad'] = normalize_text(cells[4])
            match['hhad_verify'] = normalize_text(cells[5])
            match['actual_tg'] = normalize_text(cells[6])
            match['pred_tg'] = normalize_text(cells[7])
            match['tg_verify'] = normalize_text(cells[8])
            match['actual_hf'] = normalize_text(cells[9])
            match['pred_hf'] = normalize_text(cells[10])
            match['hf_verify'] = normalize_text(cells[11])
            match['predictability'] = normalize_text(cells[12])
            match['main_rec'] = normalize_text(cells[13])
        data['matches'].append(match)

    # 提取逐场详细分析
    detail_cards = re.findall(r'<div class="match-card">(.*?)</div>\s*</div>\s*</div>', html, re.DOTALL)
    for card_html in detail_cards:
        header_m = re.search(r'<span class="match-id">(.*?)</span>', card_html)
        league_m = re.search(r'<span class="league">(.*?)</span>', card_html)
        teams_m = re.search(r'<div class="teams">(.*?)</div>', card_html)
        score_m = re.search(r'<div class="score-row">(.*?)</div>', card_html)

        detail = {
            'header': normalize_text(header_m.group(1)) if header_m else '',
            'league': normalize_text(league_m.group(1)) if league_m else '',
            'teams': normalize_text(teams_m.group(1)) if teams_m else '',
            'score': normalize_text(score_m.group(1)) if score_m else '',
        }

        # 提取详细表格行
        detail_rows = re.findall(r'<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>', card_html)
        detail['rows'] = []
        for r in detail_rows:
            detail['rows'].append({
                'item': normalize_text(r[0]),
                'pred': normalize_text(r[1]),
                'prob': normalize_text(r[2]),
                'odds': normalize_text(r[3]),
                'actual': normalize_text(r[4]),
                'result': normalize_text(r[5]),
            })

        # 提取盘口行
        line_m = re.search(r'<td>盘口</td>\s*<td colspan="2">(.*?)</td>\s*<td>总进球: (.*?)</td>\s*<td colspan="2">半场: (.*?)</td>', card_html)
        if line_m:
            detail['goal_line'] = normalize_text(line_m.group(1))
            detail['total_goals'] = normalize_text(line_m.group(2))
            detail['half_score'] = normalize_text(line_m.group(3))

        # 匹配到对应的match
        for m in data['matches']:
            if m['key'] in detail['header']:
                m['detail'] = detail
                break

    # 提取Brier分数
    brier_m = re.search(r'Brier分数: ([\d.]+) \((.*?)\)', html)
    if brier_m:
        data['advanced']['brier'] = brier_m.group(1)
        data['advanced']['brier_eval'] = brier_m.group(2)

    # 提取RPS和Log Loss
    rps_m = re.search(r'RPS分数: ([\d.]+) \| <strong>Log Loss:</strong> ([\d.]+)', html)
    if rps_m:
        data['advanced']['rps'] = rps_m.group(1)
        data['advanced']['log_loss'] = rps_m.group(2)

    # 提取校准分析
    cal_m = re.search(r'校准分析:</strong> 校准(.*?)\(ECE=([\d.]+\))', html)
    if cal_m:
        data['calibration']['eval'] = cal_m.group(1).strip('，, ')
        data['calibration']['ece'] = cal_m.group(2)

    # 提取混淆矩阵
    conf_m = re.search(r'混淆矩阵:</strong> 整体准确率([\d.]+%), 最弱方向\'(.*?)\'\(F1=([\d.]+)\)', html)
    if conf_m:
        data['confusion']['accuracy'] = conf_m.group(1) + '%'
        data['confusion']['weakest'] = conf_m.group(2)
        data['confusion']['f1'] = conf_m.group(3)

    # 提取置信度校准
    conf_cal_m = re.search(r'校准摘要:</strong> (.*?) (\d+)/(\d+)=(.*?)\((.*?)\)', html)
    if conf_cal_m:
        data['calibration']['conf_level'] = conf_cal_m.group(1)
        data['calibration']['conf_hits'] = conf_cal_m.group(2)
        data['calibration']['conf_total'] = conf_cal_m.group(3)
        data['calibration']['conf_rate'] = conf_cal_m.group(4)
        data['calibration']['conf_status'] = conf_cal_m.group(5)

    # 提取教训
    lessons_m = re.search(r'<ol class="lessons">(.*?)</ol>', html, re.DOTALL)
    if lessons_m:
        lesson_items = re.findall(r'<li>(.*?)</li>', lessons_m.group(1), re.DOTALL)
        for item in lesson_items:
            data['lessons'].append(normalize_text(item))

    # 提取历史统计
    hist_m = re.search(r'历史累计统计.*?HAD累计命中率: ([\d/.]+) = ([\d.]+%)', html)
    if hist_m:
        data['history']['had_hist'] = hist_m.group(1) + ' = ' + hist_m.group(2)
    hhad_hist_m = re.search(r'HHAD累计命中率: ([\d/.]+) = ([\d.]+%)', html)
    if hhad_hist_m:
        data['history']['hhad_hist'] = hhad_hist_m.group(1) + ' = ' + hhad_hist_m.group(2)

    # 提取CUSUM
    cusum_m = re.search(r'CUSUM漂移检测:</strong> 模型(.*?)\(CUSUM=([\d.]+/[\d.]+)', html)
    if cusum_m:
        data['advanced']['cusum_status'] = cusum_m.group(1)
        data['advanced']['cusum_value'] = cusum_m.group(2)

    # 提取贝叶斯
    bayes_m = re.search(r'贝叶斯命中率:</strong> 后验命中率([\d.]+%) \(95%CI: ([\d.%-]+)\)', html)
    if bayes_m:
        data['advanced']['bayes_rate'] = bayes_m.group(1)
        data['advanced']['bayes_ci'] = bayes_m.group(2)

    return data


# ============================================================
# 背景绘制
# ============================================================
def draw_bg(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(BG_PAGE)
    canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    canvas.restoreState()


# ============================================================
# 构建统计卡片网格
# ============================================================
def build_stat_grid(stats, styles):
    """构建统计卡片网格 — 明亮配色"""
    items = list(stats.items())
    # 每4个一行
    rows = []
    current_row = []
    for i, (label, value) in enumerate(items):
        # 判断颜色
        val_clean = value.replace('%', '').replace('-', '').replace('/', '')
        try:
            v = float(val_clean)
        except (ValueError, TypeError):
            v = None  # 非数值 (如 N/A) 不触发颜色判断

        bg_color = BG_CARD
        text_color = ACCENT_BLUE
        if '命中' in label and '/' in value:
            parts = value.split('/')
            if len(parts) == 2:
                hit, total = int(parts[0]), int(parts[1])
                if total > 0 and hit / total >= 0.5:
                    bg_color = BG_HIT
                    text_color = ACCENT_GREEN
                else:
                    bg_color = BG_MISS
                    text_color = ACCENT_RED
        elif 'ROI' in label and '-' in value:
            bg_color = BG_MISS
            text_color = ACCENT_RED
        elif 'ROI' in label and ('+' in value or (v is not None and v > 0)):
            bg_color = BG_HIT
            text_color = ACCENT_GREEN

        cell_data = [[
            Paragraph(value, ParagraphStyle('sv', parent=styles['stat_value'], textColor=text_color)),
        ], [
            Paragraph(label, styles['stat_label']),
        ]]
        cell = Table(cell_data, colWidths=[(CW - 12) / 4])
        cell.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), bg_color),
            ('BOX', (0, 0), (-1, -1), 1, BORDER_LIGHT),
            ('TOPPADDING', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 2),
            ('TOPPADDING', (0, 1), (-1, 1), 2),
            ('BOTTOMPADDING', (0, 1), (-1, 1), 10),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ])
        current_row.append(cell)

        if len(current_row) == 4:
            rows.append(current_row)
            current_row = []

    if current_row:
        while len(current_row) < 4:
            current_row.append('')
        rows.append(current_row)

    elements = []
    for row in rows:
        row_data = [row]
        t = Table(row_data, colWidths=[CW / 4] * 4)
        t.setStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 3),
            ('RIGHTPADDING', (0, 0), (-1, -1), 3),
        ])
        elements.append(t)

    return elements


# ============================================================
# 构建汇总表
# ============================================================
def build_summary_table(matches, styles):
    """构建验证汇总表 — 明亮配色"""
    header = [
        Paragraph('场次', styles['th']),
        Paragraph('对阵', styles['th']),
        Paragraph('比分', styles['th']),
        Paragraph('实际HAD', styles['th']),
        Paragraph('预测HAD', styles['th']),
        Paragraph('HAD', styles['th']),
        Paragraph('实际HHAD', styles['th']),
        Paragraph('预测HHAD', styles['th']),
        Paragraph('HHAD', styles['th']),
        Paragraph('主推', styles['th']),
    ]
    data = [header]

    for m in matches:
        had_hit = '命中' in m.get('had_verify', '')
        hhad_hit = '命中' in m.get('hhad_verify', '')

        main_rec_text = m.get('main_rec', '')
        main_hit = '命中' in main_rec_text

        row = [
            Paragraph(m['key'], styles['td']),
            Paragraph(normalize_text(m['teams']), styles['td']),
            Paragraph(m['score'], ParagraphStyle('sc', parent=styles['td'], textColor=ACCENT_AMBER, fontName='CJK-Bold')),
            Paragraph(m.get('actual_had', ''), styles['td']),
            Paragraph(m.get('pred_had', ''), styles['td']),
            Paragraph(m.get('had_verify', ''), styles['td_hit'] if had_hit else styles['td_miss']),
            Paragraph(m.get('actual_hhad', ''), styles['td']),
            Paragraph(m.get('pred_hhad', ''), styles['td']),
            Paragraph(m.get('hhad_verify', ''), styles['td_hit'] if hhad_hit else styles['td_miss']),
            Paragraph(normalize_text(main_rec_text), styles['td_hit'] if main_hit else styles['td_miss']),
        ]
        data.append(row)

    col_widths = [22*mm, 30*mm, 14*mm, 16*mm, 16*mm, 14*mm, 16*mm, 16*mm, 14*mm, 24*mm]
    table = LongTable(data, colWidths=col_widths, repeatRows=1)

    # 构建行背景色列表
    row_bg = [BG_HEADER]  # 表头
    for m in matches:
        had_hit = '命中' in m.get('had_verify', '')
        hhad_hit = '命中' in m.get('hhad_verify', '')
        if had_hit and hhad_hit:
            row_bg.append(BG_HIT)
        elif not had_hit and not hhad_hit:
            row_bg.append(BG_MISS)
        else:
            row_bg.append(HexColor('#ffffff'))

    style_cmds = [
        ('BACKGROUND', (0, 0), (-1, 0), BG_HEADER),
        ('GRID', (0, 0), (-1, -1), 0.5, BORDER_LIGHT),
        ('LINEBELOW', (0, 0), (-1, 0), 1.5, ACCENT_BLUE),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, 0), 8),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('TOPPADDING', (0, 1), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 7),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]
    # 交替行背景
    for i in range(1, len(data)):
        if row_bg[i] == HexColor('#ffffff') and i % 2 == 0:
            style_cmds.append(('BACKGROUND', (0, i), (-1, i), BG_CARD_ALT))

    table.setStyle(style_cmds)
    return table


# ============================================================
# 构建逐场验证卡片
# ============================================================
def build_match_card(m, styles):
    """构建单场比赛的验证卡片 — 明亮配色"""
    elements = []

    detail = m.get('detail', {})

    # 比赛标题行
    header_data = [[
        Paragraph(normalize_text(detail.get('header', f'{m["key"]} {m["teams"]}')), styles['match_title']),
        Paragraph(normalize_text(detail.get('league', '')), styles['match_info']),
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

    # 比分显示行
    score = m.get('score', 'N/A')
    score_data = [[
        Paragraph(f'<font size="20"><b>{score}</b></font>',
                  ParagraphStyle('sc', fontName='CJK-Bold', fontSize=20, leading=28,
                                 textColor=ACCENT_AMBER, alignment=TA_CENTER, wordWrap='CJK')),
    ]]
    score_table = Table(score_data, colWidths=[CW])
    score_table.setStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BG_POOL),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
    ])
    elements.append(score_table)

    # 详细验证表
    rows_data = [[
        Paragraph('预测项', styles['th']),
        Paragraph('预测', styles['th']),
        Paragraph('概率', styles['th']),
        Paragraph('赔率', styles['th']),
        Paragraph('实际', styles['th']),
        Paragraph('结果', styles['th']),
    ]]

    for r in detail.get('rows', []):
        is_hit = '命中' in r.get('result', '')
        result_style = styles['td_hit'] if is_hit else styles['td_miss']
        rows_data.append([
            Paragraph(r['item'], styles['td']),
            Paragraph(r['pred'], styles['td']),
            Paragraph(r['prob'], styles['td']),
            Paragraph(r['odds'], styles['td']),
            Paragraph(r['actual'], styles['td']),
            Paragraph(r['result'], result_style),
        ])

    # 盘口行
    if detail.get('goal_line'):
        rows_data.append([
            Paragraph('盘口', styles['td']),
            Paragraph(detail['goal_line'], styles['td']),
            Paragraph('', styles['td']),
            Paragraph(f'总进球: {detail.get("total_goals", "")}', styles['td']),
            Paragraph(f'半场: {detail.get("half_score", "")}', styles['td']),
            Paragraph('', styles['td']),
        ])

    detail_table = Table(rows_data, colWidths=[CW*0.15, CW*0.12, CW*0.20, CW*0.13, CW*0.20, CW*0.20])
    detail_table.setStyle([
        ('BACKGROUND', (0, 0), (-1, 0), BG_HEADER),
        ('TEXTCOLOR', (0, 0), (-1, 0), INK_WHITE),
        ('GRID', (0, 0), (-1, -1), 0.5, BORDER_LIGHT),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        # 交替行
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [HexColor('#ffffff'), BG_CARD_ALT]),
    ])
    elements.append(detail_table)

    elements.append(Spacer(1, 12))
    return elements


# ============================================================
# 构建校准分析区
# ============================================================
def build_calibration_section(data, styles):
    """构建置信度校准和Brier分数区"""
    elements = []

    adv = data.get('advanced', {})
    cal = data.get('calibration', {})
    conf = data.get('confusion', {})

    # Brier分数 callout
    if adv.get('brier'):
        brier_bg = BG_HIT if '优秀' in adv.get('brier_eval', '') else (BG_WARN if '一般' in adv.get('brier_eval', '') else BG_MISS)
        brier_data = [[Paragraph(f'<b>Brier分数: {adv["brier"]} ({adv.get("brier_eval", "")})</b> — 衡量概率预测准确性, 越低越好(0=完美, 0.33≈随机基准)。', styles['callout'])]]
        brier_table = Table(brier_data, colWidths=[CW])
        brier_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), brier_bg),
            ('BOX', (0, 0), (-1, -1), 1, BORDER_LIGHT),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 14),
        ])
        elements.append(brier_table)
        elements.append(Spacer(1, 8))

    # 校准摘要 callout
    if cal.get('conf_status'):
        cal_bg = BG_HIT if '达标' in cal['conf_status'] else BG_WARN
        cal_data = [[Paragraph(f'<b>校准摘要:</b> {cal.get("conf_level", "")} {cal.get("conf_hits", "")}/{cal.get("conf_total", "")}={cal.get("conf_rate", "")} ({cal["conf_status"]})', styles['callout'])]]
        cal_table = Table(cal_data, colWidths=[CW])
        cal_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), cal_bg),
            ('BOX', (0, 0), (-1, -1), 1, BORDER_LIGHT),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 14),
        ])
        elements.append(cal_table)
        elements.append(Spacer(1, 8))

    # RPS & Log Loss
    if adv.get('rps'):
        rps_data = [[Paragraph(f'<b>RPS分数:</b> {adv["rps"]} | <b>Log Loss:</b> {adv.get("log_loss", "")} | {adv.get("brier_eval", "")}', styles['callout'])]]
        rps_table = Table(rps_data, colWidths=[CW])
        rps_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), BG_EXTRA),
            ('BOX', (0, 0), (-1, -1), 1, BORDER_LIGHT),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 14),
        ])
        elements.append(rps_table)
        elements.append(Spacer(1, 8))

    # 校准分析
    if cal.get('ece'):
        ece_bg = BG_HIT if '优秀' in cal.get('eval', '') else BG_WARN
        ece_data = [[Paragraph(f'<b>校准分析:</b> 校准{cal.get("eval", "")} (ECE={cal["ece"]})', styles['callout'])]]
        ece_table = Table(ece_data, colWidths=[CW])
        ece_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), ece_bg),
            ('BOX', (0, 0), (-1, -1), 1, BORDER_LIGHT),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 14),
        ])
        elements.append(ece_table)
        elements.append(Spacer(1, 8))

    # 贝叶斯
    if adv.get('bayes_rate'):
        bayes_data = [[Paragraph(f'<b>贝叶斯命中率:</b> 后验命中率{adv["bayes_rate"]} (95%CI: {adv["bayes_ci"]})', styles['callout'])]]
        bayes_table = Table(bayes_data, colWidths=[CW])
        bayes_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), BG_CARD),
            ('BOX', (0, 0), (-1, -1), 1, BORDER_LIGHT),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 14),
        ])
        elements.append(bayes_table)
        elements.append(Spacer(1, 8))

    # 混淆矩阵
    if conf.get('accuracy'):
        conf_data = [[Paragraph(f'<b>混淆矩阵:</b> 整体准确率{conf["accuracy"]}, 最弱方向\'{conf["weakest"]}\'(F1={conf["f1"]})', styles['callout'])]]
        conf_table = Table(conf_data, colWidths=[CW])
        conf_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), BG_WARN),
            ('BOX', (0, 0), (-1, -1), 1, BORDER_LIGHT),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 14),
        ])
        elements.append(conf_table)
        elements.append(Spacer(1, 8))

    # CUSUM
    if adv.get('cusum_status'):
        cusum_bg = BG_HIT if '稳定' in adv['cusum_status'] else BG_WARN
        cusum_data = [[Paragraph(f'<b>CUSUM漂移检测:</b> 模型{adv["cusum_status"]} (CUSUM={adv["cusum_value"]})', styles['callout'])]]
        cusum_table = Table(cusum_data, colWidths=[CW])
        cusum_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), cusum_bg),
            ('BOX', (0, 0), (-1, -1), 1, BORDER_LIGHT),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 14),
        ])
        elements.append(cusum_table)

    return elements


# ============================================================
# 构建教训区
# ============================================================
def build_lessons_section(data, styles):
    """构建回归分析与教训区"""
    elements = []

    lessons = data.get('lessons', [])
    if not lessons:
        return elements

    for i, lesson in enumerate(lessons, 1):
        # 解析 <strong> 标签
        lesson_clean = lesson.replace('<strong>', '<b>').replace('</strong>', '</b>')
        lesson_data = [[
            Paragraph(f'{i}. {lesson_clean}', styles['body']),
        ]]
        lesson_table = Table(lesson_data, colWidths=[CW])
        lesson_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), BG_EXTRA if i % 2 == 0 else HexColor('#ffffff')),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('LEFTPADDING', (0, 0), (-1, -1), 14),
            ('RIGHTPADDING', (0, 0), (-1, -1), 14),
        ])
        elements.append(lesson_table)

    return elements


# ============================================================
# 主函数
# ============================================================
def main():
    cjk = register_cjk_font()
    styles = get_styles(cjk)

    print("=" * 60)
    print("验证报告 PDF (手机阅读优化版)")
    print("=" * 60)

    # 解析验证HTML
    data = parse_verify_html(VERIFY_HTML)
    print(f"标题: {data['title']}")
    print(f"统计项: {len(data['stats'])} 个")
    print(f"比赛: {len(data['matches'])} 场")
    print(f"教训: {len(data['lessons'])} 条")

    # 生成PDF
    doc = SimpleDocTemplate(
        OUTPUT_PDF, pagesize=PAGE_SIZE,
        leftMargin=LM, rightMargin=RM, topMargin=TM, bottomMargin=BM,
        title=f'验证报告 {data["title"]}',
    )

    story = []

    # 标题 (明亮蓝底白字)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    title_data = [[Paragraph(data['title'], styles['title'])]]
    title_table = Table(title_data, colWidths=[CW])
    title_table.setStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BG_HEADER),
        ('TOPPADDING', (0, 0), (-1, -1), 16),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 16),
    ])
    story.append(title_table)

    meta_data = [[Paragraph(f'{data["subtitle"]} | 生成时间 {now_str}', styles['subtitle'])]]
    meta_table = Table(meta_data, colWidths=[CW])
    meta_table.setStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BG_HEADER),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ])
    story.append(meta_table)
    story.append(Spacer(1, 14))

    # 命中率总览
    story.append(Paragraph('命中率总览', styles['section']))
    if data['badge']:
        badge_data = [[Paragraph(f'<b>{data["badge"]}</b>',
                                  ParagraphStyle('badge', fontName='CJK-Bold', fontSize=16, leading=22,
                                                 textColor=ACCENT_GREEN, alignment=TA_CENTER, wordWrap='CJK'))]]
        badge_table = Table(badge_data, colWidths=[CW])
        badge_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), BG_HIT),
            ('BOX', (0, 0), (-1, -1), 2, BORDER_GREEN),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ])
        story.append(badge_table)
        story.append(Spacer(1, 10))

    story.extend(build_stat_grid(data['stats'], styles))

    # 汇总表
    story.append(Spacer(1, 14))
    story.append(Paragraph('验证汇总表', styles['section']))
    story.append(build_summary_table(data['matches'], styles))

    # 逐场详细分析
    story.append(Spacer(1, 18))
    story.append(Paragraph('逐场验证详情', styles['section']))
    for m in data['matches']:
        card = build_match_card(m, styles)
        story.extend(card)

    # 校准分析
    story.append(Spacer(1, 10))
    story.append(Paragraph('校准分析 & 高级验证', styles['section']))
    story.extend(build_calibration_section(data, styles))

    # 回归分析与教训
    if data['lessons']:
        story.append(Spacer(1, 14))
        story.append(Paragraph('回归分析与教训', styles['section']))
        story.extend(build_lessons_section(data, styles))

    # 历史统计
    if data.get('history', {}).get('had_hist'):
        story.append(Spacer(1, 14))
        story.append(Paragraph('历史累计统计', styles['section']))
        hist = data['history']
        hist_text = f'HAD累计: {hist.get("had_hist", "")} | HHAD累计: {hist.get("hhad_hist", "")}'
        hist_data = [[Paragraph(hist_text, styles['callout'])]]
        hist_table = Table(hist_data, colWidths=[CW])
        hist_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), BG_CARD),
            ('BOX', (0, 0), (-1, -1), 1, BORDER_LIGHT),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 14),
        ])
        story.append(hist_table)

    doc.build(story, onFirstPage=draw_bg, onLaterPages=draw_bg)
    print(f'\n验证报告PDF已生成 (手机阅读优化版): {OUTPUT_PDF}')


def generate_verify_pdf(html_content, output_pdf_path):
    """从HTML内容字符串直接生成验证报告PDF (供 v215_verify.py 直接调用)

    Args:
        html_content: HTML报告字符串 (不保存到文件)
        output_pdf_path: 输出PDF文件路径
    """
    global OUTPUT_PDF
    OUTPUT_PDF = output_pdf_path

    cjk = register_cjk_font()
    styles = get_styles(cjk)

    # 解析HTML内容
    data = parse_verify_html(html_content)

    # 生成PDF
    doc = SimpleDocTemplate(
        output_pdf_path, pagesize=PAGE_SIZE,
        leftMargin=LM, rightMargin=RM, topMargin=TM, bottomMargin=BM,
        title=f'验证报告 {data["title"]}',
    )

    story = []

    # 标题
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    title_data = [[Paragraph(data['title'], styles['title'])]]
    title_table = Table(title_data, colWidths=[CW])
    title_table.setStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BG_HEADER),
        ('TOPPADDING', (0, 0), (-1, -1), 16),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 16),
    ])
    story.append(title_table)

    meta_data = [[Paragraph(f'{data["subtitle"]} | 生成时间 {now_str}', styles['subtitle'])]]
    meta_table = Table(meta_data, colWidths=[CW])
    meta_table.setStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BG_HEADER),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ])
    story.append(meta_table)
    story.append(Spacer(1, 14))

    # 命中率总览
    story.append(Paragraph('命中率总览', styles['section']))
    if data['badge']:
        badge_data = [[Paragraph(f'<b>{data["badge"]}</b>',
                                  ParagraphStyle('badge', fontName='CJK-Bold', fontSize=16, leading=22,
                                                 textColor=ACCENT_GREEN, alignment=TA_CENTER, wordWrap='CJK'))]]
        badge_table = Table(badge_data, colWidths=[CW])
        badge_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), BG_HIT),
            ('BOX', (0, 0), (-1, -1), 2, BORDER_GREEN),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ])
        story.append(badge_table)
        story.append(Spacer(1, 10))

    story.extend(build_stat_grid(data['stats'], styles))

    # 汇总表
    story.append(Spacer(1, 14))
    story.append(Paragraph('验证汇总表', styles['section']))
    story.append(build_summary_table(data['matches'], styles))

    # 逐场详细分析
    story.append(Spacer(1, 18))
    story.append(Paragraph('逐场验证详情', styles['section']))
    for m in data['matches']:
        card = build_match_card(m, styles)
        story.extend(card)

    # 校准分析
    story.append(Spacer(1, 10))
    story.append(Paragraph('校准分析 & 高级验证', styles['section']))
    story.extend(build_calibration_section(data, styles))

    # 回归分析与教训
    if data['lessons']:
        story.append(Spacer(1, 14))
        story.append(Paragraph('回归分析与教训', styles['section']))
        story.extend(build_lessons_section(data, styles))

    # 历史统计
    if data.get('history', {}).get('had_hist'):
        story.append(Spacer(1, 14))
        story.append(Paragraph('历史累计统计', styles['section']))
        hist = data['history']
        hist_text = f'HAD累计: {hist.get("had_hist", "")} | HHAD累计: {hist.get("hhad_hist", "")}'
        hist_data = [[Paragraph(hist_text, styles['callout'])]]
        hist_table = Table(hist_data, colWidths=[CW])
        hist_table.setStyle([
            ('BACKGROUND', (0, 0), (-1, -1), BG_CARD),
            ('BOX', (0, 0), (-1, -1), 1, BORDER_LIGHT),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 14),
        ])
        story.append(hist_table)

    doc.build(story, onFirstPage=draw_bg, onLaterPages=draw_bg)


if __name__ == '__main__':
    main()
