#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CUSUM 漂移响应生成器 (Ultra 13.5): 从 regression.db 动态计算, 不再硬编码

历史: Ultra 13.4 版硬编码了污染期数值 (42.9%→36.8%, 6.1pp, 漂移点08-10)。
  2026-08-15 跨周污染修复后, 清洁数据的真实漂移为 51.5%→38.2% (13.2pp),
  CUSUM 检出点前移至 08-09。本版全部改为动态计算。

流程:
  1. 从 verify_stats 加载验证批次 → v215_verify.cusum_drift_detection
  2. 按检出漂移点切分, 从 verify_history 计算 漂移前/后命中率 (仅计有预测场次)
  3. 漂移后按预测方向统计失准 (direction_weakness)
  4. 权重乘数随漂移幅度缩放:
       基准响应 (漂移6pp时): market×1.12 / power×0.85 / poisson×1.0 / elo×0.70
       scale = min(1.5, drift_pp / 6.0)   — 上限1.5, 防过度反应
       mult = 1 + (基准-1) × scale        — market上限1.30, power/elo下限0.50
  5. 写 predictions/drift_state.json (v215_e2e.compute_fuse_weights 读取)

未检出漂移时写入 drift_detected=false + 全1.0乘数 (即解除响应)。
"""

import json
import os
import sys
from datetime import datetime

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(WORKSPACE, 'predictions', 'regression.db')
DRIFT_STATE_FILE = os.path.join(WORKSPACE, 'predictions', 'drift_state.json')

sys.path.insert(0, WORKSPACE)
from v215_verify import cusum_drift_detection   # noqa: E402  (口径与主验证一致)

# 基准响应 (漂移 6pp 时的乘数), 按 CUSUM 建议: 降权Power/Elo, 偏向市场
BASE_MULT = {'market': 1.12, 'power': 0.85, 'poisson': 1.00, 'elo': 0.70}
BASE_DRIFT_PP = 6.0      # 基准响应对应的漂移幅度
SCALE_CAP = 1.5          # 缩放上限 (漂移>9pp后不再加码)
MULT_BOUND = (0.50, 1.30)  # 乘数安全边界


def load_stats():
    import sqlite3
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT verify_date, has_pred, had_hits, had_rate
        FROM verify_stats
        WHERE had_rate IS NOT NULL AND has_pred > 0
        ORDER BY verify_date""").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def match_rates(point):
    """按漂移点切分 verify_history, 返回 (前[n,h,rate], 后[n,h,rate]), 仅计有预测场次"""
    import sqlite3
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    out = []
    for op in ('<', '>='):
        n, h = c.execute(f"""
            SELECT COUNT(*), COALESCE(SUM(had_hit), 0)
            FROM verify_history
            WHERE verify_date {op} ? AND had_hit IS NOT NULL""", (point,)).fetchone()
        out.append((n, h, h / n if n else 0))
    conn.close()
    return out[0], out[1]


