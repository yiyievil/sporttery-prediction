# 足球预测系统 v215 — 工作进度文档

> 更新时间: 2026-07-27 12:00
> 系统版本: Ultra 6.10 (历史标定 + 双向让平校准)
> 工作目录: `C:\Users\CCJ\OneDrive\Desktop\sporttery`

---

## 一、当前系统架构 (Ultra 6.10)

> **核心原则: 所有预测围绕体彩(Sporttery)展开**
> - Sporttery 是唯一预测目标: 体彩场次决定预测范围, 体彩赔率是预测基准
> - nowscore / 500.com / leisu 均为辅助数据源, 旨在增强体彩预测精度
> - 无体彩开盘的比赛不进入预测流程

```
                        ┌─────────────────────────────────┐
                        │     Sporttery (体彩) — 核心      │
                        │  预测目标 + 赔率基准 + 场次范围    │
                        │  HAD/HHAD赔率 + 固定奖金(比分/   │
                        │  总进球/半全场) + 赛果验证        │
                        └──────────────┬──────────────────┘
                                       │ 为每场体彩比赛增强数据
                    ┌──────────────────┼──────────────────┐
                    ▼                  ▼                  ▼
          ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
          │   nowscore      │ │   500.com       │ │   leisu         │
          │  辅助数据源(主)  │ │  降级备用       │ │  SWOT情报源     │
          │  三合一盘口+    │ │  欧指/亚赔/     │ │  有利/不利情报   │
          │  近况+交锋+积分 │ │  大小球(仅降级) │ │  (概率迁移增强)  │
          └─────────────────┘ └─────────────────┘ └─────────────────┘
```

### 数据流 (以体彩为核心)

```
Phase 1: 体彩场次获取 (Sporttery API — 必须, 决定预测范围)
  ├─ matchList API → 按周几+编号获取体彩在售比赛
  ├─ HAD/HHAD赔率 → 预测基准赔率
  └─ 固定奖金API → 比分/总进球/半全场官方赔率 (EV价值分析)
    │
Phase 1.5: 统计数据增强 (nowscore — 辅助, 为体彩预测提供数据)
  ├─ 三合一盘口: 亚盘/欧赔/大小球 (校准λ和市场信号)
  ├─ 近况战绩: 近5-6场form (指数衰减权重)
  ├─ 对赛交锋: h2h历史 (交锋趋势)
  └─ 积分榜: 场均进失球 (贝叶斯收缩参数)
    │  (nowscore失败的场次 → 降级500.com, 最终保底用sporttery赔率)
    │
Phase 2: 预测引擎 (v215_e2e.py — 所有概率最终对标体彩赔率)
  ├─ λ建模: 体彩大小球盘口校准总λ (target=0.65模型+0.35盘口)
  ├─ 四源融合: 市场(体彩HAD)+Power+校准Poisson+Elo
  ├─ 历史标定: 1452场历史数据校准联赛参数+平局率+让平率
  ├─ 平局校准: HAD双信号(主赔+平赔) + HHAD让平率(-1/-2/+1盘口)
  └─ 输出: HAD/HHAD/比分/半全场/总进球 — 全部对标体彩玩法
    │
Phase 3: SWOT情报融合 (leisu — 增强, 可选)
  └─ 概率迁移: 胜/负间线性迁移, 平局固定不动
    │
Phase 4: 报告 — PDF (对标体彩官方玩法)
Phase 5: 验证回归 (v215_verify.py — 赛果来自体彩官方)
```

## 二、通用化约定 (本次优化核心约束)

- **不依赖 Kimi 专属能力**: 所有数据获取用纯 requests; WebBridge 仅作 nowscore matchID 发现的降级备份。
- **路径通用**: 所有脚本用 `os.environ.get('SPORTTERY_WORKSPACE') or 脚本所在目录` 定位工作区; 不再硬编码 `/workspace`、`/data/user/work`。
- **字体通用**: `gen_report_pdf.py` 字体回退链: `SPORTTERY_FONT_DIR` → `./fonts/` → 解释器 fonts/ → 系统字体(simhei/msyh/PingFang/Noto)。
- **报告只出 PDF**, 不出 HTML。

## 三、运行命令 (Windows 当前工作目录)

