#!/usr/bin/env python3
"""预测报告 PDF 生成器 — 统一主推卡片版式 (Ultra 15.6, 2026-08-20)

与 pred_report_*.html / bet_guide_*.html 同一口径同一版式:
  · 每场一个主推 = cross_market.primary_bet (命中率优先, 同源概率+陷阱校准)
  · 卡片: 头行 → 主推横幅(醒目) → 陷阱/次选 → 概率条(HAD锚+HHAD同源)
    → 净胜球/穿盘 → 比分/半全场/总进球 → λ/一致性 → SWOT/影子 → 一句话理由

用法: python3 gen_pred_pdf_unified.py [pred_json_path]
输出: pred_report_<base>.pdf (与 HTML 报告同名配对)
"""
import glob
import json
import os
import re
import sys

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, Flowable,
)

from pdf_fonts import register_cjk_font
register_cjk_font(bold_name='CJKBold')

# ── 配色 (与旧 pred PDF 同族: 深蓝+金) ──
C_NAVY = HexColor('#0a1628')
C_NAVY_MID = HexColor('#112240')
C_GOLD = HexColor('#c7922e')
C_GOLD_BG = HexColor('#faf3e0')
C_GOLD_BG2 = HexColor('#f5e7c8')
C_GREEN = HexColor('#15803d')
C_GREEN_BG = HexColor('#dcfce7')
C_GREEN_DK = HexColor('#166534')
C_AMBER = HexColor('#b45309')
C_RED = HexColor('#b91c1c')
C_RED_BG = HexColor('#fef2f2')
C_TEXT = HexColor('#1f2937')
C_MUTED = HexColor('#64748b')
C_LINE = HexColor('#e2e8f0')
C_BAR_BG = HexColor('#e2e8f0')
C_BLUE = HexColor('#2563eb')

PAGE_W, PAGE_H = A4
LM = RM = 12 * mm
CW = PAGE_W - LM - RM

S = {
    'h1': ParagraphStyle('h1', fontName='CJKBold', fontSize=17, leading=22, textColor=white),
    'sub': ParagraphStyle('sub', fontName='CJK', fontSize=9, leading=13, textColor=HexColor('#cbd5e1')),
    'sec': ParagraphStyle('sec', fontName='CJKBold', fontSize=12, leading=16, textColor=C_NAVY,
                          spaceBefore=6, spaceAfter=3),
    'card_hd': ParagraphStyle('card_hd', fontName='CJK', fontSize=9.5, leading=13, textColor=C_TEXT),
    'pb_opt': ParagraphStyle('pb_opt', fontName='CJKBold', fontSize=14, leading=18, textColor=C_NAVY),
    'small': ParagraphStyle('small', fontName='CJK', fontSize=7.8, leading=11.5, textColor=C_MUTED),
    'sub_ln': ParagraphStyle('sub_ln', fontName='CJK', fontSize=8.2, leading=12, textColor=HexColor('#475569')),
    'trap': ParagraphStyle('trap', fontName='CJK', fontSize=8.2, leading=12, textColor=C_RED),
    'insight': ParagraphStyle('insight', fontName='CJK', fontSize=8, leading=11.5, textColor=HexColor('#475569')),
    'foot': ParagraphStyle('foot', fontName='CJK', fontSize=7.5, leading=10, textColor=C_MUTED, alignment=TA_CENTER),
    'stat_n': ParagraphStyle('stat_n', fontName='CJKBold', fontSize=15, leading=18, alignment=TA_CENTER),
    'stat_l': ParagraphStyle('stat_l', fontName='CJK', fontSize=7.5, leading=10, alignment=TA_CENTER, textColor=C_MUTED),
}


