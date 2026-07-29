"""football-prediction-pipeline · 全局配置

联赛映射、球队名称映射、路径配置、数据源开关。
"""
from __future__ import annotations
from pathlib import Path
import os

# ============================================================
# 路径
# ============================================================
BASE_DIR = Path(os.environ.get("SPORTTERY_WORKSPACE", Path(__file__).resolve().parent.parent))
OUTPUT_DIR = BASE_DIR / "predictions"
DATA_DIR = BASE_DIR / "predictions"
DB_PATH = DATA_DIR / "historical_odds.db"

# ============================================================
# 联赛映射: 中文 → soccerdata 代码
# ============================================================
LEAGUE_MAP = {
    "英超": "ENG-Premier League",
    "西甲": "ESP-La Liga",
    "德甲": "GER-Bundesliga",
    "意甲": "ITA-Serie A",
    "法甲": "FRA-Ligue 1",
}

# 反向映射: soccerdata 代码 → 中文
LEAGUE_MAP_REVERSE = {v: k for k, v in LEAGUE_MAP.items()}

# ============================================================
# 球队名称映射: 中文 → Understat 英文
# ============================================================
# 英超
TEAM_NAME_MAP_EPL = {
    "阿森纳": "Arsenal", "维拉": "Aston Villa", "伯恩茅斯": "Bournemouth",
    "布伦特": "Brentford", "布赖顿": "Brighton", "切尔西": "Chelsea",
    "水晶宫": "Crystal Palace", "埃弗顿": "Everton", "富勒姆": "Fulham",
    "伊普斯": "Ipswich", "莱切斯特": "Leicester", "利物浦": "Liverpool",
    "曼城": "Manchester City", "曼联": "Manchester United",
    "纽卡斯尔": "Newcastle United", "诺丁汉": "Nottingham Forest",
    "南安普敦": "Southampton", "热刺": "Tottenham", "西汉姆联": "West Ham",
    "狼队": "Wolverhampton Wanderers",
    # 降级队 (历史数据可能出现)
    "伯恩利": "Burnley", "利兹联": "Leeds United", "桑德兰": "Sunderland",
    "诺维奇": "Norwich City", "沃特福德": "Watford",
    "谢菲联": "Sheffield United", "卢顿": "Luton",
}

# 西甲
TEAM_NAME_MAP_LALIGA = {
    "阿拉维斯": "Alaves", "毕尔巴鄂": "Athletic Club", "马竞": "Atletico Madrid",
    "巴萨": "Barcelona", "塞尔塔": "Celta Vigo", "西班牙人": "Espanyol",
    "赫塔费": "Getafe", "赫罗纳": "Girona", "拉帕马斯": "Las Palmas",
    "莱加内斯": "Leganes", "马洛卡": "Mallorca", "奥萨苏纳": "Osasuna",
    "巴列卡诺": "Rayo Vallecano", "贝蒂斯": "Real Betis", "皇马": "Real Madrid",
    "皇家社会": "Real Sociedad", "巴利亚多": "Real Valladolid",
    "塞维利亚": "Sevilla", "巴伦西亚": "Valencia", "比利亚雷": "Villarreal",
    "奥维耶多": "Real Oviedo",
    # 降级/历史
    "埃尔切": "Elche", "莱万特": "Levante", "阿尔梅里亚": "Almeria",
    "加的斯": "Cadiz", "格拉纳达": "Granada",
}

# 德甲
TEAM_NAME_MAP_BUNDESLIGA = {
    "奥格斯堡": "Augsburg", "勒沃库森": "Bayer Leverkusen", "拜仁": "Bayern Munich",
    "波鸿": "Bochum", "多特蒙德": "Borussia Dortmund", "门兴": "Borussia M.Gladbach",
    "法兰克福": "Eintracht Frankfurt", "海登海姆": "FC Heidenheim",
    "弗赖堡": "Freiburg", "霍芬海姆": "Hoffenheim", "基尔": "Holstein Kiel",
    "美因茨": "Mainz 05", "莱红牛": "RasenBallsport Leipzig", "圣保利": "St. Pauli",
    "柏林联合": "Union Berlin", "斯图加特": "VfB Stuttgart",
    "不来梅": "Werder Bremen", "沃夫斯堡": "Wolfsburg",
    # 降级/历史
    "科隆": "FC Cologne", "汉堡": "Hamburger SV", "达姆施塔特": "Darmstadt",
    "埃沃斯堡": "Elversberg",
}

