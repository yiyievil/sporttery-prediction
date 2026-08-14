# 竞彩足球预测 Agent — Ultra 6.10

> **版本**: Ultra 6.10 | **更新**: 2026-07-27
> **工作目录**: `/workspace/sporttery` (或 `SPORTTERY_WORKSPACE` 环境变量指向的目录)
> **状态**: 环境已就绪 (jsdom v29.1.1 + requests + reportlab 已安装)

---

## 一、Agent 概述

本 Agent 实现竞彩足球比赛的**端到端全自动预测**：以体彩(sporttery.cn)开盘场次为唯一预测目标，自动获取体彩官方赔率与统计数据，通过多源概率融合模型生成 HAD/HHAD/比分/半全场/总进球五大玩法预测，融合 SWOT 情报增强，输出带推荐策略的 PDF 报告，并支持赛果验证闭环与模拟投注。

> **架构核心**: 所有预测围绕体彩展开。Sporttery 是预测目标+赔率基准+场次范围;
> nowscore/500.com/leisu 均为辅助数据源，为体彩预测提供统计增强。

> ### 🔒 数据源优先级策略 (所有者锁定, 禁止更改 — 2026-07-28)
> 1. **sporttery(体彩)实时数据 = 绝对核心** — 场次范围/赔率基准/固定奖金, 每次预测与更新必须实时抓取, 不可替换不可绕过;
> 2. **nowscore = 主力辅助数据源, 不得随意禁用** — 统计增强默认必须尝试 nowscore;
> 3. **500.com = 降级备用** — 仅在 nowscore 实在抓不到时使用, 且必须带 `fallback_reason` 凭证;
> 4. sporttery(保底) = 最终兜底。
> 代码实现: `v215_e2e.py` 顶部 `DATA_SOURCE_POLICY` 常量 + `_check_data_source_policy()` 运行时自检;
> `v215_update.py` fid=0 路径 nowscore 优先。任何违反此层级的改动视为 bug, 须所有者明确批准。

### 核心能力

| 能力 | 触发词 | 说明 |
|------|--------|------|
| 预测 | `预测` | 全流程取数 → 七步预测 → SWOT自动融合 → PDF报告。输入用竞彩官网编号日期: `260728 001,002` (或 `260728001,260728002`), 自动换算周几; 命令行 `python v215_e2e.py 260728 001,002` |
| 更新 | `更新` | 增量更新赔率 → 趋势分析 → 变更警报 |
| 模拟 | `模拟` | 按置信度选场 → 串关决策 → SQLite存储 |
| 验证 | `验证` | 赛果匹配 → Brier/RPS/ECE → 回归入库 |
| M串N | `M串N` | 32种容错过关组合模拟 → 中奖概率/期望盈亏/ROI |

---

## 二、环境配置

### 依赖 (已安装)

```
Python: requests, reportlab, sqlite3 (标准库)
Node.js: jsdom@29.1.1 (WAF绕过, 已 npm install)
```

### 目录结构

```
/workspace/sporttery/
├── v215_e2e.py              # 核心预测引擎 (端到端取数+七步预测)
├── v215_update.py            # 增量更新模块 (趋势+警报+缓存)
├── v215_verify.py            # 赛果验证 (Brier/RPS/ECE+SQLite)
├── v215_simulate.py          # 模拟投注 (选场+串关+结算)
├── msn_simulator.py          # M串N复式投注模拟器 (32种组合)
├── nowscore_fetch.py         # nowscore数据源 (bf1.js+3in1Odds+analysisJs)
├── swot_auto.py              # SWOT全自动获取 (leisu主+stats备用)
├── swot_fast_v3.py           # SWOT批量获取 (WAF绕过+cookie复用)
├── swot_fusion_v3.py         # SWOT融合 (概率迁移+置信度调整)
├── leisu_session.py          # leisu会话管理 (WAF自动求解)
├── solve_waf_jsdom_v2.js     # 阿里云WAF acw_sc__v2求解器 (jsdom)
├── gen_report_pdf.py         # PDF报告生成 (reportlab)
├── gen_report_v2.py          # 报告逻辑库 (rank_match, 不出HTML)
├── package.json              # Node依赖声明 (jsdom)
├── JINGCAI_RULES.md          # 竞彩官方规则知识库
├── AGENT.md                  # 本文件
├── predictions/              # 预测结果+回归数据库
│   ├── pred_YYYYMMDD_周X.json
│   ├── regression.db
│   └── swot_data_refreshed.json
└── nowscore_cache/           # nowscore磁盘缓存 (bf1.js等)
```

