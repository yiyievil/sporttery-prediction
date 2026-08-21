# -*- coding: utf-8 -*-
"""
train_upgrades_from_historical.py — 用完整历史库 historical_odds.db 训练十项升级参数

背景:
  model_upgrades.train_all() 期望 predictions/sporttery_history.db 的 matches 表
  (close_h/close_d/close_a/winFlag/matchDate/home/away/score/odds_fetched)。
  但本地真正的完整历史库是 predictions/historical_odds.db (11 张表, 4484 场,
  含体彩 HAD/HHAD 赔率 + 比分 + 赛果 + xG + ELO 等), 结构与 matches 不同。

本脚本做两件事:
  1) ETL: 把 historical_odds.db.historical_matches 规范化映射/物化为
     predictions/sporttery_history.db 的 matches 表 (含 result 记法归一:
     H/W→H, D→D, A/L→A, 空result按比分推断)。
  2) 调用 model_upgrades.train_all() 重训数据驱动参数 (odds_calibrator + dc_model),
     输出到 predictions/model_upgrades_params.json。

用法: python scripts/train_upgrades_from_historical.py
幂等可重跑; 旧 params 会先备份为 model_upgrades_params.json.bak。
"""
import json
import os
import sys
import shutil
import sqlite3
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)                      # 仓库根目录 (model_upgrades.py 所在)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

HIST_DB = os.path.join(_ROOT, 'predictions', 'historical_odds.db')
HISTORY_DB = os.path.join(_ROOT, 'predictions', 'sporttery_history.db')
PARAMS_PATH = os.path.join(_ROOT, 'predictions', 'model_upgrades_params.json')


def _norm_winflag(result, hs, as_):
    """result 记法归一 → 'H'/'D'/'A'。空值按比分推断; 无法判断返回 None。"""
    r = (result or '').strip().upper()
    if r in ('H', 'W'):
        return 'H'
    if r == 'D':
        return 'D'
    if r in ('A', 'L'):
        return 'A'
    # 空 / 未知: 用比分推断
    if hs is not None and as_ is not None:
        if hs > as_:
            return 'H'
        if hs == as_:
            return 'D'
        return 'A'
    return None


