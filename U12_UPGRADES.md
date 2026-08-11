# Ultra 12.0 十项模型升级 — 集成完成报告

**时间**: 2026-08-11 16:35 | **状态**: 全部集成完毕，回归测试通过 | **未提交**（本地 .git 丢失）

## 一、完成内容

十项数学模型升级全部落地，分两个文件：

| 文件 | 内容 |
|---|---|
| `model_upgrades.py` | 10 个升级函数模块（此前已完成） |
| `v215_e2e.py` | **本次完成 9 处集成编辑（Edit A–I）**，py_compile 通过 |

### 集成点明细（v215_e2e.py）

| # | 升级 | 集成位置 | 机制 |
|---|---|---|---|
| 1 | robust_goal_line | fetch_daxiao_goal_line | 众数→加权中位数 + 初终盘 Kalman 混合（吸附 0.25 标准盘口） |
| 2 | odds_calibration | 四源融合后 | isotonic 逐类校准再归一化（n=788 已训） |
| 3 | glicko2_form | exponential_decay_form | Glicko-2 期望胜率按 RD 降权混合（0.15–0.40） |
| 4 | h2h_shrink | parse_h2h_record | 小样本交锋向联赛基准贝塔收缩（保留 raw 值） |
| 5 | dc_lambda | λ clamp 前 | DC 攻防强度λ 与市场λ 65/35 混合（n=939/89 球队已训） |
| 6 | bivariate_poisson | compute_dc_matrix | BP 共同冲击 λ3 矩阵与 NB+DC 结果 50/50 混合 |
| 7 | learned_fusion | ensemble_fuse 前 | 历史 Brier 学习权重（**缺参休眠**，守卫回退） |
| 8 | hhad_same_source | compute_scores | HHAD 改从统一比分矩阵求和（随 #6 生效） |
| 9 | draw_window_model | 平局窗口规则 | logistic elif 分支（**缺参休眠**） |
| 10 | conf_ece | 置信度终算前 | Δ→命中率 ECE 封顶（**缺参休眠**） |

**关键设计**：全部开关守卫（`UPGRADES` 字典），缺数据/缺参数自动回退原逻辑；**开关全关 ≡ 升级前行为**。

## 二、同数据 A/B 验证（260811001 江原FC vs 大阪钢巴，同一份缓存）

| 指标 | 基线（开关全关） | 升级后 | 归因 |
|---|---|---|---|
| HAD 概率 | 28%/29%/**43%** | 27%/**31%**/42% | 平局 +2pp ← isotonic + BP 修正平局低估（呼应 LRN-20260809-002） |
| HHAD 模型源 | 6.3/14.0/79.7（Skellam） | 6.7/13.9/79.4（矩阵同源） | 自洽性改进，口径统一 |
| 大小球（2/2.5） | 大 53.3% | 大 60.1% | BP 共同冲击增大总进球方差 |
| 平局窗口HHAD优先 | 未触发 | **触发**（平 31%≥30%） | 校准后越过硬阈值 |
| 主推方向 | HAD负 / HHAD让负 | 不变 | 方向稳定，仅概率结构优化 |
| 耗时 | 2.4s | 1.3s | 无性能退化 |

休眠项：fusion_weights / draw_window / conf_calibrator 需 regression.db 积累后 `model_upgrades.train_all()` 重训激活。dc_lambda 本场回退（大阪钢巴为日职球队，不在历史库覆盖联赛内）。

## 三、遗留事项（需用户决策）

1. **本地 .git 目录丢失**（16:24–16:33 之间被删除，此前 stash 报 missing object 已是前兆）：
   - 本次改动（v215_e2e.py、model_upgrades.py、predictions/model_upgrades_params.json）**仅存在于工作区，未提交**
   - GitHub 仓库仍停留在 b43780b（不含十项升级）
   - 选项：① 重新 clone 仓库后覆盖提交（需要 token）② 用户自行处理
2. 本机 PDF 生成需 reportlab：managed venv 只有 requests；项目内 `.wbtest_venv` 依赖齐全可跑 PDF。
3. 证据文件（未提交）：`predictions/pred_001_before_samedata.json` / `pred_001_after_upgrades.json` / `pred_20260811_周二.before_upgrades.json`