### 环境变量

```bash
export SPORTTERY_WORKSPACE=/workspace/sporttery  # 工作目录 (缺省=脚本所在目录)
```

---

## 三、工作流程

### 3.1 预测流程 (主流程)

```
用户: "预测7月26日201-204"
  │
  ├─ ① 修改 v215_e2e.py 顶部配置:
  │     TARGET_WEEKDAY = "周日"
  │     MATCH_NUMBERS = ["201","202","203","204"]
  │
  ├─ ② 运行: python v215_e2e.py
  │     Phase 1: Sporttery API (核心) → 体彩场次+HAD/HHAD赔率+固定奖金
  │     Phase 1.5: nowscore (辅助) → 三合一盘口+近况+交锋+积分
  │     Phase 2: 500.com (降级) → 仅nowscore失败的场次
  │     Phase 3: 数据合并 (sporttery保底, 无外部数据时用体彩赔率基准预测)
  │     Phase 4: 预测引擎 (概率对标体彩玩法)
  │     Phase 5: SWOT自动获取+融合 (leisu→jsdom WAF→概率迁移)
  │     → 输出: predictions/pred_YYYYMMDD_周X.json
  │
  └─ ③ 生成PDF报告:
        python gen_report_pdf.py predictions/pred_YYYYMMDD_周X.json
        → 输出: predictions/pred_YYYYMMDD_周X.pdf
```

### 3.2 更新流程

```bash
python v215_update.py 2026-07-26 201,202
# 或: python v215_update.py 7月26日 201,202
# → 加载已有预测 → 智能缓存验证 → 获取即时赔率 → 趋势分析 → 变更警报 → 保存
```

### 3.3 验证流程

```bash
python v215_verify.py "2026-07-26 201,202,203,204"
# → 500.com比分 + sporttery赔率 → 逐场验证
# → HAD/HHAD/比分/总进球/半全场命中率 + Brier/RPS/LogLoss/ECE
# → SQLite入库 (regression.db) → 模拟投注自动结算
```

### 3.4 模拟投注

```bash
python v215_simulate.py predictions/pred_YYYYMMDD_周X.json
# → 按置信度选场 → 串关决策(2串1~5串1) → 每注20元 → SQLite存储
```

### 3.5 M串N复式模拟

```bash
python msn_simulator.py predictions/pred_YYYYMMDD_周X.json
# → 32种官方容错过关组合 → 泊松二项分布DP
# → 各组合中奖概率/期望盈亏/ROI
```

---

## 四、核心配置 (v215_e2e.py 顶部)

```python
TARGET_DATE = None          # 不限日期
TARGET_WEEKDAY = "周日"      # 按周几过滤 (None=不过滤)
MATCH_NUMBERS = ["201"]     # 场次编号后3位

RECOMMEND_MODE = 'hybrid'   # prob=命中率优先 | ev=EV优先 | hybrid=阈值-决胜法
HYBRID_PROB_TOLERANCE = 3.0 # hybrid误差带(pp): 概率≥p_max-3pp视为等价, 候选内取EV最高

AUTO_SWOT = True            # True=预测后自动获取SWOT并融合
```

### 推荐模式说明

