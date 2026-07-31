#!/usr/bin/env python3
"""
竞彩足球预测 PDF 生成器
读取 pred_*.json 预测数据，生成手机友好、字体清晰的 PDF。
用法: python3 gen_pred_pdf.py <json_path> [output_path]
"""

import json
import os
import sys
import re

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether
)

# ── 颜色方案 ──
C_PRIMARY   = HexColor('#1a365d')   # 深蓝
C_ACCENT    = HexColor('#2b6cb0')   # 中蓝
C_LIGHT_BG  = HexColor('#f0f4f8')   # 浅灰蓝背景
C_CARD_BG   = HexColor('#ffffff')   # 卡片白
C_BORDER    = HexColor('#e2e8f0')   # 边框灰
C_TEXT      = HexColor('#1a202c')   # 正文黑
C_MUTED     = HexColor('#718096')   # 辅助灰
C_GREEN     = HexColor('#38a169')   # 主推绿
C_ORANGE    = HexColor('#dd6b20')   # 次推橙
C_GOLD      = HexColor('#d69e2e')   # 星级金色
C_RED       = HexColor('#e53e3e')   # 高亮红

# ── 字体注册 ──
# 使用文泉驿微米黑 (wqy-microhei), 清晰现代, 手机友好
# TTC 集合: subfontIndex=0 Regular, subfontIndex=1 Bold
FONT_PATH = '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc'
pdfmetrics.registerFont(TTFont('CJK', FONT_PATH, subfontIndex=0))
pdfmetrics.registerFont(TTFont('CJKBold', FONT_PATH, subfontIndex=1))

# ── 页面设置 ──
PAGE_W, PAGE_H = A4
MARGIN = 14 * mm
CONTENT_W = PAGE_W - 2 * MARGIN

# ── 样式 ──
def make_style(name, font='CJK', size=10, leading=None, color=C_TEXT, align=TA_LEFT, space_before=0, space_after=4, bold=False):
    return ParagraphStyle(
        name, fontName='CJKBold' if bold else font, fontSize=size,
        leading=leading or size * 1.5, textColor=color, alignment=align,
        spaceBefore=space_before, spaceAfter=space_after, wordWrap='CJK',
    )

S = {
    'doc_title': make_style('DocTitle', bold=True, size=22, leading=28, color=C_PRIMARY, align=TA_CENTER, space_after=2),
    'doc_sub': make_style('DocSub', size=11, leading=15, color=C_MUTED, align=TA_CENTER, space_after=6),
    'match_title': make_style('MatchTitle', bold=True, size=14, leading=18, color=C_PRIMARY, space_before=10, space_after=2),
    'match_info': make_style('MatchInfo', size=9, leading=13, color=C_MUTED, space_after=6),
    'table_header': make_style('TableHeader', bold=True, size=9, leading=12, color=white),
    'table_body': make_style('TableBody', size=9, leading=13, color=C_TEXT),
    'table_body_bold': make_style('TableBodyBold', bold=True, size=9, leading=13, color=C_TEXT),
    'section_label': make_style('SectionLabel', bold=True, size=10, leading=14, color=C_ACCENT, space_before=6, space_after=2),
    'rec_main': make_style('RecMain', bold=True, size=11, leading=15, color=C_GREEN, space_before=2, space_after=1),
    'rec_sub': make_style('RecSub', bold=True, size=10, leading=14, color=C_ORANGE, space_before=2, space_after=1),
    'rec_detail': make_style('RecDetail', size=9, leading=13, color=C_MUTED, space_after=2),
    'insight': make_style('Insight', size=8.5, leading=12, color=C_MUTED, space_before=2, space_after=2),
    'footer': make_style('Footer', size=7.5, leading=10, color=C_MUTED, align=TA_CENTER, space_before=8),
}


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

    # 胜平负
    had = res.get('HAD', {})
    had_dir = had.get('dir', '?')
    had_odds = had.get('odds', '?')
    had_conf = had.get('conf', '')
    had_p = had.get('p', '')

    # 让球
    hhad = res.get('HHAD', {})
    hhad_dir = hhad.get('dir', '?')
    hhad_odds = hhad.get('odds', '?')
    hhad_conf = hhad.get('conf', '')
    hhad_h = hhad.get('handicap', '?')

    # 比分
    score = res.get('score', {})
    top3 = score.get('top3', '')
    wdl = score.get('wdl', '')

    # 总进球
    tg = res.get('total_goals', {})
    tg_main = tg.get('main', '')
    tg_top3 = tg.get('top3', '')

    # 半全场
    hf = res.get('half_full', {})
    hf_main = hf.get('main', '')
    hf_top3 = hf.get('top3', '')

    # 预期进球
    goals = res.get('goals', {})
    lam = res.get('lam', '')
    home_xg = goals.get('home_xg', '')
    away_xg = goals.get('away_xg', '')
    total_exp = goals.get('total_expected', '')
    key_insight = goals.get('key_insight', '')

    # 跨市场推荐
    cm = res.get('cross_market', {})
    primary = cm.get('primary_bet', {})
    double_rec = cm.get('double_recommend', {})
    hhad_primary = cm.get('hhad_primary_bet', {})
    insight = cm.get('insight', '')
    margin_dist = cm.get('margin_dist', {})

    # 体彩各玩法
    sp = res.get('sporttery_pools', {})

    # 数据质量
    dq = res.get('data_quality', {})
    quality = dq.get('quality', '?')
    difficulty = res.get('difficulty', '?')

    # EV列表
    ev_list = res.get('ev', [])

    return {
        'id': match_id,
        'home': home, 'away': away,
        'league': league, 'time': match_time,
        'date': match_date, 'weekday': weekday,
        'rank_h': rank_h, 'rank_a': rank_a,
        'had_dir': had_dir, 'had_odds': had_odds,
        'had_conf': had_conf, 'had_p': had_p,
        'hhad_dir': hhad_dir, 'hhad_odds': hhad_odds,
        'hhad_conf': hhad_conf, 'hhad_h': hhad_h,
        'top3_score': top3, 'wdl': wdl,
        'tg_main': tg_main, 'tg_top3': tg_top3,
        'hf_main': hf_main, 'hf_top3': hf_top3,
        'lam': lam, 'home_xg': home_xg, 'away_xg': away_xg,
        'total_exp': total_exp, 'key_insight': key_insight,
        'primary': primary, 'double_rec': double_rec,
        'hhad_primary': hhad_primary,
        'insight': insight, 'margin_dist': margin_dist,
        'sporttery_pools': sp,
        'quality': quality, 'difficulty': difficulty,
        'ev_list': ev_list,
    }


