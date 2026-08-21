#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
learn_swot_shift.py — 改进#5 (2026-08-21): SWOT 迁移系数学习闭环
=====================================================================

背景 (swot_calibration_analysis 审视版v1 复盘结论):
  SWOT 概率迁移的全部参数都是拍的: 迁移系数 k=1pp/点、上限 ±20pp、
  主客同权。复盘发现明显主客不对称 (客队评分占优时客胜率53.8%~69.2%有信号,
  主队评分占优时主胜率仅33%~40%近乎无信号)。本脚本把迁移参数纳入学习框架,
  替代手工调权 (方案B) — 手工调权的依据是 n=2~23 的关键词命中率噪声, 不可采信。

学习对象 (apply_swot_prob_shift 常规迁移分支):
  k           : 每评分点迁移幅度 (现值 0.01)
  max_shift   : 常规迁移上限 (现值 0.20)
  home_factor : 主队占优信号的迁移缩放 (现值 1.0; 预期学出 <1.0)
  away_factor : 客队占优信号的迁移缩放 (现值 1.0)
  不动: SWOT_FLIP_* 强信号翻转分支 (刻意设计, 样本中触发极少, 留待专项积累)

数据来源 (RULE-016 合规 + 多源纪律):
  regression.db · verify_history · independent_mode=1 场次
  (旧四源模式/单源leisu时代场次永久隔离: 评分体系 15.9 已变, 旧样本无代表性)
  × 预测JSON 的 swot 账本 (swot_score/model_dir_orig/prob_adjust.old_p/intel_source)

学习方法 (复刻 L2/L3 范式):
  1. 回放: 取迁移前 wdl (prob_adjust.old_p; 未迁移场取 had.p), 用候选参数
     经生产函数 swot_fusion_v3.apply_swot_prob_shift 重算 (直接import生产函数+
     覆写模块常量, 杜绝复刻漂移), 对实际赛果计时间衰减加权 LogLoss
  2. 网格搜索: k × max_shift × home_factor × away_factor
  3. 三道护栏: n<MIN_N 不产出; MIN_N→FULL_N 向现值线性收缩;
     加权LL改善 < MIN_GAIN 不启用 (applied=false 时消费端零行为变化)

输出:
  predictions/swot_shift_params.json
  {applied, k, max_shift, home_factor, away_factor, n_matches, gain_ll, ...}

入口:
  CLI   : python learn_swot_shift.py [--verbose]
  retrain(): v215_verify 每期验证后自动调用 (已挂载)
