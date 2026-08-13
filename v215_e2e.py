#!/usr/bin/env python3
"""
竞彩足球预测 Ultra 5.0 — 端到端全自动取数脚本
输入: 体彩场次编号 (如 "203","204","205")
输出: JSON 结构化预测结论

流程:
  用户输入场次编号 → sporttery.cn API获取赔率+队名 → 500.com首页自动匹配fixture_id
  → 并行获取500.com数据(ouzhi+shuju) → 七步预测 → JSON输出

Ultra 5.0 升级 (数学算法全面优化, EV不变, 命中率优先):
  1. 负二项分布 (Negative Binomial): 替代Poisson+过离散修正hack, 天然建模进球过离散
  2. Elo评级概率: 第4个概率源, 基于球队统计+近况, 独立于赔率
  3. 自适应校准: 平局目标根据总进球期望动态调整(低分→高平局, 高分→低平局)
  4. 含平局近况分析: 指数衰减权重现在计入平局(半胜半负), 不再忽略~25%比赛信息
  5. 四源集成融合: 市场 + Power + 校准Poisson + Elo (原三源→四源)
  6. 自适应离散参数r: 低分比赛r=8(强过离散), 高分比赛r=12(弱过离散)
  7. 共享DC矩阵函数: 消除compute_cross_market_value中重复计算
  8. Dixon-Coles低分修正保留: 修正0-0/1-0/0-1/1-1的得分依赖性
  9. Logit变换校准: 边界稳定, 对称性好
  10. 对数空间集成融合: 几何加权保持概率锐度
Ultra 11.13/11.14 (让平覆盖双选): 让平概率大时体现在HAD双选兜底, 不单推让平 —
  让平高发窗口触发 → 强制HAD双选=让平侧+平(让球盘→胜平, 受让盘→平负),
  覆盖"让平+平局+让平侧HAD方向"三大类, 让平场双选命中率100%(单选0/20必失)
"""
import math, json, re, time, os, sys
from datetime import datetime, timedelta
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import sqlite3

# 记忆系统 v2.0: 预测前自动召回铁律和相关记忆
try:
    from memory import MemoryStore
    _mem = MemoryStore()
except Exception:
    _mem = None

# nowscore 辅助数据源 (为体彩预测提供统计增强; 500.com为降级备用)
# 架构: Sporttery(体彩)是核心预测目标, nowscore/500.com均服务于体彩预测

# ============================================================
# 🔒 数据源优先级策略 (锁定 — 禁止修改)
# ============================================================
# 所有者指令 (2026-07-28): 以下层级为系统硬性约束, 任何改动
# (代码/逻辑/配置) 都不得违反, 如需变更必须由所有者明确批准:
#
#   1. sporttery(体彩)实时数据 = 绝对核心, 不可替换不可绕过
#      - 预测范围由体彩在售场次决定, 无体彩开盘不预测
#      - HAD/HHAD赔率 + 固定奖金 = 预测基准, 每次预测/更新必须实时抓取
#   2. nowscore = 主力辅助数据源, 不得随意禁用
#      - 统计增强(三合一盘口/近况/交锋/积分)默认必须尝试 nowscore
#   3. 500.com = 降级备用, 仅在 nowscore 实在抓不到时才允许使用
#      - 每场 500.com 数据必须带有 fallback_reason 降级凭证
#   4. sporttery(保底) = 最终兜底 (nowscore+500双失败时)
#
# 违反此策略的代码改动视为 bug。
DATA_SOURCE_POLICY = {
    'core': 'sporttery',          # 绝对核心, 不可更改
    'primary_aux': 'nowscore',    # 主力辅助, 不得随意禁用
    'fallback': '500.com',        # 仅 nowscore 失败时
    'last_resort': 'sporttery(保底)',
    'locked': True,               # 🔒 锁定标志
}

def _check_data_source_policy(all_data):
    """数据源策略运行时自检 (锁定策略的强制验证)

    规则: 凡 data_source='500.com' 的场次, 必须存在 fallback_reason
    (证明 nowscore 已被尝试过且失败), 否则打印违规警告。
    仅告警不中断 — 预测流程完整性优先。
    """
    violations = []
    n_nowscore = n_500 = n_fallback = 0
    for key, d in all_data.items():
        ds = d.get('data_source', '') if isinstance(d, dict) else ''
        if ds == 'nowscore' or ds.startswith('nowscore'):
            n_nowscore += 1
        elif ds == '500.com':
            n_500 += 1
            if not d.get('fallback_reason'):
                violations.append(key)
        elif '保底' in ds:
            n_fallback += 1
    print(f"  [策略自检🔒] sporttery核心 | nowscore {n_nowscore}场 | "
          f"500.com降级 {n_500}场 | 保底 {n_fallback}场")
    if violations:
        print(f"  [策略自检🔒] ⚠️ 违规: {violations} 使用了500.com但无nowscore失败凭证!")
    return violations

try:
    from nowscore_fetch import fetch_nowscore_match_data
    NOWSCORE_AVAILABLE = True
except ImportError:
    # 🔒 策略要求 nowscore 不得随意禁用: 导入失败时按策略重试一次
    # (可能是工作目录/sys.path 问题), 仍失败才降级并显著告警
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from nowscore_fetch import fetch_nowscore_match_data
        NOWSCORE_AVAILABLE = True
    except ImportError:
        NOWSCORE_AVAILABLE = False
        print("  ⚠️⚠️ [策略告警] nowscore_fetch 模块导入失败, 主力辅助数据源不可用!")
        print("  ⚠️⚠️ 按锁定策略这将全部降级500.com, 请立即检查 nowscore_fetch.py 是否存在/可导入")

# ============================================================
# Phase 0: 用户输入
# ============================================================
TARGET_DATE = None   # ← 不限日期(避免跨天分类问题)
TARGET_WEEKDAY = "周日"  # ← 指定周几过滤(如"周四"), None=不过滤
MATCH_NUMBERS = ["001","002","003","004","005","006","007","008","009","010","011","012","013","014","015","016","017","018","019","020","021","022","023","024"]  # ← 场次编号(后3位)

# ===== 工作模式 (Ultra 8.1) =====
# predict: 全新预测 — 所有数据重新拉取, 不读缓存, 完成后写入缓存
# update:  更新预测 — 重新拉取数据, 与上次预测比对, 根据变化调整结果
PRED_MODE = 'predict'

# ===== 编号日期输入 (Ultra 7.3) =====
# 竞彩官网(sporttery.cn/jc/jsq/zqspf)编号日期格式: 260728 = 2026-07-28
# 命令行: python v215_e2e.py 260728 001,002
#     或: python v215_e2e.py 260728001,260728002
# 收到编号日期后自动换算周几并开始预测, 无需手动改配置
_WEEKDAY_CN = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

def parse_code_date(code):
    """编号日期 '260728' → (date对象, '周二'); 非法输入返回 (None, None)"""
    m = re.match(r'^(\d{2})(\d{2})(\d{2})$', str(code).strip())
    if not m:
        return None, None
    try:
        d = datetime(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
    except ValueError:
        return None, None
    return d, _WEEKDAY_CN[d.weekday()]

def apply_cli_mode():
    """解析 --mode predict|update 参数, 同时剥离 --mode/--force 等标志 (Ultra 8.1)

    用法: python v215_e2e.py 260731 001,002 --mode update
          python v215_e2e.py 260731 001,002 --mode=predict --force
    """
    global PRED_MODE
    _argv_filtered = []
    _flags_to_strip = {'--force'}  # 这些标志保留在 _flags_seen 中供后续检查, 但不传入 match_input
    _flags_seen = set()
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == '--mode' and i + 1 < len(sys.argv):
            mode = sys.argv[i + 1].strip().lower()
            if mode in ('predict', 'update'):
                PRED_MODE = mode
            i += 2
            continue
        if arg.startswith('--mode='):
            mode = arg.split('=', 1)[1].strip().lower()
            if mode in ('predict', 'update'):
                PRED_MODE = mode
            i += 1
            continue
        if arg in _flags_to_strip:
            _flags_seen.add(arg)
            i += 1
            continue
        _argv_filtered.append(arg)
        i += 1
    # 恢复 --force 等标志到 argv 末尾 (供 inject_memory_context 检查)
    sys.argv = [sys.argv[0]] + _argv_filtered + sorted(_flags_seen)


def apply_cli_match_input():
    """命令行编号日期输入 → 覆盖 TARGET_WEEKDAY / MATCH_NUMBERS (无参数时用顶部配置)
    
    支持格式:
      260728001,260728002          → 完整编号
      260728 001,002               → 编号日期+场次
      260801周六001-003             → 编号日期+周几+范围 (Ultra 11.1)
      260801周六001,002,003        → 编号日期+周几+场次 (Ultra 11.1)
      260801 周六 001-016           → 编号日期+周几+范围 (带空格, Ultra 11.2)
    """
    global TARGET_WEEKDAY, MATCH_NUMBERS
    args = [a for a in sys.argv[1:] if a.strip()]
    if not args:
        return
    text = ' '.join(args).replace('，', ',').replace('、', ',').strip()

    # 先尝试去除空格匹配: 260801 周六 001-016 → 260801周六001-016
    text_no_space = text.replace(' ', '')

    # 形式3 (Ultra 11.1): 编号日期+周几+范围 260801周六001-003
    m3 = re.match(r'^(\d{6})(周[一二三四五六日])(\d{3})-(\d{3})$', text_no_space)
    if m3:
        d, wd = parse_code_date(m3.group(1))
        if wd and wd == m3.group(2):
            start, end = int(m3.group(3)), int(m3.group(4))
            TARGET_WEEKDAY = wd
            MATCH_NUMBERS = [f"{i:03d}" for i in range(start, end + 1)]
            print(f"  [输入] {m3.group(1)}{wd} {MATCH_NUMBERS[0]}-{MATCH_NUMBERS[-1]} → {wd}, 场次 {MATCH_NUMBERS}")
            return
    m3b = re.match(r'^(\d{6})(周[一二三四五六日])([0-9,]+)$', text_no_space)
    if m3b:
        d, wd = parse_code_date(m3b.group(1))
        if wd and wd == m3b.group(2):
            nums = sorted(set([x.strip()[-3:] for x in m3b.group(3).split(',') if x.strip()]))
            TARGET_WEEKDAY = wd
            MATCH_NUMBERS = nums
            print(f"  [输入] {m3b.group(1)}{wd} {nums} → {wd}, 场次 {MATCH_NUMBERS}")
            return

    # 形式1: 完整编号 260728001,260728002 (同日多场)
    full = re.findall(r'(\d{6})(\d{3})', text_no_space)
    if full and len(full) >= 1 and len({f[0] for f in full}) == 1 and \
       ''.join(f[0] + f[1] for f in full) == text_no_space.replace(',', ''):
        d, wd = parse_code_date(full[0][0])
        if wd:
            TARGET_WEEKDAY = wd
            MATCH_NUMBERS = [f[1] for f in full]
            print(f"  [输入] 编号日期 {full[0][0]} → {wd}, 场次 {MATCH_NUMBERS}")
            return
    # 形式2: 编号日期+场次 260728 001,002
    m = re.match(r'^(\d{6})\s+([0-9,\s]+)$', text)
    if m:
        d, wd = parse_code_date(m.group(1))
        nums = [x[-3:] for x in re.split(r'[,\s]+', m.group(2).strip()) if x]
        if wd and nums:
            TARGET_WEEKDAY = wd
            MATCH_NUMBERS = nums
            print(f"  [输入] 编号日期 {m.group(1)} → {wd}, 场次 {MATCH_NUMBERS}")
            return
    print(f"  [输入] ⚠️ 无法解析 '{text}', 回退文件顶部配置 "
          f"(正确格式: 260728 001,002 或 260728001,260728002 或 260801周六001-003)")


def inject_memory_context():
    """记忆系统 v2.0: 预测前自动召回铁律 + 相关记忆 + 编号验证

    在 main() 中 apply_cli_match_input() 之后调用,
    根据 MATCH_NUMBERS 和 TARGET_DATE/TARGET_WEEKDAY 构建完整编号,
    注入铁律和相关记忆到预测上下文。
    """
    if _mem is None:
        print("  [记忆] ⚠️ 记忆系统未加载, 跳过自动召回")
        return

    # 构建完整编号列表 (YYMMDD + NNN)
    match_ids = []
    if TARGET_DATE:
        date_prefix = TARGET_DATE.strftime('%y%m%d')
    elif TARGET_WEEKDAY:
        # 从 TARGET_WEEKDAY 反推日期: 用今天往前找匹配的周几
        today = datetime.now().date()
        wd_map = {'周一': 0, '周二': 1, '周三': 2, '周四': 3, '周五': 4, '周六': 5, '周日': 6}
        target_wd = wd_map.get(TARGET_WEEKDAY)
        if target_wd is not None:
            days_diff = (target_wd - today.weekday()) % 7
            d = today + timedelta(days=days_diff)
            date_prefix = d.strftime('%y%m%d')
        else:
            date_prefix = datetime.now().strftime('%y%m%d')
    else:
        date_prefix = datetime.now().strftime('%y%m%d')

    for num in MATCH_NUMBERS:
        full_id = f"{date_prefix}{num.zfill(3)}"
        match_ids.append(full_id)

    # 用户输入文本 (用于关键词召回)
    user_input = f"预测 {TARGET_WEEKDAY or ''} {' '.join(match_ids)}"

    # 获取预测上下文
    context = _mem.get_prediction_context(match_ids=match_ids, user_input=user_input)
    print(context)

    # 检查是否有编号相关的警告 (每编号只提示一次, Bug-2修复)
    for mid in match_ids:
        validation = _mem.validate_match_id(mid)
        if any('RULE-001' in w or 'RULE-003' in w or '记忆库已有此编号记录' in w
               for w in validation.get('warnings', [])):
            print(f"  [记忆] ⚠️ 编号 {mid} 有历史纠正记录, 请确认编号正确!")

    # Ultra 7.6: 纠错记录硬中止 — 编号曾被用户纠正(importance>=0.8)时中止预测
    hard = _mem.get_correction_records(match_ids)
    if hard:
        print("\n  [记忆] ❌ 检测到历史纠错记录, 中止预测:")
        for mid, texts in hard.items():
            for t in texts:
                print(f"    - {mid}: {t}")
        print("  [记忆] 请确认编号无误后在命令行追加 --force 强制继续")
        if '--force' not in sys.argv:
            return 'ABORT'
        print("  [记忆] --force 已指定, 继续预测")

    return context

# ===== 推荐模式配置 (Pro 3.9/Ultra 1.0) =====
# mode='prob':  命中率优先(默认), 纯概率排序, EV仅作参考
# mode='ev':    EV优先, 主推期望值最高
# mode='prob': 命中率第一优先, EV仅作展示参考 (用户铁律)
# 用户明确: "推荐的命中率为第一优先而不是看EV! 足球不是抛硬币, 每场几乎都是独立!"
# 足球每场独立, EV是重复投注概念, 对单场预测意义有限; 命中率(prob)才是方向判断依据
RECOMMEND_MODE = 'prob'   # 命中率优先 (唯一支持模式; ev/hybrid 已移除 Ultra 11.33)
HYBRID_PROB_TOLERANCE = 3.0  # 单位: 概率百分点 (胜率误差带, 用于平局盲区触发判定)

# ===== SWOT 自动获取开关 (Ultra 6.5) =====
# True: 预测完成后自动发现leisu情报卡片→获取SWOT→融合回预测文件
#       leisu无覆盖的场次用500/nowscore统计数据型情报兜底
# False: 跳过 (保留手动 swot_fast_v3.py + swot_fusion_v3.py 流程)
AUTO_SWOT = True

# ===== Ultra 12.0: 十项模型升级开关 (model_upgrades.py) =====
# 全部为增量安全升级: 数据/参数缺失时自动回退原逻辑, 控制台标注 [升级] 状态
UPGRADES = {
    'robust_goal_line':   True,   # 1 盘口加权中位数 + 初终盘Kalman混合
    'odds_calibration':   True,   # 2 赔率→概率 isotonic 校准 (历史库训练)
    'glicko2_form':       True,   # 3 Glicko-2 近况评分 (含不确定性降权)
    'h2h_shrink':         True,   # 4 对赛小样本贝塔收缩
    'dc_lambda':          True,   # 5 DC攻防强度λ (与市场λ混合)
    'bivariate_poisson':  True,   # 6 二元泊松比分矩阵 (共同冲击λ3)
    'learned_fusion':     True,   # 7 融合权重历史Brier学习
    'hhad_same_source':   True,   # 8 HHAD与比分矩阵同源 (随升级6自动生效)
    'draw_window_model':  True,   # 9 平局窗口 logistic 概率化
    'conf_ece':           True,   # 10 置信度 ECE 校准
}
BP_RHO = 0.12          # 二元泊松共同冲击系数 (0.1~0.15 经验区间)
DC_LAMBDA_BLEND = 0.35 # DCλ 与市场λ 的混合权重 (DC占35%)

# ===== Ultra 12.1: 双选达标池阈值 (用户铁律: 命中率达标后兼顾盈利) =====
# 双选概率≥此值即满足"命中率第一"底线, 达标池内按赔率最高选。
# 用户确认: 让胜让平76%已足够稳, 此时应选赔率更优组合而非盲目追求最高概率。
DOUBLE_QUALIFY_PROB = 75.0
try:
    import model_upgrades as _MU
    _UPG_PARAMS = _MU.load_upgrades()
    _UPG_OK = [k for k, v in UPGRADES.items() if v]
    print(f"  [升级] Ultra 12.0 已加载: {len(_UPG_OK)}项开关, 已训参数: {list(_UPG_PARAMS.keys())}")
except Exception as _e:
    _MU, _UPG_PARAMS = None, {}
    print(f"  [升级] model_upgrades 未加载, 全部回退原逻辑: {_e}")

# ===== Ultra 7.4: 杯赛首回合大比分惩罚 (仅限欧冠/欧罗巴/欧协联等两回合制杯赛) =====
from cup_leg_penalty import get_cup_leg_penalty, clear_cache as clear_leg_cache, is_cup_competition as _is_cup_league

# ============================================================
# Pro 3.1: 5星制置信度系统 (含半星)
# ============================================================
def format_stars(score):
    """将1.0-5.0的分数转为5星制字符串(含半星)
    
    例如:
      5.0 → ★★★★★    4.5 → ★★★★½
      4.0 → ★★★★      3.5 → ★★★½
      3.0 → ★★★        2.5 → ★★½
      2.0 → ★★          1.5 → ★½
      1.0 → ★
    """
    score = round(score * 2) / 2  # 取最近的0.5
    score = max(1.0, min(5.0, score))
    full = int(score)
    half = 1 if (score - full) >= 0.5 else 0
    return '★' * full + ('½' if half else '')

def stars_to_score(stars_str):
    """将星级字符串转回分数 (用于对比/校准)
    
    ★★★★★ → 5.0, ★★★★½ → 4.5, ★★★ → 3.0, ★ → 1.0
    """
    if not stars_str:
        return 0.0
    full_count = stars_str.count('★')
    has_half = '½' in stars_str
    return full_count + (0.5 if has_half else 0.0)

# ============================================================
# Phase 1: sporttery.cn API — 体彩核心: 获取场次+赔率+队名+开赛时间 (预测基准)
# ============================================================
SPORTTERY_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getMatchListV1.qry?clientCode=3001"
SPORTTERY_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://www.sporttery.cn/jc/jsq/zqspf/',
    'Accept': 'application/json',
}

HEADERS_500 = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://odds.500.com/',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}

def fetch_with_retry(url, headers, timeout=10, max_retries=2, encoding=None, params=None):
    """带指数退避重试的HTTP GET (Ultra-Opt: 降超时10s、重试2次)

    Pro 3.0: 替代直接 requests.get, 提升取数稳定性
    - 429 (Too Many Requests): 指数退避 2s/4s
    - Timeout: 线性退避 1s
    - ConnectionError: 线性退避 2s
    - params: 可选查询参数 (用于sporttery结果API)
    """
    r = None
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=headers, timeout=timeout, params=params)
            if encoding:
                r.encoding = encoding
            if r.status_code == 200:
                return r
            elif r.status_code == 429:
                wait = 2 ** attempt * 2  # 2s, 4s, 8s
                time.sleep(wait)
                continue
            elif r.status_code in (403, 503, 567):
                # Ultra-Opt: WAF拦截 (TencentEdgeOne等), 退避重试而非直接返回错误页
                # 旧版直接返回错误响应, 调用方 r.json() 崩溃且报错信息无意义
                wait = 2 ** attempt * 2
                time.sleep(wait)
                continue
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))
                continue
            raise
        except requests.exceptions.ConnectionError:
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    if r is not None and r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} after {max_retries} retries: {url[:100]}")
    return r

def fetch_sporttery_matches(match_numbers, target_date=None):
    """从 sporttery.cn API 获取指定场次的赔率和基本信息
    Key格式: '周X编号' (如 '周四201')，避免不同天同号覆盖
    target_date: 若指定则只保留该日期的比赛
    
    若比赛已结束(getMatchListV1不再返回), 自动回退到结果API获取赔率
    """
    try:
        r = fetch_with_retry(SPORTTERY_URL, SPORTTERY_HEADERS)
        data = r.json()
    except Exception as e:
        print(f"  [错误] sporttery 主API请求/解析失败: {e}")
        return {}
    
    matches = {}
    for mi in (data.get('value') or {}).get('matchInfoList', []) or []:
        weekday = mi.get('weekday', '')
        for s in mi.get('subMatchList', []) or []:
            try:
                full_num = str(s.get('matchNum', ''))
                match_num = full_num[-3:] if full_num else ''
                
                if match_num not in match_numbers:
                    continue
                
                match_date = s.get('matchDate', '')
                # 日期过滤 (统一为字符串比较, 避免 date 对象与字符串恒不等)
                if target_date and match_date != str(target_date):
                    continue
                
                # Key: 周X+编号 (如 '周四201')
                key = f"{weekday}{match_num}"
                # 同键去重保护: 若已有更晚的 matchDate 则跳过, 否则覆盖
                if key in matches and matches[key].get('match_date', '') >= match_date:
                    continue
                
                had = hhad = {}
                had_in_list = False  # Ultra 7.10: 跟踪体彩API是否实际返回HAD盘口
                for o in s.get('oddsList', []) or []:
                    if o.get('poolCode') == 'HAD':
                        had = {'h': float(o.get('h') or 0), 'd': float(o.get('d') or 0),
                               'a': float(o.get('a') or 0)}
                        had_in_list = True
                    elif o.get('poolCode') == 'HHAD':
                        hhad = {'h': float(o.get('h') or 0), 'd': float(o.get('d') or 0),
                                'a': float(o.get('a') or 0),
                                'goalLine': float(o.get('goalLine', 0) or 0)}

                matches[key] = {
                    'match_num': match_num,
                    'full_num': full_num,
                    'weekday': weekday,
                    'key': key,
                    'match_id': s.get('matchId'),
                    'league': s.get('leagueAbbName', ''),
                    'home': s.get('homeTeamAbbName', ''),
                    'away': s.get('awayTeamAbbName', ''),
                    'match_date': match_date,
                    'match_time': s.get('matchTime', ''),
                    'HAD': had,
                    'HHAD': hhad,
                    'had_in_list': had_in_list,  # Ultra 7.10: HAD是否在体彩开盘列表
                    'data_source': 'sporttery',  # 核心赔率来源
                }
            except Exception as _e:
                print(f"  [错误] 解析场次 {s.get('matchNum', '?')} 失败, 跳过: {_e}")
                continue
    
    # 周几过滤: 如果指定了TARGET_WEEKDAY, 只保留该周几的比赛
    if TARGET_WEEKDAY:
        matches = {k: v for k, v in matches.items() if v.get('weekday', '') == TARGET_WEEKDAY}
        if not matches:
            print(f"  [过滤] match list API中无{TARGET_WEEKDAY}的比赛编号 {match_numbers}")

    # 回退: 如果match list API没找到, 从结果API获取(仅限已完赛场次)
    if not matches:
        # 从编号日期解析目标日期 (如 260729 → 2026-07-29)
        _target_date_str = None
        _cli_args = [a for a in sys.argv[1:] if a.strip()]
        if _cli_args:
            _text = ' '.join(_cli_args).replace('，', ',').replace('、', ',').strip()
            _full = re.findall(r'(\d{6})(\d{3})', _text)
            if _full:
                _d, _ = parse_code_date(_full[0][0])
                if _d:
                    _target_date_str = _d.strftime('%Y-%m-%d')
            else:
                _m = re.match(r'^(\d{6})\s+', _text)
                if _m:
                    _d, _ = parse_code_date(_m.group(1))
                    if _d:
                        _target_date_str = _d.strftime('%Y-%m-%d')
        
        if _target_date_str:
            print(f"  [回退] 从结果API获取 (目标日期: {_target_date_str})...")
            matches = fetch_sporttery_matches_from_results(match_numbers, target_date=_target_date_str)
            # 结果API已在±1天窗口内查询, 优先按周几前缀匹配(如"周一201"匹配TARGET_WEEKDAY="周一")
            # 若周几匹配有结果则直接采用; 否则回退到精确日期匹配
            if matches:
                _before = len(matches)
                if TARGET_WEEKDAY:
                    _weekday_matches = {k: v for k, v in matches.items()
                                        if v.get('weekday', '') == TARGET_WEEKDAY}
                    if _weekday_matches:
                        matches = _weekday_matches
                        print(f"  [回退] 结果API按周几匹配({TARGET_WEEKDAY}): {len(matches)}场")
                    else:
                        # 周几不匹配时, 尝试精确日期匹配
                        _date_matches = {k: v for k, v in matches.items()
                                   if v.get('match_date', '') == _target_date_str}
                        if _date_matches:
                            matches = _date_matches
                            print(f"  [回退] 结果API精确匹配 {_target_date_str}: {len(matches)}场")
                        else:
                            # 周几和精确日期都不匹配 → 中止预测 (Ultra 7.6 / Bug-1修复)
                            # 旧逻辑: 保留结果继续预测, 导致预测了其他日期可能已完赛的比赛
                            _wd_list = [v.get('weekday','') for v in matches.values()]
                            _md_list = [v.get('match_date','') for v in matches.values()]
                            print(f"  [回退] ❌ 编号 {match_numbers} 在 {TARGET_WEEKDAY}({_target_date_str}) 不存在, 中止预测!")
                            print(f"         结果API匹配到的实为: 周几={set(_wd_list)}, 比赛日={set(_md_list)}")
                            print(f"         如确需预测这些比赛, 请用对应编号日期重新输入")
                            matches = {}  # 清空, 触发下方"可用编号"提示并中止
                else:
                    print(f"  [回退] 结果API匹配: {len(matches)}场")
        else:
            print(f"  [回退] 无法解析目标日期, 从结果API获取最近7天...")
            matches = fetch_sporttery_matches_from_results(match_numbers)
        
        # 最终检查: 仍然没找到 → 列出当日可用编号
        if not matches:
            _available_nums = []
            try:
                _r = fetch_with_retry(SPORTTERY_URL, SPORTTERY_HEADERS)
                _data = _r.json()
                for mi in _data['value']['matchInfoList']:
                    if mi.get('weekday', '') == TARGET_WEEKDAY:
                        for s in mi['subMatchList']:
                            _available_nums.append(str(s['matchNum'])[-3:])
            except:
                pass
            if _available_nums:
                print(f"  [错误] 比赛编号 {match_numbers} 在 {TARGET_WEEKDAY} 不存在!")
                print(f"  [提示] {TARGET_WEEKDAY}可用编号: {', '.join(sorted(_available_nums))}")
            else:
                print(f"  [错误] 未找到比赛编号 {match_numbers}, 且{TARGET_WEEKDAY}无可用比赛")

    return matches


# 竞彩官方计算器API (含单关标识/排名/赔率趋势标志, getMatchListV1没有这些字段)
SPORTTERY_CALC_URL = "https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry?clientCode=3001"


def enrich_sporttery_extra(matches):
    """用官方计算器API补充: 单关标识 bettingSingle / 联赛排名 / 赔率趋势标志

    趋势标志: 官方对每个赔率给出涨跌标记 (1=升 0=平 -1=降),
    可与我们基于快照的趋势推断交叉校验 (借鉴 SportteryAPI parse.ts)
    """
    if not matches:
        return
    try:
        r = fetch_with_retry(SPORTTERY_CALC_URL, SPORTTERY_HEADERS)
        data = r.json()
    except Exception:
        return

    def _tr(flag):
        return {'1': '↑', '0': '→', '-1': '↓'}.get(str(flag or '').strip(), '')

    extra = {}
    for mi in data.get('value', {}).get('matchInfoList', []):
        for s in mi.get('subMatchList', []):
            had = s.get('had') or {}
            extra[s.get('matchId')] = {
                'betting_single': str(s.get('bettingSingle', '0')) == '1',
                'home_rank': s.get('homeRank') or '',
                'away_rank': s.get('awayRank') or '',
                'had_trend': f"{_tr(had.get('hf'))}{_tr(had.get('df'))}{_tr(had.get('af'))}",
            }
    n = 0
    for m in matches.values():
        e = extra.get(m.get('match_id'))
        if e:
            m.update(e)
            n += 1
    if n:
        print(f"  [增强] 单关标识/排名/赔率趋势: {n}场")


def fetch_sporttery_matches_from_results(match_numbers, target_date=None):
    """从sporttery结果API获取已完场比赛的赔率数据(回退方案)
    当getMatchListV1不再返回已完场比赛时使用

    target_date: 若指定 (如 '2026-07-29'), 仅查询该日期±1天的结果
                 避免7天窗口内取到错误日期的同编号比赛
    """
    RESULT_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry"
    RESULT_HEADERS = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://www.lottery.gov.cn/jc/zqsgkj/',
        'Accept': 'application/json',
    }

    # 查询范围: 有目标日期时仅查±1天, 否则查最近7天
    if target_date:
        try:
            td = datetime.strptime(target_date, '%Y-%m-%d')
            date_begin = (td - timedelta(days=1)).strftime('%Y-%m-%d')
            date_end = (td + timedelta(days=1)).strftime('%Y-%m-%d')
            print(f"  [结果API] 按目标日期查询: {date_begin} ~ {date_end}")
        except ValueError:
            today = datetime.now()
            date_begin = (today - timedelta(days=7)).strftime('%Y-%m-%d')
            date_end = today.strftime('%Y-%m-%d')
    else:
        today = datetime.now()
        date_begin = (today - timedelta(days=7)).strftime('%Y-%m-%d')
        date_end = today.strftime('%Y-%m-%d')
    
    all_results = {}
    page = 1
    while True:
        params = {
            'matchBeginDate': date_begin,
            'matchEndDate': date_end,
            'leagueId': '',
            'pageSize': '30',
            'pageNo': str(page),
            'isFix': '0',
            'matchPage': '1',
            'pcOrWap': '1',
        }
        r = fetch_with_retry(RESULT_URL, RESULT_HEADERS, params=params)
        data = r.json()
        val = data.get('value', {})
        results = val.get('matchResult', [])
        if not results:
            break
        for m in results:
            key = m.get('matchNumStr', '')
            if key:
                # Ultra 6.5: 同一编号(如"周日201")在7天窗口内可能出现两次(上周日+本周日),
                # 必须保留 matchDate 最新的一条 (旧版直接覆盖, 后翻页的旧数据会顶掉新数据,
                # 导致停售回退拿到上周的同编号比赛)
                old = all_results.get(key)
                if old is None or str(m.get('matchDate', '')) >= str(old.get('matchDate', '')):
                    all_results[key] = m
        total_pages = val.get('pages', 1)
        if page >= total_pages:
            break
        page += 1
    
    # 筛选目标比赛
    matches = {}
    for key, m in all_results.items():
        # 从matchNumStr提取编号 (如 "周四201" → "201")
        match_num = key[-3:] if len(key) >= 3 else ''
        if match_num not in match_numbers:
            continue
        
        weekday = key[:-3] if len(key) > 3 else ''
        full_num = str(m.get('matchNum', ''))
        
        had = {}
        hhad = {}
        try:
            had = {'h': float(m.get('h') or 0), 'd': float(m.get('d') or 0), 'a': float(m.get('a') or 0)}
        except:
            pass
        # HHAD让球赔率: 结果API可能含hhadH/hhadD/hhadA字段, 否则留空(后续getFixedBonusV1会补充)
        try:
            _hhad_h = float(m.get('hhadH') or 0)
            _hhad_d = float(m.get('hhadD') or 0)
            _hhad_a = float(m.get('hhadA') or 0)
            _gl = float(m.get('goalLine', 0) or 0)
            if _hhad_h > 0:
                hhad = {'h': _hhad_h, 'd': _hhad_d, 'a': _hhad_a, 'goalLine': _gl}
            else:
                hhad = {'goalLine': _gl}  # 仅有让球数, 赔率由getFixedBonusV1补充
        except:
            pass
        
        matches[key] = {
            'match_num': match_num,
            'full_num': full_num,
            'weekday': weekday,
            'key': key,
            'match_id': m.get('matchId'),
            'league': m.get('leagueNameAbbr', ''),
            'home': m.get('homeTeam', ''),
            'away': m.get('awayTeam', ''),
            'match_date': m.get('matchDate', ''),
            'match_time': '',
            'HAD': had,
            'HHAD': hhad,
        }
        print(f"    结果API: {key} {matches[key]['home']} vs {matches[key]['away']} | HAD={had.get('h','')}/{had.get('d','')}/{had.get('a','')}")
    
    return matches


def fetch_sporttery_fixed_bonus(match_id):
    """获取竞彩官方固定奖金 (Ultra 6.5) — 比分/总进球/半全场赔率

    端点: getFixedBonusV1.qry (纯requests, 通用, 无需登录)
    用途: 竞彩 CRS(比分)/TTG(总进球)/HAFU(半全场) 官方赔率,
          与模型概率做 EV 价值分析 — 这才是实际投注时的赔率。

    返回: {'ttg': {0..7: odds}, 'hafu': {'胜胜'..'负负': odds},
           'crs': {'1-0': odds, ...}, 'crs_other': {'胜其他': odds,...}} 或 None
    """
    if not match_id:
        return None
    url = "https://webapi.sporttery.cn/gateway/uniform/football/getFixedBonusV1.qry"
    try:
        r = fetch_with_retry(url, SPORTTERY_HEADERS, params={'clientCode': '3001', 'matchId': str(match_id)}, timeout=10)
        v = r.json().get('value', {}).get('oddsHistory') or {}
        if not v:
            return None

        out = {}

        # HAD 胜平负终赔+初赔 (从hadList提取)
        # 初赔=第一条(seq=0), 终赔=最新一条(最后)
        had_list = v.get('hadList') or []
        if had_list:
            # 终赔
            had_latest = had_list[-1]
            had_h = float(had_latest.get('h', 0)) if had_latest.get('h') else 0
            had_d = float(had_latest.get('d', 0)) if had_latest.get('d') else 0
            had_a = float(had_latest.get('a', 0)) if had_latest.get('a') else 0
            if had_h > 0:
                out['had'] = {'h': had_h, 'd': had_d, 'a': had_a}
            # 初赔 (第一条)
            had_first = had_list[0]
            had_init_h = float(had_first.get('h', 0)) if had_first.get('h') else 0
            had_init_d = float(had_first.get('d', 0)) if had_first.get('d') else 0
            had_init_a = float(had_first.get('a', 0)) if had_first.get('a') else 0
            if had_init_h > 0:
                out['had_init'] = {'h': had_init_h, 'd': had_init_d, 'a': had_init_a}

        # HHAD 让球胜平负终赔+初赔 (从hhadList提取)
        hhad_list = v.get('hhadList') or []
        if hhad_list:
            # 终赔
            hhad_latest = hhad_list[-1]
            hhad_h = float(hhad_latest.get('h', 0)) if hhad_latest.get('h') else 0
            hhad_d = float(hhad_latest.get('d', 0)) if hhad_latest.get('d') else 0
            hhad_a = float(hhad_latest.get('a', 0)) if hhad_latest.get('a') else 0
            gl = float(hhad_latest.get('goalLine', 0) or 0)
            if hhad_h > 0:
                out['hhad'] = {'h': hhad_h, 'd': hhad_d, 'a': hhad_a, 'goalLine': gl}
            # 初赔 (第一条)
            hhad_first = hhad_list[0]
            hhad_init_h = float(hhad_first.get('h', 0)) if hhad_first.get('h') else 0
            hhad_init_d = float(hhad_first.get('d', 0)) if hhad_first.get('d') else 0
            hhad_init_a = float(hhad_first.get('a', 0)) if hhad_first.get('a') else 0
            if hhad_init_h > 0:
                out['hhad_init'] = {'h': hhad_init_h, 'd': hhad_init_d, 'a': hhad_init_a, 'goalLine': gl}

        # TTG 总进球: s0..s7 (7=7+球), f后缀为停售标记
        ttg_raw = (v.get('ttgList') or [{}])[0]
        ttg = {}
        for k in range(8):
            val = ttg_raw.get(f's{k}')
            if val and float(val) > 1:
                ttg[k] = float(val)
        if ttg:
            out['ttg'] = ttg

        # HAFU 半全场: hh/hd/ha/dh/dd/da/ah/ad/aa (前=半场结果, 后=全场结果; h主胜 d平 a客胜)
        hafu_raw = (v.get('hafuList') or [{}])[0]
        hafu_map = {'hh': '胜胜', 'hd': '胜平', 'ha': '胜负',
                    'dh': '平胜', 'dd': '平平', 'da': '平负',
                    'ah': '负胜', 'ad': '负平', 'aa': '负负'}
        hafu = {}
        for code, name in hafu_map.items():
            val = hafu_raw.get(code)
            if val and float(val) > 1:
                hafu[name] = float(val)
        if hafu:
            out['hafu'] = hafu

        # CRS 比分: s{主}sa{客}格式为 s01s00=1-0 (两位数字), s-1sh/s-1sd/s-1sa=胜/平/负其他
        crs_raw = (v.get('crsList') or [{}])[0]
        crs, crs_other = {}, {}
        other_map = {'s-1sh': '胜其他', 's-1sd': '平其他', 's-1sa': '负其他'}
        for code, val in crs_raw.items():
            if code.endswith('f') or not val:
                continue
            try:
                fval = float(val)
            except (ValueError, TypeError):
                continue
            if fval <= 1:
                continue
            if code in other_map:
                crs_other[other_map[code]] = fval
            else:
                m = re.match(r's(\d{2})s(\d{2})$', code)
                if m:
                    crs[f"{int(m.group(1))}-{int(m.group(2))}"] = fval
        if crs:
            out['crs'] = crs
        if crs_other:
            out['crs_other'] = crs_other

        return out if out else None
    except Exception:
        return None


# ============================================================
# Phase 2: 500.com 首页 — 自动匹配 fixture_id
# ============================================================
def fetch_500_fixture_ids():
    """从500.com赔率首页提取 场次编号→fixture_id 映射
    Key格式: '周X编号' (如 '周四201')，与sporttery的key一致
    匹配逻辑: 每个fid链接前最近的'周X编号'即为对应场次
    
    若赔率首页无已完场比赛, 回退到直播页面提取
    """
    fixture_map = fetch_500_fixture_ids_from_odds()
    if not fixture_map:
        print("  [回退] 赔率首页无数据, 从直播页面获取fixture_id...")
        fixture_map = fetch_500_fixture_ids_from_live()
    return fixture_map


def fetch_500_fixture_ids_from_odds():
    """从500.com赔率首页提取fixture_id映射"""
    r = fetch_with_retry('https://odds.500.com/', HEADERS_500, encoding='gb2312')
    html = r.text
    
    # 收集所有 周X编号 和 shuju-fid 的位置
    events = []
    for m in re.finditer(r'周[一二三四五六日]\d{3}', html):
        events.append((m.start(), 'num', m.group()))
    for m in re.finditer(r'shuju-(\d+)\.shtml', html):
        events.append((m.start(), 'fid', m.group(1)))
    
    events.sort()
    
    # 每个 fid 前最近的 num 即为对应场次
    fixture_map = {}
    last_num = None
    for pos, typ, val in events:
        if typ == 'num':
            last_num = val  # 如 '周四201'
        elif typ == 'fid' and last_num:
            fixture_map[last_num] = int(val)
    
    return fixture_map


def fetch_500_fixture_ids_from_live():
    """从500.com直播页面提取fixture_id映射(已完场比赛回退方案)"""
    try:
        r = fetch_with_retry('https://live.500.com/', HEADERS_500, encoding='gb2312')
        html = r.text
    except Exception as e:
        print(f"  直播页面请求失败: {e}")
        return {}
    
    fixture_map = {}
    # 直播页HTML中: 周X编号 后面有 fid="XXX" 属性
    # 模式: ...周四201...fid="1362264"...
    for m in re.finditer(r'(周[一二三四五六日]\d{3}).*?fid="(\d+)"', html, re.DOTALL):
        key = m.group(1)
        fid = int(m.group(2))
        if key not in fixture_map:
            fixture_map[key] = fid
    
    return fixture_map

# ============================================================
# Phase 3: 500.com 并行数据获取 (ouzhi + shuju)
# ============================================================
def fetch_ouzhi_json(fid):
    """HTTP获取500.com百家欧指JSON
    注意: 临近开赛时API可能返回返还率(0.xx)而非赔率(>1.0)
    此时从shuju页面的"平均"欧指获取实际赔率
    """
    url = f"https://odds.500.com/fenxi/json/ouzhi.php?fid={fid}&cid=&type=0"
    r = fetch_with_retry(url, HEADERS_500)
    arrays = re.findall(r'\[([0-9.]+),([0-9.]+),([0-9.]+),"([0-9:\-\s]+)"\]', r.text.strip())
    if not arrays:
        return None
    latest = arrays[0]
    initial = arrays[-1]
    latest_w, latest_d, latest_l = float(latest[0]), float(latest[1]), float(latest[2])
    init_w, init_d, init_l = float(initial[0]), float(initial[1]), float(initial[2])
    
    # 检测是否为返还率（值<1.0）而非赔率（值>1.0）
    is_return_rate = latest_w < 1.0 or latest_d < 1.0 or latest_l < 1.0
    
    return {
        'latest_w': latest_w, 'latest_d': latest_d, 'latest_l': latest_l,
        'init_w': init_w, 'init_d': init_d, 'init_l': init_l,
        'count': len(arrays),
        'change_w': latest_w - init_w,
        'is_return_rate': is_return_rate,  # 标记是否为返还率格式
    }

def fetch_shuju_page(fid):
    """HTTP获取500.com数据页面并解析关键信息"""
    url = f"https://odds.500.com/fenxi/shuju-{fid}.shtml"
    r = fetch_with_retry(url, HEADERS_500, encoding='gb2312')
    html = r.text
    html_clean = re.sub(r'<[^>]+>', '', html)
    info = {}
    
    # 近况走势
    form = re.findall(r'近况走势\s*-\s*([WLD]{4,12})', html_clean)
    if form:
        info['form_home'] = form[0]
        info['form_away'] = form[1] if len(form) > 1 else ''
    
    # 推介
    rec = re.findall(r'推介\s*-\s*([^\s]+)\s+([赢贏输])', html_clean)
    if rec:
        info['recommendation'] = f"{rec[0][0]} {rec[0][1]}"
    else:
        rec2 = re.findall(r'推介\s*-\s*(\S+)', html_clean)
        if rec2:
            info['recommendation'] = rec2[0]
    
    # 近10场战绩
    stats = re.findall(r'([\u4e00-\u9fa5A-Za-z·]{2,15})近10场战绩(\d+)胜(\d+)平(\d+)负进(\d+)球失(\d+)球', html_clean)
    if stats:
        for s in stats:
            name = s[0].strip()
            if name and len(name) < 15:
                info[f'stats_{name}'] = {'W': int(s[1]), 'D': int(s[2]), 'L': int(s[3]),
                                          'gf': int(s[4]), 'ga': int(s[5]),
                                          'avg_gf': round(int(s[4])/10, 1), 'avg_ga': round(int(s[5])/10, 1)}
    
    # 对赛成绩
    h2h = re.findall(r'对赛成绩\s*-\s*\S+\s*(\d+)胜(\d+)和(\d+)负', html_clean)
    if h2h:
        info['h2h'] = f"{h2h[0][0]}胜{h2h[0][1]}和{h2h[0][2]}负"
    
    # 评语
    comment_patterns = [
        r'([\u4e00-\u9fa5，。、]{15,80}(?:取胜|获胜|赢球|不败|大胜|失利|输球|失分))',
        r'((?:坐镇|此番|此行|有力)[^<]{10,60}(?:取胜|获胜|赢|不败))',
    ]
    for pattern in comment_patterns:
        comment = re.findall(pattern, html_clean)
        if comment:
            info['comment'] = comment[0].strip()
            break
    
    # 平均欧指 (从shuju页面提取，作为ouzhi API的备用)
    # 格式: "平均 1.85 3.40 3.57" 或类似
    avg_odds = re.findall(r'平均\s*([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)', html_clean)
    if avg_odds:
        # 取最后一组（通常是最终平均欧指）
        last = avg_odds[-1]
        w, d, l = float(last[0]), float(last[1]), float(last[2])
        if w > 1.0 and d > 1.0 and l > 1.0:  # 确保是赔率格式(>1.0)
            info['avg_odds'] = {'w': w, 'd': d, 'l': l}
    
    return info

def fetch_daxiao_goal_line(fid):
    """从500.com大小球页面提取即时盘口（goal line）
    
    解析大小球HTML页面，提取各博彩公司的即时盘口和初始盘口，
    使用众数（最常见值）作为市场盘口。
    
    返回:
        {
            'goal_line': float,        # 即时盘口（众数）
            'initial_goal_line': float, # 初始盘口（众数）
            'avg_goal_line': float,     # 平均值盘口
            'over_odds': float,         # 即时大球赔率均值
            'under_odds': float,        # 即时小球赔率均值
            'source': str,              # 数据来源描述
            'all_goal_lines': list,     # 各盘口值及出现次数
            'num_bookmakers': int,      # 博彩公司数量
        }
    """
    url = f'https://odds.500.com/fenxi/daxiao-{fid}.shtml'
    r = fetch_with_retry(url, HEADERS_500, encoding='gb2312')
    html = r.text
    
    html_clean = re.sub(r'<[^>]+>', ' ', html)
    html_clean = re.sub(r'\s+', ' ', html_clean)
    
    # ===== 提取各博彩公司即时盘口 =====
    # 格式: [大赔率][↑↓] [盘口] [小赔率][↑↓] [日期] [时间]
    # 盘口值: 2, 2.5, 2/2.5, 2.5/3, 3, 3/3.5 等
    all_goal_lines = []
    all_over_odds = []
    all_under_odds = []
    all_initial_lines = []
    
    def parse_goal_line_str(gl_str):
        """解析盘口字符串: 2.5→2.5, 2/2.5→2.25, 2.5/3→2.75"""
        if '/' in gl_str:
            parts = gl_str.split('/')
            return (float(parts[0]) + float(parts[1])) / 2
        return float(gl_str)
    
    # 即时盘口: 带箭头的赔率 + 盘口 + 带箭头的赔率 + 日期时间
    instant_pattern = re.compile(
        r'([0-9]\.?[0-9]*)[↑↓]?\s+'
        r'([0-9](?:\.5)?(?:/[0-9](?:\.5)?)?)\s+'
        r'([0-9]\.?[0-9]*)[↑↓]?\s+'
        r'\d{2}-\d{2}\s+\d{2}:\d{2}'
    )
    
    for m in instant_pattern.finditer(html_clean):
        over_odds = float(m.group(1))
        goal_line_str = m.group(2)
        under_odds = float(m.group(3))
        goal_line = parse_goal_line_str(goal_line_str)
        
        if 0.5 <= goal_line <= 6.0:
            all_goal_lines.append(goal_line)
            all_over_odds.append(over_odds)
            all_under_odds.append(under_odds)
    
    # 初始盘口: 无箭头赔率 + 盘口 + 无箭头赔率 + 日期时间
    init_section = html_clean.split('初始大小')
    if len(init_section) > 1:
        init_text = init_section[1][:5000]
        initial_pattern = re.compile(
            r'([0-9]\.?[0-9]*)\s+'
            r'([0-9](?:\.5)?(?:/[0-9](?:\.5)?)?)\s+'
            r'([0-9]\.?[0-9]*)\s+'
            r'\d{2}-\d{2}\s+\d{2}:\d{2}'
        )
        for m in initial_pattern.finditer(init_text):
            goal_line_str = m.group(2)
            goal_line = parse_goal_line_str(goal_line_str)
            if 0.5 <= goal_line <= 6.0:
                all_initial_lines.append(goal_line)
    
    # ===== 从"平均值"提取 =====
    avg_match = re.search(
        r'平均值\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)',
        html_clean
    )
    avg_goal_line = float(avg_match.group(2)) if avg_match else None
    
    # ===== 选择最终盘口 =====
    if all_goal_lines:
        counter = Counter(all_goal_lines)
        mode_goal_line = counter.most_common(1)[0][0]
        avg_over = sum(all_over_odds) / len(all_over_odds)
        avg_under = sum(all_under_odds) / len(all_under_odds)
        init_mode = Counter(all_initial_lines).most_common(1)[0][0] if all_initial_lines else mode_goal_line

        # 升级1: 加权中位数盘口 + 初终盘Kalman混合 (对离群盘口稳健)
        # 众数易被单家异常盘口带偏; 中位数稳健, Kalman按临场信息量加权初/终盘
        if _MU and UPGRADES.get('robust_goal_line'):
            try:
                _robust_gl = _MU.robust_goal_line(all_goal_lines)
                if _robust_gl:
                    mode_goal_line = _robust_gl
                if all_initial_lines:
                    _robust_init = _MU.robust_goal_line(all_initial_lines)
                    if _robust_init:
                        init_mode = _robust_init
                # Kalman混合后吸附到0.25标准盘口 (下游over_prob仅识别.0/.25/.5/.75)
                _blended = _MU.kalman_blend_goal_line(init_mode, mode_goal_line)
                mode_goal_line = round(_blended * 4) / 4
            except Exception:
                pass

        return {
            'goal_line': mode_goal_line,
            'initial_goal_line': init_mode,
            'avg_goal_line': avg_goal_line or round(sum(all_goal_lines)/len(all_goal_lines), 2),
            'over_odds': round(avg_over, 2),
            'under_odds': round(avg_under, 2),
            'source': f'500.com大小球({len(all_goal_lines)}家)',
            'all_goal_lines': sorted(counter.items()),
            'num_bookmakers': len(all_goal_lines),
        }
    elif avg_goal_line:
        return {
            'goal_line': round(avg_goal_line, 2),
            'initial_goal_line': round(avg_goal_line, 2),
            'avg_goal_line': round(avg_goal_line, 2),
            'source': '500.com平均值',
            'all_goal_lines': [],
            'num_bookmakers': 0,
        }
    else:
        return {
            'goal_line': 2.5,
            'initial_goal_line': 2.5,
            'avg_goal_line': 2.5,
            'source': '默认值',
            'all_goal_lines': [],
            'num_bookmakers': 0,
        }

# ============================================================
# Phase 3b: 500.com 初赔数据提取 (AJAX HTML端点)
# 通过 /fenxi1/ 端点获取各博彩公司的即时+初盘数据
# ============================================================
def _parse_handicap_table(t_html):
    """解析亚指/大小球数据表格, 返回 {handicap, over, under, text} 或 None"""
    trs = re.findall(r'<tr[^>]*>(.*?)</tr>', t_html, re.DOTALL)
    if not trs:
        return None
    row = trs[0]
    td_matches = re.findall(r'<td([^>]*)>(.*?)</td>', row, re.DOTALL)
    if len(td_matches) < 3:
        return None
    # 上水/大球 (水位可能是2-3位小数)
    over_text = re.sub(r'<[^>]+>', '', td_matches[0][1]).strip()
    over_val = re.match(r'(\d+\.\d{2,3})', over_text)
    if not over_val:
        return None  # 表头行
    # 盘口: ref属性在td标签上
    ref_match = re.search(r'ref="([+-]?\d+\.?\d*)"', td_matches[1][0])
    if not ref_match:
        return None
    # 下水/小球
    under_text = re.sub(r'<[^>]+>', '', td_matches[2][1]).strip()
    under_val = re.match(r'(\d+\.\d{2,3})', under_text)
    if not under_val:
        return None
    handicap_text = re.sub(r'<[^>]+>', '', td_matches[1][1]).strip()
    return {
        'handicap': float(ref_match.group(1)),
        'handicap_text': handicap_text,
        'over': float(over_val.group(1)),
        'under': float(under_val.group(1)),
    }

def fetch_initial_ouzhi(fid):
    """从500.com AJAX端点提取百家欧指即时+初盘 (胜/平/负)

    使用 /fenxi1/ouzhi.php?chupan=1 端点，HTML表格中:
    第一行=即时欧指, 第二行=初盘欧指
    过滤交易所(赔率<1.0), 只保留传统博彩公司

    返回: {
        'avg_instant': (w,d,l), 'avg_initial': (w,d,l),
        'change_w', 'change_d', 'change_l',  # 即时-初盘的变化
        'num_valid': int, 'companies': [...]
    } 或 None
    """
    url = f'https://odds.500.com/fenxi1/ouzhi.php?id={fid}&chupan=1&ctype=0&start=0&r=1&style=0&guojia=0&currentIndex=0'
    r = fetch_with_retry(url, HEADERS_500, encoding='gb2312')
    html = r.text

    company_positions = [(m.start(), m.group(1)) for m in re.finditer(r'<span class="quancheng"[^>]*>([^<]+)</span>', html)]
    table_positions = [(m.start(), m.group(1)) for m in re.finditer(r'<table[^>]*class="pl_table_data"[^>]*>(.*?)</table>', html, re.DOTALL)]

    companies = []
    for i, (comp_pos, comp_name) in enumerate(company_positions):
        next_pos = company_positions[i+1][0] if i+1 < len(company_positions) else len(html)
        for t_pos, t_html in table_positions:
            if t_pos > comp_pos and t_pos < next_pos:
                trs = re.findall(r'<tr[^>]*>(.*?)</tr>', t_html, re.DOTALL)
                if len(trs) >= 2:
                    instant_vals = re.findall(r'<td[^>]*>(\s*\d+\.\d{2}\s*)</td>', trs[0])
                    initial_vals = re.findall(r'<td[^>]*>(\s*\d+\.\d{2}\s*)</td>', trs[1])
                    if len(instant_vals) == 3 and len(initial_vals) == 3:
                        inst = tuple(float(v) for v in instant_vals)
                        init = tuple(float(v) for v in initial_vals)
                        if all(v > 1.0 for v in inst + init):
                            companies.append({
                                'company': comp_name,
                                'instant': inst, 'initial': init
                            })
                        break

    n = len(companies)
    if n == 0:
        return None

    avg_inst = tuple(sum(c['instant'][i] for c in companies)/n for i in range(3))
    avg_init = tuple(sum(c['initial'][i] for c in companies)/n for i in range(3))

    return {
        'avg_instant': avg_inst,
        'avg_initial': avg_init,
        'change_w': round(avg_inst[0] - avg_init[0], 3),
        'change_d': round(avg_inst[1] - avg_init[1], 3),
        'change_l': round(avg_inst[2] - avg_init[2], 3),
        'num_valid': n,
    }

def fetch_initial_yazhi(fid):
    """从500.com AJAX端点提取亚指(让球)即时+初盘

    返回: {
        'instant': {handicap_mode, over_avg, under_avg},
        'initial': {handicap_mode, over_avg, under_avg},
        'num_valid': int, 'companies': [...]
    } 或 None
    """
    url = f'https://odds.500.com/fenxi1/yazhi.php?id={fid}&chupan=1&ctype=0&start=0&r=1&style=0&guojia=0&currentIndex=0'
    r = fetch_with_retry(url, HEADERS_500, encoding='gb2312')
    html = r.text

    company_positions = [(m.start(), m.group(1)) for m in re.finditer(r'<span class="quancheng"[^>]*>([^<]+)</span>', html)]
    table_positions = [(m.start(), m.group(1)) for m in re.finditer(r'<table[^>]*class="pl_table_data"[^>]*>(.*?)</table>', html, re.DOTALL)]

    companies = []
    for i, (comp_pos, comp_name) in enumerate(company_positions):
        next_pos = company_positions[i+1][0] if i+1 < len(company_positions) else len(html)
        data_tables = []
        for t_pos, t_html in table_positions:
            if t_pos > comp_pos and t_pos < next_pos:
                parsed = _parse_handicap_table(t_html)
                if parsed:
                    data_tables.append(parsed)
        if len(data_tables) >= 2:
            companies.append({
                'company': comp_name,
                'instant': data_tables[0], 'initial': data_tables[1]
            })

    n = len(companies)
    if n == 0:
        return None

    inst_h = [c['instant']['handicap'] for c in companies]
    init_h = [c['initial']['handicap'] for c in companies]

    return {
        'instant': {
            'handicap_mode': Counter(inst_h).most_common(1)[0][0],
            'over_avg': round(sum(c['instant']['over'] for c in companies)/n, 3),
            'under_avg': round(sum(c['instant']['under'] for c in companies)/n, 3),
        },
        'initial': {
            'handicap_mode': Counter(init_h).most_common(1)[0][0],
            'over_avg': round(sum(c['initial']['over'] for c in companies)/n, 3),
            'under_avg': round(sum(c['initial']['under'] for c in companies)/n, 3),
        },
        'num_valid': n,
    }

def fetch_initial_daxiao(fid):
    """从500.com AJAX端点提取大小球即时+初盘

    返回: {
        'instant': {goal_line_mode, over_avg, under_avg},
        'initial': {goal_line_mode, over_avg, under_avg},
        'num_valid': int, 'companies': [...]
    } 或 None
    """
    url = f'https://odds.500.com/fenxi1/daxiao.php?id={fid}&chupan=1&ctype=0&start=0&r=1&style=0&guojia=0&currentIndex=0'
    r = fetch_with_retry(url, HEADERS_500, encoding='gb2312')
    html = r.text

    company_positions = [(m.start(), m.group(1)) for m in re.finditer(r'<span class="quancheng"[^>]*>([^<]+)</span>', html)]
    table_positions = [(m.start(), m.group(1)) for m in re.finditer(r'<table[^>]*class="pl_table_data"[^>]*>(.*?)</table>', html, re.DOTALL)]

    companies = []
    for i, (comp_pos, comp_name) in enumerate(company_positions):
        next_pos = company_positions[i+1][0] if i+1 < len(company_positions) else len(html)
        data_tables = []
        for t_pos, t_html in table_positions:
            if t_pos > comp_pos and t_pos < next_pos:
                parsed = _parse_handicap_table(t_html)
                if parsed:
                    data_tables.append(parsed)
        if len(data_tables) >= 2:
            companies.append({
                'company': comp_name,
                'instant': data_tables[0], 'initial': data_tables[1]
            })

    n = len(companies)
    if n == 0:
        return None

    inst_l = [c['instant']['handicap'] for c in companies]
    init_l = [c['initial']['handicap'] for c in companies]

    return {
        'instant': {
            'goal_line_mode': Counter(inst_l).most_common(1)[0][0],
            'over_avg': round(sum(c['instant']['over'] for c in companies)/n, 3),
            'under_avg': round(sum(c['instant']['under'] for c in companies)/n, 3),
        },
        'initial': {
            'goal_line_mode': Counter(init_l).most_common(1)[0][0],
            'over_avg': round(sum(c['initial']['over'] for c in companies)/n, 3),
            'under_avg': round(sum(c['initial']['under'] for c in companies)/n, 3),
        },
        'num_valid': n,
    }

def fetch_one_match(match_num, match_info, fixture_id):
    """获取单场比赛全量500.com数据 (含初赔)
    
    Ultra-Opt: 6个独立HTTP请求并行 (旧版串行 6×10s=60s → 并行 ≈10s)
    """
    result = {**match_info, 'fixture_id': fixture_id}
    
    def _safe(fn):
        """安全调用, 异常返回None"""
        try:
            return fn(fixture_id)
        except Exception:
            return None
    
    # 6个请求全部并行 (仅依赖fixture_id, 无相互依赖)
    with ThreadPoolExecutor(max_workers=6) as pool:
        fut_ouzhi = pool.submit(fetch_ouzhi_json, fixture_id)
        fut_shuju = pool.submit(fetch_shuju_page, fixture_id)
        fut_daxiao = pool.submit(fetch_daxiao_goal_line, fixture_id)
        fut_init_ouzhi = pool.submit(_safe, fetch_initial_ouzhi)
        fut_init_yazhi = pool.submit(_safe, fetch_initial_yazhi)
        fut_init_daxiao = pool.submit(_safe, fetch_initial_daxiao)
        
        try:
            result['ouzhi'] = fut_ouzhi.result()
        except Exception as e:
            result['ouzhi'] = None
            result['ouzhi_error'] = str(e)
        try:
            result['shuju'] = fut_shuju.result()
        except Exception as e:
            result['shuju'] = {}
            result['shuju_error'] = str(e)
        try:
            result['daxiao'] = fut_daxiao.result()
        except Exception as e:
            result['daxiao'] = {'goal_line': 2.5, 'source': '默认值(异常)', 'all_goal_lines': [], 'num_bookmakers': 0}
            result['daxiao_error'] = str(e)
        result['init_ouzhi'] = fut_init_ouzhi.result()
        result['init_yazhi'] = fut_init_yazhi.result()
        result['init_daxiao'] = fut_init_daxiao.result()
    
    return match_num, result

# ============================================================
# Phase 4: 七步预测计算
# ============================================================
def poisson(k, lam):
    """泊松分布概率"""
    if k < 0:
        return 0.0
    if lam <= 0:
        # 边界防护: 负/零 λ 无意义, 与 negbin_pmf 语义一致
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def negbin_pmf(k, lam, r=10.0):
    """负二项分布概率 (Ultra 5.0 — 替代 Poisson+过离散修正hack)

    原理: Poisson假设方差=均值, 但足球进球方差>均值(过离散)。
    Ultra 4.0用 overdispersion_correction 乘修正因子, 是近似hack。
    负二项分布天然建模过离散: Var = λ + λ²/r
      r→∞ 时退化为 Poisson (无过离散)
      r=10 时 Var = λ + λ²/10 ≈ 1.1λ (温和过离散)
      r=5  时 Var = λ + λ²/5  ≈ 1.2λ (强过离散)

    PMF: P(X=k) = C(k+r-1, k) × (r/(r+λ))^r × (λ/(r+λ))^k
    用Gamma函数避免大数阶乘: C(k+r-1,k) = Γ(k+r)/(Γ(k+1)×Γ(r))

    优势(相比Poisson+overdispersion_correction):
      1. 理论统一: 一个分布同时建模均值和方差, 无需后处理修正
      2. 参数可解释: r直接控制过离散程度
      3. 边界稳定: k=0时 P=（r/(r+λ))^r, 比Poisson的e^(-λ)更合理(高估0球)
      4. 尾部更厚: 高进球概率比Poisson更大, 更符合足球实际

    参数:
      k: 进球数
      lam: 进球期望 (λ)
      r: 离散参数 (默认10, 足球典型值8-15)
    """
    if k < 0 or lam <= 0:
        return 0.0 if lam > 0 else (1.0 if k == 0 else 0.0)
    p = r / (r + lam)
    # 用对数计算避免数值溢出
    log_coeff = math.lgamma(k + r) - math.lgamma(k + 1) - math.lgamma(r)
    log_pmf = log_coeff + r * math.log(p) + k * math.log(1 - p)
    return math.exp(log_pmf)

def skellam_pmf(k, lam_h, lam_a):
    """Skellam分布 — 两组独立泊松变量之差的概率 (Ultra 6.0)

    P(home - away = k) 直接给出净胜球概率, 用于HHAD让球预测。
    比遍历8×8比分矩阵更精确(覆盖所有进球数, 不截断在7球)。
    """
    if lam_h <= 0 or lam_a <= 0:
        # 边界防护: 任一 λ ≤ 0 时退化为确定性分布
        if lam_h <= 0 and lam_a <= 0:
            return 1.0 if k == 0 else 0.0
        lam_pos = lam_h if lam_h > 0 else lam_a
        if lam_h <= 0:  # 主队不进球: P(k) = Poisson(-k, lam_a)
            return poisson(-k, lam_pos)
        else:           # 客队不进球: P(k) = Poisson(k, lam_h)
            return poisson(k, lam_pos)
    # P(X-Y=k) = exp(-(λ₁+λ₂)) × (λ₁/λ₂)^(k/2) × I_|k|(2√(λ₁λ₂))
    # I_k = 修正贝塞尔函数第一类
    try:
        bessel_val = _iv(abs(k), 2 * math.sqrt(lam_h * lam_a))
    except Exception:
        bessel_val = 0.0
    log_base = -(lam_h + lam_a)
    if lam_a > 0:
        log_base += (k / 2.0) * math.log(lam_h / lam_a)
    return math.exp(log_base) * bessel_val

def _iv(n, x):
    """修正贝塞尔函数第一类 I_n(x) — 级数展开(足球k通常≤5, 收敛快)"""
    if x < 0:
        return 0.0
    if x == 0:
        return 1.0 if n == 0 else 0.0
    # I_n(x) = Σ_{m=0}^∞ (x/2)^{2m+n} / (m! × Γ(m+n+1))
    # 用 lgamma 计算阶乘, 支持任意 n (避免 n≥15 时 math.factorial 溢出/截断)
    half_x = x / 2.0
    term = (half_x ** n) / math.exp(math.lgamma(n + 1))
    result = term
    for m in range(1, 30):
        term *= (half_x * half_x) / (m * (m + n))
        result += term
        if abs(term) < 1e-15:
            break
    return result

def compute_dc_matrix(lam_h, lam_a, use_negbin=True, use_dc=True, league=None):
    """计算比分概率矩阵 (Ultra 9.3 — 10×10矩阵 + 负二项 + Dixon-Coles)

    被 compute_scores 和 compute_cross_market_value 共享调用。

    Ultra 9.3 改进:
      - 矩阵从8×8扩展到10×10 (0-9球), 捕获更多尾部分布概率
      - 高比分比赛(总λ≥3.5)时精度显著提升, 7+概率不再被截断低估

    Ultra 9.2: 支持联赛特定离散参数r (数据驱动)

    Ultra 5.0 改进:
      - 用 negbin_pmf 替代 poisson + overdispersion_correction
      - 负二项分布天然建模过离散, 无需后处理修正
      - Dixon-Coles低分修正保留(修正0-0/1-0/0-1/1-1的依赖性)

    流程:
      1. 负二项基础概率 P(i,j) = NB(i,λ_h) × NB(j,λ_a)
      2. Dixon-Coles低分修正: τ修正 0-0/1-0/0-1/1-1
      3. 归一化

    参数:
      use_negbin: True=负二项, False=回退Poisson(兼容)
      use_dc: 是否应用Dixon-Coles修正
      league: 联赛名, 用于联赛特定r参数
    返回: {f"{i}-{j}": probability, ...} 归一化后的概率字典
    """
    probs = {}
    # Ultra 9.2: 联赛特定离散参数r (数据驱动)
    # 优先使用联赛特定r, 回退自适应启发式
    total_lam = lam_h + lam_a
    r_param = LEAGUE_R_PARAM.get(league, 0)
    if r_param <= 0:
        # 回退: 根据总进球期望自适应 (Ultra 5.0)
        r_param = max(6.0, min(14.0, 10.0 + (total_lam - 2.5) * 1.5))

    # Ultra 9.3: 矩阵扩展到10×10 (0-9球), 更好捕获高比分尾部分布
    _MAX_GOALS = 10
    for i in range(_MAX_GOALS):
        for j in range(_MAX_GOALS):
            if use_negbin:
                p = negbin_pmf(i, lam_h, r=r_param) * negbin_pmf(j, lam_a, r=r_param)
            else:
                p = poisson(i, lam_h) * poisson(j, lam_a)
            probs[f"{i}-{j}"] = p

    # 归一化
    total_p = sum(probs.values())
    if total_p > 0:
        for k in probs:
            probs[k] /= total_p

    # Dixon-Coles低分修正
    if use_dc:
        rho = dynamic_dc_rho(lam_h, lam_a)
        if '0-0' in probs:
            probs['0-0'] *= (1 - lam_h * lam_a * rho)
        if '1-0' in probs:
            probs['1-0'] *= (1 + lam_a * rho)
        if '0-1' in probs:
            probs['0-1'] *= (1 + lam_h * rho)
        if '1-1' in probs:
            probs['1-1'] *= (1 - rho)
        total_p = sum(probs.values())
        if total_p > 0:
            for k in probs:
                probs[k] /= total_p

    # 升级6: 二元泊松共同冲击混合 (Karlis & Ntzoufras)
    # 独立泊松假设主客进球无关, 但真实比赛存在"比赛节奏"共同因子:
    # 开放比赛双方进球同涨, 闷战同跌。λ3=rho·min(λh,λa)建模该共同冲击,
    # 0-0/1-1等同分概率上调, 更贴合真实比分联合分布。
    # 与NB+DC结果50/50混合: 保留负二项过离散+DC低分修正, 叠加相关性结构。
    if _MU and UPGRADES.get('bivariate_poisson'):
        try:
            _bp = _MU.bp_matrix(lam_h, lam_a, rho=BP_RHO, max_goals=_MAX_GOALS - 1)
            if _bp:
                for _bi in range(_MAX_GOALS):
                    for _bj in range(_MAX_GOALS):
                        _bk = f"{_bi}-{_bj}"
                        _bpv = _bp.get((_bi, _bj), 0.0)
                        if _bk in probs:
                            probs[_bk] = 0.5 * probs[_bk] + 0.5 * _bpv
                _tp = sum(probs.values())
                if _tp > 0:
                    for _bk in probs:
                        probs[_bk] /= _tp
        except Exception:
            pass

    # Skellam分布计算净胜球概率 (覆盖所有进球数, 不截断)
    # Ultra 9.3: 扩展到-10到10
    margin_probs = {}
    for k in range(-10, 11):
        margin_probs[k] = skellam_pmf(k, lam_h, lam_a)
    # 尾部残差按主客倾向非对称分配
    tail = max(0.0, 1.0 - sum(margin_probs.values()))
    pos_share = lam_h / (lam_h + lam_a) if (lam_h + lam_a) > 0 else 0.5
    margin_probs[11] = tail * pos_share
    margin_probs[-11] = tail * (1 - pos_share)

    return probs, margin_probs

def compute_scores(lam_h, lam_a, goal_line=0, market_goal_line=2.5, top_n=5, use_dc=True, league=None):
    """泊松比分矩阵 — 动态多盘口概率 + 按盘口方向过滤的比分推荐

    Ultra 4.0: 使用共享函数 compute_dc_matrix 计算概率矩阵
    - 过离散修正 (overdispersion)
    - 动态Dixon-Coles低分修正 (dynamic ρ)
    """
    probs, margin_probs = compute_dc_matrix(lam_h, lam_a, use_negbin=True, use_dc=use_dc, league=league)

    sorted_all = sorted(probs.items(), key=lambda x: x[1], reverse=True)

    def total_goals(s):
        return int(s[0]) + int(s[2])

    # 胜平负
    w = sum(p for s, p in probs.items() if int(s[0]) > int(s[2]))
    d = sum(p for s, p in probs.items() if int(s[0]) == int(s[2]))
    l = sum(p for s, p in probs.items() if int(s[0]) < int(s[2]))

    # ===== 动态盘口概率计算 =====
    # 对任意盘口值计算大球概率（含半赢）
    def over_prob(gl):
        """计算给定盘口的大球概率
        X.0: over wins if total >= X+1, push at X
        X.5: over wins if total >= X+1
        X.25: over full if total >= X+1, half if total == X
        X.75: over full if total >= X+2, half if total == X+1
        """
        int_part = int(gl)
        frac = round(gl - int_part, 2)
        if frac == 0.5:
            return sum(p for s, p in probs.items() if total_goals(s) >= int_part + 1)
        elif frac == 0.0:
            return sum(p for s, p in probs.items() if total_goals(s) >= int_part + 1)
        elif frac == 0.25:
            full = sum(p for s, p in probs.items() if total_goals(s) >= int_part + 1)
            half = sum(p for s, p in probs.items() if total_goals(s) == int_part)
            return full + half * 0.5
        elif frac == 0.75:
            full = sum(p for s, p in probs.items() if total_goals(s) >= int_part + 2)
            half = sum(p for s, p in probs.items() if total_goals(s) == int_part + 1)
            return full + half * 0.5
        return sum(p for s, p in probs.items() if total_goals(s) >= int_part + 1)

    # 生成5个盘口: 以market为中心, ±0.5和±0.25
    gl_low  = round(market_goal_line - 0.5, 2)   # market - 0.5
    gl_ml   = round(market_goal_line - 0.25, 2)   # market - 0.25
    gl_main = round(market_goal_line, 2)          # market (主盘口)
    gl_mh   = round(market_goal_line + 0.25, 2)   # market + 0.25
    gl_high = round(market_goal_line + 0.5, 2)    # market + 0.5

    over_main = over_prob(gl_main)
    over_high = over_prob(gl_high)

    # ===== 按主盘口方向过滤比分 =====
    # 主盘口的大球阈值: total >= ceil(gl_main + 0.5) → 向上取整
    main_threshold = int(math.ceil(gl_main))
    if gl_main == int(gl_main):  # 整数盘口 (如3.0): over需要 >= main+1
        main_threshold = int(gl_main) + 1
    else:  # 半球/四分之一盘口: over需要 >= ceil(gl_main)
        main_threshold = int(math.ceil(gl_main))

    big_main = [(s, p) for s, p in sorted_all if total_goals(s) >= main_threshold]
    small_main = [(s, p) for s, p in sorted_all if total_goals(s) < main_threshold]

    # 副盘口(market+0.5)的大球阈值
    high_threshold = int(math.ceil(gl_high))
    if gl_high == int(gl_high):
        high_threshold = int(gl_high) + 1
    else:
        high_threshold = int(math.ceil(gl_high))

    big_high = [(s, p) for s, p in sorted_all if total_goals(s) >= high_threshold]
    small_high = [(s, p) for s, p in sorted_all if total_goals(s) < high_threshold]

    # ===== 确定主推方向并选择比分 =====
    if over_main > 0.5:
        top3_filtered = big_main[:top_n]
        main_dir = '大'
    else:
        top3_filtered = small_main[:top_n]
        main_dir = '小'

    # 副盘口方向
    if over_high > 0.5:
        high_top3 = big_high[:top_n]
        high_dir = '大'
    else:
        high_top3 = small_high[:top_n]
        high_dir = '小'

    # ===== Ultra 12.3: 比分top3平局方向不偏废 =====
    # 根因: Over/Under方向过滤系统性排除平局比分
    # 例: 盘口1.5/2 → 方向"小" → 阈值2 → 1-1(总进球=2)被排除
    # 实证: 周三002 实际1-1, top3仅1-0/0-0/0-1, 平局比分全部缺失
    # 策略: 当Poisson平局概率≥25%时, 确保top3至少包含一个非0-0的平局比分
    # 不影响主盘口方向判断, 仅修正top3展示的平局偏废
    _draw_prob = d  # d = sum(p for s,p in probs.items() if s[0]==s[2])
    _has_draw_in_top3 = any(s[0] == s[2] and s != '0-0' for s, _ in top3_filtered[:3])
    if not _has_draw_in_top3 and _draw_prob >= 0.25:
        # 从完整排序中找到概率最高的非0-0平局比分
        _best_draw = None
        for s, p in sorted_all:
            if s[0] == s[2] and s != '0-0':
                _best_draw = (s, p)
                break
        if _best_draw:
            # 替换top3中概率最低的非平局比分
            _non_draws = [(i, s, p) for i, (s, p) in enumerate(top3_filtered[:3])
                          if s[0] != s[2]]
            if _non_draws:
                _min_idx = min(_non_draws, key=lambda x: x[2])[0]
                top3_filtered[_min_idx] = _best_draw
                # 同步修正high_top3 (如果同样没有平局比分)
                _has_draw_in_high = any(s[0] == s[2] and s != '0-0' for s, _ in high_top3[:3])
                if not _has_draw_in_high:
                    _best_draw_high = None
                    for s, p in sorted_all:
                        if s[0] == s[2] and s != '0-0':
                            _best_draw_high = (s, p)
                            break
                    if _best_draw_high:
                        _non_draws_h = [(i, s, p) for i, (s, p) in enumerate(high_top3[:3])
                                        if s[0] != s[2]]
                        if _non_draws_h:
                            _min_idx_h = min(_non_draws_h, key=lambda x: x[2])[0]
                            high_top3[_min_idx_h] = _best_draw_high

    # HHAD概率: 使用Skellam分布精确计算 (覆盖所有进球数)
    # goal_line: 负=主让, 正=主受; 净胜球+goal_line>0=让胜, =0=让平, <0=让负
    hw = sum(v for k, v in margin_probs.items() if k + goal_line > 0)
    hd = sum(v for k, v in margin_probs.items() if k + goal_line == 0)
    hl = sum(v for k, v in margin_probs.items() if k + goal_line < 0)

    # 升级8: HHAD与比分矩阵同源 — BP开启时改从统一比分矩阵求和。
    # Skellam假设主客进球独立(与比分矩阵的BP共同冲击+DC修正不一致),
    # 同矩阵求和保证HHAD概率与比分/WDL严格自洽, 消除"比分说1-1最热,
    # 让球盘却显示让平概率低"的口径分裂。矩阵已归一化, 三类完备。
    if _MU and UPGRADES.get('hhad_same_source') and UPGRADES.get('bivariate_poisson'):
        try:
            hw = sum(p for s, p in probs.items()
                     if (int(s.split('-')[0]) - int(s.split('-')[1])) + goal_line > 0)
            hd = sum(p for s, p in probs.items()
                     if (int(s.split('-')[0]) - int(s.split('-')[1])) + goal_line == 0)
            hl = sum(p for s, p in probs.items()
                     if (int(s.split('-')[0]) - int(s.split('-')[1])) + goal_line < 0)
        except Exception:
            pass

    # ===== 盘口标签格式化 =====
    def fmt_gl(gl):
        """格式化盘口值: 2.5→2.5, 2.75→2.5/3, 3.0→3, 3.25→3/3.5"""
        int_part = int(gl)
        frac = round(gl - int_part, 2)
        if frac == 0.75:
            return f"{int_part}.5/{int_part+1}"
        elif frac == 0.25:
            return f"{int_part}/{int_part}.5"
        elif frac == 0.0:
            return f"{int_part}"
        else:
            return f"{gl}"

    return {
        'top5_raw': [[s, round(p*100, 1)] for s, p in sorted_all[:top_n]],
        'top3_filtered': [[s, round(p*100, 1)] for s, p in top3_filtered[:3]],
        'high_top3': [[s, round(p*100, 1)] for s, p in high_top3[:3]],
        'poisson_wdl': [round(w*100, 1), round(d*100, 1), round(l*100, 1)],
        'all_probs': dict(probs),  # Ultra 6.11: 完整概率分布供0-0校准使用
        # 动态多盘口概率 (以市场盘口为中心)
        'market_gl': gl_main,
        'market_gl_str': fmt_gl(gl_main),
        'over_low':   round(over_prob(gl_low) * 100, 1),
        'over_ml':    round(over_prob(gl_ml) * 100, 1),
        'over_main':  round(over_main * 100, 1),
        'over_mh':    round(over_prob(gl_mh) * 100, 1),
        'over_high':  round(over_high * 100, 1),
        'gl_low_str':  fmt_gl(gl_low),
        'gl_ml_str':   fmt_gl(gl_ml),
        'gl_main_str': fmt_gl(gl_main),
        'gl_mh_str':   fmt_gl(gl_mh),
        'gl_high_str': fmt_gl(gl_high),
        'main_dir': main_dir,
        'high_dir': high_dir,
        'hhad_wdl': [round(hw*100, 1), round(hd*100, 1), round(hl*100, 1)],
    }

def compute_half_full(lam_h, lam_a, fused_wdl=None, league=None):
    """半全场胜平负概率计算 — 对应体彩第五种玩法 (Ultra 9.3: 联赛特定+HTL效应)

    将全场90分钟分为上半场(45分钟)和下半场(45分钟)，
    分别用负二项分布建模，然后计算9种半全场组合的联合概率。

    Ultra 9.3 四大改进:
      1. 联赛特定半场进球比例 (替代固定0.45, 数据驱动)
      2. 联赛特定离散参数r (替代固定r=15/r=12, 与LEAGUE_R_PARAM一致)
      3. 8×8比分矩阵 (替代6×6, 捕获更多尾部分布)
      4. 半场领先效应(HTL): 领先方下半场λ×0.90, 落后方λ×1.10

    9种组合(体彩官方):
      胜胜/胜平/胜负/平胜/平平/平负/负胜/负平/负负
      前字=上半场主队结果, 后字=全场主队结果

    参数:
      lam_h: 主队全场进球期望
      lam_a: 客队全场进球期望
      fused_wdl: 四源融合的胜平负概率 [p_home, p_draw, p_away]
      league: 联赛名, 用于联赛特定半场比例和r参数

    返回:
      top3: 概率前3的组合字符串 (如 "胜胜:35.2 平胜:18.1 胜平:12.5")
      main: 概率最高的组合 (如 "胜胜(35.2%)")
      all: 9种组合概率字典 {组合: 概率%}
      lam_half: 半场λ (如 "1.0/0.4")
    """
    # ===== Step 1: 联赛特定半场/下半场λ分拆 (Ultra 9.3) =====
    # 历史数据: 半场比例因联赛而异, 挪超0.457 > 英超0.435 > 韩职0.428
    # 回退到全局均值0.45 (与Ultra 5.0一致, 兼容)
    ht_ratio = LEAGUE_HT_RATIO.get(league, 0.45) if league else 0.45
    lam_h_half = lam_h * ht_ratio
    lam_a_half = lam_a * ht_ratio
    lam_h_second = lam_h * (1.0 - ht_ratio)
    lam_a_second = lam_a * (1.0 - ht_ratio)

    # ===== Step 2: 联赛特定离散参数r (Ultra 9.3) =====
    # 半场进球少, 用全场r但放大(过离散弱): r_half = min(20, r * 1.3)
    # 下半场接近全场, 用全场r但略放大: r_second = min(20, r * 1.1)
    total_lam = lam_h + lam_a
    r_base = LEAGUE_R_PARAM.get(league, 0)
    if r_base <= 0:
        r_base = max(6.0, min(14.0, 10.0 + (total_lam - 2.5) * 1.5))
    r_half = min(20.0, r_base * 1.3)
    r_second = min(20.0, r_base * 1.1)

    # ===== Step 3: 8×8比分矩阵 (Ultra 9.3) =====
    # 上半场比分概率矩阵 (0-7球, 8×8覆盖优于旧版6×6)
    ht_probs = {}
    for i in range(8):
        for j in range(8):
            ht_probs[(i, j)] = negbin_pmf(i, lam_h_half, r=r_half) * negbin_pmf(j, lam_a_half, r=r_half)

    # 下半场比分概率矩阵 (无HTL效应时的基线)
    sh_probs_base = {}
    for i in range(8):
        for j in range(8):
            sh_probs_base[(i, j)] = negbin_pmf(i, lam_h_second, r=r_second) * negbin_pmf(j, lam_a_second, r=r_second)

    # Ultra 6.4: 半场矩阵 Dixon-Coles 低分修正 (与全场矩阵同一套τ, 力度一致)
    for probs, lh, la in ((ht_probs, lam_h_half, lam_a_half),
                           (sh_probs_base, lam_h_second, lam_a_second)):
        rho = dynamic_dc_rho(lh, la)
        if (0, 0) in probs:
            probs[(0, 0)] *= (1 - lh * la * rho)
            probs[(1, 0)] *= (1 + la * rho)
            probs[(0, 1)] *= (1 + lh * rho)
            probs[(1, 1)] *= (1 - rho)

    # 归一化
    ht_total = sum(ht_probs.values())
    sh_total = sum(sh_probs_base.values())
    for k in ht_probs:
        ht_probs[k] /= ht_total
    for k in sh_probs_base:
        sh_probs_base[k] /= sh_total

    # ===== Step 4: 半场领先效应(HTL) — Ultra 9.3 =====
    # 足球实证: 半场领先的球队下半场趋于保守, 落后方加强进攻
    # 宏观效果: 领先方下半场λ × 0.90, 落后方λ × 1.10
    # 战术效果: 主队主场领先时更保守(系数0.88), 客队客场领先更激进维持(系数0.95)
    # 下半场基线矩阵已归一化, 这里对每个HT比分状态重新计算下半场矩阵
    # 为避免计算量爆炸(8×8×8×8=4096), 仅对前4×4高概率状态应用HTL,
    # 其余状态使用基线下半场矩阵
    _HTL_HIGH_PROB_THRESHOLD = 0.001  # 仅对HT概率 ≥ 0.1%的状态应用HTL

    # 计算9种半全场组合的联合概率
    combos = {
        '胜胜': 0, '胜平': 0, '胜负': 0,
        '平胜': 0, '平平': 0, '平负': 0,
        '负胜': 0, '负平': 0, '负负': 0,
    }

    for (ht_h, ht_a), ht_p in ht_probs.items():
        if ht_p < 1e-9:
            continue

        # 上半场结果
        if ht_h > ht_a:
            ht_result = '胜'
        elif ht_h == ht_a:
            ht_result = '平'
        else:
            ht_result = '负'

        # 应用HTL: 根据半场比分调整下半场λ
        if ht_h != ht_a and ht_p >= _HTL_HIGH_PROB_THRESHOLD:
            # 领先方保守系数, 落后方激进攻系数
            if ht_h > ht_a:  # 主队领先
                l_lead_h = 0.90  # 主队(领先)保守
                l_trail_a = 1.10  # 客队(落后)激进
            else:  # 客队领先
                l_lead_a = 0.90  # 客队(领先)保守
                l_trail_h = 1.10  # 主队(落后)激进

            # 调整后的下半场λ
            if ht_h > ht_a:
                adj_sh_h = lam_h_second * l_lead_h
                adj_sh_a = lam_a_second * l_trail_a
            else:
                adj_sh_h = lam_h_second * l_trail_h
                adj_sh_a = lam_a_second * l_lead_a

            # 用调整后的λ重新计算下半场矩阵
            sh_probs = {}
            for i in range(8):
                for j in range(8):
                    sh_probs[(i, j)] = negbin_pmf(i, adj_sh_h, r=r_second) * negbin_pmf(j, adj_sh_a, r=r_second)
            # DC修正
            rho = dynamic_dc_rho(adj_sh_h, adj_sh_a)
            if (0, 0) in sh_probs:
                sh_probs[(0, 0)] *= (1 - adj_sh_h * adj_sh_a * rho)
                sh_probs[(1, 0)] *= (1 + adj_sh_a * rho)
                sh_probs[(0, 1)] *= (1 + adj_sh_h * rho)
                sh_probs[(1, 1)] *= (1 - rho)
            sh_total = sum(sh_probs.values())
            if sh_total > 0:
                for k in sh_probs:
                    sh_probs[k] /= sh_total
        else:
            # 半场平局或低概率状态: 使用基线下半场矩阵
            sh_probs = sh_probs_base

        for (sh_h, sh_a), sh_p in sh_probs.items():
            if sh_p < 1e-9:
                continue
            # 全场比分 = 上半场 + 下半场
            ft_h = ht_h + sh_h
            ft_a = ht_a + sh_a
            # 全场结果
            if ft_h > ft_a:
                ft_result = '胜'
            elif ft_h == ft_a:
                ft_result = '平'
            else:
                ft_result = '负'

            combo_key = f"{ht_result}{ft_result}"
            combos[combo_key] += ht_p * sh_p

    # Ultra 6.4: 融合概率边际重加权 (修复"半全场缺平"的推荐层根因)
    if fused_wdl and len(fused_wdl) == 3 and sum(fused_wdl) > 0.5:
        # 重新计算模型全场边际 (用HTL调整后的下半场矩阵)
        model_ft = {'胜': 0.0, '平': 0.0, '负': 0.0}
        for (ht_h, ht_a), ht_p in ht_probs.items():
            if ht_p < 1e-9:
                continue
            # 使用与联合概率一致的下半场矩阵 (含HTL)
            if ht_h != ht_a and ht_p >= _HTL_HIGH_PROB_THRESHOLD:
                if ht_h > ht_a:
                    adj_sh_h = lam_h_second * 0.90
                    adj_sh_a = lam_a_second * 1.10
                else:
                    adj_sh_h = lam_h_second * 1.10
                    adj_sh_a = lam_a_second * 0.90
                sh_probs = {}
                for i in range(8):
                    for j in range(8):
                        sh_probs[(i, j)] = negbin_pmf(i, adj_sh_h, r=r_second) * negbin_pmf(j, adj_sh_a, r=r_second)
                rho = dynamic_dc_rho(adj_sh_h, adj_sh_a)
                if (0, 0) in sh_probs:
                    sh_probs[(0, 0)] *= (1 - adj_sh_h * adj_sh_a * rho)
                if (1, 0) in sh_probs:
                    sh_probs[(1, 0)] *= (1 + adj_sh_a * rho)
                if (0, 1) in sh_probs:
                    sh_probs[(0, 1)] *= (1 + adj_sh_h * rho)
                if (1, 1) in sh_probs:
                    sh_probs[(1, 1)] *= (1 - rho)
                sh_total = sum(sh_probs.values())
                if sh_total > 0:
                    for k in sh_probs:
                        sh_probs[k] /= sh_total
            else:
                sh_probs = sh_probs_base

            for (sh_h, sh_a), sh_p in sh_probs.items():
                if sh_p < 1e-9:
                    continue
                ft_h, ft_a = ht_h + sh_h, ht_a + sh_a
                r = '胜' if ft_h > ft_a else ('平' if ft_h == ft_a else '负')
                model_ft[r] += ht_p * sh_p

        fw = {'胜': fused_wdl[0], '平': fused_wdl[1], '负': fused_wdl[2]}
        for key in combos:
            ft_r = key[1]
            mp = model_ft.get(ft_r, 0)
            if mp > 0.01:
                w = max(0.5, min(2.0, fw[ft_r] / mp))
                combos[key] *= w
        total_c = sum(combos.values())
        if total_c > 0:
            for key in combos:
                combos[key] /= total_c

    # 排序
    sorted_combos = sorted(combos.items(), key=lambda x: x[1], reverse=True)
    top1 = sorted_combos[0]

    return {
        'top3': ' '.join(f"{k}:{p*100:.1f}" for k, p in sorted_combos[:3]),
        'main': f"{top1[0]}({top1[1]*100:.1f}%)",
        'lam_half': f"{lam_h_half:.1f}/{lam_a_half:.1f}",
        'probs': dict(combos),
    }

def compute_total_goals(lam_h, lam_a, ttg_odds=None, league=None, xg_cv_quality=None):
    """总进球数概率计算 — 对应体彩第三种玩法 (Ultra 9.3: 自适应融合+10×10矩阵)

    体彩总进球数共8个选项: 0球/1球/2球/3球/4球/5球/6球/7+球

    Ultra 9.3 三大改进:
      1. 10×10比分矩阵聚合 (0-9球双方), 尾部分布更精确
      2. 7+球概率用负二项CCDF校正残差, 避免截断低估
      3. 自适应TTG融合权重: 基于xG质量+联赛样本量+市场深度动态调整

    Ultra 9.2 三大改进:
      1. 从DC比分矩阵聚合总进球数, 保留Dixon-Coles低分修正(0-0/1-0/0-1/1-1)
      2. 联赛特定自适应离散参数r (数据驱动, 替代固定启发式)
      3. TTG赔率校准: 用竞彩TTG赔率(Shin法)融合市场信息

    参数:
      lam_h: 主队进球期望
      lam_a: 客队进球期望
      ttg_odds: 竞彩TTG赔率字典 {0: 赔率, 1: ..., 7: 7+赔率} 或 None
      league: 联赛名, 用于联赛特定r参数
      xg_cv_quality: xG数据质量 0-1 (越高模型越可信, 市场权重越低)

    返回:
        top3: 概率前3的总进球数 (如 "3球:23.5% 2球:18.4% 1球:14.2%")
        main: 概率最高的总进球数 (如 "3球(23.5%)")
        probs: 8种总进球数概率字典 {0球: 概率%, 1球: ..., 7+球: ...}
        lam_total: 总λ值 (如 "2.8")
        fusion_weight: 模型权重 (如 0.70), 反映自适应融合比例
    """
    # ===== Step 1: 从10×10比分矩阵聚合 =====
    dc_probs, _ = compute_dc_matrix(lam_h, lam_a, use_negbin=True, use_dc=True, league=league)

    goals_probs = {f"{k}球": 0.0 for k in range(7)}
    for score, p in dc_probs.items():
        parts = score.split('-')
        total = int(parts[0]) + int(parts[1])
        if total < 7:
            goals_probs[f"{total}球"] += p

    # 7+球 = 总进球≥7的比分概率之和
    goals_probs["7+球"] = sum(p for s, p in dc_probs.items()
                              if int(s.split('-')[0]) + int(s.split('-')[1]) >= 7)

    # Ultra 9.3: 用负二项CCDF校正7+残差 (10×10矩阵仍截断于9+9=18,
    # 对于极高λ比赛, 用负二项直接计算P(总进球≥7)更精确)
    total_lam = lam_h + lam_a
    r_param = LEAGUE_R_PARAM.get(league, 0)
    if r_param <= 0:
        r_param = max(6.0, min(14.0, 10.0 + (total_lam - 2.5) * 1.5))
    # 负二项 P(总进球 ≥ 7) 用互补CDF
    # 注意: 这里用独立负二项的和的近似, 实际更精确的应用需要卷积,
    # 但作为残差校正足够 — 仅在矩阵聚合概率 < 直接CCDF时上调
    nb_ccdf_7 = 1.0 - sum(negbin_pmf(k, total_lam, r=r_param * 2) for k in range(7))
    # 矩阵聚合的7+概率
    matrix_7plus = goals_probs["7+球"]
    # 如果CCDF > 矩阵聚合, 说明矩阵截断丢失了概率质量, 用CCDF校正
    if nb_ccdf_7 > matrix_7plus * 1.05:
        # 残差校正: 差值的一半加到7+上 (保守校正, 避免过度)
        correction = (nb_ccdf_7 - matrix_7plus) * 0.5
        goals_probs["7+球"] = matrix_7plus + correction
        # 从其他选项按比例扣除校正量
        other_keys = [k for k in goals_probs if k != "7+球"]
        other_total = sum(goals_probs[k] for k in other_keys)
        if other_total > 0:
            scale = 1.0 - correction / other_total
            for k in other_keys:
                goals_probs[k] *= scale

    # 归一化
    total = sum(goals_probs.values())
    if total > 0:
        for k in goals_probs:
            goals_probs[k] /= total

    # ===== Step 2: 自适应TTG赔率校准 (Ultra 9.3) =====
    # 用竞彩TTG赔率通过Shin法提取市场隐含概率
    # 融合权重自适应: 基于xG质量、联赛样本量、市场深度
    # 基线: 模型70% + 市场30%
    f_model = 0.70
    f_market = 0.30

    if ttg_odds and isinstance(ttg_odds, dict) and len(ttg_odds) >= 6:
        ttg_keys = [0, 1, 2, 3, 4, 5, 6, 7]
        odds_list = [ttg_odds.get(k, 0) for k in ttg_keys]
        odds_list = [o for o in odds_list if o > 1]
        if len(odds_list) >= 6:
            market_probs = shin_method(odds_list)
            # 映射回进球标签
            market_map = {}
            valid_keys = [k for k in ttg_keys if ttg_odds.get(k, 0) > 1]
            for i, k in enumerate(valid_keys):
                label = f"{int(k)}球" if int(k) < 7 else "7+球"
                if i < len(market_probs):
                    market_map[label] = market_probs[i]

            if market_map:
                # Ultra 9.3: 自适应融合权重
                # 调整因子1: xG质量 (有高质量xG时更信任模型)
                xg_boost = 0.0
                if xg_cv_quality is not None:
                    xg_boost = min(0.10, xg_cv_quality * 0.12)  # xG质量0.5→+0.06, 0.8→+0.10
                # 调整因子2: 联赛r参数样本量 (更多样本→更信任模型)
                r_sample_boost = 0.0
                _r_n = _LEAGUE_R_N.get(league, 0)
                if _r_n >= 100:
                    r_sample_boost = 0.05
                elif _r_n >= 50:
                    r_sample_boost = 0.03
                # 调整因子3: 市场深度 (有效赔率越多→市场越可信)
                market_depth = len(odds_list)
                if market_depth < 7:
                    f_market_penalty = 0.05  # 赔率不全时降低市场权重
                else:
                    f_market_penalty = 0.0
                # 综合调整: 基线 + 模型增信 - 市场惩罚
                f_model = min(0.85, max(0.55, 0.70 + xg_boost + r_sample_boost - f_market_penalty))
                f_market = 1.0 - f_model

                for label in goals_probs:
                    if label in market_map:
                        goals_probs[label] = goals_probs[label] * f_model + market_map[label] * f_market

                # 重新归一化
                total = sum(goals_probs.values())
                if total > 0:
                    for k in goals_probs:
                        goals_probs[k] /= total

    # ===== Step 3: 体彩TTG池校准 (Ultra 10.3) =====
    # 基于历史数据: 小(0-2球)偏差+2.7pp, 大(4+球)偏差-3.1pp
    # 修正模型倾向: 适当上调小球, 下调大球
    if _POOLS_CALIB:
        ttg_cal = _POOLS_CALIB.get('ttg', {}).get('direction_calibration', {})
        if ttg_cal:
            # 小(0-2球): 实际频率更高 → 上调
            small_bias = ttg_cal.get('小(0-2球)', {}).get('bias_pp', 0)
            # 大(4+球): 实际频率更低 → 下调
            big_bias = ttg_cal.get('大(4+球)', {}).get('bias_pp', 0)
            if abs(small_bias) > 1 and abs(big_bias) > 1:
                # 保守修正: 应用50%偏差量
                small_correction = small_bias / 100.0 * 0.5  # +2.7pp → +0.0135
                big_correction = big_bias / 100.0 * 0.5      # -3.1pp → -0.0155
                # 小(0-2球): 上调
                small_total = sum(goals_probs.get(f"{k}球", 0) for k in range(3))
                if small_total > 0:
                    for k in range(3):
                        label = f"{k}球"
                        goals_probs[label] *= (1.0 + small_correction)
                # 大(4+球): 下调
                big_total = sum(goals_probs.get(f"{k}球", 0) for k in range(4, 8))
                if big_total > 0:
                    for k in range(4, 8):
                        label = f"{k}球" if k < 7 else "7+球"
                        goals_probs[label] *= (1.0 + big_correction)
                # 重新归一化
                total = sum(goals_probs.values())
                if total > 0:
                    for k in goals_probs:
                        goals_probs[k] /= total

    # ===== Step 4: 大小球方向偏差校准 (Ultra 10.4) =====
    # 基于4412场历史数据: 大球偏差-0.6pp (轻微低估)
    # 同时按盘口区间做更精细校准
    if _OVER_UNDER_CALIB:
        ou_overall = _OVER_UNDER_CALIB.get('overall', {})
        over_bias = ou_overall.get('over_bias_pp', 0)
        if abs(over_bias) > 0.3 and ou_overall.get('total_matches', 0) >= 1000:
            # 大球偏差: 负值=模型低估大球→上调大球概率
            correction = over_bias / 100.0 * 0.3  # 保守30%修正
            # 大球(3+球)上调/下调
            big_total = sum(goals_probs.get(f"{k}球", 0) for k in range(3, 8))
            small_total = sum(goals_probs.get(f"{k}球", 0) for k in range(3))
            if big_total > 0 and small_total > 0:
                # bias negative → 大球实际更多 → 上调大球
                big_factor = 1.0 - correction
                small_factor = 1.0 + correction * (big_total / small_total)
                for k in range(3, 8):
                    label = f"{k}球" if k < 7 else "7+球"
                    goals_probs[label] *= big_factor
                for k in range(3):
                    goals_probs[f"{k}球"] *= small_factor
                # 重新归一化
                total = sum(goals_probs.values())
                if total > 0:
                    for k in goals_probs:
                        goals_probs[k] /= total

    # ===== Step 5: 联赛实际进球分布校准 (Ultra 10.4) =====
    # 基于4449场历史数据(42个联赛): 用联赛实际big_rate校准模型预测
    # 适用于有足够样本量(>=30场)的联赛
    if _LEAGUE_PATTERNS_CALIB and league:
        lg_goal_dist = _LEAGUE_PATTERNS_CALIB.get('part1_goal_distribution', {}).get(league)
        if not lg_goal_dist:
            lg_short = re.sub(r'_\d{4}(-\d{2})?$', '', league)
            if lg_short != league:
                lg_goal_dist = _LEAGUE_PATTERNS_CALIB.get('part1_goal_distribution', {}).get(lg_short)
        if lg_goal_dist and lg_goal_dist.get('n', 0) >= 30:
            # 对比模型预测的big_rate(3+球) vs 联赛实际big_rate
            actual_big = lg_goal_dist.get('big_rate_3plus', 0.5)
            pred_big = sum(goals_probs.get(f"{k}球", 0) for k in range(3, 8))
            big_gap = actual_big - pred_big
            # 偏差>=5pp才校准, 保守30%修正
            if abs(big_gap) >= 0.05:
                corr = big_gap * 0.30
                small_total = sum(goals_probs.get(f"{k}球", 0) for k in range(3))
                if big_gap > 0:
                    # 模型低估大球→上调大球
                    big_factor = 1.0 + corr
                    small_factor = 1.0 - corr * (pred_big / small_total) if small_total > 0 else 1.0
                else:
                    # 模型高估大球→下调大球
                    big_factor = 1.0 + corr
                    small_factor = 1.0 - corr * (pred_big / small_total) if small_total > 0 else 1.0
                for k in range(3, 8):
                    label = f"{k}球" if k < 7 else "7+球"
                    goals_probs[label] *= big_factor
                for k in range(3):
                    goals_probs[f"{k}球"] *= small_factor
                total = sum(goals_probs.values())
                if total > 0:
                    for k in goals_probs:
                        goals_probs[k] /= total

    # 排序输出
    sorted_goals = sorted(goals_probs.items(), key=lambda x: x[1], reverse=True)
    top1 = sorted_goals[0]

    return {
        'top3': ' '.join(f"{k}:{p*100:.1f}%" for k, p in sorted_goals[:3]),
        'main': f"{top1[0]}({top1[1]*100:.1f}%)",
        'lam_total': f"{total_lam:.1f}",
        'probs': dict(goals_probs),
        'fusion_weight': round(f_model, 3),
    }

def normalize(w, d, l):
    t = w + d + l
    return w/t, d/t, l/t

# ============================================================
# Ultra 2.0: 高级数学模型 — 命中率优先, 兼顾赔率利益最大化
# ============================================================

def shin_method(odds_list):
    """Shin's method — 从赔率中提取更真实的概率 (优于简单1/odds归一化)

    原理: 博彩公司通过调整赔率来平衡投注, 简单1/odds归一化会高估热门选项概率。
    Shin模型假设市场上存在"内幕交易者"(insider traders), 通过引入参数z来修正:
      z = (Σ(1/odds_i) - 1) / (N - 1)  (N=选项数, z=内幕比例)
      P_i = (1/odds_i - z) / (1 - N*z)

    优势:
      1. 修正favorite-longshot bias (热门-冷门偏差)
      2. 当margin(返还率差)越小时, z越小, 修正越少
      3. 当margin越大时, 修正效果越明显
      4. 比简单1/odds更准确, 特别对高赔率选项(平局/冷门)

    参数: odds_list = [odds_h, odds_d, odds_a] 等
    返回: [prob_h, prob_d, prob_a] (归一化后, 和为1)
    """
    N = len(odds_list)
    if N < 2:
        return [1.0] + [0.0] * (len(odds_list) - 1)

    inv_odds = [1.0 / o for o in odds_list if o > 0]
    if len(inv_odds) < N:
        # 某些赔率为0或负, 回退到简单归一化 (补齐等长列表, 避免调用方越界)
        s = sum(inv_odds)
        probs = [io / s for io in inv_odds] if s > 0 else [1.0 / N] * N
        return probs + [0.0] * (N - len(probs))

    # 计算Shin参数z
    sum_inv = sum(inv_odds)
    margin = sum_inv - 1.0  # 博彩公司margin
    z = margin / (N - 1)    # 内幕比例估计

    if z < 0 or z >= 1.0 / N:
        # z异常, 回退到简单归一化
        return [io / sum_inv for io in inv_odds]

    # Shin修正概率 (denom 趋近0时回退归一化, 避免除零放大)
    denom = 1.0 - N * z
    if denom < 1e-6:
        return [io / sum_inv for io in inv_odds]
    probs = [(io - z) / denom for io in inv_odds]

    # 确保非负并归一化
    probs = [max(0, p) for p in probs]
    s = sum(probs)
    if s > 0:
        probs = [p / s for p in probs]
    else:
        probs = [1.0 / N] * N

    return probs

# ============================================================
# Ultra 6.11: 五大场景修正模块 (2026-07-28)
# 1. 近况滑坡修正: 近3场LLL时大幅下调进攻λ
# 2. 交锋压制因子: h2h胜率<35%时λ衰减
# 3. 跨盘口矛盾检测: 大球升盘+平赔下降=诱大信号
# 4. 0-0赔率校准: 模型0-0概率<市场隐含50%时上调
# 5. 防守状态识别: 客队近4场WWWW+场均失球<1.0时下调总λ
# ============================================================

def detect_form_slump(form_str, n_recent=3):
    """检测近况进攻滑坡 — 近n场中L占比>=2/3时返回True

    典型场景: DDWWDWLLLW → 最近3场LLL, 进攻断电
    返回: (is_slump: bool, slump_severity: float 0-1)
    """
    if not form_str or len(form_str) < n_recent:
        return False, 0.0
    recent = form_str[-n_recent:]
    l_count = recent.count('L')
    d_count = recent.count('D')
    if l_count >= 2:
        severity = l_count / n_recent
        return True, severity
    # 全平也是进攻哑火信号
    if d_count >= n_recent and l_count == 0:
        return True, 0.4
    return False, 0.0


def detect_defensive_away(away_form, away_stats):
    """检测客队防守回升 — 近4场全胜或3胜1平 + 场均失球<1.0

    典型场景: DLDWLLWWWW → 近4场WWWW, 防线正佳
    返回: (is_defensive: bool, factor: float 0.8-1.0)
    """
    if not away_form or not away_stats:
        return False, 1.0
    recent = away_form[-4:] if len(away_form) >= 4 else away_form
    w_count = recent.count('W')
    d_count = recent.count('D')
    avg_ga = away_stats.get('avg_ga', 99) if isinstance(away_stats, dict) else 99

    if w_count >= 3 and avg_ga < 1.0:
        factor = 0.83 if w_count == 4 else 0.85  # 实证标定: ×0.83/×0.85
        return True, factor
    if w_count >= 4 and avg_ga < 1.2:
        return True, 0.85
    return False, 1.0


def parse_h2h_record(h2h_str):
    """解析交锋记录字符串 → (wins, draws, losses, total, home_win_rate)

    支持格式:
      '11胜10和19负'
      '主队近40次交锋 11胜10平19负'
    返回 home_win_rate = wins / total (主队视角胜率)
    """
    if not h2h_str:
        return None
    # 先尝试从带前缀的格式提取
    m = re.search(r'(\d+)胜(\d+)(?:和|平)(\d+)负', h2h_str)
    if not m:
        return None
    w, d, l = int(m.group(1)), int(m.group(2)), int(m.group(3))
    total = w + d + l
    if total == 0:
        return None
    raw_rate = w / total
    # 升级4: 小样本贝塔收缩 — 5场4胜(80%)不代表真实压制力,
    # 向联赛基准(42/27/31)收缩, 样本越小收缩越强, 避免小样本极端率误导交锋压制判定
    home_wr = raw_rate
    if _MU and UPGRADES.get('h2h_shrink'):
        try:
            _shrunk = _MU.h2h_beta_shrink(w, d, l)
            home_wr = _shrunk[0]
        except Exception:
            pass
    return {'wins': w, 'draws': d, 'losses': l, 'total': total,
            'home_win_rate': home_wr, 'home_win_rate_raw': raw_rate}


def detect_cross_market_trap(initial_summary):
    """检测跨盘口矛盾 — 大球升盘 + 平赔下降 = 诱大信号

    典型场景: dx_init=-3.00→dx_now=-3.25 (升盘) + ouzhi平赔 3.92→3.82 (下降)
    返回: (is_trap: bool, trap_factor: float 0.85-1.0)
    """
    if not initial_summary:
        return False, 1.0

    dx_init = initial_summary.get('dx_init')
    dx_now = initial_summary.get('dx_now')
    ouzhi_init = initial_summary.get('ouzhi_init', '')
    ouzhi_now = initial_summary.get('ouzhi_now', '')

    if not dx_init or not dx_now or not ouzhi_init or not ouzhi_now:
        return False, 1.0

    # 大球升盘: |dx_now| > |dx_init| (负数绝对值变大=升盘)
    try:
        gl_init = abs(float(dx_init))
        gl_now = abs(float(dx_now))
    except (ValueError, TypeError):
        return False, 1.0

    ou_up = gl_now > gl_init + 0.01  # 升盘

    # 平赔下降
    try:
        parts_init = ouzhi_init.split('/')
        parts_now = ouzhi_now.split('/')
        if len(parts_init) == 3 and len(parts_now) == 3:
            draw_init = float(parts_init[1])
            draw_now = float(parts_now[1])
        else:
            return False, 1.0
    except (ValueError, TypeError):
        return False, 1.0

    draw_down = draw_now < draw_init - 0.01

    if ou_up and draw_down:
        # 升盘幅度越大 + 平赔降幅越大 → 诱大信号越强
        ou_delta = gl_now - gl_init
        draw_delta = draw_init - draw_now
        severity = min(1.0, (ou_delta / 0.5 + draw_delta / 0.3) / 2)
        factor = 1.0 - 0.15 * severity  # 最多下调15%
        return True, round(factor, 3)

    return False, 1.0


def detect_zero_zero_mispricing(scores_probs, sporttery_crs):
    """检测0-0概率被低估 — 模型0-0概率 < 市场隐含概率的50%

    返回: (is_mispriced: bool, adjustment: float 0-1, model_00: float, market_00: float)
    """
    if not scores_probs or not sporttery_crs:
        return False, 0.0, 0.0, 0.0

    # 模型0-0概率
    score_00 = scores_probs.get('0-0', 0)

    # 市场隐含0-0概率
    odds_00 = sporttery_crs.get('0-0')
    if not odds_00 or odds_00 <= 1:
        return False, 0.0, score_00, 0.0

    market_00 = 1.0 / odds_00

    if score_00 < market_00 * 0.5 and market_00 > 0.02:
        # 模型严重低估0-0, 需要上调低进球区间
        ratio = score_00 / market_00 if market_00 > 0 else 1
        adjustment = min(0.5, (0.5 - ratio) * 0.6)
        return True, round(adjustment, 3), round(score_00, 4), round(market_00, 4)

    return False, 0.0, score_00, market_00


def exponential_decay_form(form_str, decay_rate=0.15):
    """指数衰减近况权重 — 含平局信息 (Ultra 5.0 改进)

    Ultra 4.0 忽略平局, 只计W/L。Ultra 5.0 改进:
      - 平局 = 半胜半负 (win_rate += 0.5×weight, loss_rate += 0.5×weight)
      - 更准确反映球队实力 (平局表示实力接近)
      - 避免忽略~25%的比赛信息

    原理: 越近的比赛越能反映球队当前状态, 用指数衰减函数赋权:
      weight_i = exp(-λ * (n - i - 1))  for i-th most recent match (0=最近)

    参数:
      form_str: 近况字符串 (如 "WWLDWL")
      decay_rate: 衰减率 (默认0.15, 值越大衰减越快)
    返回:
      weighted_win_rate: 加权胜率 (0-1, 含平局0.5权重)
      weighted_loss_rate: 加权败率 (0-1, 含平局0.5权重)
      total_weight: 总权重
    """
    if not form_str or len(form_str) == 0:
        return 0.5, 0.5, 0.0

    n = len(form_str)
    total_weight = 0.0
    win_weight = 0.0
    loss_weight = 0.0

    for i, ch in enumerate(form_str):
        weight = math.exp(-decay_rate * (n - i - 1))
        total_weight += weight
        if ch == 'W':
            win_weight += weight
        elif ch == 'L':
            loss_weight += weight
        elif ch == 'D':
            # Ultra 5.0: 平局 = 半胜半负
            win_weight += weight * 0.5
            loss_weight += weight * 0.5

    if total_weight > 0:
        wr = win_weight / total_weight
        lr = loss_weight / total_weight
        # 升级3: Glicko-2 近况评分混合 (含不确定性降权)
        # 指数衰减只按时间加权, 不区分"连胜弱旅"与"连胜强队"的含金量差异;
        # Glicko-2 通过评分差期望+RD不确定性给出更稳的近况估计。
        # 对手强度数据不可得时用均值0.5, RD越大(样本少/波动大)混合权重越低。
        if _MU and UPGRADES.get('glicko2_form'):
            try:
                _results = []
                for ch in form_str:
                    _s = 1.0 if ch == 'W' else (0.5 if ch == 'D' else 0.0)
                    _results.append((_s, 0.5))  # 对手强度未知取均值
                _g_mu, _g_rd, _g_exp = _MU.glicko2_form(_results)
                # RD∈[0.3,1.2]: rd小(可靠)→权重0.40, rd大(不可靠)→权重0.15
                _gw = min(0.40, max(0.15, 0.40 - (_g_rd - 0.3) * 0.28))
                wr = (1 - _gw) * wr + _gw * _g_exp
                lr = (1 - _gw) * lr + _gw * (1 - _g_exp)
            except Exception:
                pass
        return wr, lr, total_weight
    return 0.5, 0.5, 0.0

def dynamic_dc_rho(lam_h, lam_a):
    """动态Dixon-Coles ρ参数 (Ultra 4.0 → 6.3 平局增强)

    原理: 固定ρ=-0.05对所有比赛一视同仁, 但实际:
      - 低分比赛(λ_h+λ_a < 2.0): 防守型, 0-0和1-1概率更高, 需要更强修正
      - 高分比赛(λ_h+λ_a > 3.0): 开放型, 低分修正效果递减, 需要弱化修正
      - 中等比赛(2.0~3.0): 固定-0.05接近最优

    公式: ρ = -0.05 × clamp(1.5 - (λ_h+λ_a)/2.5, 0.5, 1.5)
      总进球2.5时 → ρ = -0.05 × 0.5 = -0.025 (弱化)
      总进球2.0时 → ρ = -0.05 × 0.7 = -0.035
      总进球1.5时 → ρ = -0.05 × 0.9 = -0.045
      总进球1.0时 → ρ = -0.05 × 1.1 = -0.055 (强化)
      总进球0.5时 → ρ = -0.05 × 1.3 = -0.065 (强强化)

    Ultra 6.3 平局增强:
      - 两队λ接近时(|λ_h-λ_a|<0.3): 增强低分修正
      - 极接近(|λ_h-λ_a|<0.15): ρ × 2.0 (双倍修正)
      - 接近(|λ_h-λ_a|<0.30): ρ × 1.5 (1.5倍修正)
      - 参数范围扩大: [-0.15, -0.01]

    优势:
      1. 自适应不同进球预期, 避免一刀切
      2. 低分比赛修正更强(平局更多), 高分比赛修正更弱
      3. 参数范围[-0.075, -0.025], 在Dixon-Coles合理区间内
      4. Ultra 6.3: λ接近时增强修正, 直接提升0-0/1-1概率
    """
    total_lam = lam_h + lam_a
    factor = max(0.5, min(1.5, 1.5 - total_lam / 2.5))
    rho = -0.05 * factor

    # Ultra 6.3: 两队λ接近时增强低分修正
    lam_diff = abs(lam_h - lam_a)
    if lam_diff < 0.15:
        rho *= 2.0  # 极接近: 双倍修正
    elif lam_diff < 0.30:
        rho *= 1.5  # 接近: 1.5倍修正

    # 确保在合理范围内
    rho = max(-0.15, min(-0.01, rho))
    return rho


def power_method(odds_list):
    """Power方法 — Shin方法的替代/补充 (Ultra 4.0)

    原理: 用幂参数β调整1/odds的锐度
      P_i = (1/odds_i)^β / Σ(1/odds_j)^β
      β > 1: 锐化概率 (增强热门, 压制冷门)
      β < 1: 平滑概率 (增强冷门, 压制热门)
      β = 1: 退化为简单归一化

    β估计: 从市场margin推导
      margin = Σ(1/odds_i) - 1
      β = 1 / (1 + margin × 2)
      margin=0 → β=1 (无修正)
      margin=0.05 → β≈0.91 (平滑, 增强冷门)
      margin=0.10 → β≈0.83 (强平滑)

    优势:
      1. 计算简单, 无需迭代
      2. 与Shin方法互补: Shin修正insider bias, Power修正margin bias
      3. 对高margin市场(体彩)修正效果更明显
    """
    N = len(odds_list)
    if N < 2:
        return [1.0] + [0.0] * (len(odds_list) - 1)
    inv_odds = [1.0 / o for o in odds_list if o > 0]
    if len(inv_odds) < N:
        s = sum(inv_odds)
        probs = [io / s for io in inv_odds] if s > 0 else [1.0 / N] * N
        return probs + [0.0] * (N - len(probs))
    margin = sum(inv_odds) - 1.0
    beta = 1.0 / (1.0 + margin * 2.0) if margin > 0 else 1.0
    powered = [io ** beta for io in inv_odds]
    s = sum(powered)
    return [p / s for p in powered] if s > 0 else [1.0 / N] * N


def bayesian_shrinkage(sample_mean, sample_size, league_mean, k=10):
    """贝叶斯收缩 — 有限数据时向联赛均值收缩 (Ultra 2.0)

    原理: 当样本量小时, 直接使用样本均值会有高方差。
    贝叶斯收缩将样本均值向先验(联赛均值)方向调整:
      λ_shrunk = (n * λ_sample + k * λ_league) / (n + k)

    k是收缩强度:
      k越大 → 越向联赛均值收缩 (保守, 适合数据少)
      k越小 → 越信任样本均值 (激进, 适合数据多)
      k=10: 约10场比赛后样本和先验各占一半

    应用场景:
      1. 球队场均进球 (新赛季初期数据少)
      2. 球队主场/客场进攻力
      3. 联赛平均进球率

    参数:
      sample_mean: 样本均值 (如球队场均进球)
      sample_size: 样本量 (如已赛场次)
      league_mean: 联赛均值 (先验)
      k: 收缩强度 (默认10)
    """
    if sample_size <= 0:
        return league_mean
    return (sample_size * sample_mean + k * league_mean) / (sample_size + k)


def elo_probabilities(home_stats, away_stats, home_form_wr, away_form_wr, league_home_adv=65,
                      hist_elo_h=None, hist_elo_a=None):
    """Elo评级概率 (Ultra 5.0 — 第4个概率源)

    原理: Elo评级系统量化球队实力差, 转换为胜平负概率。
    优先使用历史库真实Elo评级, 其次从球队统计推导:
      1. 若有历史Elo → 直接使用 (最准确, 基于完整时间序列)
      2. 无历史Elo → 从场均进球/失球推导基础Elo: 进攻力-防守力
      3. 从近况加权胜率微调: 反映当前状态
      4. 用Elo公式转换为胜平负概率

    Elo→概率转换:
      P(home win) = 1 / (1 + 10^(-(R_h - R_a + HFA) / 400))
      平局概率: P_draw = 0.28 - 0.10 × |P_win - P_lose| (经验校准)

    参数:
      home_stats: 主队统计 {avg_gf, avg_ga, ...} 或 None
      away_stats: 客队统计 或 None
      home_form_wr: 主队加权胜率 (0-1, from exponential_decay_form)
      away_form_wr: 客队加权胜率
      league_home_adv: 主场优势Elo点数 (默认65)
      hist_elo_h: 历史库Elo评级 (主队), 优先使用
      hist_elo_a: 历史库Elo评级 (客队), 优先使用
    返回:
      [P_win, P_draw, P_lose] 或 None (数据不足时)
    """
    # 推导Elo评级
    if hist_elo_h is not None and hist_elo_a is not None:
        # 优先使用历史库真实Elo (基于完整时间序列计算)
        R_home = hist_elo_h
        R_away = hist_elo_a
    elif home_stats and away_stats:
        # 基础Elo: 进攻力 - 防守力, 缩放到Elo尺度 (×100)
        home_attack = home_stats.get('avg_gf', 1.3)
        home_defense = home_stats.get('avg_ga', 1.3)
        away_attack = away_stats.get('avg_gf', 1.3)
        away_defense = away_stats.get('avg_ga', 1.3)

        R_home = (home_attack - home_defense) * 100
        R_away = (away_attack - away_defense) * 100
    else:
        # 无统计时用近况推导
        R_home = (home_form_wr - 0.5) * 200
        R_away = (away_form_wr - 0.5) * 200

    # 近况微调: 胜率偏离0.5的部分×50 (温和调整)
    # 注意: 使用历史Elo时不做近况微调 (Elo已反映真实实力, 近况由其他源捕获)
    if hist_elo_h is None or hist_elo_a is None:
        R_home += (home_form_wr - 0.5) * 50
        R_away += (away_form_wr - 0.5) * 50

    # Elo→胜率
    diff = R_home - R_away + league_home_adv
    p_win_raw = 1.0 / (1.0 + 10 ** (-diff / 400.0))
    p_lose_raw = 1.0 - p_win_raw

    # 平局概率: 实力越接近平局越多, 一边倒时平局越少
    # 经验公式: P_draw = 0.28 - 0.10 × |P_win - P_lose|
    # 实际足球数据校准:
    #   均势(50/50): draw ≈ 28%
    #   中等优势(70/30): draw ≈ 24%
    #   强优势(90/10): draw ≈ 20%
    p_draw = max(0.15, 0.28 - 0.10 * abs(p_win_raw - p_lose_raw))

    # 从胜负中扣除平局
    remaining = 1.0 - p_draw
    p_win = p_win_raw * remaining / (p_win_raw + p_lose_raw) if (p_win_raw + p_lose_raw) > 0 else remaining / 2
    p_lose = remaining - p_win

    return [max(0.05, p_win), max(0.05, p_draw), max(0.05, p_lose)]


# ============================================================
# Ultra 3.0: 高级概率校准 + 集成预测模型
# 目标: 命中率优先, 兼顾赔率利益最大化
# ============================================================

def calibrate_probabilities(probs, source='poisson', lam_total=None, lam_h=None, lam_a=None,
                            league=None, draw_odds=None):
    """概率校准 — 自适应Logit校准 (Ultra 5.0 → 6.3 平局增强)

    Ultra 4.0使用固定0.27作为平局目标, Ultra 5.0改为自适应:
      - 低分比赛(λ_total<2.0): 平局目标0.30 (防守型, 平局更多)
      - 中分比赛(λ_total≈2.5): 平局目标0.27 (标准)
      - 高分比赛(λ_total>3.5): 平局目标0.22 (开放型, 平局更少)

    Ultra 6.2 平局增强 (基于验证反馈: 3场平局全部漏掉):
      - 所有源都做平局校准 (不只是Poisson)
      - market源: 赔率本身压平局, 需更强的shift
      - 提高shift上限: 0.35→0.50
      - 当两队λ接近时(|λ_h-λ_a|<0.3): 额外平局加成

    Ultra 6.3 进一步增强:
      - λ接近时额外加成: |λ_h-λ_a|<0.3 → shift额外+0.15
      - 极接近时(|λ_h-λ_a|<0.15): shift额外+0.25 (势均力敌, 平局概率最高)
      - 最低平局概率保底: 校准后平局概率不低于0.15 (防止极端情况)

    原理: 泊松/负二项模型系统性地低估平局概率, 需要校准。
    Logit变换:
      logit(P) = ln(P / (1-P))
      logit(P_cal) = logit(P_raw) + shift
      P_cal = sigmoid(logit(P_raw) + shift)

    优势:
      1. 自适应: 根据比赛类型调整平局修正强度
      2. 边界稳定: P接近0/1时修正量自然减小
      3. 对称性: 胜和负的修正对称
      4. Ultra 6.2: 全源校准 + λ接近时额外加成
      5. Ultra 6.3: 分级λ接近加成 + 最低平局保底

    参数:
      probs: [P_win, P_draw, P_lose] 原始概率 (和为1)
      source: 'poisson' 或 'market'
      lam_total: 总进球期望λ (用于自适应平局目标)
      lam_h: 主队进球期望λ (用于λ接近检测)
      lam_a: 客队进球期望λ (用于λ接近检测)
    返回:
      [P_win_cal, P_draw_cal, P_lose_cal] 校准后概率 (归一化)
    """
    p_w, p_d, p_l = probs

    # Ultra 5.0: 自适应平局目标
    if lam_total is not None:
        # 线性插值: λ=1.5→target=0.32, λ=2.5→0.27, λ=3.5→0.22
        target_draw = max(0.20, min(0.35, 0.27 - (lam_total - 2.5) * 0.05))
    else:
        target_draw = 0.27

    # Ultra 6.4: 联赛平局率先验 + 平赔信号
    # 联赛历史平局率是平局频率的直接先验 (此前target仅由λ决定)
    # Ultra 11.3: 杯赛不走此块 — 其平局先验由下方 CUP_DRAW_BASE 决定,
    #             权避免被 _CALIBRATION 覆盖的杯赛主名低值(如欧冠0.1832)拉低
    if league and not _is_cup_league(league):
        league_rate = LEAGUE_DRAW_RATE.get(league, LEAGUE_DRAW_RATE.get('default'))
        if league_rate:
            target_draw = 0.7 * target_draw + 0.3 * league_rate

    # Ultra 11.3 (回归分析 2026-08-05): 杯赛平局加成
    # 回归验证(260804周二): 003奥林匹亚0-0、004圣吉3-3 两回合制杯赛平局全漏(HAD判胜/胜负)
    # 根因: 杯赛(尤其资格赛)次回合落后方保守强攻受限、领先方留力, 平局倾向高于联赛
    #       欧冠 7-8月资格赛平局率 20%~25%, 均高于模型默认先验
    # 关键: '欧冠'等主名会被 _CALIBRATION(sample>=20) 覆盖为低值(如0.1832),
    #       故杯赛分支必须用固定先验 CUP_DRAW_BASE, 不能依赖可能被覆盖的 LEAGUE_DRAW_RATE
    # 机制: ① 固定杯赛平局先验 0.28 直接加权(权重0.5), 不受数据库覆盖影响
    #       ② 分级加成: λ差<0.40 +4pp(势均力敌强加成), <0.80 +2pp(小幅差距中加成)
    #       ③ 一边倒(λ差>=0.80)靠固定先验兜底, 不硬掰
    if league and _is_cup_league(league):
        # ① 固定杯赛平局先验 (取实测区间 20%~25% 上沿), 覆盖被数据库低值覆盖的情况
        _cup_rate = max(CUP_DRAW_BASE, LEAGUE_DRAW_RATE.get(league, CUP_DRAW_BASE))
        target_draw = 0.5 * target_draw + 0.5 * _cup_rate
        # ② 分级平局加成 (杯赛整体倾向平局, 势均力敌更强)
        if lam_h is not None and lam_a is not None:
            _lam_diff_cup = abs(lam_h - lam_a)
            if _lam_diff_cup < 0.40:
                target_draw += 0.04
            elif _lam_diff_cup < 0.80:
                target_draw += 0.02
    # 平赔<3.4: 市场定价认为平局可能性高 → 目标+2pp
    # 平赔>4.0: 市场认为平局罕见 → 目标-1pp
    if draw_odds and draw_odds > 1.5:
        if draw_odds < 3.4:
            target_draw += 0.02
        elif draw_odds > 4.0:
            target_draw -= 0.01
    # Ultra 6.5: 平局偏差在线反馈 (verify_history 实际平局率 vs 预测均值, 有界±0.03)
    target_draw += query_draw_bias()
    target_draw = max(0.18, min(0.36, target_draw))

    def _logit(p):
        p = max(0.001, min(0.999, p))
        return math.log(p / (1 - p))

    def _sigmoid(x):
        return 1.0 / (1.0 + math.exp(-max(-20, min(20, x))))

    # 平局校准: shift量与低估程度成正比
    draw_gap = max(0, target_draw - p_d)

    if source == 'poisson':
        # Poisson源: 标准校准
        shift = min(0.65, draw_gap * 2.5)
    elif source == 'market':
        # Ultra 6.2: 市场源赔率本身压平局, 需更强校准
        shift = min(0.65, draw_gap * 3.0)
    else:
        shift = min(0.55, draw_gap * 2.5)

    # Ultra 6.3: 两队λ接近时额外平局加成
    # 势均力敌的比赛平局概率最高, 但模型最容易低估
    if lam_h is not None and lam_a is not None:
        lam_diff = abs(lam_h - lam_a)
        if lam_diff < 0.15:
            # 极接近: 额外+0.25 shift
            shift = min(0.65, shift + 0.25)
        elif lam_diff < 0.30:
            # 接近: 额外+0.15 shift
            shift = min(0.60, shift + 0.15)

    logit_w = _logit(p_w)
    logit_d = _logit(p_d) + shift
    logit_l = _logit(p_l)

    p_w_new = _sigmoid(logit_w)
    p_d_new = _sigmoid(logit_d)
    p_l_new = _sigmoid(logit_l)

    # Ultra 6.3: 最低平局概率保底 (防止极端情况平局概率过低)
    p_d_new = max(0.18, p_d_new)

    p_w_new = max(0.01, p_w_new)
    p_l_new = max(0.01, p_l_new)
    total = p_w_new + p_d_new + p_l_new
    return [p_w_new / total, p_d_new / total, p_l_new / total]


def ensemble_fuse(probs_list, weights=None):
    """多源概率融合 — 对数空间加权集成 (Ultra 4.0)

    Ultra 3.0使用算术加权平均, Ultra 4.0改用对数空间(几何平均)融合:
      P_fused[i] ∝ Π_j P_j[i]^{w_j}
      log P_fused[i] = Σ_j w_j × log P_j[i]

    对数空间融合优势:
      1. 极端概率处理更好: 当某源给某选项极低概率时, 融合后该选项概率也被压制
      2. 信息论基础: 等价于最小化加权KL散度, 有严格数学意义
      3. 避免算术平均的"拉平效应": 算术平均会让概率趋近均匀, 几何平均保持锐度

    融合策略:
      1. 当各源方向一致时 → 高置信度, 权重均匀
      2. 当各源方向分歧时 → 低置信度, 偏向市场(更可靠)
      3. 对数空间加权, softmax归一化

    参数:
      probs_list: [[Pw,Pd,Pl], ...] 多个概率源
      weights: 各源权重, None则自动计算
    返回:
      fused_probs: [Pw, Pd, Pl] 融合后概率
      agreement: 0-1, 各源一致程度 (1=完全一致)
    """
    n = len(probs_list)
    if n == 0:
        return [0.33, 0.34, 0.33], 0.0
    if n == 1:
        return probs_list[0], 1.0

    # 各源的主方向
    directions = [p.index(max(p)) for p in probs_list]

    # 计算一致性: 方向相同的源占比
    dir_counts = Counter(directions)
    majority_dir = dir_counts.most_common(1)[0]
    agreement = majority_dir[1] / n

    if weights is None:
        # 源特定基础权重: 市场>校准Poisson>Elo>Power
        base_weights = [1.5, 0.8, 1.2, 1.0]  # market, power, poisson, elo
        if n <= len(base_weights):
            weights = base_weights[:n]
        else:
            weights = [1.0] * n
        # 一致性调节: 分歧时更偏向市场
        if agreement < 0.5:
            weights[0] *= 1.5

    # 归一化权重
    total_w = sum(weights)
    weights = [w / total_w for w in weights]

    # 对数空间加权融合 (几何加权平均)
    log_fused = [0.0, 0.0, 0.0]
    for i, probs in enumerate(probs_list):
        for j in range(3):
            p = max(probs[j], 1e-6)  # 防止log(0)
            log_fused[j] += weights[i] * math.log(p)

    # softmax归一化: exp(log_fused) / Σ exp(log_fused)
    max_log = max(log_fused)
    exp_vals = [math.exp(lf - max_log) for lf in log_fused]
    total_exp = sum(exp_vals)
    fused = [ev / total_exp for ev in exp_vals]

    return fused, agreement


# ============================================================
# Ultra 7.6: 公共融合权重函数 (HAD/HHAD 共用)
# 修复报告问题: P2 dq二值化悬崖 / P3 Power悖论 / P5 HHAD无动态调节
#               P8 Elo精度分级 / P12 proxy xG降权
# ============================================================
def compute_fuse_weights(dq_score, market_probs=None, power_probs=None,
                         hist_elo=False, xg_proxy=False, ppda_stab=0.0):
    """根据数据质量/方向一致性/数据精度计算四源融合权重

    参数:
      dq_score:      数据质量评分 0-100 (assess_data_quality)
      market_probs:  市场概率 [w,d,l] (用于方向一致性判断)
      power_probs:   Power方法概率
      hist_elo:      是否有历史Elo (完整时间序列, 高精度)
      xg_proxy:      xG是否为占位符proxy (非五大联赛真实Understat数据)
      ppda_stab:     PPDA稳定性因子 0-1
    返回:
      [market_w, power_w, poisson_w, elo_w]
    """
    # P2: dq渐变化 — 连续过渡替代二值跳变
    # 过渡带 40→75 (dq评分分布集中在40~70, 50→80会让多数比赛停在低区分度段)
    f = max(0.0, min(1.0, (dq_score - 40) / 35.0))  # 40→0, 75→1
    market_w  = 1.5 + (1 - f) * 0.5   # 1.5~2.0 (低质量时偏向市场)
    power_w   = 0.8 + (1 - f) * 0.2   # 0.8~1.0 (Power同样源自赔率)
    poisson_w = 0.3 + f * 0.9         # 0.3~1.2 (渐变, 消除0.3↔1.2悬崖)
    elo_w     = 0.2 + f * 0.8         # 0.2~1.0

    # P12: proxy xG → Poisson可信度降权 (占位符无法反映真实攻防)
    if xg_proxy:
        poisson_w = min(poisson_w, 0.6)
    elif ppda_stab > 0.7:
        # 报告建议4: PPDA稳定时Poisson小幅加权 (仅在真实xG前提下)
        poisson_w *= 1.1

    # P8: Elo按数据精度分级
    if hist_elo:
        elo_w = min(1.3, elo_w * 1.3)   # 历史Elo高精度 → 最高1.3
    else:
        elo_w = min(elo_w, 0.8)         # 推导Elo低精度 → 上限0.8

    # P3: Power悖论修复 — 方向一致时是独立验证信号, 应加分而非减分
    if market_probs and power_probs:
        if market_probs.index(max(market_probs)) == power_probs.index(max(power_probs)):
            power_w = max(power_w, 1.0)

    return [round(market_w, 3), round(power_w, 3), round(poisson_w, 3), round(elo_w, 3)]


def infer_goal_line_from_had(had):
    """P4: HHAD让球盘口缺失/为0时, 从HAD赔率反推让球档

    竞彩让球为整数档, 主胜赔率与让球数强相关:
      主胜<1.40 → 让2球(-2); 1.40~1.80 → 让1球(-1)
      主胜>4.00 → 受让2球(+2); 2.60~4.00 → 受让1球(+1)
      其余 → 0 (不让球, 不强行推断)
    返回: 推断的goalLine (整数), 无法判断时返回0
    """
    if not had or 'h' not in had:
        return 0
    h, a = had.get('h', 0), had.get('a', 0)
    if h <= 1 or a <= 1:
        return 0
    if h < 1.40:
        return -2
    if h < 1.80:
        return -1
    if a < 1.40:
        return 2
    if a < 1.80:
        return 1
    return 0


# ============================================================
# Ultra 7.6: 球队名称归一化 (修复巴甲等联赛队名割裂)
# ============================================================
_TEAM_ALIAS_CACHE = None

def team_name_variants(name):
    """返回队名的所有已知变体 (含自身), 用于数据库IN查询

    从 predictions/team_alias.json 加载映射: 标准名 → [变体列表]
    双向展开: 输入变体也能找到标准名及其他变体
    """
    global _TEAM_ALIAS_CACHE
    if _TEAM_ALIAS_CACHE is None:
        _TEAM_ALIAS_CACHE = {}
        _p = os.path.join(
            os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__)),
            'predictions', 'team_alias.json')
        if os.path.exists(_p):
            try:
                with open(_p, 'r', encoding='utf-8') as _f:
                    raw = json.load(_f)
                # 双向展开: 标准名→变体, 变体→同组所有名
                for std, variants in raw.items():
                    group = {std} | set(variants)
                    for n in group:
                        _TEAM_ALIAS_CACHE[n] = group
            except Exception:
                _TEAM_ALIAS_CACHE = {}
    group = _TEAM_ALIAS_CACHE.get(name)
    return sorted(group) if group else [name]


def compute_js_agreement(probs_list):
    """JS散度一致性 (P10): 0-1, 越高各源概率分布越相似

    相比argmax方向一致性(离散0.5/0.75/1.0), JS散度捕捉分布形状差异:
    两个源都预测"胜"但一个90%一个34%时, 方向一致=1.0 但JS一致性显著更低。
    回测(3930场): 触发差异仅0.13%, 故仅作信息字段, 不改变融合权重。
    """
    n = len(probs_list)
    if n < 2:
        return 1.0
    avg = [sum(p[j] for p in probs_list) / n for j in range(3)]
    def _kl(a, b):
        return sum(a[j] * math.log(max(a[j], 1e-9) / max(b[j], 1e-9)) for j in range(3))
    js = sum(0.5 * _kl(p, avg) + 0.5 * _kl(avg, p) for p in probs_list) / n
    return round(max(0.0, 1.0 - js / math.log(2)), 3)


def match_difficulty_score(had_probs, poisson_probs, data_quality, agreement):
    """比赛可预测性评分 (Ultra 3.0)

    综合评估一场比赛的可预测性, 用于:
      1. 调整置信度星级
      2. 决定是否推荐(低可预测性 → 谨慎推荐)
      3. 模拟投注选场(高可预测性 → 优先选入串关)

    评分维度:
      1. 概率差距 (30%): top1-top2 概率差越大越可预测
      2. 模型一致性 (25%): 市场方向 vs 模型方向一致 → 高分
      3. 数据质量 (25%): 数据越完整越可靠
      4. 方向集中度 (20%): 概率越集中(非均匀)越可预测

    返回: 0-100 分数, 100=最可预测
    """
    # 1. 概率差距 (0-100)
    sorted_probs = sorted(had_probs, reverse=True)
    prob_gap = sorted_probs[0] - sorted_probs[1]
    gap_score = min(100, prob_gap * 300)  # 0.33差距 → 100分

    # 2. 模型一致性 (0-100)
    consistency_score = agreement * 100

    # 3. 数据质量 (0-100)
    dq_score = data_quality if isinstance(data_quality, (int, float)) else 50

    # 4. 方向集中度: 熵的逆指标 (0-100)
    # 熵越低 → 概率越集中 → 越可预测
    entropy = 0
    for p in had_probs:
        if p > 0.001:
            entropy -= p * math.log2(p)
    max_entropy = math.log2(3)  # 均匀分布的熵 ≈ 1.585
    concentration_score = (1 - entropy / max_entropy) * 100

    # 加权综合
    total = (gap_score * 0.30 + consistency_score * 0.25 +
             dq_score * 0.25 + concentration_score * 0.20)

    return round(total, 1)


def _build_initial_summary(init_ouzhi, init_yazhi, init_daxiao):
    """构建初赔摘要 (精简, 节省token)"""
    s = {}
    if init_ouzhi:
        ai = init_ouzhi['avg_initial']
        ai0 = init_ouzhi['avg_instant']
        s['ouzhi_init'] = f"{ai[0]:.2f}/{ai[1]:.2f}/{ai[2]:.2f}"
        s['ouzhi_now'] = f"{ai0[0]:.2f}/{ai0[1]:.2f}/{ai0[2]:.2f}"
        s['ouzhi_n'] = init_ouzhi['num_valid']
    if init_yazhi:
        yi = init_yazhi['initial']
        yn = init_yazhi['instant']
        s['yazhi_init'] = f"{yi['handicap_mode']:+.2f}"
        s['yazhi_now'] = f"{yn['handicap_mode']:+.2f}"
    if init_daxiao:
        di = init_daxiao['initial']
        dn = init_daxiao['instant']
        s['dx_init'] = f"{di['goal_line_mode']:+.2f}"
        s['dx_now'] = f"{dn['goal_line_mode']:+.2f}"
    return s if s else None

# ============================================================
# Pro 3.0: Kelly公式 + 数据质量评分 + 动态主场优势
# ============================================================
def pool_margin(odds_list):
    """玩法抽水率 (借鉴 SportteryAPI derive.ts): margin = 1 - 1/Σ(1/o)

    竞彩各玩法抽水深度不同 (如比分玩法远高于胜平负),
    用于value判定加权: 抽水越深, 要求的EV边际越高
    """
    valid = [o for o in odds_list if o and o > 1]
    s = sum(1.0 / o for o in valid)
    return max(0.0, 1.0 - 1.0 / s) if s > 0 else 0.0


def kelly_criterion(prob, odds, margin=0.0):
    """四分之一Kelly投注比例 + margin加权value判定
    f* = (bp - q) / b, 实际使用 f* / 4
    value: f>0 且 EV >= margin/2 (抽水越深的玩法要求越高边际, 默认margin=0保持旧行为)
    返回: {'stake_pct': float, 'ev': float, 'value': bool}
    """
    b = odds - 1
    if b <= 0:
        return {'stake_pct': 0, 'ev': 0, 'value': False}
    f = (b * prob - (1 - prob)) / b
    ev = prob * odds - 1  # EV = P×赔率 - 1
    stake = max(0, f * 0.25) * 100
    # Optimize: margin/2 阈值偏保守, 改为 margin*0.3
    # 实证: 比分玩法margin高达30-40%, margin/2=15-20%阈值过高,
    # 很多略高于0的正EV被忽略; margin*0.3在保守和敏感之间折中
    value_threshold = margin * 0.3
    return {'stake_pct': round(stake, 1), 'ev': round(ev * 100, 1),
            'value': f > 0 and ev >= value_threshold}


def _hhad_display_label(option, handicap):
    """HHAD选项/洞察文案术语规范化 (Ultra 11.10 铁律, 与 gen_pred_pdf._hhad_option_label 一致)

    - 负盘(≤-1)=让球: 让胜/让负/让平 不变
    - 正盘(≥+1)=受让: 让胜→受让胜, 让负→受让负, 让平→受让平
    - 0=平盘: 保持让X不变
    只处理含 '让胜'/'让负'/'让平' 的文本, 其余原样返回。
    """
    if not option:
        return option
    try:
        hcap = float(handicap)
    except (TypeError, ValueError):
        return option
    if hcap <= 0:
        return option  # 让球盘或平盘, 术语不变
    # 幂等替换 (ERR-20260809-001): 用负向后瞻 (?<!受) 避免对已含"受让X"的文本二次替换成"受受让X"
    # 例如传入"HHAD受让胜"时, "让胜"前已有"受", 不再替换 → 不会变成"HHAD受受让胜"
    for src, dst in [('让胜', '受让胜'), ('让负', '受让负'), ('让平', '受让平')]:
        option = re.sub(r'(?<!受)' + src, dst, option)
    return option


def compute_cross_market_value(had_probs, had_dict, hhad_probs, hhad_dict, handicap, lam_h, lam_a, mode='prob', difficulty=None):
    """跨玩法价值分析 — 命中率优先, EV仅作参考

    三模式推荐系统:
        mode='prob' (默认, 命中率优先): 纯概率排序, 概率最高即主推, EV不参与排序
        mode='ev'   (EV优先):          主推=EV最高, 概率为辅
        mode='hybrid'(混合):           score = 0.6×prob + 0.4×ev_norm

    Ultra 11.11: 新增 difficulty 参数 — 用于让平/平局盲区补偿的"中等难度"触发判定
    (深度因子分析 LRN-20260809-002: 受让盘让平率36% vs 让球盘23%; 中等难度45-65让平率44%)

    Returns:
        all_ranked: 全部选项按主模式排序
        all_ev: 全部选项按EV降序(辅助参考)
        prob_ranked: 全部选项按概率降序(辅助参考)
        value_bets: 正EV投注列表
        primary_bet: 主推投注(按mode决定)
        primary_mode: 使用的推荐模式
        pass_risk: 穿盘风险分析
        margin_dist: 净胜球概率分布
        risk_assessment: 风险评估文字
        insight: 综合洞察文字
        let_draw_hotspot: 让平高发窗口标记 (Ultra 11.11)
    """
    all_options = []
    had_labels = ['HAD胜', 'HAD平', 'HAD负']
    hhad_labels = ['HHAD让胜', 'HHAD让平', 'HHAD让负']

    # ===== 本质分析: 真单选 vs 伪单选 (Pro 3.9) =====
    # HHAD让球在整数盘口下, 某些方向实际覆盖两个结果(伪单选)
    #   主队+1受让: 让胜=胜+平(伪), 让负=仅输2+(真), 让平=仅输1球(真)
    #   主队-1让球: 让负=平+负(伪), 让胜=仅赢2+(真), 让平=仅赢1球(真)
    # 半球盘口(±0.5)无让平, 所有方向都是真单选
    # HAD胜/平/负 永远是真单选
    is_integer_handicap = (handicap == int(handicap))
    
    def get_selection_type(market, option_idx, handicap):
        """返回 (selection_type, coverage_desc, cost_advantage)"""
        if market == 'HAD':
            return ('真单选', ['胜','平','负'][option_idx], None)
        # HHAD
        if not is_integer_handicap:
            return ('真单选', f'让{["胜","平","负"][option_idx]}', None)
        # 整数盘口
        if handicap > 0:  # 主队受让
            if option_idx == 0:  # 让胜 = 胜+平
                return ('伪单选', '胜+平', '2元覆盖HAD胜平双选(4元), 省一半成本')
            else:
                return ('真单选', f'让{["胜","平","负"][option_idx]}', None)
        else:  # 主队让球
            if option_idx == 2:  # 让负 = 平+负
                return ('伪单选', '平+负', '2元覆盖HAD平负双选(4元), 省一半成本')
            else:
                return ('真单选', f'让{["胜","平","负"][option_idx]}', None)

    # HAD选项EV
    # 体彩规则: 每注2元, 单选=1注=2元, 赔率含义=每元含本金回报
    # 单注中奖 = 2元 × 赔率, 净赚 = 2×赔率 - 2
    # ROI = P×赔率 - 1 (每元投入的期望收益率)
    if had_dict and 'h' in had_dict:
        had_odds_list = [had_dict['h'], had_dict['d'], had_dict['a']]
        had_margin = pool_margin(had_odds_list)
        for i in range(3):
            ev = had_probs[i] * had_odds_list[i] - 1
            kelly = kelly_criterion(had_probs[i], had_odds_list[i], had_margin)
            sel_type, coverage, _ = get_selection_type('HAD', i, handicap)
            all_options.append({
                'market': 'HAD',
                'option': had_labels[i],
                'prob': round(had_probs[i] * 100, 1),
                'odds': had_odds_list[i],
                'bets': 1,
                'cost': 2,
                'ev_pct': round(ev * 100, 1),
                'kelly_pct': kelly['stake_pct'],
                'value': kelly['value'],
                'selection_type': sel_type,
                'coverage': coverage,
                'cost_advantage': None,
            })

    # HHAD选项EV
    if hhad_dict and 'h' in hhad_dict:
        hhad_odds_list = [hhad_dict['h'], hhad_dict['d'], hhad_dict['a']]
        hhad_margin = pool_margin(hhad_odds_list)
        for i in range(3):
            ev = hhad_probs[i] * hhad_odds_list[i] - 1
            kelly = kelly_criterion(hhad_probs[i], hhad_odds_list[i], hhad_margin)
            sel_type, coverage, cost_adv = get_selection_type('HHAD', i, handicap)
            all_options.append({
                'market': 'HHAD',
                'option': hhad_labels[i],
                'prob': round(hhad_probs[i] * 100, 1),
                'odds': hhad_odds_list[i],
                'bets': 1,
                'cost': 2,
                'ev_pct': round(ev * 100, 1),
                'kelly_pct': kelly['stake_pct'],
                'value': kelly['value'],
                'selection_type': sel_type,
                'coverage': coverage,
                'cost_advantage': cost_adv,
            })

    # HAD双选选项EV (Pro 3.3: 跨玩法双选对比)
    # 体彩规则: 双选=复式投注=2注=4元, 每注独立计算
    #   注1: 押选项A, 成本2元, 命中则奖金=2×odds_A
    #   注2: 押选项B, 成本2元, 命中则奖金=2×odds_B
    #   总成本=4元, P(A)与P(B)互斥
    #   EV金额 = P1×2×odds1 + P2×2×odds2 - 4 = 2×(P1×odds1 + P2×odds2 - 2)
    #   ROI% = EV金额/4×100 = (P1×odds1 + P2×odds2 - 2)/2 × 100
    #   与单选ROI%可直接比较 (均为每元投入的期望收益率)
    if had_dict and 'h' in had_dict:
        had_odds_list_dbl = [had_dict['h'], had_dict['d'], had_dict['a']]
        had_double_configs = [
            ('HAD胜平双选', 0, 1, '主队不败'),
            ('HAD胜负双选', 0, 2, '分胜负'),
            ('HAD平负双选', 1, 2, '客队不败'),
        ]
        for label, idx1, idx2, direction in had_double_configs:
            p1 = had_probs[idx1]
            p2 = had_probs[idx2]
            odds1 = had_odds_list_dbl[idx1]
            odds2 = had_odds_list_dbl[idx2]
            combined_prob = p1 + p2
            # 加权平均赔率 (条件期望, 命中时的平均赔率)
            if combined_prob > 0:
                avg_odds = round((p1 * odds1 + p2 * odds2) / combined_prob, 2)
            else:
                avg_odds = 0
            # 双选ROI% = (P1×odds1 + P2×odds2 - 2) / 2 × 100
            roi = (p1 * odds1 + p2 * odds2 - 2) / 2
            ev_pct = roi * 100
            # Kelly: 有效赔率 = avg_odds / 成本注数, 因为b=odds-1是每元净赔率
            # 双选2注, 每元有效赔率 = avg_odds/2
            effective_odds = avg_odds / 2 if avg_odds > 0 else 0
            kelly = kelly_criterion(combined_prob, effective_odds, had_margin) if effective_odds > 1 else {'stake_pct': 0}
            # Bugfix: 双选value判定应与单选一致, 使用margin感知阈值
            double_value = roi > 0 and (roi * 100) >= had_margin / 2
            all_options.append({
                'market': 'HAD双选',
                'option': label,
                'prob': round(combined_prob * 100, 1),
                'odds': avg_odds,
                'odds_detail': f'{odds1}/{odds2}',
                'bets': 2,
                'cost': 4,
                'ev_pct': round(ev_pct, 1),
                'kelly_pct': kelly['stake_pct'],
                'value': double_value,
                'direction': direction,
            })

    # HHAD双选选项EV (Ultra 11.22: 让球双选纳入候选 — 用户要求)
    # 体彩规则同HAD双选: 2注=4元, ROI% = (P1×odds1 + P2×odds2 - 2)/2 × 100
    # 让球三结果: 让胜/让平/让负 (受让+1时依次=主不败/恰输1/输2+; 让球-1时=赢2+/恰赢1/平+负)
    # 作用: 让HHAD双选与HAD双选同台竞争, 主推在让球盘有价值时(double_recommend选型)可选让球双选
    if hhad_dict and 'h' in hhad_dict:
        hhad_odds_list_dbl = [hhad_dict['h'], hhad_dict['d'], hhad_dict['a']]
        hhad_double_configs = [
            ('HHAD让胜让平双选', 0, 1),
            ('HHAD让胜让负双选', 0, 2),
            ('HHAD让平让负双选', 1, 2),
        ]
        for label, idx1, idx2 in hhad_double_configs:
            p1 = hhad_probs[idx1]
            p2 = hhad_probs[idx2]
            odds1 = hhad_odds_list_dbl[idx1]
            odds2 = hhad_odds_list_dbl[idx2]
            combined_prob = p1 + p2
            if combined_prob > 0:
                avg_odds = round((p1 * odds1 + p2 * odds2) / combined_prob, 2)
            else:
                avg_odds = 0
            roi = (p1 * odds1 + p2 * odds2 - 2) / 2
            ev_pct = roi * 100
            effective_odds = avg_odds / 2 if avg_odds > 0 else 0
            kelly = kelly_criterion(combined_prob, effective_odds, hhad_margin) if effective_odds > 1 else {'stake_pct': 0}
            double_value = roi > 0 and (roi * 100) >= hhad_margin / 2
            all_options.append({
                'market': 'HHAD双选',
                'option': label,
                'prob': round(combined_prob * 100, 1),
                'odds': avg_odds,
                'odds_detail': f'{odds1}/{odds2}',
                'bets': 2,
                'cost': 4,
                'ev_pct': round(ev_pct, 1),
                'kelly_pct': kelly['stake_pct'],
                'value': double_value,
            })

    # Ultra 3.0: 移除all_ev和value_bets计算 (prob模式下不需要, 节省计算+token)
    # EV信息已包含在all_options每项的ev_pct字段中, 可按需提取

    # ===== 主推选择 (只支持命中率优先 mode='prob'; ev/hybrid 已移除, 见 Ultra 11.33) =====
    # 理由: 用户铁律 "命中率第一优先, EV仅作展示参考"。足球每场独立, EV是重复投注概念,
    #       对单场预测意义有限。旧 ev/hybrid 分支含 EV 硬过滤(EV>=-10%才推平局、
    #       EV作决胜排序), 违背该原则, 已整体移除。EV仍计算并随 all_options 展示, 不参与选推。
    # Pro 3.8: 主推只从单选中选, 双选是3选2概率天然高, 不代表方向判断。
    primary_bet = None
    risk_assessment = ''
    all_ranked = sorted(all_options, key=lambda x: x['prob'], reverse=True)
    single_options = [o for o in all_options if o['market'] in ('HAD', 'HHAD')]
    single_ranked = sorted(single_options, key=lambda x: x['prob'], reverse=True)
    primary_bet = single_ranked[0] if single_ranked else None

    # ===== HHAD独立主推 (Pro 3.5) =====
    # 问题: HAD双选概率天然高于HHAD单选, 概率优先模式下HHAD永远无法成为主推
    # 解决: 额外输出HHAD主推, 从3个HHAD单选按概率选最优
    hhad_options = [o for o in all_options if o['market'] == 'HHAD']
    hhad_primary_bet = None
    if hhad_options:
        hhad_ranked = sorted(hhad_options, key=lambda x: x['prob'], reverse=True)
        hhad_primary_bet = hhad_ranked[0]

    # ===== 双选保险方案 (Pro 3.8) =====
    # 双选是3选2, 概率天然高, 不作为主推(主推代表方向判断)
    # 但作为保险方案独立输出, 供用户在方向不够明确时参考
    #
    # Ultra 11.23 (用户纠正): 双选择优改为命中率(prob)第一优先, EV仅作展示参考
    # Ultra 9.4: 双选必须与主推方向一致 (ERR-20260804-003)
    # 之前: 双选可能与主推方向完全相反(主推胜+双选平负覆盖全部结果), 推荐失去意义
    # 修复: 先过滤出包含主推方向的双选, 再在其中按命中率取最高
    # Ultra 11.22: 双选不再只限HAD, HHAD双选纳入候选 (用户要求)
    # HAD双选: 胜平/胜负/平负 (主玩法保险); HHAD双选: 让胜让平/让胜让负/让平让负 (让球盘保险)
    # 主推为HHAD方向时, HHAD双选(含主推让方向)与HAD双选同台择优;
    # 平局盲区补偿仅对HAD双选生效(平是HAD概念, 让球无直接平), 但HHAD双选命中率更高可覆盖
    # Ultra 11.23: 双选择优改为命中率(prob)第一优先, EV仅作展示参考 (用户纠正)
    #   用户铁律: "推荐的命中率为第一优先而不是看EV! 足球不是抛硬币, 每场几乎都是独立!"
    #   与 mode='prob' 主推定位一致(命中率优先, EV不参与排序), 双选同为推荐, 必须同口径
    had_double_options = [o for o in all_options if o['market'] == 'HAD双选']
    hhad_double_options = [o for o in all_options if o['market'] == 'HHAD双选']
    all_doubles = had_double_options + hhad_double_options
    double_recommend = None
    # Ultra 11.9: 双选并列输出 — 平局盲区补偿触发时, 双选作为"并列主推"输出
    # 回归: 260806周四002 平局概率进前二, 双选"胜平"覆盖实际平局, 是三条校准中唯一挽回命中的
    # 效果: 平局盲区补偿不只在保险位, 而是并列主推, 提高用户实际落地率
    double_parallel_output = False
    if all_doubles:
        # 主推方向 → 双选必须包含主推结果
        # HAD胜/平/负 以 胜/平/负 结尾; HHAD让胜/让平/让负 以 让X 表达
        primary_dir = (primary_bet or {}).get('option', '')
        primary_mkt = (primary_bet or {}).get('market', '')
        if primary_dir:
            pref_result = primary_dir[-1]  # 胜/平/负
            pref_hhad = primary_dir[4:] if primary_mkt == 'HHAD' else ''  # 让胜/让平/让负
            dbl_coverage = {
                'HAD胜平双选': {'胜', '平'},
                'HAD胜负双选': {'胜', '负'},
                'HAD平负双选': {'平', '负'},
            }
            hhad_dbl_cov = {
                'HHAD让胜让平双选': {'让胜', '让平'},
                'HHAD让胜让负双选': {'让胜', '让负'},
                'HHAD让平让负双选': {'让平', '让负'},
            }
            # 与主推方向一致的双选 (必须包含主推结果)
            aligned = []
            # Ultra 12.1: HHAD双选始终纳入候选 — 原逻辑仅主推为HHAD方向时才纳入,
            #   导致主推为HAD时让球双选被排除在池外(003让胜让平76%@2.41被漏)。
            #   纳入后由达标池按概率+赔率竞争, 与主推不兼容的HHAD双选概率天然低,
            #   进不了DOUBLE_QUALIFY_PROB达标池, 不会污染推荐。
            if pref_hhad:
                aligned += [o for o in hhad_double_options
                            if pref_hhad in hhad_dbl_cov.get(o.get('option', ''), set())]
            else:
                aligned += hhad_double_options
            # HAD双选: 始终按末字(胜/平/负)对齐, 作为跨市场保险基准
            aligned += [o for o in had_double_options
                        if pref_result in dbl_coverage.get(o.get('option', ''), set())]
            # Ultra 12.1: 剔除押注主推对立面的双选 — 达标池按赔率选时,
            #   方向矛盾的组合(如主推胜却含"负"的HAD胜负=分胜负)可能因赔率高被误选。
            #   003: HAD胜负75%@2.53(含客赢10%)赔率>让胜让平76%@2.41, 但方向矛盾,
            #   剔除对立面后让胜让平才正确胜出。对立面映射: 胜↔负/让负, 负↔胜/让胜。
            _opp_map = {'胜': {'负', '让负'}, '负': {'胜', '让胜'},
                        '平': {'胜', '负', '让胜', '让负'}}
            _opp = _opp_map.get(pref_result, set())
            if _opp:
                def _dbl_covers(o):
                    return (dbl_coverage.get(o.get('option', '')) or
                            hhad_dbl_cov.get(o.get('option', ''), set()))
                aligned = [o for o in aligned if not (_dbl_covers(o) & _opp)]
            # Ultra 11.8: 平局盲区低估补偿 (HAD专项回归 2026-08-06)
            # 回归发现(51场): 预测平局仅4场(7.8%), 实际平局13场(25.5%), 11场平局被完全漏掉
            # 根因: 模型平均平局概率27.8%标定良好, 但平局概率从未成为argmax(方向选择)
            # 修正: 当平局概率进入前二且接近胜/负(top2差<容差)时, 双选优先覆盖平局
            #       即 主推胜→胜平双选, 主推负→平负双选, 避免"硬砍平局"漏判
            # 效果: 不改变主推方向(平局仍非argmax), 但双选保险覆盖平局盲区
            #
            # Ultra 11.11: 平局盲区触发条件增强 (深度因子分析 LRN-20260809-002)
            # 因子发现(83场): 预测胜/负但实际平局23%误判率, "平"方向F1=0.00
            # 原逻辑要求 top2差<容差(3pp) 才触发, 但平局漏判代价=0命中
            # 增强: 平局概率≥25%(普通)或 top2差<容差(势均) 即触发, 放宽覆盖平局的门槛
            if aligned and len(had_probs) >= 3:
                _draw_idx = 1  # HAD 平局索引
                _sp0, _sp1, _sp2 = sorted(had_probs, reverse=True)
                _top2_gap = _sp0 - _sp1
                _draw_prob = had_probs[_draw_idx]
                _draw_in_top2 = (_sp1 == _draw_prob or _sp2 == _draw_prob)  # 平局进前二或前三
                # 触发: 平局概率≥25% 或 (平局进前二 且 top2差<容差)
                _draw_trigger = (_draw_prob >= 0.25) or (_draw_in_top2 and _top2_gap < HYBRID_PROB_TOLERANCE / 100.0)
                if _draw_trigger:
                    # Ultra 12.1: 平局盲区不再强制裁剪 aligned=[draw_base],
                    #   改为仅标记并列主推(parallel), 让达标池在完整候选中按赔率竞争。
                    #   原裁剪会让让胜让平等高赔率HHAD双选被draw_base(仅HAD胜平)顶替,
                    #   违背"命中率达标后赔率优先"。平局风险经parallel标记+insight保留提示。
                    #   注: 让胜让平等不覆盖平局的组合若达标且赔率更高会胜出(003场景),
                    #   此时平局靠"平局关注"提示而非双选兜底, 属用户确认的赔率优先取舍。
                    double_parallel_output = True
            if aligned:
                # Ultra 12.1: 双选达标池+赔率优先 (用户铁律: 命中率达标后兼顾盈利)
                # 概率≥DOUBLE_QUALIFY_PROB的双选均已满足"命中率第一"底线,
                # 池内改按赔率(avg_odds)最高选 — 同稳度下取更优赔率
                # 例003: HAD胜平90%@2.26 vs 让胜让平76%@2.41 → 均达标, 选赔率更高的让胜让平
                _qualified = [o for o in aligned if o['prob'] >= DOUBLE_QUALIFY_PROB]
                if _qualified:
                    double_recommend = sorted(_qualified, key=lambda x: x['odds'], reverse=True)[0]
                else:
                    double_recommend = sorted(aligned, key=lambda x: x['prob'], reverse=True)[0]
                # Ultra 11.9: 并列输出时, 双选为主推方向的并列保险(主推胜→胜平, 主推负→平负)
                if double_parallel_output:
                    double_recommend = dict(double_recommend)
                    double_recommend['parallel'] = True
        if double_recommend is None:
            # 无主推 或 无法对齐时, 回退: 达标池内赔率最高, 否则命中率最高
            _qualified = [o for o in all_doubles if o['prob'] >= DOUBLE_QUALIFY_PROB]
            if _qualified:
                double_recommend = sorted(_qualified, key=lambda x: x['odds'], reverse=True)[0]
            else:
                double_recommend = sorted(all_doubles, key=lambda x: x['prob'], reverse=True)[0]

    # ===== 纯方向判断 (Pro 3.9) =====
    # 从真单选中选概率最高的, 排除伪单选(打包两结果的)
    # 伪单选概率虚高(覆盖两个结果), 真单选才是纯方向判断
    real_singles = [o for o in single_options if o.get('selection_type') == '真单选']
    pure_direction_bet = sorted(real_singles, key=lambda x: x['prob'], reverse=True)[0] if real_singles else None

    # ===== 净胜球概率分布 (穿盘风险分析) — Ultra 4.0: 使用共享函数 =====
    # Ultra 4.0: 复用compute_dc_matrix, 避免重复计算(原~15行→1行)
    dc_probs, margin_probs = compute_dc_matrix(lam_h, lam_a, use_negbin=True, use_dc=True)

    # margin_probs 已由 compute_dc_matrix 通过 Skellam 分布计算

    # 净胜球分布汇总
    p_win_2plus = sum(v for k, v in margin_probs.items() if k >= 2)
    p_win_1 = margin_probs.get(1, 0)
    p_draw = margin_probs.get(0, 0)
    p_lose = sum(v for k, v in margin_probs.items() if k < 0)

    margin_dist = {
        'win_2plus': round(p_win_2plus * 100, 1),
        'win_1': round(p_win_1 * 100, 1),
        'draw': round(p_draw * 100, 1),
        'lose': round(p_lose * 100, 1),
    }

    # ===== Ultra 11.33: 胜平负双推荐 = 6单选概率Top2 (命中率第一优先) =====
    # 背景: 用户明确要求 "推荐只取6个选项中概率最高的两项" (胜平负列含had+hhad).
    #       旧实现(Ultra 11.30/11.32)把净胜球拆原子区间再映射选项, 但 win 被拆成
    #       win_2plus + win_1 两片, HAD胜(覆盖两片,P≈48%)被碎片化, 反被单个的
    #       lose/draw 区间比下去 → 出现 "主推HAD胜, 双推却推HAD负/平" 的主推矛盾,
    #       且可能把概率最低的HAD选项推进推荐 (违背"命中率第一优先"). (ERR-20260810-011)
    # 方案: 直接对6个单选(HAD胜/平/负 + HHAD让胜/让平/让负)按模型融合概率降序取Top2.
    #       - 主推必然在推荐首位 (主推=概率最高单选), 不再矛盾
    #       - 命中率第一, EV仅标注
    # 冗余规避: 若Top2为同一选项(受让盘/半球盘区间映射重合的旧病), 顺延取下一个不同选项.
    _singles_sorted = sorted(single_options, key=lambda x: x['prob'], reverse=True)
    wdl_picks = []
    for _o in _singles_sorted:
        if len(wdl_picks) >= 2:
            break
        _key = (_o['market'], _o['option'])
        if _key not in {(p['market'], p['option']) for p in wdl_picks}:
            wdl_picks.append({'market': _o['market'], 'option': _o['option'], 'prob': _o['prob']})
    # 兜底: 万一单选不足2个, 用主推+HHAD主推补齐
    if len(wdl_picks) < 2:
        for _o in (primary_bet, hhad_primary_bet):
            if _o and (_o.get('market'), _o.get('option')) not in {(p['market'], p['option']) for p in wdl_picks}:
                wdl_picks.append({'market': _o.get('market'), 'option': _o.get('option'), 'prob': _o.get('prob', 0)})

    # 穿盘风险: P(赢恰好|handicap|球) = 让平命中概率
    pass_risk_prob = 0.0
    pass_risk_level = '低'
    pass_risk_desc = ''

    # Ultra 11.11: 让平高发窗口检测 (深度因子分析 LRN-20260809-002)
    # 因子发现(83场): 受让盘让平率36% vs 让球盘23%; 中等难度(45-65)让平率高达44%
    # 让平盲区根因: ①受让盘主队"恰好输1球"易被让平 ②中等难度双方无压倒性身位最易被让平咬住
    # Ultra 11.14: 触发条件回测放宽 (LRN-20260809-005)
    #   回测(19场让平): 原条件(恰1球>20%+中难度45-65)覆盖率仅63%(12/19);
    #   放宽(恰1球≥18%+难度40-70)覆盖率84%(16/19), 对照场双选命中率75%→80%不降反升
    #   因为让平覆盖双选覆盖"让平侧HAD方向+平"两个大类, 命中率高, 放宽触发反而更稳
    #   受让盘让平率36%>让球盘23%, 故受让盘不强制难度限制(恰1球≥18%即标记)
    # Ultra 11.15: 大规模回测修正触发信号 (LRN-20260809-006, historical_odds.db 4397场)
    #   大规模下受让盘让平率23.9% vs 让球盘24.9% 几乎无差(小样本36%>23%不成立);
    #   恰1球≥20%区分度弱; 真正有区分度的是 HHAD让平隐含概率:
    #   未去margin HHAD让平P≥0.30 → 让平率29.4% (843场) vs 基准24.6% (+4.8pp)
    #   是唯一统计显著的让平强信号, 双选命中85.8%
    # Ultra 11.16: 恰1球辅助收紧 (LRN-20260809-007, 4397场)
    #   原辅助"恰1球≥0.20"太宽泛: 触发率被拉到65%, 补充场让平率仅22.3%(低于基准),
    #   把主信号区分度(+4.8)稀释到几乎归零(+0.5); 辅助收紧到恰1球≥0.26
    #   (补充场让平率30.2% +5.6pp, 与主信号同向, 不稀释)
    #   最终: 主信号未去margin HHAD让平P≥0.30 OR 辅助恰1球≥0.26
    let_draw_hotspot = False
    _shou_rang = (handicap > 0)  # 受让盘
    # Ultra 11.15: HHAD让平隐含概率 (1/让平赔率) — 大规模验证的最强让平信号
    _hhad_draw_odds = hhad_dict.get('d') if hhad_dict else None
    _hhad_draw_impl = (1.0 / _hhad_draw_odds) if _hhad_draw_odds and _hhad_draw_odds > 1.0 else 0.0

    if handicap < 0:  # 主队让球
        abs_h = abs(int(handicap))
        pass_risk_prob = margin_probs.get(abs_h, 0)
        if pass_risk_prob >= 0.25:
            pass_risk_level = '高'
            pass_risk_desc = f'主队赢恰好{abs_h}球概率{pass_risk_prob*100:.0f}%，穿盘风险高，让平值得考虑'
        elif pass_risk_prob >= 0.15:
            pass_risk_level = '中'
            pass_risk_desc = f'主队赢恰好{abs_h}球概率{pass_risk_prob*100:.0f}%，穿盘风险中等，让平有可能'
        else:
            pass_risk_level = '低'
            pass_risk_desc = f'主队赢恰好{abs_h}球概率{pass_risk_prob*100:.0f}%，穿盘风险低'
        # Ultra 11.16: 让球盘触发 = HHAD让平P≥0.30(主信号) OR 恰1球≥0.26(收紧辅助)
        if _hhad_draw_impl >= 0.30 or pass_risk_prob >= 0.26:
            let_draw_hotspot = True
    elif handicap > 0:  # 主队受让
        abs_h = abs(int(handicap))
        pass_risk_prob = margin_probs.get(-abs_h, 0)
        if pass_risk_prob >= 0.25:
            pass_risk_level = '高'
            pass_risk_desc = f'主队输恰好{abs_h}球概率{pass_risk_prob*100:.0f}%，穿盘风险高，让平值得考虑'
        elif pass_risk_prob >= 0.15:
            pass_risk_level = '中'
            pass_risk_desc = f'主队输恰好{abs_h}球概率{pass_risk_prob*100:.0f}%，穿盘风险中等'
        else:
            pass_risk_level = '低'
            pass_risk_desc = f'主队输恰好{abs_h}球概率{pass_risk_prob*100:.0f}%，穿盘风险低'
        # Ultra 11.16: 受让盘触发 = HHAD让平P≥0.30(主信号) OR 恰1球≥0.26(收紧辅助)
        if _hhad_draw_impl >= 0.30 or pass_risk_prob >= 0.26:
            let_draw_hotspot = True
    else:
        pass_risk_desc = '无让球，无穿盘风险'

    pass_risk = {
        'prob': round(pass_risk_prob * 100, 1),
        'level': pass_risk_level,
        'desc': pass_risk_desc,
        'let_draw_hotspot': let_draw_hotspot,  # Ultra 11.11: 让平高发窗口标记
    }
    # Ultra 11.11: 让平高发窗口 → 穿盘风险至少"中" (让平率高发=穿盘高发)
    # 让平高发窗口让平率44%(中等难度)远高于让球盘23%, 是穿盘风险的强信号
    if let_draw_hotspot and pass_risk_level == '低':
        pass_risk['level'] = '中'
        pass_risk['desc'] = (pass_risk_desc + ' | 让平高发窗口(受让/中难度), 注意让平覆盖').strip()

    # ===== 风险评估 (命中率优先, EV仅作参考) =====
    if primary_bet:
        # Ultra 3.0: 按需计算EV最高选项 (EV不参与选推, 仅作对比提示)
        ev_best = max(all_options, key=lambda x: x['ev_pct']) if all_options else None
        if ev_best and ev_best != primary_bet:
            ev_gap = ev_best['ev_pct'] - primary_bet['ev_pct']
            if ev_gap > 10:
                risk_assessment += (
                    f"EV参考: {ev_best['option']}(EV={ev_best['ev_pct']}%)"
                    f"比主推高{ev_gap:.0f}%, 命中率优先选概率最高"
                )

        # 低赔率薄利提示
        if primary_bet['odds'] < 1.60:
            if not risk_assessment:
                risk_assessment = ''
            risk_assessment += (
                f"。主推赔率{primary_bet['odds']}偏低, 建议小注"
            )

    # ===== 综合洞察生成 (Ultra 1.0: 精简, 降低token) =====
    insight_parts = []
    mode_label = '命中优先'

    def fmt_bet(bet, prefix=''):
        if not bet:
            return ''
        return f"{prefix}{bet['option']}@{bet['odds']}(P={bet['prob']}%,EV={bet['ev_pct']}%)"

    if primary_bet:
        insight_parts.append(f"[{mode_label}]主推{fmt_bet(primary_bet)}")
    if hhad_primary_bet:
        insight_parts.append(f"HHAD主推{fmt_bet(hhad_primary_bet)}")
    if pure_direction_bet and pure_direction_bet['option'] != (primary_bet['option'] if primary_bet else ''):
        insight_parts.append(f"纯方向{fmt_bet(pure_direction_bet)}")
    if double_recommend:
        if double_recommend.get('parallel'):
            insight_parts.append(f"双选并列主推{double_recommend['option']}@{double_recommend['odds']}(P={double_recommend['prob']}%,EV={double_recommend['ev_pct']}%,平局盲区覆盖)")
        else:
            insight_parts.append(f"双选保险{double_recommend['option']}@{double_recommend['odds']}(P={double_recommend['prob']}%,EV={double_recommend['ev_pct']}%)")
    if risk_assessment:
        insight_parts.append(risk_assessment.lstrip('。'))
    if pass_risk_level in ('高', '中'):
        insight_parts.append(pass_risk_desc)

    # 让平价值提示 (仅在有正EV或高穿盘风险时)
    # Ultra 2.0: 修复 opt_map 在定义前使用的bug
    opt_map = {o['option']: o for o in all_options}  # 提前定义
    let_draw_opt = opt_map.get('HHAD让平')
    # Ultra 11.11: 让平高发窗口消费 (深度因子分析 LRN-20260809-002)
    # 因子发现(83场): 受让盘让平率36% vs 让球盘23%; 中等难度45-65让平率44%
    # 让平高发窗口 = 受让/让球盘 + 恰好1球差>20% + 中等难度
    # 此时即使让平EV不为正, 也主动提示让平作为高发方向的覆盖选项
    if let_draw_opt:
        if let_draw_opt['value']:
            insight_parts.append(f"让平正EV({let_draw_opt['ev_pct']}%)@{let_draw_opt['odds']},价值投注")
        elif pass_risk_level == '高':
            insight_parts.append(f"让平EV={let_draw_opt['ev_pct']}%@{let_draw_opt['odds']},穿盘高风险")
        if let_draw_hotspot:
            insight_parts.append(f"让平高发窗口(让平率高达44%,受让/中难度),建议覆盖让平@{let_draw_opt['odds']}")

    # ===== Ultra 11.13: 让平覆盖双选 (LRN-20260809-004 深度因子/回归) =====
    # 背景: 用户被让平坑太多次, 要求"让平概率大时体现在胜平负(HAD)双选里, 不单推让平"
    # 回归(88场): 实际让平23场, 现状单选(让胜/让负)0/23全失(-100%);
    #   含让平双选 23/23覆盖让平; HAD双选"让平侧+平"能兜住让平+HAD平局
    # 因子: xG差NOT让平信号(让平|ΔxG|0.19 vs 非0.13, 让平反而xG差更大);
    #   中等难度(45-65)才是让平最强信号(让平64% vs 非让平25%, 3倍区分度)
    # 关键映射: 让平在HAD里的对应结果 —
    #   让球盘(主让-1..): 让平=主队恰好赢1球 = HAD"胜"
    #   受让盘(主受+1..): 让平=主队恰好输1球 = HAD"负"
    # 设计: 让平高发窗口(let_draw_hotspot)触发时, 强制HAD双选落在"让平侧+平",
    #   即 让球盘→HAD胜平双选, 受让盘→HAD平负双选 (让平侧+平, 兜住让平+平局)
    #   不改变主推方向(让胜/让负仍是概率最高), 双选作为并列保险输出
    let_draw_double = None
    if let_draw_hotspot and had_dict and 'h' in had_dict:
        _ld_odds_list = [had_dict['h'], had_dict['d'], had_dict['a']]
        # 让平对应的HAD侧 + 平, 构成覆盖双选
        if handicap < 0:  # 让球盘: 让平=HAD胜 → 胜平双选
            _ld_cover_idx = 0  # HAD胜
            _ld_cover_label = 'HAD胜平双选'
            _ld_cover_desc = '主队让球,让平=主队恰好赢1球=HAD胜,胜平双选兜住让平+平局'
        else:  # 受让/平盘: 让平=HAD负 → 平负双选
            _ld_cover_idx = 2  # HAD负
            _ld_cover_label = 'HAD平负双选'
            _ld_cover_desc = '主队受让,让平=主队恰好输1球=HAD负,平负双选兜住让平+平局'
        _p_side = had_probs[_ld_cover_idx]  # 让平侧的HAD概率(胜或负)
        _p_draw = had_probs[1]              # 平
        _o_side = _ld_odds_list[_ld_cover_idx]
        _o_draw = _ld_odds_list[1]
        _combined = _p_side + _p_draw
        if _combined > 0:
            _ld_avg_odds = (_p_side * _o_side + _p_draw * _o_draw) / _combined
            _ld_roi = (_p_side * _o_side + _p_draw * _o_draw - 2) / 2
            let_draw_double = {
                'market': 'HAD双选',
                'option': _ld_cover_label,
                'prob': round(_combined * 100, 1),
                'odds': round(_ld_avg_odds, 2),
                'odds_detail': f'{_o_side}/{_o_draw}',
                'bets': 2,
                'cost': 4,
                'ev_pct': round(_ld_roi * 100, 1),
                'kelly_pct': kelly_criterion(_combined, _ld_avg_odds / 2, had_margin)['stake_pct'],
                'let_draw_cover': True,
                'direction': '胜平' if _ld_cover_idx == 0 else '平负',
                'desc': _ld_cover_desc,
            }
            # 让平覆盖双选作为"并列双选"输出 (覆盖原双选, 不改变主推)
            if pass_risk_level in ('高', '中'):
                double_recommend = dict(let_draw_double)
                double_parallel_output = True
                insight_parts.append(
                    f"让平覆盖双选{_ld_cover_label}@{_ld_avg_odds:.2f}"
                    f"(P={_combined*100:.0f}%,ROI={_ld_roi*100:+.0f}%,{_ld_cover_desc})"
                )

    # ===== Ultra 11.17: 让平直推 (LRN-20260809-005) =====
    # 背景: 用户要求预测报告直接看到"让平"推荐。历史实际让平率≈25%(近1/4),
    #   但让平概率(23-31%)从未成为HHAD argmax(让胜/让负更高), 导致报告从不直接推让平,
    #   让平只作为HAD双选(让平侧+平)兜底出现, 用户无法直观看到单推让平。
    # 依据: 模型自检验证(843场)显示 未去margin HHAD让平P≥0.30 → 让平实际率29.4%(+4.8pp),
    #   是唯一统计显著的让平强信号; 让球/受让盘基准让平率≈24.6%。
    # 设计: 让平作为独立直推信号输出, 不再只藏在覆盖双选里。触发条件(满足任一):
    #   Ultra 11.32: 去掉条件②的"EV>0"硬门槛 — EV仅参考, 不参与推荐触发 (ERR-20260810-008)
    #   ① HHAD让平概率≥0.28 (接近模型最强信号0.30, 显著高于基准24.6%)
    #   ② HHAD让平概率≥0.25 且 让平高发窗口(let_draw_hotspot)触发
    #   若让平已是HHAD主推(hhad_primary_bet), 不重复输出(主推卡已展示)。
    let_draw_rec = None
    if let_draw_opt:
        _ldr_prob = let_draw_opt.get('prob', 0)   # 已为百分比(如26.1)
        _ldr_ev = let_draw_opt.get('ev_pct', 0)   # 已为百分比(如+9.6)
        _ldr_odds = let_draw_opt.get('odds', 0)
        _ldr_already_primary = (hhad_primary_bet or {}).get('option') == 'HHAD让平'
        _ldr_trigger = (_ldr_prob >= 28) or (_ldr_prob >= 25 and let_draw_hotspot)
        if _ldr_trigger and not _ldr_already_primary:
            let_draw_rec = {
                'market': 'HHAD',
                'option': 'HHAD让平',
                'prob': round(_ldr_prob, 1),
                'odds': _ldr_odds,
                'odds_detail': str(_ldr_odds),
                'ev_pct': _ldr_ev,
                'let_draw_direct': True,
                'direction': '让平',
                'desc': '让平直推: 模型对让平(恰好1球差)给出独立推荐',
            }
            _ldr_reason = []
            if _ldr_prob >= 28:
                _ldr_reason.append(f"P={_ldr_prob:.0f}%≥28%强信号")
            if let_draw_hotspot:
                _ldr_reason.append("让平高发窗口")
            insight_parts.append(
                f"让平直推@{_ldr_odds}(P={_ldr_prob:.0f}%,EV={_ldr_ev:+.0f}%,"
                f"历史让平率≈25%{(';'+';'.join(_ldr_reason)) if _ldr_reason else ''})"
            )

    # ===== Ultra 11.18: 平局关注 (LRN-20260809-008) =====
    # 背景: 用户质疑"足球胜平负只有三种结果, 却从不见主推平"。
    #   实证(4484场历史): 全局真实平局率24.7%, 但市场隐含平局概率均值仅24.5%,
    #   主胜43.7%/客胜31.8%均显著更高; 平局隐含概率成为HAD三选项最高的场次仅0.2%。
    #   且主推常被HHAD让方向(伪单选, 覆盖两结果)抢走, 故平局只作为双选兜底出现。
    # 设计: 当融合后平局概率显著高于基准(≥30%)且平局不是整体主推时,
    #   输出平局关注标记, 与让平直推一样以醒目一行文字提示, 而非只藏双选。
    # 触发: 平局概率≥30% (历史实证: 平局隐含30-34%时实际平局率36.6%, +12pp显著)
    draw_attention = None
    if had_probs and len(had_probs) >= 3:
        _dar_p = had_probs[1]  # HAD平局融合概率
        _dar_odds = had_dict.get('d', 0) if had_dict else 0
        _primary_is_draw = (primary_bet or {}).get('option') == 'HAD平'
        # 平局概率显著偏高(≥30%) 且 平局不是整体主推(避免重复)
        if _dar_p >= 0.30 and not _primary_is_draw:
            _dar_ev = (_dar_p * _dar_odds - 1) if _dar_odds else 0
            draw_attention = {
                'market': 'HAD',
                'option': 'HAD平',
                'prob': round(_dar_p * 100, 1),
                'odds': _dar_odds,
                'odds_detail': str(_dar_odds),
                'ev_pct': round(_dar_ev * 100, 1),
                'draw_attention': True,
                'direction': '平',
                'desc': '平局关注: 平局概率显著高于基准(24.7%), 注意平局',
            }
            insight_parts.append(
                f"平局关注@{_dar_odds}(P={_dar_p*100:.0f}%,EV={_dar_ev*100:+.0f}%,"
                f"历史平局率≈25%,平局概率显著偏高高发)"
            )

    # ===== Ultra 11.19: 平局窗口HHAD优先 (LRN-20260809-009) =====
    # 背景: 260809周日001-011因子分析发现 平局场次 HAD 0/3(0%) vs HHAD 2/3(67%)
    #   平局是当前最大漏点(全库101场实际平局27场, 仅预测6场, 漏判20场)。
    #   根因: HAD单选制下平局概率再高(26-32%)也排不进胜/负之前成为argmax,
    #   平局概率标定良好却永远选不中 → HAD对平局结构性失效。
    # 关键洞察: 平局场次的判别力在让球盘(HHAD)而非胜平负(HAD)。
    #   让球盘把强弱拉到接近, 让平(平局)窗口天然更高, 且HHAD方向不受
    #   "平局概率排不进前二"的约束(让胜/让负概率更分散, 让平更接近主导)。
    # 设计: 当HAD平局概率≥30%(平局关注触发)时, 标记此场为"平局窗口",
    #   提示用户 HHAD 优先参考(让球方向判别力更强), 而非只看HAD主胜/主负。
    #   不改变HAD主推方向(平局仍非argmax), 不改变HHAD概率(模型已标定),
    #   仅新增一个"平局场次让球盘优先"的消费侧标记, 供PDF/JSON醒目展示。
    draw_window_hhad_priority = False
    # 升级9: 平局窗口 logistic 概率化 — 硬阈值(平P≥30%)一刀切,
    # 模型用 [P平, top2差, |让球|, 联赛平局率] 输出HHAD判别力优于HAD的概率,
    # 回归库样本充足时按概率触发(P≥0.6), 缺参时自动回退下方硬规则。
    _dw_model_p = None
    if _MU and UPGRADES.get('draw_window_model') and _UPG_PARAMS.get('draw_window'):
        try:
            _dwm = _MU.DrawWindowModel()
            _dwm.w = _UPG_PARAMS['draw_window']['w']
            _sp_sorted = sorted(had_probs, reverse=True)
            _dw_x = [had_probs[1], _sp_sorted[0] - _sp_sorted[1],
                     abs(handicap or 0), 0.25]
            _dw_model_p = _dwm.predict(_dw_x)
        except Exception:
            _dw_model_p = None
    if draw_attention is not None:
        draw_window_hhad_priority = True
        _hhad_dir_val = (hhad_primary_bet or {}).get('option', '')
        _hhad_odds_val2 = (hhad_primary_bet or {}).get('odds', 0)
        # Ultra 11.19: HHAD主推缺失(未开盘)时, 给出通用让球盘优先提示, 避免"参考@0"
        if _hhad_dir_val and _hhad_odds_val2 and _hhad_odds_val2 > 0:
            _dw_ref = f"HHAD参考{_hhad_dir_val}@{_hhad_odds_val2}"
        else:
            _dw_ref = ("让球盘方向优先参考HHAD" if (hhad or {}).get('h', 0) > 0
                       else "让球盘未开盘, 平局概率偏高注意提防")
        insight_parts.append(
            f"平局窗口HHAD优先: HAD平局P={_dar_p*100:.0f}%≥30%,让球盘判别力更稳"
            f",{_dw_ref}"
        )
    elif _dw_model_p is not None and _dw_model_p >= 0.6:
        # 模型触发(硬规则未触发时): 平局概率未达30%但特征组合指向HHAD更稳
        draw_window_hhad_priority = True
        insight_parts.append(
            f"平局窗口HHAD优先(模型P={_dw_model_p:.0%}≥60%): 让球盘判别力更稳"
        )

    # 净胜球分布提示
    if p_win_1 > p_win_2plus:
        insight_parts.append(f"赢1球({p_win_1*100:.0f}%)>赢2+球({p_win_2plus*100:.0f}%),小胜概率大")

    # HHAD单选 vs HAD双选对比
    double_compare = []
    dir_pairs = [
        ('HHAD让负', 'HAD平负双选', '客队不败'),
        ('HHAD让胜', 'HAD胜平双选', '主队不败'),
    ]
    # opt_map 已在上方定义
    for hhad_label, had_label, direction in dir_pairs:
        hhad_opt = opt_map.get(hhad_label)
        had_opt = opt_map.get(had_label)
        if hhad_opt and had_opt:
            prob_diff = had_opt['prob'] - hhad_opt['prob']
            if abs(prob_diff) > 0:
                winner = had_label if prob_diff > 0 else hhad_label
                double_compare.append(f"{direction}:{winner}高{abs(prob_diff):.0f}%")
    if double_compare:
        insight_parts.append(' '.join(double_compare))

    insight = '。'.join(insight_parts) if insight_parts else '无明显信号'
    # 受让盘(+1等)洞察文案统一术语: HHAD让胜/让平/让负 → 受让胜/受让平/受让负
    insight = _hhad_display_label(insight, handicap)

    # Ultra 3.0: 精简返回结构, 降低token消耗 ~40%
    # 移除: value_bets (可从all_ranked派生), risk_assessment/double_compare (已折叠进insight)
    # 精简: all_ranked 仅保留top5 + 每项仅保留核心字段
    def slim_option(o):
        """精简单个选项的字段, 保留核心信息"""
        slim = {
            'opt': o['option'],
            'mkt': o['market'],
            'prob': o['prob'],
            'odds': o['odds'],
            'ev': o['ev_pct'],
        }
        if o.get('selection_type'):
            slim['type'] = o['selection_type']
        return slim

    # Ultra 11.10 铁律: 预测报告须正确标注让/受让 — 受让盘输出副本统一术语
    # (ERR-20260811-002: cross_market 各 HHAD 输出项此前未过 _hhad_display_label,
    #  JSON摘要"HHAD主推: HHAD让胜@..." 与 insight(已整体转换)矛盾;
    #  仅转换输出副本, 原对象保留"让X"供内部逻辑比较(如 _ldr_already_primary=='HHAD让平'))
    def _hhad_out(o):
        if not o or not isinstance(o, dict):
            return o  # 布尔标记(double_parallel_output)等非 dict 原样返回
        cp = dict(o)
        # slim dict 用 'mkt', 完整 dict 用 'market' — 两者都检查
        if str(cp.get('market') or cp.get('mkt') or '').startswith('HHAD'):
            for _f in ('option', 'opt'):
                if _f in cp:
                    cp[_f] = _hhad_display_label(cp[_f], handicap)
        return cp

    all_ranked_slim = [_hhad_out(slim_option(o)) for o in all_ranked[:3]]  # 仅top3

    return {
        'top3': all_ranked_slim,  # Ultra 6.0: 精简为top3
        'primary_bet': _hhad_out(primary_bet),
        'hhad_primary_bet': _hhad_out(hhad_primary_bet),
        'wdl_picks': [_hhad_out(p) for p in wdl_picks],  # Ultra 11.30: 胜平负共斥双推(净胜球互斥Top2)
        'let_draw_rec': _hhad_out(let_draw_rec),  # Ultra 11.17: 让平直推
        'draw_attention': draw_attention,  # Ultra 11.18: 平局关注
        'draw_window_hhad_priority': draw_window_hhad_priority,  # Ultra 11.19: 平局窗口HHAD优先
        'double_recommend': _hhad_out(double_recommend),
        'double_parallel_output': _hhad_out(double_parallel_output),
        'pure_direction_bet': _hhad_out(pure_direction_bet),
        'primary_mode': mode,
        'pass_risk': pass_risk,
        'margin_dist': margin_dist,
        'insight': insight,
    }

def assess_data_quality(data):
    """评估单场比赛数据质量 0-100分
    检查: HAD是否完整、HHAD是否完整、ouzhi是否有数据、shuju是否有数据、daxiao是否有数据、init三盘是否有数据
    """
    score = 0
    details = []
    # HAD完整 (15分)
    if data.get('HAD') and 'h' in data.get('HAD', {}):
        score += 15; details.append('HAD✓')
    else:
        details.append('HAD✗')
    # HHAD完整 (15分)
    if data.get('HHAD') and 'h' in data.get('HHAD', {}):
        score += 15; details.append('HHAD✓')
    else:
        details.append('HHAD✗')
    # ouzhi数据 (15分)
    if data.get('ouzhi'):
        score += 15; details.append('欧指✓')
    else:
        details.append('欧指✗')
    # shuju数据 (20分): 近况10分 + 球队统计10分
    shuju = data.get('shuju', {})
    if shuju:
        has_form = bool(shuju.get('form_home'))
        has_stats = any(k.startswith('stats_') for k in shuju)
        if has_form:
            score += 10
        if has_stats:
            score += 10
        details.append(f'近况{"✓" if has_form else "△"}统计{"✓" if has_stats else "△"}')
    else:
        details.append('近况✗')
    # daxiao数据 (10分)
    if data.get('daxiao', {}).get('num_bookmakers', 0) > 0:
        score += 10; details.append('大小✓')
    else:
        details.append('大小✗')
    # init三盘 (25分)
    init_count = sum(1 for x in ['init_ouzhi', 'init_yazhi', 'init_daxiao'] if data.get(x))
    score += init_count * 8  # 每个8分,最多24分
    if init_count == 3:
        details.append('初赔三盘✓')
    elif init_count > 0:
        details.append(f'初赔{init_count}/3')
    else:
        details.append('初赔✗')
    
    quality = '高' if score >= 80 else '中' if score >= 50 else '低'
    return {'score': score, 'quality': quality, 'details': ' '.join(details)}

# ============================================================
# Ultra 6.6: 联赛标定参数 (从历史数据自动生成, 替代经验值)
# 数据源: predictions/historical_odds.db (680场 2025-2026赛季)
# 自动回退: 无标定数据时使用经验值, 不影响原有逻辑
# ============================================================
# (sqlite3 已在文件顶部导入, 不重复导入)

_CALIBRATION_DB = os.path.join(
    os.environ.get('SPORTTERY_WORKSPACE', os.path.dirname(os.path.abspath(__file__))),
    'predictions', 'historical_odds.db'
)

def _load_league_calibration():
    """从历史数据库加载联赛标定参数
    
    返回: {
        'leagues': {
            '瑞超': {'h_rate': 0.43, 'd_rate': 0.25, 'a_rate': 0.31,
                     'avg_goals': 2.76, 'avg_home_goals': 1.45, 'avg_away_goals': 1.31,
                     'home_adv': 1.15, 'sample_size': 250},
            ...
        },
        'odds_calibration': {  # 赔率→实际概率偏差 (按联赛+赔率区间)
            '瑞超': {'1.0-1.5': {'implied': 0.676, 'actual': 0.725, 'bias': 0.049}, ...},
            ...
        }
    }
    或 None (数据库不存在时)
    """
    if not os.path.exists(_CALIBRATION_DB):
        return None
    
    conn = None
    try:
        conn = sqlite3.connect(_CALIBRATION_DB)
        c = conn.cursor()
        
        leagues = {}
        # 按联赛汇总 (合并2025+2026赛季, 加权平均)
        c.execute('''SELECT league,
                      COUNT(*) as n,
                      ROUND(SUM(CASE WHEN result='H' THEN 1.0 ELSE 0 END)/COUNT(*), 4) as h_rate,
                      ROUND(SUM(CASE WHEN result='D' THEN 1.0 ELSE 0 END)/COUNT(*), 4) as d_rate,
                      ROUND(SUM(CASE WHEN result='A' THEN 1.0 ELSE 0 END)/COUNT(*), 4) as a_rate,
                      ROUND(AVG(home_score + away_score), 4) as avg_goals,
                      ROUND(AVG(home_score), 4) as avg_home_goals,
                      ROUND(AVG(away_score), 4) as avg_away_goals,
                      ROUND(AVG(home_score - away_score), 4) as avg_gd
                   FROM historical_matches
                   WHERE home_score IS NOT NULL
                   GROUP BY league HAVING COUNT(*) >= 10''')
        
        for row in c.fetchall():
            lg = row[0]
            # 主场优势 = (主队均进球 / 客队均进球) 的平方根 (对称乘数)
            avg_hg = row[6] if row[6] > 0 else 1.3
            avg_ag = row[7] if row[7] > 0 else 1.3
            home_adv = round((avg_hg / avg_ag) ** 0.5, 3) if avg_ag > 0 else 1.15
            # 限制在合理范围 [1.05, 1.35]
            home_adv = max(1.05, min(1.35, home_adv))
            
            leagues[lg] = {
                'h_rate': row[2], 'd_rate': row[3], 'a_rate': row[4],
                'avg_goals': row[5],
                'avg_home_goals': avg_hg, 'avg_away_goals': avg_ag,
                'home_adv': home_adv,
                'avg_gd': row[8],
                'sample_size': row[1],
            }
        
        # 赔率→实际概率标定 (按联赛, 5个赔率区间)
        odds_cal = {}
        bins = [(1.0, 1.5, '1.0-1.5'), (1.5, 2.0, '1.5-2.0'),
                (2.0, 2.5, '2.0-2.5'), (2.5, 3.5, '2.5-3.5'), (3.5, 99, '3.5+')]
        
        c.execute('''SELECT league, sp_had_h, sp_had_d, sp_had_a, result
                     FROM historical_matches
                     WHERE sp_had_h IS NOT NULL AND sp_had_h > 1.0 AND result != '' ''')
        all_odds = c.fetchall()
        
        # 4列版本 (去除league列, 用于全局赔率区间标定)
        all_odds_4 = [(row[1], row[2], row[3], row[4]) for row in all_odds]
        
        # 按联赛分组
        lg_odds = {}
        for row in all_odds:
            lg = row[0]
            if lg not in lg_odds:
                lg_odds[lg] = []
            lg_odds[lg].append((row[1], row[2], row[3], row[4]))
        
        for lg, data in lg_odds.items():
            if len(data) < 20:
                continue
            odds_cal[lg] = {}
            for lo, hi, label in bins:
                bin_data = [(h, d, a, r) for h, d, a, r in data if lo <= h < hi]
                if len(bin_data) < 5:
                    continue
                # 隐含概率 (1/odds normalized)
                implied_h = sum(1/h / (1/h + 1/d + 1/a) for h, d, a, r in bin_data) / len(bin_data)
                actual_h = sum(1 for h, d, a, r in bin_data if r == 'H') / len(bin_data)
                bias = actual_h - implied_h
                # 限制偏差在 ±15pp (防止小样本噪音)
                bias = max(-0.15, min(0.15, bias))
                odds_cal[lg][label] = {
                    'implied': round(implied_h, 4),
                    'actual': round(actual_h, 4),
                    'bias': round(bias, 4),
                    'sample': len(bin_data),
                }
        
        # Ultra 6.8: 按赔率区间的平局率标定 (全局, 用于融合后平局校准)
        # 历史数据发现: 2.5-3.5区间实际平局率33% vs 隐含27% (+6pp)
        # 3.5+区间实际平局率28% vs 隐含21% (+7pp) → 系统性低估
        draw_by_range = {}
        for lo, hi, label in bins:
            bin_data = [(h, d, a, r) for h, d, a, r in all_odds_4 if lo <= h < hi]
            if len(bin_data) < 20:
                continue
            actual_d = sum(1 for h, d, a, r in bin_data if r == 'D') / len(bin_data)
            implied_d = sum(1/d / (1/h + 1/d + 1/a) for h, d, a, r in bin_data) / len(bin_data)
            draw_by_range[label] = {
                'actual_draw_rate': round(actual_d, 4),
                'implied_draw_rate': round(implied_d, 4),
                'draw_bias': round(actual_d - implied_d, 4),
                'sample': len(bin_data),
            }

        # Ultra 6.9: 按平局赔率区间的平局率标定 (最强信号)
        # 数据发现: 平赔2.5-3.0 实际平局率40.9% vs 隐含30.6% (+10.3pp!)
        draw_by_d_odds = {}
        d_bins = [(2.5, 3.0, '2.5-3.0'), (3.0, 3.3, '3.0-3.3'),
                  (3.3, 3.5, '3.3-3.5'), (3.5, 4.0, '3.5-4.0'), (4.0, 99, '4.0+')]
        for lo, hi, label in d_bins:
            bin_data = [(h, d, a, r) for h, d, a, r in all_odds_4 if lo <= d < hi]
            if len(bin_data) < 15:
                continue
            actual_d = sum(1 for h, d, a, r in bin_data if r == 'D') / len(bin_data)
            implied_d = sum(1/d / (1/h + 1/d + 1/a) for h, d, a, r in bin_data) / len(bin_data)
            draw_by_d_odds[label] = {
                'actual_draw_rate': round(actual_d, 4),
                'implied_draw_rate': round(implied_d, 4),
                'draw_bias': round(actual_d - implied_d, 4),
                'sample': len(bin_data),
            }

        # Ultra 6.10: 让球盘口让平率标定 (HHAD专用, 覆盖-1/-2/+2盘口)
        # 历史发现: 低赔区间让平率系统性高于隐含概率4-5pp
        # -1: 839场, 主赔1.0-1.5偏差+4~5pp
        # -2: 52场, 主赔1.0-1.3偏差+5.1pp (与-1同区间一致)
        # +2: 7场 (样本不足, 搭建基础设施待数据积累)
        hcap_bins = [(1.0, 1.3, '1.0-1.3'), (1.3, 1.5, '1.3-1.5'),
                     (1.5, 1.8, '1.5-1.8'), (1.8, 2.0, '1.8-2.0'),
                     (2.0, 2.5, '2.0-2.5'), (2.5, 99, '2.5+')]

        # 各盘口配置: (盘口字符串, 让平时的比分差, 最小样本数)
        # -1球让平: 主队赢1球 (diff=1)
        # -2球让平: 主队赢2球 (diff=2)
        # +1球让平: 主队输1球 (diff=-1)
        # +2球让平: 主队输2球 (diff=-2)
        hcap_configs = [('-1', 1, 15), ('-2', 2, 10), ('+1', -1, 15), ('+2', -2, 5)]

        hcap_calibration = {}
        for gl_str, draw_diff, min_sample in hcap_configs:
            c.execute('''SELECT league, sp_had_h, sp_had_d, sp_had_a, home_score, away_score
                         FROM historical_matches
                         WHERE sp_goal_line = ?
                           AND home_score IS NOT NULL AND away_score IS NOT NULL
                           AND sp_had_h IS NOT NULL AND sp_had_h > 1.0''', (gl_str,))
            hcap_rows = c.fetchall()

            if not hcap_rows:
                continue

            # 按主赔区间的让平率
            by_range = {}
            for lo, hi, label in hcap_bins:
                bin_data = [r for r in hcap_rows if lo <= r[1] < hi]
                if len(bin_data) < min_sample:
                    continue
                actual_d = sum(1 for r in bin_data if r[4] - r[5] == draw_diff) / len(bin_data)
                implied_d = sum(1/r[2] / (1/r[1] + 1/r[2] + 1/r[3]) for r in bin_data) / len(bin_data)
                by_range[label] = {
                    'actual_draw_rate': round(actual_d, 4),
                    'implied_draw_rate': round(implied_d, 4),
                    'draw_bias': round(actual_d - implied_d, 4),
                    'sample': len(bin_data),
                }

            # 按联赛的让平率
            by_league = {}
            lg_hcap = {}
            for r in hcap_rows:
                lg = r[0]
                if lg not in lg_hcap:
                    lg_hcap[lg] = []
                lg_hcap[lg].append(r)
            for lg, data in lg_hcap.items():
                if len(data) < min_sample:
                    continue
                d_count = sum(1 for r in data if r[4] - r[5] == draw_diff)
                by_league[lg] = {
                    'draw_rate': round(d_count / len(data), 4),
                    'sample': len(data),
                }

            if by_range or by_league:
                hcap_calibration[gl_str] = {'by_range': by_range, 'by_league': by_league}

        # Ultra 7.0: 全局赔率区间偏差标定 (跨联赛, 全量数据)
        # 数据发现: 2.5-3.5区间偏差最大 (-4.2pp), 需大幅下调主胜概率
        global_odds_cal = {}
        for lo, hi, label in bins:
            bin_data = [(h, d, a, r) for h, d, a, r in all_odds_4 if lo <= h < hi]
            if len(bin_data) < 50:
                continue
            implied_h = sum(1/h / (1/h + 1/d + 1/a) for h, d, a, r in bin_data) / len(bin_data)
            actual_h = sum(1 for h, d, a, r in bin_data if r == 'H') / len(bin_data)
            global_odds_cal[label] = {
                'implied': round(implied_h, 4),
                'actual': round(actual_h, 4),
                'bias': round(actual_h - implied_h, 4),
                'sample': len(bin_data),
            }

        # Ultra 7.0: 赔率变动信号标定 (从 odds_change_history)
        # 数据发现: 胜赔下降→主胜率高, 上升→主胜率低; 大幅变动(>0.3)→反向信号
        odds_change_sig = {}
        try:
            c.execute('''SELECT oc.match_db_id,
                         MIN(CASE WHEN oc.seq=0 THEN oc.h END) as init_h,
                         MAX(CASE WHEN oc.seq=(
                             SELECT MAX(seq) FROM odds_change_history o2
                             WHERE o2.match_db_id=oc.match_db_id AND o2.odds_type='had'
                         ) THEN oc.h END) as final_h,
                         hm.result
                         FROM odds_change_history oc
                         JOIN historical_matches hm ON hm.id=oc.match_db_id
                         WHERE oc.odds_type='had' AND oc.match_db_id IS NOT NULL
                           AND hm.result IS NOT NULL AND hm.result != ''
                         GROUP BY oc.match_db_id''')
            change_rows = c.fetchall()

            if change_rows:
                # 按变动方向统计 (行格式: match_db_id, init_h, final_h, result)
                drop_data = [(i, f, r) for _, i, f, r in change_rows if i and f and f < i]
                rise_data = [(i, f, r) for _, i, f, r in change_rows if i and f and f > i]
                unchanged_data = [(i, f, r) for _, i, f, r in change_rows if i and f and abs(f - i) < 0.01]

                odds_change_sig = {
                    'drop': {
                        'sample': len(drop_data),
                        'h_rate': round(sum(1 for _, _, r in drop_data if r == 'H') / len(drop_data), 4) if drop_data else 0,
                    },
                    'rise': {
                        'sample': len(rise_data),
                        'h_rate': round(sum(1 for _, _, r in rise_data if r == 'H') / len(rise_data), 4) if rise_data else 0,
                    },
                    'unchanged': {
                        'sample': len(unchanged_data),
                        'h_rate': round(sum(1 for _, _, r in unchanged_data if r == 'H') / len(unchanged_data), 4) if unchanged_data else 0,
                    },
                    'magnitude': {}
                }

                # 按变动幅度统计
                mag_configs = [
                    ('drop_small', drop_data, lambda i, f: abs(f - i) < 0.1),
                    ('drop_medium', drop_data, lambda i, f: 0.1 <= abs(f - i) < 0.3),
                    ('drop_large', drop_data, lambda i, f: abs(f - i) >= 0.3),
                    ('rise_small', rise_data, lambda i, f: abs(f - i) < 0.1),
                    ('rise_medium', rise_data, lambda i, f: 0.1 <= abs(f - i) < 0.3),
                    ('rise_large', rise_data, lambda i, f: abs(f - i) >= 0.3),
                ]
                for mag_name, mag_data, mag_filter in mag_configs:
                    filtered = [(i, f, r) for i, f, r in mag_data if mag_filter(i, f)]
                    if filtered:
                        odds_change_sig['magnitude'][mag_name] = {
                            'h_rate': round(sum(1 for _, _, r in filtered if r == 'H') / len(filtered), 4),
                            'sample': len(filtered),
                        }
        except Exception:
            pass  # odds_change_history 表可能不存在

        if not leagues:
            return None

        result = {'leagues': leagues, 'odds_calibration': odds_cal,
                  'draw_by_odds_range': draw_by_range,
                  'draw_by_d_odds_range': draw_by_d_odds,
                  'hcap_calibration': hcap_calibration,
                  'global_odds_calibration': global_odds_cal,
                  'odds_change_signal': odds_change_sig}
        print(f'  [标定] 加载{len(leagues)}个联赛参数 ({sum(l["sample_size"] for l in leagues.values())}场历史数据)')
        if draw_by_range:
            print(f'  [标定] 主赔区间平局率: {len(draw_by_range)}个区间')
        if draw_by_d_odds:
            print(f'  [标定] 平赔区间平局率: {len(draw_by_d_odds)}个区间')
        for gl_str, data in hcap_calibration.items():
            print(f'  [标定] 让{gl_str}球让平率: {len(data["by_range"])}个赔率区间, {len(data["by_league"])}个联赛')
        if global_odds_cal:
            print(f'  [标定] 全局赔率区间偏差: {len(global_odds_cal)}个区间')
        if odds_change_sig and odds_change_sig.get('magnitude'):
            print(f'  [标定] 赔率变动信号: {len(odds_change_sig["magnitude"])}个变动类别')
        return result
        
    except Exception as e:
        print(f'  [标定] 加载失败, 使用经验值: {e}')
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass

# 启动时加载 (失败则使用经验值, 不影响原有逻辑)
_CALIBRATION = _load_league_calibration()


def get_league_param(league, param, default):
    """获取联赛标定参数, 无数据时回退默认值"""
    if not _CALIBRATION or not league:
        return default
    lg_data = _CALIBRATION['leagues'].get(league)
    if not lg_data:
        return default
    return lg_data.get(param, default)


def calibrate_shin_probs(probs, league, home_odds):
    """联赛标定: 对Shin's method输出按联赛+赔率区间做偏差修正
    
    原理: 标定分析发现Shin方法在不同联赛有系统性偏差
      - 瑞超/芬超 低赔率区间(1.0-1.5): 实际主胜率比隐含高5-7%
      - 瑞超 中赔率区间(2.5-3.5): 实际主胜率比隐含低11%
      - 挪超 高赔率区间(3.5+): 实际主胜率比隐含高10%
    
    修正方式: 在主胜概率上施加有界偏差修正, 平局/客胜按比例补偿
    """
    if not _CALIBRATION or not league:
        return probs
    
    odds_cal = _CALIBRATION.get('odds_calibration', {}).get(league)
    if not odds_cal or not home_odds:
        return probs
    
    # 确定赔率区间
    label = None
    for lo, hi, lbl in [(1.0,1.5,'1.0-1.5'),(1.5,2.0,'1.5-2.0'),
                         (2.0,2.5,'2.0-2.5'),(2.5,3.5,'2.5-3.5'),(3.5,99,'3.5+')]:
        if lo <= home_odds < hi:
            label = lbl
            break
    if not label:
        return probs
    
    cal = odds_cal.get(label)
    if not cal or cal.get('sample', 0) < 5:
        return probs
    
    bias = cal.get('bias', 0)
    if abs(bias) < 0.01:  # 偏差<1pp不调整
        return probs
    
    # 修正主胜概率, 平局承担30%, 客胜承担70% (双向同一补偿比例, 主胜增减主要从客胜补)
    pw, pd, pl = probs
    pw_new = pw + bias
    pd_new = pd - bias * 0.3
    pl_new = pl - bias * 0.7
    
    # 边界保护
    pw_new = max(0.05, min(0.90, pw_new))
    pd_new = max(0.05, min(0.60, pd_new))
    pl_new = max(0.05, min(0.90, pl_new))
    
    # 归一化
    s = pw_new + pd_new + pl_new
    return [pw_new/s, pd_new/s, pl_new/s]


def calibrate_global_odds_bias(probs, home_odds):
    """全局赔率区间偏差校准 — 基于全量3099场比赛数据分析
    
    数据发现 (2933场有赔率+赛果):
      1.0-1.5: 隐含68.2% 实际69.4% 偏差+1.3% → 微调上调
      1.5-2.0: 隐含52.0% 实际50.0% 偏差-2.0% → 下调
      2.0-2.5: 隐含40.1% 实际42.6% 偏差+2.5% → 上调
      2.5-3.5: 隐含30.5% 实际26.3% 偏差-4.2% → 大幅下调 (最大偏差区!)
      3.5+:    隐含18.6% 实际18.0% 偏差-0.6% → 无需调整
    
    该函数在 calibrate_shin_probs 之后调用, 作为全局层补充。
    """
    if not _CALIBRATION or not home_odds or home_odds <= 1:
        return probs
    
    global_cal = _CALIBRATION.get('global_odds_calibration', {})
    if not global_cal:
        return probs
    
    # 确定赔率区间
    label = None
    for lo, hi, lbl in [(1.0,1.5,'1.0-1.5'),(1.5,2.0,'1.5-2.0'),
                         (2.0,2.5,'2.0-2.5'),(2.5,3.5,'2.5-3.5'),(3.5,99,'3.5+')]:
        if lo <= home_odds < hi:
            label = lbl
            break
    if not label:
        return probs
    
    cal = global_cal.get(label)
    if not cal or cal.get('sample', 0) < 50:
        return probs
    
    bias = cal.get('bias', 0)
    if abs(bias) < 0.01:
        return probs
    
    # 限制偏差幅度 (防止过度修正)
    bias = max(-0.06, min(0.06, bias))
    # 回测验证: 2.5-3.5区间过度修正→跳过; 其他区间应用50%
    if label == '2.5-3.5':
        return probs
    bias = bias * 0.5

    # 修正主胜概率, 平局承担30%, 客胜承担70% (双向同一补偿比例)
    pw, pd, pl = probs
    pw_new = pw + bias
    pd_new = pd - bias * 0.3
    pl_new = pl - bias * 0.7
    
    # 边界保护
    pw_new = max(0.05, min(0.90, pw_new))
    pd_new = max(0.05, min(0.60, pd_new))
    pl_new = max(0.05, min(0.90, pl_new))
    
    s = pw_new + pd_new + pl_new
    return [pw_new/s, pd_new/s, pl_new/s]


def calibrate_odds_change_signal(probs, init_odds, final_odds, league=None, home_odds=None):
    """赔率变动信号校准 — 基于初赔→终赔变动方向与赛果关系
    
    Ultra 10.1: 升级为三层校准体系:
      1. 整体方向信号 (全局基准)
      2. 联赛特定信号 (如挪超-34.3pp, 英冠-33.5pp)
      3. 赔率区间特定信号 (2.0-3.0区间最强)
    
    数据发现 (3274场 odds_change_history):
      胜赔下降: 主胜率44.3% (n=1389) → 看涨主胜
      胜赔上升: 主胜率33.0% (n=1544) → 看跌主胜
      整体信号: -11.3pp (正常市场)
    
    强信号联赛:
      挪超: -34.3pp (降赔64.9% vs 升赔30.6%)
      英冠: -33.5pp (降赔47.8% vs 升赔14.3%)
      瑞超: -20.0pp (降赔51.4% vs 升赔31.4%)
    
    参数:
      probs: [pw, pd, pl] 当前概率
      init_odds: 初赔胜赔 (或None)
      final_odds: 终赔胜赔 (或None)
      league: 联赛名 (用于联赛特定校准, 可选)
      home_odds: 当前主胜赔率 (用于赔率区间校准, 可选)
    """
    if not _CALIBRATION:
        return probs
    
    sig = _CALIBRATION.get('odds_change_signal', {})
    if not sig:
        return probs
    
    if init_odds is None or final_odds is None or init_odds <= 1 or final_odds <= 1:
        return probs
    
    change = final_odds - init_odds  # 正=上升, 负=下降
    abs_change = abs(change)
    
    # 确定变动类别
    if abs_change < 0.01:
        return probs
    
    pw, pd, pl = probs
    
    # ===== Ultra 10.1: 三层校准 =====
    # 1. 按幅度 + 方向 (全局基准, 同原有逻辑)
    mag = sig.get('magnitude', {})
    if change < 0:
        mag_key = 'drop_small' if abs_change < 0.1 else ('drop_medium' if abs_change < 0.3 else 'drop_large')
        fallback_h = 0.552 if abs_change < 0.1 else (0.477 if abs_change < 0.3 else 0.211)
    else:
        mag_key = 'rise_small' if abs_change < 0.1 else ('rise_medium' if abs_change < 0.3 else 'rise_large')
        fallback_h = 0.524 if abs_change < 0.1 else (0.308 if abs_change < 0.3 else 0.211)
    mag_entry = mag.get(mag_key)
    target_h = mag_entry['h_rate'] if mag_entry and mag_entry.get('sample', 0) >= 50 else fallback_h
    
    # 基准主胜率: 优先用标定库方向样本加权均值
    _dirs = [sig.get(k) for k in ('drop', 'rise', 'unchanged')]
    _n = sum(d['sample'] for d in _dirs if d and d.get('sample'))
    base_h_rate = (sum(d['h_rate'] * d['sample'] for d in _dirs if d and d.get('sample')) / _n) if _n >= 200 else 0.43
    
    # 2. 联赛特定信号 (Ultra 10.1)
    # 从预加载的 odds_movement_calibration.json 获取联赛特定校准因子
    _league_correction = 0.0
    if league and _ODDS_MOVEMENT_CALIB:
        lg_data = _ODDS_MOVEMENT_CALIB.get('by_league', {}).get(league, {})
        if lg_data.get('n', 0) >= 20:
            lg_signal = lg_data.get('信号强度', 0)  # 如挪超 -34.3pp
            # 信号强度/200 作为修正比例 (保守: 34.3pp → 0.17 修正, 即约一半信号)
            _league_correction = lg_signal / 200.0
    
    # 3. 赔率区间特定信号 (Ultra 10.1)
    _range_correction = 0.0
    if home_odds and home_odds > 1 and _ODDS_MOVEMENT_CALIB:
        for lo, hi, lbl in [(1.0,1.5,'1.0-1.5'),(1.5,2.0,'1.5-2.0'),
                             (2.0,3.0,'2.0-3.0'),(3.0,5.0,'3.0-5.0'),(5.0,99,'5.0+')]:
            if lo <= home_odds < hi:
                rng_data = _ODDS_MOVEMENT_CALIB.get('by_odds_range', {}).get(lbl, {})
                down_n = rng_data.get('赔率降(样本/主胜)', {}).get('n', 0)
                up_n = rng_data.get('赔率升(样本/主胜)', {}).get('n', 0)
                if down_n >= 10 and up_n >= 10:
                    rng_signal = rng_data.get('信号强度(pp)', 0)
                    # 信号强度/300 作为修正比例 (更保守: 7.4pp → 0.025)
                    _range_correction = rng_signal / 300.0
                break
    
    # 计算偏差 (目标 - 基准)
    delta = target_h - base_h_rate
    
    # 修正量: 综合全局 + 联赛 + 赔率区间
    if abs_change > 0.3:
        correction = delta * 0.10
    elif abs_change < 0.1:
        correction = delta * 0.15
    else:
        correction = delta * 0.20
    
    # 叠加联赛特定修正 (仅对变动方向一致时增强)
    if change < 0 and _league_correction < 0:
        # 赔率下降 + 联赛信号为负(降赔→主胜率高) → 增强主胜信心
        correction += abs(_league_correction) * 0.5
    elif change > 0 and _league_correction > 0:
        # 赔率上升 + 联赛信号为正(反向市场) → 减弱看空力度
        correction -= _league_correction * 0.5
    elif change < 0 and _league_correction > 0:
        # 赔率下降 + 联赛信号为正(诱盘) → 减弱看多力度
        correction -= _league_correction * 0.3
    elif change > 0 and _league_correction < 0:
        # 赔率上升 + 联赛信号为负(正常市场) → 增强看空力度
        correction += abs(_league_correction) * 0.3
    
    # 叠加赔率区间修正 (轻量)
    if change < 0 and _range_correction < 0:
        correction += abs(_range_correction) * 0.3
    elif change > 0 and _range_correction > 0:
        correction -= _range_correction * 0.3
    
    # 限制修正量
    correction = max(-0.10, min(0.10, correction))
    
    if abs(correction) < 0.005:
        return probs
    
    # 应用修正: 平局承担35%, 客胜承担65% (双向同一补偿比例)
    pw_new = pw + correction
    pd_new = pd - correction * 0.35
    pl_new = pl - correction * 0.65
    
    # 边界保护
    pw_new = max(0.05, min(0.90, pw_new))
    pd_new = max(0.05, min(0.60, pd_new))
    pl_new = max(0.05, min(0.90, pl_new))
    
    s = pw_new + pd_new + pl_new
    return [pw_new/s, pd_new/s, pl_new/s]


# 动态主场优势: 不同联赛主场优势不同
# Ultra 6.6: 优先使用历史标定值, 回退经验值
LEAGUE_HOME_ADV = {
    # Ultra 7.1: 回退值按 3099 场全量库重算 (仅数据库不可用时生效)
    '韩职': 1.05, '韩K': 1.05,
    '日职': 1.21, '日乙': 1.15,
    '英超': 1.11, '西甲': 1.20, '德甲': 1.05,
    '意甲': 1.05, '法甲': 1.06, '英冠': 1.09,
    '瑞超': 1.10, '挪超': 1.18, '芬超': 1.12,
    '美职': 1.09, '美职联': 1.09, '巴甲': 1.23,
    '中超': 1.20, '亚冠': 1.15,
}

# Ultra 6.6: 用历史标定值覆盖 (如果可用)
if _CALIBRATION:
    for _lg, _data in _CALIBRATION['leagues'].items():
        if _data['sample_size'] >= 20:
            LEAGUE_HOME_ADV[_lg] = _data['home_adv']

# Ultra 6.4: 联赛历史平局率先验 (经验值, 用于平局校准目标)
# Ultra 6.6: 优先使用历史标定值
LEAGUE_DRAW_RATE = {
    # Ultra 7.1: 回退值按 3099 场全量库重算 (仅数据库不可用时生效)
    '韩职': 0.30, '韩K': 0.30,
    '日职': 0.26, '日乙': 0.29,
    '芬超': 0.25, '瑞超': 0.24, '挪超': 0.19, '丹超': 0.26,
    '美职': 0.22, '美职联': 0.22, '巴甲': 0.30,
    '英超': 0.26, '西甲': 0.31, '德甲': 0.27,
    '意甲': 0.21, '法甲': 0.26, '英冠': 0.29,
    # Ultra 11.3 (回归分析 2026-08-05): 杯赛平局先验上调
    # 两回合制杯赛(欧冠/欧罗巴/欧协联资格赛)次回合平局倾向显著高于联赛:
    #   欧冠 7-8月资格赛平局率 20%~25%, 欧罗巴资格赛 ~22%, 均高于字典旧值0.18
    #   回归验证(260804周二): 003奥林匹亚0-0、004圣吉3-3 两场杯赛平局全漏
    #   根因: 资格赛首回合落后方次回合保守固+强队留力, 易成平局
    # 说明: LEAGUE_DRAW_RATE 会被 _CALIBRATION 的 sample_size>=20 覆盖,
    #       此处提高可作用于 sample_size<20 的杯赛/资格赛名(dict覆盖不到时回退本值)
    '欧冠': 0.22, '欧冠资格赛': 0.24, '欧冠附': 0.24,
    '欧罗巴': 0.22, '欧联': 0.22, '欧联杯': 0.22, '欧联资格赛': 0.24,
    '欧协联': 0.22, '欧协联资格赛': 0.24,
    '中超': 0.26, '亚冠': 0.25,
    'default': 0.25,
}

# Ultra 11.3 (回归分析 2026-08-05): 杯赛平局固定先验
# 两回合制杯赛(欧冠/欧罗巴/欧协联资格赛)平局倾向实测 20%~25%, 取上沿 0.28
# 需稳定高于主流联赛标定值(英超0.25/巴甲0.30), 否则加成被稀释
# 不受 _CALIBRATION(sample>=20) 覆盖影响 — 杯赛主名如'欧冠'常被覆盖为低值(0.1832)
CUP_DRAW_BASE = 0.28

# Ultra 6.6: 用历史标定值覆盖平局率
if _CALIBRATION:
    for _lg, _data in _CALIBRATION['leagues'].items():
        if _data['sample_size'] >= 20:
            LEAGUE_DRAW_RATE[_lg] = _data['d_rate']

# Ultra 6.6: 联赛特定场均进球 (替代硬编码 LEAGUE_AVG_GF=1.3)
# 语义区分 (Ultra 7.1 修复): 标定库 avg_goals 是"全场总进球"(主+客, 约2.4~3.3),
# 而贝叶斯收缩先验需要"单队场均进球"(约1.2~1.65, 与原硬编码1.3同语义) → 必须减半;
# 降级路径 total_goals_base 才是全场总进球语义 → 用 LEAGUE_AVG_GOALS_MAP。
LEAGUE_AVG_GF_MAP = {}     # 单队场均进球 — bayesian_shrinkage 先验
LEAGUE_AVG_GOALS_MAP = {}  # 全场总进球 — 降级路径总λ基数
if _CALIBRATION:
    for _lg, _data in _CALIBRATION['leagues'].items():
        if _data['sample_size'] >= 20:
            LEAGUE_AVG_GF_MAP[_lg] = round(_data['avg_goals'] / 2, 4)
            LEAGUE_AVG_GOALS_MAP[_lg] = _data['avg_goals']

# Ultra 9.3: 联赛特定负二项离散参数r (贝叶斯收缩)
# 从历史数据估计: r = μ² / (σ² - μ), 其中μ=avg_goals, σ²=进球方差
# 负二项方差 = μ + μ²/r, 当r→∞时退化为泊松(方差=均值)
# 足球典型值: r=8~15 (低分联赛r小=过离散强, 高分联赛r大=接近泊松)
#
# Ultra 9.3 贝叶斯收缩改进:
#   小样本联赛的r估计有高方差, 极端值会降低预测稳定性和精度。
#   方法: 计算全局加权平均r_global, 然后对每个联赛做:
#     r_shrunk = (n / (n + C)) * r_ml + (C / (n + C)) * r_global
#   其中C=50为收缩强度常数, 样本<50时显著向全局均值收缩。
#   效果: 保持大样本联赛的精确r, 小样本联赛获得稳定先验。
LEAGUE_R_PARAM = {}
_LEAGUE_R_N = {}  # Ultra 9.3: 各联赛r参数样本量 (用于自适应TTG权重)
_DB_R = os.path.join(
    os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__)),
    'predictions', 'historical_odds.db')
if os.path.exists(_DB_R):
    try:
        _conn_r = sqlite3.connect(_DB_R)
        _c_r = _conn_r.cursor()
        # 收集所有联赛的原始MLE估计 + 样本量
        _all_r_estimates = {}  # league -> (r_ml, n)
        # 从主表(五大联赛)获取
        _c_r.execute('''
            SELECT league_cn, AVG(1.0*home_goals+away_goals) as mu,
                   AVG(1.0*(home_goals+away_goals)*(home_goals+away_goals)) - AVG(1.0*home_goals+away_goals)*AVG(1.0*home_goals+away_goals) as var,
                   COUNT(*) as n
            FROM understat_matches
            WHERE is_result=1 AND home_goals IS NOT NULL AND away_goals IS NOT NULL
            GROUP BY league_cn
            HAVING n >= 30
        ''')
        for _row_r in _c_r.fetchall():
            _lg_r, _mu_r, _var_r, _n_r = _row_r
            if _mu_r and _var_r and _var_r > _mu_r * 0.01:
                _r_est = (_mu_r * _mu_r) / max(0.01, _var_r - _mu_r)
                _r_clamped = max(4.0, min(20.0, _r_est))
                _all_r_estimates[_lg_r] = (_r_clamped, _n_r)
        # 从proxy表获取非五大联赛的r
        _c_r.execute('''
            SELECT league_cn, AVG(1.0*home_goals+away_goals) as mu,
                   AVG(1.0*(home_goals+away_goals)*(home_goals+away_goals)) - AVG(1.0*home_goals+away_goals)*AVG(1.0*home_goals+away_goals) as var,
                   COUNT(*) as n
            FROM understat_proxy
            WHERE is_result=1 AND home_goals IS NOT NULL AND away_goals IS NOT NULL
            GROUP BY league_cn
            HAVING n >= 20
        ''')
        for _row_r in _c_r.fetchall():
            _lg_r, _mu_r, _var_r, _n_r = _row_r
            if _mu_r and _var_r and _var_r > _mu_r * 0.01 and _lg_r not in _all_r_estimates:
                _r_est = (_mu_r * _mu_r) / max(0.01, _var_r - _mu_r)
                _r_clamped = max(4.0, min(20.0, _r_est))
                _all_r_estimates[_lg_r] = (_r_clamped, _n_r)
        _conn_r.close()

        if _all_r_estimates:
            # 计算全局加权平均r (用样本量加权)
            _total_n = sum(n for _, n in _all_r_estimates.values())
            _r_global = sum(r * n for r, n in _all_r_estimates.values()) / _total_n if _total_n > 0 else 10.0
            # 贝叶斯收缩: 向全局均值收缩
            _SHRINK_C = 50.0  # 收缩强度常数
            for _lg_r, (_r_ml, _n_r) in _all_r_estimates.items():
                _shrink_weight = _n_r / (_n_r + _SHRINK_C)
                _r_shrunk = _shrink_weight * _r_ml + (1.0 - _shrink_weight) * _r_global
                LEAGUE_R_PARAM[_lg_r] = max(4.0, min(20.0, _r_shrunk))
                _LEAGUE_R_N[_lg_r] = _n_r
    except Exception:
        pass

# Ultra 9.3: 联赛特定半场进球比例 (从 historical_matches 数据驱动)
# 替代固定 0.45/0.55 分拆, 用于 compute_half_full 半全场预测
# 足球半场进球比例因联赛风格而异: 挪超(0.457) > 荷甲(0.484) > 英超(0.435)
# 联赛特定比例可提升半全场联合分布精度, 特别影响含平组合(胜平/平胜/平平)
LEAGUE_HT_RATIO = {}
_DB_HT = os.path.join(
    os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__)),
    'predictions', 'historical_odds.db')
if os.path.exists(_DB_HT):
    try:
        _conn_ht = sqlite3.connect(_DB_HT)
        _c_ht = _conn_ht.cursor()
        # 从 historical_matches 表计算每联赛半场进球比例
        # 半场比例 = 半场总进球 / 全场总进球
        _c_ht.execute('''
            SELECT league, COUNT(*) as n,
                   AVG(1.0*half_home_score+half_away_score) / NULLIF(AVG(1.0*home_score+away_score), 0) as ht_ratio
            FROM historical_matches
            WHERE half_home_score IS NOT NULL AND home_score IS NOT NULL
              AND (home_score+away_score) > 0
              AND league IS NOT NULL AND league != ''
            GROUP BY league
            HAVING n >= 30
        ''')
        for _row_ht in _c_ht.fetchall():
            _lg_ht, _n_ht, _ratio_ht = _row_ht
            if _ratio_ht and 0.2 < _ratio_ht < 0.6:
                LEAGUE_HT_RATIO[_lg_ht] = round(_ratio_ht, 3)
        _conn_ht.close()
    except Exception:
        pass


# ============================================================
# Ultra 6.7: 高级标定 (6大模块)
# 1. 半场→全场转移概率矩阵 (静态, per league)
# 2. 赛程密度/休息天数效应 (动态, 查询DB)
# 3. 球队×赔率区间偏差 (静态, per team)
# 4. 跨市场一致性信号 (静态阈值, 动态判断)
# 5. 近期状态序列效应 (动态, 查询DB)
# 6. H2H历史交锋模式 (动态, 查询DB)
# 数据源: predictions/advanced_calibration.json + historical_odds.db
# 自动回退: 无数据时跳过, 不影响原有逻辑
# ============================================================

_ADV_CALIB_PATH = os.path.join(
    os.environ.get('SPORTTERY_WORKSPACE', os.path.dirname(os.path.abspath(__file__))),
    'predictions', 'advanced_calibration.json'
)

def _load_advanced_calibration():
    """加载高级标定参数 (6大模块)"""
    if not os.path.exists(_ADV_CALIB_PATH):
        return None
    try:
        with open(_ADV_CALIB_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        n_modules = sum(1 for k in ['ht_ft_transition','rest_days','team_odds_bias',
                                     'cross_market','form_effect','h2h'] if data.get(k))
        print(f'  [高级标定] 加载{n_modules}大模块 ({data.get("total_matches",0)}场数据)')
        return data
    except Exception as e:
        print(f'  [高级标定] 加载失败, 跳过: {e}')
        return None

_ADV_CALIB = _load_advanced_calibration()

# Ultra 10.1: 赔率变动方向与幅度预测价值量化校准因子
# 数据源: predictions/odds_movement_calibration.json (3274场, 31个联赛, 5个赔率区间, 4个幅度分层)
_ODDS_MOVEMENT_CALIB_PATH = os.path.join(
    os.environ.get('SPORTTERY_WORKSPACE', os.path.dirname(os.path.abspath(__file__))),
    'predictions', 'odds_movement_calibration.json'
)

def _load_odds_movement_calibration():
    """加载赔率变动校准因子 (Ultra 10.1)
    
    包含:
    - 整体方向信号 (全局 -11.3pp)
    - 按联赛分层 (30个联赛, 含挪超-34.3pp等强信号)
    - 按赔率区间分层 (5个区间, 2.0-3.0信号最强-7.4pp)
    - 按变动幅度分层 (4个幅度)
    """
    if not os.path.exists(_ODDS_MOVEMENT_CALIB_PATH):
        print('  [赔率变动] 校准文件不存在, 跳过')
        return None
    try:
        with open(_ODDS_MOVEMENT_CALIB_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        n_leagues = len(data.get('by_league', {}))
        n_matches = data.get('total_matches_analyzed', 0)
        print(f'  [赔率变动] 加载校准因子 ({n_matches}场, {n_leagues}个联赛)')
        return data
    except Exception as e:
        print(f'  [赔率变动] 加载失败, 跳过: {e}')
        return None

_ODDS_MOVEMENT_CALIB = _load_odds_movement_calibration()

# Ultra 10.3: 模型校准偏差修正因子 (3290场数据分析)
# 数据源: predictions/model_calibration.json
_MODEL_CALIB_PATH = os.path.join(
    os.environ.get('SPORTTERY_WORKSPACE', os.path.dirname(os.path.abspath(__file__))),
    'predictions', 'model_calibration.json'
)

def _load_model_calibration():
    """加载模型校准偏差修正因子 (Ultra 10.3)
    
    包含:
    - 概率校准曲线 (6个置信度区间偏差)
    - 方向校准 (主/平/客偏差)
    - 联赛命中率对比 (vs整体)
    """
    if not os.path.exists(_MODEL_CALIB_PATH):
        print('  [模型校准] 校准文件不存在, 跳过')
        return None
    try:
        with open(_MODEL_CALIB_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f'  [模型校准] 加载偏差修正因子 (基于{data.get("overall",{}).get("model_hit_rate","?")}%命中率)')
        return data
    except Exception as e:
        print(f'  [模型校准] 加载失败, 跳过: {e}')
        return None

_MODEL_CALIB = _load_model_calibration()

# Ultra 10.3: 体彩各玩法赔率偏差校准因子
# 数据源: predictions/sporttery_pools_calibration.json
_POOLS_CALIB_PATH = os.path.join(
    os.environ.get('SPORTTERY_WORKSPACE', os.path.dirname(os.path.abspath(__file__))),
    'predictions', 'sporttery_pools_calibration.json'
)

def _load_pools_calibration():
    """加载体彩池赔率偏差校准因子 (Ultra 10.3)
    
    包含 TTG/HAFU/CRS 三个玩法的偏差分析
    """
    if not os.path.exists(_POOLS_CALIB_PATH):
        print('  [体彩池] 校准文件不存在, 跳过')
        return None
    try:
        with open(_POOLS_CALIB_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f'  [体彩池] 加载赔率偏差校准 (TTG/HAFU/CRS)')
        return data
    except Exception as e:
        print(f'  [体彩池] 加载失败, 跳过: {e}')
        return None

_POOLS_CALIB = _load_pools_calibration()

# Ultra 10.4: 大小球盘口准确性校准因子 (4412场TTG→大小球转换分析)
# 数据源: predictions/over_under_analysis.json
_OVER_UNDER_CALIB_PATH = os.path.join(
    os.environ.get('SPORTTERY_WORKSPACE', os.path.dirname(os.path.abspath(__file__))),
    'predictions', 'over_under_analysis.json'
)

def _load_over_under_calibration():
    """加载大小球盘口准确性校准因子 (Ultra 10.4)
    
    关键发现: 整体命中率58.1%, 大球偏差-0.6pp (轻微低估)
    """
    if not os.path.exists(_OVER_UNDER_CALIB_PATH):
        print('  [大小球校准] 校准文件不存在, 跳过')
        return None
    try:
        with open(_OVER_UNDER_CALIB_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f'  [大小球校准] 加载 (命中率{data["overall"]["accuracy"]:.1%}, {data["overall"]["total_matches"]}场)')
        return data
    except Exception as e:
        print(f'  [大小球校准] 加载失败, 跳过: {e}')
        return None

_OVER_UNDER_CALIB = _load_over_under_calibration()

# Ultra 10.4: HHAD穿盘/输盘规律校准因子 (4412场让球盘分析)
# 数据源: predictions/hhad_yazhi_analysis.json
_HHAD_CALIB_PATH = os.path.join(
    os.environ.get('SPORTTERY_WORKSPACE', os.path.dirname(os.path.abspath(__file__))),
    'predictions', 'hhad_yazhi_analysis.json'
)

def _load_hhad_calibration():
    """加载HHAD穿盘/输盘规律校准因子 (Ultra 10.4)
    
    关键发现: 穿盘33.3%, 走水13.3%, 输盘53.4%
    Skellam分布系统性高估走水概率(偏差-9.2pp)
    """
    if not os.path.exists(_HHAD_CALIB_PATH):
        print('  [HHAD校准] 校准文件不存在, 跳过')
        return None
    try:
        with open(_HHAD_CALIB_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f'  [HHAD校准] 加载 (穿盘{data["hhad_analysis"]["overall_hhad"]["win"]["pct"]:.1%}, {data["metadata"]["total_sample"]}场)')
        return data
    except Exception as e:
        print(f'  [HHAD校准] 加载失败, 跳过: {e}')
        return None

_HHAD_CALIB = _load_hhad_calibration()

# Ultra 10.4: 联赛模式校准因子 (4449场, 42个联赛)
# 数据源: predictions/league_patterns_analysis.json
_LEAGUE_PATTERNS_CALIB_PATH = os.path.join(
    os.environ.get('SPORTTERY_WORKSPACE', os.path.dirname(os.path.abspath(__file__))),
    'predictions', 'league_patterns_analysis.json'
)

def _load_league_patterns_calibration():
    """加载联赛模式校准因子 (Ultra 10.4)
    
    包含: 进球分布/主场优势/比分分布
    """
    if not os.path.exists(_LEAGUE_PATTERNS_CALIB_PATH):
        print('  [联赛模式] 校准文件不存在, 跳过')
        return None
    try:
        with open(_LEAGUE_PATTERNS_CALIB_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        n_leagues = data.get('analysis_meta', {}).get('total_leagues', 0)
        print(f'  [联赛模式] 加载 ({n_leagues}个联赛, {data["analysis_meta"]["total_matches"]}场)')
        return data
    except Exception as e:
        print(f'  [联赛模式] 加载失败, 跳过: {e}')
        return None

_LEAGUE_PATTERNS_CALIB = _load_league_patterns_calibration()

# Ultra 10.6: 体彩赔率变动特征 + 玩法矛盾信号校准因子 (3274场)
# 数据源: predictions/odds_change_analysis_calibration.json
_ODDS_CHANGE_ANALYSIS_CALIB_PATH = os.path.join(
    os.environ.get('SPORTTERY_WORKSPACE', os.path.dirname(os.path.abspath(__file__))),
    'predictions', 'odds_change_analysis_calibration.json'
)

def _load_odds_change_analysis_calibration():
    """加载赔率变动特征分析校准因子 (Ultra 10.6 → 11.0)
    
    包含:
    - draw_change: 平局赔率变动信号 (平赔下降→主胜44.1%)
    - conflict_signal: HAD/HHAD/亚盘矛盾信号 (三方向一致55.9%)
    - had_hhad_linkage: HAD-HHAD联动信号 (同时上升→主胜45.9%)
    - had_change_detail: HAD赔率变动幅度分层信号
    - odds_movement: 体彩初赔→终赔变动方向校准 (Ultra 11.0)
    """
    if not os.path.exists(_ODDS_CHANGE_ANALYSIS_CALIB_PATH):
        print('  [赔率变动分析] 校准文件不存在, 跳过')
        return None
    try:
        with open(_ODDS_CHANGE_ANALYSIS_CALIB_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f'  [赔率变动分析] 加载 ({data["total_had_sample"]}场HAD, {data["total_conflict_sample"]}场矛盾信号)')
        return data
    except Exception as e:
        print(f'  [赔率变动分析] 加载失败, 跳过: {e}')
        return None

_ODDS_CHANGE_ANALYSIS_CALIB = _load_odds_change_analysis_calibration()

# --- 历史数据库连接 (单例, 懒加载, 进程级缓存) ---
# Ultra 10.0: 统一连接管理, 所有DB查询均通过此函数获取连接
_ADV_DB_CONN = None

def _get_adv_db():
    """获取历史数据库单例连接 (Ultra 10.0: 统一单例)
    
    所有历史数据库查询统一通过此函数获取连接, 避免重复 connect/close。
    使用 WAL 模式 + 默认 Row 工厂, 性能与一致性兼顾。
    
    注意: DB 文件暂时不存在时返回 None, 但不缓存 None, 下次调用会重试;
    若连接建立后文件被删除/损坏, 关闭连接并清空缓存以便重建。
    """
    global _ADV_DB_CONN
    if _ADV_DB_CONN is not None:
        try:
            _ADV_DB_CONN.execute("SELECT 1")
            return _ADV_DB_CONN
        except Exception:
            # 连接已失效(文件被删/损坏), 关闭并重建
            try:
                _ADV_DB_CONN.close()
            except Exception:
                pass
            _ADV_DB_CONN = None
    if not os.path.exists(_CALIBRATION_DB):
        return None
    try:
        _ADV_DB_CONN = sqlite3.connect(_CALIBRATION_DB)
        _ADV_DB_CONN.execute("PRAGMA journal_mode=WAL")
        _ADV_DB_CONN.row_factory = sqlite3.Row
        return _ADV_DB_CONN
    except Exception:
        return None


def _query_one(sql, params=None):
    """单行查询快捷函数 (Ultra 10.0)
    
    返回单行 dict 或 None。
    """
    conn = _get_adv_db()
    if not conn:
        return None
    try:
        c = conn.cursor()
        if params:
            c.execute(sql, params)
        else:
            c.execute(sql)
        row = c.fetchone()
        if row:
            return dict(row)
        return None
    except Exception:
        return None


def _query_all(sql, params=None):
    """多行查询快捷函数 (Ultra 10.0)
    
    返回 list[dict]。
    """
    conn = _get_adv_db()
    if not conn:
        return []
    try:
        c = conn.cursor()
        if params:
            c.execute(sql, params)
        else:
            c.execute(sql)
        return [dict(row) for row in c.fetchall()]
    except Exception:
        return []


def _compute_rest_days(home_team, away_team, match_date_str):
    """计算主客队休息天数 (距上一场比赛的天数)
    返回: (home_rest_days, away_rest_days) 或 (None, None)
    """
    conn = _get_adv_db()
    if not conn or not match_date_str or not home_team or not away_team:
        return None, None
    try:
        c = conn.cursor()
        md = match_date_str[:10]
        d_match = datetime.strptime(md, '%Y-%m-%d')

        c.execute('''SELECT MAX(match_date) FROM historical_matches
                     WHERE (home_team=? OR away_team=?) AND match_date<? AND home_score IS NOT NULL''',
                  (home_team, home_team, md))
        row = c.fetchone()
        home_rest = (d_match - datetime.strptime(row[0][:10], '%Y-%m-%d')).days if row and row[0] else None

        c.execute('''SELECT MAX(match_date) FROM historical_matches
                     WHERE (home_team=? OR away_team=?) AND match_date<? AND home_score IS NOT NULL''',
                  (away_team, away_team, md))
        row = c.fetchone()
        away_rest = (d_match - datetime.strptime(row[0][:10], '%Y-%m-%d')).days if row and row[0] else None

        return home_rest, away_rest
    except Exception:
        return None, None


def _compute_form_score(team, match_date_str, n=5):
    """计算球队近期状态 (最近n场场均积分, 0-3)
    返回: float 或 None
    """
    conn = _get_adv_db()
    if not conn or not match_date_str or not team:
        return None
    try:
        c = conn.cursor()
        md = match_date_str[:10]
        c.execute('''SELECT home_team, away_team, result FROM historical_matches
                     WHERE (home_team=? OR away_team=?) AND match_date<?
                     AND result IN ('H','D','A')
                     ORDER BY match_date DESC LIMIT ?''',
                  (team, team, md, n))
        rows = c.fetchall()
        if not rows:
            return None
        points = 0
        for home, away, result in rows:
            if home == team:
                points += 3 if result == 'H' else (1 if result == 'D' else 0)
            else:
                points += 3 if result == 'A' else (1 if result == 'D' else 0)
        return points / len(rows)
    except Exception:
        return None


def _compute_h2h(home_team, away_team):
    """计算H2H历史交锋统计
    返回: {'home_wins', 'away_wins', 'draws', 'total', 'avg_goals'} 或 None
    """
    conn = _get_adv_db()
    if not conn or not home_team or not away_team:
        return None
    try:
        c = conn.cursor()
        c.execute('''SELECT home_team, away_team, home_score, away_score, result
                     FROM historical_matches
                     WHERE ((home_team=? AND away_team=?) OR (home_team=? AND away_team=?))
                     AND result IN ('H','D','A')
                     ORDER BY match_date DESC LIMIT 10''',
                  (home_team, away_team, away_team, home_team))
        rows = c.fetchall()
        if not rows:
            return None
        home_wins = away_wins = draws = 0
        total_goals = 0
        for h, a, hs, as_, r in rows:
            total_goals += hs + as_
            if r == 'D':
                draws += 1
            elif (h == home_team and r == 'H') or (a == home_team and r == 'A'):
                home_wins += 1
            else:
                away_wins += 1
        return {
            'home_wins': home_wins, 'away_wins': away_wins, 'draws': draws,
            'total': len(rows), 'avg_goals': round(total_goals / len(rows), 2),
        }
    except Exception:
        return None


# ============================================================
# Ultra 8.0: xG/xGA 数据集成 — 从 Understat 获取预期进球数据
# 替代实际进球, 降低运气噪声; 双源交叉验证作为特征质量指标
# ============================================================

# 中文→英文球队名映射 (五大联赛)
# 唯一数据源为 src/config.TEAM_NAME_MAP (与 run_pipeline 采集器共用),
# 下方 _XG_TEAM_MAP_LOCAL 仅作导入失败时的兜底及本地覆盖
_XG_TEAM_MAP_LOCAL = {
    # 英超
    "阿森纳": "Arsenal", "维拉": "Aston Villa", "伯恩茅斯": "Bournemouth",
    "布伦特": "Brentford", "布赖顿": "Brighton", "切尔西": "Chelsea",
    "水晶宫": "Crystal Palace", "埃弗顿": "Everton", "富勒姆": "Fulham",
    "伊普斯": "Ipswich", "莱切斯特": "Leicester", "利物浦": "Liverpool",
    "曼城": "Manchester City", "曼联": "Manchester United",
    "纽卡斯尔": "Newcastle United", "诺丁汉": "Nottingham Forest",
    "南安普敦": "Southampton", "热刺": "Tottenham", "西汉姆联": "West Ham",
    "狼队": "Wolverhampton Wanderers", "伯恩利": "Burnley", "利兹联": "Leeds United",
    # 西甲
    "阿拉维斯": "Alaves", "毕尔巴鄂": "Athletic Club", "马竞": "Atletico Madrid",
    "巴萨": "Barcelona", "塞尔塔": "Celta Vigo", "西班牙人": "Espanyol",
    "赫塔费": "Getafe", "赫罗纳": "Girona", "拉帕马斯": "Las Palmas",
    "莱加内斯": "Leganes", "马洛卡": "Mallorca", "奥萨苏纳": "Osasuna",
    "巴列卡诺": "Rayo Vallecano", "贝蒂斯": "Real Betis", "皇马": "Real Madrid",
    "皇家社会": "Real Sociedad", "巴利亚多": "Real Valladolid",
    "塞维利亚": "Sevilla", "巴伦西亚": "Valencia", "比利亚雷": "Villarreal",
    # 德甲
    "奥格斯堡": "Augsburg", "勒沃库森": "Bayer Leverkusen", "拜仁": "Bayern Munich",
    "波鸿": "Bochum", "多特蒙德": "Borussia Dortmund", "门兴": "Borussia M.Gladbach",
    "法兰克福": "Eintracht Frankfurt", "海登海姆": "FC Heidenheim",
    "弗赖堡": "Freiburg", "霍芬海姆": "Hoffenheim", "基尔": "Holstein Kiel",
    "美因茨": "Mainz 05", "莱红牛": "RasenBallsport Leipzig", "圣保利": "St. Pauli",
    "柏林联合": "Union Berlin", "斯图加特": "VfB Stuttgart",
    "不来梅": "Werder Bremen", "沃夫斯堡": "Wolfsburg", "科隆": "FC Koln",
    # 意甲
    "AC米兰": "AC Milan", "亚特兰大": "Atalanta", "博洛尼亚": "Bologna",
    "卡利亚里": "Cagliari", "科莫": "Como", "恩波利": "Empoli",
    "佛罗伦萨": "Fiorentina", "热那亚": "Genoa", "国际米兰": "Inter",
    "尤文图斯": "Juventus", "拉齐奥": "Lazio", "莱切": "Lecce",
    "蒙扎": "Monza", "那不勒斯": "Napoli", "帕尔马": "Parma Calcio 1913",
    "罗马": "Roma", "都灵": "Torino", "乌迪内斯": "Udinese",
    "威尼斯": "Venezia", "维罗纳": "Verona",
    # 法甲
    "昂热": "Angers", "欧塞尔": "Auxerre", "布雷斯特": "Brest",
    "勒阿弗尔": "Le Havre", "朗斯": "Lens", "里尔": "Lille",
    "里昂": "Lyon", "马赛": "Marseille", "摩纳哥": "Monaco",
    "蒙彼利埃": "Montpellier", "南特": "Nantes", "尼斯": "Nice",
    "巴黎圣曼": "Paris Saint Germain", "兰斯": "Reims", "雷恩": "Rennes",
    "圣埃蒂安": "Saint-Etienne", "斯特拉斯": "Strasbourg", "图卢兹": "Toulouse",
}

# 合并: src/config 为唯一源, 本地表覆盖差异项 (如 利兹联→Leeds United)
try:
    from src.config import TEAM_NAME_MAP as _CFG_TEAM_MAP
    _XG_TEAM_MAP = {**_CFG_TEAM_MAP, **_XG_TEAM_MAP_LOCAL}
except Exception:
    _XG_TEAM_MAP = _XG_TEAM_MAP_LOCAL

# 支持xG数据的联赛集合 (含大五联赛 + 数据库中有xG数据的联赛)
_BIG5_LEAGUES = {"英超", "西甲", "德甲", "意甲", "法甲"}
_XG_SUPPORTED_LEAGUES = _BIG5_LEAGUES | {
    "瑞超", "挪超", "美职", "美职联", "芬超", "日职", "韩职",
    "葡超", "澳超", "荷甲", "英冠", "欧冠", "欧罗巴",
}

# Ultra 9.2: 非五大联赛xG数据在 understat_proxy 表(含3315场真实xG)
# 因此 _XG_REAL_LEAGUES 只保留五大联赛(understat_matches表)
# 非五大联赛自动路由到 understat_proxy
_XG_REAL_LEAGUES = _BIG5_LEAGUES.copy()


def fetch_xg_rolling_stats(team_cn, match_date, league_cn='', window=10):
    """从 Understat 数据库获取球队滚动 xG/xGA/PPDA 统计

    优化1: 用 xG/xGA 替代实际进球, 降低运气噪声
    优化2: 计算 xG vs 实际进球的交叉验证差异作为特征质量指标
    优化3: 提取PPDA压迫强度特征 (防守压迫指标)

    PPDA (Passes Per Defensive Action) 说明:
        - 衡量球队无球时的压迫强度, 值越低越激进
        - 低PPDA (<8): 高位逼抢, 在对方半场夺回球权, 创造更多机会
        - 高PPDA (>14): 被动防守, 允许对手更多传球, 防守深度退守

    Args:
        team_cn: 中文球队名
        match_date: 比赛日期 (YYYY-MM-DD)
        league_cn: 中文联赛名
        window: 滚动窗口 (默认10场)

    Returns:
        dict with:
            avg_xg_for:        场均预期进球
            avg_xg_against:    场均预期失球
            avg_ppda:          场均PPDA (自身压迫强度, 越低越激进)
            avg_opp_ppda:      对手场均PPDA (对方压迫强度, 越低越被压制)
            pressure_index:    压迫强度指数 0-1 (1=最激进, 基于PPDA归一化)
            ppda_diff_vs_opp:  PPDA优势 (= 对手PPDA - 自身PPDA, 正=自身压迫更强)
            ppda_stability:    PPDA稳定性 (0-1, 1=非常稳定, 基于标准差)
            overperformance:   xG超额 (实际-xG, 正=运气好)
            cv_quality:        交叉验证质量 0-1 (xG与实际进球一致性)
            n_games:           样本量
            has_xg:            是否有xG数据
        或 None (无数据)
    """
    if league_cn and league_cn not in _XG_SUPPORTED_LEAGUES:
        return None

    # 队名解析: 优先用映射表转英文, 映射不到则直接用中文
    en_team = _XG_TEAM_MAP.get(team_cn)
    team_names = [en_team] if en_team else []
    if team_cn not in team_names:
        team_names.append(team_cn)
    for v in team_name_variants(team_cn):
        if v not in team_names:
            team_names.append(v)

    # Ultra 10.0: 使用统一单例连接 + 统一 team_xg 表
    conn = _get_adv_db()
    if not conn:
        return None
    c = conn.cursor()

    try:
        # Ultra 10.0: 统一 team_xg 表, 不再路由 understat_matches / understat_proxy
        placeholders = ','.join(['?'] * len(team_names))
        c.execute(f'''
            SELECT home_team, away_team, home_xg, away_xg, home_goals, away_goals,
                   home_ppda, away_ppda, match_date
            FROM team_xg
            WHERE match_date < ? AND is_result = 1
              AND (home_team IN ({placeholders}) OR away_team IN ({placeholders}))
              AND home_xg IS NOT NULL AND away_xg IS NOT NULL
              AND home_goals IS NOT NULL AND away_goals IS NOT NULL
            ORDER BY match_date DESC
            LIMIT ?
        ''', (match_date, *team_names, *team_names, window))

        rows = c.fetchall()

        if not rows:
            return None

        xg_for_list = []
        xg_against_list = []
        ppda_list = []        # 自身PPDA (压迫强度)
        opp_ppda_list = []    # 对手PPDA (被压迫程度)
        actual_for_list = []
        cv_errors = []

        for row in rows:
            home_team, away_team, home_xg, away_xg, home_goals, away_goals, home_ppda, away_ppda, _ = row
            if home_team in team_names:
                xg_for_list.append(home_xg)
                xg_against_list.append(away_xg)
                ppda_list.append(home_ppda)      # 主队PPDA = 自身压迫强度
                opp_ppda_list.append(away_ppda)   # 客队PPDA = 对手压迫强度
                actual_for_list.append(home_goals)
                cv_errors.append(abs(home_goals - home_xg))
            else:
                xg_for_list.append(away_xg)
                xg_against_list.append(home_xg)
                ppda_list.append(away_ppda)      # 客队PPDA = 自身压迫强度
                opp_ppda_list.append(home_ppda)   # 主队PPDA = 对手压迫强度
                actual_for_list.append(away_goals)
                cv_errors.append(abs(away_goals - away_xg))

        n = len(xg_for_list)
        avg_xg_for = sum(xg_for_list) / n
        avg_xg_against = sum(xg_against_list) / n

        # PPDA: 自身压迫强度 (越低越激进)
        valid_ppda = [p for p in ppda_list if p is not None]
        avg_ppda = sum(valid_ppda) / len(valid_ppda) if valid_ppda else None

        # 对手PPDA: 对方压迫强度 (越低说明自身被压制越多)
        valid_opp_ppda = [p for p in opp_ppda_list if p is not None]
        avg_opp_ppda = sum(valid_opp_ppda) / len(valid_opp_ppda) if valid_opp_ppda else None

        # PPDA稳定性: 基于标准差的归一化 (变异系数CV的逆)
        # CV = std/mean, stability = 1/(1+CV), 越高越稳定
        if len(valid_ppda) >= 3:
            ppda_mean = sum(valid_ppda) / len(valid_ppda)
            ppda_var = sum((p - ppda_mean) ** 2 for p in valid_ppda) / len(valid_ppda)
            ppda_std = ppda_var ** 0.5
            cv = ppda_std / ppda_mean if ppda_mean > 0 else 1.0
            ppda_stability = 1.0 / (1.0 + cv)
        else:
            ppda_stability = 0.5  # 样本不足, 中等稳定性

        avg_actual = sum(actual_for_list) / n

        # 交叉验证质量: xG与实际进球的MAE → quality = 1/(1+MAE)
        mae = sum(cv_errors) / n
        cv_quality = 1.0 / (1.0 + mae)

        # xG超额表现: 实际进球 - xG (正=运气好/超额表现)
        overperformance = avg_actual - avg_xg_for

        # 压迫强度指数 (0-1, 1=最激进)
        # PPDA归一化: 基于五大联赛经验分布 P10=6.3, P50=11.2, P90=20.1
        # 使用sigmoid映射: pressure = 1 / (1 + exp((ppda - 11) / 3))
        if avg_ppda is not None:
            pressure_index = 1.0 / (1.0 + pow(2.71828, (avg_ppda - 11.0) / 3.0))
            pressure_index = round(pressure_index, 3)
        else:
            pressure_index = None

        # PPDA优势: 对手PPDA - 自身PPDA (正值=自身压迫更强)
        if avg_ppda is not None and avg_opp_ppda is not None:
            ppda_diff_vs_opp = round(avg_opp_ppda - avg_ppda, 2)
        else:
            ppda_diff_vs_opp = None

        return {
            'avg_xg_for': round(avg_xg_for, 2),
            'avg_xg_against': round(avg_xg_against, 2),
            'avg_ppda': round(avg_ppda, 2) if avg_ppda else None,
            'avg_opp_ppda': round(avg_opp_ppda, 2) if avg_opp_ppda else None,
            'pressure_index': pressure_index,
            'ppda_diff_vs_opp': ppda_diff_vs_opp,
            'ppda_stability': round(ppda_stability, 3),
            'overperformance': round(overperformance, 2),
            'cv_quality': round(cv_quality, 3),
            'n_games': n,
            'has_xg': True,
            # Ultra 9.2: proxy表(含非五大联赛3315场)已全部采集真实xG数据
            # is_proxy=False: 不再降权/封顶, 数据质量由cv_quality字段反映
            'is_proxy': False,
        }
    except Exception:
        return None


_ODDS_BINS = [(1.0,1.5,'1.0-1.5'),(1.5,2.0,'1.5-2.0'),
              (2.0,2.5,'2.0-2.5'),(2.5,3.5,'2.5-3.5'),(3.5,99,'3.5+')]

def _odds_bin(odds):
    """获取赔率区间标签"""
    for lo, hi, label in _ODDS_BINS:
        if lo <= odds < hi:
            return label
    return None


def apply_advanced_calibration(probs, sp, had, hhad):
    """应用6大高级标定 (在ensemble fusion之后调用)

    对融合概率做有界修正, 每个子模块独立修正, 总修正量有界。
    返回: (calibrated_probs, notes_list)
    """
    if not _ADV_CALIB:
        return probs, []

    league = sp.get('league', '')
    home_team = sp.get('home', '')
    away_team = sp.get('away', '')
    match_date = sp.get('date', '') or sp.get('matchDate', '')

    pw, pd, pl = probs
    notes = []

    # --- 1. 半场→全场转移: 平局粘性修正 ---
    ht_ft = _ADV_CALIB.get('ht_ft_transition', {}).get(league)
    if ht_ft:
        ht_d = ht_ft.get('D', {})
        if ht_d.get('sample', 0) >= 10:
            # HT=D → FT=D 的概率反映联赛"平局粘性"
            draw_stickiness = ht_d.get('D', 0.35)
            # 与全局均值比较, 15%权重修正
            draw_adj = (draw_stickiness - 0.35) * 0.15
            draw_adj = max(-0.04, min(0.04, draw_adj))
            if abs(draw_adj) > 0.008:
                pd += draw_adj
                pw -= draw_adj * 0.5
                pl -= draw_adj * 0.5
                notes.append(f'半全场转移: 平局粘性{draw_stickiness:.0%}→平{"↑" if draw_adj>0 else "↓"}{abs(draw_adj)*100:.1f}pp')

    # --- 2. 休息天数效应 ---
    home_rest, away_rest = _compute_rest_days(home_team, away_team, match_date)
    if home_rest is not None and away_rest is not None:
        rest_diff_data = _ADV_CALIB.get('rest_days', {}).get('rest_diff', {}).get(league)
        if rest_diff_data:
            diff = home_rest - away_rest
            bucket = 'home_less' if diff <= -3 else ('home_more' if diff >= 3 else 'equal')
            bd = rest_diff_data.get(bucket)
            if bd and bd.get('sample', 0) >= 10:
                lg_h_rate = get_league_param(league, 'h_rate', 0.45)
                h_adj = (bd['h_rate'] - lg_h_rate) * 0.20
                h_adj = max(-0.05, min(0.05, h_adj))
                if abs(h_adj) > 0.01:
                    pw += h_adj
                    pl -= h_adj * 0.7
                    pd -= h_adj * 0.3
                    notes.append(f'休息天数: 主{home_rest}d/客{away_rest}d→主{"↑" if h_adj>0 else "↓"}{abs(h_adj)*100:.1f}pp')

    # --- 3. 球队×赔率区间偏差 ---
    team_bias = _ADV_CALIB.get('team_odds_bias', {})
    if had and 'h' in had:
        home_odds = had.get('h', 0)
        away_odds = had.get('a', 0)

        # 主队偏差
        if home_odds > 1:
            h_label = _odds_bin(home_odds)
            h_bias_info = team_bias.get('home', {}).get(home_team, {}).get(h_label) if h_label else None
            if h_bias_info and h_bias_info.get('sample', 0) >= 5:
                bias = h_bias_info['bias']
                pw += bias * 0.30
                pl -= bias * 0.70
                pd -= bias * 0.30
                if abs(bias) > 0.05:
                    notes.append(f'球队偏差: {home_team}(主){h_label} {bias:+.0%}')

        # 客队偏差
        if away_odds > 1:
            a_label = _odds_bin(away_odds)
            a_bias_info = team_bias.get('away', {}).get(away_team, {}).get(a_label) if a_label else None
            if a_bias_info and a_bias_info.get('sample', 0) >= 5:
                bias = a_bias_info['bias']
                pl += bias * 0.30
                pw -= bias * 0.70
                pd -= bias * 0.30
                if abs(bias) > 0.05:
                    notes.append(f'球队偏差: {away_team}(客){a_label} {bias:+.0%}')

    # --- 4. 跨市场一致性信号 ---
    cross_market_cal = _ADV_CALIB.get('cross_market', {})
    if had and 'h' in had and hhad and 'goalLine' in (hhad or {}):
        goal_line = hhad.get('goalLine', 0)
        home_odds = had.get('h', 0)
        if home_odds > 1 and goal_line:
            try:
                gl_val = int(goal_line)
                raw = [1/had['h'], 1/had['d'], 1/had['a']]
                s = sum(raw)
                implied_h = raw[0] / s
                home_fav_had = implied_h > 0.45
                home_fav_gl = gl_val < 0

                if home_fav_had != home_fav_gl:
                    disagree = cross_market_cal.get('disagree', {}).get(league)
                    agree = cross_market_cal.get('agree', {}).get(league)
                    if disagree and agree and disagree.get('sample', 0) >= 10:
                        h_shift = disagree['h_rate'] - agree['h_rate']
                        a_shift = disagree['a_rate'] - agree['a_rate']
                        pw += h_shift * 0.40
                        pl += a_shift * 0.40
                        pd -= (h_shift + a_shift) * 0.40
                        if abs(h_shift) > 0.03:
                            notes.append(f'跨市场矛盾: HAD与盘口不一致→主{h_shift*100:+.0f}pp')
            except (ValueError, TypeError, KeyError):
                pass

    # --- 5. 近期状态序列 ---
    home_form = _compute_form_score(home_team, match_date, n=5)
    away_form = _compute_form_score(away_team, match_date, n=5)
    if home_form is not None and away_form is not None:
        form_diff = home_form - away_form
        form_data = _ADV_CALIB.get('form_effect', {}).get('form_diff', {}).get(league)
        if form_data:
            bucket = 'home_worse' if form_diff <= -1 else ('home_better' if form_diff >= 1 else 'similar')
            bd = form_data.get(bucket)
            similar = form_data.get('similar')
            if bd and similar and bd.get('sample', 0) >= 10:
                h_shift = bd['h_rate'] - similar['h_rate']
                pw += h_shift * 0.25
                pl -= h_shift * 0.65
                pd -= h_shift * 0.35
                if abs(h_shift) > 0.05:
                    notes.append(f'状态差异: 主{home_form:.1f}/客{away_form:.1f}→主{h_shift*100:+.0f}pp')

    # --- 6. H2H历史交锋 ---
    h2h = _compute_h2h(home_team, away_team)
    if h2h and h2h['total'] >= 2:
        total = h2h['total']
        h_rate = h2h['home_wins'] / total
        d_rate = h2h['draws'] / total
        a_rate = h2h['away_wins'] / total
        # H2H先验权重: 2场=6%, 5场=15%, 上限15%
        prior_w = min(0.15, total * 0.03)
        pw = pw * (1 - prior_w) + h_rate * prior_w
        pd = pd * (1 - prior_w) + d_rate * prior_w
        pl = pl * (1 - prior_w) + a_rate * prior_w
        if total >= 3:
            notes.append(f'H2H: {h2h["home_wins"]}-{h2h["draws"]}-{h2h["away_wins"]}(n={total})')

    # --- 7. 模型校准偏差修正 (Ultra 10.3) ---
    # 基于3290场历史数据的闭环验证:
    #   模型在所有置信度区间系统性高估概率 (偏差-4.5~-6.8pp)
    #   修正方式: 按置信度区间将预测概率向实际校准概率调整
    if _MODEL_CALIB:
        mc = _MODEL_CALIB.get('probability_correction', {})
        if mc:
            conf = max(pw, pd, pl)
            # 确定置信度区间
            bins = [(0.0, 0.25, '0-25%'), (0.25, 0.35, '25-35%'), (0.35, 0.45, '35-45%'),
                    (0.45, 0.55, '45-55%'), (0.55, 0.65, '55-65%'), (0.65, 0.75, '65-75%'),
                    (0.75, 0.85, '75-85%'), (0.85, 1.0, '85-100%')]
            bin_label = None
            for lo, hi, lbl in bins:
                if lo <= conf < hi:
                    bin_label = lbl
                    break
            if bin_label and bin_label in mc:
                entry = mc[bin_label]
                bias_pp = entry.get('bias_pp', 0)
                # 只对偏差>2pp的区间修正, 应用50%修正量 (保守)
                if abs(bias_pp) > 2 and entry.get('n', 0) >= 50:
                    correction = bias_pp / 100.0 * 0.5
                    # 将最高概率向实际校准方向调整
                    if conf == pw:
                        pw += correction
                        pl -= correction * 0.6
                        pd -= correction * 0.4
                    elif conf == pd:
                        pd += correction
                        pw -= correction * 0.5
                        pl -= correction * 0.5
                    else:
                        pl += correction
                        pw -= correction * 0.6
                        pd -= correction * 0.4
                    notes.append(f'模型校准: {bin_label}偏差{bias_pp:+.1f}pp→{correction*100:+.1f}pp修正')

    # 归一化 + 边界保护
    s = pw + pd + pl
    if s > 0:
        pw, pd, pl = pw / s, pd / s, pl / s
    pw = max(0.05, min(0.90, pw))
    pd = max(0.05, min(0.60, pd))
    pl = max(0.05, min(0.90, pl))
    s = pw + pd + pl
    if s > 0:
        pw, pd, pl = pw / s, pd / s, pl / s

    return [pw, pd, pl], notes


# ============================================================
# Ultra 6.9: 融合后平局校准 — 数据驱动修复系统性平局低估
# 根因: 几何平均融合(ensemble_fuse)当任一源给平局低概率时,
#   融合后平局被进一步压低。历史数据证实:
#   - 主赔2.5-3.5区间: 实际平局率32.2% vs 隐含26.9% (+5.2pp)
#   - 主赔3.5+区间: 实际平局率28.6% vs 隐含22.4% (+6.2pp)
#   - 平赔2.5-3.0: 实际平局率40.9% vs 隐含30.6% (+10.3pp!)
#   - 主赔2.0-2.5区间: 实际平局率20.0% vs 隐含27.6% (-7.6pp) ← 高估!
#
# Ultra 6.9 改进 (vs 6.8):
#   1. 平赔作为主信号: 平赔与平局率相关性最强, 权重50%
#   2. 双向校准: 低估时上调, 高估时下调 (6.8只上调)
#   3. 修正强度提升: 70% gap (6.8为50%), 上限12pp (6.8为8pp)
#   4. 平局下限提升: 目标的80% (6.8为75%)
#   5. 势均力敌检测: |主赔-客赔|<0.3时额外+3pp
# ============================================================

def post_fusion_draw_calibration(probs, had, league):
    """融合后平局校准 — 双信号(主赔+平赔)数据驱动修正

    参数:
      probs: [pw, pd, pl] 融合后概率
      had: 体彩HAD赔率dict {'h':, 'd':, 'a':}
      league: 联赛名

    返回: [pw, pd, pl] 校准后概率
    """
    if not _CALIBRATION or not had or 'h' not in had:
        return probs

    pw, pd, pl = probs
    home_odds = had.get('h', 0)
    draw_odds = had.get('d', 0)
    away_odds = had.get('a', 0)

    if home_odds <= 1:
        return probs

    # --- 1. 主赔区间历史平局率 ---
    h_label = _odds_bin(home_odds)
    h_draw_cal = _CALIBRATION.get('draw_by_odds_range', {}).get(h_label) if h_label else None

    # --- 2. 平赔区间历史平局率 (主信号, 相关性最强) ---
    d_label = None
    for lo, hi, lbl in [(2.5, 3.0, '2.5-3.0'), (3.0, 3.3, '3.0-3.3'),
                         (3.3, 3.5, '3.3-3.5'), (3.5, 4.0, '3.5-4.0'), (4.0, 99, '4.0+')]:
        if draw_odds > 1 and lo <= draw_odds < hi:
            d_label = lbl
            break
    d_draw_cal = _CALIBRATION.get('draw_by_d_odds_range', {}).get(d_label) if d_label else None

    # --- 3. 综合目标平局率 ---
    # Ultra 6.9: 主赔信号40% (反映竞技平衡), 平赔信号30%, 联赛先验30%
    # (6.8原始版平赔权重50%过高, 会压制主赔信号导致过度下调)
    targets = []
    weights = []

    h_draw_rate = None
    if h_draw_cal and h_draw_cal.get('sample', 0) >= 20:
        h_draw_rate = h_draw_cal['actual_draw_rate']
        targets.append(h_draw_rate)
        weights.append(0.40)

    if d_draw_cal and d_draw_cal.get('sample', 0) >= 15:
        targets.append(d_draw_cal['actual_draw_rate'])
        weights.append(0.30)

    league_draw_rate = get_league_param(league, 'd_rate', None)
    if league_draw_rate:
        lg_val = league_draw_rate / 100.0 if league_draw_rate > 1 else league_draw_rate
        targets.append(lg_val)
        weights.append(0.30)

    if not targets:
        return probs

    # 加权平均
    total_w = sum(weights)
    target_draw = sum(t * w for t, w in zip(targets, weights)) / total_w

    # 主赔信号下限保护: 如果主赔信号高于加权目标, 取两者平均
    # (防止平赔/联赛先验拉低主赔识别出的高平局率比赛)
    if h_draw_rate and h_draw_rate > target_draw:
        target_draw = (target_draw + h_draw_rate) / 2.0

    # --- 4. 势均力敌加成 ---
    # 主客赔接近时(|h-a|<0.3), 平局概率更高
    is_close = abs(home_odds - away_odds) < 0.3 if away_odds > 1 else False
    if is_close:
        target_draw = min(0.40, target_draw + 0.03)

    # Ultra 8.0: 联赛专项平局修正 (29场回归: 瑞超HAD 0%, 历史平局率高)
    if league in ('瑞超', '韩职'):
        target_draw = min(0.42, target_draw + 0.05)

    # --- 5. 计算偏差gap ---
    gap = target_draw - pd

    # 势均力敌接近比赛额外加成: 当top-2概率差<8pp且在平局高发区间时
    # Ultra 8.0: 阈值从0.06放宽至0.08 (29场回归: 平局召回率仅9%, 需扩大触发范围)
    sorted_probs = sorted([pw, pd, pl], reverse=True)
    top2_gap = sorted_probs[0] - sorted_probs[1]
    is_competitive = 2.0 <= home_odds <= 3.5 and top2_gap < 0.08
    
    # 双向校准: gap > 0.01 上调, gap < -0.02 下调
    # 势均力敌接近比赛: 即使gap小也做修正 (因为历史偏差大)
    if abs(gap) < 0.01 and not is_competitive:
        return probs

    # --- 6. 有界修正 ---
    # Ultra 8.0: 上调修正比例70%→80%, 上限12pp→15pp (29场回归: 平局低估仍严重)
    if gap > 0 or is_competitive:
        # 势均力敌接近比赛: 最小修正3pp (确保平局得到足够权重)
        base_correction = gap * 0.80 if gap > 0 else 0
        correction = min(0.15, base_correction)
        if is_competitive:
            correction = max(correction, 0.03)  # 最小3pp上调
            correction = min(0.15, correction)
        # 平赔偏差大时额外增强
        d_bias = d_draw_cal.get('draw_bias', 0) if d_draw_cal else 0
        h_bias = h_draw_cal.get('draw_bias', 0) if h_draw_cal else 0
        if (d_bias > 0.05 or h_bias > 0.04) and not is_close:
            correction = min(0.17, correction + 0.02)
    else:
        correction = max(-0.08, gap * 0.50)

    # --- 7. 应用修正 ---
    pd_new = pd + correction

    # 从pw和pl中按比例分配/回收
    total_non_draw = pw + pl
    if total_non_draw > 0:
        pw_share = pw / total_non_draw
        pl_share = pl / total_non_draw
        pw_new = pw - correction * pw_share
        pl_new = pl - correction * pl_share
    else:
        pw_new = pw - correction * 0.5
        pl_new = pl - correction * 0.5

    # --- 8. 平局概率下限: 不低于目标的85% (Ultra 8.0: 80%→85%) ---
    draw_floor = target_draw * 0.85
    if pd_new < draw_floor:
        deficit = draw_floor - pd_new
        pd_new = draw_floor
        non_draw = pw_new + pl_new
        if non_draw > 0:
            pw_new -= deficit * (pw_new / non_draw)
            pl_new -= deficit * (pl_new / non_draw)
        else:
            pw_new -= deficit * 0.5
            pl_new -= deficit * 0.5

    # --- 9. 平局概率上限: 不超过45% (防止过度修正) ---
    if pd_new > 0.45:
        excess = pd_new - 0.45
        pd_new = 0.45
        pw_new += excess * 0.5
        pl_new += excess * 0.5

    # --- 10. 边界保护 + 归一化 ---
    pw_new = max(0.05, min(0.85, pw_new))
    pd_new = max(0.08, min(0.50, pd_new))
    pl_new = max(0.05, min(0.85, pl_new))

    s = pw_new + pd_new + pl_new
    if s > 0:
        pw_new, pd_new, pl_new = pw_new / s, pd_new / s, pl_new / s

    return [pw_new, pd_new, pl_new]


# ============================================================
# Ultra 6.10: 融合后让球平局校准 (HHAD专用)
#
# 历史数据发现 (-1球盘口, 839场):
#   - 主赔1.0-1.3区间: 让平率(主队恰好赢1球) ~23% vs 隐含 ~17% (+5pp)
#   - 主赔1.3-1.5区间: 让平率 ~25% vs 隐含 ~21% (+4pp)
#   - 按联赛: 芬超让平率32%, 欧罗巴30%, 欧冠22%
#
# -2球盘口 (52场):
#   - 主赔1.0-1.3区间: 让平率(主队恰好赢2球) ~20% vs 隐含 ~15% (+5pp)
#
# +1球盘口 (469场):
#   - 主赔2.5-3.5区间: 让平率(主队恰好输1球) ~23% vs 隐含 ~27% (-4pp, 高估!)
#   - 按联赛: 欧罗巴让平率32%, 欧协联29%, 英超23%, 芬超14%
#
# +2球盘口 (7场): 样本不足, 基础设施已搭建待数据积累
#
# -1/-2球: 模型系统性低估让平 → 上调修正
# +1球: 模型系统性高估让平 → 下调修正 (双向校准)
# 支持-1/-2/+1/+2盘口, 无标定数据的盘口直接返回。
# ============================================================

def post_fusion_hhad_draw_calibration(probs, had, hhad, handicap, league):
    """融合后让球平局校准 — 针对-1/-2/+1/+2盘口的让平率修正

    参数:
      probs: [pw, pd, pl] 融合后HHAD概率 (让胜/让平/让负)
      had: 体彩HAD赔率dict {'h':, 'd':, 'a':}
      hhad: 体彩HHAD赔率dict {'h':, 'd':, 'a':}
      handicap: 让球数 (负数表示主队让球, 如-1表示主队让1球)
      league: 联赛名

    返回: [pw, pd, pl] 校准后概率
    """
    # 支持的盘口: -1, -2, +1, +2
    # handicap为整数, 标定数据key带符号前缀(如'+1', '-1')
    if handicap is None:
        return probs
    hcap_key = str(handicap) if handicap < 0 else f'+{handicap}'
    if not _CALIBRATION or hcap_key not in ('-1', '-2', '+1', '+2'):
        return probs

    if not had or 'h' not in had:
        return probs

    pw, pd, pl = probs
    home_odds = had.get('h', 0)
    if home_odds <= 1:
        return probs

    # 获取该盘口的标定数据
    hcap_data = _CALIBRATION.get('hcap_calibration', {}).get(hcap_key, {})
    if not hcap_data:
        return probs

    # --- 1. 按HAD主赔区间获取历史让平率 ---
    h_label = None
    for lo, hi, lbl in [(1.0, 1.3, '1.0-1.3'), (1.3, 1.5, '1.3-1.5'),
                         (1.5, 1.8, '1.5-1.8'), (1.8, 2.0, '1.8-2.0'),
                         (2.0, 2.5, '2.0-2.5'), (2.5, 99, '2.5+')]:
        if lo <= home_odds < hi:
            h_label = lbl
            break

    hcap_cal = hcap_data.get('by_range', {}).get(h_label) if h_label else None

    # --- 2. 按联赛获取让平率 ---
    # 标定数据中联赛名不含赛季后缀(如'挪超'), 而引擎传入含后缀(如'挪超_2026')
    # 先精确匹配, 再尝试去后缀匹配
    lg_hcap = hcap_data.get('by_league', {}).get(league)
    if not lg_hcap and league:
        lg_short = re.sub(r'_\d{4}(-\d{2})?$', '', league)
        if lg_short != league:
            lg_hcap = hcap_data.get('by_league', {}).get(lg_short)

    # --- 3. 综合目标让平率 ---
    # 赔率区间信号55% (直接反映该赔率档的让平频率), 联赛信号45%
    targets = []
    weights = []

    if hcap_cal and hcap_cal.get('sample', 0) >= 10:
        targets.append(hcap_cal['actual_draw_rate'])
        weights.append(0.55)

    if lg_hcap and lg_hcap.get('sample', 0) >= 10:
        targets.append(lg_hcap['draw_rate'])
        weights.append(0.45)

    if not targets:
        return probs

    total_w = sum(weights)
    target_draw = sum(t * w for t, w in zip(targets, weights)) / total_w

    # --- 4. 计算偏差 ---
    gap = target_draw - pd

    # 双向校准: gap > 0.01 上调(模型低估), gap < -0.02 下调(模型高估)
    # -1/-2球: 历史显示系统性低估(正偏差为主)
    # +1球: 历史显示系统性高估(负偏差, 2.5-3.5区间-3.8pp)
    if abs(gap) < 0.01:
        return probs

    # --- 5. 有界修正 ---
    draw_bias = hcap_cal.get('draw_bias', 0) if hcap_cal else 0

    if gap > 0:
        # 上调: 65% of gap, cap 10pp
        correction = min(0.10, gap * 0.65)
        # 赔率偏差大时额外增强
        if draw_bias > 0.04:
            correction = min(0.12, correction + 0.015)
    else:
        # 下调: 50% of gap (更保守), cap 8pp
        correction = max(-0.08, gap * 0.50)

    # --- 6. 应用修正 ---
    pd_new = pd + correction

    # 从pw和pl中按比例分配
    total_non_draw = pw + pl
    if total_non_draw > 0:
        pw_share = pw / total_non_draw
        pl_share = pl / total_non_draw
        pw_new = pw - correction * pw_share
        pl_new = pl - correction * pl_share
    else:
        pw_new = pw - correction * 0.5
        pl_new = pl - correction * 0.5

    # --- 7. 让平概率下限: 不低于目标的75% ---
    draw_floor = target_draw * 0.75
    if pd_new < draw_floor:
        deficit = draw_floor - pd_new
        pd_new = draw_floor
        non_draw = pw_new + pl_new
        if non_draw > 0:
            pw_new -= deficit * (pw_new / non_draw)
            pl_new -= deficit * (pl_new / non_draw)
        else:
            pw_new -= deficit * 0.5
            pl_new -= deficit * 0.5

    # --- 8. 让平概率上限: 不超过40% (让平概率天然低于HAD平局) ---
    if pd_new > 0.40:
        excess = pd_new - 0.40
        pd_new = 0.40
        pw_new += excess * 0.5
        pl_new += excess * 0.5

    # --- 9. 边界保护 + 归一化 ---
    pw_new = max(0.05, min(0.85, pw_new))
    pd_new = max(0.08, min(0.45, pd_new))
    pl_new = max(0.05, min(0.85, pl_new))

    s = pw_new + pd_new + pl_new
    if s > 0:
        pw_new, pd_new, pl_new = pw_new / s, pd_new / s, pl_new / s

    # ===== Step 10: HHAD穿盘/输盘规律二次校准 (Ultra 10.4) =====
    # 基于4412场历史数据: Skellam分布系统性高估走水概率(偏差-9.2pp)
    # 同时按盘口和联赛做更精细校准, 与上层_CALIBRATION校准叠加
    if _HHAD_CALIB:
        hhad_analysis = _HHAD_CALIB.get('hhad_analysis', {})
        implied_vs_actual = hhad_analysis.get('implied_vs_actual', {})

        # 1. 整体隐含偏差: Skellam高估走水
        push_bias_pp = implied_vs_actual.get('push', {}).get('bias_pp', 0)

        # 2. 按盘口区间 (handicap为整数, 转为HHAD浮点key)
        hhad_gl_key = f'{float(handicap):.1f}'
        gl_data = hhad_analysis.get('by_goal_line', {}).get(hhad_gl_key, {})

        # 3. 按联赛
        lg_hhad = hhad_analysis.get('by_league', {}).get(league)
        if not lg_hhad and league:
            lg_short = re.sub(r'_\d{4}(-\d{2})?$', '', league)
            if lg_short != league:
                lg_hhad = hhad_analysis.get('by_league', {}).get(lg_short)

        # 构建二次校准目标
        hhad_targets = []
        hhad_weights = []

        # 整体偏差信号(权重30%): bias -9.2pp → 实际走水率更低
        if push_bias_pp < -3:
            adjusted_push = pd_new + push_bias_pp / 100.0 * 0.5  # 保守50%
            hhad_targets.append(adjusted_push)
            hhad_weights.append(0.30)

        # 盘口区间信号(权重40%)
        gl_push = gl_data.get('push', {}) if gl_data else {}
        if gl_push and gl_push.get('n', 0) >= 50:
            actual_push_rate = gl_push.get('pct', 0)
            if actual_push_rate > 0:
                hhad_targets.append(actual_push_rate)
                hhad_weights.append(0.40)

        # 联赛信号(权重30%)
        if lg_hhad and lg_hhad.get('n', 0) >= 20:
            lg_push_rate = lg_hhad.get('push_rate', 0)
            if lg_push_rate > 0:
                hhad_targets.append(lg_push_rate)
                hhad_weights.append(0.30)

        if hhad_targets:
            total_hw = sum(hhad_weights)
            hhad_target_draw = sum(t * w for t, w in zip(hhad_targets, hhad_weights)) / total_hw

            # 二次校准偏差
            gap2 = hhad_target_draw - pd_new
            if abs(gap2) >= 0.01:
                # 保守修正: 40% of gap, cap 5pp
                correction2 = max(-0.05, min(0.05, gap2 * 0.40))
                pd_new += correction2
                # 从pw和pl按比例分配
                total_non_draw = pw_new + pl_new
                if total_non_draw > 0:
                    pw_share = pw_new / total_non_draw
                    pl_share = pl_new / total_non_draw
                    pw_new -= correction2 * pw_share
                    pl_new -= correction2 * pl_share
                else:
                    pw_new -= correction2 * 0.5
                    pl_new -= correction2 * 0.5

                # 重新边界保护 + 归一化
                pw_new = max(0.05, min(0.85, pw_new))
                pd_new = max(0.08, min(0.45, pd_new))
                pl_new = max(0.05, min(0.85, pl_new))
                s2 = pw_new + pd_new + pl_new
                if s2 > 0:
                    pw_new, pd_new, pl_new = pw_new / s2, pd_new / s2, pl_new / s2

    # ===== Step 11: 让平概率虚高下修 (Ultra 11.4, 专项回归 2026-08-06) =====
    # 回归发现(51场HHAD): 模型平均让平概率27.1% vs 实际让平率15.7%, 让平概率虚高超11pp
    # 且让平零判别度(实际让平场26.1% vs 总体27.1%, 无差异)
    # 修正: 让平概率向实测让平率上沿(~20%)收敛, 差额等比分配给让胜/让负
    # 效果: 不改变方向选择(让平从未为argmax), 但修正让胜/让负概率欠自信偏差
    #       提升方向概率准确性 → 改善EV/置信度标定
    # 说明: 与其他Step的"上调让平"逻辑不同, 本步专门处理让平虚高的回归发现
    HHAD_DRAW_DEFLATE_TARGET = 0.20   # 实测让平率15.7%, 取上沿20%作为目标
    HHAD_DRAW_DEFLATE_STRENGTH = 0.5  # 修正强度: 只修正50%偏差, 保守
    if pd_new > HHAD_DRAW_DEFLATE_TARGET:
        _excess = pd_new - HHAD_DRAW_DEFLATE_TARGET
        _corr = _excess * HHAD_DRAW_DEFLATE_STRENGTH
        pd_new -= _corr
        _dd_tot = pw_new + pl_new
        if _dd_tot > 0:
            pw_new += _corr * (pw_new / _dd_tot)
            pl_new += _corr * (pl_new / _dd_tot)
        else:
            pw_new += _corr * 0.5
            pl_new += _corr * 0.5
        # 边界保护 + 归一化
        pw_new = max(0.05, min(0.85, pw_new))
        pd_new = max(0.08, min(0.45, pd_new))
        pl_new = max(0.05, min(0.85, pl_new))
        _dd_s = pw_new + pd_new + pl_new
        if _dd_s > 0:
            pw_new, pd_new, pl_new = pw_new / _dd_s, pd_new / _dd_s, pl_new / _dd_s

    # ===== Step 12: 方向概率欠自信上调 (Ultra 11.5, 专项回归 2026-08-06) =====
    # 回归发现(51场HHAD): 方向概率整体欠自信, 实测定向概率(P)系统性低于实际命中率
    # 校准曲线: 实际命中率在多个P档均>预测P(欠自信), 低档P<40%实际命中53% vs 预测31%
    # 修正: 概率尺度校准 f(p)=p^r (r<1增强高概率, 压缩低概率), 等比归一化
    # 效果: 单调变换保持方向排序不变 → 不改变方向命中率, 但提升概率校准(Brier/ECE)
    #       及EV/置信度标定精度
    # 说明: 幂变换是单调的, 不会翻转argmax; 方向命中率由特征排序决定, 非概率尺度
    HHAD_UC_POWER = 0.85   # 幂指数 r<1 = 欠自信上调 (增强高概率方向)
    pw_new = pw_new ** HHAD_UC_POWER
    pd_new = pd_new ** HHAD_UC_POWER
    pl_new = pl_new ** HHAD_UC_POWER
    _uc_s = pw_new + pd_new + pl_new
    if _uc_s > 0:
        pw_new, pd_new, pl_new = pw_new / _uc_s, pd_new / _uc_s, pl_new / _uc_s

    return [pw_new, pd_new, pl_new]


def apply_odds_change_analysis_calibration(probs, had, hhad, league, odds_change, 
                                            had_hhad_change=None, sp_had_probs=None, sp_hhad_probs=None,
                                            had_init=None, hhad_init=None):
    """体彩赔率变动特征 + 玩法矛盾信号校准 (Ultra 10.6 → 11.0)
    
    基于3274场数据分析的3个全新校准信号:
    1. draw_change: 平局赔率变动 → 平赔下降时看好分出胜负
    2. conflict_signal: HAD/HHAD矛盾 → HAD自信度更高时以HAD为准
    3. had_hhad_linkage: 联动信号 → 同时上升时信号更强
    
    Ultra 11.0: 新增第4个信号 — 体彩初赔→终赔变动方向校准
    基于4222场分析: 终赔相对初赔方向与命中率显著相关
    
    参数:
      probs: [pw, pd, pl] 当前概率
      had: 体彩HAD赔率dict {'h':, 'd':, 'a':}
      hhad: 体彩HHAD赔率dict {'h':, 'd':, 'a':}
      league: 联赛名
      odds_change: dict含 'draw_change' 方向 ('上升'/'下降'/'不变' 或 None)
      had_hhad_change: dict含 'had_h'/'hhad_h' 方向 或 None (用于联动信号)
      sp_had_probs: HAD Shin概率 [pw, pd, pl] 或 None (用于矛盾信号)
      sp_hhad_probs: HHAD Shin概率 [pw, pd, pl] 或 None (用于矛盾信号)
      had_init: 体彩HAD初赔dict {'h':, 'd':, 'a':} 或 None (用于变动方向校准)
      hhad_init: 体彩HHAD初赔dict {'h':, 'd':, 'a':} 或 None
    """
    if not _ODDS_CHANGE_ANALYSIS_CALIB:
        return probs, []
    
    pw, pd, pl = probs
    notes = []
    calib = _ODDS_CHANGE_ANALYSIS_CALIB
    
    # ===== 0. Ultra 11.0: 体彩初赔→终赔变动方向校准 =====
    # 基于4222场历史分析: 终赔相对初赔的变动方向与命中率显著相关
    # 主胜上升→31.7%, 下降→45.4%, 不变→53.4%
    # 平局上升→23.3%, 下降→27.5%, 不变→26.0%
    # 客胜上升→24.9%, 下降→34.9%, 不变→38.3%
    om_calib = calib.get('odds_movement')
    if om_calib and had_init and had:
        threshold = om_calib.get('threshold', 0.05)
        om_notes = []
        
        # 判断每个选项的变动方向
        def _get_dir(init_val, final_val, thresh):
            if init_val is None or final_val is None or init_val <= 0 or final_val <= 0:
                return None
            diff = final_val - init_val
            if diff > thresh:
                return '上升'
            elif diff < -thresh:
                return '下降'
            else:
                return '不变'
        
        h_dir = _get_dir(had_init.get('h'), had.get('h'), threshold)
        d_dir = _get_dir(had_init.get('d'), had.get('d'), threshold)
        a_dir = _get_dir(had_init.get('a'), had.get('a'), threshold)
        
        # 获取每个选项的LR (likelihood ratio)
        h_lr = None
        d_lr = None
        a_lr = None
        if h_dir and h_dir in om_calib.get('h', {}):
            h_lr = om_calib['h'][h_dir]['lr']
        if d_dir and d_dir in om_calib.get('d', {}):
            d_lr = om_calib['d'][d_dir]['lr']
        if a_dir and a_dir in om_calib.get('a', {}):
            a_lr = om_calib['a'][a_dir]['lr']
        
        # 用LR调整概率 (保守系数15%)
        conservative = 0.15
        if h_lr and d_lr and a_lr:
            # 计算调整量: 相对于1.0的偏差
            h_adj = (h_lr - 1.0) * conservative
            d_adj = (d_lr - 1.0) * conservative
            a_adj = (a_lr - 1.0) * conservative
            
            # 限制最大调整幅度 ±3pp
            h_adj = max(-0.03, min(0.03, h_adj))
            d_adj = max(-0.03, min(0.03, d_adj))
            a_adj = max(-0.03, min(0.03, a_adj))
            
            pw *= (1.0 + h_adj)
            pd *= (1.0 + d_adj)
            pl *= (1.0 + a_adj)
            
            # 记录变动方向
            dir_strs = []
            if h_dir: dir_strs.append(f'主{h_dir}')
            if d_dir: dir_strs.append(f'平{d_dir}')
            if a_dir: dir_strs.append(f'客{a_dir}')
            adj_strs = []
            if abs(h_adj) >= 0.005: adj_strs.append(f'主{h_adj*100:+.1f}pp')
            if abs(d_adj) >= 0.005: adj_strs.append(f'平{d_adj*100:+.1f}pp')
            if abs(a_adj) >= 0.005: adj_strs.append(f'客{a_adj*100:+.1f}pp')
            if adj_strs:
                om_notes.append(f'初终赔变动校准: {",".join(dir_strs)} → {",".join(adj_strs)}')
        
        notes.extend(om_notes)
    
    # ===== 1. 平局赔率变动校准 (Part 5) =====
    # 平赔下降→主胜44.1%, 平赔上升→平局38.9%
    # 应用在HAD平局概率上
    if odds_change and odds_change.get('draw_change'):
        dc = odds_change['draw_change']
        dc_data = calib.get('draw_change', {}).get('变动', {}).get(dc)
        if dc_data and dc_data['n'] >= 100:
            base_draw = calib['draw_change']['base_draw_rate']
            target_draw = dc_data['draw_rate']
            # 偏差量, 保守20%修正
            draw_delta = (target_draw - base_draw) * 0.20
            draw_delta = max(-0.02, min(0.02, draw_delta))
            if abs(draw_delta) > 0.005:
                pd += draw_delta
                pw -= draw_delta * 0.5
                pl -= draw_delta * 0.5
                notes.append(f'平赔变动校准: {dc}→平{"↑" if draw_delta>0 else "↓"}{abs(draw_delta)*100:.1f}pp')
    
    # ===== 2. HAD-HHAD联动信号校准 (Part 4) =====
    # 同时上升→主胜45.9%, 同时下降→主胜32.2%
    if had_hhad_change and had_hhad_change.get('had_h') and had_hhad_change.get('hhad_h'):
        hh = had_hhad_change
        linkage = calib.get('had_hhad_linkage', {})
        if hh['had_h'] == '上升' and hh['hhad_h'] == '上升':
            both = linkage.get('both_up', {})
            if both and both['n'] >= 100:
                # 联动信号, 温和增强(与原有变动信号叠加)
                linkage_home = both['home_rate']
                base_home = 0.401  # 整体基准主胜率
                home_delta = (linkage_home - base_home) * 0.15  # 15%修正
                home_delta = max(-0.015, min(0.015, home_delta))
                if abs(home_delta) > 0.003:
                    pw += home_delta
                    pd -= home_delta * 0.3
                    pl -= home_delta * 0.7
                    notes.append(f'HAD-HHAD联动: 同时上升→主{home_delta*100:+.1f}pp')
        elif hh['had_h'] == '下降' and hh['hhad_h'] == '下降':
            both = linkage.get('both_down', {})
            if both and both['n'] >= 100:
                linkage_home = both['home_rate']
                base_home = 0.401
                home_delta = (linkage_home - base_home) * 0.15
                home_delta = max(-0.015, min(0.015, home_delta))
                if abs(home_delta) > 0.003:
                    pw += home_delta  # 负值→主胜下降
                    pd -= home_delta * 0.3
                    pl -= home_delta * 0.7
                    notes.append(f'HAD-HHAD联动: 同时下降→主{home_delta*100:+.1f}pp')
    
    # ===== 3. HAD/HHAD矛盾信号 → HHAD可靠性调整 (Part 6) =====
    # 当HAD自信度>5pp高于HHAD时, HAD准确率56.4% vs HHAD仅33.5%
    # 此信号在HHAD融合后调用, 降低HHAD置信度或调整方向
    # (HAD侧的校准已有单独的逻辑, 这里仅记录信息)
    if sp_had_probs and sp_hhad_probs and hhad and 'h' in hhad:
        had_conf = max(sp_had_probs)
        hhad_conf = max(sp_hhad_probs)
        conf_diff_pp = (had_conf - hhad_conf) * 100
        
        # 确定自信度差异区间
        conf_diff_data = calib.get('conflict_signal', {}).get('confidence_diff', {})
        diff_label = None
        if conf_diff_pp > 5:
            diff_label = 'HAD自信度更高(>5pp)'
        elif conf_diff_pp > 2:
            diff_label = 'HAD略高(2~5pp)'
        elif conf_diff_pp > -2:
            diff_label = 'HAD-HHAD接近(±2pp)'
        elif conf_diff_pp > -5:
            diff_label = 'HHAD略高(-5~-2pp)'
        else:
            diff_label = 'HHAD自信度更高(<-5pp)'
        
        if diff_label and diff_label in conf_diff_data:
            entry = conf_diff_data[diff_label]
            # 返回HHAD可靠度系数, 供HHAD侧使用
            hhad_reliability = entry.get('hhad_可靠度', 0.5)
            notes.append(f'矛盾信号: {diff_label} (HAD准确率{entry["had_accuracy"]}%, HHAD可靠度{hhad_reliability:.0%})')
            return [pw, pd, pl], notes, hhad_reliability
    
    # 归一化 + 边界保护
    s = pw + pd + pl
    if s > 0:
        pw, pd, pl = pw / s, pd / s, pl / s
    pw = max(0.05, min(0.90, pw))
    pd = max(0.05, min(0.60, pd))
    pl = max(0.05, min(0.90, pl))
    s = pw + pd + pl
    if s > 0:
        pw, pd, pl = pw / s, pd / s, pl / s
    
    return [pw, pd, pl], notes, 0.5  # 默认HHAD可靠度0.5


_DRAW_BIAS_CACHE = {'value': None}

def query_draw_bias():
    """平局偏差在线反馈 (Ultra 6.5) — 校准闭环

    从 verify_history 统计: 实际平局率 vs 平均预测平局概率。
    样本 >= 30 时返回有界修正量 (±0.03), 叠加到 target_draw:
      实际平局率低于预测 → 负修正 (模型高估平局, 下调目标)
      实际平局率高于预测 → 正修正 (模型低估平局, 上调目标)
    修正量 = 偏差 × 0.5 (半速收敛, 防止单期波动过拟合)。
    结果进程级缓存, 每次运行只查一次库。
    """
    if _DRAW_BIAS_CACHE['value'] is not None:
        return _DRAW_BIAS_CACHE['value']
    _DRAW_BIAS_CACHE['value'] = 0.0
    conn = None
    try:
        db_path = os.path.join(os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__)),
                               'predictions', 'regression.db')
        if not os.path.exists(db_path):
            return 0.0
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("""SELECT pred_had_probs, pred_had_p, had_result FROM verify_history
            WHERE (pred_had_probs IS NOT NULL AND pred_had_probs != ''
                   OR pred_had_p IS NOT NULL AND pred_had_p != '')
              AND had_result IN ('胜','平','负')""")
        rows = c.fetchall()
        preds, draws = [], 0
        for probs_s, p_s, r in rows:
            # 优先用独立概率字段 pred_had_probs, 失败再用 pred_had_p 显示字符串
            m = re.findall(r'(\d+(?:\.\d+)?)%', str(probs_s or p_s))
            if len(m) == 3:
                preds.append(float(m[1]) / 100.0)
                draws += (r == '平')
        n = len(preds)
        if n < 30:
            return 0.0
        bias = draws / n - sum(preds) / n
        corr = max(-0.03, min(0.03, bias * 0.5))
        print(f"[draw-bias] n={n} 实际平局率={draws/n:.1%} 预测均值={sum(preds)/n:.1%} 修正={corr:+.1%}")
        _DRAW_BIAS_CACHE['value'] = corr
        return corr
    except Exception:
        return 0.0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

def query_historical_feedback(league, had_dir, conf_score, odds):
    """查询历史验证数据获取反馈 — 贝叶斯更新版 (Ultra 6.1)

    用Beta-Binomial共轭模型替代频率统计。
    小样本自动收缩向全局均值, 提供可信区间。

    层次贝叶斯:
        全局先验: Beta(α₀, β₀), α₀/β₀ 由全局命中率设定 (虚拟样本数=5)
        联赛后验: Beta(α₀+hits, β₀+misses)
        方向后验: Beta(α₀+hits, β₀+misses)

    参数:
      league: 联赛名
      had_dir: 预测方向 (胜/平/负)
      conf_score: 置信度分数 (1.0-5.0)
      odds: 预测赔率

    返回: dict 或 None (无历史数据时)
    """
    # Ultra-Opt: 通用路径 (旧版硬编码Linux路径, Windows上历史反馈从未生效)
    DB_PATH = os.path.join(os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__)),
                           'predictions', 'regression.db')
    if not os.path.exists(DB_PATH):
        return None

    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        feedback = {}

        # 1. 整体命中率 (用于设定层次先验)
        c.execute('''SELECT COUNT(*), SUM(had_hit) FROM verify_history
            WHERE pred_had_dir != '' AND pred_had_dir != '无预测' ''')
        row = c.fetchone()
        total, hits = row[0] or 0, row[1] or 0
        if total < 3:
            return None
        feedback['overall_rate'] = round(hits / total * 100, 1)
        feedback['sample_size'] = total

        # 层次先验: 用全局命中率设定Beta参数 (虚拟样本数=5)
        prior_strength = 5.0
        global_rate = hits / total if total > 0 else 0.333
        prior_a = global_rate * prior_strength
        prior_b = (1 - global_rate) * prior_strength

        def _bayesian(h, n):
            """计算Beta-Binomial后验均值和95%CI"""
            if n == 0:
                return 0.333, 0.0, 1.0
            a = prior_a + h
            b = prior_b + (n - h)
            mean = a / (a + b)
            # 正态近似CI
            var = a * b / ((a + b) ** 2 * (a + b + 1))
            std = math.sqrt(max(var, 1e-10))
            ci_lo = max(0.0, mean - 1.96 * std)
            ci_hi = min(1.0, mean + 1.96 * std)
            return mean, ci_lo, ci_hi

        # 2. 联赛命中率 (贝叶斯)
        if league:
            c.execute('''SELECT COUNT(*), SUM(had_hit) FROM verify_history
                WHERE pred_had_dir != '' AND league = ?''', (league,))
            row = c.fetchone()
            lg_total, lg_hits = row[0] or 0, row[1] or 0
            if lg_total >= 1:
                bayes_rate, ci_lo, ci_hi = _bayesian(lg_hits or 0, lg_total)
                feedback['league_rate'] = round(bayes_rate * 100, 1)
                feedback['league_samples'] = lg_total
                feedback['league_ci'] = f"{ci_lo*100:.0f}%-{ci_hi*100:.0f}%"

        # 3. 方向命中率 (贝叶斯)
        if had_dir:
            c.execute('''SELECT COUNT(*), SUM(had_hit) FROM verify_history
                WHERE pred_had_dir = ?''', (had_dir,))
            row = c.fetchone()
            dir_total, dir_hits = row[0] or 0, row[1] or 0
            if dir_total >= 1:
                bayes_rate, ci_lo, ci_hi = _bayesian(dir_hits or 0, dir_total)
                feedback['direction_rate'] = round(bayes_rate * 100, 1)
                feedback['direction_samples'] = dir_total
                feedback['direction_ci'] = f"{ci_lo*100:.0f}%-{ci_hi*100:.0f}%"

        # 4. 置信度校准 (贝叶斯)
        if conf_score >= 4.0:
            # 覆盖 4.0★(★★★★)/4.5★(★★★★½)/5.0★(★★★★★) 三档, 用 GLOB 精确排除高星误匹配
            c.execute('''SELECT COUNT(*), SUM(had_hit) FROM verify_history
                WHERE pred_had_dir != '' AND (
                    pred_had_conf GLOB '*★★★★★'
                    OR pred_had_conf GLOB '*★★★★½'
                    OR (pred_had_conf GLOB '*★★★★'
                        AND pred_had_conf NOT GLOB '*★★★★★'
                        AND pred_had_conf NOT GLOB '*★★★★½')
                )''')
            row = c.fetchone()
            hc_total, hc_hits = row[0] or 0, row[1] or 0
            if hc_total >= 1:
                bayes_rate, ci_lo, ci_hi = _bayesian(hc_hits or 0, hc_total)
                feedback['high_conf_rate'] = round(bayes_rate * 100, 1)
                feedback['high_conf_samples'] = hc_total
                feedback['high_conf_ci'] = f"{ci_lo*100:.0f}%-{ci_hi*100:.0f}%"
                # Ultra 7.7: 放宽阈值 0.55→0.50, 更早触发校准警告
                if bayes_rate < 0.50:
                    feedback['calibration_warning'] = '高置信度贝叶斯命中率偏低, 置信度评级可能过拟合'

        # 5. 赔率区间命中率 (贝叶斯)
        if odds and odds > 0:
            odds_min = max(1.0, odds - 0.3)
            odds_max = odds + 0.3
            c.execute('''SELECT COUNT(*), SUM(had_hit) FROM verify_history
                WHERE pred_had_dir != '' AND pred_had_odds BETWEEN ? AND ?''',
                (odds_min, odds_max))
            row = c.fetchone()
            o_total, o_hits = row[0] or 0, row[1] or 0
            if o_total >= 1:
                bayes_rate, ci_lo, ci_hi = _bayesian(o_hits or 0, o_total)
                feedback['odds_range_rate'] = round(bayes_rate * 100, 1)
                feedback['odds_range_samples'] = o_total
                feedback['odds_range_ci'] = f"{ci_lo*100:.0f}%-{ci_hi*100:.0f}%"

        if len(feedback) <= 2:
            return None

        # 生成建议 (基于贝叶斯后验, 更稳健)
        # Ultra 7.7: 放宽预警阈值 — 实证显示旧阈值(40/35/55)过保守
        recs = []
        if 'league_rate' in feedback and feedback['league_rate'] < 50:
            ci = feedback.get('league_ci', '?')
            recs.append(f"{league}贝叶斯命中率{feedback['league_rate']}%(CI:{ci})偏低")
        if 'direction_rate' in feedback and feedback['direction_rate'] < 45:
            ci = feedback.get('direction_ci', '?')
            recs.append(f"{had_dir}方向贝叶斯命中率{feedback['direction_rate']}%(CI:{ci})偏低")
        if 'high_conf_rate' in feedback and feedback['high_conf_rate'] < 55:
            ci = feedback.get('high_conf_ci', '?')
            recs.append(f"高置信度贝叶斯命中率{feedback['high_conf_rate']}%(CI:{ci})偏低")

        feedback['recommendation'] = '；'.join(recs) if recs else '历史规律正常(贝叶斯)'

        return feedback
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass

def predict_match(match_num, data):
    """七步预测 — 内部计算，仅输出结论"""
    sp = data
    had = sp['HAD']
    hhad = sp['HHAD']
    ouzhi = sp.get('ouzhi') or {}
    shuju = sp.get('shuju') or {}
    daxiao = sp.get('daxiao') or {}
    
    # ===== 初赔数据 (Phase 3b AJAX端点) =====
    init_ouzhi = sp.get('init_ouzhi')
    init_yazhi = sp.get('init_yazhi')
    init_daxiao = sp.get('init_daxiao')
    
    # ===== 市场盘口（goal line）— 优先使用初赔AJAX数据 =====
    league = sp.get('league', '')  # 提前获取联赛名 (盘口推断+标定均需要)
    if init_daxiao and init_daxiao.get('instant'):
        market_goal_line = abs(init_daxiao['instant']['goal_line_mode'])
        market_gl_source = f"500.com初赔({init_daxiao['num_valid']}家)"
        initial_goal_line = abs(init_daxiao['initial']['goal_line_mode']) if init_daxiao.get('initial') else market_goal_line
    else:
        market_goal_line = daxiao.get('goal_line', 2.5)
        market_gl_source = daxiao.get('source', '默认值')
        initial_goal_line = daxiao.get('initial_goal_line', market_goal_line)
    
    # 无外部盘口数据时, 从历史库推断市场盘口
    if market_gl_source == '默认值' and had and had.get('h'):
        _conn = _get_adv_db()
        if _conn:
            _c = _conn.cursor()
            try:
                # 查找该联赛最近10场的平均总进球 (子查询确保LIMIT生效)
                _c.execute('''SELECT AVG(total_goals) FROM (
                                  SELECT (home_score + away_score) as total_goals 
                                  FROM historical_matches 
                                  WHERE league = ? AND home_score IS NOT NULL AND home_score >= 0
                                  AND away_score IS NOT NULL AND away_score >= 0
                                  AND match_date < ? 
                                  ORDER BY match_date DESC LIMIT 10
                              )''',
                           (league, sp.get('match_date', '9999')))
                _row = _c.fetchone()
                if _row and _row[0] and _row[0] > 0:
                    market_goal_line = round(_row[0] * 2) / 2  # 取最近的0.5
                    market_goal_line = max(1.5, min(4.5, market_goal_line))
                    market_gl_source = f'历史库推断({league}场均{_row[0]:.1f})'
                    print(f"  [盘口推断] {league} 最近10场场均{_row[0]:.1f}球 → 盘口{market_goal_line}")
                else:
                    print(f"  [盘口推断] {league} 历史库无足够数据, 保持默认2.5")
            except Exception as e:
                print(f"  [盘口推断] 历史库查询失败: {e}")
    
    # Step 2: P0 (体彩 + 500.com融合)
    # Ultra 2.0: 使用Shin's method替代简单1/odds归一化, 修正favorite-longshot bias
    # 体彩HAD可能为空(未开盘)，此时用500.com欧指代替
    # league 已在盘口推断前定义
    if had and had.get('h', 0) > 0 and had.get('d', 0) > 0 and had.get('a', 0) > 0:
        # Ultra 2.0: Shin's method 替代简单归一化
        shin_probs = shin_method([had['h'], had['d'], had['a']])
        # Ultra 6.6: 联赛标定修正 Shin 输出 (按联赛+赔率区间修正系统性偏差)
        shin_probs = calibrate_shin_probs(shin_probs, league, had.get('h', 0))
        pw_s, pd_s, pl_s = shin_probs[0], shin_probs[1], shin_probs[2]
    else:
        # 体彩未开盘，用500.com欧指作为先验
        pw_s = pd_s = pl_s = 0
    
    # 获取500.com欧指: 优先使用shuju页面的平均欧指(实际赔率)，其次ouzhi API
    avg_odds = shuju.get('avg_odds')
    ouzhi_is_rr = ouzhi.get('is_return_rate', False) if ouzhi else True
    
    if avg_odds:
        ow, od, ol = avg_odds['w'], avg_odds['d'], avg_odds['l']
    elif ouzhi and not ouzhi_is_rr:
        # ouzhi返回正常赔率(>1.0)
        ow = ouzhi.get('latest_w', 2.0)
        od = ouzhi.get('latest_d', 3.2)
        ol = ouzhi.get('latest_l', 3.0)
    elif ouzhi and ouzhi_is_rr:
        # ouzhi返回返还率(<1.0)
        # 检测最新值是否收敛(三值差异<0.05→无区分度)
        rw_latest = ouzhi['latest_w']; rd_latest = ouzhi['latest_d']; rl_latest = ouzhi['latest_l']
        spread = max(rw_latest, rd_latest, rl_latest) - min(rw_latest, rd_latest, rl_latest)
        
        if spread < 0.05:
            # 最新值收敛，回退使用初始值（有区分度）
            rw = ouzhi['init_w']; rd = ouzhi['init_d']; rl = ouzhi['init_l']
            ouzhi_source = 'initial'  # 标记使用了初始值
        else:
            rw = rw_latest; rd = rd_latest; rl = rl_latest
            ouzhi_source = 'latest'
        
        rs = rw + rd + rl
        pw5, pd5, pl5 = rw/rs, rd/rs, rl/rs
        ow = od = ol = 0  # 标记已直接计算概率
    else:
        # 无ouzhi数据，用体彩或默认值
        ow = float(had.get('h')) if had and had.get('h') else 2.0
        od = float(had.get('d')) if had and had.get('d') else 3.2
        ol = float(had.get('a')) if had and had.get('a') else 3.0
    
    if ow > 0:  # 正常赔率路径
        # Ultra 2.0: Shin's method 替代简单1/odds归一化
        shin_probs5 = shin_method([ow, od, ol])
        # Ultra 6.6: 联赛标定修正 Shin 输出
        shin_probs5 = calibrate_shin_probs(shin_probs5, league, ow)
        pw5, pd5, pl5 = shin_probs5[0], shin_probs5[1], shin_probs5[2]
    
    if had and had.get('h', 0) > 0:
        p0_w = pw_s * 0.5 + pw5 * 0.5
        p0_d = pd_s * 0.5 + pd5 * 0.5
        p0_l = pl_s * 0.5 + pl5 * 0.5
    else:
        p0_w, p0_d, p0_l = pw5, pd5, pl5
    
    # Step 3-5: 修正+更新
    evidence = []
    # 优先使用初赔AJAX的赔率变化(基于真实赔率, 非返还率)
    if init_ouzhi:
        ouzhi_change = init_ouzhi['change_w']
        ouzhi_change_str = f"{'↓' if ouzhi_change < 0 else '↑' if ouzhi_change > 0 else '→'}({abs(ouzhi_change):.2f})"
    else:
        ouzhi_change = ouzhi.get('change_w', 0)
        ouzhi_change_str = f"{'↓' if ouzhi_change < 0 else '↑' if ouzhi_change > 0 else '→'}({abs(ouzhi_change):.2f})" if ouzhi else 'N/A'
    
    home_form = shuju.get('form_home', '')
    away_form = shuju.get('form_away', '')
    rec = shuju.get('recommendation', '')
    
    # 根据赔率判断方向 (仅用 >0 的有效赔率, 避免 0 值停售档位被 min 选中)
    if had and 'h' in had:
        _valid_had = [x for x in [had['h'], had['d'], had['a']] if x and x > 0]
        if len(_valid_had) == 3:
            had_list = [had['h'], had['d'], had['a']]
            had_min_idx = had_list.index(min(had_list))
        else:
            # 部分档位无效(停售/未开盘), 回退到欧指概率方向
            had_list = [ow, od, ol]
            had_min_idx = had_list.index(min(had_list))
    elif ouzhi and ouzhi_is_rr:
        # 用返还率转换的概率判断方向
        had_list = [pw5, pd5, pl5]
        had_min_idx = had_list.index(max(had_list))  # 概率最大=赔率最低
    else:
        had_list = [ow, od, ol]
        had_min_idx = had_list.index(min(had_list))
    
    if hhad and 'h' in hhad:
        _valid_hhad = [x for x in [hhad['h'], hhad['d'], hhad['a']] if x and x > 0]
        if len(_valid_hhad) == 3:
            hhad_list = [hhad['h'], hhad['d'], hhad['a']]
            hhad_min_idx = hhad_list.index(min(hhad_list))
        else:
            hhad_list = [ow, od, ol]
            hhad_min_idx = had_min_idx
    else:
        hhad_list = [ow, od, ol]
        hhad_min_idx = had_min_idx
    handicap = hhad.get('goalLine', 0) if hhad else 0
    # Ultra 7.6 (P4): goalLine缺失/为0且HHAD有独立赔率时, 从HAD赔率反推让球档
    # (结果API获取的已完赛比赛goalLine常为0, 导致让球盘口分析失效)
    if not handicap and had and 'h' in had:
        _inferred_gl = infer_goal_line_from_had(had)
        if _inferred_gl != 0:
            handicap = _inferred_gl
            if hhad is not None:
                hhad['goalLine'] = _inferred_gl
                hhad['goalLine_inferred'] = True
            print(f"  [HHAD] goalLine缺失, 从HAD赔率反推让球档: {_inferred_gl:+d} (主胜@{had['h']:.2f})")
    
    # Ultra 3.0: had_dirs和had_min_idx在ensemble_fuse后重新计算, 此处仅暂存初始odds
    if had and 'h' in had:
        odds = round(had_list[had_min_idx], 2)
    elif ouzhi and ouzhi_is_rr:
        odds = round(1 / max(pw5, pd5, pl5), 2)
    else:
        odds = round(had_list[had_min_idx], 2)
    
    # Step 5: 近况修正 — Ultra 3.0: 缓存exponential_decay_form结果(原调用4次→2次)
    # 使用 exponential_decay_form: 近期比赛权重高, 远期低
    # 概率调整 = (加权胜率 - 0.5) * 调节强度, 调节强度0.06
    home_form_cache = exponential_decay_form(home_form, decay_rate=0.15) if home_form else None
    away_form_cache = exponential_decay_form(away_form, decay_rate=0.15) if away_form else None

    form_adj_w = form_adj_l = 0
    if home_form_cache:
        h_wr, h_lr, _ = home_form_cache
        form_adj_w = (h_wr - 0.5) * 0.06
        form_adj_l = (h_lr - 0.5) * 0.06
    if away_form_cache:
        a_wr, a_lr, _ = away_form_cache
        form_adj_l += (a_wr - 0.5) * 0.06
        form_adj_w += (a_lr - 0.5) * 0.06
    
    p0_w += form_adj_w
    p0_d += 0  # 平局不直接调整
    p0_l += form_adj_l
    p0_w, p0_d, p0_l = normalize(p0_w, p0_d, p0_l)
    
    # Step 5a: 全局赔率区间偏差校准 (Ultra 7.0)
    # 基于全量3099场数据分析, 修正不同赔率区间的系统性偏差
    _home_odds_for_cal = had.get('h', 0) if had and 'h' in had else (ow if ow > 0 else 0)
    if _home_odds_for_cal and _home_odds_for_cal > 1:
        _cal = calibrate_global_odds_bias([p0_w, p0_d, p0_l], _home_odds_for_cal)
        p0_w, p0_d, p0_l = _cal[0], _cal[1], _cal[2]
    
    # Step 5b: 赔率变动信号校准 (Ultra 7.0) — 替代原有简单乘法修正
    # 基于初赔→终赔变动方向与幅度, 按历史命中率精细修正
    _init_h_odds = None
    _final_h_odds = None
    if init_ouzhi:
        _init_h_odds = init_ouzhi['avg_initial'][0]
        _final_h_odds = init_ouzhi['avg_instant'][0]
    elif ouzhi and not ouzhi_is_rr:
        _init_h_odds = ouzhi.get('init_w')
        _final_h_odds = ouzhi.get('latest_w')
    if _init_h_odds and _final_h_odds:
        _cal = calibrate_odds_change_signal([p0_w, p0_d, p0_l], _init_h_odds, _final_h_odds,
                                            league=league, home_odds=_home_odds_for_cal)
        p0_w, p0_d, p0_l = _cal[0], _cal[1], _cal[2]
    
    p0_w, p0_d, p0_l = normalize(p0_w, p0_d, p0_l)
    p1_w, p1_d, p1_l = p0_w, p0_d, p0_l
    
    # Step 6: λ值计算 (优先使用球队实际进球数据)
    home_name = sp.get('home', '')
    away_name = sp.get('away', '')
    home_stats = None
    away_stats = None
    for k, v in shuju.items():
        if k.startswith('stats_'):
            name = k.replace('stats_', '')
            if name in home_name or home_name in name:
                home_stats = v
            elif name in away_name or away_name in name:
                away_stats = v
    
    # Bug3修复: 无外部数据时, 从历史库获取球队统计
    if not home_stats or not away_stats:
        _conn = _get_adv_db()
        if _conn:
            _c = _conn.cursor()
            try:
                _md = sp.get('match_date', '9999')
                
                if not home_stats:
                    # Ultra 7.6: 队名别名变体IN查询 (修复队名割裂)
                    _hv = team_name_variants(home_name)
                    _ph = ','.join(['?'] * len(_hv))
                    _c.execute(f'''SELECT avg_gf_overall, avg_ga_overall, games_total, form_wr, form_string
                                  FROM team_rolling_stats
                                  WHERE team_name IN ({_ph}) AND match_date < ? AND avg_gf_overall IS NOT NULL
                                  ORDER BY match_date DESC LIMIT 1''', (*_hv, _md))
                    _row = _c.fetchone()
                    if _row and _row[0] is not None:
                        home_stats = {'avg_gf': _row[0], 'avg_ga': _row[1], 'games': _row[2],
                                      'form_wr': _row[3] or 0.5, 'form_string': _row[4] or ''}
                        print(f"  [历史库] {home_name} 统计: 场均进{_row[0]:.1f}/失{_row[1]:.1f} {_row[2]}场 胜率{_row[3]:.2f}")
                
                if not away_stats:
                    _av = team_name_variants(away_name)
                    _ph = ','.join(['?'] * len(_av))
                    _c.execute(f'''SELECT avg_gf_overall, avg_ga_overall, games_total, form_wr, form_string
                                  FROM team_rolling_stats
                                  WHERE team_name IN ({_ph}) AND match_date < ? AND avg_gf_overall IS NOT NULL
                                  ORDER BY match_date DESC LIMIT 1''', (*_av, _md))
                    _row = _c.fetchone()
                    if _row and _row[0] is not None:
                        away_stats = {'avg_gf': _row[0], 'avg_ga': _row[1], 'games': _row[2],
                                      'form_wr': _row[3] or 0.5, 'form_string': _row[4] or ''}
                        print(f"  [历史库] {away_name} 统计: 场均进{_row[0]:.1f}/失{_row[1]:.1f} {_row[2]}场 胜率{_row[3]:.2f}")
            except Exception as e:
                print(f"  [历史库] 球队统计查询失败: {e}")
    
    # Bug3修复: 同时获取Elo评级 (用于第4源概率)
    _hist_elo_h = None
    _hist_elo_a = None
    _conn = _get_adv_db()
    if _conn:
        _c = _conn.cursor()
        try:
            _md = sp.get('match_date', '9999')
            _hv = team_name_variants(home_name)
            _av = team_name_variants(away_name)
            _phh = ','.join(['?'] * len(_hv))
            _pha = ','.join(['?'] * len(_av))
            _c.execute(f'''SELECT elo_rating FROM team_elo_history
                          WHERE team_name IN ({_phh}) AND match_date < ? AND elo_rating IS NOT NULL
                          ORDER BY match_date DESC LIMIT 1''', (*_hv, _md))
            _row = _c.fetchone()
            if _row:
                _hist_elo_h = _row[0]
            _c.execute(f'''SELECT elo_rating FROM team_elo_history
                          WHERE team_name IN ({_pha}) AND match_date < ? AND elo_rating IS NOT NULL
                          ORDER BY match_date DESC LIMIT 1''', (*_av, _md))
            _row = _c.fetchone()
            if _row:
                _hist_elo_a = _row[0]
            if _hist_elo_h and _hist_elo_a:
                print(f"  [历史库] Elo: {home_name}={_hist_elo_h:.0f} vs {away_name}={_hist_elo_a:.0f}")
        except Exception as e:
            print(f"  [历史库] Elo查询失败: {e}")
    
    # Bug3修复: 无外部近况数据时, 从历史库form_string补充
    if not home_form and home_stats and home_stats.get('form_string'):
        home_form = home_stats['form_string']
        home_form_cache = exponential_decay_form(home_form, decay_rate=0.15)
        print(f"  [历史库] {home_name} 近况: {home_form}")
    if not away_form and away_stats and away_stats.get('form_string'):
        away_form = away_stats['form_string']
        away_form_cache = exponential_decay_form(away_form, decay_rate=0.15)
        print(f"  [历史库] {away_name} 近况: {away_form}")
    
    # Ultra 8.0: 获取 xG/xGA 数据 (替代实际进球, 降低运气噪声)
    _xg_home = fetch_xg_rolling_stats(home_name, sp.get('match_date', '9999'), sp.get('league', ''))
    _xg_away = fetch_xg_rolling_stats(away_name, sp.get('match_date', '9999'), sp.get('league', ''))
    _xg_data = None
    _ppda_stab_factor = 0.0  # Ultra 7.6: PPDA稳定性因子, 供融合权重函数使用
    if _xg_home:
        _ppda_str = f", PPDA={_xg_home.get('avg_ppda', '?')}, 压迫={_xg_home.get('pressure_index', '?')}" if _xg_home.get('avg_ppda') else ""
        print(f"  [xG] {home_name}: xG进{_xg_home['avg_xg_for']}/失{_xg_home['avg_xg_against']} "
              f"(n={_xg_home['n_games']}, 质量={_xg_home['cv_quality']}, 超额={_xg_home['overperformance']:+.2f}{_ppda_str})")
    if _xg_away:
        _ppda_str = f", PPDA={_xg_away.get('avg_ppda', '?')}, 压迫={_xg_away.get('pressure_index', '?')}" if _xg_away.get('avg_ppda') else ""
        print(f"  [xG] {away_name}: xG进{_xg_away['avg_xg_for']}/失{_xg_away['avg_xg_against']} "
              f"(n={_xg_away['n_games']}, 质量={_xg_away['cv_quality']}, 超额={_xg_away['overperformance']:+.2f}{_ppda_str})")
    if _xg_home and _xg_away:
        _xg_data = {
            'home': _xg_home,
            'away': _xg_away,
            'cv_quality_avg': round((_xg_home['cv_quality'] + _xg_away['cv_quality']) / 2, 3),
        }

    if home_stats and away_stats:
        if _xg_home and _xg_away:
            # Ultra 8.0: 使用 xG/xGA 替代实际进球 (降低运气噪声)
            # xG 过滤了折射、乌龙等运气成分, 更稳定地反映球队真实实力
            lam_h = (_xg_home['avg_xg_for'] + _xg_away['avg_xg_against']) / 2
            lam_a = (_xg_away['avg_xg_for'] + _xg_home['avg_xg_against']) / 2
            # 贝叶斯收缩 — 用实际样本量而非硬编码10
            league = sp.get('league', '')
            LEAGUE_AVG_GF = LEAGUE_AVG_GF_MAP.get(league, 1.3)
            lam_h = bayesian_shrinkage(lam_h, _xg_home['n_games'], LEAGUE_AVG_GF, k=10)
            lam_a = bayesian_shrinkage(lam_a, _xg_away['n_games'], LEAGUE_AVG_GF, k=10)
            # xG超额修正: 超额表现(实际>xG)不可持续, 适度回调
            # Ultra 7.7: 超额>+0.5 视为强回归信号, 加大回调力度
            if _xg_home['overperformance'] > 0.3:
                _adj = min(0.10, _xg_home['overperformance'] * 0.05)
                # 超额>+0.5: 回归风险高, 额外增加5%回调
                if _xg_home['overperformance'] > 0.5:
                    _adj += 0.05
                lam_h *= (1 - _adj)
            if _xg_away['overperformance'] > 0.3:
                _adj = min(0.10, _xg_away['overperformance'] * 0.05)
                # 超额>+0.5: 回归风险高, 额外增加5%回调
                if _xg_away['overperformance'] > 0.5:
                    _adj += 0.05
                lam_a *= (1 - _adj)

            # Ultra 9.0: PPDA压迫强度修正
            # 数据分析: 高压球队(PPDA<7)场均xG=1.90, 低压球队(PPDA>14)场均xG=1.47
            # 压迫强度差越大, 强队xG提升越多, 弱队xG下降
            # 调整公式: adj = ppda_diff * sensitivity * stability_factor
            #   - ppda_diff > 0: 主队压迫更强 → 主队xG↑, 客队xG↓
            #   - 有界调整 ±8%, 避免过度修正
            _ppda_h = _xg_home.get('avg_ppda')
            _ppda_a = _xg_away.get('avg_ppda')
            _ppda_stab_h = _xg_home.get('ppda_stability', 0.5)
            _ppda_stab_a = _xg_away.get('ppda_stability', 0.5)
            if _ppda_h is not None and _ppda_a is not None:
                # PPDA差: 正值=主队压迫更强(PPDA更低)
                _ppda_diff = _ppda_a - _ppda_h
                # 灵敏度系数: 每单位PPDA差调整0.4% (经验值)
                _ppda_sensitivity = 0.004
                # 稳定性因子: 两队PPDA稳定性的几何平均 (0.3~1.0)
                _stab_factor = (_ppda_stab_h * _ppda_stab_a) ** 0.5
                _stab_factor = max(0.3, min(1.0, _stab_factor))
                _ppda_stab_factor = _stab_factor  # Ultra 7.6: 供融合权重使用
                # 计算调整比例 (有界 ±8%)
                _ppda_adj = _ppda_diff * _ppda_sensitivity * _stab_factor
                _ppda_adj = max(-0.08, min(0.08, _ppda_adj))
                if abs(_ppda_adj) > 0.005:
                    lam_h *= (1 + _ppda_adj)
                    lam_a *= (1 - _ppda_adj)
                    _p_label = "高压" if _ppda_diff > 0 else "低压"
                    print(f"  [PPDA] 压迫修正: {_p_label} 主队PPDA={_ppda_h:.1f} vs 客队PPDA={_ppda_a:.1f} "
                          f"→ λ_h{'×' if _ppda_adj > 0 else '÷'}{1+abs(_ppda_adj):.3f} "
                          f"λ_a{'×' if _ppda_adj < 0 else '÷'}{1+abs(_ppda_adj):.3f} "
                          f"(稳定度={_stab_factor:.2f})")
        else:
            # 回退: 基于实际进球数据
            lam_h = (home_stats['avg_gf'] + away_stats['avg_ga']) / 2
            lam_a = (away_stats['avg_gf'] + home_stats['avg_ga']) / 2
            league = sp.get('league', '')
            LEAGUE_AVG_GF = LEAGUE_AVG_GF_MAP.get(league, 1.3)
            lam_h = bayesian_shrinkage(lam_h, 10, LEAGUE_AVG_GF, k=10)
            lam_a = bayesian_shrinkage(lam_a, 10, LEAGUE_AVG_GF, k=10)
        # Ultra 1.0: 主场优势用乘除法
        league = sp.get('league', '')
        home_adv = LEAGUE_HOME_ADV.get(league, 1.15)
        lam_h *= home_adv
        lam_a /= home_adv
    else:
        # 降级: 从赔率隐含概率推算，总进球基数由市场盘口决定
        total_goals_base = LEAGUE_AVG_GOALS_MAP.get(sp.get('league', ''), market_goal_line)
        if had_min_idx == 0:
            lam_h = total_goals_base * p1_w / (p1_w + p1_l) * 1.1
            lam_a = total_goals_base * p1_l / (p1_w + p1_l) * 0.9
        elif had_min_idx == 2:
            lam_h = total_goals_base * p1_w / (p1_w + p1_l) * 0.9
            lam_a = total_goals_base * p1_l / (p1_w + p1_l) * 1.1
        else:
            lam_h = total_goals_base * 0.45
            lam_a = total_goals_base * 0.45
    
    # 近况修正 — Ultra 3.0: 复用缓存的form结果
    if home_form_cache:
        h_wr = home_form_cache[0]
        lam_h *= (0.90 + h_wr * 0.25)
    if away_form_cache:
        a_wr = away_form_cache[0]
        lam_a *= (0.90 + a_wr * 0.25)
    
    # Ultra 6.4: 市场大小球盘口校准总λ (修复总进球系统性偏低)
    # 贝叶斯收缩(k=10→向1.3收缩50%)系统性压低总进球;
    # 市场O/U盘口线是总进球期望的直接定价信号, 此前仅用于大小标签未参与λ。
    # 混合: target = 65%模型 + 35%市场, 等比缩放进而保持主客强弱比例不变
    lam_market_calibrated = False
    if market_goal_line and 1.5 <= market_goal_line <= 4.0:
        lam_total_model = lam_h + lam_a
        # O/U盘口≈总进球中位数, 期望≈中位数+0.1 (右偏分布)
        lam_total_market = market_goal_line + 0.1
        lam_total_target = 0.65 * lam_total_model + 0.35 * lam_total_market
        if lam_total_model > 0.3:
            scale = lam_total_target / lam_total_model
            scale = max(0.80, min(1.25, scale))  # 有界缩放, 防止极端
            if abs(scale - 1.0) > 0.02:  # 偏差>2%才调整
                lam_h *= scale
                lam_a *= scale
    
    # ===== Ultra 6.11: 五大场景修正 (2026-07-28) =====
    # 在市场盘口校准后、compute_scores前施加, 修正系统性盲区
    v611_notes = []
    v611_flags = {}

    # --- 修正1: 近况滑坡 (主队近3场LLL → 下调进攻λ) ---
    h_slump, h_slump_sev = detect_form_slump(home_form, n_recent=3)
    if h_slump:
        # 实证标定(backtest_v611_calibration.py 五大联赛5256场):
        # 3L→×0.87(n=517), 2L/3→×0.92(n=2146), DDD→×0.93(n=147)
        h_slump_cut = 1.0 - min(0.13, h_slump_sev * 0.13)
        lam_h *= h_slump_cut
        v611_notes.append(f"主队近况滑坡(severity={h_slump_sev:.1f}), λ_h×{h_slump_cut:.2f}")
        v611_flags['home_slump'] = True

    # --- 修正1b: 客队近况滑坡也影响客队进攻 ---
    a_slump, a_slump_sev = detect_form_slump(away_form, n_recent=3)
    if a_slump:
        a_slump_cut = 1.0 - min(0.13, a_slump_sev * 0.13)
        lam_a *= a_slump_cut
        v611_notes.append(f"客队近况滑坡(severity={a_slump_sev:.1f}), λ_a×{a_slump_cut:.2f}")
        v611_flags['away_slump'] = True

    # --- 修正2: 交锋压制因子 (主队h2h胜率<35% → λ_h衰减, 克星方λ_a加成) ---
    h2h_str = shuju.get('h2h', '')
    h2h_info = parse_h2h_record(h2h_str)
    if h2h_info and h2h_info['total'] >= 5:
        if h2h_info['home_win_rate'] < 0.35:
            # 实证标定(n=429): 被压制方进球×0.95, 克星方进球×1.07(反向!)
            # 旧逻辑双方皆罚(λ_h×0.85/λ_a×0.90)与实证矛盾
            lam_h *= 0.95
            lam_a *= 1.05  # 克星方克制加成
            v611_notes.append(f"交锋压制(主胜率{h2h_info['home_win_rate']:.0%}, n={h2h_info['total']}), λ_h×0.95 λ_a×1.05")
            v611_flags['h2h_suppression'] = True

    # --- 修正5: 防守型客队 (近4场3W+ + 场均失球<1.0 → 主队λ衰减, 客队λ加成) ---
    is_def_away, def_factor = detect_defensive_away(away_form, away_stats)
    if is_def_away:
        # 实证标定(backtest_v611_calibration.py): 主队进球被压制×0.83/0.85(验证准确),
        # 客队自身进球反增×1.16-1.18(连胜状态), 旧逻辑双方同罚方向相反, 保守取×1.10
        lam_h *= def_factor
        lam_a *= 1.10
        v611_notes.append(f"客队防守回升(近况{away_form[-4:]}, 场均失{away_stats.get('avg_ga',0):.1f}), λ_h×{def_factor:.2f} λ_a×1.10")
        v611_flags['defensive_away'] = True

    # --- 修正3: 跨盘口矛盾 — 实证否决(backtest_v611b, 4222场) ---
    # "大球升盘+平赔降=诱大"不成立: 触发组实际/期望=1.011(不低于对照), 原λ×0.85惩罚移除
    # 保留检测作为信息字段, 不调整λ
    _initial_summary = _build_initial_summary(init_ouzhi, init_yazhi, init_daxiao)
    is_trap, trap_factor = detect_cross_market_trap(_initial_summary)
    if is_trap:
        v611_notes.append(f"跨盘口信号(O/U升盘+平赔降, 实证无诱大效应, 仅记录)")
        v611_flags['ou_trap'] = True

    # --- 升级5: DC攻防强度λ 与市场λ混合 ---
    # 市场λ来自赔率反演(含市场情绪), DCλ来自939场历史赛果的攻防强度IPF拟合;
    # 两者独立信息源按 65/35 混合, DCλ对"市场未定价的攻防失配"有增量信息。
    # 球队不在DC库(新军/数据不足)时 dc_lambda 返回 None → 自动回退纯市场λ。
    if _MU and UPGRADES.get('dc_lambda') and _UPG_PARAMS.get('dc_model'):
        try:
            _dc_lams = _MU.dc_lambda(_UPG_PARAMS['dc_model'], home_name, away_name)
            if _dc_lams:
                _lam_h_mkt, _lam_a_mkt = lam_h, lam_a
                lam_h = (1 - DC_LAMBDA_BLEND) * lam_h + DC_LAMBDA_BLEND * _dc_lams[0]
                lam_a = (1 - DC_LAMBDA_BLEND) * lam_a + DC_LAMBDA_BLEND * _dc_lams[1]
                v611_notes.append(
                    f"DCλ混合(市场{_lam_h_mkt:.2f}/{_lam_a_mkt:.2f} + "
                    f"DC{_dc_lams[0]:.2f}/{_dc_lams[1]:.2f} → {lam_h:.2f}/{lam_a:.2f})")
                v611_flags['dc_lambda_blend'] = True
        except Exception:
            pass

    lam_h = max(0.3, min(lam_h, 4.0))
    lam_a = max(0.3, min(lam_a, 4.0))

    scores = compute_scores(lam_h, lam_a, goal_line=handicap, market_goal_line=market_goal_line)

    # --- 修正4: 0-0赔率校准 (模型0-0概率<市场隐含50% → 上调低进球区间) ---
    # 方向经实证支持(backtest_v611b 5A: 市场0-0赔率校准良好, 偏差±0.02内);
    # 调整幅度(max 0.5)未经实证, 分歧场景历史样本不足, 保持原值待验证
    # 需要在scores计算后进行, 修正比分概率分布
    _sporttery_crs = {}
    _sb = sp.get('sporttery_bonus') or {}
    if isinstance(_sb, dict) and 'crs' in _sb:
        _sporttery_crs = _sb['crs']
    _scores_probs = scores.get('all_probs', {})
    _00_mispriced, _00_adj, _model_00, _market_00 = detect_zero_zero_mispricing(_scores_probs, _sporttery_crs)
    if _00_mispriced:
        # 上调0-0和1-0/0-1等低进球比分概率
        low_scores = ['0-0', '0-1', '1-0', '1-1']
        for s_key in low_scores:
            if s_key in _scores_probs and _scores_probs[s_key] > 0:
                _scores_probs[s_key] *= (1.0 + _00_adj)
        # 重新归一化
        _total_p = sum(_scores_probs.values())
        if _total_p > 0:
            for k in _scores_probs:
                _scores_probs[k] /= _total_p
            # 更新scores中的相关字段
            scores['all_probs'] = _scores_probs
            # 更新WDL概率 (round 替代 int, 保持与 compute_scores 一致)
            w_new = sum(p for s, p in _scores_probs.items() if int(s[0]) > int(s[2]))
            d_new = sum(p for s, p in _scores_probs.items() if int(s[0]) == int(s[2]))
            l_new = sum(p for s, p in _scores_probs.items() if int(s[0]) < int(s[2]))
            scores['poisson_wdl'] = [round(w_new * 100, 1), round(d_new * 100, 1), round(l_new * 100, 1)]
            # 更新top3比分
            _sorted = sorted(_scores_probs.items(), key=lambda x: x[1], reverse=True)
            scores['top3'] = ' '.join(f'{s}:{p*100:.1f}' for s, p in _sorted[:3])
            # M1修复: 同步更新全部派生字段, 避免下游(CRS EV/比分推荐/大小球)读到修正前数据
            def _tg(s_key):
                return int(s_key.split('-')[0]) + int(s_key.split('-')[1])

            def _over_prob_for(gl):
                int_part = int(gl)
                frac = round(gl - int_part, 2)
                if frac in (0.5, 0.0):
                    return sum(p for s, p in _sorted if _tg(s) >= int_part + 1)
                elif frac == 0.25:
                    return (sum(p for s, p in _sorted if _tg(s) >= int_part + 1)
                            + sum(p for s, p in _sorted if _tg(s) == int_part) * 0.5)
                elif frac == 0.75:
                    return (sum(p for s, p in _sorted if _tg(s) >= int_part + 2)
                            + sum(p for s, p in _sorted if _tg(s) == int_part + 1) * 0.5)
                return sum(p for s, p in _sorted if _tg(s) >= int_part + 1)

            _gl_main = scores.get('market_gl', market_goal_line)
            scores['top5_raw'] = [[s, round(p * 100, 1)] for s, p in _sorted[:5]]
            scores['over_main'] = round(_over_prob_for(_gl_main) * 100, 1)
            scores['over_low'] = round(_over_prob_for(round(_gl_main - 0.5, 2)) * 100, 1)
            scores['over_ml'] = round(_over_prob_for(round(_gl_main - 0.25, 2)) * 100, 1)
            scores['over_mh'] = round(_over_prob_for(round(_gl_main + 0.25, 2)) * 100, 1)
            scores['over_high'] = round(_over_prob_for(round(_gl_main + 0.5, 2)) * 100, 1)
            # 主/副盘口方向过滤
            def _threshold(gl):
                thr = int(math.ceil(gl))
                return int(gl) + 1 if gl == int(gl) else thr
            _thr_main = _threshold(_gl_main)
            _thr_high = _threshold(round(_gl_main + 0.5, 2))
            _big_m = [(s, p) for s, p in _sorted if _tg(s) >= _thr_main]
            _big_h = [(s, p) for s, p in _sorted if _tg(s) >= _thr_high]
            scores['main_dir'] = '大' if _over_prob_for(_gl_main) > 0.5 else '小'
            scores['high_dir'] = '大' if _over_prob_for(round(_gl_main + 0.5, 2)) > 0.5 else '小'
            scores['top3_filtered'] = [[s, round(p * 100, 1)] for s, p in
                                       (_big_m if scores['main_dir'] == '大' else
                                        [(s, p) for s, p in _sorted if _tg(s) < _thr_main])[:3]]
            scores['high_top3'] = [[s, round(p * 100, 1)] for s, p in
                                   (_big_h if scores['high_dir'] == '大' else
                                    [(s, p) for s, p in _sorted if _tg(s) < _thr_high])[:3]]
            # ===== Ultra 12.3: 比分top3平局方向不偏废 (0-0修正后同步) =====
            _draw_prob_00 = d_new  # 0-0修正后的Poisson平局概率
            _has_draw_00 = any(s[0] == s[2] and s != '0-0' for s, _ in scores['top3_filtered'][:3])
            if not _has_draw_00 and _draw_prob_00 >= 0.25:
                _best_draw_00 = None
                for s, p in _sorted:
                    if s[0] == s[2] and s != '0-0':
                        _best_draw_00 = (s, round(p * 100, 1))
                        break
                if _best_draw_00:
                    _nd_00 = [(i, s, p) for i, (s, p) in enumerate(scores['top3_filtered'][:3])
                              if s[0] != s[2]]
                    if _nd_00:
                        _mi_00 = min(_nd_00, key=lambda x: x[2])[0]
                        scores['top3_filtered'][_mi_00] = list(_best_draw_00)
                    _has_draw_h00 = any(s[0] == s[2] and s != '0-0' for s, _ in scores['high_top3'][:3])
                    if not _has_draw_h00:
                        _nd_h00 = [(i, s, p) for i, (s, p) in enumerate(scores['high_top3'][:3])
                                   if s[0] != s[2]]
                        if _nd_h00:
                            _mi_h00 = min(_nd_h00, key=lambda x: x[2])[0]
                            scores['high_top3'][_mi_h00] = list(_best_draw_00)
        v611_notes.append(f"0-0低估修正(模型{_model_00:.1%}→市场{_market_00:.1%}), 低进球区间+{_00_adj:.0%}")
        v611_flags['zero_zero_fix'] = True
    
    # ===== Ultra 5.0: 四源概率融合 + 自适应校准 =====
    lam_total = lam_h + lam_a

    # Ultra 6.4: 平局校准先验参数 (联赛平局率 + 平赔信号)
    _league_for_cal = sp.get('league', '')
    _draw_odds_for_cal = had.get('d') if had and 'd' in had else (od if od > 1.5 else None)

    # 1. 负二项模型概率 → 自适应Logit校准(修正平局低估)
    poisson_wdl_raw = [v/100 for v in scores['poisson_wdl']]
    poisson_calibrated = calibrate_probabilities(poisson_wdl_raw, source='poisson', lam_total=lam_total, lam_h=lam_h, lam_a=lam_a,
                                                 league=_league_for_cal, draw_odds=_draw_odds_for_cal)
    
    # 2. 市场隐含概率 (已经过form/odds修正的p1_w/d/l)
    market_probs = [p1_w, p1_d, p1_l]
    # Ultra 6.3: 市场源也做平局校准 (赔率本身压平局)
    market_probs = calibrate_probabilities(market_probs, source='market', lam_total=lam_total, lam_h=lam_h, lam_a=lam_a,
                                           league=_league_for_cal, draw_odds=_draw_odds_for_cal)
    
    # 3. Power方法概率 (从原始赔率提取, 互补Shin方法)
    if had and 'h' in had:
        power_probs = power_method([had['h'], had['d'], had['a']])
    elif ow > 0:
        power_probs = power_method([ow, od, ol])
    else:
        power_probs = market_probs  # 无赔率时回退

    # 4. Ultra 5.0: Elo评级概率 (第4源, 基于球队统计+近况)
    h_wr_for_elo = home_form_cache[0] if home_form_cache else 0.5
    a_wr_for_elo = away_form_cache[0] if away_form_cache else 0.5
    league = sp.get('league', '')
    elo_hfa = int((LEAGUE_HOME_ADV.get(league, 1.15) - 1.0) * 400)  # 主场优势→Elo点数
    elo_probs = elo_probabilities(home_stats, away_stats, h_wr_for_elo, a_wr_for_elo, league_home_adv=elo_hfa,
                                  hist_elo_h=_hist_elo_h, hist_elo_a=_hist_elo_a)

    # 5. 集成融合: 市场 + Power + 校准Poisson + Elo (四源)
    # Ultra 7.6: 公共权重函数 — dq渐变化 + Power悖论修复 + Elo分级 + proxy降权
    dq = assess_data_quality(sp)
    _xg_is_proxy = bool((_xg_home and _xg_home.get('is_proxy')) or
                        (_xg_away and _xg_away.get('is_proxy')))
    fuse_weights = compute_fuse_weights(
        dq['score'], market_probs=market_probs, power_probs=power_probs,
        hist_elo=(_hist_elo_h is not None and _hist_elo_a is not None),
        xg_proxy=_xg_is_proxy, ppda_stab=_ppda_stab_factor)
    # 升级7: 历史Brier学习权重 (已训参数存在时覆盖启发式权重; 缺参=回退)
    if _MU and UPGRADES.get('learned_fusion') and _UPG_PARAMS.get('fusion_weights'):
        _lw = _UPG_PARAMS['fusion_weights'].get('weights')
        if _lw and len(_lw) == 4 and abs(sum(_lw) - 1.0) < 0.05:
            fuse_weights = _lw
    if _xg_is_proxy:
        print(f"  [融合] ⚠️ xG为proxy占位符(非真实Understat), Poisson权重降权, 置信度封顶★★★★")
    fused_probs, model_agreement = ensemble_fuse([market_probs, power_probs, poisson_calibrated, elo_probs], weights=fuse_weights)
    # Ultra 7.6 (P10落地): JS散度一致性 — 连续分布相似度, 作为信息字段输出
    # (回测结论: 不改变融合权重触发逻辑, 仅提供更细粒度的一致性度量)
    js_agreement = compute_js_agreement([market_probs, power_probs, poisson_calibrated, elo_probs])
    p1_w, p1_d, p1_l = fused_probs  # 用融合概率替代原始概率

    # 升级2: 赔率→概率 isotonic 校准后处理 (n=788历史库训练)
    # 隐含概率与真实频率存在系统性偏差(热门低估/冷门高估),
    # PAV保序回归逐类校准后重新归一化, 修正融合输出的系统性偏移。
    if _MU and UPGRADES.get('odds_calibration') and _UPG_PARAMS.get('odds_calibrator'):
        try:
            p1_w, p1_d, p1_l = _MU.apply_odds_calibrator(
                [p1_w, p1_d, p1_l], _UPG_PARAMS['odds_calibrator'])
        except Exception:
            pass
    
    # Ultra 6.7: 高级标定 (6大模块) — 在四源融合后施加有界修正
    _adv_probs, _adv_notes = apply_advanced_calibration([p1_w, p1_d, p1_l], sp, had, hhad)
    p1_w, p1_d, p1_l = _adv_probs

    # Ultra 10.6 → 11.0: 赔率变动特征分析校准 — 平局赔率变动 + HAD-HHAD联动 + 初终赔变动方向
    # 注: 平赔变动信号需odds_change_history数据, 预测时无逐场数据则跳过
    _oca_notes = []
    if _ODDS_CHANGE_ANALYSIS_CALIB and had and 'h' in had:
        # Ultra 11.0: 获取体彩初赔数据 (用于初赔→终赔变动方向校准)
        _had_init = sp.get('sporttery_bonus', {}).get('had_init')
        _hhad_init = sp.get('sporttery_bonus', {}).get('hhad_init')
        _oca_probs, _oca_notes, _ = apply_odds_change_analysis_calibration(
            [p1_w, p1_d, p1_l], had, hhad, _league_for_cal, 
            odds_change=None, had_hhad_change=None, had_init=_had_init, hhad_init=_hhad_init)
        p1_w, p1_d, p1_l = _oca_probs
    if _oca_notes:
        _adv_notes.extend(_oca_notes)

    # Ultra 6.9: 融合后平局校准 — 双信号(主赔+平赔)修复系统性平局低估
    # 必须在advanced_calibration之后、HAD方向确定之前调用
    p1_w, p1_d, p1_l = post_fusion_draw_calibration([p1_w, p1_d, p1_l], had, _league_for_cal)

    # Ultra 8.0: HAD主场偏差修正 (29场回归: 76%预测"胜"但命中率仅41%)
    # 1. 主场概率上限65% (防止主场优势权重过大)
    # 2. 客队赔率<3.0时(非弱旅), 对"胜"方向-3pp
    away_odds_val = had.get('a', 0) if had else 0
    if p1_w > 0.65:
        excess = p1_w - 0.65
        p1_w = 0.65
        p1_d += excess * 0.5
        p1_l += excess * 0.5
    if away_odds_val and 1 < away_odds_val < 3.0 and p1_w > 0.40:
        p1_w -= 0.03
        p1_d += 0.015
        p1_l += 0.015

    fused_probs = [p1_w, p1_d, p1_l]
    
    # 4. 重新确定HAD方向 (基于融合概率)
    had_dirs = ['胜', '平', '负']
    had_min_idx = fused_probs.index(max(fused_probs))

    # ===== Ultra 12.2: 平局方向覆盖 (HAD argmax结构性盲区修复) =====
    # 根因: HAD方向=argmax([p_w, p_d, p_l]), 平局概率即使校准到27-32%也永远排不进前二
    # 实证: 4484场历史中平局隐含概率成为HAD三选项最高的场次仅0.2%
    # 回归(83场): 预测胜/负但实际平局23%误判率, "平"方向F1=0.00
    # 策略: 平局概率≥30%且top2差≤8pp(势均力敌)时, 强制覆盖HAD方向为"平"
    # 历史实证: 平局隐含30-34%时实际平局率36.6%(+12pp显著优于市场)
    _draw_override = False
    _draw_override_reason = ""
    if had_min_idx != 1:  # 平局不是当前argmax
        _home_odds = had.get('h', 0) if had else 0
        _draw_prob = p1_d
        _top_prob = fused_probs[had_min_idx]
        _top2_gap = _top_prob - _draw_prob
        # 触发条件: 平局概率≥30% (强信号) 且 (top2差≤8pp 或 平局概率≥32%)
        _draw_strong = _draw_prob >= 0.30
        _draw_very_strong = _draw_prob >= 0.32
        _gap_small = _top2_gap <= 0.08
        # 排除极端热门(主赔<1.50, 强队碾压局平局概率虚高来自校准)
        _not_extreme_fav = _home_odds >= 1.50 or _home_odds == 0
        if _draw_strong and (_gap_small or _draw_very_strong) and _not_extreme_fav:
            had_min_idx = 1  # 覆盖为平局
            _draw_override = True
            _draw_override_reason = (
                f"平局覆盖: P平={_draw_prob:.0%}≥30% top2差={_top2_gap:.0%}≤8pp "
                f"主赔={_home_odds:.2f} (HAD argmax结构性盲区修复)"
            )
            v611_notes.append(_draw_override_reason)
            v611_flags['draw_override'] = True

    # ===== Ultra 6.0: λ-赔率方向冲突校准 =====
    # 当λ统计模型的主客强弱方向与四源融合概率方向矛盾时
    # (典型场景: 主场优势×1.15反转了客队更强的原始数据),
    # 用融合概率重新分配λ比例(保持总进球量不变),
    # 避免扭曲半全场/总进球/比分预测。
    lam_dir_home_strong = lam_h > lam_a
    fused_dir_home_strong = p1_w > p1_l
    lam_conflict = lam_dir_home_strong != fused_dir_home_strong

    lam_recalibrated = False
    lam_h_orig, lam_a_orig = round(lam_h, 2), round(lam_a, 2)
    lam_calib_note = ""

    if lam_conflict:
        # 方向冲突: 用融合概率重新分配λ
        # 分配公式: 主队份额 = P(胜)+0.5×P(平), 客队份额 = P(负)+0.5×P(平)
        # 平局概率均分给两队, 保持总进球量不变
        total_lam = lam_h + lam_a
        home_share = p1_w + 0.5 * p1_d
        away_share = p1_l + 0.5 * p1_d

        if home_share > 0.01 and away_share > 0.01:
            lam_h_new = total_lam * home_share
            lam_a_new = total_lam * away_share
            lam_h = max(0.3, min(lam_h_new, 4.0))
            lam_a = max(0.3, min(lam_a_new, 4.0))
            lam_recalibrated = True
            lam_calib_note = f"λ校准: {lam_h_orig}/{lam_a_orig}→{round(lam_h,2)}/{round(lam_a,2)}(方向冲突,按融合概率重分配)"
            # 重新计算scores (比分/HHAD/一致性检查将使用校准后λ)
            scores = compute_scores(lam_h, lam_a, goal_line=handicap, market_goal_line=market_goal_line)

    # ===== Ultra 7.7: 杯赛首回合大比分惩罚 (仅限欧冠/欧罗巴/欧协联等两回合制杯赛) =====
    # 当首回合分差≥3球时, 落后方λ提升(背水一战强攻), 领先方λ下调(保守轮换)
    # 置信度封顶★★★★, 联赛不适用, 仅对有主客场制的杯赛生效
    cup_leg_penalty_info = None
    try:
        cup_leg_penalty_info = get_cup_leg_penalty(match_num, league, home_name, away_name)
        if cup_leg_penalty_info and cup_leg_penalty_info.get('applied'):
            factor = cup_leg_penalty_info['lambda_factor']
            leader_factor = cup_leg_penalty_info.get('leader_factor', 1.0)
            side = cup_leg_penalty_info['trailing_side']
            # Ultra 7.7: 落后方背水一战加成 + 领先方保守修正
            if side == 'home':
                lam_h *= factor
                lam_a *= leader_factor
            else:
                lam_a *= factor
                lam_h *= leader_factor
            lam_h = max(0.3, min(lam_h, 4.0))
            lam_a = max(0.3, min(lam_a, 4.0))
            # 重新计算scores (惩罚改变了λ, 所有下游计算需更新)
            scores = compute_scores(lam_h, lam_a, goal_line=handicap, market_goal_line=market_goal_line)
            v611_notes.append(f"杯赛首回合惩罚: {cup_leg_penalty_info['note']}")
            v611_flags['cup_leg_penalty'] = True
    except Exception as _e:
        pass  # 惩罚模块失败不影响主预测流程

    # ===== 半全场胜平负 (体彩第五种玩法) =====
    # Ultra 9.3: 联赛特定半场比例 + 联赛特定r + 8×8矩阵 + HTL效应
    half_full = compute_half_full(lam_h, lam_a, fused_wdl=[p1_w, p1_d, p1_l],
                                  league=sp.get('league', ''))

    # ===== 总进球数 (体彩第三种玩法) =====
    # Ultra 9.3: 10×10矩阵聚合 + 自适应TTG融合权重 + 7+残差校正
    _ttg_odds = sp.get('sporttery_bonus', {}).get('ttg') if sp.get('sporttery_bonus') else None
    _xg_quality = _xg_data['cv_quality_avg'] if _xg_data else None
    total_goals_pred = compute_total_goals(lam_h, lam_a, ttg_odds=_ttg_odds,
                                           league=sp.get('league', ''),
                                           xg_cv_quality=_xg_quality)
    
    # ===== 进球预期分析 (goals字段) =====
    total_expected = round(lam_h + lam_a, 1)
    over_under = '大' if (lam_h + lam_a) > market_goal_line else '小'
    if lam_h > lam_a * 1.3:
        if _xg_home:
            attack = f"主队进攻占优(xG {_xg_home['avg_xg_for']:.1f})"
        elif home_stats:
            attack = f"主队进攻占优(场均{home_stats.get('avg_gf',0):.1f}球)"
        else:
            attack = "主队预期占优"
        key_insight = f"{attack}, 预期主{round(lam_h,1)}:客{round(lam_a,1)}, 总{total_expected}球偏{over_under}"
    elif lam_a > lam_h * 1.3:
        if _xg_away:
            attack = f"客队进攻占优(xG {_xg_away['avg_xg_for']:.1f})"
        elif away_stats:
            attack = f"客队进攻占优(场均{away_stats.get('avg_gf',0):.1f}球)"
        else:
            attack = "客队预期占优"
        key_insight = f"{attack}, 预期主{round(lam_h,1)}:客{round(lam_a,1)}, 总{total_expected}球偏{over_under}"
    else:
        key_insight = f"双方预期进球接近(主{round(lam_h,1)}:客{round(lam_a,1)}), 总{total_expected}球偏{over_under}"
    goals = {
        'home_expected': round(lam_h, 1),
        'away_expected': round(lam_a, 1),
        'home_recent': f"{home_stats.get('avg_gf',0):.1f}/{home_stats.get('avg_ga',0):.1f}" if home_stats else "N/A",
        'away_recent': f"{away_stats.get('avg_gf',0):.1f}/{away_stats.get('avg_ga',0):.1f}" if away_stats else "N/A",
        'total_expected': total_expected,
        'over_under': over_under,
        'key_insight': key_insight,
        # Ultra 8.0: xG 数据
        'home_xg': f"{_xg_home['avg_xg_for']:.1f}/{_xg_home['avg_xg_against']:.1f}" if _xg_home else None,
        'away_xg': f"{_xg_away['avg_xg_for']:.1f}/{_xg_away['avg_xg_against']:.1f}" if _xg_away else None,
        'xg_overperformance': f"主{_xg_home['overperformance']:+.2f}/客{_xg_away['overperformance']:+.2f}" if _xg_home and _xg_away else None,
        'xg_cv_quality': _xg_data['cv_quality_avg'] if _xg_data else None,
        'using_xg': bool(_xg_home and _xg_away),
    }
    
    # ===== 三条预测: HAD + HHAD + 比分 =====
    # --- HAD预测 (Ultra 3.0: 基于融合概率) ---
    # Ultra 7.10: HAD未开盘时不生成推荐 (体彩停售/未开HAD盘时禁止推荐)
    had_open = bool(had and had.get('h', 0) > 0)
    had_probs = [p1_w, p1_d, p1_l]
    if had_open:
        had_dir = had_dirs[had_min_idx]
        odds = round([had['h'], had['d'], had['a']][had_min_idx], 2)
    else:
        had_dir = '未开盘'
        odds = None
    
    # --- HHAD预测 (基于Poisson让球概率) ---
    hhad_dirs = ['让胜', '让平', '让负']
    hhad_w, hhad_d, hhad_l = [v/100 for v in scores['hhad_wdl']]
    hhad_probs_poisson = [hhad_w, hhad_d, hhad_l]
    hhad_poisson_idx = hhad_probs_poisson.index(max(hhad_probs_poisson))
    
    # 如果HHAD有开盘，用赔率隐含概率验证；否则用Poisson概率
    if hhad and 'h' in hhad:
        hhad_odds_list = [hhad['h'], hhad['d'], hhad['a']]
        # Ultra 5.0: Shin + Power + 校准Poisson + Elo 四源融合
        hhad_shin = shin_method(hhad_odds_list)
        hhad_power = power_method(hhad_odds_list)
        hhad_poisson_cal = calibrate_probabilities(hhad_probs_poisson, source='poisson', lam_total=lam_total, lam_h=lam_h, lam_a=lam_a,
                                                   league=_league_for_cal, draw_odds=_draw_odds_for_cal)
        # Elo概率不得复用: elo_probs 是 HAD 事件空间(胜/平/负), 无让球信息,
        # 直接当作[让胜/让平/让负]融合属量纲错误 (P0修复: 让-1时 P(让胜)=P(净胜2+)≠P(胜),
        # 实证压低让负约6pp)。HHAD 用 市场Shin + Power + 校准Poisson 三源融合。
        # Ultra 7.6 (P5): 保留数据质量自适应权重
        _hhad_weights = compute_fuse_weights(
            dq['score'], market_probs=hhad_shin, power_probs=hhad_power,
            hist_elo=(_hist_elo_h is not None and _hist_elo_a is not None),
            xg_proxy=_xg_is_proxy, ppda_stab=_ppda_stab_factor)
        hhad_final_probs, hhad_agreement = ensemble_fuse(
            [hhad_shin, hhad_power, hhad_poisson_cal], weights=_hhad_weights[:3])
        hhad_final_idx = hhad_final_probs.index(max(hhad_final_probs))
        hhad_dir = hhad_dirs[hhad_final_idx]
        hhad_odds_val = round(hhad_odds_list[hhad_final_idx], 2)
    else:
        hhad_dir = hhad_dirs[hhad_poisson_idx]
        hhad_odds_val = 0
        hhad_final_probs = hhad_probs_poisson
        hhad_final_idx = hhad_poisson_idx

    # Ultra 6.10: 融合后让球平局校准 — 修复-1球盘口让平率系统性低估
    # 在HHAD融合后、方向确定前调用 (与HAD平局校准对称)
    hhad_final_probs = post_fusion_hhad_draw_calibration(
        hhad_final_probs, had, hhad, handicap, _league_for_cal)

    # Ultra 10.6 → 11.0: HAD/HHAD矛盾信号 → HHAD可靠性调整 + 初终赔变动方向
    # 当HAD自信度>5pp高于HHAD时, HAD准确率56.4% vs HHAD仅33.5%
    # 此时降低HHAD置信度, 让HAD预测主导
    _oca_hhad_notes = []
    if _ODDS_CHANGE_ANALYSIS_CALIB and hhad and 'h' in hhad and had and 'h' in had:
        try:
            _sp_had_shin = shin_method([had['h'], had['d'], had['a']])
            _sp_hhad_shin = shin_method([hhad['h'], hhad['d'], hhad['a']])
            _had_init = sp.get('sporttery_bonus', {}).get('had_init')
            _hhad_init = sp.get('sporttery_bonus', {}).get('hhad_init')
            _oca_hhad, _oca_hhad_notes, _hhad_rel = apply_odds_change_analysis_calibration(
                hhad_final_probs, had, hhad, _league_for_cal,
                odds_change=None, sp_had_probs=_sp_had_shin, sp_hhad_probs=_sp_hhad_shin,
                had_init=_had_init, hhad_init=_hhad_init)
            if _hhad_rel < 0.5:
                # HHAD不可靠, 降低其置信度: 向均匀分布收缩
                _hhad_pull = (1.0 - _hhad_rel) * 0.15  # 最多收缩15%
                _max_i = hhad_final_probs.index(max(hhad_final_probs))
                for _i in range(3):
                    if _i == _max_i:
                        hhad_final_probs[_i] -= _hhad_pull * 0.8
                    else:
                        hhad_final_probs[_i] += _hhad_pull * 0.4
                # 重新归一化
                _s = sum(hhad_final_probs)
                if _s > 0:
                    hhad_final_probs = [p / _s for p in hhad_final_probs]
            if _oca_hhad_notes:
                print(f'  {" ".join(_oca_hhad_notes)}')
        except Exception as _e:
            pass  # 矛盾信号校准失败时跳过, 不影响原有逻辑

    hhad_final_idx = hhad_final_probs.index(max(hhad_final_probs))
    if hhad and 'h' in hhad:
        hhad_dir = hhad_dirs[hhad_final_idx]
        hhad_odds_val = round(hhad_odds_list[hhad_final_idx], 2)
    else:
        hhad_dir = hhad_dirs[hhad_final_idx]

    # Ultra 11.10 铁律: 预测报告须正确标注让/受让 — 受让盘(handicap>0)必须标"受让X"而非"让X"
    # (ERR-20260811-002: hhad_dir 此前未过 _hhad_display_label, 受让盘(+1等)的 JSON dir/
    #  控制台摘要/一致性文案全标成"让胜/让负", 与跨玩法(已转换)矛盾; 此处在方向确定后统一转换)
    hhad_dir = _hhad_display_label(hhad_dir, handicap)

    # ===== 置信度计算 (Ultra 12.1: 星级 = 校准命中率分档) =====
    # 新定义(用户要求): 星级体现模型对预测结果的信心程度, 越高说明越稳。
    #   信心 = 校准命中率(模型概率经历史校准后的真实命中期望), 星越高=真实命中越高。
    # 两步: ① calibrate_hit_rate 把模型概率校准为真实命中率(修正高概率高估)
    #       ② hit_rate_to_score 按命中率分档映射星级
    # 校准系数来源(LRN-20260809-002 深度因子分析83场分档实测):
    #   p60-70%档: 预测64% 实际42% (高估22pp) → ×0.66
    #   p50-60%档: 预测52% 实际55% (校准良好) → ×1.00
    #   p40-50%档: 轻度低估 → ×1.15
    #   p<40%档:  预测34% 实际55% (低估) → ×1.30
    # 注: regression.db(verify_history)积累后可切换 isotonic 连续校准(升级10 conf_ece)
    def calibrate_hit_rate(p):
        """模型概率 → 校准命中率 (经验分段, 修正高概率系统性高估)"""
        if p >= 0.60: return p * 0.66
        if p >= 0.50: return p * 1.00
        if p >= 0.40: return p * 1.15
        return p * 1.30

    def hit_rate_to_score(p_cal):
        """校准命中率 → 星级分档 (越高越稳, 单调可解读)
        5★≥65%(极稳) 4.5★≥58% 4★≥52% 3.5★≥45% 3★≥38% 2.5★≥33% 2★≥28% 1.5★≥24% 1★<24%"""
        if p_cal >= 0.65: return 5.0
        if p_cal >= 0.58: return 4.5
        if p_cal >= 0.52: return 4.0
        if p_cal >= 0.45: return 3.5
        if p_cal >= 0.38: return 3.0
        if p_cal >= 0.33: return 2.5
        if p_cal >= 0.28: return 2.0
        if p_cal >= 0.24: return 1.5
        return 1.0

    had_spread = sorted(had_probs, reverse=True)
    had_delta = had_spread[0] - had_spread[1]
    had_hit_rate = calibrate_hit_rate(had_probs[had_min_idx])   # 主推方向校准命中率
    had_conf_score = hit_rate_to_score(had_hit_rate)

    # Ultra 8.0: 平局风险惩罚 — 预测"胜/负"但平局概率>25%时置信度封顶★★★★
    # (29场回归: 预测"胜"→实际"平"8次, 平局概率高时高置信度是虚假信心)
    if had_probs[1] > 0.25 and had_min_idx != 1:  # 平局概率>25%且预测方向非平局
        had_conf_score = min(had_conf_score, 4.0)

    hhad_spread = sorted(hhad_final_probs, reverse=True)
    hhad_delta = hhad_spread[0] - hhad_spread[1]
    hhad_hit_rate = calibrate_hit_rate(hhad_final_probs[hhad_final_idx])
    hhad_conf_score = hit_rate_to_score(hhad_hit_rate)

    # 数据质量调节 (Ultra 1.0): 质量差则降星
    # Ultra 7.6 (P7): 复用上方dq结果, 不再重复调用 assess_data_quality(sp)
    if dq['score'] < 50:
        had_conf_score = max(1.0, had_conf_score - 0.5)
        hhad_conf_score = max(1.0, hhad_conf_score - 0.5)
        had_conf_score = min(had_conf_score, 3.0)
    elif dq['score'] >= 80:
        had_conf_score = min(5.0, had_conf_score + 0.0)  # 高质量不额外加星, 防止过拟合

    # Ultra 7.6 (P12): proxy xG 置信度封顶★★★★ (占位符数据不应给出满星信心)
    if _xg_is_proxy:
        had_conf_score = min(had_conf_score, 4.0)
        hhad_conf_score = min(hhad_conf_score, 4.0)

    # Ultra 8.0: 联赛专项置信度封顶 (29场回归: 韩职HAD 0%, 欧冠HHAD 0%)
    _league_name = sp.get('league', '')
    if _league_name == '韩职':
        had_conf_score = min(had_conf_score, 3.0)
        hhad_conf_score = min(hhad_conf_score, 3.0)
    if _league_name in ('欧冠', '欧罗巴'):
        hhad_conf_score = min(hhad_conf_score, 3.5)

    # Ultra 8.0: xG 交叉验证质量加星 — xG与实际进球一致性高时增信
    if _xg_data and _xg_data['cv_quality_avg'] >= 0.45:
        had_conf_score = min(5.0, had_conf_score + 0.5)
        hhad_conf_score = min(5.0, hhad_conf_score + 0.5)

    # HAD-Poisson一致性验证 — Ultra 3.0: 使用ensemble_fuse返回的agreement
    poisson_wdl = [v/100 for v in scores['poisson_wdl']]
    poisson_had_idx = poisson_wdl.index(max(poisson_wdl))
    had_poisson_consistent = (had_min_idx == poisson_had_idx)
    if not had_poisson_consistent:
        had_conf_score = max(1.0, had_conf_score - 0.5)

    # Ultra 3.0: 模型一致性降星 (使用ensemble_fuse的agreement)
    if model_agreement < 0.5:  # 市场与Poisson方向不一致
        had_conf_score = max(1.0, had_conf_score - 0.5)

    # HAD-HHAD一致性验证
    hhad_has_data = hhad and 'h' in hhad
    consistency_note = ""
    if hhad_has_data:
        if had_min_idx == hhad_final_idx:
            consistency_note = f"HAD({had_dir})=HHAD({hhad_dir})一致"
        elif (had_min_idx == 0 and hhad_final_idx == 2) or (had_min_idx == 2 and hhad_final_idx == 0):
            consistency_note = f"HAD({had_dir})≠HHAD({hhad_dir})弱一致(险胜)"
            had_conf_score = max(1.0, had_conf_score - 0.5)
        else:
            consistency_note = f"HAD({had_dir})≠HHAD({hhad_dir})分歧"
            had_conf_score = max(1.0, had_conf_score - 0.5)
    else:
        consistency_note = "HHAD未开盘"

    # Ultra 3.0: 比赛可预测性评分
    difficulty = match_difficulty_score(had_probs, poisson_wdl, dq['score'], model_agreement)

    had_conf = format_stars(had_conf_score)
    # Ultra 6.0: 历史规律反馈 — 从验证数据库查询历史命中率
    historical_feedback = query_historical_feedback(
        league=sp.get('league', ''),
        had_dir=had_dir,
        conf_score=had_conf_score,
        odds=odds
    )
    
    # 根据历史反馈调整置信度 (Ultra 7.7: 硬约束升级)
    if historical_feedback:
        # 联赛命中率偏低 — 降级HAD+HHAD (阈值放宽: 45→50, 样本5→3)
        if historical_feedback.get('league_rate', 100) < 50 and historical_feedback.get('league_samples', 0) >= 3:
            had_conf_score = max(1.0, had_conf_score - 0.5)
            hhad_conf_score = max(1.0, hhad_conf_score - 0.5)
            had_conf = format_stars(had_conf_score)
        # 方向命中率偏低 — 降级HAD+HHAD (阈值放宽: 40→45, 样本10→5)
        if historical_feedback.get('direction_rate', 100) < 45 and historical_feedback.get('direction_samples', 0) >= 5:
            had_conf_score = max(1.0, had_conf_score - 0.5)
            hhad_conf_score = max(1.0, hhad_conf_score - 0.5)
            had_conf = format_stars(had_conf_score)
        # Ultra 7.5→7.7: 历史校准警告自动降星 — 高置信度命中率偏低时降星
        if historical_feedback.get('calibration_warning'):
            had_conf_score = max(1.0, had_conf_score - 0.5)
            hhad_conf_score = max(1.0, hhad_conf_score - 0.5)
            had_conf = format_stars(had_conf_score)
        # Ultra 7.7: 高置信度命中率严重偏低(<45%) — 追加降级+硬封顶
        _hc_rate = historical_feedback.get('high_conf_rate', 100)
        _hc_samples = historical_feedback.get('high_conf_samples', 0)
        if _hc_rate < 45 and _hc_samples >= 3:
            had_conf_score = max(1.0, had_conf_score - 0.5)
            hhad_conf_score = max(1.0, hhad_conf_score - 0.5)
            had_conf = format_stars(had_conf_score)
            v611_notes.append(f"[历史反馈] 高置信命中率{_hc_rate}%严重偏低, 追加-0.5★")
        # Ultra 7.7: 高置信度命中率极低(<40%) — 硬封顶★★★½
        if _hc_rate < 40 and _hc_samples >= 3:
            had_conf_score = min(had_conf_score, 3.5)
            hhad_conf_score = min(hhad_conf_score, 3.5)
            had_conf = format_stars(had_conf_score)
            v611_notes.append(f"[历史反馈] 高置信命中率{_hc_rate}%极低, 硬封顶★★★½")

    # Ultra 7.7: xG超额表现回归风险 — 超额>+0.5时降级置信度
    # 实证案例: 波兹南超额+0.86, 次回合0-3惨败 → 高超额=高回归风险
    _xg_regress_flag = False
    if _xg_home and _xg_home.get('overperformance', 0) > 0.5:
        had_conf_score = max(1.0, had_conf_score - 0.5)
        hhad_conf_score = max(1.0, hhad_conf_score - 0.5)
        had_conf = format_stars(had_conf_score)
        v611_notes.append(f"[xG回归] 主队超额{_xg_home['overperformance']:+.2f}>+0.5, 置信度-0.5★")
        _xg_regress_flag = True
    if _xg_away and _xg_away.get('overperformance', 0) > 0.5:
        had_conf_score = max(1.0, had_conf_score - 0.5)
        hhad_conf_score = max(1.0, hhad_conf_score - 0.5)
        had_conf = format_stars(had_conf_score)
        v611_notes.append(f"[xG回归] 客队超额{_xg_away['overperformance']:+.2f}>+0.5, 置信度-0.5★")
        _xg_regress_flag = True

    # Ultra 7.4: 杯赛首回合大比分惩罚 — 置信度封顶★★★★
    if cup_leg_penalty_info and cup_leg_penalty_info.get('applied'):
        conf_cap = cup_leg_penalty_info.get('conf_cap', 4.0)
        if had_conf_score > conf_cap:
            had_conf_score = conf_cap
            had_conf = format_stars(had_conf_score)
        if hhad_conf_score > conf_cap:
            hhad_conf_score = conf_cap

    # Ultra 7.5: 高难度比赛置信度封顶 — 可预测性低则限制最高置信度
    # difficulty < 30 (极难预测) → 封顶★★★; < 40 (难预测) → 封顶★★★½
    if difficulty < 30:
        had_conf_score = min(had_conf_score, 3.0)
        hhad_conf_score = min(hhad_conf_score, 3.0)
    elif difficulty < 40:
        had_conf_score = min(had_conf_score, 3.5)
        hhad_conf_score = min(hhad_conf_score, 3.5)

    # ===== Ultra 11.6: 高赔价值陷阱降置信 (HAD专项回归 2026-08-06) =====
    # 回归发现(51场HAD): 预测赔率>2.5 命中率16.7%(低于随机33.3%), ROI -35%
    # 高赔区间是价值陷阱: 高赔方向多为冷门/Nordic强队, 模型高估其命中
    # 修正: 预测赔率>2.5时直接降置信, >3.0封顶★★, 2.5-3.0封顶★★½
    # 效果: 规避高赔陷阱, 置信度与真实命中率匹配
    if odds and odds > 2.5:
        _hc_odds_cap = 2.5 if odds <= 3.0 else 2.0
        had_conf_score = min(had_conf_score, _hc_odds_cap)
        v611_notes.append(f"[回归] 高赔{odds:.2f}>2.5价值陷阱(命中率16.7%), 置信度封顶{format_stars(_hc_odds_cap)}")

    # ===== Ultra 11.7: 高置信热度惩罚 (HAD专项回归 2026-08-06) =====
    # 回归发现(51场HAD): ≥3.5★ 命中率41.2%为全档最低, ROI仅+1.0%
    # 高置信失误集中在低赔热门(1.3-1.9): 瓦勒伦加1.53/AIK1.32/莫尔德1.47 全翻车
    # 根因: 大热门低赔高置信, 赔率本身已难有判别力, 高置信=虚假信心
    # 修正: 高置信(≥3.5★) + 低赔热门(≤2.0) → 热度惩罚-0.5★, 封顶★★★
    # 效果: 高置信档位不再虚高, 与41.2%真实命中率匹配
    if had_conf_score >= 3.5 and odds and odds <= 2.0:
        had_conf_score = min(had_conf_score - 0.5, 3.0)
        v611_notes.append(f"[回归] 高置信{format_stars(had_conf_score+0.5 if had_conf_score<3.5 else 3.0)}低赔热门{odds:.2f}热度惩罚, 封顶★★★")

    # ===== Ultra 11.9: 强化热度惩罚 — 低赔热门 + 主队xG超额 反向风险 (HAD专项回归 2026-08-07) =====
    # 首次实战(260806周四004): 塞萨洛 欧指1.65→1.46低赔大热门, 实际0-1负
    #   且主队xG超额+0.39(进球远超xG)有回归风险, 是典型"热度陷阱"
    # 修正: 低赔热门(≤1.5) + 主队xG超额(>+0.3) → 置信度硬封顶★★ + 提示反向风险
    # 效果: "欧指走热+xG超额"组合明确降级, 避免虚假高信心误导投注
    if odds and odds <= 1.5 and _xg_home and _xg_home.get('overperformance', 0) > 0.3:
        had_conf_score = min(had_conf_score, 2.0)
        v611_notes.append(f"[回归] 低赔热门{odds:.2f}≤1.5+主队xG超额{_xg_home['overperformance']:+.2f}>+0.3热度陷阱, 置信度封顶★★, 提示反向风险")

    # ===== Ultra 11.12: 深度因子分析ROI修正 (LRN-20260809-002, 83场) =====
    # 三块落地: ①热量陷阱降权 ②概率校准修正(高概率高估) ③黄金窗口加码
    # 数据依据:
    #   ① 高置信≥4★+低赔<1.8 → 命中29%/ROI -4.7 (热量陷阱, 市场已定价)
    #      中置信3-4★+低赔<1.8 → 命中55%/ROI +3.9 (唯一命中盈利双优甜区)
    #   ② HAD主推 p60-70%档 平均预测64% 实际命中42% (偏差22pp, 过度自信)
    #      p30-40%被低估(34%→55%), 仅p50-60%校准良好(52%→55%)
    #   ③ 黄金窗口: HAD负+低赔<1.5 (5/5=100%,+1.7) 与 HHAD让负+低赔<1.5 (3/4=75%,+5.6) 双正
    # 注意: 子样本4-30场, 置信区间宽, 属短期规律, 宜作"优先观察"而非"重仓规则"
    _had_main_prob = had_probs[had_min_idx] if had_probs else 0

    # ① 热量陷阱降权 — 高置信(≥4★, 即delta≥0.10) + 低赔热门(<1.8)
    # 深度因子分析: 该组合命中率仅29%, 与中置信甜区(55%)倒挂
    # 与 Ultra 11.7(≥3.5★+≤2.0封顶★★★)互补: 11.7已封顶3.0, 此处对最危险档再压一档
    if had_conf_score >= 4.0 and odds and odds < 1.8:
        had_conf_score = min(had_conf_score - 0.5, 2.5)
        v611_notes.append(f"[因子] 高置信≥4★+低赔{odds:.2f}<1.8热量陷阱(命中29%), 置信度封顶★★½")

    # ② 概率校准修正 — Ultra 12.1 已移除: 高概率高估(≥60%档实际42%)现已由
    #    calibrate_hit_rate 在基础分中统一处理(p≥0.60 ×0.66), 此处不再重复-0.5★,
    #    避免与命中率校准双重惩罚同一场景

    # ③ 黄金窗口加码 — HAD负/HHAD让负 + 低赔<1.5
    # 因子发现: 该两窗口命中+ROI双正(HAD负100%/HHAD让负75%), 是可控的盈利甜区
    # 修正: 命中两个黄金窗口 → 置信度+0.5★(不突破5.0上限)
    # (ERR-20260811-002: hhad_dir 已做受让术语转换, 逻辑判断改用索引 hhad_final_idx==2,
    #  让负/受让负同源(idx=2), 保证受让盘黄金窗口行为与修复前一致)
    _golden_hit = False
    if had_dir == '负' and odds and odds < 1.5:
        had_conf_score = min(5.0, had_conf_score + 0.5)
        _golden_hit = True
        v611_notes.append(f"[因子] HAD负+低赔{odds:.2f}<1.5黄金窗口(100%命中), 置信度+0.5★")
    if hhad_has_data and hhad_final_idx == 2 and hhad_odds_val and hhad_odds_val < 1.5:
        hhad_conf_score = min(5.0, hhad_conf_score + 0.5)
        v611_notes.append(f"[因子] HHAD让负+低赔{hhad_odds_val:.2f}<1.5黄金窗口(75%命中), 置信度+0.5★")

    # 升级10: 置信度 ECE 校准封顶 — Δ(top1-top2概率差)→历史实际命中率
    # isotonic映射。Δ大但历史命中率不支撑高星时封顶, 使星级与真实
    # 期望命中率单调一致, 消除"高Δ虚高置信"。缺参(回归库不足)自动跳过。
    if _MU and UPGRADES.get('conf_ece') and _UPG_PARAMS.get('conf_calibrator'):
        try:
            _sp_had = sorted(had_probs, reverse=True)
            _delta = _sp_had[0] - _sp_had[1]
            _exp_hit = _MU.calibrated_confidence(_delta, _UPG_PARAMS['conf_calibrator'])
            if _exp_hit is not None:
                _ece_cap = (5.0 if _exp_hit >= 0.65 else
                            4.5 if _exp_hit >= 0.58 else
                            4.0 if _exp_hit >= 0.50 else
                            3.5 if _exp_hit >= 0.42 else 3.0)
                if had_conf_score > _ece_cap:
                    had_conf_score = _ece_cap
                    v611_notes.append(
                        f"[ECE] Δ={_delta:.2f}→期望命中{_exp_hit:.0%}, 置信度封顶{format_stars(_ece_cap)}")
        except Exception:
            pass

    had_conf = format_stars(had_conf_score)
    hhad_conf = format_stars(hhad_conf_score)

    # ===== 证据收集 =====
    had_str = f"{had['h']}/{had['d']}/{had['a']}" if had_open else "未开"
    hhad_str = f"{handicap} {hhad['h']}/{hhad['d']}/{hhad['a']}" if hhad and 'h' in hhad else f"{handicap} 未开"
    evidence = [
        f"HAD {had_str}→{had_dir}@{odds}" if had_open else f"HAD 未开→不推荐",
        f"HHAD {hhad_str}→{hhad_dir}",
    ]
    if ouzhi or avg_odds or init_ouzhi:
        if init_ouzhi:
            ai = init_ouzhi['avg_initial']
            ai0 = init_ouzhi['avg_instant']
            ev_odds = f"{ai0[0]:.2f}/{ai0[1]:.2f}/{ai0[2]:.2f}"
            ev_init = f"初{ai[0]:.2f}/{ai[1]:.2f}/{ai[2]:.2f}"
            evidence.append(f"500:{ev_odds} Δ{ouzhi_change_str}({ev_init})")
        elif avg_odds:
            ev_odds = f"{ow}/{od}/{ol}"
            evidence.append(f"500:{ev_odds} Δ{ouzhi_change_str}")
        elif ouzhi and ouzhi_is_rr:
            ev_odds = f"{pw5:.0%}/{pd5:.0%}/{pl5:.0%}"
            evidence.append(f"500:{ev_odds} Δ{ouzhi_change_str}")
        else:
            ev_odds = f"{ow}/{od}/{ol}"
            evidence.append(f"500:{ev_odds} Δ{ouzhi_change_str}")
    if home_form:
        evidence.append(f"主:{home_form}")
    if away_form:
        evidence.append(f"客:{away_form}")
    
    # top3比分 — 按市场盘口方向过滤，确保一致性
    top3_str = ' '.join(f"{s}:{p}" for s, p in scores['top3_filtered'][:3])
    high_top3_str = ' '.join(f"{s}:{p}" for s, p in scores['high_top3'][:3])
    wdl_str = '/'.join(f"{v}" for v in scores['poisson_wdl'])
    hhad_wdl_str = '/'.join(f"{v}" for v in scores['hhad_wdl'])

    # Ultra 3.0: 移除goals_line多盘口字符串 (节省~80 tokens/场), 仅保留over_main/over_low/over_high

    # ===== Pro 3.0: Kelly公式 (四分之一Kelly) =====
    p_for_had_dir = had_probs[had_min_idx]
    p_for_hhad_dir = hhad_final_probs[hhad_final_idx]

    # ===== Pro 3.9: 跨玩法价值分析 (概率优先, EV仅参考) =====
    # Ultra 11.11: 传入 difficulty — 让平高发窗口/平局盲区补偿依赖"中等难度"判定
    # (深度因子分析 LRN-20260809-002: 中等难度45-65让平率44%, 受让盘让平率36%)
    cross_market = compute_cross_market_value(
        had_probs, had, hhad_final_probs, hhad, handicap, lam_h, lam_a,
        mode=RECOMMEND_MODE, difficulty=difficulty
    )

    # ===== Ultra 6.5: 竞彩固定奖金 EV 价值分析 =====
    # 用竞彩官方赔率(实际投注赔率)对模型概率做EV检验
    # EV = 模型概率 × 官方赔率 - 1; EV>0 才有长期价值
    # Optimize: 各玩法添加margin计算, 过滤EV< -margin*0.7的极端负值选项
    # 高margin玩法(比分30-40%)的EV天然更低, 不加过滤会展示大量负EV选项
    sporttery_pools = None
    bonus = sp.get('sporttery_bonus')
    if bonus:
        sporttery_pools = {}
        # TTG 总进球: 模型probs {'0球'..'7+球'} vs 官方 ttg {0..7}
        # Ultra 11.31: 命中率第一优先 — 去掉按EV硬过滤, 否则赔率低的最高概率进球(2球/3球)
        #   会被EV<0剔除, 只剩高赔冷门(4球/5球), 与比分/胜平负推荐矛盾 (LRN-20260810-010)
        if bonus.get('ttg') and total_goals_pred.get('probs'):
            tg_margin = pool_margin(list(bonus['ttg'].values()))
            tg_picks = []
            for k, o in bonus['ttg'].items():
                label = f"{int(k)}球" if int(k) < 7 else "7+球"
                p = total_goals_pred['probs'].get(label, 0)
                if p > 0.01:
                    ev_pct = round((p * o - 1) * 100, 1)
                    tg_picks.append({'option': label, 'odds': o, 'prob': round(p * 100, 1),
                                     'ev_pct': ev_pct, 'margin': round(tg_margin * 100, 1)})
            tg_picks.sort(key=lambda x: x['prob'], reverse=True)
            if tg_picks:
                sporttery_pools['ttg'] = tg_picks[:3]
        # HAFU 半全场: 模型probs {'胜胜'..} vs 官方 hafu
        # Ultra 11.31: 同TTG, 命中率优先, EV仅作标注
        if bonus.get('hafu') and half_full.get('probs'):
            hf_margin = pool_margin(list(bonus['hafu'].values()))
            hf_picks = []
            for name, o in bonus['hafu'].items():
                p = half_full['probs'].get(name, 0)
                if p > 0.01:
                    ev_pct = round((p * o - 1) * 100, 1)
                    hf_picks.append({'option': name, 'odds': o, 'prob': round(p * 100, 1),
                                     'ev_pct': ev_pct, 'margin': round(hf_margin * 100, 1)})
            hf_picks.sort(key=lambda x: x['prob'], reverse=True)
            if hf_picks:
                sporttery_pools['hafu'] = hf_picks[:3]
        # CRS 比分: 模型top5比分 vs 官方 crs
        # Ultra 11.31: 同TTG, 命中率优先, EV仅作标注
        if bonus.get('crs') and scores.get('top5_raw'):
            crs_margin = pool_margin(list(bonus['crs'].values()))
            crs_picks = []
            for s, pct in scores['top5_raw']:
                o = bonus['crs'].get(s)
                if o:
                    p = pct / 100.0
                    ev_pct = round((p * o - 1) * 100, 1)
                    crs_picks.append({'option': s, 'odds': o, 'prob': pct,
                                      'ev_pct': ev_pct, 'margin': round(crs_margin * 100, 1)})
            crs_picks.sort(key=lambda x: x['prob'], reverse=True)
            if crs_picks:
                sporttery_pools['crs'] = crs_picks[:3]
        if not sporttery_pools:
            sporttery_pools = None

    return {
        'HAD': {
            'dir': had_dir,
            'odds': odds,
            'conf': had_conf if had_open else '—',
            'conf_hit_rate': round(had_hit_rate * 100, 1) if had_open else None,  # Ultra 12.1: 校准命中率%
            'p': f"{p1_w:.0%}/{p1_d:.0%}/{p1_l:.0%}" if had_open else '未开盘',
            'had_open': had_open,
            'draw_override': _draw_override,  # Ultra 12.2: 平局方向覆盖标记
        },
        'HHAD': {
            'dir': hhad_dir,
            'handicap': handicap,
            'odds': hhad_odds_val,
            'conf': hhad_conf,
            'conf_hit_rate': round(hhad_hit_rate * 100, 1),  # Ultra 12.1: 校准命中率%
            'p': f"{hhad_final_probs[0]:.0%}/{hhad_final_probs[1]:.0%}/{hhad_final_probs[2]:.0%}",
            'poisson': hhad_wdl_str,
        },
        'kelly': {
            'HAD': kelly_criterion(p_for_had_dir, odds,
                                   pool_margin([had['h'], had['d'], had['a']]) if had_open else 0.0) if had_open else {'stake_pct': 0, 'ev': 0, 'value': False},
            'HHAD': kelly_criterion(p_for_hhad_dir, hhad_odds_val,
                                    pool_margin([hhad['h'], hhad['d'], hhad['a']]) if hhad and 'h' in hhad else 0.0),
        },
        'data_quality': dq,
        'difficulty': difficulty,  # Ultra 3.0: 比赛可预测性评分 0-100
        'model_agreement': round(model_agreement, 2),  # Ultra 3.0: 模型一致性 0-1
        'js_agreement': js_agreement,  # Ultra 7.6 (P10): JS散度分布一致性 0-1 (信息字段)
        'score': {
            'top3': top3_str,
            'high_top3': high_top3_str,
            'wdl': wdl_str,
            'main_dir': scores['main_dir'],
            'high_dir': scores.get('high_dir', ''),
            'market_gl_str': scores['market_gl_str'],
            'over_main': scores['over_main'],
            # Ultra 3.0: 精简goals_line, 保留关键over值
            'over_low': scores['over_low'],
            'over_high': scores['over_high'],
        },
        'lam': f"{lam_h:.1f}/{lam_a:.1f}",
        'lam_calibration': {
            'recalibrated': lam_recalibrated,
            'original': f"{lam_h_orig}/{lam_a_orig}" if lam_recalibrated else None,
            'calibrated': f"{round(lam_h,2)}/{round(lam_a,2)}" if lam_recalibrated else None,
            'reason': lam_calib_note if lam_recalibrated else '无冲突',
        },
        'goals': goals,
        'half_full': {
            'main': half_full['main'],
            'top3': half_full['top3'],
            'recalibrated': lam_recalibrated,
            # Ultra 3.0: 移除all (节省~150 tokens/场), top3已足够
        },
        'total_goals': {
            'main': total_goals_pred['main'],
            'top3': total_goals_pred['top3'],
            # Ultra 3.0: 移除all (节省~100 tokens/场)
        },
        'market_gl_source': market_gl_source,
        'initial_gl': round(initial_goal_line, 2),
        'ev': evidence[:4] + [consistency_note] + ([lam_calib_note] if lam_recalibrated else []) + _adv_notes + v611_notes + _oca_hhad_notes
              + ([f"[xG] 使用xG替代进球(质量{_xg_data['cv_quality_avg']:.2f}), 超额修正"
                  if _xg_data and _xg_data['cv_quality_avg'] > 0 else ""] if _xg_data else []),
        'initial': _build_initial_summary(init_ouzhi, init_yazhi, init_daxiao),
        'cross_market': cross_market,
        'historical_feedback': historical_feedback,
        'sporttery_pools': sporttery_pools,  # Ultra 6.5: 竞彩固定奖金EV分析
        'v611_flags': v611_flags,  # Ultra 6.11: 五大场景修正标记
        'cup_leg_penalty': cup_leg_penalty_info,  # Ultra 7.4: 杯赛首回合惩罚信息
        # Ultra 8.0: xG/xGA 数据 + 交叉验证质量
        'xg_data': _xg_data,
    }

# ============================================================
# Monitor: 全程监测工具
# ============================================================
def estimate_tokens(text):
    """粗略估算token数: 中文≈1.5字符/token, 英文≈4字符/token"""
    if isinstance(text, (dict, list)):
        text = json.dumps(text, ensure_ascii=False)
    cn_chars = len(re.findall(r'[\u4e00-\u9fa5]', str(text)))
    en_chars = len(str(text)) - cn_chars
    return int(cn_chars / 1.5 + en_chars / 4)

def fmt_size(s):
    return f"{len(s)}B/{estimate_tokens(s)}T"

# ============================================================
# Phase 1.5: nowscore 辅助数据源 — 为体彩预测提供统计增强 (盘口+近况+交锋+积分)
# ============================================================
def _fetch_one_nowscore(key, mi):
    """单场nowscore获取 (供线程池并行调用)"""
    try:
        ns = fetch_nowscore_match_data(mi['home'], mi['away'])
        if ns:
            for k, v in mi.items():
                if k not in ns:
                    ns[k] = v
            # 保留原始数据源 (sporttery), 不覆盖
            if 'data_source' not in ns or ns.get('data_source') in ('nowscore', None):
                ns['data_source'] = 'nowscore'
            return key, ns, None
        else:
            # P1-4: 记录降级原因
            return key, None, 'no_data'
    except Exception as e:
        return key, None, str(e)

def fetch_nowscore_for_matches(matches):
    """用nowscore为体彩比赛获取统计数据增强 (辅助数据源)

    参数:
        matches: sporttery API返回的 {key: match_info} 字典

    返回: (nowscore_data, failed_keys)
        - nowscore_data: {key: merged_data} 成功获取的比赛
        - failed_keys: 需要降级到500.com的比赛key列表
    
    Ultra-Opt: ThreadPoolExecutor 并行获取多场 (旧版串行for循环, 9场×20s=180s+)
    """
    nowscore_data = {}
    failed_keys = []
    
    if not NOWSCORE_AVAILABLE:
        print("  [nowscore] 模块未加载, 全部降级500.com")
        return nowscore_data, list(matches.keys())
    
    # 分离历史比赛 (跳过) 和可获取的比赛
    fetchable = {}
    for key, mi in matches.items():
        try:
            md = mi.get('match_date', '')
            if md:
                from datetime import date as _date
                md_date = datetime.strptime(md, '%Y-%m-%d').date()
                if (_date.today() - md_date).days > 1:
                    failed_keys.append(key)
                    # P1-4: 记录降级原因
                    matches[key]['fallback_reason'] = f'历史比赛({md}), nowscore跳过'
                    print(f"  [nowscore] ⏭ {key} 历史比赛({md}), 直接降级500.com")
                    continue
        except Exception:
            pass
        fetchable[key] = mi
    
    if not fetchable:
        return nowscore_data, failed_keys
    
    # 并行获取 (max_workers=5: 平衡并发与服务器友好度)
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_fetch_one_nowscore, key, mi): key
                   for key, mi in fetchable.items()}
        for fut in as_completed(futures):
            key = futures[fut]
            mi = fetchable[key]
            _, ns, err = fut.result()
            if ns:
                nowscore_data[key] = ns
                print(f"  [nowscore] ✅ {key} {mi['home']} vs {mi['away']}")
            else:
                failed_keys.append(key)
                # P1-2/P1-4: 标记降级原因
                reason = f'nowscore失败({err})' if err and err != 'no_data' else 'nowscore无数据'
                matches[key]['fallback_reason'] = reason
                if err and err != 'no_data':
                    print(f"  [nowscore] ❌ {key} {mi['home']} vs {mi['away']} 异常: {err} → 降级500.com")
                else:
                    print(f"  [nowscore] ❌ {key} {mi['home']} vs {mi['away']} → 降级500.com")
    
    return nowscore_data, failed_keys


# ============================================================
# Ultra 8.1: 更新模式 — 数据比对与预测调整
# ============================================================
def compare_and_adjust_for_update(prev_results, new_results):
    """更新模式: 比对上次预测与新数据, 根据变化调整预测结果

    比对维度:
      1. HAD/HHAD 赔率变化 (>5% 视为显著)
      2. 预测方向是否反转
      3. 置信度是否需要修正

    调整规则:
      - 方向一致 + 赔率向有利于预测方向移动 → 置信度微升 (最多+0.5★)
      - 方向一致 + 赔率反向移动 → 置信度不变 (市场分歧, 维持判断)
      - 方向反转 → 置信度封顶 ★★★ (信号矛盾, 降级处理)
      - 新增场次 → 不调整 (无对比基准)

    Returns:
        (adjusted_results, change_log)
    """
    change_log = []
    # 半星兼容: 用 stars_to_score (支持 ★★★½ 等半星) 替代只识别整星的 _STAR_MAP
    def _star_score(conf_str):
        return stars_to_score(conf_str or '')

    for key in new_results:
        new_r = new_results[key]
        if key not in prev_results:
            change_log.append(f"  {key}: 新增场次 (上次无预测)")
            continue

        prev_r = prev_results[key]
        changes_for_key = []

        # --- 比对 HAD ---
        prev_had = prev_r.get('HAD', {})
        new_had = new_r.get('HAD', {})
        prev_had_dir = prev_had.get('dir', '')
        new_had_dir = new_had.get('dir', '')

        had_odds_changed = False
        had_odds_shift = 0.0  # 正=赔率上升(概率下降), 负=赔率下降(概率上升)
        for ok in ('h', 'd', 'a'):
            po = prev_had.get(ok, 0) or 0
            no = new_had.get(ok, 0) or 0
            if po > 0 and no > 0:
                pct = (no - po) / po
                if abs(pct) > 0.05:
                    had_odds_changed = True
                    had_odds_shift += pct
                    changes_for_key.append(f"HAD.{ok}: {po}→{no} ({pct*100:+.1f}%)")

        # 方向是否反转
        had_dir_reversed = (prev_had_dir and new_had_dir and prev_had_dir != new_had_dir)
        if had_dir_reversed:
            changes_for_key.append(f"HAD方向反转: {prev_had_dir}→{new_had_dir}")

        # --- 比对 HHAD ---
        prev_hhad = prev_r.get('HHAD', {})
        new_hhad = new_r.get('HHAD', {})
        prev_hhad_dir = prev_hhad.get('dir', '')
        new_hhad_dir = new_hhad.get('dir', '')

        hhad_dir_reversed = (prev_hhad_dir and new_hhad_dir and prev_hhad_dir != new_hhad_dir)
        if hhad_dir_reversed:
            changes_for_key.append(f"HHAD方向反转: {prev_hhad_dir}→{new_hhad_dir}")

        # --- 调整置信度 ---
        # HAD 方向反转 → 封顶 ★★★
        if had_dir_reversed and new_had_dir:
            cur_conf = new_had.get('conf', '')
            cur_stars = _star_score(cur_conf)
            if cur_stars > 3:
                new_had['conf'] = '★★★'
                changes_for_key.append(f"HAD置信度降级: {cur_conf}→★★★ (方向反转)")

        # HHAD 方向反转 → 封顶 ★★★
        if hhad_dir_reversed and new_hhad_dir:
            cur_conf = new_hhad.get('conf', '')
            cur_stars = _star_score(cur_conf)
            if cur_stars > 3:
                new_hhad['conf'] = '★★★'
                changes_for_key.append(f"HHAD置信度降级: {cur_conf}→★★★ (方向反转)")

        # 方向一致 + 预测方向赔率显著变化 → 微调
        # 只看预测方向的赔率变化 (而非三个赔率之和)
        if had_odds_changed and not had_dir_reversed and new_had_dir:
            cur_conf = new_had.get('conf', '')
            cur_stars = _star_score(cur_conf)
            _dir_odds_key = {'胜': 'h', '平': 'd', '负': 'a'}.get(new_had_dir, '')
            if _dir_odds_key:
                _po = prev_had.get(_dir_odds_key, 0) or 0
                _no = new_had.get(_dir_odds_key, 0) or 0
                if _po > 0 and _no > 0:
                    _pred_pct = (_no - _po) / _po
                    # 预测方向赔率下降(概率上升) → 市场确认, 微升
                    if _pred_pct < -0.05 and cur_stars < 5 and cur_stars > 0:
                        new_stars = min(cur_stars + 1, 5)
                        new_had['conf'] = format_stars(new_stars)
                        changes_for_key.append(f"HAD置信度微升: {cur_conf}→{new_had['conf']} (市场确认, 赔率{_pred_pct*100:+.1f}%)")
                    # 预测方向赔率上升(概率下降) → 市场分歧, 维持但标注
                    elif _pred_pct > 0.05:
                        changes_for_key.append(f"HAD赔率上升(市场分歧, {_pred_pct*100:+.1f}%), 维持{cur_conf}")

        if changes_for_key:
            change_log.append(f"  {key}: " + " | ".join(changes_for_key))

    return new_results, change_log


# ============================================================
# Main: 端到端执行 + 全程监测
# ============================================================
def main():
    t0 = time.time()
    monitor = []  # [(phase, elapsed, data_size, detail)]

    # Ultra 8.1: 工作模式解析 (必须在 match_input 之前, 过滤 --mode 参数)
    apply_cli_mode()
    print(f"  [模式] {PRED_MODE}" + (" (全新预测, 不读缓存)" if PRED_MODE == 'predict' else " (更新预测, 比对上次)"))

    # Ultra 7.3: 命令行编号日期输入 (如 260728 001,002) 优先于顶部配置
    apply_cli_match_input()

    # 记忆系统 v2.0: 预测前自动召回铁律 + 相关记忆 + 编号验证
    if inject_memory_context() == 'ABORT':
        print("\n[中止] 因历史纠错记录终止本次预测")
        return
    
    # ===== Phase 1: Sporttery API (核心 — 体彩场次+赔率基准+固定奖金) =====
    t1 = time.time()
    matches = fetch_sporttery_matches(MATCH_NUMBERS, TARGET_DATE)
    # Ultra 7.7: 官方单关标识/联赛排名/赔率趋势增强 (借鉴 SportteryAPI 字段)
    enrich_sporttery_extra(matches)
    # Ultra 6.5: 并行获取竞彩官方固定奖金 (比分/总进球/半全场赔率, 供EV价值分析)
    if matches:
        with ThreadPoolExecutor(max_workers=4) as pool:
            bonus_futures = {pool.submit(fetch_sporttery_fixed_bonus, mi.get('match_id')): key
                             for key, mi in matches.items() if mi.get('match_id')}
            for fut in as_completed(bonus_futures):
                key = bonus_futures[fut]
                try:
                    bonus = fut.result()
                    if bonus:
                        matches[key]['sporttery_bonus'] = bonus
                        # 从固定奖金中回填HAD/HHAD赔率 (结果API回退时体彩终赔可能缺失)
                        # Ultra 7.10: HAD未在体彩开盘列表时禁止从历史赔率回填 (停售/未开HAD盘)
                        if bonus.get('had') and not matches[key].get('HAD') and matches[key].get('had_in_list', True):
                            matches[key]['HAD'] = bonus['had']
                        if bonus.get('hhad'):
                            _old_hhad = matches[key].get('HHAD', {})
                            # 优先使用getFixedBonusV1的HHAD (有独立赔率, 非HAD复制)
                            if not _old_hhad or _old_hhad.get('h', 0) == matches[key].get('HAD', {}).get('h', -1):
                                matches[key]['HHAD'] = bonus['hhad']
                except Exception:
                    pass
        n_bonus = sum(1 for mi in matches.values() if mi.get('sporttery_bonus'))
        if n_bonus:
            print(f"  [固定奖金] {n_bonus}/{len(matches)}场获取成功 (比分/总进球/半全场/HAD/HHAD赔率)")
    dt1 = time.time() - t1
    raw_sporttery = json.dumps(matches, ensure_ascii=False)
    monitor.append(('Phase1-sporttery', dt1, len(raw_sporttery), 
                     f"匹配{len(matches)}场, 原始{fmt_size(raw_sporttery)}"))
    
    # ===== Phase 1.5: nowscore (辅助 — 为体彩预测提供统计增强) =====
    t15 = time.time()
    nowscore_data, failed_keys = fetch_nowscore_for_matches(matches)
    dt15 = time.time() - t15
    monitor.append(('Phase1.5-nowscore', dt15, len(json.dumps(nowscore_data, ensure_ascii=False, default=str)),
                     f"nowscore {len(nowscore_data)}场成功, {len(failed_keys)}场降级500.com"))
    
    # ===== Phase 2: 500.com (降级 — 仅nowscore失败的场次) =====
    t2 = time.time()
    needs_500 = {k: matches[k] for k in failed_keys}
    matched = {}
    if needs_500:
        fixture_map = fetch_500_fixture_ids()
        for key, mi in needs_500.items():
            if key in fixture_map:
                mi['fixture_id'] = fixture_map[key]
                # P1-2: 标记数据源为500.com (nowscore降级)
                mi['data_source'] = '500.com'
                matched[key] = mi
        unmatched_500 = [k for k in needs_500 if k not in matched]
        if unmatched_500:
            monitor.append(('Phase2-warn', 0, 0, f"⚠️500.com未匹配场次: {unmatched_500}"))
    else:
        fixture_map = {}
    dt2 = time.time() - t2
    monitor.append(('Phase2-fixture_id', dt2, len(str(fixture_map)),
                     f"500.com仅{len(needs_500)}场需取(已跳过{len(nowscore_data)}场nowscore)"))
    
    # ===== Phase 3: 500.com 并行数据获取 (仅对needs_500) =====
    t3 = time.time()
    all_data = dict(nowscore_data)  # 以nowscore数据为基础
    if matched:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(fetch_one_match, key, mi, mi['fixture_id']): key
                        for key, mi in matched.items()}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    _, all_data[key] = future.result()
                except Exception as e:
                    all_data[key] = matched[key]
                    all_data[key]['fetch_error'] = str(e)
    dt3 = time.time() - t3
    
    raw_all = json.dumps(all_data, ensure_ascii=False, default=str)
    monitor.append(('Phase3-total', dt3, len(raw_all), 
                     f"nowscore {len(nowscore_data)} + 500.com {len(matched)} = {len(all_data)}场, 原始{fmt_size(raw_all)}"))
    
    # Ultra 6.5: sporttery 保底 — nowscore/500 双失败的场次不再静默丢弃
    # (旧版: 外部源全失败时该场从输出中消失, 用户误以为sporttery数据没获取)
    # predict_match 对 ouzhi/shuju/daxiao 缺失有 .get 兜底, 可用纯sporttery赔率做基准预测
    dropped = [k for k in matches if k not in all_data]
    if dropped:
        for k in dropped:
            matches[k]['data_source'] = 'sporttery(保底)'
            # P1-4: 记录降级原因
            matches[k]['fallback_reason'] = 'nowscore+500.com均失败, 用sporttery赔率基准预测'
            all_data[k] = matches[k]
        print(f"  [保底] {len(dropped)}场无外部数据, 用sporttery赔率基准预测: {dropped}")
        monitor.append(('Phase3-fallback', 0, 0, f"sporttery保底{len(dropped)}场: {dropped}"))

    # 🔒 数据源策略自检 (锁定策略: sporttery核心/nowscore主力/500仅降级)
    policy_violations = _check_data_source_policy(all_data)
    results = {}  # 必须先初始化, 供下方 policy_violations 记录使用 (原顺序引用会 NameError)
    if policy_violations:
        results.setdefault('_policy_violations', policy_violations)
    
    # ===== Phase 4: 七步预测 =====
    # Ultra 7.4: 清除杯赛首回合惩罚缓存 (每次运行使用最新SWOT数据)
    try:
        clear_leg_cache()
    except Exception:
        pass

    t4 = time.time()
    for key in all_data:
        try:
            results[key] = predict_match(key, all_data[key])
        except Exception as e:
            # 单场预测失败不中断整批, 记录后继续 (防止整批结果丢失)
            import traceback as _tb
            print(f"  [错误] 预测场次 {key} 失败: {e}")
            print(f"          {_tb.format_exc().splitlines()[-2:]}")
            results[key] = None
    # 过滤预测失败场次
    results = {k: v for k, v in results.items() if v is not None}
    dt4 = time.time() - t4
    monitor.append(('Phase4-predict', dt4, 0, f"预测{len(results)}场"))
    
    total = time.time() - t0
    
    # ===== 输出: 结果 + 监测报告 =====
    meta = {key: {'home': m.get('home', ''), 'away': m.get('away', ''), 'match_date': m.get('match_date', ''), 
                  'match_time': m.get('match_time', ''), 'weekday': m.get('weekday', ''),
                  'league': m.get('league', ''), 'fid': m.get('fixture_id', 0),
                  'data_source': m.get('data_source', '500.com'),
                  'fallback_reason': m.get('fallback_reason', ''),
                  'betting_single': m.get('betting_single', False),
                  'home_rank': m.get('home_rank', ''), 'away_rank': m.get('away_rank', ''),
                  'had_trend': m.get('had_trend', '')}
            for key, m in all_data.items()}
    
    output_json = json.dumps({
        'meta': meta,
        'results': results,
    }, ensure_ascii=False, indent=1)
    output_size = len(output_json)
    output_tokens = estimate_tokens(output_json)
    
    # 构建缓存 (供更新模块复用, 节约token)
    cache = {}
    for key in all_data:
        d = all_data.get(key, {})
        cache[key] = {'shuju': d.get('shuju', {})}
    
    # 摘要行
    summary_lines = []
    for key in all_data:
        if key in results:
            r = results[key]
            m = meta[key]
            had_info = r['HAD']
            hhad_info = r['HHAD']
            sc = r['score']
            gl_str = sc.get('market_gl_str', '2.5')
            high_dir = sc.get('high_dir', '')
            ds = m.get('data_source', '500.com')
            fr = m.get('fallback_reason', '')
            ds_display = f"{ds} ⚠️{fr}" if fr else ds
            summary_lines.append(f"  {key} {m['home']} vs {m['away']} [{ds_display}]")
            if had_info.get('had_open', True):
                _hr = had_info.get('conf_hit_rate')
                _hr_s = f" 校准命中≈{_hr:.0f}%" if _hr is not None else ""
                _do_tag = " [平局覆盖]" if had_info.get('draw_override') else ""
                summary_lines.append(f"    HAD:  {had_info['dir']}@{had_info['odds']} {had_info['conf']}{_hr_s}{_do_tag} P={had_info['p']}")
            else:
                summary_lines.append(f"    HAD:  未开盘 (仅参考HHAD)")
            _hhr = hhad_info.get('conf_hit_rate')
            _hhr_s = f" 校准命中≈{_hhr:.0f}%" if _hhr is not None else ""
            summary_lines.append(f"    HHAD: {hhad_info['dir']}@{hhad_info['odds']} {hhad_info['conf']}{_hhr_s} P={hhad_info['p']}")
            summary_lines.append(f"    盘口({r.get('market_gl_source','')}): {gl_str} (市场盘口)")
            # 初赔对比
            init = r.get('initial')
            if init:
                init_parts = []
                if 'ouzhi_init' in init:
                    init_parts.append(f"欧指:{init.get('ouzhi_init','')}→{init.get('ouzhi_now','')}")
                if 'dx_init' in init:
                    init_parts.append(f"大小:{init.get('dx_init','')}→{init.get('dx_now','')}")
                if 'yazhi_init' in init:
                    init_parts.append(f"亚指:{init.get('yazhi_init','')}→{init.get('yazhi_now','')}")
                if init_parts:
                    summary_lines.append(f"    初赔: {' | '.join(init_parts)}")
            summary_lines.append(f"    比分({sc['main_dir']}{gl_str}): {sc['top3']}")
            # Ultra 3.0: 精简盘口概率行, 仅显示主盘口
            summary_lines.append(f"    盘口({gl_str}): 大{sc.get('over_main','')}% 小{100-sc.get('over_main',0):.1f}%")
            # 半全场 (Pro 3.2)
            hf = r.get('half_full', {})
            if hf:
                summary_lines.append(f"    半全场: {hf.get('main','')} | Top3: {hf.get('top3','')}")
            # 总进球数 (Pro 3.2)
            tg = r.get('total_goals', {})
            if tg:
                summary_lines.append(f"    总进球: {tg.get('main','')} | Top3: {tg.get('top3','')}")
            # Ultra 3.0: 可预测性评分
            diff = r.get('difficulty', 0)
            agree = r.get('model_agreement', 0)
            summary_lines.append(f"    可预测性: {diff}/100 | 模型一致性: {agree:.0%}")
            # 跨玩法价值分析 (Ultra 3.0: 精简输出)
            cm = r.get('cross_market', {})
            if cm:
                mode_label = {'prob': '命中率优先', 'ev': 'EV优先', 'hybrid': '混合'}.get(cm.get('primary_mode',''), '')
                pb = cm.get('primary_bet', {})
                if pb:
                    summary_lines.append(f"    主推[{mode_label}]: {pb.get('option','')}@{pb.get('odds','')} P={pb.get('prob','')}% EV={pb.get('ev_pct','')}%")
                hpb = cm.get('hhad_primary_bet', {})
                if hpb:
                    summary_lines.append(f"    HHAD主推: {hpb.get('option','')}@{hpb.get('odds','')} P={hpb.get('prob','')}% EV={hpb.get('ev_pct','')}%")
                pdb = cm.get('pure_direction_bet', {})
                if pdb and pdb.get('option','') != (pb.get('option','') if pb else ''):
                    summary_lines.append(f"    纯方向: {pdb.get('option','')}@{pdb.get('odds','')} P={pdb.get('prob','')}%")
                dr = cm.get('double_recommend', {})
                if dr:
                    _dr_label = '双选并列主推' if (dr.get('parallel') or cm.get('double_parallel_output')) else '双选保险'
                    summary_lines.append(f"    {_dr_label}: {dr.get('option','')}@{dr.get('odds','')} P={dr.get('prob','')}%")
                pr = cm.get('pass_risk', {})
                if pr.get('level', '低') in ('高', '中'):
                    summary_lines.append(f"    穿盘风险: {pr.get('level','')} | {pr.get('desc','')}")
                insight = cm.get('insight', '')
                if insight:
                    summary_lines.append(f"    跨玩法: {insight}")
            # Ultra 6.5: 竞彩固定奖金EV (官方赔率 × 模型概率)
            sp_pools = r.get('sporttery_pools')
            if sp_pools:
                for pool_name, label in (('ttg', '总进球'), ('hafu', '半全场'), ('crs', '比分')):
                    picks = sp_pools.get(pool_name)
                    if picks:
                        # Ultra 11.24: picks 已按命中率(prob)排序, 展示命中率+EV仅作参考
                        best_p = picks[0]['prob']
                        flag = '✅' if best_p >= 20 else '⚠️'
                        txt = ' | '.join(f"{p['option']}@{p['odds']} P={p['prob']}% EV={p['ev_pct']}%" for p in picks[:2])
                        summary_lines.append(f"    竞彩{label}{flag}: {txt}")
            if sc.get('high_top3'):
                summary_lines.append(f"    副盘口比分: {sc['high_top3']}")
    
    print("=" * 60)
    print("【预测结果摘要】")
    print('\n'.join(summary_lines))
    print("=" * 60)
    # 精简监测: 仅输出关键阶段耗时+数据量
    keep_phases = {'Phase1-sporttery', 'Phase1.5-nowscore', 'Phase3-total', 'Phase4-predict', 'Phase2-warn'}
    key_info = {p: (dt, sz, d) for p, dt, sz, d in monitor if p in keep_phases}
    p1 = key_info.get('Phase1-sporttery', (0,0,''))
    p15 = key_info.get('Phase1.5-nowscore', (0,0,''))
    p3 = key_info.get('Phase3-total', (0,0,''))
    print(f"  耗时: sporttery {p1[0]:.1f}s | nowscore {p15[0]:.1f}s | 500.com {p3[0]:.1f}s | 预测 {dt4:.1f}s | 总计 {total:.1f}s")
    print(f"  数据: API {fmt_size(raw_all)} | 输出 {output_size}B/{output_tokens}T | 含缓存 {len(cache)}场")
    
    # ===== 保存预测结果到文件 (供赛果验证使用) =====
    # Ultra-Opt: 通用路径 — 优先 SPORTTERY_WORKSPACE 环境变量, 缺省脚本所在目录
    # (旧版硬编码 '/workspace/predictions' 为Linux路径)
    predictions_dir = os.path.join(os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__)), 'predictions')
    os.makedirs(predictions_dir, exist_ok=True)

    # 获取日期+周几作为文件名(避免不同周几同号比赛冲突)
    pred_dates = sorted(set(m['match_date'] for m in meta.values()))
    date_tag = pred_dates[0].replace('-', '') if pred_dates else time.strftime('%Y%m%d')
    # 提取周几前缀(如"周六"), 加入文件名避免跨周几冲突
    weekday_prefixes = set(k[:2] for k in results.keys() if k.startswith('周'))
    wd_tag = '_'.join(sorted(weekday_prefixes)) if weekday_prefixes else ''
    pred_file = os.path.join(predictions_dir, f'pred_{date_tag}_{wd_tag}.json' if wd_tag else f'pred_{date_tag}.json')

    # ===== 保存预测结果到文件 (Ultra 8.1: 按模式区分) =====
    existing_history = []
    update_changes = []  # 更新模式的数据变化记录

    if PRED_MODE == 'predict':
        # ---- predict 模式: 全新预测, 不读取已有结果做增量合并 ----
        # 保留旧文件的版本历史 (不丢失版本追踪), 但不合并旧结果
        if os.path.exists(pred_file):
            try:
                with open(pred_file, 'r', encoding='utf-8') as f:
                    old_data = json.load(f)
                existing_history = old_data.get('history', [])
                old_count = len(old_data.get('results', {}))
                # 记录版本历史
                existing_history.append({
                    'saved_at': old_data.get('saved_at', ''),
                    'mode': old_data.get('mode', 'unknown'),
                    'match_count': old_count,
                    'archived_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                })
                print(f"  [预测] 已有文件({old_count}场)归档到历史, 创建全新预测文件")
            except Exception as e:
                print(f"  [预测] 读取旧文件历史失败({e}), 创建全新文件")

        pred_data = {
            'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'mode': 'predict',
            'update_count': 0,  # 全新预测: 更新次数从0开始
            'meta': meta,
            'results': results,
            'cache': cache,
            'history': existing_history,
        }

    else:
        # ---- update 模式: 加载上次预测, 比对数据变化, 调整后合并 ----
        prev_results = {}
        prev_meta = {}
        prev_cache = {}
        prev_data = None
        if os.path.exists(pred_file):
            try:
                with open(pred_file, 'r', encoding='utf-8') as f:
                    prev_data = json.load(f)
                prev_results = prev_data.get('results', {})
                prev_meta = prev_data.get('meta', {})
                prev_cache = prev_data.get('cache', {})
                existing_history = prev_data.get('history', [])
                print(f"  [更新] 加载上次预测: {len(prev_results)}场 (保存于{prev_data.get('saved_at','')})")
            except Exception as e:
                print(f"  [更新] 读取上次预测失败({e}), 降级为全新预测")
        else:
            print(f"  [更新] 未找到上次预测文件, 降级为全新预测")

        # 比对数据变化并调整预测
        if prev_results:
            results, update_changes = compare_and_adjust_for_update(prev_results, results)
            if update_changes:
                print(f"  [更新] 检测到 {len(update_changes)} 处数据变化:")
                for line in update_changes:
                    print(line)
            else:
                print(f"  [更新] 无显著数据变化 (赔率变动均<5%)")

        # 合并: 保留上次未更新的场次 + 本次新结果
        merged_results = prev_results.copy()
        merged_results.update(results)
        merged_meta = prev_meta.copy()
        merged_meta.update(meta)
        merged_cache = prev_cache.copy()
        merged_cache.update(cache)

        # 记录版本历史
        existing_history.append({
            'saved_at': prev_data.get('saved_at', '') if prev_data else '',
            'mode': 'update',
            'match_count': len(prev_results),
            'changes': len(update_changes),
            'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        })

        # 更新计数器: 从上次文件读取, 无则首次更新记为1
        prev_update_count = prev_data.get('update_count', 0) if prev_data else 0
        update_count = prev_update_count + 1

        results = merged_results
        meta = merged_meta
        cache = merged_cache

        pred_data = {
            'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'mode': 'update',
            'update_count': update_count,  # 第N次更新
            'meta': meta,
            'results': results,
            'cache': cache,
            'history': existing_history,
            'update_changes': update_changes,
        }

    # Ultra 11.20: 版本全量归档 — 覆盖前把当前文件完整快照归档, 防止历史版本丢失
    try:
        from version_archive import archive_before_save
        archive_before_save(pred_file, pred_data, expected_keys=MATCH_NUMBERS)
    except Exception as _ve:
        print(f"  [版本归档] ⚠️ 归档失败(不影响保存): {_ve}")

    with open(pred_file, 'w', encoding='utf-8') as f:
        json.dump(pred_data, f, ensure_ascii=False, indent=1)
    _uc = pred_data.get('update_count', 0)
    print(f"  已保存: {pred_file} (共{len(results)}场, 含{len(cache)}场缓存, 模式={PRED_MODE}, 第{_uc}次更新)")

    # ===== Phase 5: SWOT 全自动获取+融合 (Ultra 6.5) =====
    # leisu情报为主, 500/nowscore统计数据型情报为备用兜底
    # 获取后自动调用 swot_fusion_v3 融合回预测文件, 全程无需人工提供URL
    if AUTO_SWOT:
        try:
            from swot_auto import fetch_swot_auto
            print("\n  ===== Phase 5: SWOT 自动获取 =====")
            swot_results = fetch_swot_auto(matches, all_data)
            if swot_results:
                from swot_fusion_v3 import fuse_swot_into_predictions
                fuse_swot_into_predictions(pred_file)
        except Exception as ex:
            print(f"  [SWOT] 自动获取/融合失败 (不影响预测结果): {ex}")

    # ===== Phase 6: 自动生成PDF报告 (Ultra 11.2 — 保证PDF与预测场次一致) =====
    # PDF 输出到工作区根目录 (SPORTTERY_WORKSPACE 优先, 无则脚本目录), 便于手机端直接访问
    _pdf_out_dir = os.path.dirname(os.path.abspath(__file__))  # 交付物落地脚本目录(/workspace/sporttery, 用户可见可打开)
    _pdf_basename = os.path.basename(pred_file).replace('.json', '.pdf')
    pdf_path = os.path.join(_pdf_out_dir, _pdf_basename)
    try:
        # 字体由 pdf_fonts 统一回退 (霞鹜文楷→本地fonts/→系统CJK), 无需硬编码/usr/share路径
        import importlib
        gen_pdf = importlib.import_module('gen_pred_pdf')
        gen_pdf.generate_pdf(pred_data, pdf_path)
        print(f"  [PDF] 已自动生成: {pdf_path} ({len(results)}场)")
    except Exception as ex:
        print(f"  [PDF] 自动生成失败 (不影响预测结果, 可手动运行 `python3 gen_pred_pdf.py {pred_file} {pdf_path}`): {ex}")

    # ===== Phase 7: 投注选择显性化指南 (三档: ✅单选/⚠️双选兜底/🚫避开) =====
    # 源自 260811 用户实测复盘(彩票6中2根因): 平局窗口场须改买HHAD覆盖项, 强主场误判场须避开
    try:
        from gen_bet_guide_html import generate as _gen_bet_guide
        _guide = _gen_bet_guide(pred_file)
        if _guide:
            print(f"  [指南] 投注选择指南: {_guide}")
    except Exception as _ge:
        print(f"  [指南] 生成失败(不影响预测): {_ge}")

if __name__ == '__main__':
    main()