def fmt_odds(v):
    """格式化赔率/概率"""
    if isinstance(v, (int, float)):
        return f"{v:.2f}" if isinstance(v, float) else str(v)
    return str(v)


def build_match_card(m):
    """构建一场比赛的 PDF 内容 (返回 flowable 列表)"""
    story = []

    # ── 比赛标题 ──
    title_text = f"{m['id']} {m['home']} vs {m['away']}"
    story.append(Paragraph(title_text, S['match_title']))
    info_parts = [f"🕐 {m['date']}({m['weekday']}) {m['time']}"]
    if m['league']:
        info_parts.append(f"🏆 {m['league']}")
    if m['rank_h'] or m['rank_a']:
        info_parts.append(f"📊 {m['rank_h']} · {m['rank_a']}")
    story.append(Paragraph(' | '.join(info_parts), S['match_info']))

    # ── 分隔线 ──
    story.append(_divider_line())

    # ── 项目/结果 表格 ──
    table_data = [
        ['项目', '结果'],
        ['胜平负', f"{m['had_dir']}@{m['had_odds']}  {m['had_conf']}  ({m['had_p']})"],
        ['让球胜平负', f"{m['hhad_dir']}@{m['hhad_odds']}  {m['hhad_conf']}  (让{m['hhad_h']}球)  {m['wdl']}"],
        ['比分', m['top3_score']],
        ['总进球', f"{m['tg_top3']}"],
        ['半全场', f"{m['hf_top3']}"],
        ['预期进球', f"主{m['home_xg']} / 客{m['away_xg']}  总{m['total_exp']}球"],
    ]
    t = _make_item_table(table_data)
    story.append(t)

    # ── 主推 ──
    pri = m['primary']
    if pri:
        opt = pri.get('option', '?')
        mk = pri.get('market', '')
        prob = pri.get('prob', 0)
        odds = pri.get('odds', 0)
        ev = pri.get('ev_pct', 0)
        val = '✓' if pri.get('value') else '✗'
        cov = pri.get('coverage', '')
        story.append(Paragraph('● 主推', S['rec_main']))
        detail = f"{mk} {opt}  @{odds}  P={prob:.1f}%  EV={ev:+.1f}%  {val}"
        if cov:
            detail += f"  覆盖:{cov}"
        story.append(Paragraph(detail, S['rec_detail']))

    # ── 次推 ──
    dr = m['double_rec']
    hp = m['hhad_primary']
    sub_items = []
    if dr and dr.get('option'):
        sub_items.append(('双选', dr))
    if hp and hp.get('option') and hp.get('option') != pri.get('option'):
        sub_items.append(('防守', hp))

    if sub_items:
        story.append(Paragraph('● 次推', S['rec_sub']))
        for label, item in sub_items:
            opt = item.get('option', '?')
            mk = item.get('market', '')
            prob = item.get('prob', 0)
            odds = item.get('odds', 0)
            ev = item.get('ev_pct', 0)
            val = '✓' if item.get('value') else '✗'
            dir_ = item.get('direction', '')
            detail = f"{mk} {opt}  @{odds}  P={prob:.1f}%  EV={ev:+.1f}%  {val}"
            if dir_:
                detail += f"  → {dir_}"
            story.append(Paragraph(detail, S['rec_detail']))

    # ── 一句话洞察 ──
    if m['insight']:
        # 截取关键部分
        short = m['insight']
        if len(short) > 120:
            short = short[:120] + '...'
        story.append(Paragraph(short, S['insight']))

    # ── 体彩各玩法最优 ──
    sp = m['sporttery_pools']
    pool_lines = []
    for pool_key, pool_label in [('ttg', '总进球'), ('hafu', '半全场'), ('crs', '比分')]:
        items = sp.get(pool_key, [])
        if items:
            sorted_items = sorted(items, key=lambda x: x.get('ev_pct', -999), reverse=True)[:3]
            # 显示margin信息
            margin_val = sorted_items[0].get('margin', '')
            margin_str = f'(抽水{margin_val:.0f}%)' if margin_val else ''
            parts = []
            for it in sorted_items:
                opt = it.get('option', '?')
                odd = it.get('odds', '?')
                ev2 = it.get('ev_pct', 0)
                parts.append(f"{opt}@{odd}(EV{ev2:+.0f}%)")
            pool_lines.append(f"{pool_label}{margin_str}: {' | '.join(parts)}")
    if pool_lines:
        story.append(Paragraph('🎯 体彩玩法EV优选', S['section_label']))
        for line in pool_lines:
            story.append(Paragraph(line, S['insight']))

    # ── 边际分布 ──
    md = m['margin_dist']
    if md:
        parts = []
        for k, v in [('赢2+球', 'win_2plus'), ('赢1球', 'win_1'), ('平局', 'draw'), ('输球', 'lose')]:
            val = md.get(v, 0)
            if val:
                parts.append(f"{k}={val:.1f}%")
        if parts:
            story.append(Paragraph('  净胜分布: ' + ' | '.join(parts), S['insight']))

    # 底部留白
    story.append(Spacer(1, 4 * mm))
    return story