"""
import json
import math
import os
import sqlite3
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'predictions', 'regression.db')
PRED_DIR = os.path.join(BASE_DIR, 'predictions')
OUT_PATH = os.path.join(PRED_DIR, 'swot_shift_params.json')

# 现值 = 先验 (收缩锚点 / 基线)
CURRENT = {'k': 0.01, 'max_shift': 0.20, 'home_factor': 1.0, 'away_factor': 1.0}
# 三道护栏参数 (与 learn_fusion_weights / calibrate_indep_probs 同量级)
MIN_N = 60
FULL_N = 150
MIN_GAIN = 0.002
HALF_LIFE_DAYS = 90
_EPS = 1e-6

# 搜索网格 (粗而稳; 小样本不配细网格 — LRN-20260820-001: 小样本一律网格搜索)
GRID_K = [0.0, 0.005, 0.0075, 0.01, 0.0125, 0.015, 0.02]
GRID_MAX_SHIFT = [0.10, 0.15, 0.20, 0.25]
GRID_HOME_FACTOR = [0.3, 0.5, 0.7, 0.85, 1.0]
GRID_AWAY_FACTOR = [0.8, 1.0, 1.15, 1.3]

DIRS = ('胜', '平', '负')


def _parse_wdl(s):
    """'32%/29%/38%' 或 '32.0/29.0/38.0' → [0.32,0.29,0.38]; 非法 → None"""
    if not s or not isinstance(s, str):
        return None
    import re
    m = re.findall(r'([\d.]+)', s)
    if len(m) != 3:
        return None
    try:
        p = [float(x) / 100.0 for x in m]
    except ValueError:
        return None
    if min(p) <= 0.0 or max(p) >= 1.0:
        return None
    t = sum(p)
    return [x / t for x in p]


def _parse_swot_score(s):
    """'主3.5/客1.0' → (3.5, 1.0); 非法 → None"""
    if not s or not isinstance(s, str):
        return None
    import re
    m = re.search(r'主(-?[\d.]+)/客(-?[\d.]+)', s)
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


def _indep_filter_sql(c):
    """RULE-016 模式隔离: independent_mode 列存在则用之, 否则回退 pred_file 含 _indep"""
    cols = [r[1] for r in c.execute('PRAGMA table_info(verify_history)')]
    if 'independent_mode' in cols:
        return 'independent_mode = 1'
    return "pred_file LIKE '%_indep%'"


def _find_match_swot(pred_json, home, away):
    """在预测JSON results 中按主客队名找 swot 账本块"""
    rs = pred_json.get('results') or {}
    items = rs.values() if isinstance(rs, dict) else (rs if isinstance(rs, list) else [])
    for m in items:
        mh = str(m.get('home_name') or m.get('home') or '')
        ma = str(m.get('away_name') or m.get('away') or '')
        if mh and ma and (home in mh or mh in home) and (away in ma or ma in away):
            return m
    return None


def load_rows(verbose=False):
    """读取独立模式带SWOT账本的验证场次。
    返回 [{date, wdl_pre, diff, outcome_idx, w_date, intel_source, flipped}]
    wdl_pre: 迁移前概率 (prob_adjust.old_p 优先; 未迁移场取 had.p 当前值)
    """
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    c = conn.cursor()
    where = _indep_filter_sql(c)
    rows = c.execute(
        f'SELECT verify_date, home, away, had_result, pred_file FROM verify_history '
        f'WHERE {where} AND had_result IS NOT NULL').fetchall()
    conn.close()

    pred_cache = {}
    out = []
    today = time.time()
    for vdate, home, away, result, pred_file in rows:
        if result not in DIRS or not pred_file:
            continue
        if pred_file not in pred_cache:
            path = os.path.join(PRED_DIR, pred_file)
            try:
                with open(path, encoding='utf-8') as f:
                    pred_cache[pred_file] = json.load(f)
            except Exception:
                pred_cache[pred_file] = None  # 文件缺失(成品清理策略) → 跳过
        pj = pred_cache[pred_file]
        if not pj:
            continue
        m = _find_match_swot(pj, home, away)
        if not m:
            continue
        sw = m.get('swot') or {}
        scores = _parse_swot_score(sw.get('swot_score'))
        if not scores:
            continue
        pa = sw.get('prob_adjust') or {}
        wdl_pre = _parse_wdl(pa.get('old_p')) or _parse_wdl((m.get('HAD') or {}).get('p'))
        if not wdl_pre:
            continue
        # 时间衰减权重
        try:
            dt = time.mktime(time.strptime(vdate, '%Y-%m-%d'))
            age_d = max(0.0, (today - dt) / 86400.0)
        except Exception:
            age_d = 0.0
        w_date = 0.5 ** (age_d / HALF_LIFE_DAYS)
        out.append({
            'date': vdate,
            'wdl_pre': wdl_pre,
            'diff': scores[0] - scores[1],
            'outcome_idx': DIRS.index(result),
            'w_date': w_date,
            'intel_source': sw.get('intel_source', ''),
            'flipped': bool(pa.get('flipped')),
        })
    if verbose:
        print(f'[learn_swot_shift] 可用场次: {len(out)} / 独立模式验证 {len(rows)}')
    return out


def _weighted_logloss(rows, k, max_shift, hf, af):
    """用生产函数回放候选参数, 返回时间衰减加权 LogLoss。
    直接覆写 swot_fusion_v3 模块常量后调生产函数 — 与线上行为严格同源。"""
    import swot_fusion_v3 as sf
    sf.SWOT_SHIFT_PER_POINT = k
    sf.SWOT_MAX_SHIFT = max_shift
    sf._SHIFT_HOME_FACTOR = hf
    sf._SHIFT_AWAY_FACTOR = af
    tot_w = 0.0
    tot_ll = 0.0
    for r in rows:
        # apply_swot_prob_shift(wdl, home_score, away_score) 函数内只用评分差,
        # 本学习器只存了 diff — 用 (diff, 0) 复原, 语义等价
        new_wdl, _shift, _applied = sf.apply_swot_prob_shift(r['wdl_pre'], r['diff'], 0.0)
        p = max(_EPS, min(1.0 - _EPS, new_wdl[r['outcome_idx']]))
        tot_ll += -math.log(p) * r['w_date']
        tot_w += r['w_date']
    return tot_ll / tot_w if tot_w > 0 else None


def learn(rows, verbose=False):
    """网格搜索 + 三道护栏。返回产出 dict (含 applied 标记)"""
    n = len(rows)
    base = {
        'applied': False,
        'n_matches': n,
        'min_n_required': MIN_N,
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'current': dict(CURRENT),
    }
    # 护栏①: 冷启动不产出
    if n < MIN_N:
        base['note'] = f'冷启动: 独立模式SWOT账本场次 {n}<{MIN_N}, 不产出, 维持现值'
        return base

    base_ll = _weighted_logloss(rows, **{'k': CURRENT['k'], 'max_shift': CURRENT['max_shift'],
                                         'hf': CURRENT['home_factor'], 'af': CURRENT['away_factor']})
    if base_ll is None:
        base['note'] = '回放失败(权重和为0), 不产出'
        return base

    best = None
    for k in GRID_K:
        for ms in GRID_MAX_SHIFT:
            for hf in GRID_HOME_FACTOR:
                for af in GRID_AWAY_FACTOR:
                    ll = _weighted_logloss(rows, k=k, max_shift=ms, hf=hf, af=af)
                    if ll is not None and (best is None or ll < best[0]):
                        best = (ll, k, ms, hf, af)
    best_ll, bk, bms, bhf, baf = best
    gain = base_ll - best_ll
    if verbose:
        print(f'[learn_swot_shift] 基线LL={base_ll:.4f} 最优LL={best_ll:.4f} '
              f'增益={gain:.4f} @ k={bk} max={bms} hf={bhf} af={baf}')

    # 护栏③: 增益检验
    if gain < MIN_GAIN:
        base['note'] = f'增益不足 ({gain:.4f}<{MIN_GAIN}), 不启用, 维持现值'
        base['baseline_ll'] = round(base_ll, 5)
        base['best_ll'] = round(best_ll, 5)
        return base

    # 护栏②: MIN_N→FULL_N 向现值线性收缩
    lam = min(1.0, (n - MIN_N) / max(1, FULL_N - MIN_N))
    learned = {
        'k': CURRENT['k'] + (bk - CURRENT['k']) * lam,
        'max_shift': CURRENT['max_shift'] + (bms - CURRENT['max_shift']) * lam,
        'home_factor': CURRENT['home_factor'] + (bhf - CURRENT['home_factor']) * lam,
        'away_factor': CURRENT['away_factor'] + (baf - CURRENT['away_factor']) * lam,
    }
    base.update({
        'applied': True,
        'k': round(learned['k'], 5),
        'max_shift': round(learned['max_shift'], 4),
        'home_factor': round(learned['home_factor'], 4),
        'away_factor': round(learned['away_factor'], 4),
        'shrink_lambda': round(lam, 3),
        'baseline_ll': round(base_ll, 5),
        'best_ll': round(best_ll, 5),
        'gain_ll': round(gain, 5),
        'grid_best': {'k': bk, 'max_shift': bms, 'home_factor': bhf, 'away_factor': baf},
        'note': '已启用 (三道护栏通过)',
    })
    return base


def retrain(verbose=False):
    """v215_verify 自动调用入口。返回一句话摘要。"""
    rows = load_rows(verbose=verbose)
    result = learn(rows, verbose=verbose)
    try:
        with open(OUT_PATH, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return f'SWOT迁移参数学习: 写盘失败 {e}'
    if result['applied']:
        return (f"SWOT迁移参数学习: 已启用 n={result['n_matches']} "
                f"k={result['k']} max={result['max_shift']} "
                f"hf={result['home_factor']} af={result['away_factor']} "
                f"(增益{result['gain_ll']})")
    return f"SWOT迁移参数学习: {result.get('note', '未启用')} (n={result['n_matches']})"


def main():
    verbose = '--verbose' in sys.argv
    print(retrain(verbose=verbose))


if __name__ == '__main__':
    main()
