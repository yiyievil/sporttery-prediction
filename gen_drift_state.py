#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CUSUM 漂移响应生成器 (Ultra 14.3): 独立模式专属 + 冷启动重新积累

Ultra 14.0 架构切换 (2026-08-20 用户裁决): 独立模式 (赔率零输入) 为唯一预测路径。
原 CUSUM 漂移数据全部来自旧四源融合模式 (含赔率输入) 的验证场次, 其命中率特性
对新架构无代表性 — 旧漂移乘数 (elo×0.88 等) 不应继续作用于新模型。
用户裁决 (2026-08-20): 漂移数据从新模型启用日 (2026-08-20) 起重新积累。

RULE-016 数据隔离: 漂移/CUSUM 只消费 independent_mode=1 的验证场次。
  - verify_history.independent_mode 由 v215_verify.py 入库时按 pred_file 含 '_indep' 写入
  - 历史旧模式场次 (2026-08-20 前) 一律 independent_mode=0, 永不进入本计算

流程:
  1. 从 verify_history 聚合 independent_mode=1 的批次 (verify_stats 无模式列, 弃用)
  2. 冷启动: 独立模式场次 < MIN_SAMPLES → 写中性状态 (全1.0乘数), 注明积累进度
  3. 样本充足后: v215_verify.cusum_drift_detection 检测漂移点
  4. 按检出漂移点切分, 计算漂移前/后命中率 (仅独立模式场次)
  5. 漂移后按预测方向统计失准 (direction_weakness)
  6. 权重乘数随漂移幅度缩放 (独立模式仅两源, 无 market/power):
       基准响应 (漂移6pp时): poisson×1.12 / elo×0.70 — 漂移期偏向 xG-Poisson 主源
       scale = min(1.5, drift_pp / 6.0)   — 上限1.5, 防过度反应
       mult = 1 + (基准-1) × scale        — poisson上限1.30, elo下限0.50
  7. 写 predictions/drift_state.json (v215_e2e._get_drift_multipliers 读取)

未检出漂移/冷启动期写入 drift_detected=false + 全1.0乘数 (即中性状态)。
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

# Ultra 14.3: 独立模式基准响应 (漂移 6pp 时的乘数)
# 旧版含 market×1.12/power×0.85 — 两源在独立模式不存在, 移除。
# 漂移期 (模型失准) 偏向 xG-Poisson 主源 (数据驱动), 降权 Elo (统计推导侧)。
BASE_MULT = {'poisson': 1.12, 'elo': 0.70}
BASE_DRIFT_PP = 6.0      # 基准响应对应的漂移幅度
SCALE_CAP = 1.5          # 缩放上限 (漂移>9pp后不再加码)
MULT_BOUND = (0.50, 1.30)  # 乘数安全边界
MIN_SAMPLES = 60         # 冷启动最低场次: 独立模式验证场次不足时保持中性
INDEP_START = '2026-08-20'   # 独立模式启用日 (数据积累起点)


