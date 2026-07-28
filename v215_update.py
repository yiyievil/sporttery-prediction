#!/usr/bin/env python3
"""
预测更新模块 Ultra 5.0 — 增量更新即时赔率，保留历史数据，节约token

关键词: 更新
输入: 更新 7月25日 201,202
  或命令行: python3 v215_update.py 2026-07-25 201,202
  或命令行: python3 v215_update.py 7月25日 201,202

设计理念:
  - 初盘和历史数据(近况/对赛/战绩)相对固定 → 从上次预测缓存中复用
  - 即时赔率(体彩HAD/HHAD + 外部欧指/大小球/初赔)会变化 → 每次更新时重新获取
  - 只更新变化的数据，最大化节约token

🔒 数据源策略 (锁定, 与 v215_e2e.py DATA_SOURCE_POLICY 一致, 禁止修改):
  1. sporttery 实时数据 = 绝对核心 (HAD/HHAD/固定奖金, 每次更新必抓)
  2. nowscore = 主力辅助数据源, fid=0 场次优先 nowscore 更新
  3. 500.com = 降级备用, 仅 nowscore 实在抓不到时使用
  (Ultra 7.2 起: fid=0 路径已由 500优先 修正为 nowscore优先)

节约的HTTP请求:
  ❌ 跳过: fetch_500_fixture_ids (fid已知，~10KB)
  ❌ 跳过: fetch_shuju_page (近况/对赛/战绩不变，~15KB/场)
  ✅ 获取: fetch_sporttery_matches (即时HAD/HHAD)
  ✅ 获取: fetch_ouzhi_json (即时百家欧指)
  ✅ 获取: fetch_daxiao_goal_line (即时大小球盘口)
  ✅ 获取: fetch_initial_ouzhi/yazhi/daxiao (即时初赔AJAX)

输出:
  - 更新后的预测JSON (含历史记录)
  - 变更对比报告 (哪些赔率变了、方向是否改变、置信度是否调整)
  - Token节约统计

Pro 3.0 新功能:
  1. 趋势追踪: compare_predictions 现返回 (changes, trend_info)，结合历史记录
     分析赔率/方向变化趋势(强化/弱化/稳定/反转)
  2. 重大变更警报: Step 6b 检测方向反转、赔率大幅变化(>10%)、趋势反转、
     置信度跃升等关键变化并发出警报
  3. 智能缓存验证: fetch_one_update 校验缓存新鲜度(24小时)，过期缓存自动刷新
     shuju 数据，避免使用过期历史数据
"""

import sys, os, json, re, time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# Import prediction functions from main script
# Ultra-Opt: 通用路径 (旧版硬编码 '/workspace')
_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _WORKSPACE)
import v215_e2e
from v215_e2e import (
    fetch_sporttery_matches,
    fetch_sporttery_fixed_bonus,
    fetch_ouzhi_json,
    fetch_daxiao_goal_line,
    fetch_initial_ouzhi,
    fetch_initial_yazhi,
    fetch_initial_daxiao,
    fetch_shuju_page,
    predict_match,
    estimate_tokens,
    fmt_size,
    stars_to_score,
)

PREDICTIONS_DIR = os.path.join(_WORKSPACE, 'predictions')

# ============================================================
# Phase 0: 用户输入
# ============================================================
INPUT = "2026-07-25 201,202"

# 命令行参数覆盖
if len(sys.argv) > 1:
    INPUT = ' '.join(sys.argv[1:])


def parse_update_input(input_str):
    """解析更新输入
    支持:
      '7月25日 201,202'     → (date_str, ['201','202'])
      '2026-07-25 201,202'  → (date_str, ['201','202'])
      '周五201,周五202'      → (date_str, ['201','202'])

    注意: 不再从日期推算周几 — 体彩周几基于开盘日(businessDate)而非比赛日(matchDate)，
    预测文件中的key(如"周五201")已包含正确的周几，直接从文件中提取即可。

    返回: (date_str, match_numbers)
    """
    input_str = input_str.strip()

    # 格式3: 直接周X编号 → 提取编号, 推算本周日期
    if re.match(r'周[一二三四五六日]\d{3}', input_str):
        pairs = re.findall(r'周([一二三四五六日])(\d{3})', input_str)
        if pairs:
            nums = [p[1] for p in pairs]
            wd_map = {'一':0,'二':1,'三':2,'四':3,'五':4,'六':5,'日':6}
            today = datetime.now()
            wd = wd_map[pairs[0][0]]
            diff = (today.weekday() - wd) % 7
            business_date = today - timedelta(days=diff)
            date_str = business_date.strftime('%Y-%m-%d')
            return date_str, nums

    # 格式1: 2026-07-25 201,202
    m = re.match(r'(\d{4}-\d{2}-\d{2})\s+(.+)', input_str)
    if m:
        date_str = m.group(1)
        nums = re.findall(r'\d{3}', m.group(2))
        return date_str, nums

    # 格式2: 7月25日 201,202
    m2 = re.match(r'(\d{1,2})月(\d{1,2})日\s+(.+)', input_str)
    if m2:
        month = int(m2.group(1))
        day = int(m2.group(2))
        year = datetime.now().year
        date_str = f"{year}-{month:02d}-{day:02d}"
        nums = re.findall(r'\d{3}', m2.group(3))
        return date_str, nums

    # 格式4: 纯编号 (用今天日期)
    date_str = datetime.now().strftime('%Y-%m-%d')
    nums = re.findall(r'\d{3}', input_str)
    return date_str, nums


