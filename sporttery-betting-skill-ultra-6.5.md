# 竞彩足球预测系统 — 完整 Skill (Ultra 6.5)

## 概述

本 Skill 实现竞彩足球比赛的端到端预测：从体彩场次编号出发，自动获取赔率与统计数据，通过多源概率融合模型生成 HAD/HHAD/比分/半全场/总进球预测，融合 SWOT 数据增强，最终输出带第一/第二推荐的 **PDF 报告**（不出 HTML）。

**通用化原则（Ultra 6.4 起）**：所有数据获取用纯 requests，不依赖 Kimi 专属能力（WebBridge 仅作 nowscore matchID 发现的降级备份）；所有路径通过 `SPORTTERY_WORKSPACE` 环境变量或脚本所在目录定位，无硬编码绝对路径。

## 触发条件

- 用户消息包含"预测"关键词
- 用户给出体彩场次编号（如"预测7月25日201-211"）
- 用户要求"更新"已有预测
- 用户要求生成报告或PDF

## 工作流程

### 阶段 1：数据获取

1. **sporttery.cn API**：获取场次赔率、队名、开赛时间（WAF 403/503/567 退避重试；结果 API 回退按 TARGET_WEEKDAY 过滤）
2. **nowscore.com**（主数据源，纯 requests 通用通道，全程优先）：
   - matchID 发现优先级：match_id_map → **bf1.js（主，今日+明日全赛程，30 分钟磁盘缓存）** → WebBridge 渲染 schedule.aspx（降级备份） → sc1.js（旧通道）
   - `analysisJs/data{mid}.js`：近况（form）、交锋（h2h）、积分榜（场均进失），纯 requests 直连
3. **500.com**（降级备用）：仅当 nowscore 失败时启用，获取 fixture_id 后并行抓取数据
4. **初赔 AJAX**：获取 500.com 初赔三盘数据（欧指/亚指/大小球）

### 阶段 2：七步预测（predict_match 函数）

核心预测引擎，对每场比赛执行：

1. **市场概率**：Shin 方法修正 favorite-longshot bias + 500.com 欧指融合
2. **期望进球 λ 计算**：
   - 基础进攻力 = (本队场均进 + 对手场均失) / 2
   - 贝叶斯收缩：向联赛均值 1.3 收缩（k=10）
   - 主场优势：`λ_主 × 联赛系数`，`λ_客 ÷ 联赛系数`
   - 近况修正：指数衰减权重（含平局=半胜半负）
3. **四源概率融合**：
   - 市场隐含概率（Shin + form 修正）
   - Power 方法概率（互补 Shin）
   - 校准 Poisson 概率（负二项分布 + Logit 校准）
   - Elo 评级概率（球队统计 + 近况）
   - 对数空间加权集成（几何平均，保持概率锐度）
4. **λ-赔率方向冲突校准（Ultra 6.0）**：
   - 检测 λ 方向与融合概率方向是否矛盾
   - 冲突时用融合概率重新分配 λ（保持总进球量不变）
   - 重新计算比分矩阵，使半全场/总进球/比分与 HAD 方向一致
5. **比分矩阵**：负二项分布 + Dixon-Coles 低分修正 + Skellam 净胜球分布
6. **跨玩法分析**：HAD/HHAD/双选/纯方向的价值排序（prob/ev/hybrid 三种模式；hybrid 为阈值-决胜法，见关键配置）
7. **置信度计算**：概率差值 + 数据质量 + 模型一致性 + SWOT 调整 + 历史贝叶斯反馈

### 阶段 3：SWOT 融合（swot_fusion_v3.py，Ultra 6.4）

- **全自动获取（swot_auto.py）**：leisu 当日情报卡片自动发现（leisu_session.py 会话 + solve_waf_jsdom_v2.js WAF 求解，需 `npm install` jsdom），匹配 sporttery 场次后写入 `swot_data_refreshed.json`；500.com 为备用源
- 手动补充仍可用 `swot_fast_v3.py` 批量获取（WAF 自动求解 + Cookie 复用，9 页 <15 秒）
- 加权评分：优势条目数量 + 关键情报（排名/交锋/伤停/状态）+ 走势数据
- **实际调整概率**：胜/负间线性迁移，每评分点 1pp，上限 ±8pp，评分差 <2 不调，**平局固定不动**，任一侧不低于 2%；记录 `prob_adjust {delta,old_p,new_p,flipped}`
- **置信度调整**：±0.5★/±1★ 写回 `HAD.conf`（旧值存 `conf_old`，防重复调整）；方向翻转时更新 dir+odds
- 一致性判断对比**调整前**方向

### 阶段 4：报告生成 — 只出 PDF

```bash
python gen_report_pdf.py predictions\pred_xxx.json
```

