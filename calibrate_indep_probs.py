#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calibrate_indep_probs.py — Ultra 15.0 进化三层·第三层: 独立模式概率校准闭环
=======================================================================

用户需求 (2026-08-20):
  "让模型输出的概率真正对齐实际频率 (52%的概率真的命中52%), 方向选择自然更准。
   C1-C4 目前是启发式, 之后用统计校准替换。"

数据来源 (RULE-016 合规):
  regression.db · verify_history · independent_mode=1 场次
  - pred_had_probs : 预测时模型输出的胜/平/负概率 ("55%/28%/17%")
  - actual_had     : 实际赛果
  旧四源模式场次永久隔离 (赔率融合的概率特性对新模型无代表性)。

校准方法 — 逐类 Platt scaling (logistic 回归):
  每类独立拟合 p_cal = sigmoid(a·logit(p) + b), 三类输出重归一:
  - a<1: 概率分布过锐(过度自信) → 压平; a>1: 过钝 → 锐化
  - b>0: 该类系统性低估 → 抬升; b<0: 高估 → 压低
  典型病灶: 三源几何融合易低估平局 (b_平 > 0 修正)

护栏 (与 L2 同框架):
  1. 冷启动: n<100 不产出 (概率校准比权重学习更易过拟合, 门槛更高)
  2. 收缩  : 100→200 线性向恒等校准(a=1,b=0)收缩
  3. 增益  : 加权对数损失须改善 ≥ MIN_GAIN, 否则不启用

输出:
  predictions/indep_prob_calibration.json
  v215_e2e 独立分支在 C1-C3 之后应用 (applied=true 时), 重归一后进入方向判定。

入口:
  CLI    : python3 calibrate_indep_probs.py [--min-n 100] [--full-n 200]
  retrain(): v215_verify 每期验证后自动调用 (已挂载), 返回一句话摘要
