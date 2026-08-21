#!/usr/bin/env python3
"""投注选择指南 HTML 生成器 — 统一主推版 (Ultra 15.6, 2026-08-20 用户裁决)

统一原则 (与预测报告 gen_pred_html 同一口径):
  · 每场只有一个主推 = cross_market.primary_bet (命中率优先; 15.3 同源概率
    + 15.2 陷阱校准后的诚实概率), 与预测报告/PDF 完全一致, 不再有两套推荐
  · 旧"四档主推"分类 (单选/双选兜底/平局直击/避开) 退役 — 平局玩法降为
    "参考玩法"小注补充, 不再占据推荐位
  · 双选/纯方向/让平窗口为次选参考行, 全部来自同一 cross_market 体系

用法: python3 gen_bet_guide_html.py [pred_json_path]
"""
import glob
import json
import os
import re
import sys

# 复用预测报告的公共渲染助手 (同目录模块)
from gen_pred_html import _parse3, _odds_s, _ev_s, _match_sort_key


def _parse_probs(s):
    """'25.5/26.0/48.5' → (25.5, 26.0, 48.5)"""
    try:
        nums = [float(x) for x in re.findall(r'([\d.]+)', str(s))][:3]
        if len(nums) == 3:
            return tuple(nums)
    except Exception:
        pass
    return (0.0, 0.0, 0.0)


def _draw_refs(dr, argmax_p, w, l, league, is_cup):
    """平局参考玩法判定 (原四档 classify 的平局支线, 降级为参考)

    返回 (strike, strike_reason, value, value_reason)
      strike 平局直击: 平P为argmax(≥25%) — 模型最看好平局
      value  平局价值: 联赛平P≥26/杯赛≥28 且距argmax≤10pp — 回测该档平局率33%
    """
    strike = strike_reason = value = value_reason = None
    thr_v = (28 if is_cup else 26)
    if dr >= 25 and dr == argmax_p:
        strike, strike_reason = True, f'平P{dr:.0f}%为argmax — 模型最看好平局'
    elif dr >= thr_v and (argmax_p - dr) <= 10:
        gap = argmax_p - max(w, l)
        value, value_reason = True, (f'平P{dr:.0f}%距最高方向{argmax_p:.0f}%仅{gap:.0f}pp — '
                                     f'回测该档平局率33%, 优于热门方向EV')
    return strike, strike_reason, value, value_reason


def _is_cup(league):
    return any(k in str(league) for k in ('杯', '冠', '联杯', 'Euro', 'Copa', '非洲联', '欧冠', '欧联', '欧协', '亚冠', '沙王冠'))


