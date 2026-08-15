#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CUSUM 漂移复检脚本 (Ultra 13.5)

用途: 漂移重标定 (2026-08-15 02:21) 生效后, 定期复检模型是否恢复,
      并决定维持/解除 drift_state.json 的漂移响应。

数据源: predictions/regression.db (verify_stats + verify_history)
算法:   与 v215_verify.cusum_drift_detection 完全一致 (直接导入, 保证口径统一)

用法:
  python3 cusum_recheck.py            # 只读复检, 打印报告
  python3 cusum_recheck.py --apply    # 复检后将结果写回 drift_state.json

解除漂移响应的条件 (三项全部满足):
  1. 重标定后 (verify_date > 2026-08-14) 已积累 ≥3 个验证批次
  2. 最近3批滚动命中率 ≥ 42% (漂移前基线 42.9% - 1pp 容差)
  3. CUSUM_pos 回落至预警线以下

重标定简介: 2026-08-15 CUSUM 检测到 08-10 起准确率漂移下降, 已执行
  数据源降权 (market×1.12 / power×0.85 / elo×0.70) + 概率校准重标定。
  本脚本监控降权后的恢复情况。
"""
import json
import math
import os
import sqlite3
import sys
from datetime import datetime

WS = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(WS, 'predictions', 'regression.db')
DRIFT_STATE = os.path.join(WS, 'predictions', 'drift_state.json')

sys.path.insert(0, WS)
from v215_verify import cusum_drift_detection   # noqa: E402  (口径与主验证一致)

DRIFT_POINT = '2026-08-10'        # 兜底值; 实际从 drift_state.json 读取 (gen_drift_state 写入)
try:
    with open(DRIFT_STATE, encoding='utf-8') as f:
        _ds = json.load(f)
    if _ds.get('drift_point'):
        DRIFT_POINT = _ds['drift_point']
except Exception:
    pass
RECAL_DATE = '2026-08-14'         # 重标定前最后一个验证批次 (含)
TARGET_RATE = 0.5
THRESHOLD = 3.0
RELEASE_MIN_BATCHES = 3           # 解除所需的重标定后批次数
RELEASE_RATE = 42.0               # 最近3批滚动命中率门槛 (基线42.9%-1pp)


def load_stats():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT verify_date, total, has_pred, had_hits, had_rate, hhad_hits,
               guide_bets, guide_hits, primary_bets, primary_hits
        FROM verify_stats
        WHERE had_rate IS NOT NULL AND has_pred > 0
        ORDER BY verify_date""").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def running_cusum_table(stats):
    """逐批 CUSUM 轨迹 (镜像 cusum_drift_detection 公式, 仅用于展示)"""
    rates = [s['had_rate'] / 100 for s in stats]
    mean_r = sum(rates) / len(rates)
    sigma = math.sqrt(sum((r - mean_r) ** 2 for r in rates) / len(rates)) if len(rates) >= 2 else 0.1
    sigma = max(sigma, 0.05)
    cp, cn = 0.0, 0.0
    table = []
    for s in stats:
        dev = TARGET_RATE - (s['had_rate'] or 0) / 100
        cp = max(0, cp + dev)
        cn = min(0, cn + dev)
        table.append({'date': s['verify_date'], 'rate': s['had_rate'],
                      'n': s['has_pred'], 'cusum_pos': round(cp, 3), 'cusum_neg': round(cn, 3)})
    return table, sigma, THRESHOLD * sigma


def window_split(point):
    """逐场口径 (与 gen_drift_state.match_rates 一致): 覆盖全部有预测场次,
    比 verify_stats 批次口径更完整 (批次表可能缺行/滞后)"""
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    out = []
    for op in ('<', '>='):
        n, h = c.execute(f"""
            SELECT COUNT(*), COALESCE(SUM(had_hit), 0)
            FROM verify_history
            WHERE verify_date {op} ? AND had_hit IS NOT NULL""", (point,)).fetchone()
        out.append((n, h, h / n * 100 if n else 0))
    conn.close()
    return out[0], out[1]


