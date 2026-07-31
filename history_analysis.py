# -*- coding: utf-8 -*-
"""
history_analysis.py — 基于 sporttery_history.db 的联赛校准分析

分析内容 (全部基于体彩官方初赔/终赔/赛果):
1. 联赛基础参数: 主胜/平/客胜率, 场均进球, 平局率 (对比 LEAGUE_DRAW_RATE 经验值)
2. 终赔校准: 隐含概率 vs 实际命中率 (分箱 ECE)
3. 初赔→终赔移动信号: 临场降赔方向的实际命中率 vs 不降赔
4. 热门-冷门偏差: 按终赔赔率段统计投注热门方的实际 ROI
输出: predictions/history_analysis.md
"""
import os, sys, json, sqlite3
from collections import defaultdict

_WS = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(_WS, 'predictions', 'sporttery_history.db')
OUT = os.path.join(_WS, 'predictions', 'history_analysis.md')

LEAGUE_ORDER = ['英超', '瑞超', '挪超', '芬超', '韩职', '巴甲']

def load():
    conn = sqlite3.connect(DB)
    rows = {}
    for mid, lg, date, sc, hsc, ch, cd, ca in conn.execute(
            "SELECT matchId,league,matchDate,score,halfScore,close_h,close_d,close_a FROM matches"):
        try:
            hg, ag = (int(x) for x in sc.split(':'))
        except Exception:
            continue
        rows[mid] = dict(league=lg, date=date, hg=hg, ag=ag,
                         close=(ch, cd, ca))
    for mid, pool, ij, cj in conn.execute("SELECT matchId,pool,init_json,close_json FROM odds WHERE pool IN ('HAD','HHAD')"):
        if mid not in rows:
            continue
        i, c = json.loads(ij), json.loads(cj)
        if pool == 'HAD':
            rows[mid]['init'] = (_f(i.get('h')), _f(i.get('d')), _f(i.get('a')))
        else:
            rows[mid]['hh_init'] = (_f(i.get('h')), _f(i.get('d')), _f(i.get('a')), i.get('goalLine'))
            rows[mid]['hh_close'] = (_f(c.get('h')), _f(c.get('d')), _f(c.get('a')), c.get('goalLine'))
    conn.close()
    return rows

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

def result(m):
    if m['hg'] > m['ag']:
        return 0
    if m['hg'] == m['ag']:
        return 1
    return 2

def implied(odds):
    inv = [1.0 / o if o else 0 for o in odds]
    s = sum(inv)
    return [x / s for x in inv] if s else None