def generate(pred_json=None):
    _ws = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
    if pred_json is None:
        cands = sorted(glob.glob(os.path.join(_ws, 'predictions', 'pred_*.json')),
                       key=os.path.getmtime)
        cands = [c for c in cands if '__v' not in c]
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
    _indep = bool(d.get('independent_mode'))

    cards = []
    n_had = n_hhad = n_strike = n_value = 0
    for key in sorted(res.keys(), key=_match_sort_key):
        m = res[key]
        meta = meta_all.get(key, {})
        had, hh = m.get('HAD', {}), m.get('HHAD', {})
        had_open = had.get('had_open', True)
        cm = m.get('cross_market') or {}
        league = meta.get('league', '')

        # ===== 统一主推 =====
        pb = cm.get('primary_bet') or {}
        mkt = pb.get('market', '')
        if mkt == 'HAD':
            n_had += 1
        elif mkt == 'HHAD':
            n_hhad += 1
        cls = 'single' if mkt == 'HAD' else 'cover'
        mtag = '✅ 主推·胜平负' if mkt == 'HAD' else '🎯 主推·让球'
        prob = pb.get('prob')
        prob_s = f'P={prob:.1f}%' if isinstance(prob, (int, float)) else ''
        _ev = pb.get('ev_pct')
        # Ultra 15.8-C: 概率校准闭环(n<100)未启动 → EV未校准标注
        _ev_uncal = '⚠未校准' if cm.get('ev_uncalibrated') else ''
        rec_html = (
            f"<span class='rec-badge'>🏆 主推</span>"
            f"<span class='rec-opt'>{pb.get('option','—')}</span>"
            f"<span class='chip odds'>{_odds_s(pb.get('odds'))}</span>"
            + (f"<span class='chip'>{prob_s}</span>" if prob_s else '')
            + (f"<span class='chip ev'>EV={_ev:+.1f}%{_ev_uncal}</span>" if isinstance(_ev, (int, float)) else ''))

        trap_lines = ''
        for tnote in (pb.get('trap_note'), (pb.get('trap_risk') and '让球侧陷阱风险高') or None):
            if tnote:
                trap_lines += f"<div class='mc-trap'>⚠️ {tnote}</div>"

        # 次选参考 (同一 cross_market 体系)
        sub_lines = ''
        dr_ = cm.get('double_recommend')
        if dr_ and dr_.get('option'):
            sub_lines += (f"<div class='mc-sub'>⑵ 双选: <b>{dr_['option']} {_odds_s(dr_.get('odds'))}</b>"
                          f" P={dr_.get('prob','?')}% {_ev_s(dr_.get('ev_pct'))}"
                          f"{' · ' + dr_['trap_note'] if dr_.get('trap_note') else ''}</div>")
        pd_ = cm.get('pure_direction_bet')
        if pd_ and pd_.get('option') and pd_.get('option') != pb.get('option') \
                and isinstance(pd_.get('prob'), (int, float)) and pd_.get('prob', 0) >= 35:
            sub_lines += (f"<div class='mc-sub'>⑶ 纯方向: <b>{pd_['option']} {_odds_s(pd_.get('odds'))}</b>"
                          f" P={pd_.get('prob','?')}% {_ev_s(pd_.get('ev_pct'))}</div>")
        ldr = cm.get('let_draw_rec')
        if ldr and ldr.get('option'):
            sub_lines += (f"<div class='mc-sub'>💡 让平窗口: <b>{ldr['option']}</b> {ldr.get('reason','')[:60]}</div>")

        # 平局参考玩法 (原四档平局支线, 降级为小注参考)
        strike = strike_reason = value = value_reason = None
        draw_odds = had.get('draw_odds') or '3.00'
        if had_open:
            w, dr, l = _parse_probs(had.get('p', '0/0/0'))
            strike, strike_reason, value, value_reason = _draw_refs(
                dr, max(w, dr, l), w, l, league, _is_cup(league))
        if strike:
            n_strike += 1
            sub_lines += (f"<div class='mc-draw-strike'>🎯 平局参考: <b>平 @{draw_odds}</b> (胜平负) — "
                          f"{strike_reason}, 可直接买平局</div>")
        elif value:
            n_value += 1
            sub_lines += (f"<div class='mc-draw-strike'>💡 平局价值参考: <b>平 @{draw_odds}</b> (胜平负) — "
                          f"{value_reason}, 建议1/3本金小注</div>")

        # 影子对照 / 市场分歧
        div_html = ''
        _md = m.get('market_divergence')
        if _md and _md.get('flagged'):
            _arrow = '方向相反' if _md.get('dir_conflict') else '幅度偏离'
            if _indep:
                div_html = (f"<div class='mc-mkt-div'>🔭 影子对照({_arrow}): 独立意见{_md.get('model_dir','?')}"
                            f"{_md.get('model_prob',0):.0f}% vs 市场热门{_md.get('market_dir','?')}"
                            f"{_md.get('market_prob',0):.0f}%, 差{_md.get('max_diff_pp',0):.0f}pp</div>")
            else:
                div_html = (f"<div class='mc-mkt-div'>⚠️ 市场分歧({_arrow}): 模型{_md.get('model_dir','?')}"
                            f"{_md.get('model_prob',0):.0f}% vs 市场{_md.get('market_dir','?')}"
                            f"{_md.get('market_prob',0):.0f}%, 分歧{_md.get('max_diff_pp',0):.0f}pp</div>")
        # 庄家意图 (非独立模式)
        bk_html = ''
        _bk = m.get('bookmaker_intent') if not _indep else None
        if _bk and _bk.get('tier') not in (None, 'neutral'):
            _tier = _bk.get('tier')
            _c = {'strong_confirm': 'bk-strong', 'confirm': 'bk-confirm',
                  'caution': 'bk-caution', 'fade': 'bk-fade'}.get(_tier, '')
            _e = {'strong_confirm': '💰✅', 'confirm': '💰',
                  'caution': '💰⚠️', 'fade': '💰🚫'}.get(_tier, '💰')
            bk_html = (f"<div class='mc-intent {_c}'>{_e} 庄家意图·{_bk.get('tier_label','')}: "
                       f"{_bk.get('note','')}</div>")
        ss_html = ''
        if (m.get('swot') or {}).get('sample_warning'):
            ss_html = f"<div class='mc-sample'>{m['swot']['sample_warning']}</div>"

        had_p = _parse3(had.get('p')) if had_open else None
        hhad_p = _parse3(hh.get('p'))
        probs_s = ('—' if not had_p else f'{had_p[0]:.0f}/{had_p[1]:.0f}/{had_p[2]:.0f}')
        if hhad_p:
            probs_s += f' | 让球 {hhad_p[0]:.0f}/{hhad_p[1]:.0f}/{hhad_p[2]:.0f}'
        conf = hh.get('conf', '') if not had_open else had.get('conf', '')
        ins = cm.get('insight') or ''
        reason_s = ins[:140] + ('…' if len(ins) > 140 else '')

        cards.append({
            'no': key, 'html': f'''<div class="mc {cls}">
  <div class="mc-top"><span class="mc-no">{key}</span>
    <span class="mc-time">{meta.get('match_time','')}</span><span class="mc-lg">{league}</span>
    <span class="mc-teams"><b>{meta.get('home','?')}</b> vs {meta.get('away','?')}</span>
    <span class="mc-conf">{conf}</span>
    <span class="mc-tag {cls}">{mtag}</span></div>
  <div class="mc-rec">{rec_html}</div>
  {trap_lines}{sub_lines}{div_html}{bk_html}{ss_html}
  <div class="mc-meta">概率(胜/平/负) {probs_s}% · {conf} · 可预测性 {m.get('difficulty','?')} · 一致性 {m.get('model_agreement',0):.0%}</div>
  <div class="mc-reason">{reason_s}</div>
</div>''',
            'pb_prob': prob if isinstance(prob, (int, float)) else 0,
            'pb_opt': pb.get('option', ''),
        })

    # ===== 汇总区 =====
    top = sorted([c for c in cards if c['pb_prob'] > 0], key=lambda x: x['pb_prob'], reverse=True)
    # 前缀剥离: 先HHAD后HAD (replace顺序反了会把HHAD吃成"H")
    parlay = ' + '.join(f"{c['no']} {re.sub(r'^(HHAD|HAD)', '', c['pb_opt'])}" for c in top[:3])
    stat_html = (f'<div class="stat">'
                 f'<div class="st"><b>{len(cards)}</b><span>场次</span></div>'
                 f'<div class="st s1"><b>{n_had}</b><span>✅ 主推·胜平负</span></div>'
                 f'<div class="st s2"><b>{n_hhad}</b><span>🎯 主推·让球</span></div>'
                 f'<div class="st s3"><b>{sum(1 for c in top if c["pb_prob"]>=65)}</b><span>P≥65% 高把握</span></div>'
                 f'</div>')
    card_html = ''.join(c['html'] for c in cards)

    html = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>投注选择指南 {base}</title><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f1f5f9;color:#1f2937;padding:14px;max-width:980px;margin:0 auto}}