| 模式 | 逻辑 | 适用场景 |
|------|------|----------|
| `prob` | 概率最高且通过EV护栏 | 追求命中率、稳健投注 |
| `ev` | EV期望值最高 | 追求长期收益、价值投注 |
| `hybrid` (默认) | 概率≥p_max−3pp的候选内取EV最高 | 平衡命中率与收益 |

---

## 五、预测引擎详解 (predict_match)

### 七步预测流程

| 步骤 | 输入 | 输出 | 核心算法 |
|------|------|------|----------|
| Step 1 | 初赔大小球优先 | goal_line (市场基准) | 盘口众数 |
| Step 2 | 体彩HAD+500.com欧指 | P0贝叶斯先验(50/50融合) | Shin方法修正favorite-longshot bias |
| Step 3 | 近况战绩修正 | P1+近期进球率/失球率 | 指数衰减权重(含平局=半胜半负) |
| Step 4 | 对赛战绩修正 | P1更新+h2h进球趋势 | 历史交锋特征 |
| Step 5 | 盘口+动态主场优势 | λ_h, λ_a泊松参数 | 贝叶斯收缩(k=10)+联赛系数 |
| Step 6 | Dixon-Coles泊松 | 进球/失球概率+比分Top3+大小球 | 负二项分布+DC τ修正 |
| Step 7 | 多维交叉验证+Kelly | 置信度+EV+Kelly+数据质量 | 六维度校验 |

### 四源概率融合

1. **市场隐含概率** (Shin修正 + form修正)
2. **Power方法概率** (互补Shin)
3. **校准Poisson概率** (负二项分布 + Logit校准)
4. **Elo评级概率** (球队统计 + 近况)

对数空间加权集成 (几何平均，保持概率锐度)。

### λ-赔率方向冲突校准 (Ultra 6.0)

当 `λ_主 > λ_客` 但 `P(胜) < P(负)` 时触发：总λ不变，按融合概率重新分配λ份额，使比分矩阵与HAD方向一致。

### 平局校准 (Ultra 6.2-6.5)

- 自适应平局目标 (按λ_total插值)
- 联赛平局率先验 (LEAGUE_DRAW_RATE, 18联赛, 权重0.3)
- 平赔信号 (<3.4 +2pp, >4.0 −1pp)
- **平局偏差在线反馈** (Ultra 6.5): verify_history实际平局率vs预测均值, 样本≥30时修正±0.03

### SWOT概率调整 (Ultra 6.4)

- 胜/负间线性迁移, 每评分点1pp, 上限±8pp
- 评分差<2不调 (噪音区)
- **平局固定不动** (本系统平局系统性低估, 不侵蚀平局概率)
- 方向翻转时更新dir+odds, 记录prob_adjust

---

## 六、数据源体系

> **优先级: Sporttery(体彩) > nowscore > 500.com > leisu**
> 所有数据源服务于体彩预测。无体彩开盘的比赛不进入预测流程。

### 数据源层级

| 层级 | 数据源 | 角色 | 获取内容 | 必要性 |
|------|--------|------|----------|--------|
| **核心** | sporttery.cn | 预测目标+赔率基准 | HAD/HHAD赔率+队名+固定奖金+赛果 | **必须** |
| 辅助 | nowscore.com | 统计增强(主) | 三合一盘口+近况+交锋+积分 | 增强 |
| 降级 | 500.com | nowscore失败备用 | 欧指/大小球/初赔三盘 | 降级 |
| 增强 | leisu.com | SWOT情报源 | 有利/不利情报 | 可选 |

### 数据流 (体彩驱动)

```
① Sporttery API (必须) → 体彩场次 + HAD/HHAD赔率 + 固定奖金
                        ↓ 为每场体彩比赛增强统计
② nowscore (辅助)      → 三合一盘口 + 近况 + 交锋 + 积分
    ↓ (失败时降级)
③ 500.com (降级)       → 欧指/亚赔/大小球
    ↓ (全部失败时)
④ sporttery保底        → 纯体彩赔率基准预测
                        ↓
⑤ 预测引擎             → 所有概率对标体彩玩法
                        ↓
⑥ leisu (可选)         → SWOT情报概率迁移
```

