<div align="center">

# ⚽ 竞彩足球预测系统 (sporttery-prediction)

**以体彩（sporttery.cn）为核心的端到端竞彩足球预测引擎**

多源数据融合 · 五大玩法全覆盖 · 概率模型校准 · 赛果验证闭环 · 模拟投注

</div>

---

## 📌 项目简介

本项目是一套**端到端、全自动**的竞彩足球预测系统。它以中国体育彩票（sporttery.cn）竞彩足球开盘场次为唯一预测目标，自动获取体彩官方赔率与多方统计数据，通过多源概率融合模型生成 **胜平负（HAD）/ 让球胜平负（HHAD）/ 比分 / 总进球 / 半全场** 五大玩法预测，融合 SWOT 情报增强，输出带推荐策略的 PDF 报告，并支持赛果验证闭环与模拟投注。

> ⚠️ **免责声明**：本项目仅供研究学习参考，不构成任何投注建议。所有预测严格遵循体彩竞彩足球官方规则。

---

## ✨ 核心特性

| 能力 | 说明 |
|------|------|
| 🎯 **五大玩法全覆盖** | HAD / HHAD / 比分 / 总进球 / 半全场，与体彩官方玩法一一对应 |
| 🧠 **多源概率融合** | Shin 方法 + Power + 校准 Poisson + Elo 四源加权融合 |
| 📊 **历史数据标定** | 基于 3000+ 场历史数据校准联赛参数、平局率、让平率 |
| 📈 **Dixon-Coles 修正** | 修正低比分概率，提升比分预测准确度 |
| 💰 **Kelly 投注公式** | 提供 EV 价值分析与资金管理建议 |
| 🏟️ **动态主场优势** | 按联赛自动调整主场优势系数 |
| 🔄 **赛果验证闭环** | Brier / RPS / ECE / ROI 多指标追踪，贝叶斯反馈优化 |
| 🔁 **增量更新** | 智能缓存 + 趋势追踪 + 重大变更警报 |
| 🎰 **模拟投注** | 按置信度自动选场、串关决策、自动结算与 ROI 统计 |
| 📜 **PDF 报告** | 手机阅读优化格式的高清预测报告 |
| 🧠 **跨会话记忆** | 零依赖文件持久化记忆系统，积累经验教训 |


## 🚀 快速开始

### 环境要求

- Python 3.9+（`requests`, `reportlab`, `pandas`, `numpy`, `sqlite3`）
- Node.js（`jsdom@29`）

### 安装依赖

```bash
pip install requests reportlab pandas numpy
npm install jsdom
```

### 环境变量

```bash
export SPORTTERY_WORKSPACE=/workspace/sporttery   # 工作目录（缺省=脚本所在目录）
```

### 核心工作流

```bash
# 1. 预测全流程（先修改 v215_e2e.py 顶部 TARGET_WEEKDAY / MATCH_NUMBERS）
python v215_e2e.py

# 2. 生成 PDF 预测报告
python gen_report_pdf.py predictions/pred_20260726_周日.json

# 3. M串N 复式模拟（容错过关组合的中奖概率/期望盈亏/ROI）
python msn_simulator.py predictions/pred_20260726_周日.json

# 4. 赛果验证（赛后）
python v215_verify.py "2026-07-26 201,202,203,204"

# 5. 增量更新赔率
python v215_update.py 2026-07-26 201,202
```

---

## 📁 目录结构

```
sporttery/
├── v215_e2e.py              # 核心预测引擎（端到端取数 + 七步预测）
├── v215_update.py           # 增量更新模块（趋势 + 警报 + 缓存）
├── v215_verify.py           # 赛果验证（Brier / RPS / ECE + SQLite）
├── v215_simulate.py         # 模拟投注（选场 + 串关 + 结算）
├── msn_simulator.py         # M串N 复式投注模拟器（32 种组合）
├── nowscore_fetch.py        # nowscore 数据源
├── swot_auto.py             # SWOT 全自动获取（leisu 主 + stats 备用）
├── swot_fusion_v3.py        # SWOT 融合（概率迁移 + 置信度调整）
├── leisu_session.py         # leisu 会话管理（WAF 自动求解）
├── gen_report_pdf.py        # PDF 报告生成（reportlab）
├── gen_report_v2.py         # 报告逻辑库
├── pdf_fonts.py             # PDF 中文字体注册公共模块（LxgwWenKai 优先）
├── memory.py                # 跨会话记忆系统（文件持久化）
├── src/                     # 数据采集层（config / data_collectors）
├── lab/                     # 实验管线（features / models / ensemble）
├── scripts/                 # 一次性分析脚本（赔率变动 / 联赛模式 / 回测等）
├── predictions/             # 预测结果 + 回归数据库
│   ├── pred_*.json          # 预测结果
│   ├── historical_odds.db   # 历史赔率数据库（3000+ 场）
│   ├── regression.db        # 回归验证数据库
│   └── archive/             # 历史归档
└── *.md                     # 项目文档（AGENT / CRITICAL_RULES / JINGCAI_RULES 等）
```

---

## 🧠 预测引擎（七步流程）

| 步骤 | 输入 | 输出 | 核心算法 |
|------|------|------|----------|
| Step 1 | 初赔大小球优先 | goal_line（市场基准） | 盘口众数 |
| Step 2 | 体彩 HAD  | P0 贝叶斯先验 | Shin 方法修正 |
| Step 3 | 近况战绩修正 | P1 + 近期进球/失球率 | 指数衰减权重 |
| Step 4 | 对赛战绩修正 | P1 更新 + h2h 进球趋势 | 历史交锋特征 |
| Step 5 | 盘口 + 动态主场优势 | λ_h, λ_a 泊松参数 | 贝叶斯收缩 |
| Step 6 | Dixon-Coles 泊松 | 比分 Top3 + 大小球 | 负二项分布 + DC τ 修正 |
| Step 7 | 多维交叉验证 + Kelly | 置信度 + EV + 数据质量 | 六维度校验 |

### 五大玩法预测

| 体彩玩法 | 系统字段 | 选项数 | 预测方式 |
|----------|----------|--------|----------|
| 胜平负 | `HAD` | 3 | 赔率 + 贝叶斯 + 泊松 |
| 让球胜平负 | `HHAD` | 3 | HHAD 赔率 + 亚指 + 泊松修正 |
| 比分 | `score` | 31 | Dixon-Coles 泊松分布 Top3 |
| 总进球数 | `total_goals` | 8 | 泊松 λ_total 分布 |
| 半全场 | `half_full` | 9 | 半场泊松分布联合概率 |

---

## 📄 项目文档

| 文档 | 说明 |
|------|------|
| [ABOUT.md](./ABOUT.md) | 项目详细介绍（架构 / 算法 / 数据流 / 开发） |
| [AGENT.md](./AGENT.md) | Agent 系统配置与使用说明 |
| [CRITICAL_RULES.md](./CRITICAL_RULES.md) | 铁律库（不可违反的系统规则） |
| [JINGCAI_RULES.md](./JINGCAI_RULES.md) | 竞彩官方规则知识库 |
| [LEARNINGS.md](./LEARNINGS.md) | 经验教训库（跨会话持久化） |
| [WORK_PROGRESS.md](./WORK_PROGRESS.md) | 工作进度与架构演进记录 |


---

## 📜 许可

本项目仅供研究学习使用，不构成投资或投注建议。数据来源于公开渠道，版权归各平台所有。

**投注有风险，参与需谨慎。**
