#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
learn_fusion_weights.py — Ultra 15.0 进化三层·第二层: 独立模式融合权重学习
=====================================================================

用户需求 (2026-08-20):
  "现在三源权重 (xG-Poisson 1.2/0.8、Elo 1.0、H2H 0.10+0.05n) 是启发式定值。
   正确做法: 用带赛果的干净库回放各源独立命中率, 让融合权重 = 各源真实预测力的回归解。"

数据来源 (RULE-016 合规):
  regression.db · verify_history · independent_mode=1 场次
  - src_poisson / src_elo / src_h2h : v215_e2e 预测时存档的逐源概率 ("55.0/28.0/17.0")
  - had_result                      : 实际赛果 (胜/平/负)
  旧四源模式场次永久隔离 (架构不同, 源结构不可比)。

学习方法:
  1. 源回放   : 各源 argmax 方向命中率 + 多项对数损失 (逐源真实预测力)
  2. 权重搜索 : 复刻 v215_e2e.ensemble_fuse 的对数空间几何融合,
                在单纯形上网格搜索 (粗0.05→细0.01) 最小化时间衰减加权对数损失
  3. 收缩护栏 : n<60 不产出 (冷启动); 60→150 线性收缩向先验
                (Bayesian shrinkage 思想: 小样本不信点估计, 信先验)
  4. 增益检验 : 学习版对数损失须优于先验版 ≥ MIN_GAIN, 否则不启用 (防过拟合噪声)

输出:
  predictions/indep_fusion_weights.json
  {applied, weights{poisson,elo,h2h}(归一和=1), lambda, n_matches, metrics, sources, ...}
  v215_e2e 独立分支仅在 applied=true 时加载替代启发式 (冷启动护栏)。

入口:
  CLI   : python3 learn_fusion_weights.py [--min-n 60] [--full-n 150] [--verbose]
  retrain(): v215_verify 每期验证后自动调用 (已挂载), 返回一句话摘要
