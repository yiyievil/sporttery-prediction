#!/usr/bin/env python3
"""模拟投注模块 (Ultra 6.0)

触发关键词: "模拟"
功能:
    1. 读取最新预测结果, 按置信度选场
    2. 自动构建串关 (2串1~5串1, 依据置信度决定串数)
    3. 每注20元 (2元×10倍), 存入SQLite
    4. 验证赛果时自动计算盈亏

体彩串关规则:
    - M串1: M场比赛全部猜对才中奖
    - 奖金 = 2元 × 各场赔率连乘 × 倍数
    - 20元 = 2元 × 10倍

用法:
    python3 v215_simulate.py                    # 自动选最新预测文件
    python3 v215_simulate.py 2026-07-25         # 指定日期
    python3 v215_simulate.py 周六               # 指定周几
"""

import os
import sys
import json
import sqlite3
import re
from datetime import datetime
from pathlib import Path

# ===== 配置 =====
# Ultra-Opt: 通用路径 (旧版硬编码 '/workspace')
_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
PRED_DIR = os.path.join(_WORKSPACE, 'predictions')
DB_PATH = os.path.join(PRED_DIR, 'regression.db')
STAKE = 20          # 每注20元
BET_UNIT = 2         # 体彩每注2元
MULTIPLIER = STAKE // BET_UNIT  # 倍数 = 10

# 星级→数值 (用于排序和串关决策)
STAR_SCORE = {
    '★★★★★': 5.0, '★★★★½': 4.5, '★★★★': 4.0, '★★★½': 3.5,
    '★★★': 3.0, '★★½': 2.5, '★★': 2.0, '★½': 1.5, '★': 1.0,
}

# 串关决策: 根据最低星级决定最大串数
def decide_max_legs(matches):
    """根据选中场次的最低置信度决定最大串数

    规则:
        全部 ★★★★★ → 最多5串1
        全部 ★★★★+ → 最多4串1
        全部 ★★★+  → 最多3串1
        其他        → 2串1
    """
    if not matches:
        return 2
    min_score = min(m['star_score'] for m in matches)
    if min_score >= 5.0:
        return min(5, len(matches))
    elif min_score >= 4.0:
        return min(4, len(matches))
    elif min_score >= 3.0:
        return min(3, len(matches))
    else:
        return 2


def stars_to_score(stars_str):
    """星级字符串转数值"""
    return STAR_SCORE.get(stars_str, 0)


def find_pred_files(date_arg=None, weekday=None):
    """查找预测文件, 优先最新"""
    files = sorted(Path(PRED_DIR).glob('pred_*.json'), reverse=True)
    if not files:
        return []

    # 解析日期为 YYYYMMDD 格式
    date_tag = None
    if date_arg:
        # "2026-07-25" → "20260725"
        m = re.match(r'(\d{4})-(\d{2})-(\d{2})', date_arg)
        if m:
            date_tag = m.group(1) + m.group(2) + m.group(3)
        else:
            # "7月25日" → "20260725" (补零+补年份)
            m2 = re.match(r'(\d{1,2})月(\d{1,2})日', date_arg)
            if m2:
                month, day = int(m2.group(1)), int(m2.group(2))
                year = datetime.now().year
                date_tag = f'{year}{month:02d}{day:02d}'

    if date_tag and weekday:
        # 精确匹配: 日期 + 周几
        matched = [f for f in files if date_tag in f.name and weekday in f.name]
        if matched:
            return matched
        # 回退: 仅日期
        matched = [f for f in files if date_tag in f.name]
        return matched if matched else files[:1]
    if date_tag:
        matched = [f for f in files if date_tag in f.name]
        return matched if matched else files[:1]
    if weekday:
        matched = [f for f in files if weekday in f.name]
        return matched if matched else files[:1]
    return files[:1]


