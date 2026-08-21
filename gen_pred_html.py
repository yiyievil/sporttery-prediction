#!/usr/bin/env python3
"""预测报告 HTML 生成器 — 指南版式统一版 (Ultra 15.5, 2026-08-20 用户裁决)

统一原则 (指南与预测并轨, 一个口径):
  · 每场只有一个主推 = cross_market.primary_bet (命中率优先; Ultra 15.3 同源
    HAD锚×净胜球形状 + 15.2 陷阱校准后的诚实概率)
  · 旧"四档主推"分类 (单选/双选兜底/平局直击/避开) 不再作为推荐输出
  · 双选推荐 / 纯方向 / 让平直推 / 平局关注 作为次选参考, 全部来自同一 cross_market 体系

用法: python3 gen_pred_html.py [pred_json_path]
"""
import glob
import json
import os
import re
import sys


def _parse3(s):
    nums = re.findall(r'([\d.]+)\s*%', str(s or ''))
    if len(nums) >= 3:
        return [float(x) for x in nums[:3]]
    return None


def _odds_s(o):
    return f'@{o}' if o else '(以盘口为准)'


def _ev_s(e):
    return f'EV={e:+.1f}%' if isinstance(e, (int, float)) else ''


def _bars(labels, vals):
    html = ''
    mx = max(vals) if vals else 1
    for lab, v in zip(labels, vals):
        w = int(v / mx * 100) if mx else 0
        hot = ' pb-hot' if v == mx else ''
        html += (f'<div class="prow"><span class="plab">{lab}</span>'
                 f'<span class="ptrack"><span class="pbar{hot}" style="width:{w}%"></span></span>'
                 f'<span class="pval">{v:.0f}%</span></div>')
    return html


