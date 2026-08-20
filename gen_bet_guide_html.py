#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_bet_guide_html.py — 投注选择显性化指南 (HTML, 数据驱动)
=============================================================
从预测 JSON 读取每场数据, 按「四档显性化」规则给出每场该怎么买:
  ✅ 单选    方向明确(argmax≥50 且非误判高发) — 照主推买
  ⚠️ 双选兜底 平局窗口(平P≥28联赛/≥30杯赛 且方向模糊<50) — 改买 HHAD 覆盖项(含平局)
  🚫 避开    方向性误判高发(胜P≥60 且 平P≥25) — 不买

规则源自 260811 周二 9 场实测复盘 (用户彩票6中2的根因分析):
  - 005/010 平P32/33%+方向模糊 → HHAD覆盖项(受让胜/让负)命中, HAD单选全错
  - 003 胜P62%但平P25% → 方向性误判黑天鹅, HAD/HHAD全错
  - 004 胜P50%方向明确 → HAD单选命中 (平P28但方向不模糊, 不走覆盖)

平局阈值统一 (回测定参 4449场 2026-08, 真实模型融合概率, 与 v215_e2e.py 同口径):
  - 平局直击: 联赛 P平≥33% / 杯赛 P平≥35% 且距argmax≤10pp (v13.9收紧: 33-36%实测平局率39%, 30-32%档31%无优势)
  - 平局价值: 联赛 P平≥28% / 杯赛 P平≥30% 且距argmax≤10pp (1/3本金小注)
  - 双选兜底: 联赛 P平≥28% / 杯赛 P平≥30% 且方向模糊(<50)
  - 杯赛实际平局率18.7% < 联赛25.8%, 故杯赛阈值高于联赛
    (原"杯赛平局率远超联赛"结论基于260814小样本9场过拟合, 已按674场回测修正)

用法:
  python3 gen_bet_guide_html.py <pred_json路径>        # 指定预测文件
  python3 gen_bet_guide_html.py                        # 最新预测文件