def load_stats():
    """Ultra 14.3: 从 verify_history 聚合独立模式批次 (RULE-016 数据隔离)

    verify_stats 无模式标记列, 不再使用; 直接按 independent_mode=1 聚合。
    """
    import sqlite3
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT verify_date,
                   COUNT(*) AS has_pred,
                   COALESCE(SUM(had_hit), 0) AS had_hits,
                   CASE WHEN COUNT(had_hit) > 0
                        THEN 100.0 * COALESCE(SUM(had_hit), 0) / COUNT(had_hit)
                        ELSE NULL END AS had_rate
            FROM verify_history
            WHERE independent_mode = 1 AND had_hit IS NOT NULL
            GROUP BY verify_date
            ORDER BY verify_date""").fetchall()
    except Exception:
        rows = []   # 列缺失(极旧库) → 视为无独立模式数据
    conn.close()
    return [dict(r) for r in rows]


def indep_match_count():
    """独立模式累计验证场次 (含 had_hit 为 NULL 的无预测场次不计)"""
    import sqlite3
    conn = sqlite3.connect(DB)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM verify_history WHERE independent_mode=1 AND had_hit IS NOT NULL"
        ).fetchone()[0]
    except Exception:
        n = 0
    conn.close()
    return n


def match_rates(point):
    """按漂移点切分独立模式 verify_history, 返回 (前[n,h,rate], 后[n,h,rate])"""
    import sqlite3
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    out = []
    for op in ('<', '>='):
        n, h = c.execute(f"""
            SELECT COUNT(*), COALESCE(SUM(had_hit), 0)
            FROM verify_history
            WHERE independent_mode = 1 AND verify_date {op} ? AND had_hit IS NOT NULL""",
            (point,)).fetchone()
        out.append((n, h, h / n if n else 0))
    conn.close()
    return out[0], out[1]


def direction_weakness(point):
    """漂移后各预测方向命中率 (仅独立模式)"""
    import sqlite3
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
        SELECT pred_had_dir, COUNT(*), SUM(had_hit)
        FROM verify_history
        WHERE independent_mode = 1 AND verify_date >= ?
          AND had_hit IS NOT NULL AND pred_had_dir IN ('胜','平','负')
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


def _write_state(state):
    with open(DRIFT_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    n_total = indep_match_count()
    stats = load_stats()
    print(f'[漂移响应·独立模式] 验证批次 {len(stats)} 个, 场次 {n_total} (积累起点 {INDEP_START})')

    # ===== 0. Ultra 14.3 冷启动: 样本不足保持中性 (RULE-016) =====
    # 独立模式场次 < MIN_SAMPLES 时, 任何漂移信号都可能被小样本噪声主导,
    # 写中性状态 (全1.0), 注明积累进度 — 旧模式数据永不参与。
    if n_total < MIN_SAMPLES:
        state = {
            'drift_detected': False,
            'weight_multipliers': {k: 1.0 for k in BASE_MULT},
            'mode': 'independent',
            'restarted_at': INDEP_START,
            'accumulated_matches': n_total,
            'min_samples': MIN_SAMPLES,
            'activated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'source': 'v215_verify.py CUSUM + regression.db (independent_mode=1 only)',
            'note': (f'Ultra 14.3 冷启动: 独立模式 (2026-08-20启用) 验证场次 '
                     f'{n_total}/{MIN_SAMPLES} 不足, 漂移响应中性 (全1.0)。 '
                     f'旧四源模式历史数据已按 RULE-016 隔离, 不参与新模型漂移计算。'),
        }
        _write_state(state)
        print(f'[漂移响应] 冷启动 ({n_total}/{MIN_SAMPLES}场) → 已写入中性乘数 (全1.0)')
        print(f'  旧模式数据已隔离; 积累满 {MIN_SAMPLES} 场后 CUSUM 自动启用')
        return

    # ===== 1. CUSUM 检测 (仅独立模式数据) =====
    result = cusum_drift_detection(stats, target_rate=0.5, threshold=3.0)
    detected = bool(result['drift_detected'])
    point = result['drift_point'] or ''
    print(f'  CUSUM: pos={result["cusum_pos"]:.3f} 漂移={"是" if detected else "否"} '
          f'{result["drift_direction"]} @ {point or "-"}')

    if not detected or not point:
        state = {
            'drift_detected': False,
            'weight_multipliers': {k: 1.0 for k in BASE_MULT},
            'mode': 'independent',
            'restarted_at': INDEP_START,
            'accumulated_matches': n_total,
            'activated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'source': 'v215_verify.py CUSUM + regression.db (independent_mode=1 only)',
            'note': f'CUSUM未检出漂移 ({result["interpretation"][:60]}), 响应解除',
        }
        _write_state(state)
        print('[漂移响应] 未检出漂移 → 已写入全1.0乘数 (响应解除)')
        return

    # ===== 2. 漂移前后命中率 (仅独立模式) =====
    (pre_n, pre_h, pre_r), (post_n, post_h, post_r) = match_rates(point)
    drift_pp = (pre_r - post_r) * 100
    print(f'  漂移前(<{point}): {pre_h}/{pre_n} = {pre_r*100:.1f}%')
    print(f'  漂移后(≥{point}): {post_h}/{post_n} = {post_r*100:.1f}%')
    print(f'  漂移幅度: {drift_pp:+.1f}pp')

    # ===== Ultra 13.16: 实测无退化护栏 =====
    # CUSUM是累积量, 检出点可能被早期亏损主导; 若漂移后命中率并未低于漂移前
    # (drift_pp<=0), 说明无实际退化, 不应降权 — 写全1.0解除响应, 只留检出记录。
    MIN_DRIFT_PP = 2.0   # 实测退化<2pp视为噪音, 不触发权重响应
    if drift_pp < MIN_DRIFT_PP:
        state = {
            'drift_detected': False,
            'cusum_flagged': True,
            'cusum_drift_point': point,
            'weight_multipliers': {k: 1.0 for k in BASE_MULT},
            'mode': 'independent',
            'restarted_at': INDEP_START,
            'measured_drift_pp': round(drift_pp, 1),
            'pre_hit_rate': round(pre_r, 3),
            'post_hit_rate': round(post_r, 3),
            'activated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'source': 'v215_verify.py CUSUM + regression.db (independent_mode=1 only)',
            'note': (f'CUSUM累积量于{point}越限, 但实测漂移{drift_pp:+.1f}pp<{MIN_DRIFT_PP}pp'
                     f'(漂移后{post_r*100:.1f}% vs 漂移前{pre_r*100:.1f}%), '
                     f'无实际退化 → 权重响应解除(全1.0)'),
        }
        _write_state(state)
        print(f'[漂移响应] 实测漂移{drift_pp:+.1f}pp<{MIN_DRIFT_PP}pp(无退化) → 已写入全1.0乘数 (响应解除)')
        return

    # ===== 3. 方向失准 (仅独立模式) =====
    dw = direction_weakness(point)
    print(f'  漂移后方向命中: {dw}')

    # ===== 4. 权重乘数 (随漂移幅度缩放, 独立模式两源) =====
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

        'mode': 'independent',
        'restarted_at': INDEP_START,
        'accumulated_matches': n_total,
        'activated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'v215_verify.py CUSUM + regression.db (independent_mode=1 only, RULE-016)',
        'note': (f'独立模式CUSUM漂移响应: 漂移{drift_pp:.1f}pp → '
                 f'xG-Poisson×{mult["poisson"]}/Elo×{mult["elo"]} (缩放{scale}×); '
                 f'数据自2026-08-20重新积累, 旧模式场次已隔离'),
    }

    _write_state(state)
    print(f'[漂移响应] 已写入 {DRIFT_STATE_FILE}')
    print(f'  乘数: ' + '  '.join(f'{k}×{v}' for k, v in mult.items()))


if __name__ == '__main__':
    main()