def direction_weakness(point):
    """漂移后各预测方向命中率"""
    import sqlite3
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
        SELECT pred_had_dir, COUNT(*), SUM(had_hit)
        FROM verify_history
        WHERE verify_date >= ? AND had_hit IS NOT NULL AND pred_had_dir IN ('胜','平','负')
        GROUP BY pred_had_dir""", (point,))
    dw = {}
    for d, n, h in c.fetchall():
        dw[d] = round(h / n, 2) if n else 0.0
    conn.close()
    return dw


def scaled_multipliers(drift_pp):
    # Ultra 13.16: 统一返回 (mult, scale) 元组 — 原版 drift_pp<=0 分支只返回dict导致解包崩溃
    if drift_pp <= 0:
        return {k: 1.0 for k in BASE_MULT}, 0.0
    scale = min(SCALE_CAP, drift_pp / BASE_DRIFT_PP)
    mult = {}
    for k, base in BASE_MULT.items():
        v = 1 + (base - 1) * scale
        v = max(MULT_BOUND[0], min(MULT_BOUND[1], v))
        mult[k] = round(v, 2)
    return mult, round(scale, 2)


def main():
    stats = load_stats()
    print(f'[漂移响应] 验证批次 {len(stats)} 个, 场次 {sum(s["has_pred"] for s in stats)}')

    # ===== 1. CUSUM 检测 =====
    result = cusum_drift_detection(stats, target_rate=0.5, threshold=3.0)
    detected = bool(result['drift_detected'])
    point = result['drift_point'] or ''
    print(f'  CUSUM: pos={result["cusum_pos"]:.3f} 漂移={"是" if detected else "否"} '
          f'{result["drift_direction"]} @ {point or "-"}')

    if not detected or not point:
        state = {
            'drift_detected': False,
            'weight_multipliers': {k: 1.0 for k in BASE_MULT},
            'activated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'source': 'v215_verify.py CUSUM (Ultra 8.0)',
            'note': f'CUSUM未检出漂移 ({result["interpretation"][:60]}), 响应解除',
        }
        with open(DRIFT_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print('[漂移响应] 未检出漂移 → 已写入全1.0乘数 (响应解除)')
        return

    # ===== 2. 漂移前后命中率 (清洁数据) =====
    (pre_n, pre_h, pre_r), (post_n, post_h, post_r) = match_rates(point)
    drift_pp = (pre_r - post_r) * 100
    print(f'  漂移前(<{point}): {pre_h}/{pre_n} = {pre_r*100:.1f}%')
    print(f'  漂移后(≥{point}): {post_h}/{post_n} = {post_r*100:.1f}%')
    print(f'  漂移幅度: {drift_pp:+.1f}pp')

    # ===== Ultra 13.16: 实测无退化护栏 =====
    # CUSUM是累积量, 检出点可能被早期亏损主导; 补录清洁赛果后若漂移后命中率
    # 并未低于漂移前(drift_pp<=0, 如08-16补录后39.5%→40.3%), 说明无实际退化,
    # 不应再对Power/Elo降权 — 写入全1.0乘数解除响应, 只保留检出记录供追溯。
    MIN_DRIFT_PP = 2.0   # 实测退化<2pp视为噪音, 不触发权重响应
    if drift_pp < MIN_DRIFT_PP:
        state = {
            'drift_detected': False,
            'cusum_flagged': True,
            'cusum_drift_point': point,
            'weight_multipliers': {k: 1.0 for k in BASE_MULT},
            'measured_drift_pp': round(drift_pp, 1),
            'pre_hit_rate': round(pre_r, 3),
            'post_hit_rate': round(post_r, 3),
            'activated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'source': 'v215_verify.py CUSUM (Ultra 8.0)',
            'note': (f'CUSUM累积量于{point}越限, 但实测漂移{drift_pp:+.1f}pp<{MIN_DRIFT_PP}pp'
                     f'(补录清洁赛果后漂移后{post_r*100:.1f}% vs 漂移前{pre_r*100:.1f}%), '
                     f'无实际退化 → 权重响应解除(全1.0)'),
        }
        with open(DRIFT_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print(f'[漂移响应] 实测漂移{drift_pp:+.1f}pp<{MIN_DRIFT_PP}pp(无退化) → 已写入全1.0乘数 (响应解除)')
        return

    # ===== 3. 方向失准 =====
    dw = direction_weakness(point)
    print(f'  漂移后方向命中: {dw}')

    # ===== 4. 权重乘数 (随漂移幅度缩放) =====
    mult, scale = scaled_multipliers(drift_pp)
    print(f'  响应缩放: {scale}× (基准{BASE_DRIFT_PP}pp, 实际{drift_pp:.1f}pp, 上限{SCALE_CAP})')

    state = {
        'drift_detected': True,
        'drift_direction': result['drift_direction'],
        'drift_point': point,

        'baseline_hit_rate': round(pre_r, 3),
        'recent_hit_rate': round(post_r, 3),
        'drift_magnitude_pp': round(drift_pp, 1),

        'weight_multipliers': mult,
        'multiplier_basis': {
            'base': BASE_MULT,
            'base_drift_pp': BASE_DRIFT_PP,
            'scale': scale,
            'rule': 'mult = 1 + (base-1) × min(1.5, drift_pp/6.0), 边界[0.50,1.30]',
        },

        'direction_weakness': dw,

        'cusum_pos': result['cusum_pos'],
        'early_warning': result.get('early_warning', False),
        'consecutive_low': result.get('consecutive_low', False),

        'activated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'v215_verify.py CUSUM (Ultra 8.0) + regression.db 清洁数据 (跨周污染修复后)',
        'note': (f'CUSUM漂移响应: 漂移{drift_pp:.1f}pp → '
                 f'市场×{mult["market"]}/Power×{mult["power"]}/Elo×{mult["elo"]} '
                 f'(缩放{scale}×); 配合 recalibrate_model.py 同步重标定'),
    }

    with open(DRIFT_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f'[漂移响应] 已写入 {DRIFT_STATE_FILE}')
    print(f'  乘数: ' + '  '.join(f'{k}×{v}' for k, v in mult.items()))


if __name__ == '__main__':
    main()