```bash
cd "C:\Users\CCJ\OneDrive\Desktop\sporttery"

# 1. 预测全流程 (一条命令: sporttery+固定奖金 → nowscore → 预测 → SWOT自动获取+融合)
#    修改 v215_e2e.py 顶部 TARGET_WEEKDAY / MATCH_NUMBERS 后运行; AUTO_SWOT=False 可关SWOT
python v215_e2e.py

# 2. PDF 报告
python gen_report_pdf.py predictions\pred_20260726_周日.json

# 2b. M串N 复式模拟 (容错过关组合的中奖概率/期望盈亏/ROI)
python msn_simulator.py predictions\pred_20260726_周日.json

# 3. 赛果验证 (赛后)
python v215_verify.py "2026-07-26 201,202,203,204"
```

### Ultra 6.5 新增模块

| 文件 | 功能 |
|------|------|
| `JINGCAI_RULES.md` | 竞彩官方规则知识库: 六大玩法/投注方式/32种M串N组合表/奖金计算/封顶与限额 |
| `msn_simulator.py` | M串N容错过关模拟器: 32种官方组合枚举+泊松二项分布DP, 输出各组合中奖概率/期望盈亏/ROI (用法: `python msn_simulator.py predictions\pred_xxx.json`) |
| `swot_auto.py` | SWOT全自动: leisu guide发现→队名匹配→批量获取→stats备用兜底→写swot_data_refreshed.json |
| `leisu_session.py` | leisu会话工具: jsdom WAF自动求解 + 页面获取 |
| `solve_waf_jsdom_v2.js` | 阿里云WAF acw_sc__v2 求解器 (jsdom, 需 `npm install jsdom`, 通用) |

### 推荐规则 (hybrid 阈值-决胜法, 当前默认)

- `RECOMMEND_MODE='hybrid'`: 候选 = 概率 ≥ p_max − `HYBRID_PROB_TOLERANCE`(默认3.0pp), 候选内取 EV 最高。
- 原理: 模型概率存在 ±3pp 量级估计误差, 误差带内选项命中率等价 → 用 EV 决胜, 不牺牲命中率前提下兼顾赔率价值。取代旧 0.6×概率+0.4×EV 线性混合 (比例无理论依据)。
- 报告侧: 第一/第二推荐 = HAD/HHAD 单选中概率最高/次高 (双选不进推荐位, 仅作"双选保险"信息行)。
- 投注哲学: 每玩法本质是各选1, 全包必亏; 目标是"最可能命中 + 兼顾高赔率(EV)"。
- `HYBRID_PROB_TOLERANCE` 可随 verify ECE 校准结果调整。

### Ultra 6.5 Phase 1 增强

- **sporttery 固定奖金**: `getFixedBonusV1.qry?matchId=` 纯requests获取官方 比分/总进球/半全场 赔率,
  predict_match 内做 EV 价值分析 (模型概率×官方赔率-1), 输出 `sporttery_pools` + 摘要行
- **sporttery 保底**: nowscore/500 双失败的场次不再丢弃, 用纯sporttery赔率基准预测 (data_source='sporttery(保底)')
- match list/结果 API 均已记录 match_id

### SWOT 自动化 (leisu为主, 500/nowscore stats为备)

- leisu.com/guide 列表页自动发现当日情报卡片 (队名/时间/联赛/swot-ID), 无需人工提供URL
- WAF: jsdom 真实DOM执行挑战脚本 (静态算法与纯Node桩对新版混淆WAF均无效, jsdom是必需依赖)
- 500.com无免费SWOT情报 ("专家情报"为付费功能) → 备用方案改用 500/nowscore 已获取的
  近况/交锋/积分统计数据生成"数据型情报" (source='stats'), 保证每场都有SWOT输入
- 获取后自动调用 swot_fusion_v3 融合回预测文件 (Ultra 6.4 概率迁移规则)

## 四、Ultra 6.4/6.5 回测与实测记录

- 07-25 回测 (6.4): 命中 5/11 与 6.3 持平, Brier 7.202→7.204 (噪音级), 无回归。
- 07-25 验证全流程: HAD 6/11, Brier 0.2724, ECE 0.239, 入库成功, sim 结算运行正常。
- nowscore 全链路: 单场约 0.9s (缓存命中) / 9.8s (首次); bf1.js 覆盖今日+明日 605 场。
- 平局偏差反馈 (6.5): 当前 n=50, 实际平局率 22.0% vs 预测均值 26.0% → 修正 -2.0pp (模型略高估平局, 自动下调)。
- 历史贝叶斯反馈修复后: 65 样本, overall 49.2%, 韩职 49.7%/10场, 高置信 49.9%/30场 + calibration_warning。

