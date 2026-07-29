# -*- coding: utf-8 -*-
"""
deep_analysis.py — 历史数据深度利用 (任务1/2/3/5, 一切为了命中率)

任务1: 各联赛球队攻防强度评分 (大样本λ先验)  → predictions/team_ratings.json
任务2: 半全场实测联合分布 (compute_half_full校准) → predictions/half_full_empirical.json
任务3: 市场终赔隐含分布 vs 实际分布 (CRS/TTG/HAFU定价偏差)
任务5: regression.db 分联赛/置信度/赔率段 ECE 与命中率

输出报告: predictions/deep_analysis.md
"""
import os, json, sqlite3
from collections import defaultdict

_WS = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
HDB = os.path.join(_WS, 'predictions', 'sporttery_history.db')
RDB = os.path.join(_WS, 'predictions', 'regression.db')
OUT_MD = os.path.join(_WS, 'predictions', 'deep_analysis.md')
OUT_TR = os.path.join(_WS, 'predictions', 'team_ratings.json')
OUT_HF = os.path.join(_WS, 'predictions', 'half_full_empirical.json')

LEAGUES = ['英超', '瑞超', '挪超', '芬超', '韩职', '巴甲']
SHRINK_K = 5  # 小样本收缩系数

def load_matches():
    conn = sqlite3.connect(HDB)
    ms = []
    for lg, home, away, sc, hsc in conn.execute(
            "SELECT league,home,away,score,halfScore FROM matches WHERE score IS NOT NULL"):
        try:
            hg, ag = (int(x) for x in sc.split(':'))
            hh, ah = (int(x) for x in hsc.split(':')) if hsc else (None, None)
        except Exception:
            continue
        ms.append(dict(league=lg, home=home, away=away, hg=hg, ag=ag, hh=hh, ah=ah))
    conn.close()
    return ms

def wdl(g1, g2):
    return 'H' if g1 > g2 else ('D' if g1 == g2 else 'A')

# ---------- 任务1: 球队攻防强度 ----------
def task1(ms, L):
    L.append("\n## 任务1: 联赛球队攻防强度评分 (大样本λ先验)\n")
    ratings = {}
    for lg in LEAGUES:
        games = [m for m in ms if m['league'] == lg]
        if len(games) < 15:
            continue
        lh = sum(m['hg'] for m in games) / len(games)   # 联赛场均主队进球
        la = sum(m['ag'] for m in games) / len(games)
        teams = defaultdict(lambda: [0, 0, 0, 0, 0, 0, 0, 0])
        #            [主场次,主进,主失, 客场次,客进,客失, 总场次, 总失球(未用)]
        for m in games:
            t = teams[m['home']]
            t[0] += 1; t[1] += m['hg']; t[2] += m['ag']
            t = teams[m['away']]
            t[3] += 1; t[4] += m['ag']; t[5] += m['hg']
        lg_rat = {'league_avg_home_goals': round(lh, 3), 'league_avg_away_goals': round(la, 3),
                  'matches': len(games), 'teams': {}}
        for name, t in teams.items():
            hn, hgs, hgc, an, ags, agc = t[0], t[1], t[2], t[3], t[4], t[5]
            if hn + an < 3:
                continue
            # 收缩: 实测比例 vs 1.0 按样本量加权
            def shr(raw, n):
                return round((raw * n + 1.0 * SHRINK_K) / (n + SHRINK_K), 3)
            lg_rat['teams'][name] = {
                'home_attack': shr(hgs / hn / lh, hn) if hn else None,
                'home_defense': shr(hgc / hn / la, hn) if hn else None,
                'away_attack': shr(ags / an / la, an) if an else None,
                'away_defense': shr(agc / an / lh, an) if an else None,
                'games': hn + an,
            }
        ratings[lg] = lg_rat
        top = sorted(lg_rat['teams'].items(), key=lambda kv: -(kv[1]['home_attack'] or 0))[:3]
        L.append(f"**{lg}** ({len(games)}场, 场均 {lh:.2f}/{la:.2f}): 主场进攻TOP3: " +
                 ", ".join(f"{n} {v['home_attack']}" for n, v in top))
    with open(OUT_TR, 'w', encoding='utf-8') as f:
        json.dump(ratings, f, ensure_ascii=False, indent=1)
    L.append(f"\n评分口径: 1.0=联赛平均, >1进攻强/防守差; 小样本按k={SHRINK_K}收缩。已存 `{OUT_TR}`")
    L.append("用途: λ先验 = 联赛均值 × 主队主场进攻 × 客队客场防守, 与现有nowscore近况λ融合(建议权重0.4-0.5)")
    return ratings