- 一步出 PDF（reportlab），含第一/第二推荐（HAD/HHAD 单选按概率排序）、竞彩官方玩法区块（半全场/总进球/比分 EV）、M串N 推荐表、prob_adjust 徽标、data_source 标识
- `gen_report_v2.py` 仅作逻辑库（`rank_match`/`REPORT_TITLE`），不再独立出 HTML
- 综合评分 = 置信度×15 + 概率×0.4 + SWOT 加成 + EV 微调 + 覆盖加成
- 第一/第二推荐必须不同类型（不同市场或单选/双选不同）
- 字体通用回退链：`SPORTTERY_FONT_DIR` → `./fonts/` → 解释器 fonts/ → 系统字体（simhei/msyh/PingFang/Noto）

### 阶段 5：验证回归（v215_verify.py）

```bash
python v215_verify.py "2026-07-26 201,202,203,204"
```

- 赛果匹配 + Brier/RPS/LogLoss + ECE 校准分析
- 入库 `predictions/regression.db`（INSERT OR REPLACE + UNIQUE(verify_date,match_key)，无重复）
- 验证报告保留 HTML（weasyprint 不可用；PDF-only 约束仅针对预测报告）

## 核心文件清单

| 文件 | 用途 |
|------|------|
| `v215_e2e.py` | 核心预测引擎（端到端取数+七步预测，Ultra 6.5） |
| `nowscore_fetch.py` | nowscore 数据源抓取（bf1.js 通用通道 + analysisJs 补数据） |
| `swot_fusion_v3.py` | SWOT 数据融合（实际调整概率+置信度，Ultra 6.4） |
| `swot_fast_v3.py` | SWOT 批量获取（WAF 绕过） |
| `gen_report_v2.py` | 报告逻辑库（rank_match/REPORT_TITLE，不出 HTML） |
| `gen_report_pdf.py` | PDF 报告生成（reportlab，字体通用回退链） |
| `v215_verify.py` | 赛果验证 + 贝叶斯反馈数据库 |
| `v215_update.py` | 预测增量更新模块 |
| `swot_auto.py` | SWOT 全自动获取调度（leisu 主 + 500 备用） |
| `leisu_session.py` | leisu 会话管理（Cookie/WAF 复用） |
| `solve_waf_jsdom_v2.js` | leisu 新版混淆 WAF 求解（Node + jsdom，见 package.json） |
| `msn_simulator.py` | M串N 复式投注模拟（32 种组合表内置校验） |
| `JINGCAI_RULES.md` | 竞彩官方玩法/规则参考文档 |
| `package.json` | Node 依赖声明（jsdom，zip 不含 node_modules，需 `npm install`） |

## 关键配置

```python
# v215_e2e.py 顶部配置区
TARGET_DATE = None        # 不限日期
TARGET_WEEKDAY = "周六"    # 按周几过滤
MATCH_NUMBERS = ["201","202",...,"211"]  # 场次编号
RECOMMEND_MODE = 'hybrid'   # prob=命中率优先 | ev=EV优先 | hybrid=阈值-决胜法(默认)
HYBRID_PROB_TOLERANCE = 3.0  # hybrid 误差带(pp): 概率≥p_max-3pp 视为等价, 候选内取EV最高
```

**hybrid 阈值-决胜法**：模型概率有 ±3pp 量级估计误差，误差带内选项命中率等价，用 EV 决胜 → 不牺牲命中率的前提下兼顾赔率价值。取代旧 0.6/0.4 线性混合。报告侧第一/第二推荐 = HAD/HHAD 单选中概率最高/次高（双选不进推荐位）。

## 数学模型详解

### 期望进球 λ 计算链

```
基础进攻力 → 贝叶斯收缩(k=10) → 主场优势(×联赛系数) → 近况修正(指数衰减)
→ 市场大小球盘口校准总λ (Ultra 6.4: target=0.65×模型+0.35×(盘口+0.1), 等比缩放∈[0.80,1.25], 偏差>2%才调)
```

### 平局校准（calibrate_probabilities，Ultra 6.2-6.5）

- 自适应平局目标（按 λ_total 插值）+ **联赛平局率先验**（LEAGUE_DRAW_RATE，18 联赛，权重 0.3）
- **平赔信号**：平赔 <3.4 目标 +2pp，>4.0 减 1pp
- **平局偏差在线反馈（Ultra 6.5）**：从 verify_history 统计"实际平局率 vs 平均预测平局概率"，样本 ≥30 时按偏差×0.5 修正目标，有界 ±0.03（当前 n=50，修正 -2.0pp）
- λ 接近加成（|λ_h-λ_a|<0.15 → shift +0.25）+ 最低平局保底 0.15

### λ-赔率方向冲突校准（Ultra 6.0）

当 `λ_主 > λ_客` 但 `P(胜) < P(负)` 时触发：

```
总λ不变
主队份额 = P(胜) + 0.5 × P(平)
客队份额 = P(负) + 0.5 × P(平)
λ_主_new = 总λ × 主队份额
λ_客_new = 总λ × 客队份额
```

