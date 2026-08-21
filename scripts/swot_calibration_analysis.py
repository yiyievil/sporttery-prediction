#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""swot_calibration_analysis.py — SWOT 迁移效果量化分析 (改进#5 修正版, 2026-08-21 入库)

修正自"审视版v1"外部报告的三处方法论错误 (审阅结论):
  1. 翻转三分解: prob_adjust.flipped=True 混合了三种机制, 必须拆开评估 —
     (a) 强信号翻转   |diff|>=6  (Ultra 13.3 设计的翻转分支)
     (b) 常规迁移穿越 2<=|diff|<6 (argmax 顺带反超 — 改进#5已加不穿越上限, 新数据不再产生)
     (c) 平局提升穿越 |diff|<2   (Ultra 9.1 draw-boost 刻意设计)
     v1 把 (b)(c) 的全败记在了 (a) 头上; 而 (a) 在 92 场样本中触发≈0 次。
  2. 方向对比一律用 model_dir_orig (迁移前), 不用迁移后的 model_dir/had.dir
     (v1 用后验方向对比, 得出"方向一致仍被标翻转"的假象)。
  3. 反事实对照: 反向场次必须并列计算 "若按模型原方向 / 若按SWOT方向 / 实际采用方向"
     三个命中率, 否则"SWOT覆盖是负贡献"的因果不成立。
另: 评分预测力回归按 "SWOT倾向侧胜率 vs |评分差|" 设定, 分主/客分别拟合
    (v1 把主客信号混入主胜率, 相互抵消得到 r≈0 的假结论)。

样本纪律 (RULE-016 扩展): 独立模式场次为主口径 (independent_mode=1 或 pred_file 含
_indep); 旧模式场次单独成段仅作描述。多源体系 (15.9, intel_source 字段) 单独分组。

用法: python scripts/swot_calibration_analysis.py [--verbose]
输出: predictions/swot_calibration_data.json + 终端报告
"""
import json
import os
import sqlite3
import sys
import time

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DB_PATH = os.path.join(BASE_DIR, 'predictions', 'regression.db')
PRED_DIR = os.path.join(BASE_DIR, 'predictions')
OUT_PATH = os.path.join(PRED_DIR, 'swot_calibration_data.json')

FLIP_DIFF = 6.0   # 与 swot_fusion_v3.SWOT_FLIP_DIFF 保持一致
MIN_DIFF = 2.0    # 与 swot_fusion_v3.SWOT_MIN_DIFF 保持一致
DIRS = ('胜', '平', '负')


def _parse_swot_score(s):
    import re
    if not s:
        return None
    m = re.search(r'主(-?[\d.]+)/客(-?[\d.]+)', str(s))
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


def _indep_pred(c):
    cols = [r[1] for r in c.execute('PRAGMA table_info(verify_history)')]
    if 'independent_mode' in cols:
        return 'independent_mode'
    return None


def _find_match(pred_json, home, away):
    rs = pred_json.get('results') or {}
    items = rs.values() if isinstance(rs, dict) else (rs if isinstance(rs, list) else [])
    for m in items:
        mh = str(m.get('home_name') or m.get('home') or '')
        ma = str(m.get('away_name') or m.get('away') or '')
        if mh and ma and (home in mh or mh in home) and (away in ma or ma in away):
            return m
    return None


def load_joined(verbose=False):
    """verify_history × 预测JSON 关联, 返回带SWOT账本的逐场记录"""
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    c = conn.cursor()
    indep_col = _indep_pred(c)
    sel = f', {indep_col}' if indep_col else ", (pred_file LIKE '%_indep%')"
    rows = c.execute(
        f'SELECT verify_date, home, away, had_result, pred_file{sel} '
        f'FROM verify_history WHERE had_result IS NOT NULL').fetchall()
    conn.close()

    cache = {}
    out = []
    for row in rows:
        vdate, home, away, result, pred_file, is_indep = row
        if result not in DIRS or not pred_file:
            continue
        if pred_file not in cache:
            try:
                with open(os.path.join(PRED_DIR, pred_file), encoding='utf-8') as f:
                    cache[pred_file] = json.load(f)
            except Exception:
                cache[pred_file] = None
        pj = cache[pred_file]
        if not pj:
            continue
        m = _find_match(pj, home, away)
        if not m:
            continue
        sw = m.get('swot') or {}
        scores = _parse_swot_score(sw.get('swot_score'))
        swot_dir = sw.get('swot_dir')
        model_orig = sw.get('model_dir_orig')  # 迁移前方向 (修正点②)
        if not scores or swot_dir not in DIRS or model_orig not in DIRS:
            continue
        pa = sw.get('prob_adjust') or {}
        final_dir = (m.get('HAD') or {}).get('dir')
        diff = scores[0] - scores[1]
        out.append({
            'date': vdate, 'home': home, 'away': away, 'actual': result,
            'diff': diff, 'swot_dir': swot_dir, 'model_dir_orig': model_orig,
            'final_dir': final_dir, 'flipped': bool(pa.get('flipped')),
            'intel_source': sw.get('intel_source', ''),
            'indep': bool(is_indep),
        })
    if verbose:
        print(f'[swot_analysis] 关联成功 {len(out)} 场 / 验证库 {len(rows)} 场')
    return out


def _hit(recs, dir_key):
    """dir_key 方向命中率"""
    n = len(recs)
    if n == 0:
        return None, 0
    h = sum(1 for r in recs if r.get(dir_key) == r['actual'])
    return h / n, n


def _flip_class(r):
    """翻转三分解 (修正点①)"""
    if not r['flipped']:
        return None
    ad = abs(r['diff'])
    if ad >= FLIP_DIFF:
        return 'strong_signal'      # (a) 强信号翻转 (设计机制)
    if ad >= MIN_DIFF:
        return 'incidental_cross'   # (b) 常规迁移穿越 (已加不穿越上限)
    return 'draw_boost_cross'       # (c) 平局提升穿越 (刻意设计)


def analyze(rows):
    rep = {'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'n_total': len(rows)}
    indep = [r for r in rows if r['indep']]
    legacy = [r for r in rows if not r['indep']]
    rep['n_indep'] = len(indep)
    rep['n_legacy_descriptive_only'] = len(legacy)

    # --- 主口径: 独立模式; 样本不足时全量降级并标注 ---
    scope, scope_note = (indep, '独立模式') if len(indep) >= 30 else (rows, '全量(独立模式<30场, 降级, 结论仅方向性)')
    rep['scope'] = scope_note

    # ① 同向/反向 + 反事实三方对照 (修正点③)
    agree = [r for r in scope if r['swot_dir'] == r['model_dir_orig']]
    disagree = [r for r in scope if r['swot_dir'] != r['model_dir_orig']]
    rep['agree_vs_disagree'] = {
        'agree': {'n': len(agree), 'final_hit': _hit(agree, 'final_dir')[0]},
        'disagree': {
            'n': len(disagree),
            'final_hit': _hit(disagree, 'final_dir')[0],
            'counterfactual_model_hit': _hit(disagree, 'model_dir_orig')[0],
            'counterfactual_swot_hit': _hit(disagree, 'swot_dir')[0],
        },
    }

    # ② 翻转三分解评估 (修正点①)
    flips = {}
    for r in scope:
        cls = _flip_class(r)
        if cls:
            flips.setdefault(cls, []).append(r)
    rep['flip_decomposition'] = {
        cls: {
            'n': len(rs),
            'final_hit': _hit(rs, 'final_dir')[0],
            'counterfactual_model_hit': _hit(rs, 'model_dir_orig')[0],
            'matches': [f"{r['date']} {r['home']}vs{r['away']} diff={r['diff']:+.1f} "
                        f"模型原向{r['model_dir_orig']}→{r['final_dir']} 实际{r['actual']}"
                        for r in rs],
        } for cls, rs in flips.items()
    }

    # ③ 评分差分档: SWOT倾向侧胜率 (修正: 不按主胜率, 按倾向侧), 分主/客
    bands = [(6, 99, '≥6'), (3, 6, '3-6'), (1, 3, '1-3'), (0, 1, '0-1')]
    side_stats = {'home_favored': {}, 'away_favored': {}}
    for lo, hi, label in bands:
        for key, cond in (('home_favored', lambda r: r['diff'] > 0),
                          ('away_favored', lambda r: r['diff'] < 0)):
            rs = [r for r in scope if cond(r) and lo <= abs(r['diff']) < hi]
            if rs:
                hit, n = _hit(rs, 'swot_dir')
                side_stats[key][label] = {'n': n, 'swot_side_win_rate': round(hit, 3)}
    rep['swot_side_winrate_by_band'] = side_stats

    # ④ 情报源分组 (多源体系 15.9 起)
    src_groups = {}
    for r in scope:
        src_groups.setdefault(r['intel_source'] or 'unknown', []).append(r)
    rep['by_intel_source'] = {
        s: {'n': len(rs), 'final_hit': _hit(rs, 'final_dir')[0]}
        for s, rs in src_groups.items()
    }

    # 样本量警示 (护栏文化: 结论强度标注)
    rep['confidence'] = ('方向性参考 (样本<60)' if len(scope) < 60 else '可支撑参数调整')
    return rep


def print_report(rep):
    print('=' * 64)
    print(f'SWOT 迁移效果量化 (修正版) · 口径: {rep["scope"]} · 结论强度: {rep["confidence"]}')
    print(f'总关联 {rep["n_total"]} 场 (独立模式 {rep["n_indep"]}, 旧模式描述性 {rep["n_legacy_descriptive_only"]})')
    avd = rep['agree_vs_disagree']
    print(f'\n[同向/反向 + 反事实]')
    print(f'  同向 n={avd["agree"]["n"]}: 采用方向命中 {_pct(avd["agree"]["final_hit"])}')
    d = avd['disagree']
    print(f'  反向 n={d["n"]}: 采用 {_pct(d["final_hit"])} | 若按模型原向 {_pct(d["counterfactual_model_hit"])} '
          f'| 若按SWOT {_pct(d["counterfactual_swot_hit"])}')
    print(f'\n[翻转三分解]')
    labels = {'strong_signal': '(a)强信号翻转(|diff|≥6)', 'incidental_cross': '(b)常规迁移穿越',
              'draw_boost_cross': '(c)平局提升穿越'}
    for cls in ('strong_signal', 'incidental_cross', 'draw_boost_cross'):
        info = rep['flip_decomposition'].get(cls)
        if info:
            print(f'  {labels[cls]}: n={info["n"]} 采用命中 {_pct(info["final_hit"])} '
                  f'| 若不翻(模型原向) {_pct(info["counterfactual_model_hit"])}')
        else:
            print(f'  {labels[cls]}: n=0')
    print(f'\n[SWOT倾向侧胜率 × |评分差|]')
    for key, label in (('home_favored', '主队占优'), ('away_favored', '客队占优')):
        bands = rep['swot_side_winrate_by_band'].get(key, {})
        txt = ' '.join(f'{b}:{v["swot_side_win_rate"]:.0%}(n={v["n"]})' for b, v in bands.items())
        print(f'  {label}: {txt or "无数据"}')
    print(f'\n[情报源]')
    for s, v in rep['by_intel_source'].items():
        print(f'  {s}: n={v["n"]} 命中 {_pct(v["final_hit"])}')
    print('=' * 64)


def _pct(x):
    return f'{x:.1%}' if x is not None else 'N/A'


def main():
    verbose = '--verbose' in sys.argv
    rows = load_joined(verbose=verbose)
    if not rows:
        print('无可用数据 (验证库缺失或预测JSON已按成品清理策略移除), 退出')
        return
    rep = analyze(rows)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print_report(rep)
    print(f'数据已写 {OUT_PATH}')


if __name__ == '__main__':
    main()