def _divider_line():
    """浅色分隔线"""
    from reportlab.platypus import Flowable
    class Divider(Flowable):
        def __init__(self, w, h=0.5, color=C_BORDER):
            Flowable.__init__(self)
            self.width = w
            self.height = h
            self.color = color
            self.spaceAfter = 4
            self.spaceBefore = 2
        def draw(self):
            self.canv.setStrokeColor(self.color)
            self.canv.setLineWidth(self.height)
            self.canv.line(0, 0, self.width, 0)
    return Divider(CONTENT_W)


def _make_item_table(data):
    """创建项目/结果的双列表格"""
    header_style = ParagraphStyle('TH', fontName='CJKBold', fontSize=10, leading=14,
                                   textColor=white, alignment=TA_CENTER, wordWrap='CJK')
    body_style_r = ParagraphStyle('TR', fontName='CJK', fontSize=9.5, leading=14,
                                   textColor=C_TEXT, alignment=TA_LEFT, wordWrap='CJK')
    body_style_l = ParagraphStyle('TL', fontName='CJKBold', fontSize=9.5, leading=14,
                                   textColor=C_PRIMARY, alignment=TA_LEFT, wordWrap='CJK')

    wrapped = []
    for i, row in enumerate(data):
        if i == 0:
            wrapped.append([Paragraph(str(c), header_style) for c in row])
        else:
            wrapped.append([
                Paragraph(str(row[0]), body_style_l),
                Paragraph(str(row[1]), body_style_r),
            ])

    col_w = CONTENT_W * 0.22, CONTENT_W * 0.78
    t = Table(wrapped, colWidths=col_w)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), C_ACCENT),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('TOPPADDING', (0, 0), (-1, 0), 6),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 5),
        ('TOPPADDING', (0, 1), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, C_BORDER),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [HexColor('#f7fafc'), white]),
    ]))
    return t


def generate_pdf(data, output_path):
    """生成完整的预测 PDF"""
    weekday = ''
    match_date = ''
    matches = []
    for mid in sorted(data.get('meta', {}).keys()):
        m = extract_match(data, mid)
        if m:
            matches.append(m)
            if not match_date:
                match_date = m['date']
                weekday = m['weekday']

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=16 * mm, bottomMargin=14 * mm,
    )

    story = []

    # ── 封面区域 ──
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph('竞彩足球预测', S['doc_title']))
    date_label = f"{match_date}（{weekday}）" if match_date else ''
    story.append(Paragraph(f"📅 {date_label}  · 共 {len(matches)} 场", S['doc_sub']))
    story.append(_divider_line())
    story.append(Spacer(1, 2 * mm))

    # ── 每场比赛 ──
    for i, m in enumerate(matches):
        card = build_match_card(m)
        story.extend(card)
        if i < len(matches) - 1:
            story.append(Spacer(1, 2 * mm))

    # ── 页脚 ──
    story.append(Spacer(1, 6 * mm))
    story.append(_divider_line())
    import datetime
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    story.append(Paragraph(f"生成时间: {now}  |  数据来源: 500.com / Nowscore  |  仅供参考, 理性购彩", S['footer']))

    doc.build(story)
    return output_path


def main():
    if len(sys.argv) < 2:
        print("用法: python3 gen_pred_pdf.py <json_path> [output_path]")
        sys.exit(1)

    json_path = sys.argv[1]
    if not os.path.exists(json_path):
        print(f"错误: 文件不存在 {json_path}")
        sys.exit(1)

    # 默认输出路径: 同目录下同名 .pdf
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