def load_predictions(pred_file):
    """加载预测文件, 提取可投注的场次"""
    with open(pred_file, 'r') as f:
        data = json.load(f)

    meta = data.get('meta', {})
    results = data.get('results', {})

    candidates = []
    for key, r in results.items():
        m = meta.get(key, {})
        had = r.get('HAD', {})
        hhad = r.get('HHAD', {})
        cm = r.get('cross_market', {})

        # 选场策略: 模拟投注优先追求命中率, 选择置信度最高的方向
        # 1. 收集 primary_bet 和 HHAD/HAD 方向, 比较置信度
        # 2. primary_bet 概率需>=30% 且置信度不低于HHAD方向才采用
        # 3. 否则回退到 HHAD 方向 (通常置信度最高)
        pb = cm.get('primary_bet') if cm else None
        pb_prob = pb.get('prob', 0) if pb else 0
        pb_odds = pb.get('odds', 0) if pb else 0

        # HHAD 方向 (通常最高置信度)
        hhad_dir = hhad.get('dir', '')
        hhad_odds = hhad.get('odds', 0)
        hhad_conf = hhad.get('conf', '')
        hhad_score = stars_to_score(hhad_conf)

        # primary_bet 对应方向的置信度
        if pb and pb_odds > 0:
            pb_option = pb.get('option', '')
            if '让' in pb_option:
                pb_conf = hhad_conf
            else:
                pb_conf = had.get('conf', '')
            pb_score = stars_to_score(pb_conf)
        else:
            pb_score = 0

        # 决策: 用 primary_bet 还是 HHAD 方向
        use_primary = (pb and pb_odds > 0 and pb_prob >= 30
                       and pb_score >= hhad_score - 0.5)  # 允许半星差距

        if use_primary:
            option = pb.get('option', '')
            odds = pb_odds
            # 修复: 必须先检查HHAD, 否则'HAD' in 'HHAD让胜' = True 导致误判
            if option.startswith('HHAD'):
                market = 'HHAD'
            else:
                market = 'HAD'
            conf = pb_conf
            bet_dir = option.replace('HAD', '').replace('HHAD', '').replace('双选', '')
        else:
            # 回退: 取HHAD方向 (通常置信度更高)
            option = f"HHAD{hhad_dir}"
            odds = hhad_odds
            conf = hhad_conf
            market = 'HHAD'
            bet_dir = hhad_dir
            if not bet_dir or not odds:
                # 再回退到HAD
                option = f"HAD{had.get('dir', '')}"
                odds = had.get('odds', 0)
                conf = had.get('conf', '')
                market = 'HAD'
                bet_dir = had.get('dir', '')

        if not bet_dir or not odds or odds <= 1.0:
            continue

        star_score = stars_to_score(conf)
        if star_score < 2.0:  # 最低 ★★ 才考虑
            continue

        candidates.append({
            'key': key,
            'home': m.get('home', ''),
            'away': m.get('away', ''),
            'league': m.get('league', ''),
            'match_date': m.get('match_date', ''),
            'match_time': m.get('match_time', ''),
            'market': market,
            'option': option,
            'bet_dir': bet_dir,  # 胜/平/负 或 让胜/让平/让负
            'odds': odds,
            'conf': conf,
            'star_score': star_score,
        })

    # 按置信度降序
    candidates.sort(key=lambda x: x['star_score'], reverse=True)
    return candidates, data.get('meta', {})


def select_and_build_parlay(candidates):
    """选场并构建串关

    策略:
        1. 过滤 ★★ 以下
        2. 取置信度最高的N场
        3. 根据最低置信度决定串数
        4. 至少2场 (2串1), 最多5场 (5串1)
    """
    if len(candidates) < 2:
        print(f"  ⚠️ 可投注场次仅{len(candidates)}场, 不足2场, 无法构建串关")
        return None

    # 取前5场 (最多5串1)
    pool = candidates[:5]
    max_legs = decide_max_legs(pool)

    # 选择: 取置信度最高的 max_legs 场
    selected = pool[:max_legs]
    legs = len(selected)

    # 构建串关
    total_odds = 1.0
    bet_legs = []
    for s in selected:
        total_odds *= s['odds']
        bet_legs.append({
            'key': s['key'],
            'home': s['home'],
            'away': s['away'],
            'league': s['league'],
            'market': s['market'],
            'option': s['option'],
            'bet_dir': s['bet_dir'],
            'odds': s['odds'],
            'conf': s['conf'],
        })

    potential_payout = round(BET_UNIT * total_odds * MULTIPLIER, 2)

    return {
        'legs': legs,
        'bet_type': f'{legs}串1',
        'stake': STAKE,
        'multiplier': MULTIPLIER,
        'total_odds': round(total_odds, 4),
        'potential_payout': potential_payout,
        'matches': bet_legs,
    }


def init_sim_db():
    """初始化模拟投注表"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sim_bets (
        bet_id TEXT PRIMARY KEY,
        created_at TEXT,
        pred_file TEXT,
        bet_date TEXT,
        bet_type TEXT,
        legs INTEGER,
        stake REAL,
        multiplier INTEGER,
        total_odds REAL,
        potential_payout REAL,
        matches_json TEXT,
        status TEXT DEFAULT 'pending',
        actual_payout REAL DEFAULT 0,
        profit REAL DEFAULT 0,
        verified_at TEXT
    )''')
    conn.commit()
    conn.close()


def save_bet(parlay, pred_file, pred_meta):
    """保存模拟投注到数据库"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().strftime('%Y%m%d_%H%M%S')
    import random as _rnd
    bet_id = f'SIM_{now}_{_rnd.randint(100, 999)}'

    # 从pred文件名提取日期
    fname = os.path.basename(pred_file)
    date_match = re.search(r'pred_(\d{8})', fname)
    bet_date = date_match.group(1) if date_match else now[:8]

    matches_json = json.dumps(parlay['matches'], ensure_ascii=False)

    c.execute('''INSERT INTO sim_bets
        (bet_id, created_at, pred_file, bet_date, bet_type, legs, stake, multiplier,
         total_odds, potential_payout, matches_json, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')''',
        (bet_id, now, pred_file, bet_date, parlay['bet_type'], parlay['legs'],
         parlay['stake'], parlay['multiplier'], parlay['total_odds'],
         parlay['potential_payout'], matches_json))

    conn.commit()
    conn.close()
    return bet_id


