#!/usr/bin/env python3
"""
赛果验证脚本 Ultra 7.11 — 自动化验证预测结果
输入: 体彩日期 + 比赛编号 (如 "周二205,周二206,周二207")
      或日期+编号 (如 "2026-07-22 205,206,207")

流程:
  用户输入 → sporttery比分直播API(zqbfzb,优先) → sporttery赛果API(补充赔率)
  → 500.com(fallback,zqbfzb无比分时) → 加载预测文件 → 自动验证 → PDF报告(手机优化) → 回归分析入库

Ultra 7.11 升级:
  - zqbfzb API(getMatchDataPageListV1)作为主数据源: 比赛结束后立即更新比分
  - 500.com降级为末位fallback: 仅当zqbfzb无比分时才调用
  - 三级数据源: zqbfzb(比分) → sporttery赛果API(赔率/goalLine) → 500.com(fallback)

Ultra 6.1 升级:
  - 贝叶斯更新模型 (Beta-Binomial共轭, 小样本收缩, 可信区间)
  - 概率校准分析 (ECE/MCE, 预测概率vs实际命中率对比)
  - 混淆矩阵分析 (3x3预测vs实际, 精确率/召回率/F1)
  - CUSUM模型漂移检测 (监控准确率随时间变化)
  - Bootstrap置信区间 (非参数估计, 小样本更稳健)
  - 逻辑回归因子分析 (识别影响预测成功率的关键因素)
  - 历史规律反馈升级为层次贝叶斯 (全局先验+联赛/方向后验)

Ultra 6.0 保留:
  - RPS (Ranked Probability Score) 替代Brier
  - Log Loss 交叉熵指标
  - 统计显著性检验 (二项检验)
  - 历史规律反馈闭环 (验证→预测)
"""
import json, re, time, os, sys, sqlite3, math, html
from datetime import datetime, timedelta
import requests
from v215_e2e import stars_to_score
from gen_bet_guide_html import classify, _parse_probs

# ============================================================
# Phase 0: 用户输入
# ============================================================
# 方式1: 直接指定matchNumStr (如 "周六001-003" 或 "周六001,周六002,周六003")
# 方式2: 日期+编号 (如 "2026-07-22 001,002,003")
# 方式3: 6位日期码+编号 (如 "260731001-003" 或 "260731001,002,003")
# 支持范围展开: "周六001-003" 自动展开为 "周六001,周六002,周六003"
# 统一格式: 预测用 "260801周六001-003", 验证用 "周六001-003"
INPUT = "周二205,周二206,周二207"

# 命令行参数覆盖
if len(sys.argv) > 1:
    INPUT = sys.argv[1]

# ★★★ Ultra 7.7: 手动比分覆盖 — 当API未返回比分时, 使用已验证的真实比分 ★★★
# 格式: {"周X编号": "主队比分:客队比分"}
# 数据来源: 必须从可靠来源(懂球帝/flashscore/sporttery官方)确认后填入
# 严禁编造! 仅在API确实未返回数据且比赛已结束时使用
MANUAL_SCORES = {
    "周三005": "0:0",  # 弗鲁米嫩 0-0 巴伊亚 (来源: flashscore/fichajes 2026-07-30, 平局)
    "周三006": "0:4",  # 维多利亚 0-4 帕梅拉斯 (来源: 懂球帝/flashscore 2026-07-30, 半场0-2)
    "周六025": "1:1",  # 博塔弗戈 1-1 弗鲁米嫩塞 (来源: 懂球帝/球迷屋/唯彩 2026-08-09 巴甲第22轮, 平局; 特莱斯破门+伊格纳西奥救主)
    # 注意: 已删除过期的周三001(旧: 弗鲁米嫩1-3瓦斯科达伽马 2026-08-06巴西杯), 避免污染后续周三001场次验证
}

# ============================================================
# Phase 1: Sporttery 赛果API
# ============================================================
SPORTTERY_RESULT_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry"
SPORTTERY_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://www.lottery.gov.cn/jc/zqsgkj/',
    'Accept': 'application/json',
}

# Ultra 7.11: Sporttery比分直播API (zqbfzb页面背后的JSON API)
# 优势: 比赛结束后立即更新比分, 比赛果API(getUniformMatchResultV1)更快
# API: getMatchDataPageListV1.qry?method=all (全部=已完成+进行中+待开)
ZQBFZB_API_URL = "https://webapi.sporttery.cn/gateway/uniform/fb/getMatchDataPageListV1.qry"
# Ultra 7.11.1: 详情比分API (getMatchLiveV1) — 官网 zqbfzb 实际使用的接口
# 修复: getMatchDataPageListV1 在 matchStatus=10(待开奖)/6(直播结束) 时
#       sectionsNo999 常为空, 该接口能返回完整比分(含半场), 作为比分缺失时的补充源
ZQBFZB_LIVE_API_URL = "https://webapi.sporttery.cn/gateway/uniform/fb/getMatchLiveV1.qry"
ZQBFZB_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://www.sporttery.cn/jc/zqbfzb/',
    'Accept': 'application/json',
}

# Ultra-Opt: 通用路径 — 优先 SPORTTERY_WORKSPACE 环境变量, 缺省脚本所在目录
_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
PREDICTIONS_DIR = os.path.join(_WORKSPACE, 'predictions')
# Ultra 12.2: 修复云端路径硬编码 — 云端(GitHub Actions 设 SPORTTERY_WORKSPACE=/workspace)
# 与本地(脚本目录)自适应, 此前固定 '/workspace' 导致本地验证 PDF 生成失败 [Errno 2]
REPORT_DIR = os.environ.get('SPORTTERY_WORKSPACE') or _WORKSPACE
DB_PATH = os.path.join(PREDICTIONS_DIR, 'regression.db')