# 意甲
TEAM_NAME_MAP_SERIEA = {
    "AC米兰": "AC Milan", "亚特兰大": "Atalanta", "博洛尼亚": "Bologna",
    "卡利亚里": "Cagliari", "科莫": "Como", "恩波利": "Empoli",
    "佛罗伦萨": "Fiorentina", "热那亚": "Genoa", "国际米兰": "Inter",
    "尤文图斯": "Juventus", "拉齐奥": "Lazio", "莱切": "Lecce",
    "蒙扎": "Monza", "那不勒斯": "Napoli", "帕尔马": "Parma Calcio 1913",
    "罗马": "Roma", "都灵": "Torino", "乌迪内斯": "Udinese",
    "威尼斯": "Venezia", "维罗纳": "Verona",
    # 降级/历史
    "克雷莫纳": "Cremonese", "萨索洛": "Sassuolo", "弗洛西诺内": "Frosinone",
    "比萨": "Pisa", "萨勒尼塔纳": "Salernitana",
}

# 法甲
TEAM_NAME_MAP_LIGUE1 = {
    "昂热": "Angers", "欧塞尔": "Auxerre", "布雷斯特": "Brest",
    "勒阿弗尔": "Le Havre", "朗斯": "Lens", "里尔": "Lille",
    "里昂": "Lyon", "马赛": "Marseille", "摩纳哥": "Monaco",
    "蒙彼利埃": "Montpellier", "南特": "Nantes", "尼斯": "Nice",
    "巴黎圣曼": "Paris Saint Germain", "兰斯": "Reims", "雷恩": "Rennes",
    "圣埃蒂安": "Saint-Etienne", "斯特拉斯": "Strasbourg", "图卢兹": "Toulouse",
    # 降级/历史
    "梅斯": "Metz", "洛里昂": "Lorient", "圣旺红星": "Red Star",
    "巴黎FC": "Paris FC", "克莱蒙": "Clermont Foot",
}

# 合并所有映射
TEAM_NAME_MAP = {}
TEAM_NAME_MAP.update(TEAM_NAME_MAP_EPL)
TEAM_NAME_MAP.update(TEAM_NAME_MAP_LALIGA)
TEAM_NAME_MAP.update(TEAM_NAME_MAP_BUNDESLIGA)
TEAM_NAME_MAP.update(TEAM_NAME_MAP_SERIEA)
TEAM_NAME_MAP.update(TEAM_NAME_MAP_LIGUE1)

# 反向映射: 英文 → 中文
TEAM_NAME_MAP_REVERSE = {v: k for k, v in TEAM_NAME_MAP.items()}

# ============================================================
# 数据源配置
# ============================================================
class config:
    # 数据源开关
    understat_enabled = True
    fbref_enabled = False     # FBref需要Chrome浏览器, 默认禁用 (Understat 已提供全部所需数据)
    whoscored_enabled = False  # 已禁用

    # 赛季列表 (Understat格式)
    seasons = ["2023-2024", "2024-2025", "2025-2026"]

    # 缓存有效期 (秒) — 7天
    cache_max_age = 7 * 24 * 3600

    # 滚动统计窗口
    rolling_window = 10

    # 贝叶斯收缩强度
    bayes_k = 10

    # 模型权重
    model_weights = {
        "poisson": 0.35,
        "xgboost": 0.35,
        "elo": 0.30,
    }


# ============================================================
# 辅助函数
# ============================================================
def cn_to_en_team(cn_name: str) -> str | None:
    """中文球队名 → Understat 英文名"""
    return TEAM_NAME_MAP.get(cn_name)


def en_to_cn_team(en_name: str) -> str | None:
    """Understat 英文名 → 中文球队名"""
    return TEAM_NAME_MAP_REVERSE.get(en_name)


def cn_to_en_league(cn_league: str) -> str | None:
    """中文联赛名 → soccerdata 代码"""
    return LEAGUE_MAP.get(cn_league)


def is_big5_league(cn_league: str) -> bool:
    """是否为五大联赛"""
    return cn_league in LEAGUE_MAP