class ProbPanel(Flowable):
    """概率条面板: 左HAD锚 / 右HHAD同源 (两组各3行条形图)"""

    def __init__(self, width, left, right):
        """left/right: (title, [(label, pct), ...]) 或 None"""
        super().__init__()
        self.width = width
        self.left = left
        self.right = right
        self.row_h = 5.2 * mm
        self.title_h = 5.5 * mm
        n = max(len((left or (None, []))[1]), len((right or (None, []))[1]))
        self.height = self.title_h + n * self.row_h + 2 * mm

    def _bars(self, c, x, y, w, group):
        title, rows = group
        c.setFont('CJKBold', 7.5)
        c.setFillColor(C_MUTED)
        c.drawString(x, y + self.title_h - 9, title)
        mx = max([r[1] for r in rows]) if rows else 1
        yy = y + 2
        for lab, v in rows:
            c.setFont('CJK', 7.5)
            c.setFillColor(HexColor('#475569'))
            c.drawRightString(x + 13 * mm, yy, lab)
            tx, tw = x + 14 * mm, w - 14 * mm - 9 * mm
            c.setFillColor(C_BAR_BG)
            c.roundRect(tx, yy - 1, tw, 3.4 * mm, 1.7 * mm, stroke=0, fill=1)
            bw = max(tw * v / 100.0, 1.5)
            c.setFillColor(C_BLUE if v == mx else HexColor('#94a3b8'))
            c.roundRect(tx, yy - 1, min(bw, tw), 3.4 * mm, 1.7 * mm, stroke=0, fill=1)
            c.setFont('CJKBold', 7.5)
            c.setFillColor(C_TEXT)
            c.drawString(tx + tw + 1.5 * mm, yy, f'{v:.0f}%')
            yy -= self.row_h

    def draw(self):
        c = self.canv
        c.saveState()
        c.setFillColor(HexColor('#f8fafc'))
        c.setStrokeColor(C_LINE)
        c.roundRect(0, 0, self.width, self.height, 2.5 * mm, stroke=1, fill=1)
        half = (self.width - 12 * mm) / 2.0
        y0 = self.height - self.title_h
        if self.left:
            self._bars(c, 4 * mm, y0 - 3 * mm, half - 4 * mm, self.left)
        if self.right:
            self._bars(c, half + 8 * mm, y0 - 3 * mm, half - 4 * mm, self.right)
        c.restoreState()


def _odds_s(o):
    return f'@{o}' if o else '(以盘口为准)'


def _parse3(s):
    nums = re.findall(r'([\d.]+)\s*%', str(s or ''))
    return [float(x) for x in nums[:3]] if len(nums) >= 3 else None


def _match_sort_key(k):
    m = re.search(r'(\d+)$', k)
    return (re.sub(r'\d+$', '', k), int(m.group(1)) if m else 0)


def _strip_emoji(s):
    """CJK字体无彩色emoji字形, PDF里替换为纯文本标记"""
    return (str(s)
            .replace('🏆', '').replace('⚠️', '[!]').replace('⚠', '[!]')
            .replace('💡', '*').replace('🎯', '').replace('🔭', '')
            .replace('⑵', '2)').replace('⑶', '3)').replace('·', '·'))


def _pb_banner(pb, is_had, ev_uncalibrated=False):
    """主推横幅: Table 徽标+选项+赔率/概率/EV 胶囊"""
    prob = pb.get('prob')
    _ev = pb.get('ev_pct')
    chips = _odds_s(pb.get('odds'))
    if isinstance(prob, (int, float)):
        chips += f'   P={prob:.1f}%'
    if isinstance(_ev, (int, float)):
        # Ultra 15.8-C: 概率校准闭环(n<100)未启动 → EV未校准标注
        chips += f'   EV={_ev:+.1f}%' + ('(未校准)' if ev_uncalibrated else '')
    badge = Table([[Paragraph('主 推', ParagraphStyle(
        'bdg', fontName='CJKBold', fontSize=8.5, leading=11,
        textColor=white, alignment=TA_CENTER))]],
        colWidths=[16 * mm], rowHeights=[6.5 * mm])
    badge.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), C_GREEN if is_had else C_GOLD),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    opt = Paragraph(_strip_emoji(pb.get('option', '?')), S['pb_opt'])
    ch = Paragraph(chips, ParagraphStyle(
        'chips', fontName='CJKBold', fontSize=9.5, leading=13,
        textColor=C_AMBER if isinstance(_ev, (int, float)) and _ev > 0 else C_TEXT))
    t = Table([[badge, opt, ch]], colWidths=[17 * mm, None, None])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), C_GREEN_BG if is_had else C_GOLD_BG2),
        ('BOX', (0, 0), (-1, -1), 1.1, C_GREEN if is_had else C_GOLD),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    return t


