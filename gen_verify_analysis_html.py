#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_verify_analysis_html.py — 直观版验证分析报告生成器 (HTML, 数据驱动)
======================================================================
从 predictions/regression.db 的 verify_history 读取赛果+预测+命中数据,
生成一份直观可视化的验证分析报告 (替代原纯表格 PDF 的分析呈现)。

用法:
  python3 gen_verify_analysis_html.py 2026-08-11                 # 指定日期
  python3 gen_verify_analysis_html.py                            # 最近一次验证
输出:
  {SPORTTERY_WORKSPACE|/workspace/sporttery}/verify_analysis_YYYYMMDD.html

数据源: verify_history 表 (had_hit/hhad_hit/score_hit/tg_hit/hf_hit, 五玩法命中)
       及 pred_* 预测字段 / actual_* 实际字段 / roi_return/rps_score/log_loss 指标
"""

import os
import sys
import sqlite3

_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
REGRESSION_DB = os.path.join(_WORKSPACE, 'predictions', 'regression.db')
# 交付物输出到脚本所在目录 (/workspace/sporttery, 用户可见可打开), 无论隐藏运行与否
REPORT_DIR = os.path.dirname(os.path.abspath(__file__))

PLAY_KEYS = ['had', 'hhad', 'score', 'ttg', 'hf']
PLAY_LABELS = {'had': '胜平负', 'hhad': '让球胜平负', 'score': '比分',
               'ttg': '总进球', 'hf': '半全场'}
HIT_COLS = {'had': 'had_hit', 'hhad': 'hhad_hit', 'score': 'score_hit',
            'ttg': 'tg_hit', 'hf': 'hf_hit'}


def _pct(v):
    try:
        return float(v) * 100
    except (TypeError, ValueError):
        return 0.0


def _ring(hit, tot, label):
    pct = hit / tot * 100 if tot else 0
    r, c = 34, 2 * 3.14159 * 34
    dash = c * hit / tot if tot else 0
    color = '#16a34a' if pct >= 60 else ('#f59e0b' if pct >= 40 else '#dc2626')
    return (f'<div class="ring"><svg viewBox="0 0 90 90" width="86" height="86">'
            f'<circle cx="45" cy="45" r="{r}" fill="none" stroke="#e5e7eb" stroke-width="9"/>'
            f'<circle cx="45" cy="45" r="{r}" fill="none" stroke="{color}" stroke-width="9" stroke-linecap="round"'
            f' stroke-dasharray="{dash:.1f} {c:.1f}" transform="rotate(-90 45 45)"/>'
            f'<text x="45" y="42" text-anchor="middle" font-size="19" font-weight="700" fill="{color}">{hit}'
            f'<tspan font-size="13" fill="#9ca3af">/{tot}</tspan></text>'
            f'<text x="45" y="58" text-anchor="middle" font-size="10" fill="#6b7280">{pct:.0f}%</text></svg>'
            f'<div class="ringlabel">{label}</div></div>')


def _badge(ok):
    return '<span class="hit">✓</span>' if ok else '<span class="miss">✗</span>'


def generate(verify_date=None):
    if not os.path.exists(REGRESSION_DB):
        print(f'[错误] 回归库不存在: {REGRESSION_DB}')
        return None
    conn = sqlite3.connect(REGRESSION_DB)
    conn.row_factory = sqlite3.Row
    if verify_date is None:
        verify_date = conn.execute(
            'SELECT verify_date FROM verify_history ORDER BY id DESC LIMIT 1').fetchone()
        verify_date = verify_date[0] if verify_date else None
    if not verify_date:
        print('[错误] 回归库无验证数据')
        conn.close()
        return None
    rows = conn.execute(
        'SELECT * FROM verify_history WHERE verify_date=? ORDER BY match_key', (verify_date,)).fetchall()
    conn.close()
    if not rows:
        print(f'[错误] {verify_date} 无验证数据')
        return None

    # 汇总统计
    tot = len(rows)
    hits = {k: sum(1 for r in rows if r[HIT_COLS[k]] == 1) for k in PLAY_KEYS}
    roi = sum((r['roi_return'] or 0) for r in rows)
    roi_pct = roi / tot * 100 if tot else 0
    brier_vals = [r['brier_score'] for r in rows if r['brier_score'] is not None]
    rps_vals = [r['rps_score'] for r in rows if r['rps_score'] is not None]
    ll_vals = [r['log_loss'] for r in rows if r['log_loss'] is not None]
    brier = sum(brier_vals) / len(brier_vals) if brier_vals else None
    rps = sum(rps_vals) / len(rps_vals) if rps_vals else None
    logloss = sum(ll_vals) / len(ll_vals) if ll_vals else None
    had_rate = hits['had'] / tot * 100 if tot else 0

    # 平局漏点统计
    draw_rows = [r for r in rows if r['actual_had'] == '平' or r['had_result'] == '平']
    draw_missed = [r for r in draw_rows if r['pred_had_dir'] != '平']
    w_in_d = [r for r in draw_rows if r['pred_had_dir'] == '胜']  # 预测胜实际平

    # 每场行
    def _cell(pred_txt, act, key, r):
        ok = r[HIT_COLS[key]] == 1
        cls = 'c-hit' if ok else 'c-miss'
        return f'<td class="{cls}">{pred_txt}<div class="act">实际 {act} {_badge(ok)}</div></td>'

    body_rows = ''
    for r in rows:
        score = f"{r['home_score']}-{r['away_score']}"
        n_hit = sum(1 for k in PLAY_KEYS if r[HIT_COLS[k]] == 1)
        row_cls = 'row-win' if r['had_hit'] == 1 else 'row-lose'
        body_rows += (f'<tr class="{row_cls}"><td class="no">{r["match_key"].replace("周", "周")}</td>'
                      f'<td class="teams"><b>{r["home"]}</b> vs {r["away"]}<div class="final">{score}</div></td>'
                      + _cell(f'胜平负 <b>{r["pred_had_dir"]}</b>@{r["pred_had_odds"]}', r['actual_had'] or r['had_result'], 'had', r)
                      + _cell(f'让球 <b>{r["pred_hhad_dir"]}</b>@{r["pred_hhad_odds"]}', r['actual_hhad'] or r['hhad_result'], 'hhad', r)
                      + f'<td class="c-meta">{n_hit}/5 种玩法命中</td></tr>')

    # 命中矩阵
    mat_rows = ''
    for r in rows:
        score = f"{r['home_score']}-{r['away_score']}"
        tds = ''.join(
            f'<td class="{"mx-hit" if r[HIT_COLS[k]]==1 else "mx-miss"}">{"✓" if r[HIT_COLS[k]]==1 else "✗"}</td>'
            for k in PLAY_KEYS)
        mat_rows += f'<tr><td class="no">{r["match_key"]}</td><td class="t2">{r["home"]} vs {r["away"]} <b>{score}</b></td>{tds}</tr>'

    rings = ''.join(_ring(hits[k], tot, PLAY_LABELS[k]) for k in PLAY_KEYS)

    # 洞察
    insights = []
    if draw_rows:
        insights.append(('bad',
            f"⚠️ <b>平局是最大漏点</b>：{tot} 场中 {len(draw_rows)} 场实际平局"
            f"（{', '.join(r['match_key']+' '+r['home'] for r in draw_rows)}），胜平负漏判 {len(draw_missed)} 场"
            f"{'，「预测胜→实际平」' + str(len(w_in_d)) + ' 次' if w_in_d else ''}。"))
    # 让球盘判别力
    hhad_in_draw_hit = sum(1 for r in draw_rows if r['hhad_hit'] == 1)
    had_in_draw_hit = sum(1 for r in draw_rows if r['had_hit'] == 1)
    if draw_rows and hhad_in_draw_hit > had_in_draw_hit:
        insights.append(('good',
            f"✅ <b>让球盘在平局场次判别力更强</b>：平局场次让球命中 {hhad_in_draw_hit}/{len(draw_rows)} "
            f"vs 胜平负 {had_in_draw_hit}/{len(draw_rows)}——受让盘把强弱拉近，平局窗口天然更高。"))
    # 穿盘
    through = [r for r in rows if r['had_hit'] == 1 and r['hhad_hit'] == 0 and abs((r['home_score'] or 0) - (r['away_score'] or 0)) >= 2]
    if through:
        insights.append(('bad',
            f"⚠️ <b>让球盘大比分穿盘</b>：{', '.join(r['match_key']+' '+r['home'] for r in through)} "
            f"净胜≥2球击穿让球盘预测。"))
    full_hit = [r for r in rows if sum(1 for k in PLAY_KEYS if r[HIT_COLS[k]] == 1) >= 4]
    if full_hit:
        insights.append(('good',
            f"✅ <b>高置信度场次可靠</b>：{', '.join(r['match_key']+' '+r['home'] for r in full_hit)} 四中四/五中五。"))
    insights.append(('warn',
        "📊 <b>建议</b>：平局概率≥28% 的场次附加「平+方向」双选兜底（不直推平），"
        "大比分倾向（预期进球≥2.5）的让球盘慎用让负。"))

    ins_html = ''.join(
        f'<div class="ins {cls}">{txt}</div>' for cls, txt in insights)

    date_disp = verify_date
    kpi_brier = f'{brier:.3f}' if brier is not None else 'N/A'
    kpi_rps = f'{rps:.3f}' if rps is not None else 'N/A'
    kpi_ll = f'{logloss:.2f}' if logloss is not None else 'N/A'

    html = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>竞彩验证报告 {date_disp}</title><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f1f5f9;color:#1f2937;padding:14px;max-width:980px;margin:0 auto}}
h1{{font-size:21px;font-weight:800;letter-spacing:.5px}}
.sub{{color:#64748b;font-size:12.5px;margin-top:4px}}
.card{{background:#fff;border-radius:14px;padding:16px;margin:14px 0;box-shadow:0 1px 4px rgba(15,23,42,.07)}}
.grid5{{display:flex;justify-content:space-between;gap:6px;flex-wrap:wrap}}
.ring{{text-align:center;flex:1;min-width:80px}}
.ringlabel{{font-size:11.5px;color:#475569;margin-top:2px}}
.kpis{{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}}
.kpi{{flex:1;min-width:100px;background:#f8fafc;border-radius:10px;padding:10px;text-align:center}}
.kpi b{{font-size:18px;display:block}}
.kpi span{{font-size:11px;color:#64748b}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th{{text-align:left;color:#64748b;font-weight:600;padding:8px 6px;border-bottom:2px solid #e2e8f0;font-size:11.5px}}
td{{padding:9px 6px;border-bottom:1px solid #eef2f7;vertical-align:top}}
tr.row-win{{background:#f0fdf4}} tr.row-lose{{background:#fff1f2}}
.no{{font-weight:700;color:#334155;white-space:nowrap}}
.teams b{{color:#0f172a}} .final{{font-size:16px;font-weight:800;color:#0f172a;margin-top:2px}}
.act{{font-size:11px;color:#94a3b8;margin-top:3px}}
.hit{{color:#16a34a;font-weight:800}} .miss{{color:#dc2626;font-weight:800}}
.c-hit{{color:#166534}} .c-miss{{color:#991b1b}}
.c-meta{{color:#64748b;white-space:nowrap;font-size:11.5px}}
.mx-hit{{background:#dcfce7;color:#16a34a;font-weight:800;text-align:center}}
.mx-miss{{background:#fee2e2;color:#dc2626;text-align:center}}
.t2{{font-size:11.5px}} .sec{{font-size:15px;font-weight:800;margin:20px 0 8px}}
.ins{{background:#fffbeb;border-left:4px solid #f59e0b;padding:11px 13px;border-radius:8px;font-size:12.5px;margin:8px 0;line-height:1.7}}
.ins.bad{{background:#fef2f2;border-color:#dc2626}} .ins.good{{background:#f0fdf4;border-color:#16a34a}}
.ins.warn{{background:#fffbeb;border-color:#f59e0b}}
.foot{{text-align:center;color:#94a3b8;font-size:11px;margin:18px 0}}
</style></head><body>
<h1>⚽ 竞彩预测验证报告</h1>
<div class="sub">{date_disp} · {tot} 场 · 赛后实际赛果对照</div>

<div class="card"><div class="sec" style="margin-top:0">🎯 五大玩法命中率</div>
<div class="grid5">{rings}</div>
<div class="kpis">
  <div class="kpi"><b style="color:#16a34a">{roi_pct:+.1f}%</b><span>累计ROI(固定1单位)</span></div>
  <div class="kpi"><b>{kpi_brier}</b><span>Brier(越低越好)</span></div>
  <div class="kpi"><b>{kpi_rps}</b><span>RPS(越低越好)</span></div>
  <div class="kpi"><b>{had_rate:.1f}%</b><span>胜平负准确率</span></div>
</div></div>

<div class="card"><div class="sec" style="margin-top:0">📋 每场预测 vs 实际</div>
<table><tr><th>编号</th><th>对阵 · 实际比分</th><th>胜平负</th><th>让球胜平负</th><th>小结</th></tr>{body_rows}</table></div>

<div class="card"><div class="sec" style="margin-top:0">🔬 玩法命中矩阵</div>
<table><tr><th>编号</th><th>对阵 · 比分</th><th>胜平负</th><th>让球</th><th>比分</th><th>总进球</th><th>半全场</th></tr>{mat_rows}</table></div>

<div class="card"><div class="sec" style="margin-top:0">💡 关键洞察</div>{ins_html}</div>
<div class="foot">基于 {date_disp} {tot} 场实测 · 仅供研究学习，不构成投注建议</div>
</body></html>'''

    date_tag = verify_date.replace('-', '')
    out_path = os.path.join(REPORT_DIR, f'verify_analysis_{date_tag}.html')
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'[报告] 已生成: {out_path} ({tot}场, HAD {hits["had"]}/{tot})')
    return out_path


if __name__ == '__main__':
    vd = sys.argv[1] if len(sys.argv) > 1 else None
    generate(vd)