def print_bet_card(bet_id, parlay, pred_file):
    """打印投注卡"""
    print('=' * 60)
    print(f'  【模拟投注】{bet_id}')
    print(f'  来源: {os.path.basename(pred_file)}')
    print(f'  类型: {parlay["bet_type"]}  |  投注: {parlay["stake"]}元 ({parlay["multiplier"]}倍)')
    print(f'  总赔率: {parlay["total_odds"]}  |  潜在奖金: {parlay["potential_payout"]}元')
    print('-' * 60)
    for i, m in enumerate(parlay['matches'], 1):
        print(f'  第{i}场 {m["key"]} {m["home"]} vs {m["away"]}')
        print(f'       {m["market"]} {m["option"]} @{m["odds"]} {m["conf"]}')
    print('-' * 60)
    print(f'  中奖条件: {parlay["legs"]}场全部猜对')
    print(f'  若全中: {parlay["potential_payout"]}元 (净赚{parlay["potential_payout"] - parlay["stake"]:.2f}元)')
    print('=' * 60)


def show_pending_bets():
    """显示所有待结算的模拟投注"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM sim_bets WHERE status='pending' ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()

    if not rows:
        print("  无待结算的模拟投注")
        return

    print(f"\n{'='*60}")
    print(f"  待结算模拟投注 ({len(rows)}注)")
    print(f"{'='*60}")
    for row in rows:
        bet_id = row[0]
        bet_type = row[4]
        stake = row[6]
        total_odds = row[8]
        potential = row[9]
        matches_json = row[10]
        matches = json.loads(matches_json)
        print(f"\n  {bet_id} | {bet_type} | {stake}元 | 总赔率{total_odds}")
        for i, m in enumerate(matches, 1):
            print(f"    {i}. {m['key']} {m['home']} vs {m['away']} → {m['option']}@{m['odds']}")
        print(f"    潜在奖金: {potential}元")
    print(f"{'='*60}")


def show_history():
    """显示历史模拟投注统计"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT bet_id, bet_type, stake, total_odds, potential_payout, status, actual_payout, profit FROM sim_bets ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()

    if not rows:
        print("  无历史模拟投注记录")
        return

    total_stake = sum(r[2] for r in rows)
    total_payout = sum(r[6] or 0 for r in rows)
    total_profit = total_payout - total_stake
    won = sum(1 for r in rows if r[5] == 'won')
    lost = sum(1 for r in rows if r[5] == 'lost')
    pending = sum(1 for r in rows if r[5] == 'pending')

    print(f"\n{'='*60}")
    print(f"  模拟投注历史统计")
    print(f"{'='*60}")
    print(f"  总注数: {len(rows)} (中奖{won} | 未中{lost} | 待结算{pending})")
    print(f"  总投入: {total_stake}元")
    print(f"  总回收: {total_payout:.2f}元")
    print(f"  总盈亏: {total_profit:+.2f}元")
    print(f"  ROI: {total_profit/total_stake*100:+.1f}%" if total_stake > 0 else "  ROI: N/A")
    print(f"{'='*60}")


def run_simulation(date_arg=None, weekday=None):
    """执行模拟投注流程"""
    init_sim_db()

    # 1. 查找预测文件
    files = find_pred_files(date_arg, weekday)
    if not files:
        print("  ⚠️ 未找到预测文件")
        return

    pred_file = str(files[0])
    print(f"  📂 加载预测: {os.path.basename(pred_file)}")

    # 2. 加载预测, 提取候选场次
    candidates, pred_meta = load_predictions(pred_file)
    print(f"  📊 可投注场次: {len(candidates)}场")

    if not candidates:
        print("  ⚠️ 无符合条件的场次 (需 ★★+)")
        return

    for c in candidates:
        print(f"    {c['key']} {c['home']} vs {c['away']} → {c['option']}@{c['odds']} {c['conf']}")

    # 3. 构建串关
    parlay = select_and_build_parlay(candidates)
    if not parlay:
        return

    print(f"\n  🎯 串关决策: {parlay['bet_type']} (最高{parlay['legs']}场)")

    # 4. 保存
    bet_id = save_bet(parlay, pred_file, pred_meta)
    print(f"  💾 已保存: {bet_id}")

    # 5. 打印投注卡
    print_bet_card(bet_id, parlay, pred_file)


if __name__ == '__main__':
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if '周' in arg:
            run_simulation(weekday=arg)
        elif '月' in arg or '-' in arg:
            run_simulation(date_arg=arg)
        else:
            run_simulation(weekday=arg)
    else:
        run_simulation()