def _card(key, m, meta, had, hh, cm, goals, sc, hf, tg, dq, pb, is_had):
    conf = hh.get('conf', '') if not had.get('had_open', True) else had.get('conf', '')
    hcap = hh.get('handicap')
    hd_txt = (f'<b>{key}</b>  {meta.get("match_time","")}  {meta.get("league","")}  '
              f'<b>{meta.get("home","?")}</b> vs <b>{meta.get("away","?")}</b>  {conf}  '
              f'<font color="#{"15803d" if is_had else "b45309"}">[主推·{"胜平负" if is_had else "让球"}]</font>')
    flows = [Paragraph(hd_txt, S['card_hd']), Spacer(1, 2.2 * mm),
             _pb_banner(pb, is_had, ev_uncalibrated=bool(cm.get('ev_uncalibrated')))]

    # 覆盖说明 + 陷阱
    cov_bits = []
    if pb.get('selection_type'):
        cov_bits.append('伪单选' if pb['selection_type'] == '伪单选' else '单选')
    if pb.get('coverage'):
        cov_bits.append(f'覆盖 {pb["coverage"]}')
    if pb.get('cost_advantage'):
        cov_bits.append(pb['cost_advantage'])
    if cov_bits:
        flows.append(Spacer(1, 1.2 * mm))
        flows.append(Paragraph('  '.join(cov_bits), S['small']))
    trap_bits = []
    for tnote in (pb.get('trap_note'), (pb.get('trap_risk') and '让球侧陷阱风险高') or None,
                  hh.get('trap_cal_note')):
        if tnote and tnote not in trap_bits:  # 三来源去重 (同HTML口径)
            trap_bits.append(_strip_emoji(tnote))
    for ln in trap_bits:
        flows.append(Spacer(1, 1.2 * mm))
        flows.append(Paragraph('[!] ' + ln, S['trap']))

    # 次选
    dr_ = cm.get('double_recommend')
    if dr_ and dr_.get('option'):
        _e = f"  EV={dr_['ev_pct']:+.1f}%" if isinstance(dr_.get('ev_pct'), (int, float)) else ''
        flows.append(Spacer(1, 1.2 * mm))
        flows.append(Paragraph(f"2) 双选: <b>{_strip_emoji(dr_['option'])} {_odds_s(dr_.get('odds'))}</b>"
                               f"  P={dr_.get('prob','?')}%{_e}", S['sub_ln']))
    pd_ = cm.get('pure_direction_bet')
    if pd_ and pd_.get('option') and pd_.get('option') != pb.get('option') \
            and isinstance(pd_.get('prob'), (int, float)) and pd_.get('prob', 0) >= 35:
        _e = f"  EV={pd_['ev_pct']:+.1f}%" if isinstance(pd_.get('ev_pct'), (int, float)) else ''
        flows.append(Paragraph(f"3) 纯方向: <b>{_strip_emoji(pd_['option'])} {_odds_s(pd_.get('odds'))}</b>"
                               f"  P={pd_.get('prob','?')}%{_e}", S['sub_ln']))
    ldr = cm.get('let_draw_rec')
    if ldr and ldr.get('option'):
        flows.append(Paragraph(f"* 让平窗口: <b>{_strip_emoji(ldr['option'])}</b> {ldr.get('reason','')[:52]}",
                               S['sub_ln']))

    # 概率面板
    had_open = had.get('had_open', True)
    left = right = None
    had_p = _parse3(had.get('p')) if had_open else None
    if had_p:
        left = (f'胜平负 (锚)  {had_p[0]:.0f}/{had_p[1]:.0f}/{had_p[2]:.0f}',
                [('胜', had_p[0]), ('平', had_p[1]), ('负', had_p[2])])
    elif not had_open:
        left = ('胜平负  未开盘', [])
    hhad_p = _parse3(hh.get('p'))
    if hhad_p:
        gl = f'{float(hcap):+.1f}' if hcap is not None else ''
        ss = '同源' if hh.get('same_source') else '独立'
        lab0 = '受让胜' if (hcap or 0) > 0 else '让胜'
        lab2 = '受让负' if (hcap or 0) > 0 else '让负'
        right = (f'让球盘 {gl} ({ss})',
                 [(lab0, hhad_p[0]), ('让平', hhad_p[1]), (lab2, hhad_p[2])])
    if left or right:
        flows.append(Spacer(1, 1.6 * mm))
        flows.append(ProbPanel(CW - 12 * mm, left, right))

    # 净胜球/穿盘 + 玩法速览 + 元信息
    md = cm.get('margin_dist') or {}
    pr = cm.get('pass_risk') or {}
    line1 = (f"净胜球: 赢2+ {md.get('win_2plus',0):.0f}% · 赢1 {md.get('win_1',0):.0f}% · "
             f"平 {md.get('draw',0):.0f}% · 负 {md.get('lose',0):.0f}%"
             + (f"  |  穿盘风险[{pr.get('level','')}]: {pr.get('desc','')}" if pr else ''))
    misc = []
    if sc.get('top3'):
        misc.append(f"比分 {sc['top3'].replace(' ', '·')}")
    if hf.get('main'):
        misc.append(f"半全场 {hf['main']}")
    if tg.get('main'):
        misc.append(f"总进球 {tg['main']}")
    if sc.get('over_main') is not None:
        misc.append(f"大小 {sc.get('market_gl_str','')}盘 大{sc['over_main']:.0f}%")
    line2 = '  |  '.join(misc)
    xg = f"  · xG {goals['home_xg']}/{goals['away_xg']}" if goals.get('using_xg') and goals.get('home_xg') else ''
    line3 = (f"λ {m.get('lam','')}  · 可预测性 {m.get('difficulty','?')} · "
             f"一致性 {m.get('model_agreement',0):.0%} · 数据{dq.get('score','?')}({dq.get('quality','')}){xg}")
    flows.append(Spacer(1, 1.6 * mm))
    flows.append(Paragraph(line1, S['small']))
    if line2:
        flows.append(Paragraph(line2, S['small']))
    flows.append(Paragraph(line3, S['small']))

    # SWOT / 影子对照 / 理由
    sw = m.get('swot') or {}
    if sw.get('swot_lean') and sw.get('swot_lean') != '无SWOT数据':
        # Ultra 15.7: 带主推重选/概率同步note时放宽截断, 保证修复痕迹完整可见
        _fa = sw.get('fusion_advice') or ''
        _cap = 95 if '[主推' in _fa else 56
        adv = f" — {_fa[:_cap]}{'…' if len(_fa) > _cap else ''}" if _fa else ''
        flows.append(Paragraph(f"SWOT {sw.get('swot_lean','')} (评分 {sw.get('swot_score','')}){adv}",
                               S['sub_ln']))
    _md = m.get('market_divergence')
    if _md and _md.get('flagged'):
        _arrow = '方向相反' if _md.get('dir_conflict') else '幅度偏离'
        flows.append(Paragraph(
            f"影子对照({_arrow}): 独立意见{_md.get('model_dir','?')}{_md.get('model_prob',0):.0f}%"
            f" vs 市场热门{_md.get('market_dir','?')}{_md.get('market_prob',0):.0f}%,"
            f" 差{_md.get('max_diff_pp',0):.0f}pp", S['trap']))
    ins = cm.get('insight') or ''
    if ins:
        flows.append(Paragraph(_strip_emoji(ins)[:170] + ('…' if len(ins) > 170 else ''), S['insight']))

    # 外壳: 左侧色条 + 内容
    body = Table([[flows]], colWidths=[CW - 3.5 * mm])
    body.setStyle(TableStyle([
        ('LEFTPADDING', (0, 0), (-1, -1), 6), ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('BOX', (0, 0), (-1, -1), 0.7, C_LINE),
        ('BACKGROUND', (0, 0), (-1, -1), white),
    ]))
    shell = Table([['', body]], colWidths=[3.5 * mm, CW - 3.5 * mm])
    shell.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), C_GREEN if is_had else C_GOLD),
        ('LEFTPADDING', (0, 0), (0, -1), 0), ('RIGHTPADDING', (0, 0), (0, -1), 0),
        ('LEFTPADDING', (1, 0), (1, -1), 0), ('RIGHTPADDING', (1, 0), (1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    return KeepTogether([shell, Spacer(1, 3 * mm)])


def _header_block(base, n, n_had, n_hhad, parlay):
    hd = Table([[
        Paragraph('预测报告 · 统一主推', S['h1']),
        Paragraph(f'{base} · {n} 场 · 主推 ✅胜平负 {n_had} / 🎯让球 {n_hhad}'.replace('✅', '').replace('🎯', ''),
                  S['sub']),
    ]], colWidths=[None, None])
    hd.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), C_NAVY),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 10), ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 8), ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LINEBELOW', (0, 0), (-1, -1), 2, C_GOLD),
    ]))
    # 统计 + 串关
    stat = Table([[Paragraph(str(n), S['stat_n']), Paragraph(str(n_had), ParagraphStyle('a', parent=S['stat_n'], textColor=C_GREEN)),
                   Paragraph(str(n_hhad), ParagraphStyle('b', parent=S['stat_n'], textColor=C_AMBER))],
                  [Paragraph('场次', S['stat_l']), Paragraph('主推·胜平负', S['stat_l']),
                   Paragraph('主推·让球', S['stat_l'])]],
                 colWidths=[CW / 3.0] * 3)
    stat.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.7, C_LINE),
        ('INNERGRID', (0, 0), (-1, -1), 0.7, C_LINE),
        ('TOPPADDING', (0, 0), (-1, 0), 6), ('BOTTOMPADDING', (0, 1), (-1, 1), 6),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    info = Table([[Paragraph(f'<b>串关参考 (Top3 概率)</b>: {_strip_emoji(parlay) or "—"}  ·  2-3 关为宜, 宁缺毋滥', S['sub_ln']),
                   ]], colWidths=[CW])
    info.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), HexColor('#f8fafc')),
        ('BOX', (0, 0), (-1, -1), 0.7, C_LINE),
        ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    return [hd, Spacer(1, 4 * mm), stat, Spacer(1, 3 * mm), info,
            Spacer(1, 2 * mm),
            Paragraph('口径: 每场唯一主推=概率最高单选 (HAD锚×净胜球形状同源导出, 深盘/需净2+球侧已按回测基准收缩); '
                      '双选/纯方向/让平窗口为次选参考, 与主推同一体系。', S['small']),
            Spacer(1, 4 * mm)]


