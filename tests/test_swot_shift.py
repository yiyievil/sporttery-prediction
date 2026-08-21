#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""改进#5 单测: apply_swot_prob_shift 的 argmax不穿越语义 + 学习参数消费端

运行: python -m unittest tests.test_swot_shift -v   (仓库根目录)
背景: LRN-20260821-002 证明该函数的阈值比较是死代码高发区, 行为变更必须先补测试。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import swot_fusion_v3 as sf  # noqa: E402


def dir_of(wdl):
    return sf._wdl_dir(wdl)


class TestArgmaxNoCrossing(unittest.TestCase):
    """改进#5 核心: 常规迁移(2<=|diff|<6)不得改变 argmax 方向"""

    def test_normal_migration_cannot_overtake(self):
        # w=33/d=34/l=33, diff=+5 → 原逻辑 shift=+5pp → w=38 反超 d=34 (穿越)
        # 新逻辑: 上限 cur_max−w−eps = 0.9pp → w=33.9 仍 < 34, 方向不变
        wdl = [0.33, 0.34, 0.33]
        new, shift, applied = sf.apply_swot_prob_shift(wdl, 5.0, 0.0)
        self.assertTrue(applied)
        self.assertLess(new[0], new[1], '常规迁移不得反超当前argmax(平)')
        self.assertEqual(dir_of(new), '平')

    def test_normal_migration_symmetric_away(self):
        # 对称: w=33/d=34/l=33, diff=−5 → l 不得反超 d
        wdl = [0.33, 0.34, 0.33]
        new, shift, applied = sf.apply_swot_prob_shift(wdl, 0.0, 5.0)
        self.assertTrue(applied)
        self.assertLess(new[2], new[1])
        self.assertEqual(dir_of(new), '平')

    def test_normal_migration_strengthens_existing_argmax(self):
        # 受益侧已是 argmax: 不受不穿越上限约束, 正常加强
        wdl = [0.40, 0.30, 0.30]
        new, shift, applied = sf.apply_swot_prob_shift(wdl, 3.0, 0.0)
        self.assertTrue(applied)
        self.assertAlmostEqual(new[0], 0.43, places=4)
        self.assertAlmostEqual(new[2], 0.27, places=4)
        self.assertEqual(dir_of(new), '胜')

    def test_tie_eps_gap_preserved(self):
        # 追平也不允许: 迁移后受益侧严格低于 argmax 至少 SWOT_TIE_EPS
        wdl = [0.33, 0.34, 0.33]
        new, _, _ = sf.apply_swot_prob_shift(wdl, 5.0, 0.0)
        self.assertLessEqual(new[0], new[1] - sf.SWOT_TIE_EPS + 1e-9)

    def test_capped_below_threshold_returns_unadjusted(self):
        # 距argmax极近时上限 < 0.5pp → 不调整 (applied=False), 而非硬塞微迁移
        wdl = [0.339, 0.340, 0.321]
        new, shift, applied = sf.apply_swot_prob_shift(wdl, 5.0, 0.0)
        self.assertFalse(applied)
        self.assertEqual(new, wdl)


class TestStrongSignalFlipPreserved(unittest.TestCase):
    """强信号分支(|diff|>=6)保留穿越权 — 改进#5不动此机制"""

    def test_strong_signal_still_flips(self):
        wdl = [0.30, 0.40, 0.30]
        new, shift, applied = sf.apply_swot_prob_shift(wdl, 8.0, 0.0)
        self.assertTrue(applied)
        self.assertEqual(dir_of(new), '胜', '强信号分支必须保留翻转能力')
        self.assertGreaterEqual(new[0], 0.40 + sf.SWOT_FLIP_MARGIN - 1e-9)

    def test_strong_signal_same_dir_no_flip(self):
        # 方向一致时强信号分支不触发翻转逻辑, 走常规迁移(仍受不穿越约束—但已是argmax)
        wdl = [0.45, 0.30, 0.25]
        new, shift, applied = sf.apply_swot_prob_shift(wdl, 8.0, 0.0)
        self.assertTrue(applied)
        self.assertEqual(dir_of(new), '胜')
        self.assertGreater(new[0], 0.45)


class TestDrawBoostUnchanged(unittest.TestCase):
    """|diff|<2 平局提升分支: 刻意设计, 行为不变"""

    def test_draw_boost_still_works(self):
        wdl = [0.34, 0.33, 0.33]
        new, boost, applied = sf.apply_swot_prob_shift(wdl, 0.5, 0.0)
        self.assertTrue(applied)
        self.assertGreater(new[1], 0.33, '平局提升分支必须保留')
        self.assertAlmostEqual(sum(new), 1.0, places=6)


class TestLearnedParamsConsumer(unittest.TestCase):
    """消费端: applied=false 零行为变化; applied=true 覆盖常量"""

    def test_no_file_no_change(self):
        self.assertIsNone(getattr(sf, '_SHIFT_PARAMS', None) if not os.path.exists(
            os.path.join(sf.PREDICTIONS_DIR, 'swot_shift_params.json')) else None)
        # 无文件时: 常量为现值
        if not os.path.exists(os.path.join(sf.PREDICTIONS_DIR, 'swot_shift_params.json')):
            self.assertEqual(sf.SWOT_SHIFT_PER_POINT, 0.01)
            self.assertEqual(sf._SHIFT_HOME_FACTOR, 1.0)

    def test_asymmetry_factor_scales_shift(self):
        # 模拟学习生效: home_factor=0.5 → 主队方向迁移减半
        old_hf = sf._SHIFT_HOME_FACTOR
        try:
            sf._SHIFT_HOME_FACTOR = 0.5
            wdl = [0.40, 0.30, 0.30]
            new, _, _ = sf.apply_swot_prob_shift(wdl, 4.0, 0.0)
            # diff=4 × hf0.5 × k0.01 = 2pp (而非4pp)
            self.assertAlmostEqual(new[0], 0.42, places=4)
        finally:
            sf._SHIFT_HOME_FACTOR = old_hf

    def test_params_validation_rejects_incomplete(self):
        # LRN-20260821-002 防御: 缺字段的坏文件不得生效
        with tempfile.TemporaryDirectory() as td:
            bad = {'applied': True, 'k': 0.02}  # 缺 max_shift/home_factor/away_factor
            p = os.path.join(td, 'swot_shift_params.json')
            with open(p, 'w', encoding='utf-8') as f:
                json.dump(bad, f)
            old_dir = sf.PREDICTIONS_DIR
            try:
                sf.PREDICTIONS_DIR = td
                self.assertIsNone(sf._load_swot_shift_params())
            finally:
                sf.PREDICTIONS_DIR = old_dir


if __name__ == '__main__':
    unittest.main()
