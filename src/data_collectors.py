"""football-prediction-pipeline · 数据采集层

FBrefCollector  — FBref 比赛数据 (cloudscraper 直连 + proxy 降级)
UnderstatCollector — Understat xG/xGA/PPDA 数据 (HTTP直连, 无需浏览器)
EloBuilder       — 从比赛历史构建 Elo 评级时间序列
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time

import pandas as pd

from .config import (
    config, DB_PATH, LEAGUE_MAP, LEAGUE_MAP_REVERSE,
    TEAM_NAME_MAP_REVERSE, FBREF_LEAGUE_MAP, FBREF_LEAGUE_MAP_REVERSE,
    FBREF_CALENDAR_YEAR_LEAGUES,
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
        try:
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
            skipped = 0
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
                except Exception as e:
                    # 修复: 原 pass 静默丢行, 改为计数 + 首例日志, 让数据缺失可见
                    skipped += 1
                    if skipped == 1:
                        logger.warning("Understat: 有行插入失败(示例 game_id=%r): %s",
                                       row.get("game_id"), e)

            conn.commit()
        finally:
            conn.close()
        if skipped:
            logger.warning("Understat: %d 行插入失败被跳过", skipped)
        logger.info(f"  Understat: 存储了 {inserted} 条比赛记录")
        return inserted


# ============================================================
# FBref 采集器 (cloudscraper 直连 + curl_cffi + proxy 三层回退)
# ============================================================
class FBrefCollector:
    """从 FBref 抓取比赛数据和 xG 统计 (StatsBomb 模型)

    FBref 被 Cloudflare Turnstile 保护, 本采集器实现了三层回退策略:

    1. cloudscraper 模式: 使用 cloudscraper 绕过 Cloudflare JS 挑战,
       用 BeautifulSoup 解析 HTML 表格提取 xG 数据。
       适用于有住宅 IP 或 cloudscraper 可用的环境。
       数据标记 source_type='fbref_direct'。

    2. curl_cffi 模式: 使用 curl_cffi 的 TLS 指纹伪装 (Chrome/Safari),
       尝试绕过 Cloudflare。比 cloudscraper 更轻量。
       数据标记 source_type='fbref_direct'。

    3. proxy 模式 (兜底): 从 historical_matches 表读取实际进球,
       按联赛计算均值作为 xG 代理。大样本下实际进球 ≈ xG (相关性 ~0.9),
       代理模式在不牺牲方向正确性的前提下提供合理的 xG 估计。
       数据标记 source_type='fbref_proxy'。

    数据字段:
        home_xg, away_xg          — 预期进球
        home_goals, away_goals    — 实际进球
        league_cn, season, match_date, home_team, away_team
    """

    # FBref 联赛名 → URL slug 映射 (用于构建 URL)
    _FBREF_SLUG_MAP = {
        "J1 League": "J1-League",
        "J2 League": "J2-League",
        "K League 1": "K-League-1",
        "Major League Soccer": "Major-League-Soccer",
        "Serie A": "Serie-A",           # 巴甲
        "Primeira Liga": "Primeira-Liga",
        "Eredivisie": "Eredivisie",
        "Championship": "Championship",
        "Saudi Pro League": "Saudi-Pro-League",
        "A-League Men": "A-League-Men",
        "Allsvenskan": "Allsvenskan",
        "Eliteserien": "Eliteserien",
        "Veikkausliiga": "Veikkausliiga",
        "Chinese Super League": "Chinese-Super-League",
        "Liga MX": "Liga-MX",
        "Champions League": "Champions-League",
        "Europa League": "Europa-League",
        "Conference League": "Conference-League",
        "League One": "League-One",
        "2. Bundesliga": "2-Bundesliga",
        "Ligue 2": "Ligue-2",
        "Eerste Divisie": "Eerste-Divisie",
        "Copa Libertadores": "Copa-Libertadores",
    }

    def __init__(self, seasons: list[str] | None = None,
                 leagues: dict | None = None,
                 mode: str = "auto"):
        """初始化 FBref 采集器

        Args:
            seasons: 赛季列表, 默认 config.seasons
            leagues: 联赛映射 {中文名: (sd_id, comp_id, fbref_name)},
                     默认 FBREF_LEAGUE_MAP
            mode: 采集模式
                  "auto"  — 自动回退 (cloudscraper → curl_cffi → proxy)
                  "cloudscraper" — 仅 cloudscraper
                  "curl_cffi"    — 仅 curl_cffi
                  "proxy"        — 仅 proxy
        """
        self.seasons = seasons or config.seasons
        self.leagues = leagues if leagues else FBREF_LEAGUE_MAP
        self.mode = mode
        self._data: pd.DataFrame | None = None
        self._available = False
        self._source_type = "fbref_proxy"  # 默认, 直连成功时更新

    # ── 公共接口 ──────────────────────────────────────────
    def collect(self) -> pd.DataFrame:
        """采集 FBref 数据, 自动选择最佳模式"""
        if self.mode == "proxy":
            return self._collect_proxy()
        elif self.mode == "cloudscraper":
            return self._collect_cloudscraper()
        elif self.mode == "curl_cffi":
            return self._collect_curl_cffi()
        else:  # auto
            return self._collect_auto()

    def _collect_auto(self) -> pd.DataFrame:
        """自动模式: cloudscraper → curl_cffi → proxy"""
        # 第1层: cloudscraper
        logger.info("FBref: 尝试 cloudscraper 直连...")
        try:
            df = self._collect_cloudscraper()
            if not df.empty:
                return df
        except Exception as e:
            logger.info(f"  cloudscraper 不可用: {e}")

        # 第2层: curl_cffi
        logger.info("FBref: 尝试 curl_cffi 直连...")
        try:
            df = self._collect_curl_cffi()
            if not df.empty:
                return df
        except Exception as e:
            logger.info(f"  curl_cffi 不可用: {e}")

        # 第3层: proxy
        logger.info("FBref: 降级到 proxy 模式")
        return self._collect_proxy()

    # ── cloudscraper 直连模式 ──────────────────────────────
    def _collect_cloudscraper(self) -> pd.DataFrame:
        """cloudscraper 直连: 绕过 Cloudflare JS 挑战, 解析 HTML 表格"""
        import cloudscraper
        from bs4 import BeautifulSoup

        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'linux', 'mobile': False},
            delay=10,
        )

        all_rows = []
        success_count = 0

        for cn_league, (sd_id, comp_id, fbref_name) in self.leagues.items():
            for season in self.seasons:
                url = self._build_fbref_url(comp_id, fbref_name, cn_league, season)
                if not url:
                    continue

                try:
                    resp = scraper.get(url, timeout=30)
                    if resp.status_code != 200:
                        logger.debug(f"  cloudscraper {cn_league} {season}: HTTP {resp.status_code}")
                        continue
                    if "Just a moment" in resp.text[:200]:
                        logger.debug(f"  cloudscraper {cn_league} {season}: Turnstile 拦截")
                        continue

                    rows = self._parse_fbref_schedule(resp.text, cn_league, fbref_name, season)
                    if rows:
                        all_rows.extend(rows)
                        success_count += 1
                        logger.info(f"  cloudscraper ✓ {cn_league} {season}: {len(rows)} 场")
                    time.sleep(2)

                except Exception as e:
                    logger.debug(f"  cloudscraper {cn_league} {season}: {e}")
                    continue

        if not all_rows:
            raise Exception("cloudscraper: 未获取到任何数据 (所有联赛均被 Cloudflare 拦截)")

        df = pd.DataFrame(all_rows)
        self._data = df
        self._available = True
        self._source_type = "fbref_direct"
        logger.info(f"  FBref cloudscraper 总计: {len(df)} 场比赛, {success_count} 个联赛-赛季")
        return df

    # ── curl_cffi 直连模式 ──────────────────────────────────
    def _collect_curl_cffi(self) -> pd.DataFrame:
        """curl_cffi 直连: TLS 指纹伪装 (Chrome 120/124)"""
        from curl_cffi import requests as curl_requests
        from bs4 import BeautifulSoup

        all_rows = []
        success_count = 0

        for cn_league, (sd_id, comp_id, fbref_name) in self.leagues.items():
            for season in self.seasons:
                url = self._build_fbref_url(comp_id, fbref_name, cn_league, season)
                if not url:
                    continue

                for impersonate in ['chrome124', 'chrome120', 'safari17_0']:
                    try:
                        resp = curl_requests.get(url, impersonate=impersonate, timeout=30)
                        if resp.status_code != 200:
                            continue
                        if "Just a moment" in resp.text[:200]:
                            continue

                        rows = self._parse_fbref_schedule(resp.text, cn_league, fbref_name, season)
                        if rows:
                            all_rows.extend(rows)
                            success_count += 1
                            logger.info(f"  curl_cffi ✓ {cn_league} {season} [{impersonate}]: {len(rows)} 场")
                        break  # 成功则跳出 impersonate 循环
                    except Exception:
                        continue
                time.sleep(1)

        if not all_rows:
            raise Exception("curl_cffi: 未获取到任何数据 (所有联赛均被 Cloudflare 拦截)")

        df = pd.DataFrame(all_rows)
        self._data = df
        self._available = True
        self._source_type = "fbref_direct"
        logger.info(f"  FBref curl_cffi 总计: {len(df)} 场比赛, {success_count} 个联赛-赛季")
        return df

    # ── proxy 兜底模式 ─────────────────────────────────────
    def _collect_proxy(self) -> pd.DataFrame:
        """代理模式: 从 historical_matches 提取实际进球, 按联赛计算 xG 代理值

        原理:
        - 大样本下, 实际进球均值 ≈ xG 均值 (相关性 ~0.9)
        - 使用联赛级均值作为 xG 代理, 比全局均值更精确
        - 标记 source_type='fbref_proxy' 以便预测引擎识别并适当降权
        """
        target_cn_leagues = list(self.leagues.keys())

        conn = sqlite3.connect(str(DB_PATH))
        all_rows = []
        league_stats = {}

        for cn_league in target_cn_leagues:
            league_names = [cn_league]
            aliases = {
                "美职联": ["美职联", "美职"],
                "韩职":   ["韩职", "韩K"],
                "沙职":   ["沙职", "沙超"],
            }
            if cn_league in aliases:
                league_names.extend(aliases[cn_league])

            placeholders = ",".join(["?"] * len(league_names))
            query = f"""
                SELECT match_date, league, home_team, away_team,
                       home_score, away_score, season
                FROM historical_matches
                WHERE league IN ({placeholders})
                  AND home_score IS NOT NULL AND away_score IS NOT NULL
                  AND result IS NOT NULL
                ORDER BY match_date DESC
            """
            try:
                c = conn.cursor()
                c.execute(query, league_names)
                rows = c.fetchall()
            except Exception:
                continue

            if not rows:
                continue

            home_goals = [int(r[4]) for r in rows if r[4] is not None]
            away_goals = [int(r[5]) for r in rows if r[5] is not None]
            if home_goals:
                league_stats[cn_league] = {
                    "home_avg":  round(sum(home_goals) / len(home_goals), 2),
                    "away_avg":  round(sum(away_goals) / len(away_goals), 2),
                    "total_avg": round((sum(home_goals) + sum(away_goals)) / len(home_goals), 2),
                    "n_matches": len(home_goals),
                }

            for row in rows:
                match_date, league, home_team, away_team, home_score, away_score, season = row
                all_rows.append({
                    "league_cn":   cn_league,
                    "league_en":   self.leagues.get(cn_league, ("", 0, ""))[2],
                    "season":      season or "",
                    "match_date":  match_date,
                    "home_team":   home_team,
                    "away_team":   away_team,
                    "home_goals":  home_score,
                    "away_goals":  away_score,
                    "home_xg":     league_stats[cn_league]["home_avg"],
                    "away_xg":     league_stats[cn_league]["away_avg"],
                    "home_ppda":   None,
                    "away_ppda":   None,
                    "is_result":   1,
                })

        conn.close()

        if not all_rows:
            logger.warning("FBref proxy: 无数据")
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)
        self._data = df
        self._available = True
        self._source_type = "fbref_proxy"

        logger.info(f"  FBref proxy: {len(df)} 场比赛, {len(league_stats)} 个联赛")
        for lg, stats in sorted(league_stats.items()):
            logger.info(f"    {lg}: 主场均值={stats['home_avg']:.2f}, "
                         f"客场均值={stats['away_avg']:.2f}, "
                         f"n={stats['n_matches']}")
        return df

    # ── HTML 解析 ──────────────────────────────────────────
    @staticmethod
    def _parse_fbref_schedule(html: str, cn_league: str,
                               fbref_name: str, season: str) -> list[dict]:
        """解析 FBref schedule 页面 HTML, 提取比赛数据

        FBref schedule 表格列:
          Wk, Day, Date, Time, Home, xG, Score, xG.1, Away,
          Attendance, Venue, Referee, Match Report, Notes

        xG 列可能因 JavaScript 动态加载而不可见,
        但 HTML 源码中通常包含完整数据。
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, 'html.parser')
        table = soup.find('table', id=lambda x: x and 'sched' in str(x))
        if not table:
            return []

        # 解析表头, 建立列索引映射
        thead = table.find('thead')
        header_cells = thead.find_all('th') if thead else []
        col_map = {}
        for i, th in enumerate(header_cells):
            text = th.get_text(strip=True)
            col_map[text] = i
            # data-stat 属性更可靠
            ds = th.get('data-stat', '')
            if ds:
                col_map[ds] = i

        # 查找列索引 (优先 data-stat, 其次列名)
        def _col_idx(*keys):
            for k in keys:
                if k in col_map:
                    return col_map[k]
            return None

        idx_home  = _col_idx('home_team', 'Home')
        idx_away  = _col_idx('away_team', 'Away')
        idx_date  = _col_idx('date', 'Date')
        idx_score = _col_idx('score', 'Score')
        idx_home_xg = _col_idx('home_xg', 'xG')
        idx_away_xg = _col_idx('away_xg', 'xG.1')
        idx_notes = _col_idx('notes', 'Notes')

        if idx_home is None or idx_away is None or idx_date is None:
            return []

        rows = []
        tbody = table.find('tbody')
        if not tbody:
            return rows

        for tr in tbody.find_all('tr'):
            # 跳过分隔行 (class="spacer" 等)
            if tr.get('class') and 'spacer' in tr.get('class', []):
                continue
            cells = tr.find_all(['td', 'th'])
            if len(cells) < max(idx_home, idx_away, idx_date) + 1:
                continue

            # 提取日期
            date_cell = cells[idx_date]
            date_text = date_cell.get_text(strip=True)
            if not date_text or not re.match(r'\d{4}-\d{2}-\d{2}', date_text):
                continue

            # 提取队名 (去除链接标签, 保留纯文本)
            home_cell = cells[idx_home]
            home_team = home_cell.get_text(strip=True) if home_cell else ""
            away_cell = cells[idx_away]
            away_team = away_cell.get_text(strip=True) if away_cell else ""

            # 提取比分
            score_text = ""
            home_goals = away_goals = None
            if idx_score is not None:
                score_cell = cells[idx_score]
                score_link = score_cell.find('a')
                score_text = score_link.get_text(strip=True) if score_link else score_cell.get_text(strip=True)
                # 解析 "2–5" 格式
                score_match = re.match(r'(\d+)\s*[–\-]\s*(\d+)', score_text)
                if score_match:
                    home_goals = int(score_match.group(1))
                    away_goals = int(score_match.group(2))
                else:
                    continue  # 未来比赛, 跳过

            # 提取 xG (如果存在)
            home_xg = away_xg = None
            if idx_home_xg is not None and idx_home_xg < len(cells):
                try:
                    home_xg = float(cells[idx_home_xg].get_text(strip=True))
                except (ValueError, TypeError):
                    pass
            if idx_away_xg is not None and idx_away_xg < len(cells):
                try:
                    away_xg = float(cells[idx_away_xg].get_text(strip=True))
                except (ValueError, TypeError):
                    pass

            # 跳过未来比赛 (notes 列通常有 "Match postponed" 等)
            if idx_notes is not None and idx_notes < len(cells):
                notes = cells[idx_notes].get_text(strip=True)
                if notes:
                    continue

            rows.append({
                "league_cn":  cn_league,
                "league_en":  fbref_name,
                "season":     season,
                "match_date": date_text,
                "home_team":  home_team,
                "away_team":  away_team,
                "home_goals": home_goals,
                "away_goals": away_goals,
                "home_xg":    home_xg,
                "away_xg":    away_xg,
                "home_ppda":  None,
                "away_ppda":  None,
                "is_result":  1,
            })

        return rows

    # ── URL 构建 ───────────────────────────────────────────
    @classmethod
    def _build_fbref_url(cls, comp_id: int, fbref_name: str,
                          cn_league: str, season: str) -> str | None:
        """构建 FBref schedule 页面 URL

        Args:
            comp_id: FBref 联赛 ID (如 25 是 J1 League)
            fbref_name: FBref 联赛名 (如 "J1 League")
            cn_league: 中文联赛名
            season: 赛季字符串 (如 "2024-2025" 或 "2025-2026")

        Returns:
            URL 字符串, 或 None (无法构建时)
        """
        # 确定赛季在 URL 中的格式
        if cn_league in FBREF_CALENDAR_YEAR_LEAGUES:
            # 日职/韩职/美职联等: 取赛季的第二年
            # "2024-2025" → "2025", "2023-2024" → "2024"
            parts = season.split('-')
            url_season = parts[-1] if len(parts) >= 2 else parts[0]
        else:
            # 跨年联赛: "2024-2025"
            url_season = season

        # 联赛名 slug
        slug = cls._FBREF_SLUG_MAP.get(fbref_name)
        if not slug:
            slug = fbref_name.replace(' ', '-')

        return f"https://fbref.com/en/comps/{comp_id}/{url_season}/schedule/{url_season}-{slug}-Scores-and-Fixtures"

    # ── 存储 ────────────────────────────────────────────────
    def store_to_db(self, df: pd.DataFrame | None = None) -> int:
        """存储到 team_xg 表

        source_type:
          - 'fbref_direct' (cloudscraper/curl_cffi 直连成功)
          - 'fbref_proxy'  (proxy 兜底)
        """
        df = df if df is not None else self._data
        if df is None or df.empty:
            logger.warning("FBref: 无数据可存储")
            return 0

        source_type = self._source_type
        conn = sqlite3.connect(str(DB_PATH))

        inserted = 0
        for _, row in df.iterrows():
            try:
                conn.execute('''INSERT OR REPLACE INTO team_xg
                    (source_type, original_id, league_en, league_cn, season,
                     match_date, home_team, away_team,
                     home_goals, away_goals, home_xg, away_xg,
                     home_ppda, away_ppda, is_result)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (source_type, None,
                     row.get("league_en", ""), row.get("league_cn", ""),
                     row.get("season", ""), row.get("match_date", ""),
                     row.get("home_team", ""), row.get("away_team", ""),
                     row.get("home_goals"), row.get("away_goals"),
                     row.get("home_xg"), row.get("away_xg"),
                     row.get("home_ppda"), row.get("away_ppda"),
                     row.get("is_result", 1)))
                inserted += 1
            except Exception:
                pass

        conn.commit()
        conn.close()
        logger.info(f"  FBref: 存储了 {inserted} 条记录到 team_xg (source_type={source_type})")
        return inserted

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def source_type(self) -> str:
        return self._source_type


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
            # NaN 比分视为无效, 跳过 (避免 NaN 被当作 0 分"负"更新 Elo)
            if pd.isna(hg) or pd.isna(ag):
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