def main():
    data = [m for m in load().values() if m.get('init') and all(m['init']) and all(m['close'])]
    L = []
    L.append("# 竞彩历史数据校准分析报告")
    L.append(f"\n样本: {len(data)} 场 (2025-01 ~ 2026-07, 体彩官方初赔/终赔/赛果)\n")

    # ---------- 1. 联赛基础参数 ----------
    L.append("\n## 一、联赛基础参数 (真实值 vs 模型经验值)\n")
    L.append("| 联赛 | 场次 | 主胜率 | 平局率 | 客胜率 | 场均进球 | 场均主球 | 场均客球 |")
    L.append("|---|---|---|---|---|---|---|---|")
    agg = {}
    for lg in LEAGUE_ORDER:
        ms = [m for m in data if m['league'] == lg]
        if len(ms) < 15:
            continue
        hw = sum(1 for m in ms if result(m) == 0) / len(ms)
        dr = sum(1 for m in ms if result(m) == 1) / len(ms)
        aw = sum(1 for m in ms if result(m) == 2) / len(ms)
        tg = sum(m['hg'] + m['ag'] for m in ms) / len(ms)
        hg = sum(m['hg'] for m in ms) / len(ms)
        ag = sum(m['ag'] for m in ms) / len(ms)
        agg[lg] = (hw, dr, aw, tg)
        L.append(f"| {lg} | {len(ms)} | {hw:.1%} | **{dr:.1%}** | {aw:.1%} | {tg:.2f} | {hg:.2f} | {ag:.2f} |")
    allms = data
    hw = sum(1 for m in allms if result(m) == 0) / len(allms)
    dr = sum(1 for m in allms if result(m) == 1) / len(allms)
    aw = sum(1 for m in allms if result(m) == 2) / len(allms)
    L.append(f"| **全部** | {len(allms)} | {hw:.1%} | **{dr:.1%}** | {aw:.1%} | {sum(m['hg']+m['ag'] for m in allms)/len(allms):.2f} | | |")

    # ---------- 2. 终赔校准 (隐含概率分箱) ----------
    L.append("\n## 二、终赔隐含概率校准 (市场本身有多准)\n")
    L.append("终赔归一化隐含概率分箱 vs 实际命中率：\n")
    L.append("| 隐含概率区间 | 样本 | 实际命中率 | 偏差 |")
    L.append("|---|---|---|---|")
    bins = [(0.2, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 1.0)]
    for lo, hi in bins:
        n = hit = 0
        for m in allms:
            p = implied(m['close'])
            if not p:
                continue
            r = result(m)
            for k in range(3):
                if lo <= p[k] < hi:
                    n += 1
                    hit += (r == k)
        if n:
            mid = (lo + hi) / 2
            L.append(f"| {lo:.0%}–{hi:.0%} | {n} | {hit/n:.1%} | {hit/n - mid:+.1%} |")

    # ---------- 3. 初赔→终赔移动信号 ----------
    L.append("\n## 三、初赔→终赔移动信号 (临场降赔方向的实际命中率)\n")
    L.append("规则: 某方向终赔较初赔下降≥2% 记为'降赔'(资金涌入)。仅统计单一方向降赔的场次。\n")
    L.append("| 联赛 | 降赔场次 | 降赔方向命中率 | 隐含概率(终) | 超额 |")
    L.append("|---|---|---|---|---|")
    for lg in LEAGUE_ORDER + [None]:
        ms = [m for m in allms if m['league'] == lg] if lg else allms
        n = hit = imps = 0
        imp_sum = 0.0
        for m in ms:
            pi, pc = implied(m['init']), implied(m['close'])
            if not pi or not pc:
                continue
            moves = [(pc[k] - pi[k]) for k in range(3)]
            drops = [k for k in range(3) if m['close'][k] <= m['init'][k] * 0.98]
            if len(drops) != 1:
                continue
            k = drops[0]
            n += 1
            hit += (result(m) == k)
            imp_sum += pc[k]
        if n >= 20:
            L.append(f"| {lg or '**全部**'} | {n} | {hit/n:.1%} | {imp_sum/n:.1%} | {hit/n - imp_sum/n:+.1%} |")

    # ---------- 4. 热门-冷门偏差 ----------
    L.append("\n## 四、热门-冷门偏差 (每个终赔赔率的实际回报)\n")
    L.append("按最低赔率(热门方)分段, 统计买热门方的实际命中率与 ROI (返奖率口径):\n")
    L.append("| 热门赔率段 | 样本 | 热门命中率 | 隐含概率 | 热门ROI |")
    L.append("|---|---|---|---|---|")
    for lo, hi in [(1.0, 1.3), (1.3, 1.5), (1.5, 1.8), (1.8, 2.2), (2.2, 3.0), (3.0, 99)]:
        n = hit = ret = 0
        imp_sum = 0.0
        for m in allms:
            fav = min(range(3), key=lambda k: m['close'][k])
            if not (lo <= m['close'][fav] < hi):
                continue
            p = implied(m['close'])
            n += 1
            won = result(m) == fav
            hit += won
            ret += (m['close'][fav] if won else 0) - 1
            imp_sum += p[fav]
        if n:
            L.append(f"| {lo:.1f}–{hi:.1f} | {n} | {hit/n:.1%} | {imp_sum/n:.1%} | {ret/n:+.1%} |")

    # ---------- 5. 平局率 vs 大小球环境 ----------
    L.append("\n## 五、平局率与环境 (模型平局校准参考)\n")
    L.append("| 总进球区间 | 样本 | 平局率 |")
    L.append("|---|---|---|")
    for lo, hi in [(0, 1), (2, 2), (3, 3), (4, 4), (5, 99)]:
        ms = [m for m in allms if lo <= m['hg'] + m['ag'] <= hi]
        if ms:
            dr = sum(1 for m in ms if result(m) == 1) / len(ms)
            L.append(f"| {lo}–{hi if hi<99 else '∞'}球 | {len(ms)} | {dr:.1%} |")

    txt = "\n".join(L) + "\n"
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write(txt)
    print(txt)
    print(f"已保存: {OUT}")

if __name__ == '__main__':
    main()