def _read_pred_keys(filepath, prefix):
    """读取预测文件中以 prefix(如'周五') 开头的 match_key 列表(排序去重)"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return []
    keys = set()
    for container in (data.get('results'), data.get('meta')):
        if isinstance(container, dict):
            for k in container.keys():
                if isinstance(k, str) and k.startswith(prefix):
                    keys.add(k)
    return sorted(keys)


def _discover_keys_from_pred_files(prefix, target_date=None):
    """从所有预测文件中收集以 prefix 开头的 match_key (跨文件去重排序)

    target_date: 若提供, 优先匹配文件名日期一致的预测文件 (pred_{yyyymmdd}_{星期}.json),
                 避免跨周收集历史场次 (如验证 260815 时误收 260808 周六的场次)。
    """
    if not os.path.exists(PREDICTIONS_DIR):
        return []
    pred_files = sorted([f for f in os.listdir(PREDICTIONS_DIR)
                         if f.startswith('pred_') and f.endswith('.json')],
                        reverse=True)
    keys = set()
    # 优先: 文件名日期与目标日期一致 (pred_{yyyymmdd}_{星期}.json)
    if target_date:
        try:
            dt = datetime.strptime(target_date, '%Y-%m-%d')
            weekday_cn = ['一', '二', '三', '四', '五', '六', '日']
            code = dt.strftime('%Y%m%d')
            wd = weekday_cn[dt.weekday()]
            for pf in pred_files:
                if pf.startswith(f'pred_{code}') and f'周{wd}' in pf:
                    keys.update(_read_pred_keys(os.path.join(PREDICTIONS_DIR, pf), prefix))
                    break
        except Exception:
            pass
    # 回退: 扫描所有文件
    if not keys:
        for pf in pred_files:
            keys.update(_read_pred_keys(os.path.join(PREDICTIONS_DIR, pf), prefix))
    return sorted(keys)


def find_match_keys_by_date(target_date, target_nums):
    """从预测文件中按开盘日(businessDate)星期查找match_key (跨天修复)

    关键: matchNumStr(如'周五001')的星期 = 体彩开盘日(businessDate)的星期,
    与比赛实际日期(matchDate)无关。比赛常在次日凌晨开赛(matchDate可+1天),
    因此不能按 meta.match_date 匹配(会漏掉凌晨场次、或错配到相邻周几),
    改为按 matchNumStr 的星期前缀精确匹配。

    Args:
        target_date: 体彩开盘日 '2026-08-14'
        target_nums: 场次编号 ['001','002',...]
    Returns:
        [match_key,...] 按 target_nums 顺序, 或 None
    """
    if not os.path.exists(PREDICTIONS_DIR):
        return None
    try:
        dt = datetime.strptime(target_date, '%Y-%m-%d')
        weekday_cn = ['一', '二', '三', '四', '五', '六', '日']
        prefix = f"周{weekday_cn[dt.weekday()]}"
    except Exception:
        return None

    code = dt.strftime('%Y%m%d')
    pred_files = sorted([f for f in os.listdir(PREDICTIONS_DIR)
                         if f.startswith('pred_') and f.endswith('.json')],
                        reverse=True)

    best = []
    # 优先: 文件名日期与开盘日一致 (pred_{yyyymmdd}_{星期}.json)
    for pf in pred_files:
        if pf.startswith(f'pred_{code}') and f'周{weekday_cn[dt.weekday()]}' in pf:
            best = _read_pred_keys(os.path.join(PREDICTIONS_DIR, pf), prefix)
            if best:
                break
    # 次选: 任意预测文件中含该星期前缀的key (按文件名倒序)
    if not best:
        for pf in pred_files:
            best = _read_pred_keys(os.path.join(PREDICTIONS_DIR, pf), prefix)
            if best:
                break
    if not best:
        return None

    by_num = {re.search(r'(\d{3})$', k).group(1): k for k in best
              if re.search(r'(\d{3})$', k)}
    result = [by_num[n] for n in target_nums if n in by_num]
    return result or None


def expand_range(input_str):
    """展开编号范围表示法 (001-003 → 001,002,003)
    支持格式: 周X001-003, 260731001-003, 001-003
    """
    # 周X001-003 → 周四001,周四002,周四003
    m = re.match(r'(周[一二三四五六日])(\d{3})-(\d{3})', input_str)
    if m:
        prefix = m.group(1)
        start, end = int(m.group(2)), int(m.group(3))
        return ','.join(f"{prefix}{i:03d}" for i in range(start, end + 1))
    # 6位日期码+范围 260731001-003
    m = re.match(r'(\d{6})(\d{3})-(\d{3})', input_str)
    if m:
        dc = m.group(1)
        start, end = int(m.group(2)), int(m.group(3))
        return ','.join(f"{dc}{i:03d}" for i in range(start, end + 1))
    # 裸范围 001-003 (配合日期/周X前缀使用, 如 "2026-08-15 001-016")
    m = re.match(r'^(\d{3})-(\d{3})$', input_str)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        return ','.join(f"{i:03d}" for i in range(start, end + 1))
    return input_str


def parse_input(input_str):
    """解析用户输入, 返回 (match_keys, date_range)
    支持格式:
      '周二205,周二206,周二207'      → match_keys(周X编号), date_range
      '2026-07-22 205,206,207'      → match_keys(自动构造周X), date_range
      '260731001-003'               → 从预测文件反查match_key, date_range
      '260731001,260731002,260731003' → 同上
      '260731 001,002,003'          → 同上

    关键: matchNumStr(如"周三201")基于体彩开盘日期(businessDate)的星期,
    不是比赛实际日期(matchDate)。因此260731(周五)的match_key实际可能是周四001。
    本函数优先从预测文件按比赛日期反查, 确保key正确。
    """
    input_str = input_str.strip()
    # ★ 触发词识别: 剥离 "验证"/"赛果" 前缀 (如 "验证 260814" → "260814", "验证260814" → "260814")
    _trig = re.match(r'^\s*(?:验证|赛果)\s*[:：]?\s*(.*)$', input_str)
    if _trig:
        print("  [触发词] 检测到关键词'验证' → 进入赛果验证流程")
        input_str = _trig.group(1).strip()
    weekdays_map = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6}
    weekday_cn = ['一', '二', '三', '四', '五', '六', '日']

    # 先展开范围表示法
    input_str = expand_range(input_str)

    # 格式4: 纯日期(无编号) — 验证该开盘日全部场次 (如 '260814'/'20260814'/'2026-08-14')
    bare = re.match(r'^(\d{6})$', input_str) or re.match(r'^(\d{8})$', input_str) \
        or re.match(r'^(\d{4}-\d{2}-\d{2})$', input_str)
    if bare:
        g = bare.group(1)
        try:
            if len(g) == 6:
                dt = datetime.strptime(g, '%y%m%d')
            elif len(g) == 8:
                dt = datetime.strptime(g, '%Y%m%d')
            else:
                dt = datetime.strptime(g, '%Y-%m-%d')
        except Exception:
            dt = None
        if dt:
            full_date = dt.strftime('%Y-%m-%d')
            prefix = f"周{weekday_cn[dt.weekday()]}"
            date_range = ((dt - timedelta(days=3)).strftime('%Y-%m-%d'),
                          (dt + timedelta(days=3)).strftime('%Y-%m-%d'))
            # 1) 从预测文件反查该开盘日全部场次 (优先按文件名日期+星期匹配, 避免跨周误收)
            keys = _discover_keys_from_pred_files(prefix, target_date=full_date)
            # 2) 兜底: 从赛果API反查(比赛实际日期可能+1天, 范围放宽到±3天)
            if not keys:
                try:
                    all_res = fetch_match_results(date_range)
                    keys = sorted(k for k in all_res.keys() if k.startswith(prefix))
                except Exception:
                    keys = []
            if keys:
                print(f"  [日期反查] 开盘日 {full_date}({prefix}) 解析到 {len(keys)} 场")
                return keys, date_range
            print(f"  [日期反查] 开盘日 {full_date}({prefix}) 未找到任何场次")
            return [], date_range

    # 格式2: 日期+编号 (日期=体彩开盘日)
    date_match = re.match(r'(\d{4}-\d{2}-\d{2})\s+(.+)', input_str)
    if date_match:
        business_date_str = date_match.group(1)
        # 先展开编号范围 (如 "001-016" → "001,002,...,016"), 再提取三位编号
        num_part = expand_range(date_match.group(2).strip())
        nums = re.findall(r'\d{3}', num_part)
        bd = datetime.strptime(business_date_str, '%Y-%m-%d')
        wd_cn = weekday_cn[bd.weekday()]
        match_keys = [f"周{wd_cn}{num}" for num in nums]
        date_range = ((bd - timedelta(days=3)).strftime('%Y-%m-%d'),
                      (bd + timedelta(days=3)).strftime('%Y-%m-%d'))
        return match_keys, date_range

    # 格式3: 6位日期码+编号 (如 '260731001-003' 经expand_range展开后)
    # 支持: '260731001,002,003' 或 '260731001,260731002,260731003'
    yymmdd_match = re.match(r'(\d{6})(\d{3}(?:[,，]\s*\d{3})*)$', input_str)
    # 也支持 '260731 001,002,003' (空格分隔)
    if not yymmdd_match:
        yymmdd_match = re.match(r'(\d{6})\s+(\d{3}(?:[,，]\s*\d{3})*)$', input_str)
    # 支持 '260731001,260731002,260731003' (重复日期码)
    if not yymmdd_match:
        parts = [p.strip() for p in re.split(r'[,，]', input_str) if p.strip()]
        if len(parts) >= 2 and all(re.match(r'^\d{6}\d{3}$', p) for p in parts):
            yymmdd_match = [True]
            date_code = parts[0][:6]
            nums = [p[6:9] for p in parts]
    else:
        date_code = yymmdd_match.group(1)
        nums = re.findall(r'\d{3}', yymmdd_match.group(2))

    if yymmdd_match:
        # 转成完整日期
        y, m, d = int(date_code[:2]), int(date_code[2:4]), int(date_code[4:6])
        full_date = f"20{y:02d}-{m:02d}-{d:02d}"

        # 从预测文件反查match_key (优先, 不受星期前缀偏移影响)
        pred_keys = find_match_keys_by_date(full_date, nums)
        if pred_keys:
            print(f"  [日期反查] 从预测文件匹配到 {pred_keys}")
            try:
                bd = datetime.strptime(full_date, '%Y-%m-%d')
                date_range = ((bd - timedelta(days=3)).strftime('%Y-%m-%d'),
                              (bd + timedelta(days=3)).strftime('%Y-%m-%d'))
            except:
                date_range = None
            return pred_keys, date_range

        # 降级: 按实际日期推算星期
        try:
            dt = datetime.strptime(full_date, '%Y-%m-%d')
            wd_cn = weekday_cn[dt.weekday()]
            keys = [f"周{wd_cn}{n}" for n in nums]
            print(f"  [日期推算] 无预测文件, 按星期{wd_cn}构造: {keys}")
            date_range = ((dt - timedelta(days=3)).strftime('%Y-%m-%d'),
                          (dt + timedelta(days=3)).strftime('%Y-%m-%d'))
            return keys, date_range
        except:
            return [f"{date_code}{n}" for n in nums], None

    # 格式1: 周X编号 (直接指定matchNumStr)
    keys = [k.strip() for k in re.split(r'[,，]', input_str) if k.strip()]
    today = datetime.now()
    for k in keys:
        m = re.match(r'周([一二三四五六日])(\d{3})', k)
        if m:
            wd = weekdays_map[m.group(1)]
            diff = (today.weekday() - wd) % 7
            business_date = today - timedelta(days=diff)
            date_range = ((business_date - timedelta(days=3)).strftime('%Y-%m-%d'),
                          (business_date + timedelta(days=3)).strftime('%Y-%m-%d'))
            return keys, date_range

    return keys, None


def fetch_match_results(date_range):
    """从sporttery赛果API获取比赛结果 (Ultra 2.0: 增加重试机制)
    返回 {matchNumStr: {match data}}
    """
    if not date_range:
        # 默认查询最近3天
        today = datetime.now()
        date_range = ((today - timedelta(days=2)).strftime('%Y-%m-%d'),
                      today.strftime('%Y-%m-%d'))

    all_results = {}
    page = 1
    while True:
        params = {
            'matchBeginDate': date_range[0],
            'matchEndDate': date_range[1],
            'leagueId': '',
            'pageSize': '30',
            'pageNo': str(page),
            'isFix': '0',
            'matchPage': '1',
            'pcOrWap': '1',
        }
        # Ultra 2.0: 增加重试机制, 避免单次请求失败导致整个验证中断
        r = None
        for attempt in range(3):
            try:
                r = requests.get(SPORTTERY_RESULT_URL, params=params, headers=SPORTTERY_HEADERS, timeout=15)
                if r.status_code == 200:
                    break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise

        if not r or r.status_code != 200:
            print(f"  ⚠️ sporttery赛果API请求失败(page={page})")
            break

        data = r.json()
        val = data.get('value', {})
        results = val.get('matchResult', [])
        if not results:
            break

        for m in results:
            key = m.get('matchNumStr', '')
            match_date = m.get('matchDate', '')
            if key:
                # 跨周去重: 同matchNumStr保留matchDate最新的 (如周一201本周 vs 上周)
                if key not in all_results or match_date > all_results[key].get('matchDate', ''):
                    all_results[key] = m

        total_pages = val.get('pages', 1)
        if page >= total_pages:
            break
        page += 1

    return all_results


def fetch_500_results(match_keys):
    """从500.com直播页面获取比分数据(sporttery API无比分时的备用方案)
    返回 {matchNumStr: {score data}}
    """
    HEADERS_500 = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    }
    try:
        r = requests.get('https://live.500.com/', headers=HEADERS_500, timeout=15)
        r.encoding = 'gb2312'
        html = r.text
    except Exception as e:
        print(f"  500.com请求失败: {e}")
        return {}

    results = {}
    for key in match_keys:
        idx = html.find(key)
        if idx == -1:
            continue
        # 取该位置前后上下文
        chunk = html[max(0, idx-200):idx+3000]
        clean = re.sub(r'<[^>]+>', '|', chunk)
        clean = re.sub(r'\|+', '|', clean)
        clean = clean.replace('&nbsp;', ' ')
        clean = re.sub(r'\s+', ' ', clean).strip()

        # 比分格式: |3|-|0| (管道符分隔)
        # 在goalLine (如(-1)/(+2)等) 后面找比分
        # 支持: 单位整数(-1/+1), 多位整数(-2/+2), 小数(-1.5/+0.5)
        gl_match_tmp = None  # 备用模式3的goalLine锚点(用于换算半场比分搜索偏移)
        score_match = re.search(r'\([+\-]\d+(?:\.\d)?\).*?\|(\d+)\|-?\|(\d+)\|', clean)
        if not score_match:
            # 备用: 找 |数字|-|数字| 模式
            score_match = re.search(r'\|(\d+)\|-\|(\d+)\|', clean)
        if not score_match:
            # 再备用: 找 数字-数字 模式 (跳过日期)
            # 日期格式 07-24, 比分在goalLine之后
            gl_match_tmp = re.search(r'\([+\-]\d+(?:\.\d)?\)', clean)
            if gl_match_tmp:
                after_gl = clean[gl_match_tmp.start():]
                score_match = re.search(r'(\d+)\s*-\s*(\d+)', after_gl)

        if not score_match:
            print(f"  500.com: {key} 未匹配到比分")
            continue

        home_score = int(score_match.group(1))
        away_score = int(score_match.group(2))

        # 解析队名: 在goalLine前面找 (支持小数盘口如-1.5/+0.5)
        gl_match = re.search(r'\|([^\|]+)\|(\([+\-]\d+(?:\.\d)?\))', clean)
        home = gl_match.group(1).strip() if gl_match else ''
        # 去除队名前的数字(排名)
        home = re.sub(r'^\d+', '', home).strip()

        # 客队: 比分后面
        away_match = re.search(r'\|(\d+)\|-?\|(\d+)\|\s*\|([^\|]+)', clean)
        if not away_match:
            away_match = re.search(r'(\d+)\s*-\s*(\d+)\s*\|([^\|]+)', clean)
        away = away_match.group(3).strip() if away_match else ''
        away = re.sub(r'^\d+', '', away).strip()

        # goalLine (支持: -1, +1, -2, +2, -1.5, +0.5 等)
        gl_str_match = re.search(r'\(([+\-]\d+(?:\.\d)?)\)', clean)
        goal_line = float(gl_str_match.group(1)) if gl_str_match else 0.0

        # 半场比分: 在全场比分(goalLine之后)附近定位, 避免命中日期(如 07-24)
        # M18修复: 原 re.findall(r'(\d+)\s*-\s*(\d+)', clean) 会命中日期格式;
        # 现改为在全场比分结束后的小窗口内搜索, 优先 "X:Y" 格式, 其次严格 "X - Y"(带空格)
        half_home = half_away = 0
        if score_match:
            # 备用模式3的score_match偏移是相对after_gl的, 需加回goalLine起始偏移
            half_abs_end = score_match.end() + (gl_match_tmp.start() if gl_match_tmp else 0)
            half_region = clean[half_abs_end:half_abs_end + 300]
            half_match = re.search(r'(\d+)\s*:\s*(\d+)', half_region)
            if not half_match:
                # 严格模式: 破折号两侧必须有空格, 排除 "07-24" 这类日期
                half_match = re.search(r'(\d+)\s+-\s+(\d+)', half_region)
            if half_match:
                half_home = int(half_match.group(1))
                half_away = int(half_match.group(2))

        # 完场标记
        finished = '完' in clean[:400]

        # HAD结果
        if home_score > away_score:
            had_result = '胜'
        elif home_score == away_score:
            had_result = '平'
        else:
            had_result = '负'

        # HHAD结果
        adjusted_home = home_score + goal_line
        if adjusted_home > away_score:
            hhad_result = '让胜'
        elif adjusted_home == away_score:
            hhad_result = '让平'
        else:
            hhad_result = '让负'

        results[key] = {
            'homeTeam': home,
            'awayTeam': away,
            'home_score': home_score,
            'away_score': away_score,
            'half_home': half_home,
            'half_away': half_away,
            'had_result': had_result,
            'hhad_result': hhad_result,
            'goal_line': goal_line,
            'finished': finished,
            'source': '500.com',
        }
        print(f"  500.com: {key} {home} {home_score}-{away_score} {away} | 半场{half_home}-{half_away} | HAD={had_result} | 完场={finished}")

    return results


def _fetch_live_score(match_id):
    """Ultra 7.11.1: 用 getMatchLiveV1 获取单场完整比分(含半场)。

    fix: getMatchDataPageListV1 在 matchStatus=10(待开奖)/6(直播结束) 时
         sectionsNo999 常为空, 官网 zqbfzb 实际用 getMatchLiveV1 展示比分。
    返回 {'sectionsNo999','sectionsNo1','matchStatus','matchStatusName'} 或 None。
    """
    if not match_id:
        return None
    try:
        r = requests.get(ZQBFZB_LIVE_API_URL,
                        params={'matchIds': str(match_id), 'eventTc': 'goals,penalty_shootout',
                                'method': 'live'},
                        headers=ZQBFZB_HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    if data.get('errorCode') != '0':
        return None
    for item in data.get('value', []):
        if item.get('matchId') == match_id:
            return {
                'sectionsNo999': item.get('sectionsNo999', ''),
                'sectionsNo1': item.get('sectionsNo1', ''),
                'matchStatus': item.get('matchStatus', ''),
                'matchStatusName': item.get('matchStatusName', ''),
            }
    return None


def fetch_zqbfzb_results(match_keys):
    """从sporttery比分直播页面API获取赛果 (主数据源, Ultra 7.11)

    数据源: https://www.sporttery.cn/jc/zqbfzb/ 页面背后的JSON API
    API: getMatchDataPageListV1.qry?method=all (全部=已完成+进行中+待开)

    优势:
    - 体彩官方数据源, 比赛结束后立即更新比分
    - JSON API, 无需HTML解析, 比赛果API(getUniformMatchResultV1)更快更稳定
    - 包含全场比分(sectionsNo999)、半场比分(sectionsNo1)、比赛状态、HAD赔率(未完场时)

    返回 {matchNumStr: {match data}} — 兼容parse_result的sporttery格式
    """
    try:
        r = requests.get(ZQBFZB_API_URL, params={'method': 'all'},
                        headers=ZQBFZB_HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"  zqbfzb API请求失败: HTTP {r.status_code}")
            return {}
        data = r.json()
    except Exception as e:
        print(f"  zqbfzb API请求异常: {e}")
        return {}

    if data.get('errorCode') != '0':
        print(f"  zqbfzb API返回错误: errorCode={data.get('errorCode')}")
        return {}

    match_info_list = data.get('value', {}).get('matchInfoList', [])
    if not match_info_list:
        print("  zqbfzb API: 无比赛数据")
        return {}

    # 建立matchNumStr → match data 映射 (跨日期组)
    all_matches = {}
    for grp in match_info_list:
        for m in grp.get('subMatchList', []):
            key = m.get('matchNumStr', '')
            if key:
                all_matches[key] = m

    # 过滤出目标比赛, 转换为parse_result兼容格式
    results = {}
    for key in match_keys:
        m = all_matches.get(key)
        if not m:
            continue

        score = m.get('sectionsNo999', '')
        half = m.get('sectionsNo1', '')
        status = m.get('matchStatusName', '')

        # Ultra 7.11.1: 当全场比赛比分缺失时, 用 getMatchLiveV1 补充
        # (getMatchDataPageListV1 在 matchStatus=10待开奖/6直播结束 时 sectionsNo999 常为空)
        if ':' not in score:
            live = _fetch_live_score(m.get('matchId'))
            if live:
                score = live.get('sectionsNo999', score)
                half = live.get('sectionsNo1', half)
                if not status:
                    status = live.get('matchStatusName', '')

        result = {
            'matchNumStr': key,
            'homeTeam': m.get('homeTeamAllName') or m.get('homeTeamAbbName', ''),
            'awayTeam': m.get('awayTeamAllName') or m.get('awayTeamAbbName', ''),
            'leagueNameAbbr': m.get('leagueAbbName', ''),
            'matchDate': m.get('matchDate', ''),
            'sectionsNo999': score,
            'sectionsNo1': half,
            'h': m.get('h', ''),
            'd': m.get('d', ''),
            'a': m.get('a', ''),
            'matchResultStatus': status,
            'matchId': m.get('matchId'),
            'source': 'zqbfzb',
        }

        # 从比分推算winFlag (zqbfzb API不返回winFlag)
        if ':' in score:
            parts = score.split(':')
            h_s, a_s = int(parts[0]), int(parts[1])
            result['winFlag'] = 'H' if h_s > a_s else ('D' if h_s == a_s else 'A')
        else:
            result['winFlag'] = ''

        score_display = score if score else '无比分'
        print(f"  zqbfzb: {key} {result['homeTeam']} {score_display} {result['awayTeam']} | 半场{half or 'N/A'} | {status}")

        results[key] = result

    return results


def parse_result(match_data):
    """解析单场比赛结果(支持sporttery API和500.com两种数据源)
    返回 {
        home, away, league,
        home_score, away_score,
        half_home, half_away,
        had_result (胜/平/负),
        hhad_result (让胜/让平/让负),
        goal_line,
        had_odds {h, d, a},
        match_status, source,
    }
    """
    source = match_data.get('source', 'sporttery')

    # 500.com独立数据源: 字段已预解析 (含home_score等字段)
    if source == '500.com' and 'home_score' in match_data:
        total_goals = match_data['home_score'] + match_data['away_score']
        # ★ Ultra 13.6: 未结束比赛(完场=False)标记 data_available=False, 跳过验证
        # (500.com对未开赛/进行中比赛会返回日期如"8-15"而非比分, 误解析为8:15会污染验证)
        _finished = bool(match_data.get('finished', True))
        return {
            'home': match_data.get('homeTeam', ''),
            'away': match_data.get('awayTeam', ''),
            'league': match_data.get('league', ''),
            'home_score': match_data['home_score'],
            'away_score': match_data['away_score'],
            'half_home': match_data.get('half_home', 0),
            'half_away': match_data.get('half_away', 0),
            'had_result': match_data['had_result'],
            'hhad_result': match_data['hhad_result'],
            'goal_line': match_data['goal_line'],
            'had_odds': {'h': 0, 'd': 0, 'a': 0},
            'total_goals': total_goals,
            'match_status': '完' if _finished else '',
            'win_flag': '',
            'data_available': _finished,
            'source': '500.com',
        }

    # sporttery API数据源
    # 全场比分: 优先 sectionsNo999, 缺失时从 winFlag/比分字段推算, 不回退用半场
    # ★★★ Ultra 7.7: 严禁编造数据 — 无比分时标记为None, 绝不默认0-0 ★★★
    full_score = match_data.get('sectionsNo999', '')
    home_score = None  # None = 未获取到真实数据
    away_score = None
    if ':' in full_score:
        parts = full_score.split(':')
        home_score = int(parts[0])
        away_score = int(parts[1])
    else:
        # sectionsNo999 缺失: 尝试从 matchScore 字段获取 (部分API版本)
        match_score = match_data.get('matchScore', '')
        if ':' in match_score:
            parts = match_score.split(':')
            home_score = int(parts[0])
            away_score = int(parts[1])

    # 半场比分: sectionsNo1 是半场, 不应作为全场回退
    half_score = match_data.get('sectionsNo1', '')
    half_home = half_away = 0
    if ':' in half_score:
        parts = half_score.split(':')
        half_home = int(parts[0])
        half_away = int(parts[1])

    # HAD结果 (winFlag: H=主胜, D=平, A=客胜)
    win_flag = match_data.get('winFlag', '')
    had_result = {'H': '胜', 'D': '平', 'A': '负'}.get(win_flag, '')

    # ★★★ Ultra 7.7: 无比分数据时不编造结果, 标记为'数据未获取' ★★★
    if home_score is None or away_score is None:
        # 无比分数据 — 绝不默认0-0, 标记为未获取
        had_result = had_result or '数据未获取'
        hhad_result = '数据未获取'
        total_goals = -1  # -1 表示无数据
        goal_line_str = match_data.get('goalLine', '0')
        try:
            goal_line = float(goal_line_str)
        except:
            goal_line = 0.0
        return {
            'home': match_data.get('homeTeam', ''),
            'away': match_data.get('awayTeam', ''),
            'league': match_data.get('leagueNameAbbr', ''),
            'home_score': None,
            'away_score': None,
            'half_home': half_home,
            'half_away': half_away,
            'had_result': had_result,
            'hhad_result': hhad_result,
            'goal_line': goal_line,
            'had_odds': {
                'h': float(match_data.get('h') or 0),
                'd': float(match_data.get('d') or 0),
                'a': float(match_data.get('a') or 0),
            },
            'total_goals': total_goals,
            'match_status': match_data.get('matchResultStatus', ''),
            'win_flag': win_flag,
            'source': 'sporttery',
            'data_available': False,  # ★ 标记数据不可用
        }

    # 当winFlag为空但有比分时, 从比分推算HAD
    if not had_result and home_score + away_score > 0:
        if home_score > away_score:
            had_result = '胜'
        elif home_score == away_score:
            had_result = '平'
        else:
            had_result = '负'
    # ★★★ 特殊处理: 比分为0-0且winFlag为空时, 仍需判定为平局 ★★★
    if not had_result and home_score == 0 and away_score == 0:
        had_result = '平'

    # HHAD结果 (让球胜平负)
    # goalLine: -1表示主队让1球, +1表示主队受让1球
    goal_line_str = match_data.get('goalLine', '0')
    try:
        goal_line = float(goal_line_str)
    except:
        goal_line = 0.0

    # 让球后的比分: 主队得分 + goalLine (goalLine为负=让球, 正=受让)
    adjusted_home = home_score + goal_line
    if adjusted_home > away_score:
        hhad_result = '让胜'
    elif adjusted_home == away_score:
        hhad_result = '让平'
    else:
        hhad_result = '让负'

    # 总进球
    total_goals = home_score + away_score

    return {
        'home': match_data.get('homeTeam', ''),
        'away': match_data.get('awayTeam', ''),
        'league': match_data.get('leagueNameAbbr', ''),
        'home_score': home_score,
        'away_score': away_score,
        'half_home': half_home,
        'half_away': half_away,
        'had_result': had_result,
        'hhad_result': hhad_result,
        'goal_line': goal_line,
        'had_odds': {
            'h': float(match_data.get('h') or 0),
            'd': float(match_data.get('d') or 0),
            'a': float(match_data.get('a') or 0),
        },
        'total_goals': total_goals,
        'match_status': match_data.get('matchResultStatus', ''),
        'win_flag': win_flag,
        'source': 'sporttery',
        'data_available': True,  # ★ 标记数据可用
    }


def _pred_teams_match(results_data, key, pred_meta):
    """Ultra 13.5: 跨周场次污染防护 — 校验预测与赛果为同一场比赛

    背景: load_predictions 按 match_key(周五003) 跨文件合并, 不同周的同编号
    场次会错位配对 (260814 验证曾用 0807 周五文件的塞伊奈vs赫尔火花 配对
    本周的基尔vs圣保利, had_hit 全错)。用队名相似度拦截:
    同场比赛的不同译名 (埃夫斯堡/埃尔夫斯堡 ≈0.89) 远高于跨场次 (<0.25)。
    """
    try:
        from difflib import SequenceMatcher
        rd = (results_data or {}).get(key) or {}
        # 修复: 赛果字典字段为 homeTeam/awayTeam (zqbfzb/500.com/体彩API), 预测meta为 home/away
        rh = str(rd.get('homeTeam', '') or rd.get('home', '') or '')
        ra = str(rd.get('awayTeam', '') or rd.get('away', '') or '')
        ph = str((pred_meta or {}).get('home', '') or (pred_meta or {}).get('homeTeam', '') or '')
        pa = str((pred_meta or {}).get('away', '') or (pred_meta or {}).get('awayTeam', '') or '')
        if not (rh and ra and ph and pa):
            return True   # 任一侧队名缺失时不拦截 (保持旧行为)
        sim = min(SequenceMatcher(None, rh, ph).ratio(),
                  SequenceMatcher(None, ra, pa).ratio())
        if sim < 0.4:
            print(f"  [跨周防护] {key} 队名不匹配, 跳过 {ph}vs{pa} (赛果: {rh}vs{ra}, sim={sim:.2f})")
            return False
        return True
    except Exception:
        return True


def load_predictions(match_keys, results_data):
    """加载预测文件, 匹配场次
    返回 {match_key: prediction_data}

    Ultra 11.20: 版本完整性锚定 — 若主文件(最新写入)覆盖的场次不足目标场次集
    (说明是部分场次重跑覆盖), 则回退到 version_archive 的"最后一个完整版",
    避免用被部分覆盖的文件做回归, 保证"验证只对最后完整版进行"。
    """
    if not os.path.exists(PREDICTIONS_DIR):
        return {}

    # 收集所有预测文件
    pred_files = sorted([f for f in os.listdir(PREDICTIONS_DIR) if f.startswith('pred_') and f.endswith('.json')],
                        reverse=True)

    predictions = {}
    # 记录每个match_key对应的 (update_count, file) 用于选取最后一次更新
    key_updates = {}

    # ===== Ultra 11.20: 先尝试"最后一个完整版"锚定 =====
    # 目标场次集合 (match_keys 可能是 周日001; 用后3位+前缀宽松匹配)
    _target_last3 = set(k[-3:] for k in match_keys)
    _query_base = None
    _complete_loaded = False

    # 从主文件集合中, 找出最匹配本次验证目标日期的基名
    for pf in pred_files:
        if not pf.startswith('pred_'):
            continue
        # base = pred_20260809_周日 (去掉.json)
        _base = pf.replace('.json', '')
        # 用 results 场次前缀(周X)与目标match_key前缀对齐
        try:
            with open(os.path.join(PREDICTIONS_DIR, pf), 'r', encoding='utf-8') as f:
                _probe = json.load(f)
            _probe_keys = [k for k in (_probe.get('results', {}) or {}).keys() if k.startswith('周')]
        except Exception:
            continue
        if not _probe_keys:
            continue
        # 目标前缀: 取 match_keys 第一个的星期前缀(如前2字符)
        _target_prefix = match_keys[0][:2] if match_keys and len(match_keys[0]) >= 3 else ''
        if _target_prefix:
            _probe_keys = [k for k in _probe_keys if k.startswith(_target_prefix)]
        if not _probe_keys:
            continue
        # 检查该文件是否覆盖目标场次(编号后3位)
        _covered = [k for k in _probe_keys if k[-3:] in _target_last3]
        if not _covered:
            continue
        _query_base = _base
        break

    # 若找到匹配基名, 到归档中取"最后一个完整版"
    if _query_base:
        # ★ Ultra 13.6: 主文件完整覆盖时优先用主文件(最新版), 仅部分覆盖才回退归档
        # (此前无条件用归档旧版, 导致验证基于 v1 首次预测而非最新 update 版)
        _main_file = os.path.join(PREDICTIONS_DIR, _query_base + '.json')
        _main_covered = 0
        try:
            with open(_main_file, 'r', encoding='utf-8') as f:
                _main_data = json.load(f)
            _main_results = _main_data.get('results', {}) or {}
            _main_covered = sum(1 for k in match_keys
                                if k in _main_results or k[-3:] in [kk[-3:] for kk in _main_results])
        except Exception:
            pass
        if _main_covered >= len(match_keys):
            print(f"  [版本锚定] 主文件完整覆盖 {_main_covered}/{len(match_keys)} 场, 用主文件(最新版): {os.path.basename(_main_file)}")
        else:
            try:
                from version_archive import find_last_complete
                _v, _vfile = find_last_complete(PREDICTIONS_DIR, _query_base, expected_keys=sorted(_target_last3))
                if _v and _v.get('snapshot'):
                    _snap = _v['snapshot']
                    _snap_results = _snap.get('results', {}) or {}
                    _snap_meta = _snap.get('meta', {}) or {}
                    _covered_all = [k for k in match_keys if k in _snap_results or k[-3:] in [kk[-3:] for kk in _snap_results]]
                    if len(_covered_all) >= len(match_keys):
                        for key in match_keys:
                            _sk = next((k for k in _snap_results if k == key or k[-3:] == key[-3:]), None)
                            if _sk:
                                # Ultra 13.5: 跨周污染防护 (归档快照同样可能来自其他周)
                                if not _pred_teams_match(results_data, key, _snap_meta.get(_sk, {})):
                                    continue
                                predictions[key] = {
                                    'prediction': _snap_results[_sk],
                                    'meta': _snap_meta.get(_sk, {}),
                                    'file': os.path.basename(_vfile),
                                    'update_count': _v.get('update_count', 0),
                                    'version_seq': _v.get('seq'),
                                    'is_version_snapshot': True,
                                }
                                key_updates[key] = (_v.get('update_count', 0), os.path.basename(_vfile))
                        _complete_loaded = True
                        print(f"  [版本锚定] 用最后完整版 v{_v.get('seq')}({len(_snap_results)}场) 作为验证基准: {os.path.basename(_vfile)}")
            except Exception as _ve:
                print(f"  [版本锚定] ⚠️ 归档读取失败, 回退主文件: {_ve}")

    # 若完整版已锚定全部目标场次, 直接返回 (不再被部分覆盖的最新文件污染)
    if _complete_loaded and len(predictions) >= len(match_keys):
        return predictions

    # ===== 原有逻辑: 主文件(目标日期一致)优先, 其余按 update_count 取最大 =====
    # ★ Ultra 13.6: 跨文件 update_count 无比较意义(各文件独立计数), 历史文件(如
    # pred_20260808_周六 uc=3)会覆盖主文件(uc=1), 导致验证用错周预测。改为:
    #   主文件(_query_base 匹配)的场次一旦加载, 其他文件不得覆盖;
    #   非主文件之间仍按 update_count 取最大(作为主文件缺失场次的回退)。
    for pf in pred_files:
        filepath = os.path.join(PREDICTIONS_DIR, pf)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except:
            continue

        meta = data.get('meta', {})
        results = data.get('results', {})
        # 该文件的更新次数 (predict=0, 每次update+1)
        uc = data.get('update_count', 0 if data.get('mode') == 'predict' else 1)
        # 是否为主文件(文件名基名与目标日期一致)
        is_main = bool(_query_base) and pf.replace('.json', '') == _query_base

        for key in match_keys:
            if key not in results:
                continue
            # Ultra 13.5: 跨周污染防护 — 队名不匹配的场次禁止配对
            if not _pred_teams_match(results_data, key, meta.get(key, {})):
                continue
            # 主文件优先: 主文件场次一旦加载, 其他文件不覆盖
            if key in predictions and not is_main:
                continue
            # 非主文件之间按 update_count 取最大
            if key not in key_updates or is_main or uc > key_updates[key][0]:
                key_updates[key] = (uc, pf)
                predictions[key] = {
                    'prediction': results[key],
                    'meta': meta.get(key, {}),
                    'file': pf,
                    'update_count': uc,
                }

    return predictions


def verify_prediction(pred_data, result_data):
    """验证单场预测
    返回验证结果字典
    """
    pred = pred_data['prediction']
    meta = pred_data['meta']

    # 预测的HAD方向
    pred_had = pred.get('HAD', {})
    pred_had_dir = pred_had.get('dir', '')  # 胜/平/负

    # 预测的HHAD方向
    pred_hhad = pred.get('HHAD', {})
    pred_hhad_dir = pred_hhad.get('dir', '')  # 让胜/让平/让负

    # 实际结果
    actual_had = result_data['had_result']
    actual_hhad = result_data['hhad_result']

    # 验证
    # Ultra 13.5: HAD未开盘/无方向时 had_hit=None (不参与命中率统计, 避免误判为未命中)
    _had_valid = pred_had_dir in ('胜', '平', '负')
    had_hit = (pred_had_dir == actual_had) if (_had_valid and actual_had) else (None if not _had_valid else False)
    # Ultra 13.5: HHAD方向归一化 — 受让胜/让胜→胜, 受让平/让平→平, 受让负/让负→负
    # (受让盘(+1)与让盘(-1)的末字才是方向, 直接字符串比较会误判 007 受让胜=让胜)
    _norm_hhad = lambda s: (s or '')[-1] if s else ''
    _hhad_valid = _norm_hhad(pred_hhad_dir) in ('胜', '平', '负')
    hhad_hit = (_norm_hhad(pred_hhad_dir) == _norm_hhad(actual_hhad)
                if (_hhad_valid and actual_hhad) else (None if not _hhad_valid else False))

    # 比分预测验证 (top3)
    score_pred = pred.get('score', {})
    top3_str = score_pred.get('top3', '')
    actual_score = f"{result_data['home_score']}-{result_data['away_score']}"

    # 解析top3比分
    top3_scores = re.findall(r'(\d+-\d+):\d+\.\d', top3_str)
    score_hit = actual_score in top3_scores if top3_scores else False

    # 总进球验证
    pred_total = score_pred.get('main_dir', '')
    actual_total = result_data['total_goals']
    total_goals_str = str(actual_total)

    # 半全场验证 (Pro 3.2)
    pred_hf = pred.get('half_full', {})
    pred_hf_main = pred_hf.get('main', '')  # 如 "胜胜(35.2%)"
    # 提取预测组合 (前两个字)
    pred_hf_combo = pred_hf_main[:2] if len(pred_hf_main) >= 2 else ''

    # 计算实际半全场结果
    half_home = result_data.get('half_home', 0)
    half_away = result_data.get('half_away', 0)
    if half_home > half_away:
        actual_ht = '胜'
    elif half_home == half_away:
        actual_ht = '平'
    else:
        actual_ht = '负'
    actual_ft = actual_had  # 全场结果 = HAD结果
    actual_hf_combo = f"{actual_ht}{actual_ft}"
    hf_hit = pred_hf_combo == actual_hf_combo if pred_hf_combo and actual_hf_combo else False

    # 总进球数验证 (Pro 3.2: 体彩第三种玩法)
    pred_tg = pred.get('total_goals', {})
    pred_tg_main = pred_tg.get('main', '')  # 如 "3球(23.5%)"
    # 提取预测总进球数 (如 "3球" → "3", "7+球" → "7+")
    pred_tg_combo = ''
    if pred_tg_main:
        m = re.match(r'(\d+\+?)球', pred_tg_main)
        if m:
            pred_tg_combo = m.group(1)

    # 计算实际总进球数类别
    actual_tg_label = ''
    if actual_total >= 7:
        actual_tg_label = '7+'
    else:
        actual_tg_label = str(actual_total)

    tg_hit = pred_tg_combo == actual_tg_label if pred_tg_combo and actual_tg_label else False

    # 检查实际总进球数是否在预测Top3中
    pred_tg_top3_str = pred_tg.get('top3', '')
    pred_tg_top3_labels = []
    if pred_tg_top3_str:
        pred_tg_top3_labels = re.findall(r'(\d+\+?)球', pred_tg_top3_str)
    tg_top3_hit = actual_tg_label in pred_tg_top3_labels if pred_tg_top3_labels and actual_tg_label else False

    # Ultra 6.0: 验证cross_market主推方向
    cross_market = pred.get('cross_market', {})
    primary_bet = cross_market.get('primary_bet') if cross_market else None
    if primary_bet:
        pb_option = primary_bet.get('option', '')
        pb_market = primary_bet.get('market', '')
        # 判断主推方向是否命中
        if pb_market == 'HAD':
            pb_dir = pb_option.replace('HAD', '')
            pb_hit = (pb_dir == actual_had) if pb_dir and actual_had else False
        elif pb_market == 'HHAD':
            pb_dir = pb_option.replace('HHAD', '')
            pb_hit = (pb_dir == actual_hhad) if pb_dir and actual_hhad else False
        elif pb_market == 'HAD双选':
            # 双选命中: 任一方向命中即可
            if '胜平' in pb_option:
                pb_hit = actual_had in ('胜', '平')
            elif '平负' in pb_option:
                pb_hit = actual_had in ('平', '负')
            elif '胜负' in pb_option:
                pb_hit = actual_had in ('胜', '负')
            else:
                pb_hit = False
        elif pb_market == 'HHAD双选':
            # 让球双选命中: 实际让球方向末字(胜/平/负)命中任一选中方向 (Ultra 11.32, 修复漏判)
            _hhad_dir = (actual_hhad or '')[-1]  # 受让胜/让胜→胜, 受让平/让平→平, ...
            if '让胜让平' in pb_option:
                pb_hit = _hhad_dir in ('胜', '平')
            elif '让胜让负' in pb_option:
                pb_hit = _hhad_dir in ('胜', '负')
            elif '让平让负' in pb_option:
                pb_hit = _hhad_dir in ('平', '负')
            else:
                pb_hit = False
        else:
            pb_hit = False
    else:
        pb_hit = False

    result = {
        'key': '',
        'home': result_data['home'],
        'away': result_data['away'],
        'league': result_data['league'],
        'actual_score': f"{result_data['home_score']}-{result_data['away_score']}",
        'half_score': f"{result_data['half_home']}-{result_data['half_away']}",
        'actual_had': actual_had,
        'actual_hhad': actual_hhad,
        'actual_hf': actual_hf_combo,
        'actual_tg': actual_tg_label,
        'goal_line': result_data['goal_line'],
        'total_goals': actual_total,
        'pred_had_dir': pred_had_dir,
        'pred_had_odds': pred_had.get('odds', ''),
        'pred_had_conf': pred_had.get('conf', ''),
        'pred_had_p': pred_had.get('p', ''),
        'pred_hhad_dir': pred_hhad_dir,
        'pred_hhad_odds': pred_hhad.get('odds', ''),
        'pred_hhad_conf': pred_hhad.get('conf', ''),
        'pred_hhad_p': pred_hhad.get('p', ''),
        'pred_hf_combo': pred_hf_combo,
        'pred_hf_main': pred_hf_main,
        'pred_tg_main': pred_tg_main,
        'pred_tg_top3': pred_tg.get('top3', ''),
        'pred_top3': top3_str,
        'pred_score_main': score_pred.get('main_dir', ''),
        'pred_market_gl': score_pred.get('market_gl_str', ''),
        'pred_file': pred_data['file'],
        'had_hit': had_hit,
        'hhad_hit': hhad_hit,
        'score_hit': score_hit,
        'hf_hit': hf_hit,
        'tg_hit': tg_hit,
        'tg_top3_hit': tg_top3_hit,
        'had_odds': result_data['had_odds'],
        'pb_hit': pb_hit,
        'pb_option': primary_bet.get('option', '') if primary_bet else '',
        'pb_market': primary_bet.get('market', '') if primary_bet else '',
        'pb_odds': primary_bet.get('odds', 0) if primary_bet else 0,
        'prediction': pred,
        'difficulty': pred.get('difficulty', 0),
        'model_agreement': pred.get('model_agreement', 0),
    }
    # Pro 3.0: 计算单场投注ROI
    result['roi'] = calculate_roi(result)
    return result


def verify_bet_guide(pred_data, result_data):
    """验证投注指南命中 — 四档(主推) + 首推(补充, PDF primary_bet)

    四档主推 (复刻 gen_bet_guide_html 原四档判定):
      🎯 draw   → 买 HAD 平局
      ✅ single → 买 HAD 主推方向
      ⚠️ cover  → 买 HHAD 覆盖项(让负/受让胜)
      🚫 avoid  → 不买 (hit=None, 不计入分母)
    首推补充 (命中率优先 = 预测PDF primary_bet):
      market=HAD  → 买 HAD 胜/平/负
      market=HHAD → 买 HHAD 让胜/让负/受让胜/...

    返回 {level, bet, hit, primary_market, primary_bet, primary_hit}
    """
    pred = pred_data.get('prediction', {})
    meta = pred_data.get('meta', {})
    had = pred.get('HAD', {})
    hh = pred.get('HHAD', {})
    league = meta.get('league', '')
    handicap = hh.get('handicap')
    had_open = had.get('had_open', True)

    actual_had = result_data.get('had_result', '')
    actual_hhad = result_data.get('hhad_result', '')

    # ===== 四档主推 =====
    if not had_open:
        hh_dir = hh.get('dir', '')
        if hh_dir:
            level, bet = 'single', hh_dir
            hit = (hh_dir == actual_hhad) if actual_hhad else False
        else:
            level, bet, hit = 'avoid', '', None
    else:
        w, dr, l = _parse_probs(had.get('p', '0/0/0'))
        argmax_p = max(w, dr, l)
        level, _reason, _ds, _dsr, _dv, _dvr = classify(dr, argmax_p, w, l, league)
        if level == 'draw':
            bet = '平'
            hit = (actual_had == '平') if actual_had else False
        elif level == 'single':
            bet = had.get('dir', '')
            hit = (bet == actual_had) if bet and actual_had else False
        elif level == 'cover':
            if handicap is not None:
                try:
                    bet = '受让胜' if float(handicap) > 0 else '让负'
                except Exception:
                    bet = hh.get('dir', '')
            else:
                bet = hh.get('dir', '')
            hit = (bet == actual_hhad) if bet and actual_hhad else False
        else:  # avoid
            bet = ''
            hit = None

    # ===== 首推补充 (PDF primary_bet, 命中率优先) =====
    cmb = pred.get('cross_market') or {}
    pb = cmb.get('primary_bet') or {}
    primary_market = pb.get('market', '')
    primary_opt = pb.get('option', '')
    if primary_market == 'HAD':
        primary_bet = primary_opt.replace('HAD', '')
        primary_hit = (primary_bet == actual_had) if actual_had else False
    elif primary_market == 'HHAD':
        primary_bet = primary_opt.replace('HHAD', '')
        primary_hit = (primary_bet == actual_hhad) if actual_hhad else False
    else:
        primary_bet, primary_hit = '', None

    return {'level': level, 'bet': bet, 'hit': hit,
            'primary_market': primary_market, 'primary_bet': primary_bet, 'primary_hit': primary_hit}


# ============================================================
# Phase 4.5: Pro 3.0 高级验证指标
# ============================================================
def calculate_roi(verified_match):
    """计算单场比赛的投注ROI (基于Kelly投注)
    
    如果预测HAD命中: 返回 = (odds - 1) * stake
    如果未命中: 返回 = -stake
    stake使用固定1单位(简化计算)
    """
    pred_odds = verified_match.get('pred_had_odds', 0)
    try:
        pred_odds = float(pred_odds)
    except:
        pred_odds = 0

    if pred_odds <= 0:
        return {'bet': 0, 'return': 0, 'roi': 0, 'profitable': None}

    bet = 1.0  # 固定1单位投注
    if verified_match.get('had_hit'):
        ret = (pred_odds - 1) * bet
        roi = (ret / bet) * 100
    else:
        ret = -bet
        roi = -100.0

    return {
        'bet': bet,
        'return': round(ret, 2),
        'roi': round(roi, 1),
        'profitable': verified_match.get('had_hit', False),
    }


def calibrate_confidence(verified_matches):
    """置信度校准分析 (Pro 3.1: 5星制)
    
    按置信度分为3档校准命中率:
    高 (4.0★+): 应 >= 60%
    中 (2.5-3.5★): 应 >= 45%
    低 (1.0-2.0★): 应 < 50%
    
    返回: {
        'calibration': [
            {'level': '★★★★+', 'total': N, 'hits': N, 'rate': X%, 'expected': '>=60%', 'status': 'good/warn'},
            ...
        ],
        'summary': '...',
    }
    """
    # Pro 3.1: 使用stars_to_score进行分组(兼容旧3星格式)
    conf_groups = {'高(4.0+)': [], '中(2.5-3.5)': [], '低(1.0-2.0)': []}
    for v in verified_matches:
        conf = str(v.get('pred_had_conf', ''))
        score = stars_to_score(conf)
        if score >= 4.0:
            conf_groups['高(4.0+)'].append(v)
        elif score >= 2.5:
            conf_groups['中(2.5-3.5)'].append(v)
        else:
            conf_groups['低(1.0-2.0)'].append(v)

    results = []
    expected_map = {'高(4.0+)': 60, '中(2.5-3.5)': 45, '低(1.0-2.0)': 0}
    label_map = {'高(4.0+)': '★★★★+', '中(2.5-3.5)': '★★~★★★½', '低(1.0-2.0)': '★~★★'}
    for level in ['高(4.0+)', '中(2.5-3.5)', '低(1.0-2.0)']:
        matches = conf_groups[level]
        if not matches:
            continue
        hits = sum(1 for v in matches if v.get('had_hit'))
        rate = hits / len(matches) * 100
        threshold = expected_map[level]
        if level == '低(1.0-2.0)':
            status = 'good' if rate < 50 else 'warn'
        else:
            status = 'good' if rate >= threshold else 'warn'
        results.append({
            'level': label_map[level], 'total': len(matches), 'hits': hits,
            'rate': round(rate, 1), 'expected': f'>={threshold}%' if threshold > 0 else '<50%',
            'status': status,
        })

    # 生成摘要
    if not results:
        summary = '无置信度数据，无法校准。'
    else:
        parts = []
        for r in results:
            tag = '达标' if r['status'] == 'good' else '未达标'
            parts.append(f"{r['level']} {r['hits']}/{r['total']}={r['rate']}%({tag})")
        summary = '；'.join(parts)

    return {'calibration': results, 'summary': summary}


def calculate_rps(verified_matches):
    """Ranked Probability Score (Ultra 6.0 — 替代二元Brier)
    
    RPS是评估有序多分类(负<平<胜)概率预测的proper scoring rule。
    比二元Brier更准确，因为足球1X2有自然顺序。
    
    RPS = (1/(r-1)) * Σ_{i=1}^{r-1} (Σ_{j=1}^{i} p_j - Σ_{j=1}^{i} a_j)^2
    
    其中r=3(胜/平/负), p_j=预测概率, a_j=实际结果(0或1)
    
    参考: Constantinou & Fenton (2012) [$TRAE_REF](https://ideas.repec.org/a/bpj/jqsprt/v8y2012i1n12.html)
    
    返回: {'rps': float, 'log_loss': float, 'interpretation': str}
    """
    rps_scores = []
    log_loss_scores = []
    
    for v in verified_matches:
        p_str = str(v.get('pred_had_p', ''))
        pred_dir = v.get('pred_had_dir', '')
        if not p_str or not pred_dir or p_str == 'N/A':
            continue
        
        try:
            probs = [float(x.strip().rstrip('%')) / 100 for x in p_str.split('/')]
            if len(probs) != 3:
                continue
        except:
            continue
        
        # 实际结果向量: 胜=[1,0,0], 平=[0,1,0], 负=[0,0,1]
        actual_idx = ['胜', '平', '负'].index(pred_dir) if pred_dir in ['胜','平','负'] else -1
        if actual_idx < 0:
            continue
        
        # 只验证实际命中的方向
        if v.get('had_hit'):
            actual = [0, 0, 0]
            actual[actual_idx] = 1
        else:
            # 未命中，需要找到实际结果
            actual_had = v.get('actual_had', '')
            if actual_had in ['胜', '平', '负']:
                actual = [0, 0, 0]
                actual_idx_actual = ['胜', '平', '负'].index(actual_had)
                actual[actual_idx_actual] = 1
            else:
                continue
        
        # RPS计算: 累积概率差
        rps = 0.0
        for i in range(2):  # r-1=2
            cum_pred = sum(probs[:i+1])
            cum_actual = sum(actual[:i+1])
            rps += (cum_pred - cum_actual) ** 2
        rps /= 2.0  # r-1=2
        rps_scores.append(rps)
        
        # Log Loss: -Σ a_j * log(p_j)
        log_loss = 0.0
        for j in range(3):
            if actual[j] > 0:
                log_loss -= actual[j] * math.log(max(probs[j], 1e-10))
        log_loss_scores.append(log_loss)
    
    if not rps_scores:
        return {'rps': None, 'log_loss': None, 'interpretation': '数据不足'}
    
    avg_rps = sum(rps_scores) / len(rps_scores)
    avg_ll = sum(log_loss_scores) / len(log_loss_scores)
    
    if avg_rps < 0.15:
        interp = '优秀'
    elif avg_rps < 0.20:
        interp = '良好'
    elif avg_rps < 0.25:
        interp = '一般'
    else:
        interp = '需改进'
    
    return {
        'rps': round(avg_rps, 4),
        'log_loss': round(avg_ll, 4),
        'interpretation': interp,
        'n_samples': len(rps_scores),
    }


def calculate_significance(hits, total, baseline=0.333):
    """统计显著性检验 — 二项检验 (Ultra 6.0)
    
    判断命中率是否显著高于随机基线(33.3% for 1X2)。
    
    使用正态近似二项检验:
      z = (p - p0) / sqrt(p0*(1-p0)/n)
      p_value = 1 - Φ(z)  (单侧检验)
    
    参数:
      hits: 命中次数
      total: 总场次
      baseline: 随机基线概率 (默认0.333)
    返回:
      {'z_score': float, 'p_value': float, 'significant': bool, 'conclusion': str}
    """
    if total < 10:
        return {'z_score': 0, 'p_value': 1.0, 'significant': False, 
                'conclusion': f'样本量不足({total}<10), 无法判断显著性'}
    
    p = hits / total
    se = math.sqrt(baseline * (1 - baseline) / total)
    if se == 0:
        return {'z_score': 0, 'p_value': 1.0, 'significant': False, 
                'conclusion': '标准误为0'}
    
    z = (p - baseline) / se
    
    # 正态CDF近似 (用erf)
    try:
        from math import erf, sqrt as msqrt
        p_value = 0.5 * (1 + erf(-z / msqrt(2)))
    except:
        p_value = 0.5  # 回退
    
    significant = p_value < 0.05
    
    if significant:
        if p_value < 0.01:
            conclusion = f'高度显著(p={p_value:.3f}), 命中率{p*100:.0f}%显著高于随机{baseline*100:.0f}%'
        else:
            conclusion = f'显著(p={p_value:.3f}), 命中率{p*100:.0f}%高于随机{baseline*100:.0f}%'
    else:
        conclusion = f'不显著(p={p_value:.3f}), 命中率{p*100:.0f}%可能为随机波动'
    
    return {
        'z_score': round(z, 2),
        'p_value': round(p_value, 3),
        'significant': significant,
        'conclusion': conclusion,
    }


# ============================================================
# Ultra 6.1: 高级验证数学模型
# ============================================================

def _beta_ppf(q, a, b, n_iter=200):
    """Beta分布分位函数 — 不完全Beta函数的逆 (二分搜索)
    
    使用继续分数法近似不完全Beta函数I_x(a,b), 然后二分搜索求解分位数。
    精度足够用于95%可信区间估计。
    """
    if a <= 0 or b <= 0:
        return 0.5
    lo, hi = 0.0, 1.0
    for _ in range(n_iter):
        mid = (lo + hi) / 2
        # 用正态近似Beta分布计算CDF
        mean = a / (a + b)
        var = a * b / ((a + b) ** 2 * (a + b + 1))
        std = math.sqrt(max(var, 1e-10))
        z = (mid - mean) / std
        cdf = 0.5 * (1 + math.erf(z / math.sqrt(2)))
        if cdf < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def bayesian_hit_rate(hits, total, prior_alpha=1.0, prior_beta=1.0):
    """贝叶斯更新模型 — Beta-Binomial共轭 (Ultra 6.1)

    用Beta分布建模命中率, 克服小样本下频率估计的不稳定性。
    
    核心思想:
        先验: P(θ) ~ Beta(α₀, β₀)  — α₀=β₀=1为均匀先验
        似然: P(data|θ) ~ Binomial(hits, total, θ)
        后验: P(θ|data) ~ Beta(α₀ + hits, β₀ + misses)  — 共轭更新

    优势:
        1. 小样本下自动收缩(shrinkage)向先验均值, 防止过拟合
        2. 提供可信区间而非仅点估计, 量化不确定性
        3. 可用层次先验(Hierarchical)融合联赛级和全局级信息

    参数:
        hits: 命中次数
        total: 总场次
        prior_alpha: 先验alpha (1.0=均匀先验; 可用全局命中率缩放)
        prior_beta: 先验beta
    返回:
        {'posterior_mean': float, 'ci_lower': float, 'ci_upper': float,
         'shrinkage': float, 'interpretation': str}
    """
    if total == 0:
        return {'posterior_mean': 0.333, 'ci_lower': 0.0, 'ci_upper': 1.0,
                'shrinkage': 1.0, 'interpretation': '无数据, 使用先验'}

    misses = total - hits
    post_alpha = prior_alpha + hits
    post_beta = prior_beta + misses

    # 后验均值 = α/(α+β)
    post_mean = post_alpha / (post_alpha + post_beta)

    # 95%可信区间
    ci_lower = _beta_ppf(0.025, post_alpha, post_beta)
    ci_upper = _beta_ppf(0.975, post_alpha, post_beta)

    # 收缩因子: 后验均值与频率估计的差异程度
    freq_est = hits / total if total > 0 else 0
    shrinkage = abs(post_mean - freq_est) / max(freq_est, 0.01) if freq_est > 0 else 0

    # 解释
    ci_width = ci_upper - ci_lower
    if total >= 30:
        reliability = '高可靠'
    elif total >= 15:
        reliability = '中可靠'
    elif total >= 5:
        reliability = '低可靠(贝叶斯收缩显著)'
    else:
        reliability = '极低(严重依赖先验)'

    interp = f"后验命中率{post_mean*100:.1f}% (95%CI: {ci_lower*100:.0f}%-{ci_upper*100:.0f}%), {reliability}"

    return {
        'posterior_mean': round(post_mean, 4),
        'ci_lower': round(ci_lower, 4),
        'ci_upper': round(ci_upper, 4),
        'shrinkage': round(shrinkage, 4),
        'interpretation': interp,
        'post_alpha': round(post_alpha, 2),
        'post_beta': round(post_beta, 2),
    }


def calibration_analysis(verified_matches, n_bins=5):
    """概率校准分析 (Ultra 6.1)

    将预测按概率分箱, 对比每箱的"预测概率"与"实际命中率"。
    这是验证模型可靠性的核心方法 — 校准良好的模型, 
    "预测70%"的比赛应该真的有约70%的命中率。

    指标:
        1. ECE (Expected Calibration Error): 加权平均校准误差
        2. MCE (Maximum Calibration Error): 最大箱校准误差
        3. 可靠性判定: ECE<0.05=优秀, <0.10=良好, <0.15=一般, ≥0.15=需校准

    应用:
        若系统性地高估概率(ECE高, 预测>实际), 应在预测端做Platt校准
        若特定概率区间偏差大, 可针对该区间调整

    参数:
        verified_matches: 验证结果列表
        n_bins: 概率分箱数 (默认5: 0-20%, 20-40%, 40-60%, 60-80%, 80-100%)
    返回:
        {'bins': [{bin_range, mean_pred, actual_rate, count, gap}],
         'ece': float, 'mce': float, 'reliable': bool, 'bias': str,
         'interpretation': str}
    """
    bins_data = {i: {'preds': [], 'hits': []} for i in range(n_bins)}

    for v in verified_matches:
        p_str = str(v.get('pred_had_p', ''))
        if not p_str or p_str == 'N/A':
            continue
        try:
            probs = [float(x.strip().rstrip('%')) / 100 for x in p_str.split('/')]
            if len(probs) != 3:
                continue
        except:
            continue

        pred_dir = v.get('pred_had_dir', '')
        if pred_dir not in ['胜', '平', '负']:
            continue

        idx = ['胜', '平', '负'].index(pred_dir)
        pred_prob = probs[idx]

        bin_idx = min(int(pred_prob * n_bins), n_bins - 1)
        bins_data[bin_idx]['preds'].append(pred_prob)
        bins_data[bin_idx]['hits'].append(1 if v.get('had_hit') else 0)

    bins_result = []
    total_n = 0
    ece_sum = 0.0
    mce = 0.0

    for i in range(n_bins):
        preds = bins_data[i]['preds']
        hits_list = bins_data[i]['hits']
        n = len(preds)
        if n == 0:
            continue

        mean_pred = sum(preds) / n
        actual_rate = sum(hits_list) / n
        gap = actual_rate - mean_pred
        abs_gap = abs(gap)

        lo = i / n_bins
        hi = (i + 1) / n_bins
        bins_result.append({
            'bin_range': f"{int(lo*100)}%-{int(hi*100)}%",
            'mean_pred': round(mean_pred, 4),
            'actual_rate': round(actual_rate, 4),
            'gap': round(gap, 4),
            'count': n,
        })

        total_n += n
        ece_sum += n * abs_gap
        mce = max(mce, abs_gap)

    ece = ece_sum / total_n if total_n > 0 else 0

    if ece < 0.05:
        reliability = '优秀'
        reliable = True
    elif ece < 0.10:
        reliability = '良好'
        reliable = True
    elif ece < 0.15:
        reliability = '一般'
        reliable = False
    else:
        reliability = '需校准'
        reliable = False

    # 偏差方向
    overall_pred = sum(b['mean_pred'] * b['count'] for b in bins_result) / total_n if total_n > 0 else 0
    overall_actual = sum(b['actual_rate'] * b['count'] for b in bins_result) / total_n if total_n > 0 else 0
    if overall_actual - overall_pred > 0.05:
        bias = '低估概率(保守)'
    elif overall_actual - overall_pred < -0.05:
        bias = '高估概率(激进)'
    else:
        bias = '无系统性偏差'

    interp = f"校准{reliability}(ECE={ece:.3f}), {bias}, 样本{total_n}场"

    return {
        'bins': bins_result,
        'ece': round(ece, 4),
        'mce': round(mce, 4),
        'reliable': reliable,
        'bias': bias,
        'interpretation': interp,
    }


def confusion_matrix_analysis(verified_matches):
    """混淆矩阵分析 (Ultra 6.1)

    构建3x3混淆矩阵: 预测方向(行) × 实际方向(列)。
    揭示系统性预测偏差, 比单纯命中率更深入。

    矩阵解读:
        对角线 = 正确预测
        非对角线 = 错误模式
        例: 预测"胜"但实际"平"的频率高 → 模型高估主场优势

    衍生指标:
        1. 精确率(Precision): 预测某方向时, 实际是该方向的比例
        2. 召回率(Recall): 实际某方向时, 被正确预测的比例
        3. F1: 精确率与召回率的调和平均
        4. 系统性偏差识别: 哪个方向最容易被误判

    参数:
        verified_matches: 验证结果列表
    返回:
        {'matrix': 3x3 dict, 'precision': dict, 'recall': dict, 'f1': dict,
         'worst_confusion': str, 'interpretation': str}
    """
    directions = ['胜', '平', '负']
    matrix = {d: {d2: 0 for d2 in directions} for d in directions}

    for v in verified_matches:
        pred_dir = v.get('pred_had_dir', '')
        actual_had = v.get('actual_had', '')
        if pred_dir in directions and actual_had in directions:
            matrix[pred_dir][actual_had] += 1

    # 计算精确率、召回率、F1
    precision = {}
    recall = {}
    f1 = {}
    for d in directions:
        tp = matrix[d][d]
        pred_total = sum(matrix[d].values())
        actual_total = sum(matrix[d2][d] for d2 in directions)
        precision[d] = tp / pred_total if pred_total > 0 else 0
        recall[d] = tp / actual_total if actual_total > 0 else 0
        if precision[d] + recall[d] > 0:
            f1[d] = 2 * precision[d] * recall[d] / (precision[d] + recall[d])
        else:
            f1[d] = 0

    # 找最大混淆(非对角线最大值)
    worst_conf = ''
    worst_count = 0
    for d in directions:
        for d2 in directions:
            if d != d2 and matrix[d][d2] > worst_count:
                worst_count = matrix[d][d2]
                worst_conf = f"预测'{d}'实际'{d2}'({worst_count}次)"

    # 整体准确率
    total = sum(matrix[d][d2] for d in directions for d2 in directions)
    correct = sum(matrix[d][d] for d in directions)
    accuracy = correct / total if total > 0 else 0

    # 解释
    worst_f1_dir = min(f1, key=f1.get) if f1 else '无'
    interp = f"整体准确率{accuracy*100:.1f}%, 最弱方向'{worst_f1_dir}'(F1={f1.get(worst_f1_dir, 0):.2f})"
    if worst_conf:
        interp += f", 主要误判: {worst_conf}"

    return {
        'matrix': matrix,
        'precision': {d: round(precision[d], 4) for d in directions},
        'recall': {d: round(recall[d], 4) for d in directions},
        'f1': {d: round(f1[d], 4) for d in directions},
        'accuracy': round(accuracy, 4),
        'worst_confusion': worst_conf,
        'total': total,
        'interpretation': interp,
    }


def cusum_drift_detection(verified_stats_list, target_rate=0.5, threshold=3.0):
    """CUSUM模型漂移检测 (Ultra 8.0: 预警升级)

    使用累积和控制图监控预测准确率随时间的变化。
    当模型性能发生漂移(突然下降或上升)时发出警报。

    Ultra 8.0 升级:
      1. 早期预警: CUSUM > 0.15 × threshold 时发出预警 (提前干预)
      2. 连续低命中检测: 连续3批命中率<40%时触发重标定建议
      3. 输出可操作建议 (降权Power/Elo源, 触发重标定)

    算法:
        对每批验证数据, 计算偏差 = target_rate - actual_rate
        CUSUM_pos = max(0, CUSUM_pos_prev + deviation)   — 检测下降
        CUSUM_neg = min(0, CUSUM_neg_prev + deviation)   — 检测上升
        若 |CUSUM| > threshold × sigma → 漂移警报

    参数:
        verified_stats_list: 按时间排序的验证统计列表 (from DB, 每条含verify_date, had_rate, has_pred)
        target_rate: 目标准确率 (默认50%, 体彩1X2合理水平)
        threshold: 控制限倍数 (3.0 = 3 sigma, 工业标准)
    返回:
        {'cusum_pos': float, 'cusum_neg': float, 'drift_detected': bool,
         'drift_direction': str, 'drift_point': str, 'interpretation': str,
         'early_warning': bool, 'consecutive_low': bool, 'recommendation': str}
    """
    if not verified_stats_list or len(verified_stats_list) < 3:
        return {'cusum_pos': 0, 'cusum_neg': 0, 'drift_detected': False,
                'drift_direction': '无', 'drift_point': '',
                'interpretation': '数据不足(需≥3批验证)',
                'early_warning': False, 'consecutive_low': False, 'recommendation': ''}

    # 计算标准差估计 (用历史数据)
    rates = [s.get('had_rate', 0) / 100 for s in verified_stats_list if s.get('had_rate') is not None]
    if len(rates) < 2:
        sigma = 0.1  # 默认估计
    else:
        mean_r = sum(rates) / len(rates)
        sigma = math.sqrt(sum((r - mean_r) ** 2 for r in rates) / len(rates))
    sigma = max(sigma, 0.05)  # 下限5%

    cusum_pos = 0.0
    cusum_neg = 0.0
    control_limit = threshold * sigma
    # Ultra 8.0: 早期预警线 = 控制限的50% (如控制限0.30则预警线0.15)
    early_warning_limit = control_limit * 0.50
    drift_detected = False
    drift_direction = '无'
    drift_point = ''
    early_warning = False

    for s in verified_stats_list:
        actual_rate = (s.get('had_rate', 0) or 0) / 100
        deviation = target_rate - actual_rate
        cusum_pos = max(0, cusum_pos + deviation)
        cusum_neg = min(0, cusum_neg + deviation)

        if not drift_detected:
            if cusum_pos > control_limit:
                drift_detected = True
                drift_direction = '下降(准确率显著降低)'
                drift_point = s.get('verify_date', '')
            elif abs(cusum_neg) > control_limit:
                drift_detected = True
                drift_direction = '上升(准确率显著提高)'
                drift_point = s.get('verify_date', '')
        # Ultra 8.0: 早期预警 (CUSUM超过预警线但未超控制限)
        if cusum_pos > early_warning_limit and not drift_detected:
            early_warning = True

    # Ultra 8.0: 连续低命中检测 (最近3批命中率均<40%)
    consecutive_low = False
    recent_rates = [(s.get('had_rate', 0) or 0) / 100 for s in verified_stats_list[-3:]]
    if len(recent_rates) >= 3 and all(r < 0.40 for r in recent_rates):
        consecutive_low = True

    # 生成可操作建议
    recommendation = ''
    if drift_detected and '下降' in drift_direction:
        recommendation = '⚠️ 建议降权Power/Elo源(偏向市场赔率), 并触发模型重标定'
    elif consecutive_low:
        recommendation = '⚠️ 连续3批命中率<40%, 建议触发模型重标定(重新标定联赛参数+赔率区间)'
    elif early_warning:
        recommendation = '⚡ CUSUM接近控制限, 建议降权Power/Elo源(增加市场赔率权重)'

    if drift_detected:
        interp = f"检测到模型漂移: {drift_direction}, 起始点: {drift_point}"
        if recommendation:
            interp += f" | {recommendation}"
    elif consecutive_low:
        interp = f"模型稳定(CUSUM={cusum_pos:.2f}/{cusum_neg:.2f}, 控制限={control_limit:.2f}) | {recommendation}"
    elif early_warning:
        interp = f"⚠️ 早期预警(CUSUM={cusum_pos:.2f}, 预警线={early_warning_limit:.2f}, 控制限={control_limit:.2f}) | {recommendation}"
    else:
        interp = f"模型稳定(CUSUM={cusum_pos:.2f}/{cusum_neg:.2f}, 控制限={control_limit:.2f})"

    return {
        'cusum_pos': round(cusum_pos, 4),
        'cusum_neg': round(cusum_neg, 4),
        'drift_detected': drift_detected,
        'drift_direction': drift_direction,
        'drift_point': drift_point,
        'control_limit': round(control_limit, 4),
        'early_warning': early_warning,
        'consecutive_low': consecutive_low,
        'recommendation': recommendation,
        'interpretation': interp,
    }


def bootstrap_confidence_interval(hits, total, n_bootstrap=10000, ci=0.95):
    """Bootstrap置信区间 (Ultra 6.1)

    通过重采样估计命中率的置信区间, 比正态近似更准确,
    尤其在命中率接近0或1、或样本量小时。

    算法:
        1. 构建原始样本: [1]*hits + [0]*misses
        2. 有放回重采样N次, 每次计算命中率
        3. 取第2.5和第97.5百分位为95% CI

    对比正态近似:
        - 正态近似在n<30或p接近0/1时偏差大
        - Bootstrap不依赖分布假设, 更稳健
        - 代价是计算量, 但10000次重采样在Python中<0.1秒

    参数:
        hits: 命中次数
        total: 总场次
        n_bootstrap: 重采样次数
        ci: 置信水平 (0.95 = 95% CI)
    返回:
        {'point_estimate': float, 'ci_lower': float, 'ci_upper': float,
         'ci_width': float, 'interpretation': str}
    """
    if total == 0:
        return {'point_estimate': 0, 'ci_lower': 0, 'ci_upper': 1,
                'ci_width': 1, 'interpretation': '无数据'}

    import random
    random.seed(42)  # 可复现

    point_est = hits / total
    sample = [1] * hits + [0] * (total - hits)

    boot_rates = []
    for _ in range(n_bootstrap):
        resampled = [random.choice(sample) for _ in range(total)]
        boot_rates.append(sum(resampled) / total)

    boot_rates.sort()
    alpha = (1 - ci) / 2
    ci_lower = boot_rates[int(n_bootstrap * alpha)]
    ci_upper = boot_rates[int(n_bootstrap * (1 - alpha))]

    ci_width = ci_upper - ci_lower

    # 解释
    if ci_width < 0.10:
        reliability = '高精度估计'
    elif ci_width < 0.20:
        reliability = '中精度估计'
    else:
        reliability = '低精度(样本量不足)'

    interp = f"命中率{point_est*100:.1f}% (95%CI: {ci_lower*100:.0f}%-{ci_upper*100:.0f}%), {reliability}"

    return {
        'point_estimate': round(point_est, 4),
        'ci_lower': round(ci_lower, 4),
        'ci_upper': round(ci_upper, 4),
        'ci_width': round(ci_width, 4),
        'interpretation': interp,
    }


def logistic_factor_analysis(verified_matches):
    """逻辑回归因子分析 (Ultra 6.1)

    用梯度下降拟合逻辑回归模型, 识别哪些因素显著影响预测命中率。
    这超越了简单的分组统计, 能同时控制多个变量的影响。

    模型:
        P(hit) = σ(β₀ + β₁·odds + β₂·conf_score + β₃·difficulty + β₄·agreement)
        其中 σ(x) = 1/(1+e^(-x))

    输出:
        各因子的回归系数和方向(正/负影响)
        因子重要性排序
        模型整体拟合度(伪R²)

    应用:
        1. 若odds系数显著为负 → 高赔率预测更易错, 应降级
        2. 若difficulty系数显著为负 → 高难度比赛应更保守
        3. 若conf_score系数不显著 → 置信度评级可能不准, 需校准

    参数:
        verified_matches: 验证结果列表
    返回:
        {'coefficients': dict, 'importance': list, 'pseudo_r2': float,
         'interpretation': str}
    """
    # 构建特征矩阵
    features = []
    labels = []
    for v in verified_matches:
        pred = v.get('prediction', {})
        if not isinstance(pred, dict):
            continue

        odds = float(v.get('pred_had_odds', 0) or 0)
        conf_str = str(v.get('pred_had_conf', ''))
        conf_score = stars_to_score(conf_str)
        difficulty = float(pred.get('difficulty', 0) or 0)
        agreement = float(pred.get('model_agreement', 0) or 0)

        if odds <= 0 or conf_score == 0:
            continue

        # 标准化特征
        features.append([odds, conf_score, difficulty, agreement])
        labels.append(1 if v.get('had_hit') else 0)

    n = len(labels)
    if n < 10:
        return {'coefficients': {}, 'importance': [], 'pseudo_r2': 0,
                'interpretation': f'样本不足({n}<10), 无法拟合回归模型'}

    # 标准化 (Z-score) — 先算mean和std, 再一次性标准化
    n_feat = 4
    means = [0] * n_feat
    stds = [1] * n_feat
    for f in features:
        for j in range(n_feat):
            means[j] += f[j]
    for j in range(n_feat):
        means[j] /= n
    for j in range(n_feat):
        var = sum((f[j] - means[j]) ** 2 for f in features) / n
        stds[j] = math.sqrt(max(var, 1e-10))
    for f in features:
        for j in range(n_feat):
            f[j] = (f[j] - means[j]) / max(stds[j], 0.01)

    # 梯度下降
    lr = 0.01
    n_epochs = 500
    beta = [0.0] * (n_feat + 1)  # 截距 + 4个系数

    for epoch in range(n_epochs):
        gradients = [0.0] * (n_feat + 1)
        for i in range(n):
            z = beta[0]
            for j in range(n_feat):
                z += beta[j + 1] * features[i][j]
            pred = 1.0 / (1.0 + math.exp(-max(-50, min(50, z))))
            error = pred - labels[i]
            gradients[0] += error
            for j in range(n_feat):
                gradients[j + 1] += error * features[i][j]
        for j in range(n_feat + 1):
            beta[j] -= lr * gradients[j] / n

    # 计算伪R² (McFadden)
    ll_null = 0.0
    p_mean = sum(labels) / n
    for y in labels:
        if y == 1:
            ll_null += math.log(max(p_mean, 1e-10))
        else:
            ll_null += math.log(max(1 - p_mean, 1e-10))

    ll_model = 0.0
    for i in range(n):
        z = beta[0]
        for j in range(n_feat):
            z += beta[j + 1] * features[i][j]
        pred = 1.0 / (1.0 + math.exp(-max(-50, min(50, z))))
        if labels[i] == 1:
            ll_model += math.log(max(pred, 1e-10))
        else:
            ll_model += math.log(max(1 - pred, 1e-10))

    pseudo_r2 = 1 - ll_model / ll_null if ll_null != 0 else 0

    # 因子名称和方向
    feat_names = ['赔率', '置信度', '难度', '模型一致性']
    coefficients = {}
    importance = []
    for j in range(n_feat):
        name = feat_names[j]
        coef = beta[j + 1]
        coefficients[name] = round(coef, 4)
        direction = '正影响(提高命中率)' if coef > 0 else '负影响(降低命中率)'
        importance.append((name, abs(coef), direction))
    importance.sort(key=lambda x: x[1], reverse=True)

    # 解释
    top_factor = importance[0] if importance else ('无', 0, '')
    interp = f"伪R²={pseudo_r2:.3f}, 最重要因子: {top_factor[0]}({top_factor[2]})"
    if pseudo_r2 < 0.02:
        interp += ', 模型解释力弱(各因子影响不显著)'
    elif pseudo_r2 < 0.10:
        interp += ', 模型有一定解释力'
    else:
        interp += ', 模型解释力强'

    return {
        'coefficients': coefficients,
        'importance': [(name, round(imp, 4), direction) for name, imp, direction in importance],
        'pseudo_r2': round(pseudo_r2, 4),
        'sample_size': n,
        'interpretation': interp,
    }


def calculate_brier_score(verified_matches):
    """计算Brier分数
    
    Brier = (1/N) * Σ (forecast_prob - actual_outcome)^2
    forecast_prob: 预测方向的概率
    actual_outcome: 1(命中)或0(未中)
    
    返回: {'brier': float, 'interpretation': str}
    """
    scores = []
    for v in verified_matches:
        # 从 pred_had_p 提取概率 (格式如 "32%/31%/37%")
        p_str = str(v.get('pred_had_p', ''))
        pred_dir = v.get('pred_had_dir', '')
        if not p_str or not pred_dir:
            continue

        # 提取预测方向对应的概率
        try:
            probs = [float(x.strip().rstrip('%')) / 100 for x in p_str.split('/')]
            dirs = ['胜', '平', '负']
            if pred_dir in dirs:
                idx = dirs.index(pred_dir)
                forecast_prob = probs[idx]
            else:
                continue
        except:
            continue

        actual = 1.0 if v.get('had_hit') else 0.0
        brier = (forecast_prob - actual) ** 2
        scores.append(brier)

    if not scores:
        return {'brier': None, 'interpretation': '数据不足'}

    avg_brier = sum(scores) / len(scores)
    if avg_brier < 0.15:
        interp = '优秀'
    elif avg_brier < 0.25:
        interp = '良好'
    elif avg_brier < 0.33:
        interp = '一般'
    else:
        interp = '需改进'

    return {'brier': round(avg_brier, 4), 'interpretation': interp}


def verify_kelly_bets(verified_matches):
    """验证Kelly投注策略的实际收益
    
    检查预测中标记为value=True的投注是否真的盈利
    """
    kelly_bets = []
    for v in verified_matches:
        # 从prediction中获取kelly数据(Pro 3.0新增)
        pred = v.get('prediction', v)  # 兼容不同数据结构
        kelly_data = None
        if isinstance(pred, dict):
            kelly_data = pred.get('kelly')
        if not kelly_data:
            # 从verified_match中直接获取
            continue

        had_kelly = kelly_data.get('HAD', {})
        if not had_kelly.get('value'):
            continue

        stake_pct = had_kelly.get('stake_pct', 0)
        if stake_pct <= 0:
            continue

        pred_odds = float(v.get('pred_had_odds') or 0)
        if pred_odds <= 0:
            continue

        bet = stake_pct / 100  # 转为比例
        if v.get('had_hit'):
            ret = bet * (pred_odds - 1)
        else:
            ret = -bet

        kelly_bets.append({
            'key': v.get('key', ''),
            'stake': round(bet * 100, 1),
            'odds': pred_odds,
            'hit': v.get('had_hit', False),
            'return': round(ret * 100, 1),
        })

    if not kelly_bets:
        return None

    total_stake = sum(b['stake'] for b in kelly_bets)
    total_return = sum(b['return'] for b in kelly_bets)
    # 修正ROI计算
    roi = (total_return / total_stake * 100) if total_stake > 0 else 0

    return {
        'bets': kelly_bets,
        'total_stake': round(total_stake, 1),
        'total_return': round(total_return, 1),
        'roi': round(roi, 1),
        'profitable': total_return > 0,
    }


HTML_CSS = """
:root {
  --bg: #0f1117; --bg2: #1a1d27; --bg3: #252937;
  --ink: #e8eaed; --muted: #8b92a5; --rule: #2d3142;
  --accent: #00d4aa; --green: #4ade80; --red: #f87171; --yellow: #ffd93d;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  background: var(--bg); color: var(--ink);
  font-family: -apple-system, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
  line-height: 1.7; font-size: 15px;
}
.container { max-width: 900px; margin: 0 auto; padding: 2rem 1.5rem 4rem; }
.hero { text-align: center; padding: 3rem 1rem 2rem; border-bottom: 1px solid var(--rule); margin-bottom: 2.5rem; }
.hero h1 { font-size: 1.8rem; font-weight: 800; margin-bottom: 0.5rem; }
.hero .subtitle { color: var(--muted); font-size: 0.95rem; }
.hero .badge {
  display: inline-block; padding: 0.3rem 1rem;
  background: rgba(0,212,170,0.12); border: 1px solid rgba(0,212,170,0.3);
  border-radius: 100px; color: var(--accent); font-size: 0.85rem; font-weight: 600; margin-top: 0.75rem;
}
.section { margin-bottom: 2.5rem; }
.section h2 {
  font-size: 1.25rem; font-weight: 700; margin-bottom: 1rem;
  padding-bottom: 0.5rem; border-bottom: 1px solid var(--rule);
  display: flex; align-items: center; gap: 0.5rem;
}
.section h2 .num { color: var(--accent); font-family: "SF Mono", monospace; font-size: 0.9rem; }
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
th {
  text-align: left; padding: 0.6rem 0.75rem; color: var(--muted);
  font-weight: 600; font-size: 0.82rem; text-transform: uppercase;
  letter-spacing: 0.03em; border-bottom: 1px solid var(--rule);
}
td { padding: 0.7rem 0.75rem; border-bottom: 1px solid var(--rule); }
tr:hover td { background: rgba(255,255,255,0.02); }
.score { font-family: "SF Mono", monospace; font-weight: 700; font-size: 1rem; }
.hit { color: var(--green); font-weight: 700; }
.miss { color: var(--red); font-weight: 700; }
.tag {
  display: inline-block; padding: 0.15rem 0.5rem; border-radius: 4px;
  font-size: 0.78rem; font-weight: 600;
}
.tag-blue { background: rgba(0,212,170,0.12); color: var(--accent); }
.stat-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 1rem; margin-bottom: 1.5rem;
}
.stat-card {
  background: var(--bg2); border: 1px solid var(--rule); border-radius: 12px;
  padding: 1.2rem 1.25rem; text-align: center;
}
.stat-card .value { font-size: 2rem; font-weight: 800; font-family: "SF Mono", monospace; margin-bottom: 0.25rem; }
.stat-card .label { color: var(--muted); font-size: 0.82rem; }
.stat-card.green .value { color: var(--green); }
.stat-card.red .value { color: var(--red); }
.stat-card.accent .value { color: var(--accent); }
.stat-card.yellow .value { color: var(--yellow); }
.callout {
  background: rgba(0,212,170,0.06); border-left: 3px solid var(--accent);
  border-radius: 0 8px 8px 0; padding: 1rem 1.25rem; margin: 1rem 0; font-size: 0.9rem;
}
.callout.warning { background: rgba(255,217,61,0.06); border-left-color: var(--yellow); }
.callout strong { color: var(--ink); }
.lessons { counter-reset: lesson; }
.lessons li {
  list-style: none; position: relative; padding-left: 2.5rem;
  margin-bottom: 0.75rem; font-size: 0.9rem;
}
.lessons li::before {
  counter-increment: lesson; content: counter(lesson);
  position: absolute; left: 0; top: 0; width: 1.8rem; height: 1.8rem;
  background: var(--bg3); border-radius: 50%; display: flex;
  align-items: center; justify-content: center; font-size: 0.78rem;
  font-weight: 700; color: var(--accent); font-family: "SF Mono", monospace;
}
.match-card {
  background: var(--bg2); border: 1px solid var(--rule); border-radius: 12px;
  padding: 1.5rem; margin-bottom: 1.2rem;
}
.match-card .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
.match-card .match-id { font-family: "SF Mono", monospace; color: var(--accent); font-weight: 700; font-size: 0.85rem; }
.match-card .league { color: var(--muted); font-size: 0.85rem; }
.match-card .teams { font-size: 1.1rem; font-weight: 700; margin-bottom: 0.5rem; }
.match-card .score-row {
  font-size: 1.3rem; font-weight: 800; font-family: "SF Mono", monospace;
  color: var(--yellow); margin-bottom: 0.5rem;
}
.detail-table { font-size: 0.85rem; }
.detail-table th { font-size: 0.78rem; }
.detail-table td { padding: 0.5rem 0.75rem; }
footer { margin-top: 3rem; padding-top: 1.5rem; border-top: 1px solid var(--rule); text-align: center; color: var(--muted); font-size: 0.82rem; }
@media (max-width: 600px) { .stat-grid { grid-template-columns: 1fr 1fr; } .container { padding: 1rem; } .hero h1 { font-size: 1.4rem; } }
"""


def _ultra61_html(cal_analysis, conf_matrix, boot_ci, bayes_overall, logistic_factors, cusum_out, sig_out, rps_out):
    """生成Ultra 6.1高级验证模型的HTML区块"""

    parts = []

    # 1. 贝叶斯估计 + Bootstrap CI
    parts.append('<div class="callout">')
    if bayes_overall:
        parts.append(f'<strong>贝叶斯命中率:</strong> {bayes_overall.get("interpretation", "N/A")}<br>')
        parts.append(f'&nbsp;&nbsp;后验均值={bayes_overall.get("posterior_mean", 0)*100:.1f}%, '
                      f'95%CI=[{bayes_overall.get("ci_lower", 0)*100:.0f}%-{bayes_overall.get("ci_upper", 0)*100:.0f}%], '
                      f'收缩因子={bayes_overall.get("shrinkage", 0):.3f}')
    if boot_ci:
        parts.append(f'<br><strong>Bootstrap CI:</strong> {boot_ci.get("interpretation", "N/A")}')
    parts.append('</div>')

    # 2. 统计显著性
    if sig_out:
        cls = 'callout' if sig_out.get('significant') else 'callout warning'
        parts.append(f'<div class="{cls}"><strong>统计显著性:</strong> {sig_out.get("conclusion", "N/A")}</div>')

    # 3. RPS和Log Loss
    if rps_out and rps_out.get('rps') is not None:
        parts.append(f'<div class="callout"><strong>RPS分数:</strong> {rps_out.get("rps")} | '
                      f'<strong>Log Loss:</strong> {rps_out.get("log_loss")} | {rps_out.get("interpretation", "")}</div>')

    # 4. 校准分析
    if cal_analysis:
        cls = 'callout' if cal_analysis.get('reliable') else 'callout warning'
        parts.append(f'<div class="{cls}"><strong>校准分析:</strong> {cal_analysis.get("interpretation", "N/A")}</div>')
        bins = cal_analysis.get('bins', [])
        if bins:
            parts.append('<table><thead><tr><th>概率区间</th><th>样本数</th><th>预测概率</th>'
                         '<th>实际命中率</th><th>偏差</th></tr></thead><tbody>')
            for b in bins:
                gap_color = 'green' if abs(b['gap']) < 0.05 else ('orange' if abs(b['gap']) < 0.10 else 'red')
                parts.append(f'<tr><td>{b["bin_range"]}</td><td>{b["count"]}</td>'
                             f'<td>{b["mean_pred"]*100:.0f}%</td><td>{b["actual_rate"]*100:.0f}%</td>'
                             f'<td style="color:{gap_color}">{b["gap"]*100:+.0f}%</td></tr>')
            parts.append('</tbody></table>')

    # 5. 混淆矩阵
    if conf_matrix:
        parts.append(f'<div class="callout"><strong>混淆矩阵:</strong> {conf_matrix.get("interpretation", "N/A")}</div>')
        matrix = conf_matrix.get('matrix', {})
        directions = ['胜', '平', '负']
        parts.append('<table><thead><tr><th>预测\\实际</th>')
        for d in directions:
            parts.append(f'<th>{d}</th>')
        parts.append('<th>精确率</th><th>召回率</th><th>F1</th></tr></thead><tbody>')
        for d in directions:
            parts.append(f'<tr><td><strong>{d}</strong></td>')
            for d2 in directions:
                val = matrix.get(d, {}).get(d2, 0)
                cls = 'hit' if d == d2 and val > 0 else ''
                parts.append(f'<td class="{cls}">{val}</td>')
            prec = conf_matrix.get('precision', {}).get(d, 0)
            rec = conf_matrix.get('recall', {}).get(d, 0)
            f1 = conf_matrix.get('f1', {}).get(d, 0)
            parts.append(f'<td>{prec*100:.0f}%</td><td>{rec*100:.0f}%</td><td>{f1*100:.0f}%</td></tr>')
        parts.append('</tbody></table>')

    # 6. 因子分析
    if logistic_factors and logistic_factors.get('importance'):
        parts.append(f'<div class="callout"><strong>因子分析:</strong> {logistic_factors.get("interpretation", "N/A")}</div>')
        parts.append('<table><thead><tr><th>因子</th><th>标准化系数</th><th>影响方向</th></tr></thead><tbody>')
        for name, imp, direction in logistic_factors.get('importance', []):
            parts.append(f'<tr><td>{name}</td><td>{imp:.4f}</td><td>{direction}</td></tr>')
        parts.append('</tbody></table>')

    # 7. CUSUM漂移检测 (Ultra 8.0: 早期预警+连续低命中)
    if cusum_out:
        if cusum_out.get('drift_detected'):
            cls = 'callout warning'
        elif cusum_out.get('early_warning') or cusum_out.get('consecutive_low'):
            cls = 'callout warning'
        else:
            cls = 'callout'
        parts.append(f'<div class="{cls}"><strong>CUSUM漂移检测:</strong> {cusum_out.get("interpretation", "N/A")}</div>')

    return '\n'.join(parts)


def generate_html_report(verified_matches, stats, date_str, brier_result=None, calibration=None, kelly_result=None,
                        cal_analysis=None, conf_matrix=None, boot_ci=None, bayes_overall=None,
                        logistic_factors=None, cusum_out=None, sig_out=None, rps_out=None):
    """生成HTML验证报告
    brier_result / calibration / kelly_result 可由调用方(main)预先计算后传入,
    避免重复计算(终端输出/HTML报告/数据库复用同一份结果); 未传入时回退到本地计算(向后兼容)。
    """
    # 修复: 只统计有预测的场次, 排除无预测的场次
    pred_matches = [v for v in verified_matches
                    if v.get('pred_had_dir') and v['pred_had_dir'] != '无预测']
    total = len(pred_matches) if pred_matches else len(verified_matches)
    had_hits = sum(1 for v in pred_matches if v['had_hit'])
    hhad_hits = sum(1 for v in pred_matches if v['hhad_hit'])
    score_hits = sum(1 for v in pred_matches if v['score_hit'])
    hf_hits = sum(1 for v in pred_matches if v.get('hf_hit'))
    tg_hits = sum(1 for v in pred_matches if v.get('tg_hit'))

    # 生成HTML
    rows_html = []
    for v in verified_matches:
        had_class = 'hit' if v['had_hit'] else 'miss'
        hhad_class = 'hit' if v['hhad_hit'] else 'miss'
        score_class = 'hit' if v['score_hit'] else 'miss'
        hf_class = 'hit' if v.get('hf_hit') else 'miss'
        tg_class = 'hit' if v.get('tg_hit') else 'miss'
        actual_hf = v.get('actual_hf', '')
        pred_hf = v.get('pred_hf_combo', '')
        hf_str = '命中' if v.get('hf_hit') else '未中'
        actual_tg = v.get('actual_tg', '')
        pred_tg = v.get('pred_tg_main', '')
        tg_str = '命中' if v.get('tg_hit') else '未中'
        # Ultra 3.0: 可预测性评分 (difficulty 0-100, 越高越难预测)
        diff = v.get('difficulty')
        difficulty_display = f"{int(diff)}" if isinstance(diff, (int, float)) else '-'
        # Ultra 6.0: 主推命中情况
        pb_option_display = v.get('pb_option', '')
        pb_hit_flag = v.get('pb_hit')
        if pb_option_display:
            pb_class = 'hit' if pb_hit_flag else 'miss'
            pb_text = '命中' if pb_hit_flag else '未中'
            # L8: 对插入HTML的队名/方向做转义, 防止特殊字符破坏结构
            pb_cell = f'<td class="{pb_class}">{html.escape(str(pb_option_display))} {pb_text}</td>'
        else:
            pb_cell = '<td>-</td>'
        rows_html.append(f"""
        <tr>
          <td><span class="tag tag-blue">{html.escape(str(v['key']))}</span></td>
          <td>{html.escape(str(v['home']))} vs {html.escape(str(v['away']))}</td>
          <td class="score">{html.escape(str(v['actual_score']))}</td>
          <td>{html.escape(str(v['actual_had']))}</td>
          <td class="{had_class}">{html.escape(str(v['pred_had_dir']))}</td>
          <td class="{had_class}">{'命中' if v['had_hit'] else '未中'}</td>
          <td>{html.escape(str(v['actual_hhad']))}</td>
          <td class="{hhad_class}">{html.escape(str(v['pred_hhad_dir']))}</td>
          <td class="{hhad_class}">{'命中' if v['hhad_hit'] else '未中'}</td>
          <td>{html.escape(str(actual_tg))}球</td>
          <td class="{tg_class}">{html.escape(str(pred_tg))}</td>
          <td class="{tg_class}">{tg_str}</td>
          <td>{html.escape(str(actual_hf))}</td>
          <td class="{hf_class}">{html.escape(str(pred_hf))}</td>
          <td class="{hf_class}">{hf_str}</td>
          <td>{html.escape(str(difficulty_display))}</td>
          {pb_cell}
        </tr>""")

    had_rate = had_hits / total * 100 if total else 0
    hhad_rate = hhad_hits / total * 100 if total else 0
    score_rate = score_hits / total * 100 if total else 0
    hf_rate = hf_hits / total * 100 if total else 0
    tg_rate = tg_hits / total * 100 if total else 0

    # Pro 3.0: 高级验证指标 (ROI / Brier / 置信度校准 / Kelly)
    bet_matches = [v for v in verified_matches if (v.get('roi') or {}).get('bet', 0) > 0]
    total_bet = sum((v.get('roi') or {}).get('bet', 0) for v in bet_matches)
    total_ret = sum((v.get('roi') or {}).get('return', 0) for v in bet_matches)
    cum_roi = (total_ret / total_bet * 100) if total_bet > 0 else None
    if brier_result is None:
        brier_result = calculate_brier_score(verified_matches)
    brier_val = brier_result.get('brier')
    brier_interp = brier_result.get('interpretation', '')
    if calibration is None:
        calibration = calibrate_confidence(verified_matches)
    if kelly_result is None:
        kelly_result = verify_kelly_bets(verified_matches)

    if cum_roi is None:
        roi_card_class = 'accent'
        roi_value = 'N/A'
    elif cum_roi > 0:
        roi_card_class = 'green'
        roi_value = f"{cum_roi:+.1f}%"
    else:
        roi_card_class = 'red'
        roi_value = f"{cum_roi:+.1f}%"
    brier_value = f"{brier_val}" if brier_val is not None else 'N/A'

    # Pro 3.0: 构建置信度校准表格行
    calib_rows = []
    for r in calibration.get('calibration', []):
        status_cls = 'hit' if r['status'] == 'good' else 'miss'
        status_text = '达标' if r['status'] == 'good' else '未达标'
        calib_rows.append(
            f"<tr><td>{r['level']}</td><td>{r['total']}</td><td>{r['hits']}</td>"
            f"<td>{r['rate']}%</td><td>{r['expected']}</td>"
            f"<td class=\"{status_cls}\">{status_text}</td></tr>"
        )
    calib_rows_html = ''.join(calib_rows) if calib_rows else '<tr><td colspan="6">无置信度数据</td></tr>'

    # Pro 3.0: 构建Kelly验证表格行
    if kelly_result:
        kelly_rows = []
        for b in kelly_result.get('bets', []):
            hit_cls = 'hit' if b.get('hit') else 'miss'
            kelly_rows.append(
                f"<tr><td><span class=\"tag tag-blue\">{b.get('key','')}</span></td>"
                f"<td>{b.get('stake')}</td><td>{b.get('odds')}</td>"
                f"<td class=\"{hit_cls}\">{'命中' if b.get('hit') else '未中'}</td>"
                f"<td class=\"{hit_cls}\">{b.get('return')}</td></tr>"
            )
        kelly_profit_cls = 'green' if kelly_result.get('profitable') else 'red'
        kelly_section_html = f"""
