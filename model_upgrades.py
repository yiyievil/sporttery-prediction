#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
model_upgrades.py — 预测模型十项升级 (Ultra 12.0)
================================================
按"命中率第一"原则对七步引擎的逐步升级, 纯标准库实现, 全部可开关可回退。
数据依赖: predictions/sporttery_history.db (赛果+赔率历史, sporttery_history.py 采集)
         predictions/regression.db      (验证回归库, 随验证流程积累)
学习类升级样本不足时自动回退到原逻辑, 并在控制台标注 [升级] 状态。

升级清单:
  1  robust_goal_line      盘口基准: 众数→加权中位数 + 初终盘 Kalman 混合
  2  OddsCalibrator        赔率→概率 isotonic 校准器 (PAV, 历史库训练)
  3  glicko2_form          近况评分: 指数衰减→Glicko-2 (含评分不确定性)
  4  h2h_beta_shrink       对赛战绩: 小样本贝塔-二项收缩
  5  dc_ratio_fit          λ 估计: 完整 Dixon-Coles 攻防强度 (比值法IPF+时间衰减)
  6  bp_matrix             比分矩阵: 独立泊松→二元泊松 (共同冲击λ3)
  7  learn_fusion_weights  融合权重: 静态→按历史Brier网格学习 (stacking)
  8  hhad_from_matrix      HHAD 与比分同源: 从统一矩阵求和, 消除多口径漂移
  9  DrawWindowModel       平局窗口: 硬阈值30%→logistic 概率化
  10 ConfidenceCalibrator  置信度: Δ→星级排序代理→ECE isotonic 校准