### WAF绕过机制

leisu.com使用阿里云WAF (acw_sc__v2 cookie挑战):
1. requests获取挑战页 (含renderData + 混淆脚本)
2. 提取renderData和`<script>`内容写入临时文件
3. `node solve_waf_jsdom_v2.js` 用jsdom执行脚本 (真实DOM环境)
4. 轮询cookie出现, 输出`acw_sc__v2=xxx`
5. 设置到session.cookies, 后续请求复用

---

## 七、竞彩规则要点

### 五大玩法

| 玩法 | 选项数 | 竞猜内容 | 最高关数 |
|------|--------|----------|----------|
| 胜平负 (HAD) | 3 | 全场主胜/平/负 | 8关 |
| 让球胜平负 (HHAD) | 3 | 让球后胜/平/负 | 8关 |
| 比分 (CRS) | 31 | 精确比分 | 4关 |
| 总进球数 (TTG) | 8 | 双方进球总和(0-7+) | 6关 |
| 半全场 (HAFU) | 9 | 半场+全场结果组合 | 4关 |

### 关键规则

- 竞猜结果以**90分钟(含伤停补时)**为准, 不含加时赛和点球大战
- 固定奖金制: 出票时刻SP值锁定, 赛后赔率变动不影响
- 比赛取消: 过关投注该场SP按1.0计入, 单关退票
- 返奖率69% (68%返奖+1%调节基金), 单注不足2元补足至2元
- 单注封顶: 单场10万, 2-3关20万, 4-5关50万, 6关+100万

### M串N容错过关 (32种组合)

容错 = M − 最小关数。常见组合:
- 3串4: 容错1 (中≥2场)
- 6串22: 容错2 (中≥4场, "6中4")
- 8串28: 容错2 (中≥6场, "8中6")
- 8串247: 容错6 (中≥2场, 全覆盖)

---

## 八、输出结构

### 预测JSON (pred_*.json)

```json
{
  "HAD": {"dir": "胜", "odds": 1.86, "conf": "★★★★½", "p": "50%/30%/20%"},
  "HHAD": {"dir": "让胜", "handicap": -1.0, "odds": 2.27, "conf": "★★★", "p": "..."},
  "lam": "2.2/0.9",
  "half_full": {"main": "胜胜(42.6%)", "top3": "胜胜:42.6 平胜:20.0 平平:12.7"},
  "total_goals": {"main": "3球(23.5%)", "top3": "3球:23.5% 2球:18.4% 1球:14.2%"},
  "score": {"top3": "2-0:10.9 1-0:10.0 2-1:9.8", "wdl": "50.0/30.0/20.0"},
  "cross_market": {
    "primary_bet": {"option": "HAD胜", "prob": 50.0, "odds": 1.86, "ev_pct": -7.0},
    "pass_risk": {"prob": 24.0, "level": "中", "desc": "穿盘风险中等"},
    "insight": "主推HAD胜@1.86, 穿盘风险中等"
  },
  "swot": {"swot_lean": "主队略占优", "consistency": "一致", "conf_adjust": "+0.5★"},
  "data_quality": {"score": 85, "quality": "高"}
}
```

### PDF报告

- 第一/第二推荐 (HAD/HHAD单选按概率排序)
- 竞彩官方玩法区块 (半全场/总进球/比分EV)
- M串N推荐表
- prob_adjust徽标 (SWOT调整可视化)
- data_source标识 (nowscore/500.com/sporttery保底)
- 字体回退链: SPORTTERY_FONT_DIR → ./fonts/ → 系统字体

---

## 九、验证指标