"""
import json
import os
import sqlite3
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'predictions', 'regression.db')
OUT_PATH = os.path.join(BASE_DIR, 'predictions', 'indep_fusion_weights.json')

# 先验权重 = 当前启发式 (xG-Poisson 1.2 / Elo 1.0 / H2H 典型值0.20), 归一化后参与收缩
PRIOR_RAW = {'poisson': 1.2, 'elo': 1.0, 'h2h': 0.20}
# 冷启动护栏 (与 gen_drift_state 一致的量级): <MIN_N 不学习; FULL_N 起完全信任点估计
MIN_N = 60
FULL_N = 150
# h2h 源参与3单纯形搜索的最低样本 (不足则固定先验占比, 只搜 poisson:elo)
MIN_N_H2H = 20
# 增益门槛: 加权对数损失改善 < 此值 → 不启用 (噪声保护)
MIN_GAIN = 0.002
# 时间衰减半衰期(天): 近期场次权重高, 淘汰阵容/规则变化前的旧特性
HALF_LIFE_DAYS = 90
_EPS = 1e-6  # log 安全下限 (概率0截断)

DIRS = ('胜', '平', '负')


def _parse_src(s):
    """'55.0/28.0/17.0' → [0.55,0.28,0.17] (重归一, Round后和≠1); None/非法 → None"""
    if not s or not isinstance(s, str):
        return None
    try:
        p = [float(x) / 100.0 for x in s.split('/')]
    except (ValueError, ZeroDivisionError):
        return None
    if len(p) != 3 or min(p) <= 0.0 or max(p) >= 1.0:
        return None
    t = sum(p)
    return [x / t for x in p]


def load_rows(min_h2h_valid=False):
    """读取独立模式带赛果场次; 返回 [{date, poisson, elo, h2h|None, outcome_idx, w_date}]"""
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    c = conn.cursor()
    try:
        rows = c.execute('''SELECT verify_date, src_poisson, src_elo, src_h2h, had_result
                            FROM verify_history
                            WHERE independent_mode=1
                              AND had_result IN ('胜','平','负')
                              AND src_poisson IS NOT NULL AND src_elo IS NOT NULL
                            ORDER BY verify_date''').fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    out = []
    today = time.strftime('%Y-%m-%d')
    for date_str, sp, se, sh, res in rows:
        p_p, p_e = _parse_src(sp), _parse_src(se)
        p_h = _parse_src(sh) if sh else None
        if not p_p or not p_e:
            continue
        if min_h2h_valid and not p_h:
            continue
        # 时间衰减: age_days 天前的场次权重 0.5^(age/HALF_LIFE)
        try:
            age = (time.mktime(time.strptime(today, '%Y-%m-%d'))
                   - time.mktime(time.strptime(date_str, '%Y-%m-%d'))) / 86400.0
        except ValueError:
            age = 0.0
        out.append({'date': date_str, 'poisson': p_p, 'elo': p_e, 'h2h': p_h,
                    'outcome': DIRS.index(res), 'w_date': 0.5 ** (max(age, 0.0) / HALF_LIFE_DAYS)})
    return out


def _fuse(probs_list, weights):
    """复刻 ensemble_fuse 对数空间几何融合 (softmax 归一), 供回放复用"""
    import math
    total_w = sum(weights)
    if total_w <= 0:
        return [1 / 3.0] * 3
    log_f = [0.0, 0.0, 0.0]
    for probs, w in zip(probs_list, weights):
        if w <= 0:
            continue
        for i in range(3):
            log_f[i] += (w / total_w) * math.log(max(probs[i], _EPS))
    m = max(log_f)
    ex = [math.exp(x - m) for x in log_f]
    s = sum(ex)
    return [x / s for x in ex]


def _fuse_eval(rows, wp, we, wh):
    """给定权重 → (加权平均对数损失, argmax命中率, n)"""
    import math
    ll_sum, w_sum, hit, n = 0.0, 0.0, 0, 0
    for r in rows:
        srcs, ws = [r['poisson'], r['elo']], [wp, we]
        if r['h2h']:
            srcs.append(r['h2h'])
            ws.append(wh)
        fused = _fuse(srcs, ws)
        ll_sum += r['w_date'] * (-math.log(max(fused[r['outcome']], _EPS)))
        w_sum += r['w_date']
        if fused.index(max(fused)) == r['outcome']:
            hit += 1
        n += 1
    return (ll_sum / w_sum if w_sum else 99.0), (hit / n if n else 0.0), n


def _grid_search(rows, fixed_h2h=None):
    """单纯形网格搜索: 粗 step=0.05 → 细 step=0.01; fixed_h2h 给定时只搜 poisson:elo 一维"""
    def eval_simplex(wp, we, wh):
        return _fuse_eval(rows, wp, we, wh)[0]

    if fixed_h2h is not None:
        # h2h 样本不足: 固定 h2h=先验占比, 只优化 P:E 比例
        best, bw = (99.0, None)
        for k in range(1, 100):  # wp 占比 0.01..0.99
            wp, we = k / 100.0, 1 - k / 100.0
            ll = eval_simplex(wp, we, fixed_h2h)
            if ll < best:
                best, bw = ll, (wp, we, fixed_h2h)
        return bw

    def refine(step, center):
        wp0, we0, wh0 = center
        best, bw = (99.0, None)
        rng = int(round(2 * 0.05 / step))  # 在粗最优±0.05 邻域细化
        for dp in range(-rng, rng + 1):
            for de in range(-rng, rng + 1):
                for dh in range(-rng, rng + 1):
                    wp = round(wp0 + dp * step, 4)
                    we = round(we0 + de * step, 4)
                    wh = round(wh0 + dh * step, 4)
                    if min(wp, we, wh) < 0 or abs(wp + we + wh - 1.0) > 1e-6:
                        continue
                    ll = eval_simplex(wp, we, wh)
                    if ll < best:
                        best, bw = ll, (wp, we, wh)
        return bw

    # 粗搜 (step=0.05 单纯形整数格点)
    best, bw = (99.0, None)
    for a in range(21):
        for b in range(21 - a):
            wp, we, wh = a * 0.05, b * 0.05, round(1.0 - (a + b) * 0.05, 4)
            ll = eval_simplex(wp, we, wh)
            if ll < best:
                best, bw = ll, (wp, we, wh)
    return refine(0.01, bw)


def _normalize(d):
    t = sum(d.values())
    return {k: v / t for k, v in d.items()}


def learn(min_n=MIN_N, full_n=FULL_N, verbose=False):
    """主流程: 回放 → 搜索 → 收缩 → 护栏 → 写 JSON; 返回结果 dict (冷启动时 applied=False)"""
    rows = load_rows()
    n_total = len(rows)
    n_h2h = sum(1 for r in rows if r['h2h'])
    prior = _normalize(PRIOR_RAW)
    result = {
        'version': 'ultra15.0-l2',
        'trained_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'n_matches': n_total, 'n_h2h': n_h2h,
        'min_n': min_n, 'full_n': full_n,
        'prior_weights': {k: round(v, 4) for k, v in prior.items()},
        'half_life_days': HALF_LIFE_DAYS,
    }
    if n_total == 0:
        result.update({'applied': False, 'reason': 'no_data',
                       'progress': f"0/{min_n}"})
        _write(result)
        return result

    dates = (rows[0]['date'], rows[-1]['date'])
    result['sample_range'] = f"{dates[0]}~{dates[1]}"

    # ① 源回放: 各源独立命中率 + 对数损失 (真实预测力)
    src_stats = {}
    for name in ('poisson', 'elo', 'h2h'):
        sub = [r for r in rows if r[name]]
        if not sub:
            continue
        import math
        ll = sum(r['w_date'] * (-math.log(max(r[name][r['outcome']], _EPS))) for r in sub)
        wsum = sum(r['w_date'] for r in sub)
        hit = sum(1 for r in sub if r[name].index(max(r[name])) == r['outcome'])
        src_stats[name] = {'n': len(sub), 'hit_rate': round(hit / len(sub), 4),
                           'log_loss': round(ll / wsum, 4)}
    result['sources'] = src_stats

    # ② 冷启动护栏: 样本不足不产出学习权重 (启发式继续服役)
    if n_total < min_n:
        result.update({'applied': False, 'reason': 'cold_start',
                       'progress': f"{n_total}/{min_n}",
                       'note': f"冷启动积累中 {n_total}/{min_n}, 启发式权重继续服役"})
        _write(result)
        return result

    # ③ 权重搜索 (h2h 样本不足 → 固定先验占比只搜 P:E)
    fixed_h2h = prior['h2h'] if n_h2h < MIN_N_H2H else None
    w_opt = _grid_search(rows, fixed_h2h=fixed_h2h)
    if w_opt is None:
        result.update({'applied': False, 'reason': 'search_failed'})
        _write(result)
        return result

    # ④ 收缩: λ = (n-min_n)/(full_n-min_n), 学习版 ←先验插值 (小样本向先验收缩)
    lam = max(0.0, min(1.0, (n_total - min_n) / max(full_n - min_n, 1)))
    w_final = tuple(round(lam * w_opt[i] + (1 - lam) * prior[k], 4)
                    for i, k in enumerate(('poisson', 'elo', 'h2h')))
    # 重归一 (插值后和=1, 数值误差兜底)
    s = sum(w_final)
    w_final = tuple(round(x / s, 4) for x in w_final)

    # ⑤ 增益检验: 收缩版必须仍优于先验, 否则保持启发式
    ll_prior = _fuse_eval(rows, prior['poisson'], prior['elo'], prior['h2h'])
    ll_final = _fuse_eval(rows, *w_final)
    ll_opt = _fuse_eval(rows, *w_opt)
    result['metrics'] = {
        'log_loss_prior': round(ll_prior[0], 4),
        'log_loss_learned': round(ll_final[0], 4),
        'log_loss_best_grid': round(ll_opt[0], 4),
        'hit_prior': round(ll_prior[1], 4), 'hit_learned': round(ll_final[1], 4),
        'gain': round(ll_prior[0] - ll_final[0], 4),
    }
    if ll_prior[0] - ll_final[0] < MIN_GAIN:
        result.update({'applied': False, 'reason': 'no_gain',
                       'weights': {k: round(v, 4) for k, v in zip(('poisson', 'elo', 'h2h'), w_final)},
                       'note': f"增益{result['metrics']['gain']:.4f}<{MIN_GAIN}, 疑似噪声, 保持先验"})
        _write(result)
        return result

    result.update({'applied': True, 'lambda': round(lam, 3),
                   'weights': {k: round(v, 4) for k, v in zip(('poisson', 'elo', 'h2h'), w_final)},
                   'note': f"λ={lam:.2f}收缩, LL {ll_prior[0]:.3f}→{ll_final[0]:.3f} "
                           f"(命中 {ll_prior[1]:.1%}→{ll_final[1]:.1%})"})
    _write(result)
    return result


def _write(result):
    tmp = OUT_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_PATH)


def retrain():
    """v215_verify 验证后自动调用入口 — 返回一句话摘要 (None=静默)"""
    r = learn()
    if r.get('reason') == 'no_data':
        return None  # 尚无独立模式场次, 不刷屏
    if r.get('applied'):
        w = r['weights']
        m = r.get('metrics', {})
        return (f"融合权重已学习 (n={r['n_matches']}, λ={r.get('lambda')}): "
                f"P{w['poisson']:.2f}/E{w['elo']:.2f}/H{w['h2h']:.2f}, "
                f"LL {m.get('log_loss_prior')}→{m.get('log_loss_learned')}")
    if r.get('reason') == 'cold_start':
        return f"融合权重冷启动 {r.get('progress')}, 启发式服役"
    return f"融合权重保持先验 ({r.get('reason')}: {r.get('note', '')})"


def _print_report(r):
    print(f"[L2 融合权重学习] n={r['n_matches']}"
          + (f" ({r.get('sample_range')}, h2h源{r['n_h2h']}场)" if r.get('sample_range') else ''))
    for name, s in (r.get('sources') or {}).items():
        print(f"  源回放 {name:8s}: 命中 {s['hit_rate']:.1%} (LL {s['log_loss']}, n={s['n']})")
    m = r.get('metrics')
    if m:
        print(f"  先验融合: LL {m['log_loss_prior']} 命中 {m['hit_prior']:.1%}")
        print(f"  学习融合: LL {m['log_loss_learned']} 命中 {m['hit_learned']:.1%} "
              f"(网格最优 {m['log_loss_best_grid']}, 增益 {m['gain']})")
    if r.get('applied'):
        w = r['weights']
        print(f"  收缩λ={r['lambda']} → 输出权重 poisson {w['poisson']} / elo {w['elo']} / h2h {w['h2h']}")
        print(f"  ✅ 已写入 {os.path.basename(OUT_PATH)} (applied=true, e2e自动加载)")
    else:
        print(f"  ⏸ 未启用 ({r.get('reason')}): {r.get('note', '')}"
              + (f" 进度{r.get('progress')}" if r.get('progress') else ''))


def main():
    import argparse
    ap = argparse.ArgumentParser(description='独立模式融合权重学习 (进化三层·L2)')
    ap.add_argument('--min-n', type=int, default=MIN_N, help='冷启动最低样本 (默认60)')
    ap.add_argument('--full-n', type=int, default=FULL_N, help='完全信任样本量 (默认150)')
    ap.add_argument('--force', action='store_true', help='忽略增益门槛强制写入 (仅调试)')
    args = ap.parse_args()
    r = learn(min_n=args.min_n, full_n=args.full_n)
    if args.force and not r.get('applied') and r.get('metrics'):
        r['applied'] = True
        r['note'] = '[DEBUG强制] ' + r.get('note', '')
        _write(r)
    _print_report(r)


if __name__ == '__main__':
    main()