def generate(pred_json=None):
    if not pred_json:
        cands = sorted(glob.glob(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'predictions', 'pred_*_indep.json')))
        if not cands:
            print('[错误] 未找到预测文件')
            return None
        pred_json = cands[-1]
    if not os.path.exists(pred_json):
        print(f'[错误] 预测文件不存在: {pred_json}')
        return None

    with open(pred_json, encoding='utf-8') as f:
        d = json.load(f)
    res, meta_all = d.get('results', {}), d.get('meta', {})
    base = os.path.basename(pred_json).replace('pred_', '').replace('.json', '')

    cards, n_had, n_hhad = [], 0, 0
    tops = []
    for key in sorted(res.keys(), key=_match_sort_key):
        m = res[key]
        meta = meta_all.get(key, {})
        cm = m.get('cross_market') or {}
        pb = cm.get('primary_bet') or {}
        is_had = pb.get('market') == 'HAD'
        if is_had:
            n_had += 1
        elif pb.get('market') == 'HHAD':
            n_hhad += 1
        cards.append(_card(key, m, meta, m.get('HAD', {}), m.get('HHAD', {}), cm,
                           m.get('goals') or {}, m.get('score') or {},
                           m.get('half_full') or {}, m.get('total_goals') or {},
                           m.get('data_quality') or {}, pb, is_had))
        if isinstance(pb.get('prob'), (int, float)):
            tops.append((pb['prob'], key, pb.get('option', '')))

    tops.sort(reverse=True)
    # 前缀剥离: 先HHAD后HAD (顺序反了会把HHAD吃成"H")
    parlay = ' + '.join(f"{k} {re.sub(r'^(HHAD|HAD)', '', o)}" for _, k, o in tops[:3])

    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, f'pred_report_{base}.pdf')
    doc = SimpleDocTemplate(out_path, pagesize=A4,
                            leftMargin=LM, rightMargin=RM, topMargin=10 * mm, bottomMargin=10 * mm,
                            title=f'预测报告 {base}')
    story = _header_block(base, len(res), n_had, n_hhad, parlay) + cards
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(f'基于 {base} {len(res)} 场预测 · 统一主推口径 (Ultra 15.6) · 仅供研究学习, 不构成投注建议',
                           S['foot']))
    doc.build(story)
    print(f'[预测PDF] 已生成: {out_path} (主推: HAD {n_had} · HHAD {n_hhad})')
    return out_path


if __name__ == '__main__':
    generate(sys.argv[1] if len(sys.argv) > 1 else None)