<div class="section">
  <h2><span class="num">04</span> Kelly投注验证</h2>
  <div class="stat-grid">
    <div class="stat-card accent"><div class="value">{kelly_result.get('total_stake')}</div><div class="label">总投注(%)</div></div>
    <div class="stat-card {kelly_profit_cls}"><div class="value">{kelly_result.get('total_return')}</div><div class="label">总收益(%)</div></div>
    <div class="stat-card {kelly_profit_cls}"><div class="value">{kelly_result.get('roi')}%</div><div class="label">Kelly ROI</div></div>
  </div>
  <table>
    <thead><tr><th>场次</th><th>投注(%)</th><th>赔率</th><th>结果</th><th>收益(%)</th></tr></thead>
    <tbody>{''.join(kelly_rows)}</tbody>
  </table>
</div>
"""
    else:
        kelly_section_html = """
<div class="section">
  <h2><span class="num">04</span> Kelly投注验证</h2>
  <div class="callout warning"><strong>无 Kelly 投注数据</strong>: 本次验证未发现标记为 value=True 的Kelly投注。</div>
</div>
"""

    html_output = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>赛果验证报告 Ultra 6.1 — {date_str}</title>
<style>
{HTML_CSS}
</style>
</head>
<body>
<div class="container">

<div class="hero">
  <h1>赛果验证报告 Ultra 6.1</h1>
  <div class="subtitle">{date_str} | Ultra 6.1 策略验证 | 贝叶斯更新 · 校准分析 · 混淆矩阵 · CUSUM漂移检测</div>
  <div class="badge">HAD命中率 {had_hits}/{total} = {had_rate:.0f}%</div>
</div>

<div class="section">
  <h2><span class="num">01</span> 命中率总览</h2>
  <div class="stat-grid">
    <div class="stat-card green">
      <div class="value">{had_hits}/{total}</div>
      <div class="label">HAD命中</div>
    </div>
    <div class="stat-card {'green' if hhad_rate >= 50 else 'red'}">
      <div class="value">{hhad_hits}/{total}</div>
      <div class="label">HHAD命中</div>
    </div>
    <div class="stat-card accent">
      <div class="value">{had_rate:.0f}%</div>
      <div class="label">HAD命中率</div>
    </div>
    <div class="stat-card {'green' if hhad_rate >= 50 else 'red'}">
      <div class="value">{hhad_rate:.0f}%</div>
      <div class="label">HHAD命中率</div>
    </div>
    <div class="stat-card yellow">
      <div class="value">{score_hits}/{total}</div>
      <div class="label">比分命中(Top3)</div>
    </div>
    <div class="stat-card {'green' if hf_rate >= 30 else 'accent'}">
      <div class="value">{hf_hits}/{total}</div>
      <div class="label">半全场命中</div>
    </div>
    <div class="stat-card {'green' if tg_rate >= 30 else 'accent'}">
      <div class="value">{tg_hits}/{total}</div>
      <div class="label">总进球命中</div>
    </div>
    <div class="stat-card {roi_card_class}">
      <div class="value">{roi_value}</div>
      <div class="label">累计ROI(固定1单位)</div>
    </div>
    <div class="stat-card accent">
      <div class="value">{brier_value}</div>
      <div class="label">Brier分数 ({brier_interp})</div>
    </div>
  </div>
  <table>
    <thead>
      <tr>
        <th>场次</th><th>对阵</th><th>比分</th>
        <th>实际HAD</th><th>预测HAD</th><th>验证</th>
        <th>实际HHAD</th><th>预测HHAD</th><th>验证</th>
        <th>实际总进球</th><th>预测总进球</th><th>验证</th>
        <th>实际半全场</th><th>预测半全场</th><th>验证</th>
        <th>可预测性</th><th>主推</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows_html)}
    </tbody>
  </table>
</div>
"""

    # 逐场详细分析
    detail_cards = []
    for v in verified_matches:
        had_tag = 'tag-green' if v['had_hit'] else 'tag-red'
        hhad_tag = 'tag-green' if v['hhad_hit'] else 'tag-red'
        detail_cards.append(f"""
  <div class="match-card">
    <div class="header">
      <span class="match-id">{v['key']} {v['home']} vs {v['away']}</span>
      <span class="league">{v['league']} | 来源: {v['pred_file']}</span>
    </div>
    <div class="teams">{v['home']} {v['actual_score']} {v['away']}</div>
    <div class="score-row">{' : '.join(v['actual_score'].split('-')) if '-' in v['actual_score'] else v['actual_score']}</div>
    <table class="detail-table">
      <thead>
        <tr><th>预测项</th><th>预测</th><th>概率</th><th>赔率</th><th>实际</th><th>结果</th></tr>
      </thead>
      <tbody>
        <tr>
          <td>HAD</td>
          <td>{v['pred_had_dir']}</td>
          <td>{v['pred_had_p']}</td>
          <td>{v['pred_had_odds']}</td>
          <td>{v['actual_had']}</td>
          <td class="{'hit' if v['had_hit'] else 'miss'}">{'命中' if v['had_hit'] else '未中'}</td>
        </tr>
        <tr>
          <td>HHAD(让{int(abs(v['goal_line']))}球)</td>
          <td>{v['pred_hhad_dir']}</td>
          <td>{v['pred_hhad_p']}</td>
          <td>{v['pred_hhad_odds']}</td>
          <td>{v['actual_hhad']}</td>
          <td class="{'hit' if v['hhad_hit'] else 'miss'}">{'命中' if v['hhad_hit'] else '未中'}</td>
        </tr>
        <tr>
          <td>比分Top3</td>
          <td colspan="3">{v['pred_top3']}</td>
          <td>{v['actual_score']}</td>
          <td class="{'hit' if v['score_hit'] else 'miss'}">{'命中' if v['score_hit'] else '未中'}</td>
        </tr>
        <tr>
          <td>盘口</td>
          <td colspan="2">{v['pred_market_gl']}</td>
          <td>总进球: {v['total_goals']}</td>
          <td colspan="2">半场: {v['half_score']}</td>
        </tr>
      </tbody>
    </table>
  </div>""")

    html_output += f"""
<div class="section">
  <h2><span class="num">02</span> 逐场详细分析</h2>
  {''.join(detail_cards)}
</div>

<div class="section">
  <h2><span class="num">03</span> 置信度校准 & Brier分数</h2>
  <div class="callout">
    <strong>Brier分数: {brier_value} ({brier_interp})</strong> — 衡量概率预测准确性, 越低越好(0=完美, 0.33≈随机基准)。
  </div>
  <div class="callout warning">
    <strong>校准摘要:</strong> {calibration.get('summary', '无数据')}
  </div>
  <table>
    <thead><tr><th>置信度</th><th>场次</th><th>命中</th><th>命中率</th><th>预期</th><th>状态</th></tr></thead>
    <tbody>{calib_rows_html}</tbody>
  </table>
</div>
{kelly_section_html}
<div class="section">
  <h2><span class="num">04</span> Ultra 6.1 高级验证模型</h2>
{_ultra61_html(cal_analysis, conf_matrix, boot_ci, bayes_overall, logistic_factors, cusum_out, sig_out, rps_out)}
</div>
<div class="section">
  <h2><span class="num">05</span> 回归分析与教训</h2>
  <div class="callout warning">
    <strong>验证日期: {date_str}</strong><br>
    共验证 {total} 场比赛, HAD命中率 {had_rate:.0f}%, HHAD命中率 {hhad_rate:.0f}%, 比分命中率 {score_rate:.0f}%, 半全场命中率 {hf_rate:.0f}%。
  </div>
  <ol class="lessons">
    {generate_lessons(verified_matches, stats)}
  </ol>
</div>

<footer>
  赛果验证报告 Ultra 6.1 | 贝叶斯更新 · 校准分析 · 混淆矩阵 · CUSUM漂移检测 | {time.strftime('%Y-%m-%d %H:%M:%S')}
</footer>

</div>
</body>
</html>"""
    return html_output