输出: {SPORTTERY_WORKSPACE|脚本目录}/bet_guide_YYYYMMDD_周X.html
"""

import os
import sys
import json
import glob
import re

# Windows 控制台 GBK 编码兼容: 输出含 emoji 与中文, 强制 UTF-8 (失败则忽略)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')
    except Exception:
        pass

_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))

# Ultra 12.0: 杯赛识别关键词
_CUP_KEYWORDS = ['欧冠', '欧罗巴', '欧联', '欧协联', '亚冠', '解放者杯',
                 '南美杯', '中北美冠', '非洲冠', '资格赛', '附', '淘汰赛',
                 '决赛', '冠军联赛', '欧洲联赛', '杯赛']

# 周几排序 (修复: 原按中文字符 Unicode 排序会得到 一/三/二/五/六/四/日 的错误顺序)
_WEEKDAY_ORDER = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6}


def _match_sort_key(x):
    """match_key 形如 '周四001' → 按 (周几, 编号) 排序"""
    wd = x[1] if len(x) >= 2 and x[0] == '周' else ''
    num = re.sub(r'\D', '', x)
    return (_WEEKDAY_ORDER.get(wd, 99), int(num) if num else 0)


def _is_cup_league(league):
    """判断是否为杯赛/淘汰赛 (含资格赛、解放者杯等)"""
    if not league:
        return False
    for kw in _CUP_KEYWORDS:
        if kw in str(league):
            return True
    return False


def _parse_probs(p_str):
    try:
        vals = [float(x.replace('%', '')) for x in str(p_str).split('/')]
    except Exception:
        vals = []
    # 修复: 补齐/截断到 3 项, 避免调用方 w,dr,l 解包 2 元串时 ValueError
    return (vals + [0.0, 0.0, 0.0])[:3]


def classify(draw_p, argmax_p, win_p, loss_p, league='', bk_intent=None):
    """四档判定。返回 (level, reason, draw_strike, draw_strike_reason, draw_value, draw_value_reason)。
    level: 'draw'|'single'|'cover'|'avoid'
    draw_strike: bool — 平局直击 (P平≥30%联赛/32%杯赛 且距argmax≤10pp), 可博高赔平局
    draw_value: bool — 平局价值 (26-30%联赛/28-32%杯赛 且距argmax≤10pp), 平局小注
    阈值与 v215_e2e.py 平局覆盖规则统一 (回测定参 4449场 2026-08, 真实模型融合概率):
      联赛: 覆盖 P平≥30% 差≤10pp (35场平局率40%净增益+17.1%); 价值单 P平≥26% 差≤10pp
      杯赛: 覆盖 P平≥32% 差≤10pp; 价值单 P平≥28% 差≤10pp
      (杯赛实际平局率18.7%<联赛25.8%, 原杯赛阈值下调22/20%基于260814小样本过拟合, 方向反了)
    Ultra 12.2: 平局为argmax → 直接出平推荐 ('draw' 档), 不再退化为双选兜底
    """
    is_cup = _is_cup_league(league)
    _cover_threshold = 30 if is_cup else 28
    _avoid_threshold = 25  # 联赛/杯赛统一 (杯赛平局率低, 不单独下调)
    _draw_gap_threshold = 10  # 投注口径比引擎覆盖(7pp)宽: 指南是并行价值注, 引擎决定主方向
    _draw_min = 35 if is_cup else 33  # 平局直击最低概率 (与引擎v13.9覆盖阈值一致, 33-36%实测平局率39%)
    _draw_value_min = 30 if is_cup else 28  # 平局价值最低概率 (28-33%, 实测30-32%档中性偏价值)
    _cup_tag = ' [杯赛]' if is_cup else ''

    # Ultra 13.14 (2026-08-16): 撤销"平赔遭压→阈值降2pp" (13.12引入)
    # 原依据3233场"平赔压缩→平局率35.8%">基准32.5% — 该数据集已证实被污染
    # (bulk导入源平局率33.1%, 英冠48%/西甲41.7%, 物理不可能)。
    # 干净子集(n=560)实测: 平赔压缩→买平EV -25.0%(-2.5σ), 方向完全反转, 故整段撤销。
    _draw_bonus_tag = ''

    # 🎯 Ultra 12.2: 平局为argmax → 直接推荐平局 (不再退化为双选兜底)
    # 理由: 模型最看好的结果就是平局, 没理由躲到HHAD后面
    # 赔率差: 平局~3.0 vs HHAD覆盖项~1.4, 直接买平局收益高2倍+
    if draw_p == argmax_p and draw_p >= 25:
        _others = [p for p in [win_p, loss_p] if p != draw_p]
        _next = max(_others) if _others else 0
        return ('draw',
                f'平P{draw_p:.0f}%为最高概率, 领先次选P{_next:.0f}% {draw_p-_next:.0f}pp — '
                f'模型看好平局, 直接博高赔平局(~3.0){_cup_tag}',
                True,
                f'平P{draw_p:.0f}%为argmax — 模型最看好平局, 直接买平局赔率~3.0',
                False, '')

    # 平局直击: P平≥30%(联赛)/32%(杯赛) 且 距argmax≤10pp (与引擎覆盖规则同口径)
    draw_strike = False
    draw_strike_reason = ''
    draw_value = False
    draw_value_reason = ''
    _gap = argmax_p - draw_p
    if draw_p >= _draw_min and _gap <= _draw_gap_threshold and draw_p != argmax_p:
        draw_strike = True
        _gap_dir = '胜' if win_p == argmax_p else '负'
        draw_strike_reason = (
            f'平P{draw_p:.0f}%仅差{_gap_dir}P{argmax_p:.0f}% {_gap:.0f}pp — '
            f'回测该档平局率40%, 平局@~3.0有正EV, 可博高赔平局{_draw_bonus_tag}'
        )

    # 平局价值单: 26-30%(联赛)/28-32%(杯赛) 且 距argmax≤10pp (与引擎 draw_value 同口径)
    if not draw_strike and _draw_value_min <= draw_p < _draw_min and _gap <= _draw_gap_threshold:
        draw_value = True
        _gap_dir = '胜' if win_p == argmax_p else '负'
        draw_value_reason = (
            f'平P{draw_p:.0f}%距{_gap_dir}P{argmax_p:.0f}% {_gap:.0f}pp — '
            f'回测该档平局率33%, 优于热门方向EV, 建议1/3本金小注博平{_cup_tag}{_draw_bonus_tag}'
        )

    # 🚫 方向性误判高发: 强主场(胜P≥60)但平局不可忽视(平P≥25) → 黑天鹅特征
    if win_p >= 60 and draw_p >= _avoid_threshold:
        return ('avoid',
                f'强主场胜P{win_p:.0f}%但平P{draw_p:.0f}% — 方向性误判高发区(黑天鹅风险), 模型易高估主队{_cup_tag}',
                draw_strike, draw_strike_reason, draw_value, draw_value_reason)
    # ⚠️ 平局窗口+方向模糊: 平P≥阈值 且 argmax<50 → HAD单选不稳, 用HHAD覆盖项
    if draw_p >= _cover_threshold and argmax_p < 50:
        return ('cover',
                f'平P{draw_p:.0f}%且方向P{argmax_p:.0f}%模糊 — 平局高发, HAD单选易漏平{_cup_tag}',
                draw_strike, draw_strike_reason, draw_value, draw_value_reason)
    # ✅ 方向明确
    return ('single', f'方向P{argmax_p:.0f}%明确, 平P{draw_p:.0f}%可控',
            draw_strike, draw_strike_reason, draw_value, draw_value_reason)


def generate(pred_json=None):
    if pred_json is None:
        cands = sorted(glob.glob(os.path.join(_WORKSPACE, 'predictions', 'pred_*.json')),
                       key=os.path.getmtime)
        cands = [c for c in cands if '__v' not in c]  # 排除归档
        if not cands:
            print('[错误] 未找到预测文件')
            return None
        pred_json = cands[-1]
    if not os.path.exists(pred_json):
        print(f'[错误] 预测文件不存在: {pred_json}')
        return None

    with open(pred_json, encoding='utf-8') as f:
        d = json.load(f)
    res = d.get('results', {})
    meta_all = d.get('meta', {})
    base = os.path.basename(pred_json).replace('pred_', '').replace('.json', '')
    _indep = bool(d.get('independent_mode'))  # Ultra 13.17: 独立模式 — 赔率信号只作影子对照

    cards = []
    n_single = n_cover = n_avoid = n_draw = n_draw_strike = n_draw_value = 0
    n_primary_had = n_primary_hhad = 0
    for key in sorted(res.keys(), key=_match_sort_key):
        m = res[key]
        meta = meta_all.get(key, {})
        had, hh = m.get('HAD', {}), m.get('HHAD', {})
        had_open = had.get('had_open', True)  # 旧数据无此字段默认开盘
        handicap = hh.get('handicap')
        league = meta.get('league', '')

        # 首推补充 (命中率优先 = 预测PDF primary_bet, 仅参考不改四档主推)
        pb = (m.get('cross_market') or {}).get('primary_bet') or {}
        primary_opt = pb.get('option', '')
        primary_odds = pb.get('odds', '')
        primary_prob = pb.get('prob', '')
        primary_market = pb.get('market', '')
        if primary_market == 'HHAD':
            n_primary_hhad += 1
        elif primary_market == 'HAD':
            n_primary_had += 1

        # 四档主推 (原判定, 主推以四档为准)
        if not had_open:
            hh_dir = hh.get('dir', '')
            hh_odds = hh.get('odds', '')
            if hh_dir and hh_odds:
                level = 'single'
                n_single += 1
                rec = f"{hh_dir} @{hh_odds} (让球·HAD未开盘)"
                rec_cls, tag = 'single', '✅ 单选'
                reason = f'HAD未开盘, 仅HHAD可选: {hh_dir} (HHAD P={hh.get("p","")})'
            elif hh_dir:
                level = 'single'
                n_single += 1
                rec = f"{hh_dir} (让球·HAD未开盘, 赔率以盘口为准)"  # P1-4: odds=0不渲染@0
                rec_cls, tag = 'single', '✅ 单选'
                reason = f'HAD未开盘, 仅HHAD可选: {hh_dir} (HHAD赔率未同步)'
            else:
                level = 'avoid'
                n_avoid += 1
                rec = '— 本场不买 —'
                rec_cls, tag = 'avoid', '🚫 避开'
                reason = 'HAD/HHAD均未开盘, 不买'
            draw_strike = False
            draw_strike_reason = ''
            draw_value = False
            draw_value_reason = ''
            w, dr, l = _parse_probs(hh.get('p', '0/0/0'))
        else:
            w, dr, l = _parse_probs(had.get('p', '0/0/0'))
            argmax_p = max(w, dr, l)
            level, reason, draw_strike, draw_strike_reason, draw_value, draw_value_reason = classify(
                dr, argmax_p, w, l, league, bk_intent=m.get('bookmaker_intent'))  # Ultra 13.12: 传入庄家意图

            # HHAD 覆盖项 (含平局的一侧): 让球盘→让负(平+负), 受让盘→受让胜(胜+平)
            if handicap is not None:
                cover_side = '受让胜' if float(handicap) > 0 else '让负'
            else:
                cover_side = hh.get('dir', '')

            if level == 'draw':
                n_draw += 1
                _do = had.get('draw_odds') or '3.00'  # P1-4: 兜底默认
                rec = f"平 @{_do} (胜平负·平局直击)"
                rec_cls, tag = 'draw', '🎯 平局直击'
            elif level == 'single':
                n_single += 1
                _ho = had.get('odds', '')
                # P1-4: odds=None/0 (SWOT翻转或档位停售) → 注明以盘口为准, 不渲染"@0"
                _ho_s = f'@{_ho}' if _ho else '(赔率以盘口为准)'
                rec = f"{had.get('dir','')} {_ho_s} (胜平负)"
                rec_cls, tag = 'single', '✅ 单选'
            elif level == 'cover':
                n_cover += 1
                cover_odds = hh.get('odds', '') if cover_side == hh.get('dir', '') else ''
                if cover_odds:
                    rec = f"{cover_side} @{cover_odds} (让球·覆盖平局)"
                else:
                    rec = f"{cover_side} (让球·覆盖平局, 赔率以盘口为准)"
                rec_cls, tag = 'cover', '⚠️ 双选兜底'
            else:
                n_avoid += 1
                rec = '— 本场不买 —'
                rec_cls, tag = 'avoid', '🚫 避开'

        if draw_strike:
            n_draw_strike += 1
        if draw_value:
            n_draw_value += 1

        conf = hh.get('conf', '') if not had_open else had.get('conf', '')
        # 首推参考行 (补充, 不改四档主推)
        primary_note = ''
        if primary_opt and primary_odds:
            pct = f"P={primary_prob:.0f}%" if isinstance(primary_prob, (int, float)) else ''
            _po_s = f'@{primary_odds}' if primary_odds else '(赔率以盘口为准)'  # P1-4
            primary_note = f'📌 首推参考(命中率优先): <b>{primary_opt} {_po_s}</b> {pct}'
        cards.append({
            'no': key, 'home': meta.get('home', '?'), 'away': meta.get('away', '?'),
            'time': meta.get('match_time', ''), 'league': league,  # P2-8: 加比赛时间+联赛
            'level': level, 'tag': tag, 'rec': rec, 'reason': reason,
            'probs': f'{w:.0f}/{dr:.0f}/{l:.0f}', 'conf': conf,
            'diff': m.get('difficulty', 0), 'agree': m.get('model_agreement', 0),
            'rec_cls': rec_cls,
            'draw_strike': draw_strike, 'draw_strike_reason': draw_strike_reason,
            'draw_value': draw_value, 'draw_value_reason': draw_value_reason,
            'draw_odds': had.get('draw_odds', '3.00'),
            'primary_note': primary_note,
            'mkt_div': m.get('market_divergence'),  # 优化③: 模型-市场分歧
            'bk_intent': (None if _indep else m.get('bookmaker_intent')),  # Ultra 13.17: 独立模式不展示赔率资金信号(方向参照可能已过时)
            'swot_sample_warning': (m.get('swot') or {}).get('sample_warning'),  # 优化②: 小样本警示
        })

    # 过关建议: 四档主推 (单选+平局直击场)
    single_list = [c for c in cards if c['level'] == 'single']
    cover_list = [c for c in cards if c['level'] == 'cover']
    draw_list = [c for c in cards if c['level'] == 'draw']
    draw_value_list = [c for c in cards if c.get('draw_value')]

    card_html = ''
    for c in cards:
        ds_html = ''
        if c.get('draw_strike') and c['level'] != 'draw':
            ds_html = f'<div class="mc-draw-strike">🎯 平局直击: <b>平 @{c.get("draw_odds","3.00")}</b> (胜平负) — {c.get("draw_strike_reason","")}</div>'
        dv_html = ''
        if c.get('draw_value'):
            dv_html = f'<div class="mc-draw-value">💡 平局价值: <b>平 @{c.get("draw_odds","3.00")}</b> (胜平负·小注) — {c.get("draw_value_reason","")}</div>'
        primary_html = f'<div class="mc-primary">{c["primary_note"]}</div>' if c.get('primary_note') else ''
        # 优化③ (Ultra 13.11): 模型-市场分歧警示行 (≥15pp)
        # Ultra 13.17: 独立模式改标"影子对照" — 市场方向只记录不决策, 供双账本对账
        div_html = ''
        _md = c.get('mkt_div')
        if _md and _md.get('flagged'):
            _arrow = '方向相反' if _md.get('dir_conflict') else '幅度偏离'
            if _indep:
                div_html = (f'<div class="mc-mkt-div">🔭 影子对照({_arrow}): 独立意见{_md.get("model_dir","?")}'
                            f'{_md.get("model_prob",0):.0f}% vs 市场热门{_md.get("market_dir","?")}'
                            f'{_md.get("market_prob",0):.0f}%, 差{_md.get("max_diff_pp",0):.0f}pp'
                            f' — 独立模式: 赔率仅作对照记录, 不参与决策, 赛后双账本对账</div>')
            else:
                div_html = (f'<div class="mc-mkt-div">⚠️ 市场分歧({_arrow}): 模型{_md.get("model_dir","?")}'
                            f'{_md.get("model_prob",0):.0f}% vs 市场{_md.get("market_dir","?")}'
                            f'{_md.get("market_prob",0):.0f}%, 分歧{_md.get("max_diff_pp",0):.0f}pp'
                            f' — {_md.get("note","").split("—")[-1].strip() if "—" in str(_md.get("note","")) else "谨慎参考"}</div>')
        # Ultra 13.12: 庄家意图五档行 (资金动量×模型方向)
        bk_html = ''
        _bk = c.get('bk_intent')
        if _bk and _bk.get('tier') not in (None, 'neutral'):
            _tier = _bk.get('tier')
            _cls = {'strong_confirm': 'bk-strong', 'confirm': 'bk-confirm',
                    'caution': 'bk-caution', 'fade': 'bk-fade'}.get(_tier, '')
            _emoji = {'strong_confirm': '💰✅', 'confirm': '💰',
                      'caution': '💰⚠️', 'fade': '💰🚫'}.get(_tier, '💰')
            bk_html = (f'<div class="mc-intent {_cls}">{_emoji} 庄家意图·{_bk.get("tier_label","")}: '
                       f'{_bk.get("note","")}</div>')
        # 优化② (Ultra 13.11): SWOT小样本警示行
        ss_html = ''
        if c.get('swot_sample_warning'):
            ss_html = f'<div class="mc-sample">{c["swot_sample_warning"]}</div>'
        card_html += f'''<div class="mc {c['rec_cls']}">
  <div class="mc-top"><span class="mc-no">{c['no']}</span>
    <span class="mc-time">{c.get('time','')}</span><span class="mc-lg">{c.get('league','')}</span>
    <span class="mc-teams"><b>{c['home']}</b> vs {c['away']}</span>
    <span class="mc-tag {c['rec_cls']}">{c['tag']}</span></div>
  <div class="mc-rec">{c['rec']}</div>
  {primary_html}
  {div_html}
  {bk_html}
  {ss_html}
  {ds_html}
  {dv_html}
  <div class="mc-meta">概率 {c['probs']}% · {c['conf']} · 可预测性 {c['diff']} · 一致性 {c['agree']:.0%}</div>
  <div class="mc-reason">{c['reason']}</div>
</div>'''

    guide = ''
    if draw_list:
        picks = ' + '.join(f"{c['no']}({c['rec'].split('(')[0].strip()})" for c in draw_list)
        guide += f'<div class="ins draw">🎯 <b>平局直击 {n_draw} 场</b>：{picks}。模型最看好平局，直接买平局赔率~3.0，比HHAD覆盖项高2倍收益。</div>'
    if single_list:
        picks = ' + '.join(f"{c['no']}({c['rec'].split('(')[0].strip()})" for c in single_list)
        guide += f'<div class="ins good">✅ <b>可单选 {n_single} 场</b>：{picks}。建议 <b>2-3 关</b> 组合（容错高），不要 6 场全选。</div>'
    if cover_list:
        picks = ' + '.join(f"{c['no']}({c['rec'].split('(')[0].strip()})" for c in cover_list)
        guide += f'<div class="ins warn">⚠️ <b>双选兜底 {n_cover} 场</b>：{picks}。平局高发，务必买 HHAD 覆盖项而非胜平负单选。</div>'
    if draw_value_list:
        picks = ' + '.join(f"{c['no']}(平@{c.get('draw_odds','3.00')})" for c in draw_value_list)
        guide += f'<div class="ins draw">💡 <b>平局价值 {n_draw_value} 场</b>：{picks}。回测该档(平P≥26%且差≤10pp)平局率33%、优于热门方向EV，平局@~3.0接近正期望值。建议用主推的<b>1/3本金</b>小注博高赔，不影响主推收益。</div>'
    if n_draw_strike:
        draw_strike_list = [c for c in cards if c.get('draw_strike') and c['level'] != 'draw']
        if draw_strike_list:
            picks = ' + '.join(f"{c['no']}(平@{c.get('draw_odds','3.00')})" for c in draw_strike_list)
            guide += f'<div class="ins draw">🎯 <b>平局直击 {len(draw_strike_list)} 场</b>：{picks}。平P紧贴argmax(≤10pp)，回测该档平局率40%、平局正EV，可博高赔平局。</div>'
    if n_avoid:
        guide += f'<div class="ins bad">🚫 <b>避开 {n_avoid} 场</b>：方向性误判高发，不买。</div>'
    # 首推参考汇总 (补充, 不改四档主推)
    guide += f'<div class="ins">📌 <b>首推参考(命中率优先)</b>：✅胜平负{n_primary_had}场 · 🎯让球{n_primary_hhad}场。首推=预测PDF「主推」，仅作补充参考，主推以四档为准。</div>'

    # Ultra 13.17: 独立模式横幅 — 本指南由"赔率零输入"预测生成
    _indep_banner = (''
        if not _indep else
        '<div class="ins warn" style="margin:10px 0">🔭 <b>独立模式账本</b>：本指南由独立预测生成 — '
        '赔率零输入，仅 xG-Poisson + Elo + SWOT情报 三要素决策；体彩赔率只作"影子对照"记录，'
        '赛后与市场热门双账本对账，检验独立模型 vs 市场谁更准。</div>')

    html = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>投注选择指南 {base}</title><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f1f5f9;color:#1f2937;padding:14px;max-width:980px;margin:0 auto}}
h1{{font-size:21px;font-weight:800}}
.sub{{color:#64748b;font-size:12.5px;margin-top:4px}}
.card{{background:#fff;border-radius:14px;padding:16px;margin:14px 0;box-shadow:0 1px 4px rgba(15,23,42,.07)}}
.legend{{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}}
.lg{{flex:1;min-width:150px;border-radius:10px;padding:10px;font-size:12px;line-height:1.5}}
.lg.single{{background:#f0fdf4;border:1px solid #86efac}}
.lg.cover{{background:#fffbeb;border:1px solid #fcd34d}}
.lg.avoid{{background:#fef2f2;border:1px solid #fca5a5}}
.lg.draw{{background:#fefce8;border:1px solid #facc15}}
.lg b{{font-size:14px;display:block;margin-bottom:3px}}
.stat{{display:flex;gap:10px;margin-top:10px}}
.st{{flex:1;text-align:center;background:#f8fafc;border-radius:10px;padding:10px}}
.st b{{font-size:22px;display:block}}
.st span{{font-size:11px;color:#64748b}}
.st.s1 b{{color:#16a34a}} .st.s2 b{{color:#d97706}} .st.s3 b{{color:#dc2626}}
.mc{{background:#fff;border-radius:12px;padding:13px 14px;margin:10px 0;border-left:5px solid #cbd5e1;box-shadow:0 1px 3px rgba(15,23,42,.06)}}
.mc.single{{border-left-color:#16a34a}} .mc.cover{{border-left-color:#f59e0b}} .mc.avoid{{border-left-color:#dc2626;background:#fafafa}} .mc.draw{{border-left-color:#eab308;background:linear-gradient(135deg,#fffbeb,#fefce8)}}
.mc-top{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.mc-no{{font-weight:800;color:#334155;background:#eef2f7;border-radius:6px;padding:2px 8px;font-size:12px}}
.mc-time{{color:#0ea5e9;font-weight:700;font-size:12px;font-variant-numeric:tabular-nums}}
.mc-lg{{color:#94a3b8;font-size:11px}}
.mc-teams{{font-size:14px;flex:1}}
.mc-tag{{font-size:12px;font-weight:700;border-radius:6px;padding:3px 8px}}
.mc-tag.single{{background:#dcfce7;color:#15803d}} .mc-tag.cover{{background:#fef3c7;color:#b45309}} .mc-tag.avoid{{background:#fee2e2;color:#b91c1c}} .mc-tag.draw{{background:#fef9c3;color:#92400e}}
.mc-rec{{font-size:17px;font-weight:800;color:#0f172a;margin:9px 0 4px}}
.mc.avoid .mc-rec{{color:#9ca3af}}
.mc-draw-strike{{background:linear-gradient(135deg,#fef3c7,#fef9c3);border:1px solid #f59e0b;border-radius:8px;padding:8px 10px;margin:6px 0;font-size:13px;color:#92400e;line-height:1.5}}
.mc-draw-strike b{{color:#b45309}}
.mc-draw-value{{background:linear-gradient(135deg,#f0f9ff,#e0f2fe);border:1px solid #7dd3fc;border-radius:8px;padding:8px 10px;margin:6px 0;font-size:13px;color:#0c4a6e;line-height:1.5}}
.mc-draw-value b{{color:#0369a1}}
.mc-meta{{font-size:11px;color:#94a3b8}}
.mc-reason{{font-size:12px;color:#475569;margin-top:6px;line-height:1.6;background:#f8fafc;border-radius:8px;padding:8px 10px}}
.mc-primary{{font-size:12px;margin:6px 0;padding:7px 9px;border-radius:8px;line-height:1.5;background:#f0f9ff;border:1px dashed #93c5fd;color:#1d4ed8}}
.mc-mkt-div{{font-size:12px;margin:6px 0;padding:7px 9px;border-radius:8px;line-height:1.5;background:#fef2f2;border:1px solid #f87171;color:#b91c1c;font-weight:600}}
.mc-sample{{font-size:12px;margin:6px 0;padding:7px 9px;border-radius:8px;line-height:1.5;background:#fff7ed;border:1px dashed #fb923c;color:#c2410c}}
.mc-intent{{font-size:12px;margin:6px 0;padding:7px 9px;border-radius:8px;line-height:1.5;font-weight:600}}
.mc-intent.bk-strong{{background:#f0fdf4;border:1px solid #4ade80;color:#15803d}}
.mc-intent.bk-confirm{{background:#f7fee7;border:1px solid #a3e635;color:#4d7c0f}}
.mc-intent.bk-caution{{background:#fffbeb;border:1px solid #fbbf24;color:#b45309}}
.mc-intent.bk-fade{{background:#fef2f2;border:2px solid #dc2626;color:#b91c1c}}
.sec{{font-size:15px;font-weight:800;margin:16px 0 4px}}
.ins{{padding:11px 13px;border-radius:8px;font-size:12.5px;margin:8px 0;line-height:1.7;border-left:4px solid}}
.ins.good{{background:#f0fdf4;border-color:#16a34a}} .ins.warn{{background:#fffbeb;border-color:#f59e0b}} .ins.bad{{background:#fef2f2;border-color:#dc2626}}
.foot{{text-align:center;color:#94a3b8;font-size:11px;margin:18px 0}}
</style></head><body>
<h1>🎯 投注选择指南</h1>
<div class="sub">{base} · {len(cards)} 场 · 四档主推 + 首推参考</div>
{_indep_banner}

<div class="card"><div class="sec" style="margin-top:0">📖 四档图例（主推）</div>
<div class="legend">
  <div class="lg draw"><b>🎯 平局直击</b>平局为最高概率，直接买平局</div>
  <div class="lg single"><b>✅ 单选</b>方向明确，照主推买</div>
  <div class="lg cover"><b>⚠️ 双选兜底</b>平局高发，买 HHAD 覆盖项（含平局）</div>
  <div class="lg avoid"><b>🚫 避开</b>方向性误判高发，不买</div>
</div>
<div class="stat">
  <div class="st s1"><b>{n_draw}</b><span>🎯 平局直击</span></div>
  <div class="st s1"><b>{n_single}</b><span>✅ 可单选</span></div>
  <div class="st s2"><b>{n_cover}</b><span>⚠️ 双选兜底</span></div>
  <div class="st s3"><b>{n_avoid}</b><span>🚫 避开</span></div>
</div>
<div class="ins" style="margin-top:10px">📌 <b>首推参考（命中率优先，仅补充）</b>：✅胜平负 {n_primary_had} 场 · 🎯让球 {n_primary_hhad} 场。首推=预测PDF「主推」，与四档主推并存，仅供参考。</div>
</div>

<div class="card"><div class="sec" style="margin-top:0">🎯 投注建议</div>{guide}</div>

<div class="sec">📋 每场选择</div>
{card_html}

<div class="card"><div class="sec" style="margin-top:0">📏 判定规则</div>
<div class="ins warn" style="border-color:#94a3b8;background:#f8fafc">
<b>四档主推</b>：<br>
· <b>🎯 平局直击</b>：平P为argmax（≥25%） — 模型最看好平局，直接买平局（赔率~3.0，收益远超HHAD覆盖项）<br>
· <b>🎯 平局紧贴</b>：联赛平P≥30%/杯赛≥32% 且距argmax≤10pp — 回测该档平局率40%、平局正EV，可博高赔平局<br>
· <b>💡 平局价值</b>：联赛平P≥26%/杯赛≥28% 且距argmax≤10pp — 回测该档平局率33%、优于热门方向EV，建议1/3本金小注<br>
· <b>✅ 单选</b>：方向P≥50 且非误判高发 · <b>⚠️ 双选兜底</b>：平P≥28(联赛)/≥30(杯赛) 且 方向P&lt;50（平局窗口，改买HHAD覆盖项） · <b>🚫 避开</b>：胜P≥60 且 平P≥25（强主场方向性误判黑天鹅）<br>
<b>杯赛策略</b>：杯赛实际平局率18.7%低于联赛25.8%（674场回测），故杯赛阈值高于联赛，不再单独下调。<br>
<b>首推参考</b>：卡片上的「首推参考」=预测PDF「主推(命中率优先)」，仅作补充，不改变四档主推。<br>
<b>过关</b>：优先 2-3 关，忌 6 场全选（容错为0）。命中率第一，宁缺毋滥。</div></div>
<div class="foot">基于 {base} {len(cards)} 场预测 · 四档主推 + 首推参考 · 仅供研究学习，不构成投注建议</div>
</body></html>'''

    # 交付物输出到脚本所在目录 (/workspace/sporttery, 用户可见可打开)
    # 运行数据(pred JSON)仍在 SPORTTERY_WORKSPACE 隐藏目录, 仅最终指南落地用户区
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f'bet_guide_{base}.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'[指南] 已生成: {out_path} (四档: 🎯{n_draw} ✅{n_single} ⚠️{n_cover} 🚫{n_avoid} | 首推参考: ✅{n_primary_had} 🎯{n_primary_hhad})')
    return out_path


if __name__ == '__main__':
    generate(sys.argv[1] if len(sys.argv) > 1 else None)