"""
import json
import math
import os
import sqlite3
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'predictions', 'regression.db')
OUT_PATH = os.path.join(BASE_DIR, 'predictions', 'indep_prob_calibration.json')

MIN_N = 100     # 冷启动门槛 (概率校准样本需求高于权重学习)
FULL_N = 200    # 完全信任样本量
MIN_GAIN = 0.001
HALF_LIFE_DAYS = 90   # 与 learn_fusion_weights 同口径的时间衰减
_EPS = 1e-6
# Platt 参数界 (防小样本退化: a过锐/过钝属过拟合; b=±0.5 可修正约7pp量级的系统偏差)
A_RANGE = (0.5, 1.6)
B_RANGE = (-0.50, 0.50)
CLIP_P = (0.02, 0.98)   # logit 输入裁剪

DIRS = ('胜', '平', '负')


def _parse_probs(s):
    """'55%/28%/17.0%' → [0.55,0.28,0.17] (重归一); None/非法 → None"""
    if not s or not isinstance(s, str):
        return None
    try:
        p = [float(x.strip().rstrip('%')) / 100.0 for x in s.split('/')]
    except ValueError:
        return None
    if len(p) != 3 or min(p) <= 0.0 or max(p) >= 1.0:
        return None
    t = sum(p)
    return [x / t for x in p]


def load_rows():
    """独立模式带模型概率+赛果场次; 返回 [{date, probs, outcome_idx, w_date}]"""
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    c = conn.cursor()
    try:
        rows = c.execute('''SELECT verify_date, pred_had_probs, actual_had
                            FROM verify_history
                            WHERE independent_mode=1
                              AND actual_had IN ('胜','平','负')
                              AND pred_had_probs IS NOT NULL AND pred_had_probs != ''
                            ORDER BY verify_date''').fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    out = []
    today = time.strftime('%Y-%m-%d')
    for date_str, probs_s, res in rows:
        p = _parse_probs(probs_s)
        if not p:
            continue
        try:
            age = (time.mktime(time.strptime(today, '%Y-%m-%d'))
                   - time.mktime(time.strptime(date_str, '%Y-%m-%d'))) / 86400.0
        except ValueError:
            age = 0.0
        out.append({'date': date_str, 'probs': p, 'outcome': DIRS.index(res),
                    'w_date': 0.5 ** (max(age, 0.0) / HALF_LIFE_DAYS)})
    return out


def _logit(p):
    p = min(max(p, CLIP_P[0]), CLIP_P[1])
    return math.log(p / (1 - p))


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-max(min(x, 30), -30)))


def _platt_loss(xs, ys, ws, a, b, reg_w=0.3):
    """加权二类交叉熵 + 向恒等(1,0)的ridge罚"""
    ce = 0.0
    for x, y, w in zip(xs, ys, ws):
        p = _sigmoid(a * x + b)
        ce += w * (-(y * math.log(max(p, _EPS)) + (1 - y) * math.log(max(1 - p, _EPS))))
    return ce + reg_w * ((a - 1.0) ** 2 + b ** 2)


def _fit_platt(xs, ys, ws):
    """二类 Platt 参数拟合: 界内网格搜索 (粗0.05→细0.01) 最小化加权交叉熵+ridge

    弃用 Newton-Raphson: 平局类概率分布集中 (x方差极小) 时 Hessian 病态,
    阻尼+参数界下振荡不收敛 (实测低估7pp只修正1pp)。网格搜索确定性收敛,
    与 learn_fusion_weights 同框架。reg_w 主收缩仍由外层 λ 完成, 此处仅防退化。
    """
    def best_at(step, center=None):
        if center:
            a0, b0 = center
            a_rng = [round(a0 + d * step, 4) for d in range(-5, 6)]
            b_rng = [round(b0 + d * step, 4) for d in range(-10, 10)]
        else:
            a_rng = [round(A_RANGE[0] + i * step, 4)
                     for i in range(int((A_RANGE[1] - A_RANGE[0]) / step) + 1)]
            b_rng = [round(B_RANGE[0] + i * step, 4)
                     for i in range(int((B_RANGE[1] - B_RANGE[0]) / step) + 1)]
        best, ba_bb = float('inf'), (1.0, 0.0)
        for a in a_rng:
            if not (A_RANGE[0] - 1e-9 <= a <= A_RANGE[1] + 1e-9):
                continue
            for b in b_rng:
                if not (B_RANGE[0] - 1e-9 <= b <= B_RANGE[1] + 1e-9):
                    continue
                l = _platt_loss(xs, ys, ws, a, b)
                if l < best:
                    best, ba_bb = l, (a, b)
        return ba_bb

    coarse = best_at(0.05)
    fine = best_at(0.01, center=coarse)
    return round(fine[0], 4), round(fine[1], 4)


def _apply_params(probs, params):
    """逐类 Platt + 重归一; params = {类序: (a,b)}"""
    out = []
    for i in range(3):
        a, b = params.get(i, (1.0, 0.0))
        out.append(_sigmoid(a * _logit(probs[i]) + b))
    s = sum(out)
    return [x / s for x in out]


def _eval(rows, params=None):
    """加权对数损失 + Brier + argmax命中率 (params=None 即原始概率)"""
    ll = bs = hit = 0.0
    wsum = 0.0
    for r in rows:
        p = r['probs'] if params is None else _apply_params(r['probs'], params)
        ll += r['w_date'] * (-math.log(max(p[r['outcome']], _EPS)))
        bs += r['w_date'] * sum((p[i] - (1.0 if i == r['outcome'] else 0.0)) ** 2 for i in range(3))
        if p.index(max(p)) == r['outcome']:
            hit += 1
        wsum += r['w_date']
    n = len(rows)
    return {'log_loss': round(ll / wsum, 4), 'brier': round(bs / wsum / 3, 4),
            'hit_rate': round(hit / n, 4) if n else 0.0}


def _reliability(rows, params=None, n_bins=5):
    """逐类可靠性分箱: bin → {n, mean_pred, obs_freq}"""
    bins = {c: [None] * n_bins for c in range(3)}
    for c in range(3):
        acc = [[] for _ in range(n_bins)]
        for r in rows:
            p = r['probs'] if params is None else _apply_params(r['probs'], params)
            bi = min(int(p[c] * n_bins), n_bins - 1)
            acc[bi].append((p[c], 1.0 if r['outcome'] == c else 0.0))
        bins[c] = [{'n': len(g),
                    'pred': round(sum(x[0] for x in g) / len(g), 3) if g else None,
                    'obs': round(sum(x[1] for x in g) / len(g), 3) if g else None}
                   for g in acc]
    return bins


def learn(min_n=MIN_N, full_n=FULL_N):
    rows = load_rows()
    n = len(rows)
    result = {'version': 'ultra15.0-l3', 'trained_at': time.strftime('%Y-%m-%d %H:%M:%S'),
              'n_matches': n, 'min_n': min_n, 'full_n': full_n,
              'half_life_days': HALF_LIFE_DAYS}
    if n == 0:
        result.update({'applied': False, 'reason': 'no_data', 'progress': f"0/{min_n}"})
        _write(result)
        return result
    result['sample_range'] = f"{rows[0]['date']}~{rows[-1]['date']}"
    result['metrics_raw'] = _eval(rows, None)
    result['reliability_raw'] = _reliability(rows, None)

    if n < min_n:
        result.update({'applied': False, 'reason': 'cold_start',
                       'progress': f"{n}/{min_n}",
                       'note': f"冷启动积累中 {n}/{min_n}, 未校准概率直接输出"})
        _write(result)
        return result

    # ① 逐类 Platt 拟合 (二类目标: 结果是否为该类)
    fitted = {}
    for c in range(3):
        xs = [_logit(r['probs'][c]) for r in rows]
        ys = [1.0 if r['outcome'] == c else 0.0 for r in rows]
        ws = [r['w_date'] for r in rows]
        fitted[c] = _fit_platt(xs, ys, ws)

    # ② 收缩: λ=(n-min_n)/(full_n-min_n), 参数向恒等(1,0)插值
    lam = max(0.0, min(1.0, (n - min_n) / max(full_n - min_n, 1)))
    final = {c: (round(lam * fitted[c][0] + (1 - lam) * 1.0, 4),
                 round(lam * fitted[c][1] + (1 - lam) * 0.0, 4)) for c in range(3)}
    if lam < 1.0 and all(abs(final[c][0] - 1.0) < 1e-4 and abs(final[c][1]) < 1e-4 for c in range(3)):
        final = fitted  # 收缩后仍近恒等 → 用拟合值检验是否有真实增益

    # ③ 增益检验
    m_raw, m_cal = _eval(rows, None), _eval(rows, final)
    result['metrics_cal'] = m_cal
    result['params_fitted'] = {DIRS[c]: fitted[c] for c in range(3)}
    if m_raw['log_loss'] - m_cal['log_loss'] < MIN_GAIN:
        result.update({'applied': False, 'reason': 'no_gain',
                       'params': {DIRS[c]: final[c] for c in range(3)},
                       'note': f"LL增益{m_raw['log_loss']-m_cal['log_loss']:.4f}<{MIN_GAIN}, 保持原始概率"})
        _write(result)
        return result

    result.update({'applied': True, 'lambda': round(lam, 3),
                   'params': {DIRS[c]: final[c] for c in range(3)},
                   'reliability_cal': _reliability(rows, final),
                   'note': f"λ={lam:.2f}, LL {m_raw['log_loss']}→{m_cal['log_loss']}, "
                           f"Brier {m_raw['brier']}→{m_cal['brier']}"})
    _write(result)
    return result


def _write(result):
    tmp = OUT_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_PATH)


def retrain():
    """v215_verify 验证后自动调用 — 返回一句话摘要 (None=静默)"""
    r = learn()
    if r.get('reason') == 'no_data':
        return None
    if r.get('applied'):
        p = r['params']
        return (f"概率校准已产出 (n={r['n_matches']}, λ={r.get('lambda')}): "
                f"胜a{p['胜'][0]}/b{p['胜'][1]:+}, 平a{p['平'][0]}/b{p['平'][1]:+}, "
                f"负a{p['负'][0]}/b{p['负'][1]:+}")
    if r.get('reason') == 'cold_start':
        return f"概率校准冷启动 {r.get('progress')}"
    return f"概率校准保持原始 ({r.get('reason')})"


def _print_report(r):
    print(f"[L3 概率校准] n={r['n_matches']}"
          + (f" ({r.get('sample_range')})" if r.get('sample_range') else ''))
    if r.get('metrics_raw'):
        m = r['metrics_raw']
        print(f"  原始概率: LL {m['log_loss']} Brier {m['brier']} 命中 {m['hit_rate']:.1%}")
    # 平局类可靠性摘要 (三源融合典型病灶)
    rel = r.get('reliability_raw')
    if rel:
        used = [(b['pred'], b['obs'], b['n']) for b in rel[1] if b['n'] >= 5]
        if used:
            line = ' | '.join(f"p{p:.2f}→{o:.2f}(n={n})" for p, o, n in used)
            print(f"  平局可靠性: {line}")
    if r.get('metrics_cal'):
        m = r['metrics_cal']
        print(f"  校准概率: LL {m['log_loss']} Brier {m['brier']} 命中 {m['hit_rate']:.1%}")
    if r.get('applied'):
        p = r['params']
        print(f"  ✅ 已启用 (λ={r['lambda']}): 胜(a={p['胜'][0]},b={p['胜'][1]:+}) "
              f"平(a={p['平'][0]},b={p['平'][1]:+}) 负(a={p['负'][0]},b={p['负'][1]:+})")
    else:
        print(f"  ⏸ 未启用 ({r.get('reason')}): {r.get('note', '')}"
              + (f" 进度{r.get('progress')}" if r.get('progress') else ''))


def main():
    import argparse
    ap = argparse.ArgumentParser(description='独立模式概率校准 (进化三层·L3)')
    ap.add_argument('--min-n', type=int, default=MIN_N, help='冷启动最低样本 (默认100)')
    ap.add_argument('--full-n', type=int, default=FULL_N, help='完全信任样本量 (默认200)')
    args = ap.parse_args()
    _print_report(learn(min_n=args.min_n, full_n=args.full_n))


if __name__ == '__main__':
    main()
