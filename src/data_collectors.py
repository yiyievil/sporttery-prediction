"""football-prediction-pipeline · 数据采集层

FBrefCollector  — FBref 比赛数据 (需Chrome, 环境不支持时自动降级)
UnderstatCollector — Understat xG/xGA/PPDA 数据 (HTTP直连, 无需浏览器)
EloBuilder       — 从比赛历史构建 Elo 评级时间序列
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import numpy as np

from .config import (
    config, DB_PATH, LEAGUE_MAP, LEAGUE_MAP_REVERSE,
    TEAM_NAME_MAP, TEAM_NAME_MAP_REVERSE,
    cn_to_en_team, en_to_cn_team, cn_to_en_league,
)

logger = logging.getLogger("collectors")

# ============================================================
# Understat 采集器
# ============================================================
class UnderstatCollector:
    """从 Understat 抓取 xG/xGA/PPDA 等高级指标

    数据字段 (per match):
        home_xg, away_xg          — 预期进球
        home_np_xg, away_np_xg    — 非点球预期进球
        home_ppda, away_ppda      — 每次防守动作允许的传球数 (越低越激进)
        home_deep_completions     — 深入传球次数
        home_expected_points      — 预期积分
    """

    def __init__(self, seasons: list[str] | None = None):
        self.seasons = seasons or config.seasons
        self._data: pd.DataFrame | None = None

    def collect(self) -> pd.DataFrame:
        """采集所有五大联赛所有赛季的比赛级 xG 数据"""
        import soccerdata as sd

        all_frames = []
        for cn_league, en_league in LEAGUE_MAP.items():
            for season in self.seasons:
                try:
                    logger.info(f"  Understat: {en_league} {season}")
                    us = sd.Understat(en_league, season)
                    # 赛程 (含 xG)
                    sched = us.read_schedule()
                    # 球队比赛统计 (含 PPDA, deep_completions 等)
                    try:
                        team_stats = us.read_team_match_stats()
                    except Exception:
                        team_stats = None

                    # 合并赛程和详细统计
                    df = sched.copy()
                    if team_stats is not None:
                        # 合并详细统计字段
                        extra_cols = [c for c in team_stats.columns
                                      if c not in df.columns]
                        if extra_cols:
                            merge_keys = ["game_id"]
                            df = df.merge(
                                team_stats[merge_keys + extra_cols],
                                on="game_id", how="left", suffixes=("", "_ts")
                            )

                    df["league_cn"] = cn_league
                    df["league_en"] = en_league
                    df["season"] = season
                    all_frames.append(df)
                    time.sleep(0.5)
                except Exception as e:
                    logger.warning(f"  Understat {en_league} {season} 失败: {e}")

        if not all_frames:
            logger.error("Understat: 未获取到任何数据")
            return pd.DataFrame()

        df = pd.concat(all_frames, ignore_index=False)
        # 添加中文队名
        df["home_team_cn"] = df["home_team"].map(TEAM_NAME_MAP_REVERSE)
        df["away_team_cn"] = df["away_team"].map(TEAM_NAME_MAP_REVERSE)
        # 标准化日期
        df["date"] = pd.to_datetime(df["date"])
        df["match_date"] = df["date"].dt.strftime("%Y-%m-%d")

        self._data = df
        logger.info(f"  Understat 总计: {len(df)} 场比赛, "
                     f"{df['home_team'].nunique()} 支球队")
        return df

    def store_to_db(self, df: pd.DataFrame | None = None) -> int:
        """存储到历史数据库 understat_matches 表"""
        df = df if df is not None else self._data
        if df is None or df.empty:
            logger.warning("Understat: 无数据可存储")
            return 0

        conn = sqlite3.connect(str(DB_PATH))
        c = conn.cursor()

        # 创建表
        c.execute('''
            CREATE TABLE IF NOT EXISTS understat_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER UNIQUE,
                league_en TEXT, league_cn TEXT, season TEXT,
                match_date TEXT,
                home_team TEXT, away_team TEXT,
                home_team_cn TEXT, away_team_cn TEXT,
                home_goals INTEGER, away_goals INTEGER,
                home_xg REAL, away_xg REAL,
                home_np_xg REAL, away_np_xg REAL,
                home_np_xg_diff REAL, away_np_xg_diff REAL,
                home_ppda REAL, away_ppda REAL,
                home_deep_completions REAL, away_deep_completions REAL,
                home_expected_points REAL, away_expected_points REAL,
                is_result INTEGER,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')

        # 插入数据
        inserted = 0
        for _, row in df.iterrows():
            try:
                c.execute('''INSERT OR REPLACE INTO understat_matches
                    (game_id, league_en, league_cn, season, match_date,
                     home_team, away_team, home_team_cn, away_team_cn,
                     home_goals, away_goals, home_xg, away_xg,
                     home_np_xg, away_np_xg, home_np_xg_diff, away_np_xg_diff,
                     home_ppda, away_ppda, home_deep_completions, away_deep_completions,
                     home_expected_points, away_expected_points, is_result)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (int(row.get("game_id", 0)),
                     row.get("league_en", ""), row.get("league_cn", ""),
                     row.get("season", ""), row.get("match_date", ""),
                     row.get("home_team", ""), row.get("away_team", ""),
                     row.get("home_team_cn", ""), row.get("away_team_cn", ""),
                     row.get("home_goals"), row.get("away_goals"),
                     row.get("home_xg"), row.get("away_xg"),
                     row.get("home_np_xg"), row.get("away_np_xg"),
                     # M11: 建表列名为 home_np_xg_diff/away_np_xg_diff, 取值列名需一致
                     row.get("home_np_xg_diff"), row.get("away_np_xg_diff"),
                     row.get("home_ppda"), row.get("away_ppda"),
                     row.get("home_deep_completions"), row.get("away_deep_completions"),
                     row.get("home_expected_points"), row.get("away_expected_points"),
                     int(row.get("is_result", False))))
                inserted += 1
            except Exception:
                pass

        conn.commit()
        conn.close()
        logger.info(f"  Understat: 存储了 {inserted} 条比赛记录")
        return inserted


# ============================================================
# FBref 采集器 (需Chrome浏览器, 不支持时降级)
# ============================================================
class FBrefCollector:
    """从 FBref 抓取比赛数据和 xG 统计

    FBref 被 Cloudflare 保护, 需要 Chrome 浏览器。
    环境不支持时自动降级为示例数据模式。
    """

    def __init__(self, seasons: list[str] | None = None):
        self.seasons = seasons or config.seasons
        self._data: pd.DataFrame | None = None
        self._available = False

    def collect(self) -> pd.DataFrame:
        """采集 FBref 赛程和球队统计"""
        try:
            import soccerdata as sd
            fbref = sd.FBref(leagues=list(LEAGUE_MAP.values()),
                             seasons=self.seasons)
            schedule = fbref.read_schedule()
            # 尝试获取 shooting 统计 (含 xG)
            try:
                shooting = fbref.read_team_season_stats(stat_type="shooting")
            except Exception:
                shooting = None

            df = schedule.copy()
            df["league_cn"] = df["league"].map(LEAGUE_MAP_REVERSE)
            self._data = df
            self._available = True
            logger.info(f"  FBref: {len(df)} 场比赛")
            return df
        except Exception as e:
            logger.warning(f"  FBref 不可用 (需Chrome): {e}")
            logger.info("  FBref 降级: 仅使用 Understat 数据")
            self._available = False
            return pd.DataFrame()

    @property
    def is_available(self) -> bool:
        return self._available


# ============================================================
# Elo 评级构建器
# ============================================================
class EloBuilder:
    """从比赛历史构建 Elo 评级时间序列

    使用标准 Elo 公式:
      R_new = R_old + K × (actual - expected)
    K 因子: 联赛 20, 杯赛 30
    主场优势: +65 Elo 点
    """

    K_LEAGUE = 20
    K_CUP = 30
    HFA = 65  # Home Field Advantage
    INIT_RATING = 1500

    CUP_LEAGUES = {
        "欧冠", "欧冠杯", "冠军联赛", "欧洲冠军联赛", "欧冠资格赛", "欧冠附",
        "欧罗巴", "欧联", "欧联杯", "欧洲联赛", "欧罗巴联赛",
        "欧协联", "欧协联杯", "欧洲协会联赛",
        "亚冠", "亚冠杯", "亚足联冠军联赛",
        "解放者杯", "南美解放者杯",
    }

    def __init__(self):
        self.ratings: dict[str, float] = {}

    def build(self, matches: pd.DataFrame) -> pd.DataFrame:
        """构建 Elo 评级历史

        Args:
            matches: 比赛数据, 需含 home_team, away_team, home_goals, away_goals, date, league
        Returns:
            DataFrame: 每场比赛的赛前 Elo 评级
        """
        records = []
        # 按日期排序
        matches = matches.sort_values("date").reset_index(drop=True)

        for _, m in matches.iterrows():
            home = m.get("home_team", "")
            away = m.get("away_team", "")
            hg = m.get("home_goals")
            ag = m.get("away_goals")
            league = m.get("league", "") or m.get("league_cn", "")

            if hg is None or ag is None or not home or not away:
                continue

            # 赛前评级
            r_home = self.ratings.get(home, self.INIT_RATING)
            r_away = self.ratings.get(away, self.INIT_RATING)

            # 预期胜率
            diff = r_home - r_away + self.HFA
            e_home = 1.0 / (1.0 + 10 ** (-diff / 400.0))

            # 实际结果 (1=胜, 0.5=平, 0=负)
            if hg > ag:
                actual = 1.0
            elif hg == ag:
                actual = 0.5
            else:
                actual = 0.0

            # K 因子
            k = self.K_CUP if league in self.CUP_LEAGUES else self.K_LEAGUE

            # 更新评级
            delta = k * (actual - e_home)
            self.ratings[home] = r_home + delta
            self.ratings[away] = r_away - delta

            records.append({
                "date": m.get("date"),
                "home_team": home,
                "away_team": away,
                "league": league,
                "elo_home_pre": round(r_home, 1),
                "elo_away_pre": round(r_away, 1),
                "elo_home_post": round(self.ratings[home], 1),
                "elo_away_post": round(self.ratings[away], 1),
                "elo_diff": round(r_home - r_away, 1),
            })

        return pd.DataFrame(records)