h1{{font-size:21px;font-weight:800}}
.sub{{color:#64748b;font-size:12.5px;margin-top:4px}}
.card{{background:#fff;border-radius:14px;padding:16px;margin:14px 0;box-shadow:0 1px 4px rgba(15,23,42,.07)}}
.stat{{display:flex;gap:10px;margin-top:10px}}
.st{{flex:1;text-align:center;background:#f8fafc;border-radius:10px;padding:10px}}
.st b{{font-size:22px;display:block}}
.st span{{font-size:11px;color:#64748b}}
.st.s1 b{{color:#16a34a}} .st.s2 b{{color:#d97706}} .st.s3 b{{color:#dc2626}}
.mc{{background:#fff;border-radius:12px;padding:13px 14px;margin:10px 0;border-left:5px solid #cbd5e1;box-shadow:0 1px 3px rgba(15,23,42,.06)}}
.mc.single{{border-left-color:#16a34a}} .mc.cover{{border-left-color:#f59e0b}}
.mc-top{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.mc-no{{font-weight:800;color:#334155;background:#eef2f7;border-radius:6px;padding:2px 8px;font-size:12px}}
.mc-time{{color:#0ea5e9;font-weight:700;font-size:12px;font-variant-numeric:tabular-nums}}
.mc-lg{{color:#94a3b8;font-size:11px}}
.mc-teams{{font-size:14px;flex:1}}
.mc-conf{{color:#b45309;font-size:12px}}
.mc-tag{{font-size:12px;font-weight:700;border-radius:6px;padding:3px 8px}}
.mc-tag.single{{background:#dcfce7;color:#15803d}} .mc-tag.cover{{background:#fef3c7;color:#b45309}}
.mc-rec{{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin:10px 0 5px;padding:10px 13px;border-radius:10px;background:linear-gradient(135deg,#fffbeb,#fde68a);border:2px solid #f59e0b}}
.mc.single .mc-rec{{background:linear-gradient(135deg,#f0fdf4,#bbf7d0);border-color:#16a34a}}
.rec-badge{{font-size:12px;font-weight:800;color:#fff;background:#f59e0b;border-radius:6px;padding:3px 9px;letter-spacing:2px}}
.mc.single .rec-badge{{background:#16a34a}}
.rec-opt{{font-size:21px;font-weight:800;color:#0f172a;letter-spacing:.5px}}
.chip{{font-size:12.5px;font-weight:700;color:#334155;background:#fff;border:1px solid #e2e8f0;border-radius:999px;padding:3px 10px;font-variant-numeric:tabular-nums}}
.chip.odds{{color:#b45309;border-color:#fcd34d;background:#fffbeb}}
.chip.ev{{color:#15803d;border-color:#86efac;background:#f0fdf4}}
.mc-trap{{font-size:12px;margin:6px 0;padding:7px 9px;border-radius:8px;line-height:1.5;background:#fef2f2;border:1px solid #f87171;color:#b91c1c;font-weight:600}}
.mc-sub{{font-size:12.5px;margin:5px 0;padding:7px 9px;border-radius:8px;line-height:1.6;background:#f8fafc;border:1px dashed #cbd5e1;color:#475569}}
.mc-sub b{{color:#1f2937}}
.mc-draw-strike{{background:linear-gradient(135deg,#fef3c7,#fef9c3);border:1px solid #f59e0b;border-radius:8px;padding:8px 10px;margin:6px 0;font-size:12.5px;color:#92400e;line-height:1.6}}
.mc-draw-strike b{{color:#b45309}}
.mc-meta{{font-size:11px;color:#94a3b8;margin-top:6px;line-height:1.7}}
.mc-mkt-div{{font-size:12px;margin:6px 0;padding:7px 9px;border-radius:8px;line-height:1.5;background:#fef2f2;border:1px solid #f87171;color:#b91c1c;font-weight:600}}
.mc-sample{{font-size:12px;margin:6px 0;padding:7px 9px;border-radius:8px;line-height:1.5;background:#fff7ed;border:1px dashed #fb923c;color:#c2410c}}
.mc-intent{{font-size:12px;margin:6px 0;padding:7px 9px;border-radius:8px;line-height:1.5;font-weight:600}}
.mc-intent.bk-strong{{background:#f0fdf4;border:1px solid #4ade80;color:#15803d}}
.mc-intent.bk-confirm{{background:#f7fee7;border:1px solid #a3e635;color:#4d7c0f}}
.mc-intent.bk-caution{{background:#fffbeb;border:1px solid #fbbf24;color:#b45309}}
.mc-intent.bk-fade{{background:#fef2f2;border:2px solid #dc2626;color:#b91c1c}}
.mc-reason{{font-size:12px;color:#475569;margin-top:6px;line-height:1.6;background:#f8fafc;border-radius:8px;padding:8px 10px}}
.sec{{font-size:15px;font-weight:800;margin:16px 0 4px}}
.ins{{padding:11px 13px;border-radius:8px;font-size:12.5px;margin:8px 0;line-height:1.7;border-left:4px solid #94a3b8;background:#f8fafc}}
.foot{{text-align:center;color:#94a3b8;font-size:11px;margin:18px 0}}
</style></head><body>
<h1>🎯 投注选择指南（统一主推）</h1>
<div class="sub">{base} · {len(cards)} 场 · 与预测报告同一主推口径（命中率优先 · 同源概率 · 陷阱校准）</div>

<div class="card">
  {stat_html}
  <div class="ins" style="margin-top:10px">🔗 <b>串关参考（Top3 概率）</b>：{parlay or '—'}。2-3 关为宜，宁缺毋滥。</div>
  <div class="ins">📌 <b>口径说明</b>：每场唯一主推=概率最高单选；双选/纯方向/平局参考为次选补充，同一体系。平局玩法仅为小注参考（🎯直击/💡价值），不占主推位。</div>
</div>

<div class="sec">📋 每场选择</div>
{card_html}

<div class="foot">基于 {base} {len(cards)} 场预测 · 统一主推口径 (Ultra 15.6) · 仅供研究学习，不构成投注建议</div>
</body></html>'''

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f'bet_guide_{base}.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'[指南] 已生成: {out_path} (统一主推: ✅HAD {n_had} · 🎯HHAD {n_hhad} | 平局参考: 🎯{n_strike} 💡{n_value})')
    return out_path


if __name__ == '__main__':
    generate(sys.argv[1] if len(sys.argv) > 1 else None)
