#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""改进#5 单测: learn_swot_shift 学习路径 (合成数据)

构造 "主队信号错误 / 客队信号正确" 的合成样本, 验证:
  1. 学习器能识别不对称 (home_factor < away_factor)
  2. 三道护栏: n<60 不产出 / 收缩 / 增益检验
运行: python -m unittest tests.test_learn_swot_shift -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import learn_swot_shift as ls  # noqa: E402
import swot_fusion_v3 as sf  # noqa: E402


def _row(diff, wdl, outcome, date='2026-08-20'):
    return {'date': date, 'wdl_pre': wdl, 'diff': diff,
            'outcome_idx': ls.DIRS.index(outcome), 'w_date': 1.0,
            'intel_source': 'leisu+stats', 'flipped': False}


def _synthetic(n_pairs=50):
    """主客不对称合成样本: 主队占优信号不准(实际多负), 客队占优信号准(实际负)"""
    rows = []
    for _ in range(n_pairs):
        # 主队信号(+4)但赛果为负 → 迁移应弱化 (home_factor ↓)
        rows.append(_row(+4.0, [0.40, 0.30, 0.30], '负'))
        # 客队信号(−4)且赛果为负 → 迁移应强化 (k/away_factor ↑)
        rows.append(_row(-4.0, [0.34, 0.33, 0.33], '负'))
    return rows


class TestLearnPath(unittest.TestCase):
    def tearDown(self):
        # 回放覆写了模块常量, 复位防串测试
        sf.SWOT_SHIFT_PER_POINT = 0.01
        sf.SWOT_MAX_SHIFT = 0.20
        sf._SHIFT_HOME_FACTOR = 1.0
        sf._SHIFT_AWAY_FACTOR = 1.0

    def test_cold_start_no_output(self):
        rep = ls.learn(_synthetic(20))  # n=40 < 60
        self.assertFalse(rep['applied'])
        self.assertIn('冷启动', rep['note'])

    def test_asymmetry_learned(self):
        rep = ls.learn(_synthetic(50))  # n=100 → 收缩 λ=(100-60)/90≈0.44
        self.assertTrue(rep['applied'], f"应启用: {rep.get('note')}")
        self.assertLess(rep['home_factor'], 1.0, '主队信号不准 → home_factor 应学低')
        self.assertLess(rep['home_factor'], rep['away_factor'], '主客不对称方向须正确')

    def test_gain_guard_blocks_noise(self):
        # 信号与赛果无关 → 任何参数都无增益 → 不启用
        rows = []
        for i in range(120):
            outcome = ls.DIRS[i % 3]
            rows.append(_row(+3.0 if i % 2 else -3.0, [0.34, 0.33, 0.33], outcome))
        rep = ls.learn(rows)
        self.assertFalse(rep['applied'])
        self.assertIn('增益不足', rep['note'])


if __name__ == '__main__':
    unittest.main()