def generate_lessons(verified_matches, stats):
    """根据验证结果自动生成回归分析教训 (Ultra 2.0: 只统计有预测的场次)"""
    lessons = []
    # Ultra 2.0: 只使用有预测的场次, 排除无预测场次对统计的干扰
    pred_matches = [v for v in verified_matches if v.get('pred_had_dir') and v['pred_had_dir'] != '无预测']

    total = len(pred_matches)
    had_hits = sum(1 for v in pred_matches if v['had_hit'])
    hhad_hits = sum(1 for v in pred_matches if v['hhad_hit'])
    score_hits = sum(1 for v in pred_matches if v['score_hit'])

    if total == 0:
        return ''.join(f'<li><strong>本次验证无有效预测数据。</strong></li>')

    # 教训1: 整体命中率
    had_rate = had_hits / total * 100
    hhad_rate = hhad_hits / total * 100
    lessons.append(f"<strong>整体命中率: HAD {had_rate:.0f}% ({had_hits}/{total}), HHAD {hhad_rate:.0f}% ({hhad_hits}/{total})</strong>。"
                   f"比分Top3命中率 {score_hits}/{total}。")

    # 教训2: 低赔率命中分析
    low_odds_matches = [v for v in pred_matches if v['pred_had_odds'] and
                        isinstance(v['pred_had_odds'], (int, float, str)) and
                        _safe_float(v['pred_had_odds']) < 1.8]
    if low_odds_matches:
        low_hits = sum(1 for v in low_odds_matches if v['had_hit'])
        low_rate = low_hits / len(low_odds_matches) * 100
        lessons.append(f"<strong>低赔率(≤1.80)主胜/客胜: {low_hits}/{len(low_odds_matches)} = {low_rate:.0f}%</strong>。"
                       "低赔率方向在本次验证中" + ("表现稳定" if low_rate >= 70 else "存在风险") + "。")

    # 教训3: 高赔率爆冷分析
    high_odds_matches = [v for v in pred_matches if v['pred_had_odds'] and
                         isinstance(v['pred_had_odds'], (int, float, str)) and
                         _safe_float(v['pred_had_odds']) > 2.5]
    if high_odds_matches:
        high_hits = sum(1 for v in high_odds_matches if v['had_hit'])
        high_rate = high_hits / len(high_odds_matches) * 100
        lessons.append(f"<strong>高赔率(>2.50)方向: {high_hits}/{len(high_odds_matches)} = {high_rate:.0f}%</strong>。"
                       "高赔率方向" + ("有价值" if high_rate >= 40 else "风险较高，建议谨慎") + "。")

    # 教训4: HAD与HHAD一致性
    both_correct = sum(1 for v in pred_matches if v['had_hit'] and v['hhad_hit'])
    both_wrong = sum(1 for v in pred_matches if not v['had_hit'] and not v['hhad_hit'])
    lessons.append(f"<strong>HAD与HHAD同时命中: {both_correct}/{total}</strong>，同时未中: {both_wrong}/{total}。"
                   "当HAD和HHAD方向一致时置信度更高。")

    # 教训5: 让球方向分析
    hhad_let_win = [v for v in pred_matches if v['pred_hhad_dir'] == '让胜']
    hhad_let_loss = [v for v in pred_matches if v['pred_hhad_dir'] == '让负']
    hhad_let_draw = [v for v in pred_matches if v['pred_hhad_dir'] == '让平']

    for dir_name, matches_list in [('让胜', hhad_let_win), ('让负', hhad_let_loss), ('让平', hhad_let_draw)]:
        if matches_list:
            hits = sum(1 for v in matches_list if v['hhad_hit'])
            rate = hits / len(matches_list) * 100
            lessons.append(f"<strong>HHAD{dir_name}方向: {hits}/{len(matches_list)} = {rate:.0f}%</strong>。")

    # 教训6: 总进球分析
    total_goals_list = [v['total_goals'] for v in pred_matches]
    if total_goals_list:
        avg_goals = sum(total_goals_list) / len(total_goals_list)
        over_2_5 = sum(1 for g in total_goals_list if g >= 3)
        under_2_5 = sum(1 for g in total_goals_list if g <= 2)
        lessons.append(f"<strong>总进球分析: 场均{avg_goals:.1f}球, 大2.5球 {over_2_5}场, 小2.5球 {under_2_5}场</strong>。")

    # 教训7: 置信度分析 (Pro 3.1: 5星制, 高=4.0★+)
    high_conf = [v for v in pred_matches if stars_to_score(str(v.get('pred_had_conf', ''))) >= 4.0]
    if high_conf:
        hc_hits = sum(1 for v in high_conf if v['had_hit'])
        hc_rate = hc_hits / len(high_conf) * 100
        lessons.append(f"<strong>高置信度(★★★★+)预测: {hc_hits}/{len(high_conf)} = {hc_rate:.0f}%</strong>。"
                       "高置信度预测" + ("可靠性高" if hc_rate >= 70 else "需进一步验证") + "。")

    # Ultra 6.0: 主推命中率
    pb_matches = [v for v in pred_matches if v.get('pb_option')]
    if pb_matches:
        pb_hits = sum(1 for v in pb_matches if v.get('pb_hit'))
        pb_rate = pb_hits / len(pb_matches) * 100
        lessons.append(f"<strong>主推命中率: {pb_hits}/{len(pb_matches)} = {pb_rate:.0f}%</strong>。"
                       "主推方向" + ("可靠性高" if pb_rate >= 60 else "需优化") + "。")

    if not lessons:
        lessons.append("<strong>数据不足，无法生成回归分析。</strong>")

    return ''.join(f'<li>{l}</li>' for l in lessons)


