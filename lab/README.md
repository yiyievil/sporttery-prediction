# lab/ — 研究线（baseline 参照，非生产路径）

> 改进 #3（2026-08-21）：明确双轨定位，消除"研究线/生产"职责混淆。

## 定位

`lab/` 是**研究版基线管线**：Poisson / XGBoost / Elo 三模型 + 等权集成
（`run_pipeline.py` 编排，`features.py` 特征，`models.py` 模型，`ensemble.py` 集成）。

它与生产主链路（`v215_e2e.py`，Ultra 15.9 独立模式）**没有调用关系**，
两者仅共享 `predictions/historical_odds.db`（xG/Elo 历史数据）。

## 用途边界

- ✅ 允许：作为新模型想法的实验场；作为生产引擎的 baseline 对照
  （"三模型等权集成在同样数据上能打到多少"）；xG 特征工程的参考实现
  （贝叶斯收缩 / 压力指数 / 防泄漏 shift(1) 等写法可直接参考）。
- ❌ 禁止：把 lab 的预测输出当作生产预测；在生产链路中 import lab
  （生产模型的唯一真源是 `v215_e2e.py` + `model_upgrades.py`）。

## 回流规则

lab 中验证有效的想法，回流生产时必须走**护栏范式**（参照
`learn_fusion_weights.py` / `calibrate_indep_probs.py`）：
冷启动不产出 → 小样本向先验收缩 → 增益检验 → `applied` 标记，
消费端在 `applied=false` 时零行为变化。禁止直接把实验参数写死进生产。

## 已知差异（有意为之，非 bug）

- lab 的 Poisson/XGBoost/Elo 是教学级实现；生产侧对应物是
  xG-Poisson λ链 / 历史Elo库+Glicko-2 / H2H贝塔收缩，复杂度不同。
- lab 集成权重来自 `src/config.py` 的 `model_weights`（0.35/0.35/0.30），
  生产融合权重由 `learn_fusion_weights.py` 学习（护栏生效前用启发式先验）。