"""

import json
import math
import os
import sqlite3
from datetime import datetime

_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
HISTORY_DB = os.path.join(_WORKSPACE, 'predictions', 'sporttery_history.db')
REGRESSION_DB = os.path.join(_WORKSPACE, 'predictions', 'regression.db')
PARAMS_PATH = os.path.join(_WORKSPACE, 'predictions', 'model_upgrades_params.json')

# 学习类升级的最小样本量 (不足则回退原逻辑)
MIN_SAMPLES = {'odds_cal': 150, 'fusion_weights': 50, 'draw_window': 40, 'conf_ece': 40, 'dc_fit': 120}


def _log(msg):
    print(f"  [升级] {msg}")


def _load_params():
    try:
        with open(PARAMS_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_params(params):
    os.makedirs(os.path.dirname(PARAMS_PATH), exist_ok=True)
    tmp = PARAMS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(params, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PARAMS_PATH)


# ======================================================================
# 升级1: 盘口基准 — 加权中位数 + Kalman 初终盘混合
# ======================================================================
def robust_goal_line(lines, weights=None):
    """盘口列表 → 加权中位数 (替代众数, 对离群盘口稳健)

    lines: [2.25, 2.5, 2.25, ...] 各公司盘口; weights: 公司可信度(缺省等权)
    """
    vals = sorted((l, (weights[i] if weights else 1.0)) for i, l in enumerate(lines) if l)
    if not vals:
        return None
    total = sum(w for _, w in vals)
    acc = 0.0
    for v, w in vals:
        acc += w
        if acc >= total / 2:
            return v
    return vals[-1][0]


def kalman_blend_goal_line(gl_init, gl_now, hours_to_kickoff=12.0):
    """初盘(先验) + 即时盘(观测) 的 Kalman 式混合。
    临场盘口信息量更大: 距开赛越近, 观测权重 K 越大 (0.5→0.85)。"""
    if gl_init is None:
        return gl_now
    if gl_now is None:
        return gl_init
    K = min(0.85, max(0.5, 1.0 - hours_to_kickoff / 48.0))
    return round(gl_init + K * (gl_now - gl_init), 2)


# ======================================================================
# 升级2: 赔率→概率 isotonic 校准器 (PAV 算法)
# ======================================================================
class Isotonic:
    """一维保序回归 (Pool Adjacent Violators), 纯Python实现"""

    def __init__(self):
        self.xs, self.ys = [], []

    def fit(self, xs, ys):
        pts = sorted(zip(xs, ys))
        xs_, ys_, ws_ = [p[0] for p in pts], [p[1] for p in pts], [1.0] * len(pts)
        i = 0
        while i < len(ys_) - 1:
            if ys_[i] > ys_[i + 1] + 1e-12:
                w = ws_[i] + ws_[i + 1]
                ys_[i] = (ys_[i] * ws_[i] + ys_[i + 1] * ws_[i + 1]) / w
                ws_[i] = w
                del ys_[i + 1], ws_[i + 1], xs_[i + 1]
                i = max(0, i - 1)
            else:
                i += 1
        self.xs, self.ys = xs_, ys_
        return self

    def predict(self, x):
        if not self.xs:
            return x
        if x <= self.xs[0]:
            return self.ys[0]
        if x >= self.xs[-1]:
            return self.ys[-1]
        for i in range(len(self.xs) - 1):
            if self.xs[i] <= x <= self.xs[i + 1]:
                t = (x - self.xs[i]) / (self.xs[i + 1] - self.xs[i] + 1e-12)
                return self.ys[i] + t * (self.ys[i + 1] - self.ys[i])
        return x


def fit_odds_calibrator(db_path=HISTORY_DB, min_samples=None):
    """从历史库学习 隐含概率→真实频率 的 isotonic 校准 (按胜/平/负分别拟合)。

    数据源: matches.close_h/d/a (终赔) + winFlag (H/D/A 赛果)
    返回 {'h': Isotonic, 'd': Isotonic, 'a': Isotonic, 'n': 样本数} 或 None
    """
    min_samples = min_samples or MIN_SAMPLES['odds_cal']
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT close_h, close_d, close_a, winFlag FROM matches "
            "WHERE odds_fetched=1 AND winFlag IN ('H','D','A') "
            "AND close_h>1 AND close_d>1 AND close_a>1").fetchall()
        conn.close()
    except Exception:
        return None
    if len(rows) < min_samples:
        return None
    buckets = {'H': ([], []), 'D': ([], []), 'A': ([], [])}
    for h, d, a, flag in rows:
        tot = 1 / h + 1 / d + 1 / a
        for cls, odds in (('H', h), ('D', d), ('A', a)):
            buckets[cls][0].append((1 / odds) / tot)          # 去水后隐含概率
            buckets[cls][1].append(1.0 if flag == cls else 0.0)
    out = {'n': len(rows)}
    for cls in 'HDA':
        xs, ys = buckets[cls]
        out[cls.lower()] = Isotonic().fit(xs, ys).__dict__
    return out


def apply_odds_calibrator(probs, calib):
    """对 [w,d,l] 概率逐类校准后重新归一化"""
    if not calib:
        return probs
    out = []
    for i, cls in enumerate('hda'):
        iso = Isotonic()
        iso.xs, iso.ys = calib[cls]['xs'], calib[cls]['ys']
        out.append(iso.predict(probs[i]))
    s = sum(out)
    return [p / s for p in out] if s > 0 else probs


# ======================================================================
# 升级3: Glicko-2 近况评分 (Glickman 标准算法, 修正 v/Δ/μ 尺度 + Illinois 波动率迭代)
# ======================================================================
# 修复说明 (2026-08-11): 旧实现存在三处致命错误
#   1. v 含 q² (应无) → v 放大 1/q²≈3万倍 → 1/v≈0 → rd 永不收缩
#   2. Δ 与 μ 更新含 q (缩放尺度下应无) → 每场 μ 仅移动 ~0.003 → 连胜/连败输出恒 ≈0.5
#   3. Illinois 波动率迭代: B 初始化错用 a±3.0 (应为 ln(Δ²-φ²-v)), 缺 f(A)/2 防停滞修正,
#      存在永真死代码 → 不保证收敛
# 正确公式 (缩放尺度 μ=r/173.7178, φ=RD/173.7178, 173.7178=1/q):
#   v = 1/Σg(φj)²E(1-E);  Δ = v·Σg(φj)(s-E);  φ'=1/√(1/φ*²+1/v);  μ'=μ+φ'²·Σg(φj)(s-E)
_GLICKO_Q = math.log(10) / 400


def _glicko_g(rd):
    """g(φ): 对手不确定性对期望得分的衰减因子 (φ 为缩放尺度)"""
    return 1 / math.sqrt(1 + 3 * _GLICKO_Q ** 2 * rd ** 2 / math.pi ** 2)


def _glicko_e(mu, mu_j, rd_j):
    """E(μ,μj,φj): 对对手 j 的期望得分 (logistic 评分差模型)"""
    return 1 / (1 + math.exp(-_glicko_g(rd_j) * (mu - mu_j)))


def _glicko2_volatility(phi, sigma, delta, v, tau):
    """Glicko-2 step 5: 波动率 σ 迭代 — 标准 Illinois 算法 (含 f(A)/2 防停滞修正)。

    f(x) = e^x·(Δ²-φ²-v-e^x) / (2·(φ²+v+e^x)²) - (x-ln(σ²))/τ²
    括号初始化: Δ²>φ²+v 时 B=ln(Δ²-φ²-v), 否则沿 a-k·τ 向下找 f<0 的界
    """
    a = math.log(sigma ** 2)

    def f(x):
        ex = math.exp(x)
        num = ex * (delta ** 2 - phi ** 2 - v - ex)
        den = 2 * (phi ** 2 + v + ex) ** 2
        return num / den - (x - a) / tau ** 2

    A = a
    if delta ** 2 > phi ** 2 + v:
        B = math.log(delta ** 2 - phi ** 2 - v)
    else:
        k = 1
        while f(a - k * tau) < 0 and k < 100:   # k 上限防死循环
            k += 1
        B = a - k * tau
    fA, fB = f(A), f(B)
    for _ in range(100):
        if abs(B - A) < 1e-6:
            break
        C = A + (A - B) * fA / (fB - fA + 1e-300)  # regula falsi 内插 (防除零)
        fC = f(C)
        if fC * fB < 0:
            A, fA = B, fB          # 根在 (B,C): 区间右移
        else:
            fA = fA / 2.0          # Illinois 修正: 同端滞留时函数值减半, 防停滞
        B, fB = C, fC
    return math.exp(A / 2)


def glicko2_form(results, base_mu=0.0, base_rd=1.2, base_phi=0.06, tau=0.5,
                 opp_scale=3.0, opp_rd=0.6):
    """近况赛果序列 → Glicko-2 评分 (标准算法, 逐场在线更新, 每场一个 mini-period)

    results: [(score, opp_strength)] 按时间从近到远
        score∈{1,0.5,0} (胜/平/负); opp_strength∈[0,1], 0.5=平均对手(未知时缺省),
        越接近 1 对手越强 → 赢强队比赢弱队获得更多提升 (含金量差异)
    返回 (mu, rd, expected_vs_avg):
        mu  缩放评分 (≈评分差/173.72), 正值=近况强于平均
        rd  评分不确定性 ∈(0,1.2], 场数越多越小 → 上游据此调混合权重
        expected_vs_avg  对平均对手(rd=opp_rd)的期望胜率 ∈[0,1], 供近况修正
    """
    mu, phi, sigma = base_mu, base_rd, base_phi
    for score, opp in results:
        mu_j = max(0.0, min(1.0, opp)) - 0.5      # 对手强度 → [-0.5, 0.5]
        mu_j *= opp_scale                          # 缩放尺度评分差 (±1.5 ≈ ±260 分)
        g = _glicko_g(opp_rd)
        e = _glicko_e(mu, mu_j, opp_rd)
        v = 1.0 / (g * g * e * (1 - e))            # step 3 (缩放尺度, 无 q²)
        delta = v * g * (score - e)                # step 4 (无 q)
        sigma = _glicko2_volatility(phi, sigma, delta, v, tau)   # step 5
        phi_star = math.sqrt(phi * phi + sigma * sigma)          # step 6 预周期
        phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)  # step 7
        mu = mu + phi * phi * g * (score - e)      # step 8 (无 q)
    return mu, phi, _glicko_e(mu, 0.0, opp_rd)


# ======================================================================
# 升级4: 对赛战绩贝塔-二项收缩
# ======================================================================
def h2h_beta_shrink(wins, draws, losses, prior_wdl=(0.42, 0.27, 0.31), prior_n=6):
    """小样本对赛战绩 → 后验均值收缩。

    prior_n: 伪样本强度 (越大收缩越强); prior_wdl: 联赛/全局基准
    """
    n = wins + draws + losses
    tot = n + prior_n
    return [(wins + prior_n * prior_wdl[0]) / tot,
            (draws + prior_n * prior_wdl[1]) / tot,
            (losses + prior_n * prior_wdl[2]) / tot]


# ======================================================================
# 升级5: Dixon-Coles 攻防强度 (比值法 IPF + 时间衰减)
# ======================================================================
def dc_ratio_fit(db_path=HISTORY_DB, min_samples=None, decay_xi=0.0045, iters=30):
    """在历史赛果库上拟合各队 attack/defence 强度 (比值迭代法, 无需scipy)。

    attack_i: 相对联赛均值的进攻倍率; defence_i: 失球倍率 (越小防守越好)
    时间衰减: w = exp(-xi * 距今天数); 主客场分开
    返回 {'teams': {name: {'att':float,'def':float}}, 'home_adv':float, 'avg_h':float,'avg_a':float,'n':int}
    """
    min_samples = min_samples or MIN_SAMPLES['dc_fit']
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT matchDate, home, away, score FROM matches "
            "WHERE score IS NOT NULL AND (score LIKE '%:%' OR score LIKE '%-%')").fetchall()
        conn.close()
    except Exception:
        return None
    games = []
    today = datetime.now().date()
    for dt, home, away, score in rows:
        try:
            hs, as_ = score.replace(':', '-').split('-')[:2]
            hs, as_ = int(hs), int(as_)
            days = (today - datetime.strptime(dt, '%Y-%m-%d').date()).days
            games.append((home, away, hs, as_, math.exp(-decay_xi * max(0, days))))
        except Exception:
            continue
    if len(games) < min_samples:
        return None

    wsum = sum(g[4] for g in games)
    avg_h = sum(g[2] * g[4] for g in games) / wsum
    avg_a = sum(g[3] * g[4] for g in games) / wsum
    home_adv = avg_h / max(1e-9, avg_a)  # 主场进球/客场进球 比值作为主场系数参考

    teams = sorted({g[0] for g in games} | {g[1] for g in games})
    att = {t: 1.0 for t in teams}
    dfn = {t: 1.0 for t in teams}
    for _ in range(iters):
        for t in teams:
            scored = conceded = base_s = base_c = 0.0
            for home, away, hs, as_, w in games:
                if home == t:
                    scored += w * hs
                    base_s += w * avg_h * dfn[away]
                    conceded += w * as_
                    base_c += w * avg_a * att[away]
                elif away == t:
                    scored += w * as_
                    base_s += w * avg_a * dfn[home]
                    conceded += w * hs
                    base_c += w * avg_h * att[home]
            if base_s > 0:
                att[t] = scored / base_s
            if base_c > 0:
                dfn[t] = conceded / base_c
        m = sum(att.values()) / len(att)
        att = {t: v / m for t, v in att.items()}

    # 小样本收缩: 出场权重不足的球队向 1.0 (联赛均值) 收缩, 避免极端比值
    played = {t: 0.0 for t in teams}
    for home, away, hs, as_, w in games:
        played[home] += w
        played[away] += w
    K_SHRINK = 3.0  # 约3场加权样本才达到半强度
    for t in teams:
        f = played[t] / (played[t] + K_SHRINK)
        att[t] = 1.0 + (att[t] - 1.0) * f
        dfn[t] = 1.0 + (dfn[t] - 1.0) * f

    return {'teams': {t: {'att': round(att[t], 4), 'def': round(dfn[t], 4)} for t in teams},
            'home_adv_ratio': round(home_adv, 4), 'avg_h': round(avg_h, 4),
            'avg_a': round(avg_a, 4), 'n': len(games),
            'fitted_at': datetime.now().strftime('%Y-%m-%d')}


def dc_lambda(dc, home, away, shrink=0.5):
    """由攻防强度计算 λ主/λ客; 未知球队回退 None (上游用市场λ)。
    shrink: 与市场λ的混合权重由调用方决定, 这里只返回纯DC估计。"""
    if not dc:
        return None
    th, ta = dc['teams'].get(home), dc['teams'].get(away)
    if not th or not ta:
        return None
    lam_h = th['att'] * ta['def'] * dc['avg_h']
    lam_a = ta['att'] * th['def'] * dc['avg_a']
    return round(lam_h, 4), round(lam_a, 4)


# ======================================================================
# 升级6: 二元泊松比分矩阵 (共同冲击 λ3 建模进球相关性)
# ======================================================================
def bp_matrix(lam_h, lam_a, rho=0.12, max_goals=8):
    """二元泊松 P(X=x,Y=y), λ1=λh-λ3, λ2=λa-λ3, λ3=rho*min(λh,λa)

    与独立泊松相比: 同分平局(0-0/1-1)概率上调, 更符合真实足球数据。
    返回 {(h,a): p} 已归一化。
    """
    lam3 = max(0.0, rho * min(lam_h, lam_a))
    l1, l2 = max(1e-6, lam_h - lam3), max(1e-6, lam_a - lam3)

    def pois(k, lam):
        return math.exp(-lam) * lam ** k / math.factorial(k)

    mat = {}
    for x in range(max_goals + 1):
        for y in range(max_goals + 1):
            p = 0.0
            for k in range(min(x, y) + 1):
                # Karlis & Ntzoufras 二元泊松卷积: X=X1+X3, Y=X2+X3 (三者独立)
                # P(X=x,Y=y) = Σ_k P1(x-k)·P2(y-k)·P3(k) — 不含组合数因子
                p += pois(x - k, l1) * pois(y - k, l2) * pois(k, lam3)
            mat[(x, y)] = p * math.exp(-0)  # 保持浮点
    s = sum(mat.values())
    return {k: v / s for k, v in mat.items()} if s > 0 else mat


def matrix_wdl(mat):
    w = sum(p for (h, a), p in mat.items() if h > a)
    d = sum(p for (h, a), p in mat.items() if h == a)
    l = sum(p for (h, a), p in mat.items() if h < a)
    return [w, d, l]


# ======================================================================
# 升级7: 融合权重学习 (网格搜索最小化历史 Brier)
# ======================================================================
def learn_fusion_weights(records, n_sources=4, step=0.1):
    """records: [{'sources': [[w,d,l],...n个源], 'outcome': 0/1/2}]
    在步长 step 的单纯形网格上找 Brier 最小权重。样本不足返回 None。"""
    if len(records) < MIN_SAMPLES['fusion_weights']:
        return None
    import itertools

    def brier(ws):
        tot = 0.0
        for r in records:
            fused = [sum(ws[j] * r['sources'][j][i] for j in range(n_sources)) for i in range(3)]
            s = sum(fused)
            fused = [p / s for p in fused]
            for i in range(3):
                y = 1.0 if r['outcome'] == i else 0.0
                tot += (fused[i] - y) ** 2
        return tot / len(records)

    grid = [i * step for i in range(int(1 / step) + 1)]
    best, best_b = None, 9e9
    for combo in itertools.product(grid, repeat=n_sources - 1):
        if sum(combo) > 1.0:
            continue
        ws = list(combo) + [1.0 - sum(combo)]
        b = brier(ws)
        if b < best_b:
            best, best_b = ws, b
    return {'weights': [round(w, 3) for w in best], 'brier': round(best_b, 4), 'n': len(records)}


# ======================================================================
# 升级8: HHAD 与比分矩阵同源
# ======================================================================
def hhad_from_matrix(mat, goal_line):
    """从统一比分矩阵按让球线求和: 净胜+goalLine>0 让胜 / =0 让平 / <0 让负"""
    w = sum(p for (h, a), p in mat.items() if (h - a) + goal_line > 0)
    d = sum(p for (h, a), p in mat.items() if (h - a) + goal_line == 0)
    l = sum(p for (h, a), p in mat.items() if (h - a) + goal_line < 0)
    s = w + d + l
    return [w / s, d / s, l / s] if s > 0 else [1 / 3] * 3


# ======================================================================
# 升级9: 平局窗口 logistic 模型
# ======================================================================
class DrawWindowModel:
    """输入特征 [P平, Δ, |handicap|, 联赛平局率], 输出 P(HHAD判别优于HAD)。
    样本不足时回退 None → 上游使用硬阈值规则。"""

    def __init__(self):
        self.w = None

    def fit(self, X, Y, lr=0.1, epochs=800, l2=1e-3):
        n, dim = len(X), len(X[0]) + 1
        Xb = [list(x) + [1.0] for x in X]
        w = [0.0] * dim
        for _ in range(epochs):
            grad = [0.0] * dim
            for xi, yi in zip(Xb, Y):
                z = sum(wj * xj for wj, xj in zip(w, xi))
                p = 1 / (1 + math.exp(-max(-30, min(30, z))))
                for j in range(dim):
                    grad[j] += (p - yi) * xi[j]
            for j in range(dim):
                w[j] -= lr * (grad[j] / n + l2 * w[j])
        self.w = w
        return self

    def predict(self, x):
        if not self.w:
            return None
        z = sum(wj * xj for wj, xj in zip(self.w, list(x) + [1.0]))
        return 1 / (1 + math.exp(-max(-30, min(30, z))))


def build_draw_window_samples(db_path=REGRESSION_DB):
    """从回归库构造样本: 特征 + 标签(该场HHAD命中而HAD未命中=1)。
    需要 verify_history 含 pred 概率与双玩法命中标记。"""
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(verify_history)")]
        need = {'pred_had_p', 'had_hit', 'hhad_hit'}
        if not need.issubset(set(cols)):
            conn.close()
            return None
        rows = conn.execute(
            "SELECT pred_had_p, had_hit, hhad_hit FROM verify_history "
            "WHERE had_hit IS NOT NULL AND hhad_hit IS NOT NULL").fetchall()
        conn.close()
    except Exception:
        return None
    X, Y = [], []
    for p_str, had_hit, hhad_hit in rows:
        try:
            parts = [float(x.replace('%', '')) / 100 for x in str(p_str).split('/')]
            if len(parts) != 3:
                continue
            sp = sorted(parts, reverse=True)
            X.append([parts[1], sp[0] - sp[1], 1.0, 0.25])  # handicap/联赛率用均值占位
            Y.append(1.0 if (hhad_hit and not had_hit) else 0.0)
        except Exception:
            continue
    return (X, Y) if len(X) >= MIN_SAMPLES['draw_window'] else None


# ======================================================================
# 升级10: 置信度 ECE 校准 (Δ→实际命中率 isotonic)
# ======================================================================
def fit_confidence_calibrator(db_path=REGRESSION_DB):
    """从 verify_history 学习 Δ(概率差) → 命中率 的 isotonic 映射。
    需要列 pred_had_p (如 '35%/30%/35%') 与 had_hit。不足样本返回 None。"""
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(verify_history)")]
        if not {'pred_had_p', 'had_hit'}.issubset(set(cols)):
            conn.close()
            return None
        rows = conn.execute(
            "SELECT pred_had_p, had_hit FROM verify_history WHERE had_hit IS NOT NULL").fetchall()
        conn.close()
    except Exception:
        return None
    xs, ys = [], []
    for p_str, hit in rows:
        try:
            parts = [float(x.replace('%', '')) / 100 for x in str(p_str).split('/')]
            if len(parts) != 3:
                continue
            sp = sorted(parts, reverse=True)
            xs.append(sp[0] - sp[1])
            ys.append(1.0 if hit else 0.0)
        except Exception:
            continue
    if len(xs) < MIN_SAMPLES['conf_ece']:
        return None
    iso = Isotonic().fit(xs, ys)
    return {'xs': iso.xs, 'ys': iso.ys, 'n': len(xs)}


def calibrated_confidence(delta, calib):
    """Δ → 校准后的期望命中率 (供星级映射/封顶判断)"""
    if not calib:
        return None
    iso = Isotonic()
    iso.xs, iso.ys = calib['xs'], calib['ys']
    return iso.predict(delta)


# ======================================================================
# 统一训练入口 (离线/定时调用)
# ======================================================================
def train_all(verbose=True):
    """重训全部数据驱动参数并保存。样本不足的项自动跳过并保留回退。"""
    params = _load_params()
    calib = fit_odds_calibrator()
    if calib:
        params['odds_calibrator'] = calib
        if verbose:
            _log(f"赔率校准器已训练 (n={calib['n']})")
    dc = dc_ratio_fit()
    if dc:
        params['dc_model'] = dc
        if verbose:
            _log(f"DC攻防强度已拟合 (n={dc['n']}, 球队={len(dc['teams'])})")
    conf = fit_confidence_calibrator()
    if conf:
        params['conf_calibrator'] = conf
        if verbose:
            _log(f"置信度校准器已训练 (n={conf['n']})")
    samples = build_draw_window_samples()
    if samples:
        X, Y = samples
        m = DrawWindowModel().fit(X, Y)
        params['draw_window'] = {'w': m.w, 'n': len(X)}
        if verbose:
            _log(f"平局窗口模型已训练 (n={len(X)})")
    params['trained_at'] = datetime.now().strftime('%Y-%m-%d %H:%M')
    _save_params(params)
    return params


def load_upgrades():
    """预测侧统一加载入口: 返回参数字典 (缺项=回退)"""
    return _load_params()


if __name__ == '__main__':
    p = train_all()
    print(f"参数已保存: {PARAMS_PATH} (keys: {list(p.keys())})")