def _safe_float(val):
    """安全转float, 失败返回999"""
    try:
        return float(val)
    except (ValueError, TypeError):
        return 999.0


# ============================================================
# Phase 6: 回归分析数据库
# ============================================================
def safe_alter(conn, table, column, definition):
    """安全添加列(如果已存在则跳过)"""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except:
        pass  # 列已存在


def init_db():
    """初始化SQLite回归数据库"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS verify_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        verify_date TEXT,
        match_key TEXT,
        match_date TEXT,
        home TEXT, away TEXT, league TEXT,
        home_score INTEGER, away_score INTEGER,
        half_home INTEGER, half_away INTEGER,
        had_result TEXT, hhad_result TEXT,
        goal_line REAL, total_goals INTEGER,
        pred_had_dir TEXT, pred_had_odds REAL,
        pred_hhad_dir TEXT, pred_hhad_odds REAL,
        pred_top3 TEXT, pred_score_main TEXT,
        had_hit INTEGER, hhad_hit INTEGER, score_hit INTEGER,
        pred_file TEXT, data_source TEXT,
        created_at TEXT,
        UNIQUE(verify_date, match_key)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS verify_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        verify_date TEXT UNIQUE,
        total INTEGER, has_pred INTEGER,
        had_hits INTEGER, hhad_hits INTEGER, score_hits INTEGER,
        had_rate REAL, hhad_rate REAL, score_rate REAL,
        avg_goals REAL, over25 INTEGER, under25 INTEGER,
        lessons TEXT,
        created_at TEXT
    )''')
    # Pro 3.0: 兼容已有数据库, 安全添加新列
    safe_alter(conn, 'verify_history', 'roi_return', 'REAL')
    safe_alter(conn, 'verify_history', 'brier_score', 'REAL')
    safe_alter(conn, 'verify_stats', 'avg_roi', 'REAL')
    safe_alter(conn, 'verify_stats', 'brier_score', 'REAL')
    # Pro 3.2: 半全场验证列
    safe_alter(conn, 'verify_history', 'hf_hit', 'INTEGER')
    safe_alter(conn, 'verify_history', 'pred_hf_combo', 'TEXT')
    safe_alter(conn, 'verify_history', 'actual_hf', 'TEXT')
    safe_alter(conn, 'verify_stats', 'hf_hits', 'INTEGER')
    safe_alter(conn, 'verify_stats', 'hf_rate', 'REAL')
    # Pro 3.2: 总进球数验证列
    safe_alter(conn, 'verify_history', 'tg_hit', 'INTEGER')
    safe_alter(conn, 'verify_history', 'pred_tg_main', 'TEXT')
    safe_alter(conn, 'verify_history', 'actual_tg', 'TEXT')
    safe_alter(conn, 'verify_stats', 'tg_hits', 'INTEGER')
    safe_alter(conn, 'verify_stats', 'tg_rate', 'REAL')
    # Ultra 6.0: 扩展验证数据列
    safe_alter(conn, 'verify_history', 'pred_had_probs', 'TEXT')  # "55%/28%/17%"
    safe_alter(conn, 'verify_history', 'pred_difficulty', 'REAL')
    safe_alter(conn, 'verify_history', 'pred_model_agreement', 'REAL')
    safe_alter(conn, 'verify_history', 'rps_score', 'REAL')
    safe_alter(conn, 'verify_history', 'log_loss', 'REAL')
    safe_alter(conn, 'verify_history', 'pb_hit', 'INTEGER')
    safe_alter(conn, 'verify_history', 'pb_option', 'TEXT')
    safe_alter(conn, 'verify_history', 'pb_odds', 'REAL')
    safe_alter(conn, 'verify_stats', 'avg_rps', 'REAL')
    safe_alter(conn, 'verify_stats', 'avg_log_loss', 'REAL')
    safe_alter(conn, 'verify_stats', 'pb_hits', 'INTEGER')
    safe_alter(conn, 'verify_stats', 'pb_rate', 'REAL')
    # Ultra 6.1: 修复缺失列 + 新增校准/混淆矩阵列
    safe_alter(conn, 'verify_history', 'pred_had_conf', 'TEXT')
    safe_alter(conn, 'verify_history', 'pred_hhad_conf', 'TEXT')
    safe_alter(conn, 'verify_history', 'pred_had_p', 'TEXT')
    safe_alter(conn, 'verify_history', 'pred_hhad_p', 'TEXT')
    safe_alter(conn, 'verify_history', 'actual_had', 'TEXT')
    safe_alter(conn, 'verify_history', 'actual_hhad', 'TEXT')
    safe_alter(conn, 'verify_history', 'difficulty', 'REAL')
    safe_alter(conn, 'verify_history', 'model_agreement', 'REAL')
    safe_alter(conn, 'verify_stats', 'avg_ece', 'REAL')
    safe_alter(conn, 'verify_stats', 'calibration_reliable', 'INTEGER')
    # Ultra 12.x: 投注指南验证列
    safe_alter(conn, 'verify_history', 'guide_level', 'TEXT')
    safe_alter(conn, 'verify_history', 'guide_market', 'TEXT')
    safe_alter(conn, 'verify_history', 'guide_bet', 'TEXT')
    safe_alter(conn, 'verify_history', 'guide_hit', 'INTEGER')
    safe_alter(conn, 'verify_history', 'primary_market', 'TEXT')
    safe_alter(conn, 'verify_history', 'primary_bet', 'TEXT')
    safe_alter(conn, 'verify_history', 'primary_hit', 'INTEGER')
    safe_alter(conn, 'verify_stats', 'guide_bets', 'INTEGER')
    safe_alter(conn, 'verify_stats', 'guide_hits', 'INTEGER')
    safe_alter(conn, 'verify_stats', 'guide_rate', 'REAL')
    safe_alter(conn, 'verify_stats', 'primary_bets', 'INTEGER')
    safe_alter(conn, 'verify_stats', 'primary_hits', 'INTEGER')
    safe_alter(conn, 'verify_stats', 'primary_rate', 'REAL')
    conn.commit()
    conn.close()


def save_to_db(verified_matches, stats, date_str, lessons_text, brier_result=None):
    """将验证结果保存到回归数据库
    brier_result 可由调用方(main)预先计算后传入, 避免与终端输出/HTML报告重复计算;
    未传入时回退到本地计算(向后兼容)。
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    c = conn.cursor()
    now = time.strftime('%Y-%m-%d %H:%M:%S')

    # 保存逐场记录 (INSERT OR REPLACE: 重复则替换, 依赖 UNIQUE(verify_date, match_key))
    for v in verified_matches:
        # M13: 赛果未获取(占位值)的记录跳过入库 —
        # 否则 actual_score='数据未获取' 被split成['0','0']伪造0-0, total_goals=-1计入均值
        if not v.get('data_available', True) or v.get('actual_score') == '数据未获取':
            continue
        score_parts = v.get('actual_score', '0-0').split('-') if '-' in v.get('actual_score', '') else ['0', '0']
        half_parts = v.get('half_score', '0-0').split('-') if '-' in v.get('half_score', '') else ['0', '0']
        roi_info = v.get('roi') or {}
        roi_return = roi_info.get('return', 0) if roi_info.get('bet', 0) > 0 else None
        c.execute('''INSERT OR REPLACE INTO verify_history
            (verify_date, match_key, match_date, home, away, league,
             home_score, away_score, half_home, half_away,
             had_result, hhad_result, goal_line, total_goals,
             pred_had_dir, pred_had_odds, pred_hhad_dir, pred_hhad_odds,
             pred_top3, pred_score_main,
             had_hit, hhad_hit, score_hit,
             pred_file, data_source, created_at, roi_return)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (date_str, v['key'], date_str,
             v.get('home', ''), v.get('away', ''), v.get('league', ''),
             int(score_parts[0]) if len(score_parts) > 0 and score_parts[0].isdigit() else 0,
             int(score_parts[1]) if len(score_parts) > 1 and score_parts[1].isdigit() else 0,
             int(half_parts[0]) if len(half_parts) > 0 and half_parts[0].isdigit() else 0,
             int(half_parts[1]) if len(half_parts) > 1 and half_parts[1].isdigit() else 0,
             v.get('actual_had', ''), v.get('actual_hhad', ''),
             v.get('goal_line', 0), v.get('total_goals', 0),
             v.get('pred_had_dir', ''), float(v.get('pred_had_odds') or 0),
             v.get('pred_hhad_dir', ''), float(v.get('pred_hhad_odds') or 0),
             v.get('pred_top3', ''), v.get('pred_score_main', ''),
             # Ultra 13.5: 无预测场次 had_hit=None → 入库NULL (不参与命中率统计)
             None if v.get('had_hit') is None else (1 if v.get('had_hit') else 0),
             None if v.get('hhad_hit') is None else (1 if v.get('hhad_hit') else 0),
             None if v.get('score_hit') is None else (1 if v.get('score_hit') else 0),
             v.get('pred_file', ''), v.get('source', '500.com'), now, roi_return))
        # Ultra 6.0: 扩展数据 (概率向量/难度/一致性/RPS/主推)
        pred = v.get('prediction', {})
        had_p = v.get('pred_had_p', '')
        difficulty = pred.get('difficulty', 0) if isinstance(pred, dict) else 0
        model_agree = pred.get('model_agreement', 0) if isinstance(pred, dict) else 0
        # 计算单场RPS
        single_rps = None
        single_ll = None
        if had_p and had_p != 'N/A':
            try:
                probs = [float(x.strip().rstrip('%')) / 100 for x in had_p.split('/')]
                if len(probs) == 3:
                    actual_had = v.get('actual_had', '')
                    if actual_had in ['胜', '平', '负']:
                        actual_idx = ['胜', '平', '负'].index(actual_had)
                        actual = [0, 0, 0]
                        actual[actual_idx] = 1
                        rps_val = 0.0
                        for i in range(2):
                            cum_p = sum(probs[:i+1])
                            cum_a = sum(actual[:i+1])
                            rps_val += (cum_p - cum_a) ** 2
                        rps_val /= 2.0
                        single_rps = round(rps_val, 4)
                        ll_val = -math.log(max(probs[actual_idx], 1e-10))
                        single_ll = round(ll_val, 4)
            except:
                pass
        c.execute('''UPDATE verify_history SET
                     pred_had_probs=?, pred_difficulty=?, pred_model_agreement=?,
                     rps_score=?, log_loss=?, pb_hit=?, pb_option=?, pb_odds=?,
                     pred_had_conf=?, pred_hhad_conf=?, pred_had_p=?, pred_hhad_p=?,
                     actual_had=?, actual_hhad=?, difficulty=?, model_agreement=?
                     WHERE verify_date=? AND match_key=?''',
                  (had_p, difficulty, model_agree,
                   single_rps, single_ll,
                   1 if v.get('pb_hit') else 0, v.get('pb_option', ''),
                   float(v.get('pb_odds') or 0),
                   v.get('pred_had_conf', ''), v.get('pred_hhad_conf', ''),
                   v.get('pred_had_p', ''), v.get('pred_hhad_p', ''),
                   v.get('actual_had', ''), v.get('actual_hhad', ''),
                   v.get('difficulty', 0), v.get('model_agreement', 0),
                   date_str, v['key']))
        # Pro 3.2: 半全场数据 (单独UPDATE, 兼容旧库)
        c.execute('''UPDATE verify_history SET pred_hf_combo=?, actual_hf=?, hf_hit=?
                     WHERE verify_date=? AND match_key=?''',
                  (v.get('pred_hf_combo', ''), v.get('actual_hf', ''),
                   None if v.get('hf_hit') is None else (1 if v.get('hf_hit') else 0), date_str, v['key']))
        # Pro 3.2: 总进球数数据 (单独UPDATE, 兼容旧库)
        c.execute('''UPDATE verify_history SET pred_tg_main=?, actual_tg=?, tg_hit=?
                     WHERE verify_date=? AND match_key=?''',
                  (v.get('pred_tg_main', ''), v.get('actual_tg', ''),
                   None if v.get('tg_hit') is None else (1 if v.get('tg_hit') else 0), date_str, v['key']))
        # Ultra 12.x: 投注指南验证数据 (四档主推 + 首推补充; hit 可为 NULL)
        _gh = v.get('guide_hit')
        _ph = v.get('primary_hit')
        c.execute('''UPDATE verify_history SET guide_level=?, guide_market=?, guide_bet=?, guide_hit=?,
                     primary_market=?, primary_bet=?, primary_hit=?
                     WHERE verify_date=? AND match_key=?''',
                  (v.get('guide_level', ''), v.get('primary_market', ''),
                   v.get('guide_bet', ''),
                   None if _gh is None else (1 if _gh else 0),
                   v.get('primary_market', ''), v.get('primary_bet', ''),
                   None if _ph is None else (1 if _ph else 0),
                   date_str, v['key']))

    # 保存汇总统计
    total = stats.get('total', 0)
    has_pred = stats.get('has_pred', 0)
    had_hits = stats.get('had_hits', 0)
    hhad_hits = stats.get('hhad_hits', 0)
    score_hits = stats.get('score_hits', 0)
    # Ultra 13.5: 命中率分母用有效预测数 (未开盘/无方向场次不计入)
    had_denom = stats.get('had_denom', has_pred)
    hhad_denom = stats.get('hhad_denom', has_pred)
    had_rate = had_hits / had_denom * 100 if had_denom else 0
    hhad_rate = hhad_hits / hhad_denom * 100 if hhad_denom else 0
    score_rate = score_hits / has_pred * 100 if has_pred else 0
    # M13: 计算均值时同样排除赛果未获取的记录(total_goals=-1)
    valid_stats = [v for v in verified_matches
                   if v.get('data_available', True) and v.get('actual_score') != '数据未获取']
    total_goals_list = [v.get('total_goals', 0) for v in valid_stats]
    avg_goals = sum(total_goals_list) / len(total_goals_list) if total_goals_list else 0
    over25 = sum(1 for g in total_goals_list if g >= 3)
    under25 = sum(1 for g in total_goals_list if g <= 2)

    # Pro 3.0: 累计ROI与Brier分数
    bet_matches = [v for v in verified_matches if (v.get('roi') or {}).get('bet', 0) > 0]
    total_bet = sum((v.get('roi') or {}).get('bet', 0) for v in bet_matches)
    total_ret = sum((v.get('roi') or {}).get('return', 0) for v in bet_matches)
    avg_roi = (total_ret / total_bet * 100) if total_bet > 0 else None
    if brier_result is None:
        brier_result = calculate_brier_score(verified_matches)
    brier_score = brier_result.get('brier')

    c.execute('''INSERT OR REPLACE INTO verify_stats
        (verify_date, total, has_pred, had_hits, hhad_hits, score_hits,
         had_rate, hhad_rate, score_rate, avg_goals, over25, under25,
         lessons, created_at, avg_roi, brier_score)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (date_str, total, has_pred, had_hits, hhad_hits, score_hits,
         had_rate, hhad_rate, score_rate, avg_goals, over25, under25,
         lessons_text, now, avg_roi, brier_score))
    # Pro 3.2: 半全场统计 (单独UPDATE, 兼容旧库)
    hf_hits_val = sum(1 for v in verified_matches if v.get('hf_hit'))
    hf_rate_val = hf_hits_val / has_pred * 100 if has_pred else 0
    c.execute('''UPDATE verify_stats SET hf_hits=?, hf_rate=?
                 WHERE verify_date=?''',
              (hf_hits_val, hf_rate_val, date_str))
    # Pro 3.2: 总进球数统计 (单独UPDATE, 兼容旧库)
    tg_hits_val = sum(1 for v in verified_matches if v.get('tg_hit'))
    tg_rate_val = tg_hits_val / has_pred * 100 if has_pred else 0
    c.execute('''UPDATE verify_stats SET tg_hits=?, tg_rate=?
                 WHERE verify_date=?''',
              (tg_hits_val, tg_rate_val, date_str))
    # Ultra 6.0: RPS和主推统计
    rps_result = calculate_rps(verified_matches)
    avg_rps = rps_result.get('rps')
    avg_ll = rps_result.get('log_loss')
    pb_hits_val = sum(1 for v in verified_matches if v.get('pb_hit'))
    pb_rate_val = pb_hits_val / has_pred * 100 if has_pred else 0
    c.execute('''UPDATE verify_stats SET avg_rps=?, avg_log_loss=?, pb_hits=?, pb_rate=?
                 WHERE verify_date=?''',
              (avg_rps, avg_ll, pb_hits_val, pb_rate_val, date_str))
    # Ultra 6.1: 校准分析数据
    cal_data = calibration_analysis(verified_matches)
    ece_val = cal_data.get('ece', 0)
    cal_reliable = 1 if cal_data.get('reliable', False) else 0
    c.execute('''UPDATE verify_stats SET avg_ece=?, calibration_reliable=?
                 WHERE verify_date=?''',
              (ece_val, cal_reliable, date_str))
    # Ultra 12.x: 投注指南统计
    guide_bets_val = stats.get('guide_bets', 0)
    guide_hits_val = stats.get('guide_hits', 0)
    guide_rate_val = (guide_hits_val / guide_bets_val * 100) if guide_bets_val else 0
    c.execute('''UPDATE verify_stats SET guide_bets=?, guide_hits=?, guide_rate=?
                 WHERE verify_date=?''',
              (guide_bets_val, guide_hits_val, guide_rate_val, date_str))
    # Ultra 12.x: 首推补充统计
    primary_bets_val = stats.get('primary_bets', 0)
    primary_hits_val = stats.get('primary_hits', 0)
    primary_rate_val = (primary_hits_val / primary_bets_val * 100) if primary_bets_val else 0
    c.execute('''UPDATE verify_stats SET primary_bets=?, primary_hits=?, primary_rate=?
                 WHERE verify_date=?''',
              (primary_bets_val, primary_hits_val, primary_rate_val, date_str))

    conn.commit()
    conn.close()


