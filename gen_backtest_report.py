#!/usr/bin/env python3
"""详细回测报告生成器 — 标准验证流程 (Ultra 15.8, 2026-08-21 用户裁决)

用户裁决: "今后的验证流程都按照这个详细回测来, 固定下来"
  · 验证(v215_verify.py)入库后自动调用本模块 (Phase5.5)
  · 逐场: 全玩法(主推/HAD/HHAD/比分/半全场/总进球) vs 实际 + SWOT + 影子对照
  · 汇总: 经济账(主推/HAD ROI) + 概率校准分桶 + λ/净胜球分析 + 模式发现(数据驱动)
  · 产出: backtest_<date>.html (与 verify_*.pdf / verify_analysis_*.html 并存)

用法: python3 gen_backtest_report.py [verify_date(YYYY-MM-DD)] [--pred pred.json]
数据源: predictions/regression.db verify_history × 预测JSON (逐场 pred_file 关联)
"""
import glob
import json
import os
import re
import sqlite3
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'predictions', 'regression.db')
REPORT_DIR = os.path.join(BASE, 'reports') if os.path.isdir(os.path.join(BASE, 'reports')) else BASE


def _p3(s):
    """'25%/26%/49%' → [25.0, 26.0, 49.0]"""
    nums = re.findall(r'([\d.]+)', str(s or ''))
    return [float(x) for x in nums[:3]] if len(nums) >= 3 else None


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _match_sort_key(k):
    m = re.search(r'(\d+)$', k)
    return (re.sub(r'\d+$', '', k), int(m.group(1)) if m else 0)