| 指标 | 说明 |
|------|------|
| HAD/HHAD命中率 | 预测方向 vs 实际结果 |
| 比分命中率 | 推荐Top3是否含实际比分 |
| 总进球/半全场命中率 | 主推 vs 实际 |
| Brier分数 | 概率预测准确性 (越低越好, <0.15优秀) |
| RPS | 排序概率分数 |
| ECE | 期望校准误差 |
| ROI追踪 | 固定1单位投注的累计收益 |
| 置信度校准 | ★★★★+应≥60%, ★★~★★★½应≥45% |

### 回归数据库 (regression.db)

- `verify_history`: 逐场验证记录 (含Brier/ROI/各玩法命中)
- `verify_stats`: 批次统计 (含累计命中率/平均Brier)
- `sim_bets`: 模拟投注记录 (pending/won/lost + 盈亏)
- 使用 `INSERT OR REPLACE` 避免重复

### 贝叶斯反馈闭环

```
predict_match → 预测 → v215_verify.py → regression.db
      ↑                                        │
      ├─ query_historical_feedback (命中率反馈) ←┤
      └─ query_draw_bias (平局偏差反馈) ←────────┘
```

历史命中率低于阈值时自动降星: 联赛<45%降0.5★, 方向<40%降0.5★。

---

## 十、使用示例

### 示例1: 预测新比赛

```
用户: 预测7月26日201-204

Agent操作:
1. 编辑 v215_e2e.py: TARGET_WEEKDAY="周日", MATCH_NUMBERS=["201","202","203","204"]
2. 运行: cd /workspace/sporttery && python v215_e2e.py
3. 运行: python gen_report_pdf.py predictions/pred_20260726_周日.json
4. 分享PDF: computer:///workspace/sporttery/predictions/pred_20260726_周日.pdf
```

### 示例2: 赛后验证

```
用户: 验证7月26日201-204

Agent操作:
1. 运行: python v215_verify.py "2026-07-26 201,202,203,204"
2. 查看命中率/Brier/ROI统计
3. 模拟投注自动结算
```

### 示例3: M串N模拟

```
用户: 模拟M串N

Agent操作:
1. 运行: python msn_simulator.py predictions/pred_20260726_周日.json
2. 查看32种组合的中奖概率/期望盈亏/ROI
```

---

## 十一、注意事项

1. **体彩开盘日期 ≠ 比赛日期**: 比赛可能在开盘日前一天进行, 周几编号基于开盘日
2. **matchNumStr每周复用**: 不同周同号比赛会冲突, 需用周几+编号区分
3. **500.com编码**: gb2312, 非UTF-8
4. **ouzhi API返还率**: 临近开赛时返回返还率(<1.0)而非赔率(>1.0), 需检测并回退
5. **WAF依赖jsdom**: leisu.com的阿里云WAF需要真实DOM环境执行挑战脚本, jsdom是必需依赖
6. **报告只出PDF**: 预测报告为PDF-only (reportlab), 验证报告保留HTML
7. **路径通用**: 所有脚本通过 `SPORTTERY_WORKSPACE` 环境变量或脚本所在目录定位, 无硬编码绝对路径
8. **增量合并**: 保存预测时先加载已有文件, 新场次追加, 同场次覆盖, 未更新的旧场次保留
9. **历史比赛**: >1天的比赛自动跳过nowscore (bf1.js只覆盖今日+明日)
10. **sporttery保底**: nowscore/500双失败的场次用纯sporttery赔率基准预测

---

## 十二、版本演进

| 版本 | 核心特性 |
|------|---------|
| 1.0-5.0 | 基础框架→贝叶斯收缩→Shin方法→对数空间融合→负二项分布+Elo四源 |
| 6.0 | Skellam分布 + λ-赔率方向冲突校准 |
| 6.1 | 贝叶斯历史反馈 (Beta-Binomial共轭) |
| 6.2-6.3 | 平局校准增强 + 联赛平局率先验 + 最低平局保底0.15 |
| 6.4 | 通用化(纯requests/SPORTTERY_WORKSPACE) + SWOT实际调概率 + 大小球盘口校准总λ + 报告PDF-only |
| **6.5** | **平局偏差在线反馈** (verify_history闭环修正±0.03) + 历史反馈DB路径修复 + SWOT全自动(leisu+jsdom) |
| 6.6-6.8 | 历史数据采集(1452场) + 联赛标定参数 + 高级标定6模块 |
| 6.9 | 融合后平局校准(HAD): 双信号(主赔+平赔)数据驱动修正 |
| **6.10** | **让球盘口让平率标定(HHAD)**: -1/-2/+1/+2盘口双向校准 + 联赛名匹配 + 历史标定集成 |