def get_historical_stats():
    """从数据库获取历史累计统计"""
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    c = conn.cursor()

    # 累计统计
    c.execute('''SELECT
        COUNT(*) as total_matches,
        SUM(has_pred) as total_pred,
        SUM(had_hits) as total_had,
        SUM(hhad_hits) as total_hhad,
        SUM(score_hits) as total_score,
        AVG(avg_goals) as avg_goals,
        SUM(over25) as total_over25,
        SUM(under25) as total_under25
        FROM verify_stats''')
    row = c.fetchone()

    # 逐场详细统计(有预测的)
    c.execute('''SELECT
        COUNT(*) as total,
        SUM(had_hit) as had_hits,
        SUM(hhad_hit) as hhad_hits,
        SUM(score_hit) as score_hits,
        SUM(hf_hit) as hf_hits,
        SUM(tg_hit) as tg_hits,
        AVG(total_goals) as avg_goals
        FROM verify_history WHERE pred_file != '无' AND pred_file != '' ''')
    detail = c.fetchone()

    # 按HAD方向统计
    c.execute('''SELECT pred_had_dir, COUNT(*), SUM(had_hit)
        FROM verify_history WHERE pred_had_dir != '' AND pred_had_dir != '无预测'
        GROUP BY pred_had_dir''')
    had_dirs = c.fetchall()

    # 按HHAD方向统计
    c.execute('''SELECT pred_hhad_dir, COUNT(*), SUM(hhad_hit)
        FROM verify_history WHERE pred_hhad_dir != '' AND pred_hhad_dir != '无预测'
        GROUP BY pred_hhad_dir''')
    hhad_dirs = c.fetchall()

    # Ultra 6.0: RPS和Log Loss历史
    c.execute('''SELECT AVG(rps_score), AVG(log_loss) 
        FROM verify_history WHERE rps_score IS NOT NULL''')
    rps_row = c.fetchone()
    
    # Ultra 6.0: 主推命中率
    c.execute('''SELECT COUNT(*), SUM(pb_hit) 
        FROM verify_history WHERE pb_option IS NOT NULL AND pb_option != '' ''')
    pb_row = c.fetchone()

    # 最近的验证记录
    c.execute('''SELECT verify_date, total, has_pred, had_hits, hhad_hits, score_hits, had_rate
        FROM verify_stats ORDER BY created_at DESC LIMIT 10''')
    recent_cols = ['verify_date', 'total', 'has_pred', 'had_hits', 'hhad_hits', 'score_hits', 'had_rate']
    recent = [dict(zip(recent_cols, row)) for row in c.fetchall()]
    # 修复: 查询是 created_at DESC(最新在前), 但 CUSUM 漂移检测与连续低命中判断
    # 都要求时间正序(最旧→最新), 需反转; 否则漂移点被记到最新批、连续低命中查的是最旧3批
    recent.reverse()

    conn.close()

    return {
        'total_matches': row[0] or 0,
        'total_pred': row[1] or 0,
        'total_had': row[2] or 0,
        'total_hhad': row[3] or 0,
        'total_score': row[4] or 0,
        'avg_goals': row[5] or 0,
        'total_over25': row[6] or 0,
        'total_under25': row[7] or 0,
        'detail_total': detail[0] or 0,
        'detail_had': detail[1] or 0,
        'detail_hhad': detail[2] or 0,
        'detail_score': detail[3] or 0,
        'detail_hf': detail[4] or 0 if len(detail) > 4 else 0,
        'detail_tg': detail[5] or 0 if len(detail) > 5 else 0,
        'detail_avg_goals': detail[6] or 0 if len(detail) > 6 else 0,
        'total_hf': detail[4] or 0 if len(detail) > 4 else 0,
        'total_tg': detail[5] or 0 if len(detail) > 5 else 0,
        'had_dirs': had_dirs,
        'hhad_dirs': hhad_dirs,
        'recent': recent,
        'avg_rps': rps_row[0] if rps_row and rps_row[0] else None,
        'avg_log_loss': rps_row[1] if rps_row and rps_row[1] else None,
        'pb_total': pb_row[0] if pb_row else 0,
        'pb_hits': pb_row[1] if pb_row else 0,
    }