def find_prediction_file(date_str, match_numbers):
    """查找包含目标场次的预测文件

    匹配逻辑:
    1. 用date_str直接查找 pred_YYYYMMDD*.json (通配周几)
    2. 在文件中按编号后3位匹配场次
    3. 体彩周几基于开盘日(businessDate)而非比赛日(matchDate),
       预测文件名中的周几可能与日期推算的周几不同, 因此不做周几精确过滤

    返回: (pred_data, file_path, found_keys) 或 (None, None, [])
      found_keys: 文件中匹配到的完整key列表 (如 ['周一201', '周一202'])
    """
    import glob as glob_module

    date_tag = date_str.replace('-', '')

    # 优先: 按日期查找文件 (通配周几后缀)
    # 匹配: pred_YYYYMMDD.json, pred_YYYYMMDD_周X.json
    candidates = glob_module.glob(os.path.join(PREDICTIONS_DIR, f'pred_{date_tag}*.json'))
    # 按修改时间倒序 (最新的优先)
    candidates.sort(key=lambda f: os.path.getmtime(f), reverse=True)

    for pred_file in candidates:
        try:
            with open(pred_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            results = data.get('results', {})
            # 按编号后3位匹配 (不做周几过滤, 因为体彩周几=开盘日≠比赛日)
            found = [k for k in results if k[-3:] in match_numbers]
            if found:
                print(f"  [匹配] 在 {os.path.basename(pred_file)} 中找到 {len(found)} 场: {found}")
                return data, pred_file, found
        except:
            continue

    # 回退: 扫描所有预测文件 (按时间倒序)
    if not os.path.exists(PREDICTIONS_DIR):
        return None, None, []

    pred_files = sorted(
        [f for f in os.listdir(PREDICTIONS_DIR) if f.startswith('pred_') and f.endswith('.json')],
        reverse=True
    )
    for pf in pred_files:
        filepath = os.path.join(PREDICTIONS_DIR, pf)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            results = data.get('results', {})
            found = [k for k in results if k[-3:] in match_numbers]
            if found:
                print(f"  [匹配] 在 {pf} 中找到 {len(found)} 场: {found}")
                return data, filepath, found
        except:
            continue

    return None, None, []


def fetch_current_odds_only(fid, match_info):
    """获取单场比赛即时赔率 (跳过shuju历史数据)

    节约: 跳过 fetch_shuju_page (~15KB/场) + fixture_id查找
    保留: 所有即时赔率数据 (ouzhi/daxiao/init_*)
    P0-4: sporttery_bonus 通过浅拷贝保留, 确保EV价值分析不丢失
    """
    result = dict(match_info)  # 浅拷贝match_info (含HAD/HHAD/sporttery_bonus)
    result['fixture_id'] = fid
    result['shuju'] = {}  # 占位，后续从缓存填充
    result['ouzhi'] = None
    result['daxiao'] = {'goal_line': 2.5, 'source': '默认值', 'all_goal_lines': [], 'num_bookmakers': 0}
    result['init_ouzhi'] = None
    result['init_yazhi'] = None
    result['init_daxiao'] = None

    # 即时百家欧指
    try:
        result['ouzhi'] = fetch_ouzhi_json(fid)
    except Exception as e:
        result['ouzhi_error'] = str(e)
    # 即时大小球盘口
    try:
        result['daxiao'] = fetch_daxiao_goal_line(fid)
    except Exception as e:
        result['daxiao_error'] = str(e)
    # 初赔AJAX即时数据 (可选数据, 获取失败时保持默认None, 不影响主流程)
    try:
        result['init_ouzhi'] = fetch_initial_ouzhi(fid)
    except Exception:
        pass
    try:
        result['init_yazhi'] = fetch_initial_yazhi(fid)
    except Exception:
        pass
    try:
        result['init_daxiao'] = fetch_initial_daxiao(fid)
    except Exception:
        pass

    return result


def compare_predictions(old_pred, new_pred, history=None):
    """对比新旧预测，生成变更列表 + 趋势分析

    新增 history 参数: 之前的更新历史列表，用于判断趋势
    返回: (changes, trend_info)
      changes: 变更列表(同原来)
        [{'field': 'HAD.odds', 'old': 1.55, 'new': 1.49, 'type': '赔率变化'},
         {'field': 'HAD.conf', 'old': '★★', 'new': '★★★', 'type': '星级变化'},
         {'field': 'HAD.dir', 'old': '平', 'new': '胜', 'type': '方向变化'}]
      trend_info: {'direction': '强化/弱化/稳定/反转', 'details': [...]}
    """
    changes = []

    def add_change(field, old_val, new_val, change_type='变化'):
        if old_val != new_val:
            changes.append({
                'field': field,
                'old': str(old_val),
                'new': str(new_val),
                'type': change_type,
            })

    # HAD对比
    old_had = old_pred.get('HAD', {})
    new_had = new_pred.get('HAD', {})
    add_change('HAD方向', old_had.get('dir', ''), new_had.get('dir', ''), '方向变化')
    add_change('HAD赔率', old_had.get('odds', ''), new_had.get('odds', ''), '赔率变化')
    add_change('HAD星级', old_had.get('conf', ''), new_had.get('conf', ''), '星级变化')
    add_change('HAD概率', old_had.get('p', ''), new_had.get('p', ''), '概率变化')

    # HHAD对比
    old_hhad = old_pred.get('HHAD', {})
    new_hhad = new_pred.get('HHAD', {})
    add_change('HHAD方向', old_hhad.get('dir', ''), new_hhad.get('dir', ''), '方向变化')
    add_change('HHAD赔率', old_hhad.get('odds', ''), new_hhad.get('odds', ''), '赔率变化')
    add_change('HHAD星级', old_hhad.get('conf', ''), new_hhad.get('conf', ''), '星级变化')

    # 初赔对比
    old_init = old_pred.get('initial', {})
    new_init = new_pred.get('initial', {})
    add_change('欧指即时', old_init.get('ouzhi_now', ''), new_init.get('ouzhi_now', ''), '赔率变化')
    add_change('亚指即时', old_init.get('yazhi_now', ''), new_init.get('yazhi_now', ''), '盘口变化')
    add_change('大小即时', old_init.get('dx_now', ''), new_init.get('dx_now', ''), '盘口变化')

    # 比分对比
    old_score = old_pred.get('score', {})
    new_score = new_pred.get('score', {})
    add_change('比分Top3', old_score.get('top3', '')[:40], new_score.get('top3', '')[:40], '比分变化')
    add_change('大小方向', old_score.get('main_dir', ''), new_score.get('main_dir', ''), '方向变化')
    add_change('主盘口', old_score.get('market_gl_str', ''), new_score.get('market_gl_str', ''), '盘口变化')

    # 半全场对比 (Pro 3.2)
    old_hf = old_pred.get('half_full', {})
    new_hf = new_pred.get('half_full', {})
    add_change('半全场主推', old_hf.get('main', ''), new_hf.get('main', ''), '半全场变化')

    # 总进球数对比 (Pro 3.2)
    old_tg = old_pred.get('total_goals', {})
    new_tg = new_pred.get('total_goals', {})
    add_change('总进球主推', old_tg.get('main', ''), new_tg.get('main', ''), '总进球变化')

    # λ值对比
    add_change('λ值', old_pred.get('lam', ''), new_pred.get('lam', ''), '参数变化')

    # 跨玩法推荐对比 (Pro 3.9)
    old_cm = old_pred.get('cross_market', {})
    new_cm = new_pred.get('cross_market', {})
    old_pb = old_cm.get('primary_bet', {})
    new_pb = new_cm.get('primary_bet', {})
    if old_pb and new_pb:
        add_change('主推选项', old_pb.get('option', ''), new_pb.get('option', ''), '推荐变化')
        add_change('主推概率', old_pb.get('prob', ''), new_pb.get('prob', ''), '概率变化')
    old_hpb = old_cm.get('hhad_primary_bet', {})
    new_hpb = new_cm.get('hhad_primary_bet', {})
    if old_hpb and new_hpb:
        add_change('HHAD主推', old_hpb.get('option', ''), new_hpb.get('option', ''), '推荐变化')
    old_pdb = old_cm.get('pure_direction_bet', {})
    new_pdb = new_cm.get('pure_direction_bet', {})
    if old_pdb and new_pdb:
        add_change('纯方向', old_pdb.get('option', ''), new_pdb.get('option', ''), '推荐变化')
    old_dr = old_cm.get('double_recommend', {})
    new_dr = new_cm.get('double_recommend', {})
    if old_dr and new_dr:
        add_change('双选保险', old_dr.get('option', ''), new_dr.get('option', ''), '推荐变化')

    # ===== 新增: 趋势分析 =====
    trend_details = []
    trend_direction = '稳定'

    # HAD赔率趋势
    old_had_odds = old_pred.get('HAD', {}).get('odds', 0)
    new_had_odds = new_pred.get('HAD', {}).get('odds', 0)
    if isinstance(old_had_odds, (int, float)) and isinstance(new_had_odds, (int, float)):
        if old_had_odds > 0:
            change_pct = (new_had_odds - old_had_odds) / old_had_odds * 100
            if abs(change_pct) > 2:
                trend_details.append(f"HAD赔率{'↑' if change_pct > 0 else '↓'}{abs(change_pct):.1f}%")
                trend_direction = '弱化' if change_pct > 0 else '强化'

    # 方向是否反转
    old_had_dir = old_pred.get('HAD', {}).get('dir', '')
    new_had_dir = new_pred.get('HAD', {}).get('dir', '')
    if old_had_dir and new_had_dir and old_had_dir != new_had_dir:
        trend_direction = '反转'
        trend_details.append(f"⚠️ HAD方向 {old_had_dir}→{new_had_dir}")

    # 初赔变化趋势
    old_init = old_pred.get('initial', {})
    new_init = new_pred.get('initial', {})
    if old_init.get('ouzhi_now') and new_init.get('ouzhi_now'):
        # 提取欧指主胜赔率
        try:
            old_w = float(old_init['ouzhi_now'].split('/')[0])
            new_w = float(new_init['ouzhi_now'].split('/')[0])
            if abs(new_w - old_w) > 0.05:
                trend_details.append(f"欧指主胜{'↓' if new_w < old_w else '↑'}{abs(new_w-old_w):.2f}")
        except:
            pass

    # 从history中提取更早的趋势
    if history:
        for h in history[-2:]:  # 最近2次历史
            old_changes = h.get('changes', {})
            for k, v in old_changes.items():
                for c in v:
                    if c.get('type') == '方向变化':
                        trend_details.append(f"历史: {k} {c.get('field')} {c.get('old')}→{c.get('new')}")

    trend_info = {
        'direction': trend_direction,
        'details': trend_details,
    }

    return changes, trend_info


def format_change_report(changes, match_key):
    """格式化变更报告"""
    if not changes:
        return f"  ✅ {match_key} 无变化"

    lines = [f"  📝 {match_key} 变更 ({len(changes)}项):"]
    for c in changes:
        arrow = '→'
        lines.append(f"    [{c['type']}] {c['field']}: {c['old']} {arrow} {c['new']}")
    return '\n'.join(lines)


def format_prediction_summary(key, meta, result):
    """格式化单场预测摘要"""
    m = meta.get(key, {})
    had = result.get('HAD', {})
    hhad = result.get('HHAD', {})
    sc = result.get('score', {})
    init = result.get('initial', {})

    ds = m.get('data_source', '500.com')
    fr = m.get('fallback_reason', '')
    ds_display = f" [{ds}]" if ds != '500.com' else ""
    if fr:
        ds_display = f" [{ds} ⚠️{fr}]"

    lines = [
        f"  {key} {m.get('home','')} vs {m.get('away','')}{ds_display}",
        f"    HAD:  {had.get('dir','')}@{had.get('odds','')} {had.get('conf','')} P={had.get('p','')}",
        f"    HHAD: {hhad.get('dir','')}@{hhad.get('odds','')} {hhad.get('conf','')} 让{hhad.get('handicap','')}",
    ]

    # 初赔对比
    if init:
        parts = []
        if 'ouzhi_init' in init:
            parts.append(f"欧指:{init.get('ouzhi_init','')}→{init.get('ouzhi_now','')}")
        if 'yazhi_init' in init:
            parts.append(f"亚指:{init.get('yazhi_init','')}→{init.get('yazhi_now','')}")
        if 'dx_init' in init:
            parts.append(f"大小:{init.get('dx_init','')}→{init.get('dx_now','')}")
        if parts:
            lines.append(f"    初赔: {' | '.join(parts)}")

    lines.append(f"    比分({sc.get('main_dir','')}{sc.get('market_gl_str','')}): {sc.get('top3','')}")
    lines.append(f"    λ={result.get('lam','')} | {result.get('market_gl_source','')}")

    return '\n'.join(lines)


# ============================================================
# Main: 增量更新流程
# ============================================================
def main():
    t0 = time.time()

    print("=" * 60)
    print("【预测更新模块 Ultra 5.0】— 增量更新即时赔率")
    print(f"输入: {INPUT}")
    print("=" * 60)

    # ===== Step 1: 解析输入 =====
    date_str, match_numbers = parse_update_input(INPUT)
    print(f"\n[Step1] 解析: 日期={date_str}, 编号={match_numbers}")

    # ===== Step 2: 加载已有预测文件 =====
    print(f"\n[Step2] 查找已有预测文件...")
    pred_data, pred_file, found_keys = find_prediction_file(date_str, match_numbers)
    if not pred_data:
        print(f"  ❌ 未找到包含编号 {match_numbers} 的预测文件")
        print(f"  请先运行完整预测: python3 v215_e2e.py")
        return

    print(f"  ✅ 加载: {pred_file}")
    print(f"  保存时间: {pred_data.get('saved_at', '未知')}")
    print(f"  匹配场次: {found_keys}")

    meta = pred_data.get('meta', {})
    cache = pred_data.get('cache', {})
    old_results = pred_data.get('results', {})
    history = pred_data.get('history', [])

    # ===== Step 3: 获取即时sporttery赔率 =====
    print(f"\n[Step3] 获取即时体彩赔率...")
    # 设置全局变量供 fetch_sporttery_matches 使用
    v215_e2e.TARGET_WEEKDAY = None  # 不过滤周几，通过key匹配
    v215_e2e.TARGET_DATE = None

    t3 = time.time()
    sporttery_matches = fetch_sporttery_matches(match_numbers)
    dt3 = time.time() - t3
    raw_sporttery = json.dumps(sporttery_matches, ensure_ascii=False)
    print(f"  sporttery: 获取{len(sporttery_matches)}场, {fmt_size(raw_sporttery)}, {dt3:.1f}s")

    # P0-4: 并行获取竞彩固定奖金 (比分/总进球/半全场赔率, 供EV价值分析)
    # 更新流程中必须重新获取, 否则sporttery_bonus丢失导致EV分析失效
    if sporttery_matches:
        with ThreadPoolExecutor(max_workers=4) as pool:
            bonus_futures = {pool.submit(fetch_sporttery_fixed_bonus, mi.get('match_id')): key
                             for key, mi in sporttery_matches.items() if mi.get('match_id')}
            for fut in as_completed(bonus_futures):
                key = bonus_futures[fut]
                try:
                    bonus = fut.result()
                    if bonus:
                        sporttery_matches[key]['sporttery_bonus'] = bonus
                except Exception:
                    pass
        n_bonus = sum(1 for mi in sporttery_matches.values() if mi.get('sporttery_bonus'))
        if n_bonus:
            print(f"  [固定奖金] {n_bonus}/{len(sporttery_matches)}场获取成功")

    # ===== Step 4: 获取500.com即时赔率 (跳过shuju) =====
    print(f"\n[Step4] 获取500.com即时赔率 (跳过历史数据)...")
    t4 = time.time()

    all_data = {}
    cache_hits = 0
    cache_misses = 0

    # 预获取500.com fixture_ids (用于fid=0的场次补全)
    fixture_map = {}
    needs_fid = any(meta.get(k, {}).get('fid', 0) == 0 for k in found_keys)
    if needs_fid:
        print("  [补全] 部分场次fid=0, 获取500.com fixture_ids...")
        try:
            fixture_map = v215_e2e.fetch_500_fixture_ids() or {}
            if fixture_map:
                print(f"  [补全] 获取到 {len(fixture_map)} 个fixture_id")
        except Exception as e:
            print(f"  [补全] fixture_ids获取失败: {e}")

    def fetch_one_update(key):
        """单场更新数据获取"""
        if key not in meta:
            return key, None

        fid = meta[key].get('fid', 0)
        if not fid:
            # 🔒 数据源策略 (锁定): fid=0 场次必须优先尝试 nowscore (主力辅助),
            # 实在抓不到才允许降级 500.com fixture_map 补全
            old_m = meta[key]
            match_info = sporttery_matches.get(key, {})
            if not match_info:
                match_info = {
                    'home': old_m.get('home', ''),
                    'away': old_m.get('away', ''),
                    'league': old_m.get('league', ''),
                    'match_date': old_m.get('match_date', ''),
                    'match_time': old_m.get('match_time', ''),
                    'weekday': old_m.get('weekday', ''),
                    'HAD': {}, 'HHAD': {},
                }
            # P0-4: 从缓存保留 sporttery_bonus (固定奖金赔率)
            cached_bonus = cache.get(key, {}).get('sporttery_bonus')
            if cached_bonus and 'sporttery_bonus' not in match_info:
                match_info['sporttery_bonus'] = cached_bonus
            # --- 首选: nowscore 更新 ---
            try:
                import nowscore_fetch as nsf
                ns_data = nsf.fetch_nowscore_match_data(match_info.get('home', ''), match_info.get('away', ''))
                if ns_data:
                    ns_data['HAD'] = match_info.get('HAD', {})
                    ns_data['HHAD'] = match_info.get('HHAD', {})
                    ns_data['fixture_id'] = 0
                    # P0-4: 保留 sporttery_bonus
                    if match_info.get('sporttery_bonus'):
                        ns_data['sporttery_bonus'] = match_info['sporttery_bonus']
                    # P1-2: 标记数据源 (策略首选路径)
                    ns_data['data_source'] = 'nowscore'
                    # P1-3: 缓存合并方向修正 — 新数据优先, 旧缓存仅填补空缺
                    cached_shuju = cache.get(key, {}).get('shuju', {})
                    if cached_shuju:
                        merged = dict(cached_shuju)  # 以旧缓存为基础
                        merged.update(ns_data.get('shuju', {}))  # 新数据覆盖旧数据
                        ns_data['shuju'] = merged
                    return key, ns_data, True
                print(f"  [降级] {key} nowscore无数据 → 降级500.com fixture_map补全")
            except Exception as e:
                print(f"  [降级] {key} nowscore更新失败: {e} → 降级500.com fixture_map补全")
            # --- 降级: 500.com fixture_map 补全 (仅nowscore失败后) ---
            fid = fixture_map.get(key, 0)
            if fid:
                print(f"  [补全] {key} fid: 0 → {fid} (nowscore失败后降级)")
                meta[key]['fid'] = fid  # 更新meta供后续保存
                meta[key]['fallback_reason'] = 'nowscore无数据/失败, 降级500.com'
            else:
                return key, None

        # 从sporttery获取即时HAD/HHAD
        match_info = sporttery_matches.get(key, {})
        if not match_info:
            # sporttery未返回(可能已开场), 使用meta中的队名信息, HAD/HHAD置空
            # predict_match已有空HAD/HHAD的降级处理(用500.com欧指代替)
            old_m = meta[key]
            match_info = {
                'home': old_m.get('home', ''),
                'away': old_m.get('away', ''),
                'league': old_m.get('league', ''),
                'match_date': old_m.get('match_date', ''),
                'match_time': old_m.get('match_time', ''),
                'weekday': old_m.get('weekday', ''),
                'HAD': {},
                'HHAD': {},
            }
            # P0-4: 从缓存保留 sporttery_bonus
            cached_bonus = cache.get(key, {}).get('sporttery_bonus')
            if cached_bonus:
                match_info['sporttery_bonus'] = cached_bonus
            # P1-2/P1-4: 标记数据源和降级原因
            match_info['data_source'] = 'sporttery(保底-未返回)'
            match_info['fallback_reason'] = 'sporttery API未返回该场次, HAD/HHAD用500.com欧指代替'
            print(f"  ⚠️ {key} sporttery未返回, HAD/HHAD用500.com欧指代替")

        # 获取即时赔率 (跳过shuju)
        fresh_data = fetch_current_odds_only(fid, match_info)

        # P1-2: 若主路径未标记data_source, 补充标记
        if 'data_source' not in fresh_data:
            fresh_data['data_source'] = '500.com'

        # 合并缓存的shuju数据 (P1-1: 场次级时间戳检查新鲜度)
        cached_entry = cache.get(key, {})
        cached_shuju = cached_entry.get('shuju', {})
        if cached_shuju:
            # P1-1: 优先使用场次级时间戳, 回退到全局 saved_at
            cache_time_str = cached_entry.get('cached_at', '') or pred_data.get('saved_at', '')
            is_fresh = False
            if cache_time_str:
                try:
                    cache_time = datetime.strptime(cache_time_str, '%Y-%m-%d %H:%M:%S')
                    age_hours = (datetime.now() - cache_time).total_seconds() / 3600
                    is_fresh = age_hours < 24
                except:
                    is_fresh = True  # 无法解析时间则视为新鲜
            else:
                is_fresh = True  # 无时间戳则视为新鲜

            if is_fresh:
                # P1-3: 合并方向修正 — 新数据(fresh_data)优先, 缓存仅填补空缺
                if fresh_data.get('shuju'):
                    merged = dict(cached_shuju)
                    merged.update(fresh_data['shuju'])
                    fresh_data['shuju'] = merged
                else:
                    fresh_data['shuju'] = cached_shuju
                return key, fresh_data, True   # cache hit
            else:
                print(f"  [缓存过期] {key} 超过24小时, 重新获取shuju...")
                try:
                    fresh_data['shuju'] = fetch_shuju_page(fid)
                except:
                    fresh_data['shuju'] = cached_shuju  # 回退到旧缓存
                return key, fresh_data, False  # cache miss (refreshed)
        else:
            # 缓存缺失, 回退获取shuju
            print(f"  [缓存缺失] {key} shuju为空, 重新获取...")
            try:
                fresh_data['shuju'] = fetch_shuju_page(fid)
            except:
                fresh_data['shuju'] = {}
            return key, fresh_data, False  # cache miss

    # 并行获取
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fetch_one_update, key): key for key in found_keys}
        for future in as_completed(futures):
            key = futures[future]
            try:
                result = future.result()
                if result and result[1]:
                    all_data[result[0]] = result[1]
                    if result[2]:
                        cache_hits += 1
                    else:
                        cache_misses += 1
            except Exception as e:
                print(f"  ❌ {key} 获取失败: {e}")

    dt4 = time.time() - t4
    raw_all = json.dumps(all_data, ensure_ascii=False)
    print(f"  500.com: 获取{len(all_data)}场, {fmt_size(raw_all)}, {dt4:.1f}s")
    print(f"  缓存命中: {cache_hits}场, 缓存缺失: {cache_misses}场")

    # P1-2/P1-4: 更新meta中的数据源和降级原因
    for key in found_keys:
        if key in all_data:
            if key not in meta:
                meta[key] = {}
            meta[key]['data_source'] = all_data[key].get('data_source', meta[key].get('data_source', '500.com'))
            if all_data[key].get('fallback_reason'):
                meta[key]['fallback_reason'] = all_data[key]['fallback_reason']

    # ===== Step 5: 重算预测 =====
    print(f"\n[Step5] 重算预测...")
    t5 = time.time()
    new_results = {}
    all_changes = {}
    all_trends = {}

    for key in found_keys:
        if key not in all_data:
            continue
        new_result = predict_match(key, all_data[key])
        new_results[key] = new_result

        # 对比新旧预测 (传入历史记录用于趋势分析)
        old_result = old_results.get(key, {})
        changes, trend_info = compare_predictions(old_result, new_result, history)
        all_changes[key] = changes
        all_trends[key] = trend_info

    dt5 = time.time() - t5

    # ===== Step 6: 输出变更报告 =====
    print(f"\n{'=' * 60}")
    print("【变更对比报告】")
    print(f"{'=' * 60}")
    total_changes = 0
    for key in found_keys:
        if key in all_changes:
            changes = all_changes[key]
            total_changes += len(changes)
            print()
            print(format_change_report(changes, key))

    if total_changes == 0:
        print("\n  ✅ 所有场次预测无变化")
    else:
        print(f"\n  📊 共 {total_changes} 项变更")

    # ===== Step 6b: 重大变更警报 =====
    print(f"\n{'=' * 60}")
    print("【重大变更警报】")
    print(f"{'=' * 60}")
    alerts = []
    for key in found_keys:
        if key in all_changes:
            changes = all_changes[key]
            trends = all_trends.get(key, {})  # 从上面的趋势分析获取
            # 检测: 方向反转
            direction_changes = [c for c in changes if c['type'] == '方向变化']
            if direction_changes:
                for dc in direction_changes:
                    alerts.append(f"🚨 {key} {dc['field']}: {dc['old']}→{dc['new']} (方向反转!)")
            # 检测: 赔率大幅变化(>10%)
            for c in changes:
                if c['type'] == '赔率变化':
                    try:
                        old_v = float(c['old'])
                        new_v = float(c['new'])
                        if old_v > 0 and abs((new_v - old_v) / old_v) > 0.10:
                            alerts.append(f"⚡ {key} {c['field']}: {c['old']}→{c['new']} ({(new_v-old_v)/old_v*100:+.1f}%)")
                    except:
                        pass
            # 检测: 趋势反转
            if trends.get('direction') == '反转':
                alerts.append(f"🔄 {key} 趋势反转: {', '.join(trends.get('details', []))}")
            # 检测: 置信度变化 (Pro 3.1: 5星制半星维度检测)
            conf_changes = [c for c in changes if c['type'] == '星级变化']
            for cc in conf_changes:
                old_score = stars_to_score(cc['old'])
                new_score = stars_to_score(cc['new'])
                delta = new_score - old_score
                if delta >= 1.0:
                    alerts.append(f"⭐ {key} {cc['field']} 置信度大幅提升: {cc['old']}→{cc['new']} (+{delta:.1f})")
                elif delta >= 0.5:
                    alerts.append(f"⭐ {key} {cc['field']} 置信度提升: {cc['old']}→{cc['new']} (+{delta:.1f})")
                elif delta <= -1.0:
                    alerts.append(f"📉 {key} {cc['field']} 置信度大幅下降: {cc['old']}→{cc['new']} ({delta:.1f})")
                elif delta <= -0.5:
                    alerts.append(f"📉 {key} {cc['field']} 置信度下降: {cc['old']}→{cc['new']} ({delta:.1f})")

    if alerts:
        for a in alerts:
            print(f"  {a}")
        print(f"\n  ⚠️ 共 {len(alerts)} 条警报")
    else:
        print("  ✅ 无重大变更")

    # ===== Step 7: 输出更新后的预测摘要 =====
    print(f"\n{'=' * 60}")
    print("【更新后预测摘要】")
    print(f"{'=' * 60}")
    for key in found_keys:
        if key in new_results:
            print()
            print(format_prediction_summary(key, meta, new_results[key]))

    # ===== Step 8: 保存更新后的预测文件 =====
    print(f"\n{'=' * 60}")
    print("【保存更新结果】")

    # 构建更新后的缓存 (保留shuju+sporttery_bonus+场次级时间戳)
    updated_cache = dict(cache)
    cache_ts = time.strftime('%Y-%m-%d %H:%M:%S')
    for key in found_keys:
        if key in all_data:
            updated_cache[key] = {
                'shuju': all_data[key].get('shuju', {}),
                # P0-4: 保留 sporttery_bonus 供下次更新EV分析使用
                'sporttery_bonus': all_data[key].get('sporttery_bonus'),
                # P1-1: 场次级时间戳, 替代全局saved_at做新鲜度判断
                'cached_at': cache_ts,
                # P1-2/P1-4: 保留数据源和降级原因
                'data_source': all_data[key].get('data_source', '500.com'),
                'fallback_reason': all_data[key].get('fallback_reason', ''),
            }

    # 历史记录: 保存上次的预测快照
    history_entry = {
        'timestamp': pred_data.get('saved_at', ''),
        'update_time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'changes': {k: v for k, v in all_changes.items() if v},
    }
    history.append(history_entry)

    # 合并: 旧结果中未被更新的场次保留, 更新的场次用新结果
    merged_results = dict(old_results)
    for key in found_keys:
        if key in new_results:
            merged_results[key] = new_results[key]

    updated_pred = {
        'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'updated_from': pred_data.get('saved_at', ''),
        'meta': meta,
        'results': merged_results,
        'cache': updated_cache,
        'history': history,
    }

    # 保存到文件
    with open(pred_file, 'w', encoding='utf-8') as f:
        json.dump(updated_pred, f, ensure_ascii=False, indent=1)
    print(f"  ✅ 已更新: {pred_file}")
    print(f"  首次预测: {pred_data.get('saved_at', '未知')}")
    print(f"  本次更新: {updated_pred['saved_at']}")
    print(f"  历史版本: {len(history)}")

    # ===== Step 9: Token节约统计 =====
    total_time = time.time() - t0
    print(f"\n{'=' * 60}")
    print("【Token节约统计】")
    print(f"{'=' * 60}")

    # 估算: 完整流程含fixture_id+shuju, 更新流程跳过 (按~4字符/token粗估)
    num_matches = len(found_keys)
    full_estimate = 2000 + 10000 + num_matches * (15000 + 5000)  # bytes
    update_estimate = 2000 + num_matches * 5000  # bytes
    saved_estimate = full_estimate - update_estimate
    actual_bytes = len(raw_all) + len(raw_sporttery)

    print(f"  完整流程估算: ~{full_estimate}B / ~{full_estimate//4}T")
    print(f"  更新流程实际: ~{actual_bytes}B / ~{estimate_tokens(raw_all)+estimate_tokens(raw_sporttery)}T")
    print(f"  节约: ~{saved_estimate}B ({saved_estimate/full_estimate*100:.0f}%)")
    print(f"  缓存命中: {cache_hits}/{num_matches}场 | 总耗时: {total_time:.1f}s")


if __name__ == '__main__':
    main()
