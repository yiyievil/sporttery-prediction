# engine/ — v215_e2e 主引擎拆分包 (改进#1, 2026-08-21 起步)
#
# 背景: v215_e2e.py 单文件 ~9900 行, 上帝对象。拆分按"行为不变增量"推进 —
# 每个增量只迁移零依赖纯函数, 原位置改为 import 绑定, 全量单测+导入冒烟验证。
#
# 边界图 (docs/engine-split-plan.md):
#   decision.py   决策/展示纯函数 (本增量)      — 已完成
#   sources/      三源构建 (xG-Poisson/Elo/H2H) — 待拆分
#   fusion.py     对数几何融合 + 权重           — 待拆分
#   calibration/  C1-C4 + Platt + 标定加载      — 待拆分
#   output/       推荐/双选/星级流水线          — 待拆分