def build_matches():
    """把 historical_odds.db.historical_matches 物化为 sporttery_history.db.matches。"""
    if not os.path.exists(HIST_DB):
        raise FileNotFoundError(f"历史库不存在: {HIST_DB}")
    src = sqlite3.connect(HIST_DB)
    rows = src.execute("""
        SELECT id, match_date, league, home_team, away_team,
               home_score, away_score, half_home_score, half_away_score,
               result, sp_had_h, sp_had_d, sp_had_a, sp_goal_line, sp_match_num,
               sporttery_match_id
        FROM historical_matches
        WHERE home_score IS NOT NULL AND away_score IS NOT NULL
    """).fetchall()
    src.close()

    os.makedirs(os.path.dirname(HISTORY_DB), exist_ok=True)
    dst = sqlite3.connect(HISTORY_DB)
    dst.execute("""CREATE TABLE IF NOT EXISTS matches(
        matchId INTEGER PRIMARY KEY, matchDate TEXT, matchNumStr TEXT,
        league TEXT, home TEXT, away TEXT, homeId INTEGER, awayId INTEGER,
        score TEXT, halfScore TEXT, winFlag TEXT, goalLine TEXT,
        close_h REAL, close_d REAL, close_a REAL, poolStatus TEXT,
        odds_fetched INTEGER DEFAULT 0)""")
    dst.execute("DELETE FROM matches")

    n_odds = 0
    for (mid, mdate, league, home, away, hs, as_, hhs, ahs,
         result, sph, spd, spa, goal_line, mnum, sp_mid) in rows:
        winflag = _norm_winflag(result, hs, as_)
        score = f"{hs}:{as_}"
        half = f"{hhs}:{ahs}" if (hhs is not None and ahs is not None) else None
        has_odds = 1 if (sph and spd and spa and sph > 1 and spd > 1 and spa > 1) else 0
        if has_odds and winflag in ('H', 'D', 'A'):
            n_odds += 1
        dst.execute("""INSERT OR REPLACE INTO matches
            (matchId,matchDate,matchNumStr,league,home,away,homeId,awayId,
             score,halfScore,winFlag,goalLine,close_h,close_d,close_a,poolStatus,odds_fetched)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, mdate, mnum, league, home, away, None, None,
             score, half, winflag, goal_line, sph, spd, spa, 'Payout', has_odds))
    dst.commit()
    total = dst.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    valid_odds = dst.execute(
        "SELECT COUNT(*) FROM matches WHERE odds_fetched=1 AND winFlag IN ('H','D','A') "
        "AND close_h>1 AND close_d>1 AND close_a>1").fetchone()[0]
    dst.close()
    print(f"[ETL] matches 表已构建: 总{total}场, 校准器有效样本{valid_odds}场")
    return total, valid_odds


def expand_dc_team_aliases(params, mu):
    """把 dc_model.teams 的每个队名按 team_alias.json 双向组扩展出所有变体键
    (指向同一攻防强度)。

    动机: 预测引擎 v215_e2e.py 的 dc_lambda 用 home_name/away_name (体彩全名,
    如 'AIK索尔纳') 直接 dc['teams'].get(...) 查询, 不做变体展开; 而本历史库
    dc_model 用短名 (如 '索尔纳')。扩展别名后, 无论引擎传全名还是短名都能命中,
    避免升级5因命名不一致而回退纯市场λ (相对旧参数的退化)。
    返回新增变体键数量。
    """
    dc = params.get('dc_model')
    if not dc or 'teams' not in dc:
        return 0
    alias_path = os.path.join(_ROOT, 'predictions', 'team_alias.json')
    if not os.path.exists(alias_path):
        return 0
    with open(alias_path, encoding='utf-8') as f:
        raw = json.load(f)
    member2group = {}
    for std, variants in raw.items():
        group = {std} | set(variants)
        for n in group:
            member2group[n] = group
    teams = dc['teams']
    added = 0
    for name in list(teams.keys()):
        group = member2group.get(name)
        if not group:
            continue
        for variant in group:
            if variant not in teams:
                teams[variant] = dict(teams[name])
                added += 1
    if added:
        mu._save_params(params)
    return added


def main():
    print(f"[ETL] 源库: {HIST_DB} ({os.path.getsize(HIST_DB)//1024//1024}MB)")
    build_matches()

    # 备份旧参数 (存在则备份, 便于回滚)
    if os.path.exists(PARAMS_PATH):
        bak = PARAMS_PATH + '.bak'
        shutil.copyfile(PARAMS_PATH, bak)
        print(f"[备份] 旧参数 -> {os.path.basename(bak)}")

    import model_upgrades as mu
    print("[训练] 调用 model_upgrades.train_all() ...")
    params = mu.train_all(verbose=True)

    # 队名别名扩展: 让 dc_lambda 的直接 .get() 对全名/短名都能命中
    added = expand_dc_team_aliases(params, mu)
    if added:
        print(f"[别名扩展] dc_model.teams 新增 {added} 个变体键 (体彩全名↔历史短名)")

    print(f"[完成] 参数已保存: {PARAMS_PATH}")
    print(f"       keys = {list(params.keys())}")
    if 'odds_calibrator' in params:
        print(f"       odds_calibrator n = {params['odds_calibrator'].get('n')}")
    if 'dc_model' in params:
        dc = params['dc_model']
        print(f"       dc_model n = {dc.get('n')}, 球队数 = {len(dc.get('teams', {}))}, "
              f"主场系数 = {dc.get('home_adv_ratio')}, avg_h = {dc.get('avg_h')}, avg_a = {dc.get('avg_a')}")


if __name__ == '__main__':
    main()
