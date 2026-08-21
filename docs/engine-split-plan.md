# v215_e2e 拆分计划（改进 #1）

> 原则：**行为不变增量**。每个增量只迁移可验证的纯函数/内聚簇，原位置改 import 绑定，
> 迁移后跑全量单测 + 导入冒烟（`import v215_e2e / v215_update / v215_verify`）+ 关键函数行为断言。
> 禁止一次性大爆炸式拆分——没有全量回归测试网时，9900 行引擎的整体重排风险不可控。

## 进度

| 增量 | 内容 | 状态 |
|---|---|---|
| 1 | `engine/decision.py`：format_stars / stars_to_score / kelly_criterion / _hhad_display_label | ✅ 2026-08-21 |
| 2 | `engine/parsing.py`：wdl/赔率/百分比解析纯函数簇（_parse_wdl_str 类） | 待做 |
| 3 | `engine/sources/`：三源构建（_build_h2h_independent_source / Elo / xG λ链） | 待做（依赖重，需先补源级单测） |
| 4 | `engine/fusion.py`：对数几何融合 + compute_fuse_weights + 学习权重加载 | 待做 |
| 5 | `engine/calibration/`：C1-C4 + L3 Platt 接入 + 标定文件加载器群 | 待做 |
| 6 | `engine/output/`：主推/双选/星级流水线（compute_cross_market_value 等） | 待做 |

## 增量 2+ 的操作规程

1. 选簇：只选无模块级状态读写、无跨簇回调的函数；有全局依赖的先改为显式参数再迁。
2. 迁移：代码**原样复制**到目标模块，原位置改 import；注释保持原样（含 Ultra 版本考古信息）。
3. 验证：`py_compile` + 导入冒烟 + 迁移函数的行为断言（同输入同输出）+ `unittest discover -s tests`。
4. 提交：单增量单 commit，信息注明"行为不变"。

## 已知拆不动/缓拆

- `predict_match` 主函数及其直接闭包：模块级常量与十余个标定 JSON 加载器交织，
  需先完成增量 5（标定加载器群收编）后才可动。
- SWOT 相关链路已独立在 `swot_fusion_v3.py`，不在本拆分范围。