## 五、当前待办

- [ ] 同步 skill 文档 `sporttery-betting-skill-ultra-6.1.md` → 6.10
- [ ] 今日周日 201-204 已出 6.4 引擎+nowscore 全数据预测 (`pred_20260726_周日.json`), 赛后跑 verify 验证实战效果

---

## 六、Ultra 6.6 ~ 6.10 历史标定集成 (2026-07-26 ~ 07-27)

### 6.1 历史数据采集 (collect_historical.py)

- 数据源: Sporttery API (matchList + matchResult) + 500.com (欧赔/亚赔/大小球)
- 覆盖联赛: 瑞超、芬超、挪超、巴甲、韩K、英超、欧冠、欧罗巴、欧协联
- 赛季: 2025赛季 + 2026赛季截至当前
- 入库: `predictions/historical_odds.db` (SQLite)
- 总量: 1452场比赛 (含HAD赔率、终赔、赛果、进球数、半场比分)

### 6.2 联赛标定参数 (Ultra 6.6)

- 输出: `predictions/league_calibration.json`
- 内容: 每联赛的 H/D/A 胜率、均进球、主场优势、主客均进球
- 集成: 替换 v215_e2e.py 中硬编码的 LEAGUE_HOME_ADV / LEAGUE_DRAW_RATE / LEAGUE_AVG_GF_MAP
- 函数: `get_league_param()` / `get_league_odds_calibration()`

### 6.3 融合后平局校准 — HAD (Ultra 6.9)

- 函数: `post_fusion_draw_calibration(probs, had, league)`
- 位置: 四源融合后、HAD方向确定前
- 双信号: 主赔区间历史平局率(40%) + 平赔区间历史平局率(30%) + 联赛先验(30%)
- 发现:
  - 主赔3.5+区间: 实际平局率28.6% vs 隐含22.4% (+6.2pp)
  - 平赔2.5-3.0: 实际平局率40.9% vs 隐含30.6% (+10.3pp)
  - 主赔2.0-2.5区间: 实际20.0% vs 隐含27.6% (-7.6pp, 高估)
- 修正: 双向有界, 上调70% gap cap 12pp, 下调50% gap cap 8pp
- 势均力敌检测: |主赔-客赔|<0.3时额外+3pp

### 6.4 让球盘口让平率标定 — HHAD (Ultra 6.10)

#### 标定数据加载

- 函数: `_load_league_calibration()` 中循环遍历 -1/-2/+1/+2 四个盘口
- 每盘口按6个主赔区间 + 按联赛计算让平率
- 让平判定:
  - -1球: 主队恰好赢1球 (diff=1)
  - -2球: 主队恰好赢2球 (diff=2)
  - +1球: 主队恰好输1球 (diff=-1)
  - +2球: 主队恰好输2球 (diff=-2)
- 最小样本: -1用15场, -2用10场, +1用15场, +2用5场

#### 标定结果

| 盘口 | 场次 | 赔率区间数 | 联赛数 | 偏差模式 |
|------|------|-----------|--------|---------|
| -1球 | 839 | 6 | 8 | 低赔区间低估+4~5pp |
| -2球 | 52 | 1 | 2 | 低赔区间低估+5.1pp |
| +1球 | 469 | 2 | 7 | 高赔区间高估-2.4pp |
| +2球 | 7 | 0 | 0 | 样本不足, 基础设施就绪 |

#### 校准函数

- 函数: `post_fusion_hhad_draw_calibration(probs, had, hhad, handicap, league)`
- 位置: HHAD四源融合后、方向确定前
- 双向校准:
  - 正偏差(低估): 上调65% gap, cap 10pp (-1/-2球)
  - 负偏差(高估): 下调50% gap, cap 8pp (+1球, 更保守)
- 联赛名匹配: 支持去赛季后缀 (如 '挪超_2026' → '挪超')
- 盘口key转换: 正数盘口加'+'前缀 (str(1) → '+1')

#### 回测验证 (Brier Score 改善)