# ---------- 任务2: 半全场实测分布 ----------
def task2(ms, L):
    L.append("\n\n## 任务2: 半全场实测联合分布\n")
    L.append("半场结果→全场结果 转移概率 (每格=该半场结果下全场结果的占比):\n")
    out = {}
    for lg in LEAGUES + ['ALL']:
        games = [m for m in ms if m['hh'] is not None and (lg == 'ALL' or m['league'] == lg)]
        if len(games) < 30:
            continue
        mat = defaultdict(lambda: defaultdict(int))
        cnt = defaultdict(int)
        for m in games:
            hr, fr = wdl(m['hh'], m['ah']), wdl(m['hg'], m['ag'])
            mat[hr][fr] += 1
            cnt[hr] += 1
        dist = {hr: {fr: round(mat[hr][fr] / cnt[hr], 3) for fr in 'HDA'} for hr in 'HDA'}
        out[lg] = {'n': len(games), 'dist': dist, 'half_n': dict(cnt)}
        if lg == 'ALL':
            L.append("| 半场↓ 全场→ | 主胜 | 平 | 客胜 | 样本 |")
            L.append("|---|---|---|---|---|")
            for hr, name in (('H', '半场主胜'), ('D', '半场平'), ('A', '半场客胜')):
                d = dist[hr]
                L.append(f"| {name} | {d['H']:.0%} | {d['D']:.0%} | {d['A']:.0%} | {cnt[hr]} |")
    # 分联赛亮点: 半场平的转化率差异
    L.append("\n半场平→全场主胜 转化率领跑: " +
             ", ".join(f"{lg} {out[lg]['dist']['D']['H']:.0%}" for lg in LEAGUES if lg in out))
    with open(OUT_HF, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    L.append(f"已存 `{OUT_HF}` (含分联赛矩阵), 可校准 compute_half_full 的τ参数与边际重加权")

# ---------- 任务3: 市场终赔隐含 vs 实际 ----------
def task3(L):
    conn = sqlite3.connect(HDB)
    L.append("\n\n## 任务3: 市场终赔隐含分布 vs 实际分布\n")
    # TTG
    ttg_imp = defaultdict(float); ttg_act = defaultdict(int); n = 0
    for mid, sc, cj in conn.execute(
            "SELECT o.matchId, m.score, o.close_json FROM odds o JOIN matches m ON o.matchId=m.matchId WHERE o.pool='TTG'"):
        try:
            tg = sum(int(x) for x in sc.split(':'))
        except Exception:
            continue
        c = json.loads(cj)
        odds = {k: _f(c.get(f's{k}')) for k in range(8)}
        if not all(odds.values()):
            continue
        inv = {k: 1 / v for k, v in odds.items()}
        s = sum(inv.values())
        for k in range(8):
            ttg_imp[k] += inv[k] / s
        ttg_act[min(tg, 7)] += 1
        n += 1
    if n:
        L.append(f"总进球 ({n}场): 定价偏差(实际-隐含)最大的:")
        diffs = sorted(((ttg_act[k] / n - ttg_imp[k] / n, k) for k in range(8)), key=lambda x: -abs(x[0]))[:3]
        for d, k in diffs:
            L.append(f"- {k}{'+' if k==7 else ''}球: 实际 {ttg_act[k]/n:.1%} vs 隐含 {ttg_imp[k]/n:.1%} ({d:+.1%})")
    # HAFU
    KEYS = {'hh': '胜胜', 'hd': '胜平', 'ha': '胜负', 'dh': '平胜', 'dd': '平平',
            'da': '平负', 'ah': '负胜', 'ad': '负平', 'aa': '负负'}
    hf_imp = defaultdict(float); hf_act = defaultdict(int); n = 0
    for mid, sc, hsc, cj in conn.execute(
            "SELECT o.matchId, m.score, m.halfScore, o.close_json FROM odds o JOIN matches m ON o.matchId=m.matchId WHERE o.pool='HAFU'"):
        try:
            hg, ag = (int(x) for x in sc.split(':'))
            hh, ah = (int(x) for x in hsc.split(':'))
        except Exception:
            continue
        act = (wdl(hh, ah) + wdl(hg, ag)).lower()
        c = json.loads(cj)
        odds = {k: _f(c.get(k)) for k in KEYS}
        if not all(odds.values()):
            continue
        inv = {k: 1 / v for k, v in odds.items()}
        s = sum(inv.values())
        for k in KEYS:
            hf_imp[k] += inv[k] / s
        hf_act[act] += 1
        n += 1
    if n:
        L.append(f"\n半全场 ({n}场): 定价偏差TOP4:")
        diffs = sorted(((hf_act[k] / n - hf_imp[k] / n, k) for k in KEYS), key=lambda x: -abs(x[0]))[:4]
        for d, k in diffs:
            L.append(f"- {KEYS[k]}: 实际 {hf_act[k]/n:.1%} vs 隐含 {hf_imp[k]/n:.1%} ({d:+.1%})")
    conn.close()
    L.append("\n用途: 模型比分矩阵可按联赛偏差方向做边际修正, 重点跟踪被市场低估的高频结果")

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

# ---------- 任务5: 验证库分维度ECE ----------
def task5(L):
    if not os.path.exists(RDB):
        L.append("\n\n## 任务5: regression.db 不存在, 跳过")
        return
    conn = sqlite3.connect(RDB)
    rows = conn.execute("""SELECT league, pred_had_p, pred_had_conf, pred_had_odds, had_hit, pred_had_probs
                           FROM verify_history WHERE pred_had_p IS NOT NULL AND had_hit IS NOT NULL""").fetchall()
    conn.close()
    L.append(f"\n\n## 任务5: 模型实战校准 (regression.db, n={len(rows)})\n")
    if len(rows) < 20:
        L.append("样本不足20, 仅列出概览")
    def _p(v):
        """pred_had_p 实际存储为 '32%/29%/38%' 三向串, 取最大值=主推方向概率"""
        if v is None:
            return 0.0
        if isinstance(v, str) and '%' in v:
            try:
                return max(float(x) for x in v.replace('%', '').split('/')) / 100
            except ValueError:
                return 0.0
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 0.0
        return v / 100 if v > 1 else v
    def band(title, key_fn, groups):
        L.append(f"\n{title}:")
        L.append("| 分组 | 样本 | 命中率 | 平均预测P | 偏差 |")
        L.append("|---|---|---|---|---|")
        for name, ok in groups:
            sub = [r for r in rows if ok(r)]
            if len(sub) < 3:
                continue
            hit = sum(r[4] for r in sub) / len(sub)
            pp = sum(_p(r[1]) for r in sub) / len(sub)
            L.append(f"| {name} | {len(sub)} | {hit:.1%} | {pp:.0%} | {hit - pp:+.1%} |")
    band("按联赛", None, [(lg, lambda r, lg=lg: r[0] == lg) for lg in set(r[0] for r in rows)])
    band("按置信度", None, [(f"{s}星", lambda r, s=s: (r[2] or '').count('★') == s) for s in (5, 4, 3, 2, 1)])
    def _o(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    band("按主推赔率段", None, [
        ("<1.4", lambda r: (_o(r[3]) or 9) < 1.4),
        ("1.4–1.8", lambda r: 1.4 <= (_o(r[3]) or 9) < 1.8),
        ("1.8–2.5", lambda r: 1.8 <= (_o(r[3]) or 9) < 2.5),
        ("≥2.5", lambda r: (_o(r[3]) or 0) >= 2.5)])
    L.append("\n用途: 样本破百后, 据此校准 HYBRID_PROB_TOLERANCE 与置信度星级映射")

def main():
    ms = load_matches()
    L = ["# 深度分析报告 (任务1/2/3/5)",
         f"\n历史样本: {len(ms)} 场 | 生成: deep_analysis.py\n"]
    task1(ms, L)
    task2(ms, L)
    task3(L)
    task5(L)
    txt = "\n".join(L) + "\n"
    with open(OUT_MD, 'w', encoding='utf-8') as f:
        f.write(txt)
    print(txt)

if __name__ == '__main__':
    main()