def main():
    apply = '--apply' in sys.argv

    stats = load_stats()
    print(f'[CUSUM复检] 验证批次: {len(stats)} 个 '
          f'({stats[0]["verify_date"]} ~ {stats[-1]["verify_date"]}), 场次合计 '
          f'{sum(s["has_pred"] for s in stats)}')

    # ===== 1. CUSUM 总检 =====
    result = cusum_drift_detection(stats, target_rate=TARGET_RATE, threshold=THRESHOLD)
    table, sigma, limit = running_cusum_table(stats)
    print(f'\n===== 1. CUSUM 状态 =====')
    print(f'sigma={sigma:.3f} 控制限={limit:.3f} 预警线={limit*0.5:.3f}')
    print(f'CUSUM_pos={result["cusum_pos"]:.3f}  CUSUM_neg={result["cusum_neg"]:.3f}')
    print(f'漂移: {"是" if result["drift_detected"] else "否"} '
          f'{result["drift_direction"]} @ {result["drift_point"] or "-"}')
    print(f'解读: {result["interpretation"]}')

    print(f'\n{"日期":12s} {"命中率":>7s} {"n":>4s} {"CUSUM+":>8s} {"CUSUM-":>8s}  阶段')
    for t in table:
        phase = ('漂移前' if t['date'] < DRIFT_POINT else
                 '漂移后·重标定前' if t['date'] <= RECAL_DATE else '重标定后')
        flag = ' ←当前' if t['date'] == table[-1]['date'] else ''
        print(f'{t["date"]:12s} {t["rate"]:6.1f}% {t["n"]:4d} {t["cusum_pos"]:8.3f} {t["cusum_neg"]:8.3f}  {phase}{flag}')

    # ===== 2. 漂移前后对比 (清洁数据, 逐场口径) =====
    (pre_n, pre_h, pre_r), (post_n, post_h, post_r) = window_split(DRIFT_POINT)
    print(f'\n===== 2. 漂移前后对比 (跨周污染修复后, 逐场口径) =====')
    print(f'漂移前 (<{DRIFT_POINT}): {pre_h}/{pre_n} = {pre_r:.1f}%')
    print(f'漂移后 (≥{DRIFT_POINT}): {post_h}/{post_n} = {post_r:.1f}%')
    print(f'漂移幅度: {pre_r - post_r:+.1f}pp')

    # ===== 3. 重标定后监测与解除判定 =====
    post_recal = [s for s in stats if s['verify_date'] > RECAL_DATE]
    print(f'\n===== 3. 解除判定 =====')
    print(f'重标定后批次: {len(post_recal)}/{RELEASE_MIN_BATCHES}')
    if post_recal:
        rolling = [s for s in stats[-3:]]
        roll_rate = sum(s['had_hits'] for s in rolling) / max(1, sum(s['has_pred'] for s in rolling)) * 100
        print(f'最近3批滚动命中率: {roll_rate:.1f}% (门槛 {RELEASE_RATE}%)')
    else:
        roll_rate = None
        print(f'最近3批滚动命中率: 尚无重标定后数据')

    c1 = len(post_recal) >= RELEASE_MIN_BATCHES
    c2 = roll_rate is not None and roll_rate >= RELEASE_RATE
    c3 = result['cusum_pos'] < limit * 0.5
    print(f'条件1 批次足够:    {"✓" if c1 else "✗"}')
    print(f'条件2 滚动命中率:  {"✓" if c2 else "✗"}')
    print(f'条件3 CUSUM回落:   {"✓" if c3 else "✗"}')

    action = '维持' if not (c1 and c2 and c3) else '解除'
    try:
        with open(DRIFT_STATE, encoding='utf-8') as f:
            _st = json.load(f)
        _m = _st.get('weight_multipliers') or {}
        mult_txt = '/'.join(f'{k}×{v}' for k, v in _m.items())
    except Exception:
        mult_txt = '无'
    print(f'\n>>> 建议: {action}漂移响应 (当前权重乘数 {mult_txt})')

    # ===== 4. 写回 drift_state.json =====
    if apply:
        with open(DRIFT_STATE, encoding='utf-8') as f:
            state = json.load(f)
        state['baseline_hit_rate'] = round(pre_r / 100, 3)
        state['recent_hit_rate'] = round(post_r / 100, 3)
        state['drift_magnitude_pp'] = round(pre_r - post_r, 1)
        state['cusum_pos'] = result['cusum_pos']
        state['cusum_control_limit'] = round(limit, 3)
        state['recheck_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if c1 and c2 and c3:
            state['drift_detected'] = False
            state['released_at'] = state['recheck_at']
            state['note'] = (f"CUSUM复检解除: 恢复{roll_rate:.1f}%≥{RELEASE_RATE}%, "
                             f"CUSUM={result['cusum_pos']:.3f}已回落; 权重乘数停用")
        else:
            state['note'] = (f"CUSUM复检维持: 漂移后{post_r:.1f}% vs 前{pre_r:.1f}%, "
                             f"CUSUM={result['cusum_pos']:.3f}/{limit:.3f}; "
                             f"重标定后批次{len(post_recal)}/{RELEASE_MIN_BATCHES}")
        with open(DRIFT_STATE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print(f'\n[已写回] drift_state.json → {"解除" if not state["drift_detected"] else "维持"}漂移响应')
    else:
        print('\n(只读模式; 加 --apply 可将复检结果写回 drift_state.json)')


if __name__ == '__main__':
    main()