| 盘口 | 场次 | 未校准 | 校准后 | 改善 |
|------|------|--------|--------|------|
| -1球 | 839 | 0.1804 | 0.1775 | +1.6% |
| -2球 | 52 | 0.1572 | 0.1504 | +4.3% |
| +1球 | 469 | 0.1759 | 0.1734 | +1.4% |

### 6.5 高级标定模块 (Ultra 6.7)

- 文件: `predictions/advanced_calibration.json`
- 6大模块: 主场优势/近况修正/联赛进球特征/赔率概率偏差/平局信号/大小球校准
- 函数: `apply_advanced_calibration()` 在四源融合后施加有界修正
- 总修正量有界, 每子模块独立修正

### 6.6 文件清单

| 文件 | 说明 |
|------|------|
| `v215_e2e.py` | 核心预测引擎 (Ultra 7.1) |
| `v215_verify.py` | 赛果验证+回归分析 |
| `v215_simulate.py` | M串N模拟 |
| `v215_update.py` | 数据更新 |
| `swot_fusion_v3.py` | SWOT融合 |
| `swot_auto.py` | SWOT自动获取 |
| `swot_fast_v3.py` | SWOT快速获取 |
| `gen_report_pdf.py` | PDF报告生成 |
| `gen_report_v2.py` | 报告逻辑库 |
| `msn_simulator.py` | M串N容错模拟器 |
| `leisu_session.py` | leisu会话工具 |
| `nowscore_fetch.py` | nowscore数据获取 |
| `predictions/historical_odds.db` | 历史数据库 (3099场 + 62294条赔率变动) |
| `predictions/league_calibration.json` | 联赛标定参数 (静态快照, 引擎实际从DB实时重算) |
| `predictions/advanced_calibration.json` | 高级标定参数 (基于1452场, 待按全量库重算) |
| `predictions/regression.db` | 回归验证数据库 |

---

## 七、Ultra 7.0 ~ 7.1 (2026-07-28)

### 7.1 全量数据采集 (Ultra 7.0 数据基础)

- `historical_odds.db`: 1452 → 3099 场 (2025-01 ~ 2026-07 全量体彩开盘), 新增 `odds_change_history` 表 62294 条赔率变动 (3055场)
- 新增联赛: 意甲/西甲/德甲/法甲/韩职/日职/英冠/葡超/荷甲/德乙/美职联等
- 采集脚本: `collect_odds_history.py` / `collect_mls.py` / `collect_mls_500.py` / `analyze_mls.py`

### 7.2 经验校准函数 (Ultra 7.0)

- `calibrate_global_odds_bias()`: 全局赔率区间偏差校准 (2.5-3.5区间跳过, 其余50%修正)
- `calibrate_odds_change_signal()`: 初终赔变动信号校准 (微调15%/中等20%/大幅10%)
- 回测 (2933场): 命中率 52.54% → 52.88% (全局偏差), 对数损失 0.9916 → 0.9911

### 7.3 Ultra 7.1 修复 (2026-07-28 下午)

- **严重**: 14:13 merge 将 25MB 全量库回退为 1452 场旧库 → 已从 git 历史 (8cda386) 恢复
- **高影响 bug 修复**: `LEAGUE_AVG_GF_MAP` 语义混淆 — 原把"全场总进球"(2.4~3.3)当作贝叶斯收缩的"单队λ先验"(应~1.3), 导致 λ 系统性高估; 拆分为 `LEAGUE_AVG_GF_MAP`(单队, avg_goals/2) + `LEAGUE_AVG_GOALS_MAP`(全场)
- `calibrate_odds_change_signal` 目标主胜率改为从标定库动态读取 (原硬编码, 数据更新不生效)
- 回退参数表按 3099 场重算, 补 `美职联` 别名
- 4个采集/回测脚本 `/workspace` 硬编码路径 → `SPORTTERY_WORKSPACE` 通用约定
- 清理: 重复 sqlite3 导入 / 4处无用导入 / 3处同分支冗余 if-else / 函数内重复 import re

### 7.4 待办参数建议 (见对话报告, 未应用)

- home_adv clamp [1.05,1.35] 过窄 (7个联赛原始值<1.05)
- 赔率变动 rise_medium/rise_large 修正回测为负收益
- advanced_calibration.json 仍基于1452场, 建议按全量库重算

---

*文档更新 — 2026-07-28*