def get_prediction_feedback(league=None, had_dir=None, conf_score=None, odds_range=None):
    """历史规律反馈 — 贝叶斯更新版 (Ultra 6.1)

    核心改进: 用Beta-Binomial共轭模型替代频率统计。
    小样本自动收缩向全局均值, 大样本趋近频率估计。
    提供可信区间而非仅点估计。

    层次贝叶斯结构:
        全局先验: Beta(α_global, β_global) — 用全局命中率设定
        联赛级: 用全局先验 + 联赛数据 → 联赛后验
        方向级: 用全局先验 + 方向数据 → 方向后验

    参数:
      league: 联赛名 (如 "韩职"), None=全部
      had_dir: 预测方向 (如 "胜"), None=全部
      conf_score: 置信度分数 (如 4.0), 返回该置信度±0.5的历史命中率
      odds_range: (min_odds, max_odds) 赔率范围, None=全部

    返回: {
      'league_stats': {联赛: {count, hit_rate, bayesian_rate, ci_lower, ci_upper}},
      'direction_stats': {方向: {count, hit_rate, bayesian_rate, ci_lower, ci_upper}},
      'confidence_stats': {置信度档: {count, hit_rate, bayesian_rate, ci_lower, ci_upper}},
      'odds_stats': {赔率区间: {count, hit_rate, bayesian_rate, ci_lower, ci_upper}},
      'overall_rate': float,
      'sample_size': int,
      'bayesian_overall': dict,
      'recommendation': str,
    }
    """
    if not os.path.exists(DB_PATH):
        return None

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    c = conn.cursor()

    result = {'sample_size': 0, 'overall_rate': 0, 'recommendation': ''}

    # 1. 整体命中率 (用于设定层次先验)
    c.execute('''SELECT COUNT(*), SUM(had_hit) FROM verify_history
        WHERE pred_had_dir != '' AND pred_had_dir != '无预测' ''')
    row = c.fetchone()
    total, hits = row[0] or 0, row[1] or 0
    result['sample_size'] = total
    result['overall_rate'] = hits / total if total > 0 else 0

    # 计算层次先验: 用全局命中率设定Beta先验参数
    # 先验强度设为"虚拟样本数"=5 (较弱先验, 数据量大时影响小)
    prior_strength = 5.0
    global_rate = result['overall_rate'] if total > 0 else 0.333
    prior_alpha = global_rate * prior_strength
    prior_beta = (1 - global_rate) * prior_strength

    # 整体贝叶斯估计
    result['bayesian_overall'] = bayesian_hit_rate(
        hits, total, prior_alpha=1.0, prior_beta=1.0)

    # 2. 按联赛统计 (使用层次先验)
    # 修复: 原 f-string 拼接 league 存在 SQL 注入, 改为参数化查询
    if league:
        c.execute('''SELECT league, COUNT(*), SUM(had_hit) FROM verify_history
            WHERE pred_had_dir != '' AND pred_had_dir != '无预测' AND league = ?
            GROUP BY league ORDER BY COUNT(*) DESC''', (league,))
    else:
        c.execute('''SELECT league, COUNT(*), SUM(had_hit) FROM verify_history
            WHERE pred_had_dir != '' AND pred_had_dir != '无预测'
            GROUP BY league ORDER BY COUNT(*) DESC''')
    league_stats = {}
    for lg, cnt, ht in c.fetchall():
        freq = ht / cnt if cnt else 0
        bayes = bayesian_hit_rate(ht or 0, cnt, prior_alpha, prior_beta)
        league_stats[lg or '未知'] = {
            'count': cnt, 'hit_rate': freq,
            'bayesian_rate': bayes['posterior_mean'],
            'ci_lower': bayes['ci_lower'],
            'ci_upper': bayes['ci_upper'],
        }
    result['league_stats'] = league_stats

    # 3. 按方向统计
    c.execute('''SELECT pred_had_dir, COUNT(*), SUM(had_hit) FROM verify_history
        WHERE pred_had_dir != '' AND pred_had_dir != '无预测'
        GROUP BY pred_had_dir''')
    direction_stats = {}
    for d, cnt, ht in c.fetchall():
        freq = ht / cnt if cnt else 0
        bayes = bayesian_hit_rate(ht or 0, cnt, prior_alpha, prior_beta)
        direction_stats[d] = {
            'count': cnt, 'hit_rate': freq,
            'bayesian_rate': bayes['posterior_mean'],
            'ci_lower': bayes['ci_lower'],
            'ci_upper': bayes['ci_upper'],
        }
    result['direction_stats'] = direction_stats

    # 4. 按置信度统计 (5档)
    c.execute('''SELECT
        CASE
            WHEN pred_had_conf LIKE '%★★★★★' THEN '5星'
            WHEN pred_had_conf LIKE '%★★★★%' THEN '4星'
            WHEN pred_had_conf LIKE '%★★★%' THEN '3星'
            WHEN pred_had_conf LIKE '%★★%' THEN '2星'
            ELSE '1星'
        END as conf_tier,
        COUNT(*), SUM(had_hit) FROM verify_history
        WHERE pred_had_dir != '' AND pred_had_dir != '无预测' AND pred_had_conf != ''
        GROUP BY conf_tier''')
    confidence_stats = {}
    for tier, cnt, ht in c.fetchall():
        freq = ht / cnt if cnt else 0
        bayes = bayesian_hit_rate(ht or 0, cnt, prior_alpha, prior_beta)
        confidence_stats[tier] = {
            'count': cnt, 'hit_rate': freq,
            'bayesian_rate': bayes['posterior_mean'],
            'ci_lower': bayes['ci_lower'],
            'ci_upper': bayes['ci_upper'],
        }
    result['confidence_stats'] = confidence_stats

    # 5. 按赔率区间统计
    c.execute('''SELECT
        CASE
            WHEN pred_had_odds < 1.5 THEN '1.0-1.5'
            WHEN pred_had_odds < 2.0 THEN '1.5-2.0'
            WHEN pred_had_odds < 2.5 THEN '2.0-2.5'
            WHEN pred_had_odds < 3.0 THEN '2.5-3.0'
            ELSE '3.0+'
        END as odds_range,
        COUNT(*), SUM(had_hit) FROM verify_history
        WHERE pred_had_dir != '' AND pred_had_dir != '无预测' AND pred_had_odds > 0
        GROUP BY odds_range ORDER BY odds_range''')
    odds_stats = {}
    for orange, cnt, ht in c.fetchall():
        freq = ht / cnt if cnt else 0
        bayes = bayesian_hit_rate(ht or 0, cnt, prior_alpha, prior_beta)
        odds_stats[orange] = {
            'count': cnt, 'hit_rate': freq,
            'bayesian_rate': bayes['posterior_mean'],
            'ci_lower': bayes['ci_lower'],
            'ci_upper': bayes['ci_upper'],
        }
    result['odds_stats'] = odds_stats

    # 6. 生成建议 (基于贝叶斯后验, 更稳健)
    recs = []
    if league and league in league_stats:
        ls = league_stats[league]
        if ls['count'] >= 3:  # 贝叶斯允许更小样本
            bayes_rate = ls['bayesian_rate']
            ci_str = f"[{ls['ci_lower']*100:.0f}%-{ls['ci_upper']*100:.0f}%]"
            recs.append(f"{league}贝叶斯命中率{bayes_rate*100:.0f}%{ci_str}({ls['count']}场)")
            if bayes_rate < 0.40 and ls['count'] >= 5:
                recs.append(f"该联赛贝叶斯命中率偏低, 建议降级置信度")

    if had_dir and had_dir in direction_stats:
        ds = direction_stats[had_dir]
        if ds['count'] >= 5:
            bayes_rate = ds['bayesian_rate']
            recs.append(f"{had_dir}方向贝叶斯命中率{bayes_rate*100:.0f}%({ds['count']}场)")
            if bayes_rate < 0.35 and ds['count'] >= 10:
                recs.append(f"{had_dir}方向贝叶斯命中率显著偏低")

    if conf_score and conf_score >= 4.0:
        if '4星' in confidence_stats or '5星' in confidence_stats:
            cs = confidence_stats.get('5星', confidence_stats.get('4星', {}))
            if cs.get('count', 0) >= 3:
                bayes_rate = cs['bayesian_rate']
                if bayes_rate < 0.55:
                    recs.append(f"高置信度贝叶斯命中率仅{bayes_rate*100:.0f}%, 置信度评级可能过拟合")

    if odds_range:
        min_o, max_o = odds_range
        for orange, os_data in odds_stats.items():
            parts = orange.split('-')
            if len(parts) == 2:
                lo = float(parts[0])
                hi = float(parts[1].rstrip('+'))
                if lo >= min_o and hi <= max_o:
                    if os_data['count'] >= 3:
                        bayes_rate = os_data['bayesian_rate']
                        recs.append(f"赔率{orange}区间贝叶斯命中率{bayes_rate*100:.0f}%")

    # 7. 校准信息 (如果有概率数据)
    c.execute('''SELECT COUNT(*) FROM verify_history
        WHERE pred_had_probs IS NOT NULL AND pred_had_probs != '' ''')
    has_probs = c.fetchone()[0]
    if has_probs >= 10:
        recs.append(f"有{has_probs}场概率数据可做校准分析")

    result['recommendation'] = '; '.join(recs) if recs else '无特殊历史规律提示'

    conn.close()
    return result


def generate_regression_report(current_stats, hist_stats, date_str):
    """生成回归分析报告(含历史数据对比)"""
    html_parts = []

    if not hist_stats:
        html_parts.append('<div class="callout warning"><strong>首次验证</strong>: 无历史数据对比</div>')
    else:
        total_pred = hist_stats['total_pred']
        total_had = hist_stats['total_had']
        total_hhad = hist_stats['total_hhad']
        total_score = hist_stats['total_score']
        total_hf = hist_stats.get('total_hf', 0) or 0
        total_tg = hist_stats.get('total_tg', 0) or 0
        had_rate = total_had / total_pred * 100 if total_pred else 0
        hhad_rate = total_hhad / total_pred * 100 if total_pred else 0
        score_rate = total_score / total_pred * 100 if total_pred else 0
        hf_rate = total_hf / total_pred * 100 if total_pred else 0
        tg_rate = total_tg / total_pred * 100 if total_pred else 0

        html_parts.append(f'''
<div class="callout">
  <strong>历史累计统计 ({hist_stats["total_matches"]}场, 有预测{total_pred}场)</strong><br>
  HAD累计命中率: {total_had}/{total_pred} = {had_rate:.1f}%<br>
  HHAD累计命中率: {total_hhad}/{total_pred} = {hhad_rate:.1f}%<br>
  比分Top3累计命中率: {total_score}/{total_pred} = {score_rate:.1f}%<br>
  半全场累计命中率: {total_hf}/{total_pred} = {hf_rate:.1f}%<br>
  总进球数累计命中率: {total_tg}/{total_pred} = {tg_rate:.1f}%<br>
  场均进球: {hist_stats["avg_goals"]:.1f} | 大2.5球: {hist_stats["total_over25"]}场 | 小2.5球: {hist_stats["total_under25"]}场
</div>''')

        # HAD方向统计
        if hist_stats['had_dirs']:
            html_parts.append('<h3>HAD方向历史统计</h3><table class="detail-table"><tr><th>方向</th><th>场次</th><th>命中</th><th>命中率</th></tr>')
            for dir_name, count, hits in hist_stats['had_dirs']:
                rate = hits / count * 100 if count else 0
                html_parts.append(f'<tr><td>{dir_name}</td><td>{count}</td><td>{hits}</td><td>{rate:.0f}%</td></tr>')
            html_parts.append('</table>')

        # HHAD方向统计
        if hist_stats['hhad_dirs']:
            html_parts.append('<h3>HHAD方向历史统计</h3><table class="detail-table"><tr><th>方向</th><th>场次</th><th>命中</th><th>命中率</th></tr>')
            for dir_name, count, hits in hist_stats['hhad_dirs']:
                rate = hits / count * 100 if count else 0
                html_parts.append(f'<tr><td>{dir_name}</td><td>{count}</td><td>{hits}</td><td>{rate:.0f}%</td></tr>')
            html_parts.append('</table>')

        # 最近验证记录
        if hist_stats['recent']:
            html_parts.append('<h3>最近验证记录</h3><table class="detail-table"><tr><th>日期</th><th>场次</th><th>有预测</th><th>HAD命中</th><th>HHAD命中</th><th>比分命中</th><th>HAD率</th></tr>')
            for r in hist_stats['recent']:
                html_parts.append(f'<tr><td>{r.get("verify_date","")}</td><td>{r.get("total",0)}</td><td>{r.get("has_pred",0)}</td><td>{r.get("had_hits",0)}</td><td>{r.get("hhad_hits",0)}</td><td>{r.get("score_hits",0)}</td><td>{(r.get("had_rate") or 0):.0f}%</td></tr>')
            html_parts.append('</table>')

    return ''.join(html_parts)


