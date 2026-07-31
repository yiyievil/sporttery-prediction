# -*- coding: utf-8 -*-
"""P9/P10 融合策略回测
A: 几何融合 + argmax方向一致性 (现状)
B: 混合融合 0.7几何+0.3算术 (P9) + argmax方向
C: 几何融合 + JS散度一致性 (P10)
D: 混合融合 (P9) + JS散度一致性 (P10)
指标: Brier / LogLoss / 准确率 / 平局概率均值(校准) — 与实际平局率24.7%对比
"""
import sqlite3, math, os

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'predictions', 'historical_odds.db')
BASE_W = [1.5, 0.8, 1.2, 1.0]  # market, power, poisson, elo

def geo_fuse(probs_list, weights):
    tw = sum(weights); ws = [w / tw for w in weights]
    log_f = [0.0, 0.0, 0.0]
    for i, p in enumerate(probs_list):
        for j in range(3):
            log_f[j] += ws[i] * math.log(max(p[j], 1e-6))
    mx = max(log_f)
    ev = [math.exp(x - mx) for x in log_f]
    s = sum(ev)
    return [x / s for x in ev]

def arith_fuse(probs_list, weights):
    tw = sum(weights); ws = [w / tw for w in weights]
    out = [sum(ws[i] * p[j] for i, p in enumerate(probs_list)) for j in range(3)]
    s = sum(out)
    return [x / s for x in out]

def dir_agreement(probs_list):
    dirs = [p.index(max(p)) for p in probs_list]
    maj = max(set(dirs), key=dirs.count)
    return dirs.count(maj) / len(dirs)

def js_agreement(probs_list):
    """1 - JS散度 (0-1, 越高越一致)"""
    n = len(probs_list)
    avg = [sum(p[j] for p in probs_list) / n for j in range(3)]
    def kl(a, b):
        return sum(a[j] * math.log(max(a[j], 1e-9) / max(b[j], 1e-9)) for j in range(3))
    js = sum(0.5 * kl(p, avg) + 0.5 * kl(avg, p) for p in probs_list) / n
    return 1.0 - js / math.log(2)  # 归一化到[0,1] (JS上界ln2)

def fuse_variant(probs_list, hybrid, js_mode):
    if js_mode:
        ag = js_agreement(probs_list)
        disagree = ag < 0.7   # JS一致性阈值 (校准后, 与方向一致的<0.5大致对应)
    else:
        ag = dir_agreement(probs_list)
        disagree = ag < 0.5
    w = list(BASE_W)
    if disagree:
        w[0] *= 1.5   # 分歧时偏向市场 (现状逻辑)
    g = geo_fuse(probs_list, w)
    if hybrid:
        a = arith_fuse(probs_list, w)
        return [0.7 * g[j] + 0.3 * a[j] for j in range(3)], ag
    return g, ag

def brier(pred, actual_idx):
    return sum((pred[j] - (1.0 if j == actual_idx else 0.0)) ** 2 for j in range(3))

def logloss(pred, actual_idx):
    return -math.log(max(pred[actual_idx], 1e-9))


def main():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('''SELECT market_p_w,market_p_d,market_p_l, power_p_w,power_p_d,power_p_l,
                        poisson_p_w,poisson_p_d,poisson_p_l, elo_p_w,elo_p_d,elo_p_l, result
                 FROM match_four_source
                 WHERE result IS NOT NULL AND market_p_w IS NOT NULL AND power_p_w IS NOT NULL
                   AND poisson_p_w IS NOT NULL AND elo_p_w IS NOT NULL''')
    rows = c.fetchall()
    conn.close()
    R2I = {'W': 0, 'D': 1, 'L': 2}
    n_draw = sum(1 for r in rows if r[12] == 'D')
    print(f"样本: {len(rows)}场, 实际平局率: {n_draw/len(rows)*100:.1f}%\n")

    variants = [('A 现状(几何+方向)', False, False),
                ('B P9(混合+方向)', True, False),
                ('C P10(几何+JS)', False, True),
                ('D P9+P10(混合+JS)', True, True)]

    print(f"{'变体':<22}{'Brier':>8}{'LogLoss':>9}{'准确率':>8}{'预测平局率':>10}{'市场基准差':>10}")
    for name, hybrid, js_mode in variants:
        tb = tl = 0.0; hit = 0; pd_sum = 0.0
        for r in rows:
            probs = [[r[0],r[1],r[2]],[r[3],r[4],r[5]],[r[6],r[7],r[8]],[r[9],r[10],r[11]]]
            ai = R2I[r[12]]
            fused, _ = fuse_variant(probs, hybrid, js_mode)
            tb += brier(fused, ai); tl += logloss(fused, ai)
            if fused.index(max(fused)) == ai: hit += 1
            pd_sum += fused[1]
        N = len(rows)
        print(f"{name:<22}{tb/N:>8.4f}{tl/N:>9.4f}{hit/N*100:>7.1f}%{pd_sum/N*100:>9.1f}%{(pd_sum/N - n_draw/N)*100:>+9.1f}pp")

    # 市场源单独基准
    tb = tl = 0.0; hit = 0; pd_sum = 0.0
    for r in rows:
        p = [r[0],r[1],r[2]]; ai = R2I[r[12]]
        tb += brier(p, ai); tl += logloss(p, ai)
        if p.index(max(p)) == ai: hit += 1
        pd_sum += p[1]
    N = len(rows)
    print(f"{'市场源单独(基准)':<22}{tb/N:>8.4f}{tl/N:>9.4f}{hit/N*100:>7.1f}%{pd_sum/N*100:>9.1f}%{(pd_sum/N - n_draw/N)*100:>+9.1f}pp")


if __name__ == '__main__':
    main()