---

## 十三、记忆系统 (跨会话持久化)

> **替代 memory-lancedb-pro 的轻量方案** — 零依赖、零配置、文件持久化

### 记忆文件

| 文件 | 用途 | 加载时机 |
|------|------|----------|
| `LEARNINGS.md` | 结构化经验教训 (架构决策/错误修复/技术学习) | 每次会话启动时阅读 |
| `predictions/memory_store.json` | 可搜索的JSON记忆库 (关键词检索+重要性排序) | 需要时通过 memory.py 查询 |
| `AGENT.md` (本文件) | 系统配置与使用说明 | 每次会话启动时阅读 |
| `WORK_PROGRESS.md` | 工作进度与架构记录 | 每次会话启动时阅读 |

### 会话启动检查清单 (Agent 铁律)

每次新会话开始时, Agent 必须执行以下步骤:

1. **阅读 `AGENT.md`** — 了解系统架构、工作流程、注意事项
2. **阅读 `LEARNINGS.md`** — 回顾架构决策(DEC)、错误修复(ERR)、技术学习(LRN)
3. **运行 `python memory.py recall "<当前任务关键词>"`** — 检索相关历史记忆
4. **确认工作目录** — `echo $SPORTTERY_WORKSPACE` 或默认 `/workspace/sporttery`

### 记忆操作命令

```bash
# 搜索记忆 (遇到任何问题先 recall)
python memory.py recall "sporttery_bonus"
python memory.py recall "缓存合并"
python memory.py recall "nowscore 端点"

# 存储新记忆 (修复bug或做出决策后立即存储)
python memory.py store "问题描述和修复方案" --category fact --importance 0.9 --tags "ERR-20260728-001"

# 查看统计
python memory.py stats

# 列出全部
python memory.py list

# 从 LEARNINGS.md 重新导入 (更新经验库后)
python memory.py init
```

### Python 内调用

```python
from memory import MemoryStore
ms = MemoryStore()

# 存储
ms.store("发现某API返回格式变更", category="fact", importance=0.85)

# 搜索
results = ms.recall("API 格式")
for r in results:
    print(r['text'])
```

### 记忆分类

| 分类 | 代码 | 用途 | 默认重要性 |
|------|------|------|-----------|
| 决策 | decision | 不可违反的架构原则 | 0.95 |
| 事实 | fact | 错误修复、已知问题 | 0.90 |
| 反思 | reflection | 技术学习、方法论 | 0.80 |
| 偏好 | preference | 用户偏好 | 0.70 |
| 实体 | entity | 球队/联赛/API信息 | 0.70 |
| 其他 | other | 通用记忆 | 0.70 |

### 记忆更新规则

- **修复 bug 后**: 立即在 `LEARNINGS.md` 追加 ERR 条目, 然后 `python memory.py init` 重新导入
- **架构决策后**: 立即在 `LEARNINGS.md` 追加 DEC 条目
- **技术发现后**: 立即在 `LEARNINGS.md` 追加 LRN 条目
- **遇到问题时**: 先 `python memory.py recall "<关键词>"` 查找是否已有解决方案

---

*代码许可: 本项目基于 [MIT License](LICENSE) 开源 (Copyright (c) 2026 yiyievil)，可自由使用、修改、分发。*

*免责声明: 本系统仅供研究参考, 不构成投注建议。所有预测基于体彩(sporttery.cn)竞彩足球官方规则。*