def main():
    print("=" * 60)
    print("【赛果验证脚本 Ultra 7.11】")
    print(f"输入: {INPUT}")
    print("=" * 60)

    # Phase 1: 解析输入
    match_keys, date_range = parse_input(INPUT)
    print(f"\n[Phase1] 解析输入: match_keys={match_keys}, date_range={date_range}")

    # Phase 2: 从sporttery比分直播API获取赛果 (Ultra 7.11: 主数据源)
    print("\n[Phase2] 从sporttery比分直播(zqbfzb)获取赛果(优先)...")
    results_zqbfzb = fetch_zqbfzb_results(match_keys)
    print(f"  zqbfzb获取到 {len(results_zqbfzb)} 场比赛赛果")

    # 从sporttery赛果API获取补充数据(赔率/goalLine/winFlag)
    print("\n  从sporttery赛果API获取赔率补充...")
    results_sporttery = fetch_match_results(date_range)

    # 500.com作为末位fallback (zqbfzb无比分时)
    need_500 = [k for k in match_keys
                if k not in results_zqbfzb or ':' not in results_zqbfzb[k].get('sectionsNo999', '')]
    results_500 = {}
    if need_500:
        print(f"\n  500.com fallback: {len(need_500)}场无比分, 尝试500.com...")
        results_500 = fetch_500_results(need_500)
        print(f"  500.com获取到 {len(results_500)} 场比赛赛果")

    # 合并: zqbfzb比分(优先) + sporttery赔率/goalLine + 500.com fallback
    target_results = {}
    for key in match_keys:
        zq = results_zqbfzb.get(key)
        sport = results_sporttery.get(key)
        r500 = results_500.get(key)

        if zq and ':' in zq.get('sectionsNo999', ''):
            # zqbfzb有比分 — 以zqbfzb为主, 补充sporttery的goalLine/赔率
            merged = zq.copy()
            if sport:
                # 补充zqbfzb缺失的字段: goalLine, winFlag, 终赔
                if not merged.get('goalLine') and sport.get('goalLine'):
                    merged['goalLine'] = sport['goalLine']
                if not merged.get('h') and sport.get('h'):
                    merged['h'] = sport['h']
                    merged['d'] = sport.get('d', '')
                    merged['a'] = sport.get('a', '')
            merged['source'] = 'zqbfzb' + ('+sporttery' if sport else '')
            target_results[key] = merged
        elif r500 and sport:
            # zqbfzb无比分, 500.com有 — 合并500.com比分 + sporttery赔率
            merged = sport.copy()
            merged['sectionsNo999'] = f"{r500['home_score']}:{r500['away_score']}"
            merged['sectionsNo1'] = f"{r500['half_home']}:{r500['half_away']}"
            merged['winFlag'] = 'H' if r500['home_score'] > r500['away_score'] else ('D' if r500['home_score'] == r500['away_score'] else 'A')
            merged['source'] = '500.com+sporttery'
            target_results[key] = merged
        elif r500:
            target_results[key] = r500
        elif zq and sport:
            # zqbfzb无比分但有比赛信息, 合并sporttery
            merged = sport.copy()
            merged['source'] = 'zqbfzb+sporttery(无比分)'
            target_results[key] = merged
        elif zq:
            target_results[key] = zq
        elif sport:
            target_results[key] = sport

    # ★★★ Ultra 7.7: 应用手动比分覆盖 (API未返回比分时使用已验证的真实比分) ★★★
    for key, manual_score in MANUAL_SCORES.items():
        if key in target_results:
            rdata = target_results[key]
            existing_score = rdata.get('sectionsNo999', '')
            if ':' not in existing_score:
                # API未返回比分, 使用手动比分
                rdata['sectionsNo999'] = manual_score
                parts = manual_score.split(':')
                h, a = int(parts[0]), int(parts[1])
                rdata['winFlag'] = 'H' if h > a else ('D' if h == a else 'A')
                rdata['source'] = rdata.get('source', 'sporttery') + '+手动验证'
                print(f"  ⚠️ {key}: API无比分, 使用手动验证比分 {manual_score}")

    print(f"\n  合并完成: {len(target_results)} 场比赛")
    for key, rdata in target_results.items():
        src = rdata.get('source', 'sporttery')
        score = rdata.get('sectionsNo999', '')
        if not score and 'home_score' in rdata:
            score = f"{rdata['home_score']}:{rdata['away_score']}"
        print(f"    {key}: {rdata.get('homeTeam','')} {score} {rdata.get('awayTeam','')} [{src}]")

    if not target_results:
        print("\n  ⚠️ 未获取到任何比赛赛果!")
        return

    # Phase 3: 加载预测文件
    print("\n[Phase3] 加载预测文件...")
    predictions = load_predictions(match_keys, target_results)
    print(f"  加载到 {len(predictions)} 场预测数据")
    if len(predictions) == 0:
        print("  ⚠️ 无预测数据(本次仅获取赛果,不进行验证)")
        print("  预测文件目录:", PREDICTIONS_DIR)
        if os.path.exists(PREDICTIONS_DIR):
            files = [f for f in os.listdir(PREDICTIONS_DIR) if f.startswith('pred_')]
            print("  可用预测文件:", files)

    # Phase 4: 验证预测
    print("\n[Phase4] 验证预测...")
    verified_matches = []
    for key, result_raw in target_results.items():
        result = parse_result(result_raw)

        # ★★★ Ultra 7.7: 检查数据是否可用, 无比分数据时跳过验证 ★★★
        if not result.get('data_available', True):
            print(f"  {key} {result['home']} vs {result['away']} | ⚠️ 赛果数据未获取(API未返回比分), 跳过验证")
            print(f"    → 请稍后重试; 若比赛已结束, 可在脚本顶部 MANUAL_SCORES 字典中填写确认比分后重跑验证")
            verified_matches.append({
                'key': key,
                'home': result['home'],
                'away': result['away'],
                'league': result['league'],
                'actual_score': '数据未获取',
                'half_score': 'N/A',
                'actual_had': '数据未获取',
                'actual_hhad': '数据未获取',
                'actual_hf': '',
                'goal_line': result['goal_line'],
                'total_goals': -1,
                'pred_had_dir': predictions.get(key, {}).get('prediction', {}).get('HAD', {}).get('dir', 'N/A') if key in predictions else '无预测',
                'pred_had_odds': '', 'pred_had_conf': '', 'pred_had_p': '',
                'pred_hhad_dir': '', 'pred_hhad_odds': '', 'pred_hhad_conf': '', 'pred_hhad_p': '',
                'pred_top3': '', 'pred_score_main': '', 'pred_market_gl': '',
                'pred_file': '有' if key in predictions else '无',
                'had_hit': None, 'hhad_hit': None, 'score_hit': None, 'hf_hit': None, 'tg_hit': None,
                'pred_hf_combo': '', 'actual_hf': '', 'pred_tg_main': '',
                'source': result.get('source', 'sporttery'),
                'data_available': False,
            })
            continue

        if key in predictions:
            v = verify_prediction(predictions[key], result)
            v['key'] = key
            v['source'] = result.get('source', 'sporttery')
            # ★ 投注指南验证: 四档(主推) + 首推(补充=PDF主推)
            g = verify_bet_guide(predictions[key], result)
            v['guide_level'] = g['level']            # 四档
            v['guide_market'] = g['primary_market']  # 首推市场 (存库用)
            v['guide_bet'] = g['bet']                # 四档方向
            v['guide_hit'] = g['hit']                # 四档命中
            v['primary_market'] = g['primary_market']
            v['primary_bet'] = g['primary_bet']
            v['primary_hit'] = g['primary_hit']
            verified_matches.append(v)
            had_str = '✓' if v['had_hit'] else '✗'
            hhad_str = '✓' if v['hhad_hit'] else '✗'
            if g['hit'] is None:
                guide_str = '🚫不买'
            else:
                guide_str = ('✓' if g['hit'] else '✗') + f" {g['bet']}"
            if g['primary_hit'] is None:
                prim_str = '无'
            else:
                prim_str = ('✓' if g['primary_hit'] else '✗') + f" {g['primary_bet']}"
            print(f"  {key} {v['home']} {v['actual_score']} {v['away']} | "
                  f"HAD:{v['pred_had_dir']}→{v['actual_had']} {had_str} | "
                  f"HHAD:{v['pred_hhad_dir']}→{v['actual_hhad']} {hhad_str} | "
                  f"四档[{g['level']}]{guide_str} | 首推[{g['primary_market']}]{prim_str}")
        else:
            print(f"  {key} {result['home']} {result['home_score']}-{result['away_score']} {result['away']} | ⚠️ 无预测数据")
            verified_matches.append({
                'key': key,
                'home': result['home'],
                'away': result['away'],
                'league': result['league'],
                'actual_score': f"{result['home_score']}-{result['away_score']}",
                'half_score': f"{result['half_home']}-{result['half_away']}",
                'actual_had': result['had_result'],
                'actual_hhad': result['hhad_result'],
                'actual_hf': '',
                'goal_line': result['goal_line'],
                'total_goals': result['total_goals'],
                'pred_had_dir': '无预测',
                'pred_had_odds': '',
                'pred_had_conf': '',
                'pred_had_p': '',
                'pred_hhad_dir': '无预测',
                'pred_hhad_odds': '',
                'pred_hhad_conf': '',
                'pred_hhad_p': '',
                'pred_top3': '',
                'pred_score_main': '',
                'pred_market_gl': '',
                'pred_file': '无',
                'had_hit': None,
                'hhad_hit': None,
                'score_hit': None,
                'hf_hit': None,
                'tg_hit': None,
                'pred_hf_combo': '',
                'actual_hf': '',
                'pred_tg_main': '',
                'actual_tg': '',
                'had_odds': result['had_odds'],
                'source': result.get('source', 'sporttery'),
            })

    # M13: 赛果未获取的占位记录不参与统计/入库 —
    # 排除后不污染命中率(total=分母), 不将 total_goals=-1 计入均值, 不伪造0-0入库
    valid_matches = [v for v in verified_matches
                     if v.get('data_available', True) and v.get('actual_score') != '数据未获取']
    skipped_no_data = len(verified_matches) - len(valid_matches)
    verified_matches = valid_matches
    if skipped_no_data:
        print(f"  ⚠️ 已排除 {skipped_no_data} 场赛果未获取的占位记录(不参与统计/入库)")

    # 统计
    total = len(verified_matches)
    has_pred = [v for v in verified_matches if v['pred_file'] != '无']
    # Ultra 13.5: 命中率分母按各玩法有效预测数 (had_hit=None 的未开盘/无方向场次不计入)
    had_denom = [v for v in has_pred if v.get('had_hit') is not None]
    hhad_denom = [v for v in has_pred if v.get('hhad_hit') is not None]
    had_hits = sum(1 for v in had_denom if v['had_hit'])
    hhad_hits = sum(1 for v in hhad_denom if v['hhad_hit'])
    score_hits = sum(1 for v in has_pred if v['score_hit'])
    hf_hits = sum(1 for v in has_pred if v.get('hf_hit'))
    tg_hits = sum(1 for v in has_pred if v.get('tg_hit'))
    # ★ 投注指南统计: 四档(主推) + 首推(补充=PDF主推)
    guide_bets = [v for v in has_pred if v.get('guide_hit') is not None]  # 四档有推荐(非avoid)
    guide_hits = sum(1 for v in guide_bets if v['guide_hit'])
    guide_by_level = {}
    for v in guide_bets:
        lv = v.get('guide_level', '?')
        d = guide_by_level.setdefault(lv, {'n': 0, 'hit': 0})
        d['n'] += 1
        if v['guide_hit']:
            d['hit'] += 1
    n_avoid = sum(1 for v in has_pred if v.get('guide_hit') is None)
    # 首推补充统计
    primary_bets = [v for v in has_pred if v.get('primary_hit') is not None]
    primary_hits = sum(1 for v in primary_bets if v['primary_hit'])
    primary_by_market = {}
    for v in primary_bets:
        mk = v.get('primary_market', '?')
        d = primary_by_market.setdefault(mk, {'n': 0, 'hit': 0})
        d['n'] += 1
        if v['primary_hit']:
            d['hit'] += 1
    stats = {
        'total': total,
        'has_pred': len(has_pred),
        'had_denom': len(had_denom),      # Ultra 13.5: HAD有效预测数(分母)
        'hhad_denom': len(hhad_denom),    # Ultra 13.5: HHAD有效预测数(分母)
        'had_hits': had_hits,
        'hhad_hits': hhad_hits,
        'score_hits': score_hits,
        'hf_hits': hf_hits,
        'tg_hits': tg_hits,
        'guide_bets': len(guide_bets),
        'guide_hits': guide_hits,
        'guide_avoid': n_avoid,
        'guide_by_level': guide_by_level,
        'primary_bets': len(primary_bets),
        'primary_hits': primary_hits,
        'primary_by_market': primary_by_market,
    }

    print(f"\n  统计: 有预测{len(has_pred)}/{total}, HAD命中{had_hits}/{len(had_denom)} ({had_hits/len(had_denom)*100:.0f}%), "
          f"HHAD命中{hhad_hits}/{len(hhad_denom)} ({hhad_hits/len(hhad_denom)*100:.0f}%), "
          f"比分命中{score_hits}, 半全场命中{hf_hits}, 总进球命中{tg_hits}")
    if guide_bets:
        _lv_zh = {'draw': '🎯平局直击', 'single': '✅单选', 'cover': '⚠️双选兜底', 'avoid': '🚫避开'}
        _lv_parts = []
        for lv in ('draw', 'single', 'cover'):
            d = guide_by_level.get(lv)
            if d:
                _lv_parts.append(f"{_lv_zh.get(lv, lv)}{d['hit']}/{d['n']}")
        print(f"  🎯 四档主推: 命中{guide_hits}/{len(guide_bets)} ({guide_hits/len(guide_bets)*100:.1f}%, 另有🚫避开{n_avoid}场) | " + " ".join(_lv_parts))
    else:
        print(f"  🎯 四档主推: 无可验证场次(避开{n_avoid}场)")
    if primary_bets:
        _pm_zh = {'HAD': '✅胜平负', 'HHAD': '🎯让球'}
        _pm_parts = []
        for mk in ('HAD', 'HHAD'):
            d = primary_by_market.get(mk)
            if d:
                _pm_parts.append(f"{_pm_zh.get(mk, mk)}{d['hit']}/{d['n']}")
        print(f"  📌 首推参考(PDF主推): 命中{primary_hits}/{len(primary_bets)} ({primary_hits/len(primary_bets)*100:.1f}%) | " + " ".join(_pm_parts))

    # Pro 3.0: 高级指标 (计算一次, 供终端输出/HTML报告/数据库复用, 避免三重计算)
    brier_out = calculate_brier_score(verified_matches)
    rps_out = calculate_rps(verified_matches)
    sig_out = calculate_significance(had_hits, len(has_pred))
    bet_ms = [v for v in verified_matches if (v.get('roi') or {}).get('bet', 0) > 0]
    t_bet = sum((v.get('roi') or {}).get('bet', 0) for v in bet_ms)
    t_ret = sum((v.get('roi') or {}).get('return', 0) for v in bet_ms)
    cum_roi_out = (t_ret / t_bet * 100) if t_bet > 0 else None
    kelly_out = verify_kelly_bets(verified_matches)
    calib_out = calibrate_confidence(verified_matches)
    # Ultra 6.1: 高级验证模型
    cal_analysis = calibration_analysis(verified_matches)
    conf_matrix = confusion_matrix_analysis(verified_matches)
    boot_ci = bootstrap_confidence_interval(had_hits, len(has_pred))
    logistic_factors = logistic_factor_analysis(verified_matches)
    bayes_overall = bayesian_hit_rate(had_hits, len(has_pred))

    print(f"  [Pro3.0] Brier分数: {brier_out.get('brier')} ({brier_out.get('interpretation')})")
    print(f"  [Ultra6.0] RPS分数: {rps_out.get('rps')} | Log Loss: {rps_out.get('log_loss')} ({rps_out.get('interpretation')})")
    print(f"  [Ultra6.0] 统计显著性: {sig_out.get('conclusion')}")
    print(f"  [Ultra6.1] 校准分析: {cal_analysis.get('interpretation')}")
    print(f"  [Ultra6.1] 混淆矩阵: {conf_matrix.get('interpretation')}")
    print(f"  [Ultra6.1] Bootstrap CI: {boot_ci.get('interpretation')}")
    print(f"  [Ultra6.1] 贝叶斯估计: {bayes_overall.get('interpretation')}")
    print(f"  [Ultra6.1] 因子分析: {logistic_factors.get('interpretation')}")
    roi_str = f"{cum_roi_out:+.1f}%" if cum_roi_out is not None else "N/A"
    print(f"  [Pro3.0] 累计ROI(固定1单位): {roi_str} (投注{len(bet_ms)}场)")
    print(f"  [Pro3.0] 置信度校准: {calib_out.get('summary', '无数据')}")
    if kelly_out:
        print(f"  [Pro3.0] Kelly验证: 投注{kelly_out.get('total_stake')}% 收益{kelly_out.get('total_return')}% ROI={kelly_out.get('roi')}%")
    else:
        print(f"  [Pro3.0] Kelly验证: 无value投注数据")

    # Phase 5: 生成PDF报告 (手机阅读优化, 不再输出HTML)
    print("\n[Phase5] 生成PDF报告 (手机阅读优化版)...")
    # 使用输入日期(开盘日)作为报告日期, 而非比赛实际日期
    input_date_match = re.match(r'(\d{4}-\d{2}-\d{2})', INPUT)
    if input_date_match:
        date_str = input_date_match.group(1)
    else:
        match_dates = sorted(set(r.get('matchDate', '') for r in target_results.values() if r.get('matchDate')))
        date_str = match_dates[0] if match_dates else (date_range[0] if date_range else time.strftime('%Y-%m-%d'))

    # 回归分析文本
    lessons_html = generate_lessons(verified_matches, stats)
    lessons_text = re.sub(r'<[^>]+>', '', lessons_html)

    # 先入库, 再获取历史数据(含本次)
    init_db()
    save_to_db(verified_matches, stats, date_str, lessons_text, brier_result=brier_out)
    hist_stats = get_historical_stats()

    # Ultra 6.1: CUSUM模型漂移检测 (使用历史统计)
    cusum_out = None
    if hist_stats and hist_stats.get('recent'):
        cusum_out = cusum_drift_detection(hist_stats['recent'])
        print(f"  [Ultra8.0] CUSUM漂移: {cusum_out.get('interpretation')}")

    # 生成报告HTML字符串 (仅用于PDF解析, 不保存到文件)
    html = generate_html_report(verified_matches, stats, date_str,
                                brier_result=brier_out, calibration=calib_out, kelly_result=kelly_out,
                                cal_analysis=cal_analysis, conf_matrix=conf_matrix,
                                boot_ci=boot_ci, bayes_overall=bayes_overall,
                                logistic_factors=logistic_factors, cusum_out=cusum_out,
                                sig_out=sig_out, rps_out=rps_out)

    # 在报告末尾插入回归分析部分
    regression_html = generate_regression_report(stats, hist_stats, date_str)
    regression_section = f"""
<div class="section">
  <h2><span class="num">06</span> 回归分析与历史数据库</h2>
  {regression_html}
</div>"""
    html = html.replace('</div>\n</body>', regression_section + '\n\n</div>\n</body>')

    # Ultra 8.2: 直接从HTML字符串生成PDF (不保存HTML文件)
    pdf_file = os.path.join(REPORT_DIR, f'verify_{date_str.replace("-","")}.pdf')
    try:
        from gen_verify_pdf import generate_verify_pdf
        generate_verify_pdf(html, pdf_file)
        print(f"  PDF报告已保存: {pdf_file}")
    except Exception as e:
        print(f"  ⚠️ PDF生成异常: {e}")

    # 直观版验证分析报告 (HTML: 命中率圆环+每场对照+命中矩阵+关键洞察)
    # 数据源: verify_history (本场已入库), 与原纯表格PDF并存, 用户指定默认输出
    try:
        from gen_verify_analysis_html import generate as _gen_html_report
        _html_report = _gen_html_report(date_str)
        if _html_report:
            print(f"  直观版报告: {_html_report}")
    except Exception as e:
        print(f"  ⚠️ 直观版报告生成异常: {e}")

    # Phase 6: 回归分析输出
    print("\n[Phase6] 回归分析...")
    print(f"  当次回归分析: HAD {had_hits}/{len(has_pred)}, HHAD {hhad_hits}/{len(has_pred)}, 比分 {score_hits}/{len(has_pred)}。")

    print(f"\n  ✓ 已入库: {date_str} 共 {total} 场比赛")
    print(f"  回归数据库: {DB_PATH}")

    print("\n" + "=" * 60)
    print("【验证完成】")
    print(f"  PDF报告: {pdf_file}")
    print(f"  数据库: {DB_PATH}")
    print("=" * 60)

    # Phase 7: 模拟投注结算 (Pro 3.4)
    print("\n[Phase7] 模拟投注结算...")
    settle_sim_bets(verified_matches)


# M串N 容错串关表 (与 v215_simulate.PARLAY_FOLDS 保持一致)
_PARLAY_FOLDS = {
    2: {'2串1': [2]},
    3: {'3串1': [3], '3串3': [2], '3串4': [2, 3]},
    4: {'4串1': [4], '4串4': [3], '4串5': [3, 4], '4串6': [2], '4串11': [2, 3, 4]},
    5: {'5串1': [5], '5串5': [4], '5串6': [4, 5], '5串10': [2],
        '5串16': [3, 4, 5], '5串20': [2, 3], '5串26': [2, 3, 4, 5]},
}


def settle_sim_bets(verified_matches):
    """结算模拟投注 — 验证赛果时自动计算盈亏

    逻辑:
        1. 从sim_bets表读取pending状态的投注
        2. 对每注的每场比赛, 检查赛果是否命中
        3. M串1: 全部命中才中奖, 奖金 = 2元 × 各场赔率连乘 × 倍数
        4. 更新status/actual_payout/profit
    """
    import json as _json

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    c = conn.cursor()

    # 获取所有待结算投注 (表可能不存在)
    try:
        c.execute("SELECT bet_id, bet_type, stake, multiplier, total_odds, potential_payout, matches_json FROM sim_bets WHERE status='pending'")
        pending_bets = c.fetchall()
    except sqlite3.OperationalError:
        pending_bets = []

    if not pending_bets:
        print("  无待结算的模拟投注")
        conn.close()
        return

    # 构建验证结果索引 (key → actual_had/actual_hhad)
    verified_map = {}
    for v in verified_matches:
        key = v.get('key', '')
        verified_map[key] = {
            'actual_had': v.get('actual_had', ''),   # 胜/平/负
            'actual_hhad': v.get('actual_hhad', ''),  # 让胜/让平/让负
            'actual_score': v.get('actual_score', ''),
        }

    settled_count = 0
    still_pending = 0

    for bet in pending_bets:
        bet_id, bet_type, stake, multiplier, total_odds, potential_payout, matches_json = bet

        # L3: matches_json解析防护 — 解析失败保留pending并跳过该注
        try:
            matches = _json.loads(matches_json)
        except Exception as _je:
            print(f"  ⚠️ {bet_id} matches_json解析失败, 保留待结算: {_je}")
            still_pending += 1
            continue

        # 检查每场比赛是否都有赛果
        all_resolved = True
        hits = []  # 每场命中标记

        for m in matches:
            key = m['key']
            v = verified_map.get(key)
            # S6: '数据未获取'占位值truthy, 不能仅靠not判断 — 显式识别占位值
            if not v or v.get('actual_had') in ('', '数据未获取', None):
                # 该场比赛还没有赛果(或赛果为占位值)
                all_resolved = False
                break

            # 判断命中: 投注方向 vs 实际结果
            bet_dir = m['bet_dir']  # 胜/平/负 或 让胜/让平/让负
            market = m['market']

            if market == 'HHAD':
                actual = v.get('actual_hhad', '')
                # S6: HHAD玩法需检查actual_hhad是否缺失
                if actual in ('', '数据未获取', None):
                    all_resolved = False
                    break
            else:
                actual = v.get('actual_had', '')

            hits.append(bet_dir == actual)

        if not all_resolved:
            still_pending += 1
            continue

        all_hit = all(hits)
        now = time.strftime('%Y-%m-%d %H:%M:%S')

        # M串N 容错串关结算 (借鉴 SportteryAPI parlay.ts):
        # 奖金 = Σ 命中组合 (2元×Π赔率×倍数), 单票封顶500万
        _mp = re.match(r'^(\d+)串(\d+)$', bet_type or '')
        if _mp and _mp.group(2) != '1':
            from itertools import combinations as _comb
            _M = int(_mp.group(1))
            _folds = _PARLAY_FOLDS.get(_M, {}).get(bet_type, [_M])
            hit_legs = [m for m, ok in zip(matches, hits) if ok]
            payout = 0.0
            for size in _folds:
                for combo in _comb(hit_legs, size):
                    co = 1.0
                    for leg in combo:
                        co *= leg['odds']
                    payout += 2 * co * multiplier
            actual_payout = round(min(payout, 5_000_000), 2)
            profit = round(actual_payout - stake, 2)
            status = 'won' if profit > 0 else ('partial' if actual_payout > 0 else 'lost')
            n_hit = sum(hits)
            icon = '✅' if status == 'won' else ('🔶' if status == 'partial' else '❌')
            print(f"  {icon} {bet_id} {bet_type} 命中{n_hit}/{len(hits)}场 奖金{actual_payout}元 盈亏{profit:+.2f}元")
            for m, ok in zip(matches, hits):
                v = verified_map.get(m['key'], {})
                market = m['market']
                actual = v.get('actual_hhad', '') if market == 'HHAD' else v.get('actual_had', '')
                print(f"     {m['key']} {m['home']} vs {m['away']} → {m['option']}@{m['odds']} {'✓' if ok else '✗'} (实际{actual})")
        elif all_hit:
            # 中奖! 奖金 = 2元 × 总赔率 × 倍数
            actual_payout = round(2 * total_odds * multiplier, 2)
            profit = round(actual_payout - stake, 2)
            status = 'won'
            print(f"  ✅ {bet_id} {bet_type} 中奖! 奖金{actual_payout}元 净赚{profit}元")
            for m in matches:
                v = verified_map.get(m['key'], {})
                print(f"     {m['key']} {m['home']} vs {m['away']} → {m['option']}@{m['odds']} ✓ ({v.get('actual_score','')})")
        else:
            # 未中奖
            actual_payout = 0
            profit = round(-stake, 2)
            status = 'lost'
            print(f"  ❌ {bet_id} {bet_type} 未中奖 亏损{abs(profit)}元")
            for m, ok in zip(matches, hits):
                v = verified_map.get(m['key'], {})
                market = m['market']
                actual = v.get('actual_hhad', '') if market == 'HHAD' else v.get('actual_had', '')
                print(f"     {m['key']} {m['home']} vs {m['away']} → {m['option']}@{m['odds']} {'✓' if ok else '✗'} (实际{actual})")

        c.execute('''UPDATE sim_bets SET status=?, actual_payout=?, profit=?, verified_at=?
                     WHERE bet_id=?''',
                  (status, actual_payout, profit, now, bet_id))
        settled_count += 1

    conn.commit()
    conn.close()

    # 汇总
    if settled_count > 0:
        print(f"\n  本次结算: {settled_count}注")
    if still_pending > 0:
        print(f"  仍待结算: {still_pending}注 (赛果未出)")

    # 显示累计统计
    show_sim_stats()


def show_sim_stats():
    """显示模拟投注累计统计"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    c = conn.cursor()
    try:
        c.execute("SELECT status, COUNT(*), SUM(stake), SUM(actual_payout), SUM(profit) FROM sim_bets GROUP BY status")
        rows = c.fetchall()
    except sqlite3.OperationalError:
        # 修复: sim_bets 表不存在(全新环境)时不应崩溃, 静默跳过
        conn.close()
        return
    conn.close()

    if not rows:
        return

    total_stake = 0
    total_payout = 0
    settled_stake = 0
    settled_profit = 0
    won = lost = pending = partial = 0

    for status, count, stake, payout, profit in rows:
        total_stake += stake or 0
        total_payout += payout or 0
        if status in ('won', 'lost', 'partial'):
            settled_stake += stake or 0
            settled_profit += profit or 0
        if status == 'won':
            won = count
        elif status == 'lost':
            lost = count
        elif status == 'partial':
            partial = count
        elif status == 'pending':
            pending = count

    # Net profit = total recovered - total invested (consistent with show_history)
    net_profit = total_payout - total_stake
    _pt = f"/{partial}保本" if partial else ""
    print(f"\n  📊 模拟投注累计: {won}中/{lost}未中{_pt}/{pending}待结 | 投入{total_stake}元 回收{total_payout:.0f}元 盈亏{net_profit:+.0f}元", end='')
    if total_stake > 0:
        print(f" ROI={net_profit/total_stake*100:+.1f}%", end='')
    if settled_stake > 0:
        print(f" (已结算ROI={settled_profit/settled_stake*100:+.1f}%)", end='')
    print()


if __name__ == '__main__':
    main()