def _match_sort_key(k):
    m = re.search(r'(\d+)$', k)
    return (re.sub(r'\d+$', '', k), int(m.group(1)) if m else 0)


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
    res = d.get('results', {})
    meta_all = d.get('meta', {})
    base = os.path.basename(pred_json).replace('pred_', '').replace('.json', '')

    cards = []
    n_had = n_hhad = 0
    for key in sorted(res.keys(), key=_match_sort_key):
        m = res[key]
        meta = meta_all.get(key, {})
        had, hh = m.get('HAD', {}), m.get('HHAD', {})
        cm = m.get('cross_market') or {}
        goals = m.get('goals') or {}
        sc = m.get('score') or {}
        hf = m.get('half_full') or {}
        tg = m.get('total_goals') or {}
        dq = m.get('data_quality') or {}
        hcap = hh.get('handicap')
        had_open = had.get('had_open', True)

        # ===== 统一主推 (唯一推荐口径) =====
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
        # Ultra 15.6: 主推横幅醒目化 — 金/绿渐变底+徽标+大字选项+胶囊赔率/概率/EV
        _ev = pb.get('ev_pct')
        # Ultra 15.8-C: 概率校准闭环(n<100)未启动 → EV未校准标注
        _ev_uncal = '⚠未校准' if cm.get('ev_uncalibrated') else ''
        primary_rec = (
            f"<span class='rec-badge'>🏆 主推</span>"
            f"<span class='rec-opt'>{pb.get('option','?')}</span>"
            f"<span class='chip odds'>{_odds_s(pb.get('odds'))}</span>"
            + (f"<span class='chip'>{prob_s}</span>" if prob_s else '')
            + (f"<span class='chip ev'>EV={_ev:+.1f}%{_ev_uncal}</span>" if isinstance(_ev, (int, float)) else ''))
        cov = pb.get('coverage') or ''
        sel_type = pb.get('selection_type') or ''
        adv = pb.get('cost_advantage') or ''
        cov_html = ''
        if sel_type or cov:
            cov_html = (f"<div class='mc-meta'>{'伪单选' if sel_type == '伪单选' else '单选'}"
                        f"{f'· 覆盖 {cov}' if cov else ''}{f' · {adv}' if adv else ''}</div>")

        # 陷阱提示 (标签+校准明细)
        trap_lines = ''
        for tnote in (pb.get('trap_note'), (pb.get('trap_risk') and '让球侧陷阱风险高') or None,
                      hh.get('trap_cal_note')):
            if tnote:
                trap_lines += f"<div class='mc-trap'>⚠️ {tnote}</div>"
        # 次选 (同一体系)
        sub_lines = ''
        dr = cm.get('double_recommend')
        if dr and dr.get('option'):
            sub_lines += (f"<div class='mc-sub'>⑵ 双选: <b>{dr['option']} {_odds_s(dr.get('odds'))}</b>"
                          f" P={dr.get('prob','?')}% {_ev_s(dr.get('ev_pct'))}"
                          f"{' · ' + dr['trap_note'] if dr.get('trap_note') else ''}</div>")
        pd_ = cm.get('pure_direction_bet')
        # 过滤: 纯方向仅在有参考价值时展示 (P>=35%), HAD未开盘场会退化到低概率HHAD项, 徒增噪音
        if pd_ and pd_.get('option') and pd_.get('option') != pb.get('option') \
                and isinstance(pd_.get('prob'), (int, float)) and pd_.get('prob', 0) >= 35:
            sub_lines += (f"<div class='mc-sub'>⑶ 纯方向: <b>{pd_['option']} {_odds_s(pd_.get('odds'))}</b>"
                          f" P={pd_.get('prob','?')}% {_ev_s(pd_.get('ev_pct'))}</div>")
        ldr = cm.get('let_draw_rec')
        if ldr and ldr.get('option'):
            sub_lines += (f"<div class='mc-sub'>💡 让平窗口: <b>{ldr['option']}</b>"
                          f" {ldr.get('reason','')[:60]}</div>")
        da = cm.get('draw_attention')
        if da and da.get('option'):
            sub_lines += (f"<div class='mc-sub'>👁 平局关注: <b>{da['option']}</b>"
                          f" P={da.get('prob','?')}% {da.get('reason','')[:50]}</div>")

        # ===== 概率区 (同源: HAD锚 + 派生HHAD) =====
        prob_html = '<div class="prob-grid">'
        had_p = _parse3(had.get('p'))
        if had_open and had_p:
            prob_html += ("<div class='pcell'><div class='ptitle'>胜平负 (锚)</div>"
                          + _bars(['胜', '平', '负'], had_p) + '</div>')
        else:
            prob_html += "<div class='pcell'><div class='ptitle'>胜平负</div><div class='pclosed'>未开盘</div></div>"
        hhad_p = _parse3(hh.get('p'))
        if hhad_p:
            _ss = '✓同源' if hh.get('same_source') else '独立'
            gl = f"{float(hcap):+.1f}" if hcap is not None else ''
            labels = ['受让胜' if (hcap or 0) > 0 else '让胜', '让平', '受让负' if (hcap or 0) > 0 else '让负']
            prob_html += (f"<div class='pcell'><div class='ptitle'>让球盘 {gl} ({_ss})</div>"
                          + _bars(labels, hhad_p) + '</div>')
        md = cm.get('margin_dist') or {}
        if md:
            prob_html += ("<div class='pcell'><div class='ptitle'>净胜球分布</div>"
                          f"<div class='mdist'>赢2+球 {md.get('win_2plus',0):.0f}% · 赢1球 {md.get('win_1',0):.0f}%"
                          f" · 平 {md.get('draw',0):.0f}% · 负 {md.get('lose',0):.0f}%</div>"
                          + (f"<div class='mdist'>穿盘风险: {cm.get('pass_risk',{}).get('level','')}"
                             f" — {cm.get('pass_risk',{}).get('desc','')}</div>" if cm.get('pass_risk') else '')
                          + '</div>')
        prob_html += '</div>'

        # 玩法速览
        misc = []
        if sc.get('top3'):
            misc.append(f"比分 {sc['top3'].replace(' ', ' · ')}")
        if hf.get('main'):
            misc.append(f"半全场 {hf['main']}")
        if tg.get('main'):
            misc.append(f"总进球 {tg['main']}")
        if sc.get('over_main') is not None:
            misc.append(f"大小 {sc.get('market_gl_str','')}盘 大{sc['over_main']:.0f}%")
        misc_html = ("<div class='mc-misc'>" + ' ｜ '.join(misc) + '</div>') if misc else ''

        # 元信息
        conf = hh.get('conf', '') if not had_open else had.get('conf', '')
        xg_s = ''
        if goals.get('using_xg') and goals.get('home_xg'):
            xg_s = f" · xG主{goals['home_xg']} 客{goals['away_xg']}"
        meta_html = (f"<div class='mc-meta'>λ {m.get('lam','')} (主{goals.get('home_expected','?')}:客{goals.get('away_expected','?')})"
                     f" · 可预测性 {m.get('difficulty','?')} · 一致性 {m.get('model_agreement',0):.0%}"
                     f" · 数据{dq.get('score','?')}({dq.get('quality','')}){xg_s}</div>")

        # SWOT / 影子对照
        swot_html = ''
        sw = m.get('swot') or {}
        if sw.get('swot_lean') and sw.get('swot_lean') != '无SWOT数据':
            # Ultra 15.7: 带主推重选/概率同步note时放宽截断, 保证修复痕迹完整可见
            _fa = sw.get('fusion_advice') or ''
            _cap = 110 if '[主推' in _fa else 70
            _fa_s = _fa[:_cap] + ('…' if len(_fa) > _cap else '')
            # Ultra 15.9: 情报源徽章 — 多源交叉(leisu+stats+xg)比单源更可信
            _src = str(sw.get('intel_source') or '')
            _n_it = sw.get('intel_items')
            _src_s = ''
            if _src:
                _multi = '+' in _src
                _src_s = (f" · {'🔗' if _multi else '·'}{_src}"
                          + (f"({_n_it}条)" if isinstance(_n_it, int) else ''))
            swot_html = (f"<div class='mc-swot'>🧠 SWOT {sw.get('swot_lean','')} (评分 {sw.get('swot_score','')}"
                         f"{_src_s}"
                         f"{', ' + _fa_s if _fa_s else ''})</div>")
        div_html = ''
        _md = m.get('market_divergence')
        if _md and _md.get('flagged'):
            _arrow = '方向相反' if _md.get('dir_conflict') else '幅度偏离'
            div_html = (f"<div class='mc-mkt-div'>🔭 影子对照({_arrow}): 独立意见{_md.get('model_dir','?')}"
                        f"{_md.get('model_prob',0):.0f}% vs 市场热门{_md.get('market_dir','?')}"
                        f"{_md.get('market_prob',0):.0f}%, 差{_md.get('max_diff_pp',0):.0f}pp</div>")

        ins = (cm.get('insight') or '')
        ins_html = f"<div class='mc-reason'>{ins[:180]}{'…' if len(ins) > 180 else ''}</div>" if ins else ''

        cards.append({
            'no': key, 'home': meta.get('home', '?'), 'away': meta.get('away', '?'),
            'time': meta.get('match_time', ''), 'league': meta.get('league', ''),
            'cls': cls, 'mtag': mtag, 'rec': primary_rec, 'conf': conf,
            'html': f'''<div class="mc {cls}">
  <div class="mc-top"><span class="mc-no">{key}</span>
    <span class="mc-time">{meta.get('match_time','')}</span><span class="mc-lg">{meta.get('league','')}</span>
    <span class="mc-teams"><b>{meta.get('home','?')}</b> vs {meta.get('away','?')}</span>
    <span class="mc-conf">{conf}</span>
    <span class="mc-tag {cls}">{mtag}</span></div>
  <div class="mc-rec">{primary_rec}</div>
  {cov_html}{trap_lines}{sub_lines}
  {prob_html}{misc_html}{meta_html}{swot_html}{div_html}{ins_html}
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
<title>预测报告 {base}</title><style>
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
.rec-sub{{font-size:12px;color:#475569;font-weight:600}}
.mc-trap{{font-size:12px;margin:6px 0;padding:7px 9px;border-radius:8px;line-height:1.5;background:#fef2f2;border:1px solid #f87171;color:#b91c1c;font-weight:600}}
.mc-sub{{font-size:12.5px;margin:5px 0;padding:7px 9px;border-radius:8px;line-height:1.6;background:#f8fafc;border:1px dashed #cbd5e1;color:#475569}}
.mc-sub b{{color:#1f2937}}
.prob-grid{{display:flex;gap:10px;flex-wrap:wrap;margin:8px 0 2px}}
.pcell{{flex:1;min-width:200px;background:#f8fafc;border-radius:10px;padding:9px 11px}}
.ptitle{{font-size:11.5px;font-weight:700;color:#64748b;margin-bottom:5px}}
.pclosed{{font-size:12px;color:#94a3b8;padding:8px 0}}
.prow{{display:flex;align-items:center;gap:7px;margin:3px 0}}
.plab{{width:46px;font-size:11.5px;color:#475569;text-align:right}}
.ptrack{{flex:1;background:#e2e8f0;border-radius:5px;height:11px;overflow:hidden}}
.pbar{{display:block;height:100%;background:#94a3b8;border-radius:5px}}
.pbar.pb-hot{{background:#2563eb}}
.pval{{width:36px;font-size:11.5px;font-weight:700;color:#1f2937;font-variant-numeric:tabular-nums}}
.mdist{{font-size:11.5px;color:#475569;line-height:1.7}}
.mc-misc{{font-size:12px;color:#475569;background:#f0f9ff;border-radius:8px;padding:7px 9px;margin-top:6px;line-height:1.7}}
.mc-meta{{font-size:11px;color:#94a3b8;margin-top:6px;line-height:1.7}}
.mc-swot{{font-size:12px;margin:6px 0;padding:7px 9px;border-radius:8px;line-height:1.5;background:#f5f3ff;border:1px solid #c4b5fd;color:#5b21b6}}
.mc-mkt-div{{font-size:12px;margin:6px 0;padding:7px 9px;border-radius:8px;line-height:1.5;background:#fef2f2;border:1px solid #f87171;color:#b91c1c;font-weight:600}}
.mc-reason{{font-size:12px;color:#475569;margin-top:6px;line-height:1.6;background:#f8fafc;border-radius:8px;padding:8px 10px}}
.sec{{font-size:15px;font-weight:800;margin:16px 0 4px}}
.ins{{padding:11px 13px;border-radius:8px;font-size:12.5px;margin:8px 0;line-height:1.7;border-left:4px solid #94a3b8;background:#f8fafc}}
.foot{{text-align:center;color:#94a3b8;font-size:11px;margin:18px 0}}
</style></head><body>
<h1>📋 预测报告（统一主推）</h1>
<div class="sub">{base} · {len(cards)} 场 · 每场一个主推（命中率优先 · 同源概率 · 陷阱校准后）</div>

<div class="card">
  {stat_html}
  <div class="ins" style="margin-top:10px">🔗 <b>串关参考（Top3 概率）</b>：{parlay or '—'}。2-3 关为宜，宁缺毋滥。</div>
  <div class="ins">📌 <b>统一口径</b>：主推=概率最高单选（HAD锚×净胜球形状同源导出，深盘/需净2+球侧已按4412场回测基准收缩）。双选/纯方向/让平窗口为次选参考，与主推同一体系、互不打架。</div>
</div>

<div class="sec">📋 每场预测</div>
{card_html}

<div class="foot">基于 {base} {len(cards)} 场预测 · 统一主推口径 (Ultra 15.5) · 仅供研究学习，不构成投注建议</div>
</body></html>'''

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f'pred_report_{base}.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'[预测报告] 已生成: {out_path} (主推: ✅HAD {n_had} · 🎯HHAD {n_hhad})')
    return out_path


if __name__ == '__main__':
    generate(sys.argv[1] if len(sys.argv) > 1 else None)