def load_backtest_data(verify_date):
    """从DB拉取当日已验证场次, 关联预测JSON → 行数据列表"""
    if not os.path.exists(DB_PATH):
        print('[回测] 回归数据库不存在')
        return None, None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''SELECT * FROM verify_history
                 WHERE verify_date=? AND pred_file != '无' AND pred_file != ''
                 ORDER BY match_key''', (verify_date,))
    vh = [dict(r) for r in c.fetchall()]
    conn.close()
    if not vh:
        print(f'[回测] {verify_date} 无已验证场次(带预测)')
        return None, None
    # 模式判定: 该批任一 pred_file 含 _indep → 独立模式批次 (RULE-016 同口径)
    mode = 1 if any('_indep' in (v.get('pred_file') or '') for v in vh) else 0
    # 预测JSON: 取该批最常用的 pred_file (同一验证日可能混有凌晨旧批次场次,
    # 如2026-08-20含9场周四独立+6场周三旧模式 — 只回测主导批次的场次)
    pf = {}
    for v in vh:
        pf[v['pred_file']] = pf.get(v['pred_file'], 0) + 1
    pred_file = max(pf, key=pf.get)
    vh = [v for v in vh if v['pred_file'] == pred_file]
    pred_path = None
    for cand in (pred_file, os.path.join(BASE, 'predictions', pred_file),
                 os.path.join(BASE, pred_file)):
        if cand and os.path.exists(cand):
            pred_path = cand
            break
    if not pred_path:  # 兜底: 按日期glob
        pats = sorted(glob.glob(os.path.join(BASE, 'predictions', f'pred_{verify_date.replace("-","")[2:]}*_indep.json')))
        pred_path = pats[-1] if pats else None
    if not pred_path:
        print(f'[回测] 预测文件未找到: {pred_file}')
        return None, None
    with open(pred_path, encoding='utf-8') as f:
        d = json.load(f)
    res = d.get('results', {})

    rows = []
    for v in vh:
        m = res.get(v['match_key'], {})
        had, hh, cm = m.get('HAD', {}), m.get('HHAD', {}), m.get('cross_market', {})
        sw, sc = m.get('swot', {}), m.get('score', {})
        hf, tg = m.get('half_full', {}), m.get('total_goals', {})
        pb = cm.get('primary_bet', {})
        rows.append({
            'key': v['match_key'], 'home': v['home'], 'away': v['away'],
            'league': v.get('league') or '', 'score': f"{v['home_score']}-{v['away_score']}",
            'half': f"{v.get('half_home')}-{v.get('half_away')}",
            'actual_had': v['actual_had'], 'actual_hhad': v['actual_hhad'],
            'hcap': v.get('goal_line'), 'lam': m.get('lam', ''),
            'had_dir': had.get('dir'), 'had_p': had.get('p'), 'had_odds': had.get('odds'),
            'hhad_dir': hh.get('dir'), 'hhad_p': hh.get('p'),
            'pb': pb.get('option'), 'pb_prob': _f(pb.get('prob')),
            'pb_odds': _f(pb.get('odds')), 'pb_ev': _f(pb.get('ev_pct')),
            'pb_cov': pb.get('coverage'), 'pb_type': pb.get('selection_type'),
            'score_top3': sc.get('top3'), 'hf_main': hf.get('main'), 'tg_main': tg.get('main'),
            'swot_lean': sw.get('swot_lean'), 'swot_advice': str(sw.get('fusion_advice') or ''),
            'insight': str(cm.get('insight') or ''),
            'had_hit': v['had_hit'], 'hhad_hit': v['hhad_hit'], 'pb_hit': v['pb_hit'],
            'score_hit': v['score_hit'], 'hf_hit': v['hf_hit'], 'tg_hit': v['tg_hit'],
            'rps': _f(v.get('rps_score')), 'll': _f(v.get('log_loss')),
            'mkt_dir': v.get('market_fav_dir'), 'mkt_hit': v.get('market_fav_hit'),
            'mvsm': v.get('model_vs_market'),
            'actual_hf': v.get('actual_hf'), 'actual_tg': v.get('actual_tg'),
            'pred_hf': v.get('pred_hf_combo'), 'pred_tg': v.get('pred_tg_main'),
            'roi': _f(v.get('roi_return')),
            'goals_actual': (_f(v.get('home_score'), 0) or 0) + (_f(v.get('away_score'), 0) or 0),
        })
    return rows, {'mode': mode, 'pred_file': os.path.basename(pred_path), 'date': verify_date}


def _findings(rows):
    """数据驱动的模式发现 — 自动扫描, 只报告有数据支撑的模式"""
    F = []
    n = len(rows)

    # 1. 概率校准: 高置信 vs 低置信
    hi = [r for r in rows if (r['pb_prob'] or 0) >= 60]
    lo = [r for r in rows if r['pb_prob'] is not None and r['pb_prob'] < 60]
    if hi and lo:
        hi_rate = sum(1 for r in hi if r['pb_hit']) / len(hi)
        lo_rate = sum(1 for r in lo if r['pb_hit']) / len(lo)
        detail_hi = ' · '.join(f"{r['key']}{r['pb_prob']:.0f}%{'✓' if r['pb_hit'] else '✗'}" for r in hi)
        if lo_rate > hi_rate + 0.2:
            F.append(('校准反转', f'高置信(≥60%)命中 {hi_rate:.0%} 反而低于低置信 {lo_rate:.0%}',
                      f'{detail_hi} — 概率高估(ECE激进), 高置信数字在概率校准闭环达标前不可作为加仓依据'))
        elif hi_rate < 0.4:
            F.append(('高置信失准', f'高置信(≥60%)仅命中 {hi_rate:.0%}',
                      f'{detail_hi} — 概率校准闭环未启动, EV由未校准概率算出需谨慎'))
    elif hi:
        hi_rate = sum(1 for r in hi if r['pb_hit']) / len(hi)
        F.append(('高置信表现', f'高置信(≥60%)命中 {hi_rate:.0%} ({sum(1 for r in hi if r["pb_hit"])}/{len(hi)})', ''))

    # 2. 主推市场结构 (受让胜/让负覆盖单 vs HAD直选)
    for pat, name in (('受让', '受让侧'),):
        sz = [r for r in rows if pat in str(r['pb'])]
        if sz and len(sz) >= 2:
            rate = sum(1 for r in sz if r['pb_hit'])
            if rate == 0:
                F.append((f'{name}覆盖单全败', f'{len(sz)}场{name}主推 0/{len(sz)}',
                          '悬殊对决覆盖单系统性高估 — λ离散度校准(15.8-A)与深盘收缩(15.2)作用范围核查'))

    # 3. 影子对照: 分歧场表现
    diff = [r for r in rows if r['mvsm'] == 'diff']
    same = [r for r in rows if r['mvsm'] == 'same']
    if diff:
        dm = sum(1 for r in diff if r['had_hit'])
        dk = sum(1 for r in diff if r['mkt_hit'])
        verdict = '完败' if dm == 0 and dk > 0 else ('平手' if dm == dk else '占优' if dm > dk else '落后')
        F.append(('影子对照·分歧场', f'模型 {dm}/{len(diff)} vs 市场 {dk}/{len(diff)} ({verdict})',
                  f"同向{len(same)}场: 模型{sum(1 for r in same if r['had_hit'])}中/"
                  f"市场{sum(1 for r in same if r['mkt_hit'])}中"))

    # 4. SWOT翻转场次表现
    flips = [r for r in rows if '翻转' in r['swot_advice'] or '翻转' in r['insight']]
    if flips:
        hits = sum(1 for r in flips if r['pb_hit'])
        F.append(('SWOT翻转', f'{len(flips)}场翻转: 主推{hits}/{len(flips)}命中',
                  ' · '.join(f"{r['key']}{'✓' if r['pb_hit'] else '✗'}" for r in flips)))

    # 5. λ/净胜球: 总进球mode扎堆 + 实际分布
    tgs_pred = {}
    for r in rows:
        # pred_tg 形如 '2球(24.0%)' → 归一化为 '2球' 再聚合
        _tg = re.sub(r'\(.*', '', str(r['pred_tg'] or '—'))
        tgs_pred[_tg] = tgs_pred.get(_tg, 0) + 1
    if tgs_pred and max(tgs_pred.values()) >= max(3, n * 0.6):
        mode_tg = max(tgs_pred, key=tgs_pred.get)
        act_hi = sum(1 for r in rows if (r['goals_actual'] or 0) >= 3)
        F.append(('总进球扎堆', f'{tgs_pred[mode_tg]}/{n}场预测同一mode"{mode_tg}", 实际≥3球{act_hi}场',
                  'λ离散度不足(向联赛均值过度收缩) — 15.8-A离散度校准作用核查'))

    # 6. 主推同向性重选 (15.8-B) 触发与结果
    realigned = [r for r in rows if '同向性' in r['insight']]
    if realigned:
        hits = sum(1 for r in realigned if r['pb_hit'])
        F.append(('主推同向性重选', f'{len(realigned)}场触发重选: {hits}/{len(realigned)}命中',
                  ' · '.join(f"{r['key']}{'✓' if r['pb_hit'] else '✗'}" for r in realigned)))

    # 7. 深盘收缩 (trap_cal) 触发场次
    # (trap note在预测JSON HHAD.trap_cal_note, 主推命中与否在行内)
    return F


def generate(verify_date=None, pred_path=None):
    """主入口: 生成详细回测HTML. 返回文件路径或None"""
    if not verify_date:
        verify_date = sys.argv[1] if len(sys.argv) > 1 else None
    if not verify_date:
        print('[回测] 用法: gen_backtest_report.py <verify_date YYYY-MM-DD>')
        return None
    rows, meta = load_backtest_data(verify_date)
    if not rows:
        return None

    n = len(rows)
    pb_hits = sum(1 for r in rows if r['pb_hit'])
    had_rows = [r for r in rows if r['had_dir'] and str(r['had_dir']) not in ('未开盘', 'None')]
    had_hits = sum(1 for r in rows if r['had_hit'])
    hhad_hits = sum(1 for r in rows if r['hhad_hit'])
    sc_hits = sum(1 for r in rows if r['score_hit'])
    hf_hits = sum(1 for r in rows if r['hf_hit'])
    tg_hits = sum(1 for r in rows if r['tg_hit'])
    mkt_rows = [r for r in rows if r['mkt_dir']]
    mkt_hits = sum(1 for r in rows if r['mkt_hit'])
    rps_l = [r['rps'] for r in rows if r['rps'] is not None]
    ll_l = [r['ll'] for r in rows if r['ll'] is not None]

    # 经济账: 主推每场1单位
    stake = n
    ret = sum(r['pb_odds'] or 0 for r in rows if r['pb_hit'])
    pb_roi = (ret - stake) / stake * 100 if stake else 0
    had_bet = [r for r in rows if r['roi'] is not None]
    had_profit = sum(r['roi'] for r in had_bet)
    had_roi = had_profit / len(had_bet) * 100 if had_bet else None

    # 校准分桶
    buckets = [('≥70% (高置信)', [r for r in rows if (r['pb_prob'] or 0) >= 70]),
               ('60-70% (中置信)', [r for r in rows if 60 <= (r['pb_prob'] or 0) < 70]),
               ('<60% (低置信)', [r for r in rows if r['pb_prob'] is not None and r['pb_prob'] < 60])]

    # 逐场卡片
    cards = []
    for r in rows:
        hcap = _f(r['hcap'], 0) or 0
        hc_s = f"让{hcap:+g}" if hcap < 0 else f"受让{hcap:g}".replace('受让0', '平手')
        pb_cls = 'win' if r['pb_hit'] else 'lose'
        ev_s = f"EV{r['pb_ev']:+.1f}%" if r['pb_ev'] is not None else ''
        tag = lambda hit: ('<span class="tag ok">✓</span>' if hit
                           else '<span class="tag miss">✗</span>')
        cards.append(f"""
<div class="card">
  <div class="card-hd"><span>【{r['key']}】{r['home']} <b>{r['score']}</b> {r['away']} <small>(半{r['half']})</small></span><span class="mk">{r['league']}</span></div>
  <div class="banner {pb_cls}">
    <span>主推 {'✅' if r['pb_hit'] else '❌'}</span><span class="opt">{r['pb']}</span>
    <span>P={r['pb_prob']:.1f}%{'@' + str(r['pb_odds']) if r['pb_odds'] else ''}</span><span>{ev_s}</span>
    <span style="margin-left:auto;font-size:12px;color:#64748b">覆盖[{r['pb_cov'] or '—'}] · λ={r['lam']}</span>
  </div>
  <div class="grid2">
    <div>
      <div class="row"><span>胜平负</span><span>{r['had_dir']} ({r['had_p']}) → {r['actual_had']} {tag(r['had_hit'])}</span></div>
      <div class="row"><span>让球盘({hc_s})</span><span>{r['hhad_dir']} ({r['hhad_p']}) → {r['actual_hhad']} {tag(r['hhad_hit'])}</span></div>
      <div class="row"><span>比分top3</span><span>{r['score_top3']} {tag(r['score_hit'])}</span></div>
    </div>
    <div>
      <div class="row"><span>半全场</span><span>{r['pred_hf']} → {r['actual_hf']} {tag(r['hf_hit'])}</span></div>
      <div class="row"><span>总进球</span><span>{r['pred_tg']} → {r['actual_tg']}球 {tag(r['tg_hit'])}</span></div>
      <div class="row"><span>市场热门</span><span>{r['mkt_dir'] or '—'} {tag(r['mkt_hit']) if r['mkt_dir'] else ''} · {r['mvsm'] or '—'}</span></div>
      <div class="row"><span>概率质量</span><span>RPS={r['rps'] or '—'} LL={r['ll'] or '—'}</span></div>
    </div>
  </div>
  <div class="note">🧠 SWOT {r['swot_lean']} — {r['swot_advice'][:80]}</div>
</div>""")

    cal_rows = []
    for label, rs in buckets:
        if not rs:
            continue
        h = sum(1 for x in rs if x['pb_hit'])
        pct = h / len(rs) * 100
        detail = ' · '.join(f"{x['key']}{x['pb_prob']:.0f}%{'✓' if x['pb_hit'] else '✗'}" for x in rs)
        gap = pct - sum(x['pb_prob'] for x in rs) / len(rs)
        cal_rows.append(f"""<tr><td><b>{label}</b></td><td>{h}/{len(rs)}</td><td>{pct:.0f}%</td>
<td>{sum(x['pb_prob'] for x in rs)/len(rs):.1f}%</td>
<td class="{'miss' if gap < -20 else ''}">{gap:+.1f}pp</td><td style="font-size:11.5px">{detail}</td></tr>""")

    findings = _findings(rows)
    f_html = ''.join(f"""
<div class="finding"><div class="f-no">{i+1}</div><div class="f-body">
<b>{t}</b><p>{d}{(' — ' + note) if note else ''}</p></div></div>""" for i, (t, d, note) in enumerate(findings))

    mode_s = '独立模式' if meta['mode'] == 1 else '四源模式'
    had_roi_s = f'{had_roi:+.1f}%' if had_roi is not None else '—'
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>详细回测 {verify_date} · {mode_s}</title>
<style>
:root{{--navy:#0a1628;--navy2:#112240;--gold:#c7922e;--goldbg:#faf3e0;--green:#15803d;--greenbg:#dcfce7;--red:#b91c1c;--redbg:#fef2f2;--txt:#1f2937;--muted:#64748b;--line:#e2e8f0}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#f1f5f9;color:var(--txt);padding:20px;font-size:14px}}
.wrap{{max-width:1100px;margin:0 auto}}
.hd{{background:linear-gradient(135deg,#0a1628,#1a2f4a);color:#fff;border-radius:12px;padding:24px 28px;margin-bottom:16px}}
.hd h1{{font-size:20px;margin-bottom:6px}} .hd .sub{{color:#cbd5e1;font-size:12px}}
.stats{{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-top:16px}}
.stat{{background:rgba(255,255,255,.08);border-radius:8px;padding:10px 8px;text-align:center}}
.stat b{{display:block;font-size:18px}} .stat span{{font-size:11px;color:#94a3b8}}
.sec{{background:#fff;border-radius:12px;padding:20px 24px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.sec h2{{font-size:16px;color:var(--navy);border-left:4px solid var(--gold);padding-left:10px;margin-bottom:14px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:var(--navy);color:#fff;padding:7px 9px;text-align:left}} td{{padding:7px 9px;border-bottom:1px solid var(--line)}}
tr:nth-child(even) td{{background:#f8fafc}}
.ok{{color:var(--green);font-weight:700}} .miss{{color:var(--red);font-weight:700}}
.tag{{padding:1px 6px;border-radius:10px;font-size:11px}} .tag.ok{{background:var(--greenbg);color:var(--green)}} .tag.miss{{background:var(--redbg);color:var(--red)}}
.card{{border:1px solid var(--line);border-radius:10px;margin-bottom:14px;overflow:hidden}}
.card-hd{{background:var(--navy2);color:#fff;padding:9px 14px;display:flex;justify-content:space-between;font-size:13px}}
.card-hd .mk{{color:#fbbf24;font-weight:700}}
.banner{{display:flex;align-items:center;gap:10px;padding:10px 14px;font-size:15px}}
.banner.win{{background:var(--greenbg)}} .banner.lose{{background:var(--redbg)}}
.banner .opt{{font-size:17px;font-weight:800;color:var(--navy)}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:0 24px;padding:10px 14px;font-size:12.5px}}
.row{{padding:3px 0;display:flex;justify-content:space-between;border-bottom:1px dashed #eef2f7}}
.row span:first-child{{color:var(--muted)}}
.note{{padding:8px 14px;font-size:12px;color:#475569;background:#f8fafc;border-top:1px solid var(--line)}}
.finding{{display:flex;gap:10px;padding:12px 0;border-bottom:1px dashed var(--line)}}
.f-no{{flex:none;width:26px;height:26px;border-radius:50%;background:var(--goldbg);color:#b45309;font-weight:800;display:flex;align-items:center;justify-content:center;font-size:13px}}
.f-body b{{display:block;color:var(--navy);margin-bottom:3px}} .f-body p{{font-size:12.5px;color:#475569;line-height:1.7}}
.foot{{text-align:center;color:var(--muted);font-size:11px;padding:12px}}
@media(max-width:700px){{.stats{{grid-template-columns:repeat(3,1fr)}} .grid2{{grid-template-columns:1fr}}}}
</style></head><body><div class="wrap">
<div class="hd">
  <h1>📊 详细回测 · {verify_date} ({mode_s} {n}场)</h1>
  <div class="sub">标准验证流程 (Ultra 15.8) · 数据源: {meta['pred_file']} × regression.db</div>
  <div class="stats">
    <div class="stat"><b>{pb_hits}/{n}</b><span>统一主推</span></div>
    <div class="stat"><b>{had_hits}/{len(had_rows)}</b><span>胜平负方向</span></div>
    <div class="stat"><b>{hhad_hits}/{n}</b><span>让球盘方向</span></div>
    <div class="stat"><b>{hf_hits}/{n}</b><span>半全场</span></div>
    <div class="stat"><b>{sc_hits}/{n}</b><span>比分</span></div>
    <div class="stat"><b>{tg_hits}/{n}</b><span>总进球</span></div>
  </div>
</div>
<div class="sec"><h2>一、总览与经济账</h2><table>
<tr><th>维度</th><th>结果</th><th>备注</th></tr>
<tr><td>统一主推</td><td><b>{pb_hits}/{n} ({pb_hits/n*100:.1f}%)</b></td><td>{' · '.join(r['key']+('✓' if r['pb_hit'] else '✗') for r in rows)}</td></tr>
<tr><td>主推投注ROI(每场1单位)</td><td class="{'ok' if pb_roi>0 else 'miss'}">{pb_roi:+.1f}%</td><td>投{n}回{ret:.2f}</td></tr>
<tr><td>HAD方向投注ROI</td><td>{had_roi_s}</td><td>{len(had_bet)}注</td></tr>
<tr><td>影子对照</td><td>模型 {had_hits}/{len(had_rows)} vs 市场 {mkt_hits}/{len(mkt_rows)}</td><td>同向{sum(1 for r in rows if r['mvsm']=='same')}场 / 分歧{sum(1 for r in rows if r['mvsm']=='diff')}场</td></tr>
<tr><td>概率质量</td><td>RPS均值 {sum(rps_l)/len(rps_l):.4f} · LogLoss均值 {sum(ll_l)/len(ll_l):.3f}</td><td>{len(rps_l)}场有预测概率</td></tr>
</table></div>
<div class="sec"><h2>二、逐场全玩法明细</h2>{''.join(cards)}</div>
<div class="sec"><h2>三、主推概率校准</h2><table>
<tr><th>概率档</th><th>命中</th><th>实际</th><th>预测均值</th><th>偏差</th><th>明细</th></tr>
{''.join(cal_rows) or '<tr><td colspan="6">无主推概率数据</td></tr>'}</table></div>
<div class="sec"><h2>四、模式发现(数据驱动)</h2>{f_html or '<p>本批无显著模式</p>'}</div>
<div class="foot">标准回测流程 · {verify_date} · 预测时点数据(赛前) · 模式: {mode_s}</div>
</div></body></html>"""

    out = os.path.join(REPORT_DIR, f'backtest_{verify_date.replace("-","")}.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'  [回测] 详细回测报告: {out}')
    return out


if __name__ == '__main__':
    generate()
