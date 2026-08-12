#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_bet_guide_html.py — 投注选择显性化指南 (HTML, 数据驱动)
=============================================================
从预测 JSON 读取每场数据, 按「三档显性化」规则给出每场该怎么买:
  ✅ 单选    方向明确(argmax≥50 且非误判高发) — 照主推买
  ⚠️ 双选兜底 平局窗口(平P≥28 且方向模糊<50) — 改买 HHAD 覆盖项(含平局)
  🚫 避开    方向性误判高发(胜P≥60 且 平P≥25) 或低质量场 — 不买

规则源自 260811 周二 9 场实测复盘 (用户彩票6中2的根因分析):
  - 005/010 平P32/33%+方向模糊 → HHAD覆盖项(受让胜/让负)命中, HAD单选全错
  - 003 胜P62%但平P25% → 方向性误判黑天鹅, HAD/HHAD全错
  - 004 胜P50%方向明确 → HAD单选命中 (平P28但方向不模糊, 不走覆盖)

用法:
  python3 gen_bet_guide_html.py <pred_json路径>        # 指定预测文件
  python3 gen_bet_guide_html.py                        # 最新预测文件
输出: {SPORTTERY_WORKSPACE|脚本目录}/bet_guide_YYYYMMDD_周X.html
"""

import os
import sys
import json
import glob

_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))


def _parse_probs(p_str):
    try:
        return [float(x.replace('%', '')) for x in str(p_str).split('/')]
    except Exception:
        return [0.0, 0.0, 0.0]


def classify(draw_p, argmax_p, win_p, loss_p):
    """三档判定。返回 (level, reason, cover_side)。
    level: 'single'|'cover'|'avoid';  cover_side: 让球盘→'让负' / 受让盘→'受让胜'(含平局项)"""
    # 🚫 方向性误判高发: 强主场(胜P≥60)但平局不可忽视(平P≥25) → 003黑天鹅特征
    if win_p >= 60 and draw_p >= 25:
        return 'avoid', f'强主场胜P{win_p:.0f}%但平P{draw_p:.0f}% — 方向性误判高发区(黑天鹅风险), 模型易高估主队'
    # ⚠️ 平局窗口+方向模糊: 平P≥28 且 argmax<50 → HAD单选不稳, 用HHAD覆盖项
    if draw_p >= 28 and argmax_p < 50:
        return 'cover', f'平P{draw_p:.0f}%且方向P{argmax_p:.0f}%模糊 — 平局高发, HAD单选易漏平'
    # ✅ 方向明确
    return 'single', f'方向P{argmax_p:.0f}%明确, 平P{draw_p:.0f}%可控'


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

    d = json.load(open(pred_json, encoding='utf-8'))
    res = d.get('results', {})
    meta_all = d.get('meta', {})
    base = os.path.basename(pred_json).replace('pred_', '').replace('.json', '')

    cards = []
    n_single = n_cover = n_avoid = 0
    for key in sorted(res.keys(), key=lambda x: x.replace('周二', '').replace('周', '')):
        m = res[key]
        meta = meta_all.get(key, {})
        had, hh = m.get('HAD', {}), m.get('HHAD', {})
        w, dr, l = _parse_probs(had.get('p', '0/0/0'))
        argmax_p = max(w, dr, l)
        handicap = hh.get('handicap')
        level, reason = classify(dr, argmax_p, w, l)

        # HHAD 覆盖项 (含平局的一侧): 让球盘→让负(平+负), 受让盘→受让胜(胜+平)
        if handicap is not None:
            cover_side = '受让胜' if float(handicap) > 0 else '让负'
        else:
            cover_side = hh.get('dir', '')

        if level == 'single':
            n_single += 1
            rec = f"{had.get('dir','')} @{had.get('odds','')} (胜平负)"
            rec_cls, tag = 'single', '✅ 单选'
        elif level == 'cover':
            n_cover += 1
            # 覆盖项赔率: 用HHAD对应项; 概率取覆盖侧概率
            hside = hh.get('p', '').split('/')
            rec = f"{cover_side} @{hh.get('odds','')} (让球·覆盖平局)"
            rec_cls, tag = 'cover', '⚠️ 双选兜底'
        else:
            n_avoid += 1
            rec = '— 本场不买 —'
            rec_cls, tag = 'avoid', '🚫 避开'

        conf = had.get('conf', '')
        cards.append({
            'no': key, 'home': meta.get('home', '?'), 'away': meta.get('away', '?'),
            'level': level, 'tag': tag, 'rec': rec, 'reason': reason,
            'probs': f'{w:.0f}/{dr:.0f}/{l:.0f}', 'conf': conf,
            'diff': m.get('difficulty', 0), 'agree': m.get('model_agreement', 0),
            'rec_cls': rec_cls,
        })

    # 过关建议: 只用单选场
    single_list = [c for c in cards if c['level'] == 'single']
    cover_list = [c for c in cards if c['level'] == 'cover']

    card_html = ''
    for c in cards:
        card_html += f'''<div class="mc {c['rec_cls']}">
  <div class="mc-top"><span class="mc-no">{c['no']}</span>
    <span class="mc-teams"><b>{c['home']}</b> vs {c['away']}</span>
    <span class="mc-tag {c['rec_cls']}">{c['tag']}</span></div>
  <div class="mc-rec">{c['rec']}</div>
  <div class="mc-meta">概率 {c['probs']}% · {c['conf']} · 可预测性 {c['diff']} · 一致性 {c['agree']:.0%}</div>
  <div class="mc-reason">{c['reason']}</div>
</div>'''

    guide = ''
    if single_list:
        picks = ' + '.join(f"{c['no']}({c['rec'].split('(')[0].strip()})" for c in single_list)
        guide += f'<div class="ins good">✅ <b>可单选 {n_single} 场</b>：{picks}。建议 <b>2-3 关</b> 组合（容错高），不要 6 场全选。</div>'
    if cover_list:
        picks = ' + '.join(f"{c['no']}({c['rec'].split('(')[0].strip()})" for c in cover_list)
        guide += f'<div class="ins warn">⚠️ <b>双选兜底 {n_cover} 场</b>：{picks}。平局高发，务必买 HHAD 覆盖项而非胜平负单选。</div>'
    if n_avoid:
        guide += f'<div class="ins bad">🚫 <b>避开 {n_avoid} 场</b>：方向性误判高发，不买。</div>'

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
.lg b{{font-size:14px;display:block;margin-bottom:3px}}
.stat{{display:flex;gap:10px;margin-top:10px}}
.st{{flex:1;text-align:center;background:#f8fafc;border-radius:10px;padding:10px}}
.st b{{font-size:22px;display:block}}
.st span{{font-size:11px;color:#64748b}}
.st.s1 b{{color:#16a34a}} .st.s2 b{{color:#d97706}} .st.s3 b{{color:#dc2626}}
.mc{{background:#fff;border-radius:12px;padding:13px 14px;margin:10px 0;border-left:5px solid #cbd5e1;box-shadow:0 1px 3px rgba(15,23,42,.06)}}
.mc.single{{border-left-color:#16a34a}} .mc.cover{{border-left-color:#f59e0b}} .mc.avoid{{border-left-color:#dc2626;background:#fafafa}}
.mc-top{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.mc-no{{font-weight:800;color:#334155;background:#eef2f7;border-radius:6px;padding:2px 8px;font-size:12px}}
.mc-teams{{font-size:14px;flex:1}}
.mc-tag{{font-size:12px;font-weight:700;border-radius:6px;padding:3px 8px}}
.mc-tag.single{{background:#dcfce7;color:#15803d}} .mc-tag.cover{{background:#fef3c7;color:#b45309}} .mc-tag.avoid{{background:#fee2e2;color:#b91c1c}}
.mc-rec{{font-size:17px;font-weight:800;color:#0f172a;margin:9px 0 4px}}
.mc.avoid .mc-rec{{color:#9ca3af}}
.mc-meta{{font-size:11px;color:#94a3b8}}
.mc-reason{{font-size:12px;color:#475569;margin-top:6px;line-height:1.6;background:#f8fafc;border-radius:8px;padding:8px 10px}}
.sec{{font-size:15px;font-weight:800;margin:16px 0 4px}}
.ins{{padding:11px 13px;border-radius:8px;font-size:12.5px;margin:8px 0;line-height:1.7;border-left:4px solid}}
.ins.good{{background:#f0fdf4;border-color:#16a34a}} .ins.warn{{background:#fffbeb;border-color:#f59e0b}} .ins.bad{{background:#fef2f2;border-color:#dc2626}}
.foot{{text-align:center;color:#94a3b8;font-size:11px;margin:18px 0}}
</style></head><body>
<h1>🎯 投注选择指南</h1>
<div class="sub">{base} · {len(cards)} 场 · 三档显性化（照标注选场即可）</div>

<div class="card"><div class="sec" style="margin-top:0">📖 三档图例</div>
<div class="legend">
  <div class="lg single"><b>✅ 单选</b>方向明确，照主推买</div>
  <div class="lg cover"><b>⚠️ 双选兜底</b>平局高发，买 HHAD 覆盖项（含平局）</div>
  <div class="lg avoid"><b>🚫 避开</b>方向性误判高发，不买</div>
</div>
<div class="stat">
  <div class="st s1"><b>{n_single}</b><span>✅ 可单选</span></div>
  <div class="st s2"><b>{n_cover}</b><span>⚠️ 双选兜底</span></div>
  <div class="st s3"><b>{n_avoid}</b><span>🚫 避开</span></div>
</div></div>

<div class="card"><div class="sec" style="margin-top:0">🎯 投注建议</div>{guide}</div>

<div class="sec">📋 每场选择</div>
{card_html}

<div class="card"><div class="sec" style="margin-top:0">📏 判定规则</div>
<div class="ins warn" style="border-color:#94a3b8;background:#f8fafc">
<b>✅ 单选</b>：方向P≥50 且非误判高发 · <b>⚠️ 双选兜底</b>：平P≥28 且 方向P&lt;50（平局窗口，改买HHAD覆盖项） · <b>🚫 避开</b>：胜P≥60 且 平P≥25（强主场方向性误判黑天鹅）<br>
<b>过关</b>：优先 2-3 关，忌 6 场全选（容错为0）。命中率第一，宁缺毋滥。</div></div>
<div class="foot">基于 {base} {len(cards)} 场预测 · 规则源自 260811 实测复盘 · 仅供研究学习，不构成投注建议</div>
</body></html>'''

    # 交付物输出到脚本所在目录 (/workspace/sporttery, 用户可见可打开)
    # 运行数据(pred JSON)仍在 SPORTTERY_WORKSPACE 隐藏目录, 仅最终指南落地用户区
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f'bet_guide_{base}.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'[指南] 已生成: {out_path} (✅{n_single} ⚠️{n_cover} 🚫{n_avoid})')
    return out_path


if __name__ == '__main__':
    generate(sys.argv[1] if len(sys.argv) > 1 else None)