校准后重新计算比分矩阵，使半全场/总进球/比分与 HAD 方向一致。

### 半全场概率（compute_half_full，Ultra 6.4）

- 上半场 λ = 全场 λ × 0.45（开局谨慎）
- 下半场 λ = 全场 λ × 0.55（体能下降+战术放开）
- 上半场 r=15（进球少→过离散弱），下半场 r=12
- 9 种组合联合概率：`P(HT结果, FT结果) = Σ P(HT比分) × P(下半场比分)`
- **半场矩阵补标准 Dixon-Coles τ**（与全场同式，不做放大）
- **融合概率边际重加权**：按 FT 结果边际比率调整，权重有界 0.5-2.0，使半全场与 HAD 方向一致

### 负二项分布（negbin_pmf）

```python
def negbin_pmf(k, lam, r=10.0):
    # r 越小→过离散越强（冷门更多）
    # r=8: 低分比赛（强过离散）
    # r=12: 高分比赛（弱过离散）
    # r=15: 上半场（进球少→过离散弱）
```

## 输出结构

```json
{
  "HAD": {"dir": "负", "odds": 1.39, "conf": "★★★★", "p": "27%/26%/47%"},
  "HHAD": {"dir": "让胜", "handicap": 1.0, "odds": 2.5, "conf": "★★★★", "p": "43%/26%/31%"},
  "lam": "1.0/1.5",
  "lam_calibration": {
    "recalibrated": true,
    "original": "1.29/1.24",
    "calibrated": "1.02/1.52",
    "reason": "λ校准: 方向冲突, 按融合概率重分配"
  },
  "half_full": {"main": "负负(29.3%)", "top3": "负负:29.3 平负:16.7 平平:16.4"},
  "total_goals": {"main": "2球(23.4%)", "top3": "2球:23.4% 1球:21.2% 3球:19.0%"},
  "score": {"top3": "0-1:11.7 1-1:11.5 0-0:9.3", "wdl": "25.9/25.9/48.2"},
  "swot": {"swot_lean": "主队略占优", "consistency": "不一致", "conf_adjust": "-0.5★",
           "prob_adjust": {"delta": -0.03, "old_p": "27%/26%/47%", "new_p": "27%/26%/47%", "flipped": false}},
  "goals": {"home_expected": 1.0, "away_expected": 1.5, "key_insight": "..."}
}
```

## Ultra 版本演进

| 版本 | 核心特性 |
|------|---------|
| 1.0 | 基础框架、置信度计算、主场优势乘除法 |
| 2.0 | 贝叶斯收缩、Shin 方法 |
| 3.0 | 集成预测、比赛可预测性评分、精简 token |
| 4.0 | 对数空间融合、共享 DC 矩阵函数 |
| 5.0 | 负二项分布、Elo 四源融合、自适应校准 |
| 6.0 | Skellam 分布、**λ-赔率方向冲突校准** |
| 6.1 | 贝叶斯历史反馈（Beta-Binomial 共轭） |
| 6.2 | 平局校准增强：全源校准 + market 源更强 shift + λ 接近加成 |
| 6.3 | 分级 λ 接近加成 + 最低平局保底 0.15 |
| 6.4 | 通用化（纯 requests/SPORTTERY_WORKSPACE）+ SWOT 实际调概率 + 联赛平局率先验/平赔信号 + 大小球盘口校准总λ + 半全场 DC τ/边际重加权 + 报告 PDF-only |
| 6.5 | **平局偏差在线反馈**：verify_history 实际平局率 vs 预测均值闭环修正 target_draw（±0.03）+ 历史反馈 DB 路径修复（Windows 生效） |

## 使用示例

### 预测新比赛

```
用户: 预测7月26日201-204
```

→ 修改 `MATCH_NUMBERS` 和 `TARGET_WEEKDAY`，运行 `python v215_e2e.py`

### SWOT 增强

```
用户: https://www.leisu.com/guide/swot-4467105 ...
```

→ 运行 `swot_fast_v3.py` 获取 SWOT → `python swot_fusion_v3.py predictions\pred_xxx.json` 融合

### 生成报告（PDF）

```bash
python gen_report_pdf.py predictions\pred_20260726_周日.json
```

### 更新预测

```bash
python v215_update.py  # 增量更新，保留历史
```

## 依赖

```
requests, reportlab, sqlite3
Node.js + jsdom (SWOT WAF 绕过)
```

## 验证闭环

```
predict_match → 预测结果 → v215_verify.py → regression.db
      ↑                                          │
      ├──── query_historical_feedback (贝叶斯命中率反馈) ←─┤
      └──── query_draw_bias (平局偏差在线反馈, Ultra 6.5) ←┘
```

历史命中率低于阈值时自动降星：联赛命中率 <45% 降 0.5★，方向命中率 <40% 降 0.5★。平局偏差反馈样本 ≥30 时自动修正平局目标（±0.03 有界）。
