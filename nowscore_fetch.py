#!/usr/bin/env python3
"""
nowscore_fetch.py — Nowscore数据获取模块
功能: 从nowscore.com获取亚盘/欧赔/大小球数据, 作为主要数据源
设计: nowscore为主 → 500.com备用降级

数据获取流程 (2026-07-25 更新):
1. 获取matchID: 优先从match_id_map.json缓存读取 → 失败则获取sc1.js赛程解析
2. 用matchID获取3in1Odds.aspx → 解析三合一盘口数据 (live子域名,稳定可用)
3. 转换为与500.com函数兼容的数据格式

端点变更说明:
- www.nowscore.com 主域名已失效 (3in1Odds.aspx和data/sc1.js均重定向到404)
- live.nowscore.com 子域名稳定可用:
  - 盘口: https://live.nowscore.com/odds/3in1Odds.aspx?companyid=3&id={mid} ✅
  - 赛程页: https://live.nowscore.com/schedule.aspx?f=sc1 ✅ (HTML,含JS引用)
  - 赛程数据: https://live.nowscore.com/data/sc1.js ⚠️ (代理阻止,需浏览器获取)

代理问题处理:
- 3in1Odds.aspx 在live子域名可直连 (已验证6/6成功, 3/3稳定性)
- data/sc1.js 路径被代理阻止 (ProxyError), 需通过浏览器预取到缓存
- 失败时返回None, 由调用方降级到500.com
"""

import requests
import re
import json
import os
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

# ============================================================
# 配置
# ============================================================
# 2026-07-25: www.nowscore.com 主域名已失效(3in1Odds.aspx和data/sc1.js均重定向到404)
# 稳定可用端点迁移至 live.nowscore.com 子域名:
#   盘口: https://live.nowscore.com/odds/3in1Odds.aspx?companyid=3&id={mid}
#   赛程: https://live.nowscore.com/schedule.aspx?f=sc1 (HTML页面,JS动态加载data/sc1.js)
# 注意: data/sc1.js 路径被代理阻止(Max retries exceeded), 需通过浏览器获取或使用match_id_map.json
NOWSCORE_BASE = 'https://live.nowscore.com'
NOWSCORE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Referer': 'https://live.nowscore.com/schedule.aspx?f=sc1',
}

# 浏览器预取缓存目录
# Ultra-Opt: 优先读 SPORTTERY_WORKSPACE 环境变量, 缺省回退脚本所在目录
# (旧版硬编码 '/data/user/work/nowscore_cache' 为Linux路径, Windows上缓存永不命中)
_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
BROWSER_CACHE_DIR = os.path.join(_WORKSPACE, 'nowscore_cache')

# 球队名别名映射 (sporttery名 → nowscore可能的名称变体)
# 覆盖韩K联、日职、英超、西甲、德甲、意甲、法甲等主流联赛
TEAM_NAME_ALIASES = {
    # ===== 韩国联赛 =====
    '金泉尚武': ['金泉尚武', '金泉'],
    '大田市民': ['大田市民', '大田'],
    '浦项制铁': ['浦项制铁', '浦项'],
    '全北现代': ['全北现代', '全北'],
    '光州FC': ['光州FC', '光州'],
    '济州SK': ['济州SK', '济州联', '济州'],
    '安养FC': ['安养FC', 'FC安养', '安养'],
    '江原FC': ['江原FC', '江原'],
    '首尔FC': ['首尔FC', 'FC首尔', '首尔'],
    '蔚山现代': ['蔚山现代', '蔚山HD', '蔚山'],
    '仁川联': ['仁川联', '仁川联队', '仁川'],
    '富川FC': ['富川FC', '富川'],
    '大邱FC': ['大邱FC', '大邱'],
    '水原FC': ['水原FC', '水原'],
    '水原三星': ['水原三星', '水原'],
    # ===== 北欧联赛 =====
    'TPS图尔': ['TPS图尔', 'TPS图尔库', 'TPS土尔库', 'TPS杜古', 'TPS Turku'],
    '赫尔辛基': ['赫尔辛基', '赫爾辛基', 'HJK'],
    '国际图尔': ['国际图尔', '国际土尔库', '英特土尔库', 'Inter Turku'],
    '坦山猫': ['坦山猫', '坦佩雷山猫', 'Ilves'],
    # ===== 日本联赛 =====
    '横滨水手': ['横滨水手', '横滨'],
    '川崎前锋': ['川崎前锋', '川崎'],
    '鹿岛鹿角': ['鹿岛鹿角', '鹿岛'],
    '浦和红钻': ['浦和红钻', '浦和'],
    '大阪钢巴': ['大阪钢巴', '钢巴'],
    '大阪樱花': ['大阪樱花', '樱花'],
    '名古屋鲸': ['名古屋鲸', '名古屋'],
    '广岛三箭': ['广岛三箭', '广岛'],
    '札幌冈萨': ['札幌冈萨', '札幌'],
    '福冈黄蜂': ['福冈黄蜂', '福冈'],
    '神户胜利船': ['神户胜利船', '神户'],
    '东京FC': ['东京FC', 'FC东京', '东京'],
    '东京绿茵': ['东京绿茵', '绿茵'],
    # ===== 英超 =====
    '曼城': ['曼城', '曼彻斯特城'],
    '利物浦': ['利物浦'],
    '阿森纳': ['阿森纳', '阿仙奴'],
    '切尔西': ['切尔西', '车路士'],
    '热刺': ['热刺', '托特纳姆'],
    '曼联': ['曼联', '曼彻斯特联'],
    '纽卡斯尔': ['纽卡斯尔', '纽卡素'],
    '布莱顿': ['布莱顿', '白礼顿'],
    '西汉姆': ['西汉姆', '韦斯咸'],
    '维拉': ['维拉', '阿斯顿维拉'],
    '埃弗顿': ['埃弗顿', '爱华顿'],
    '富勒姆': ['富勒姆', '富咸'],
    '伯恩利': ['伯恩利', '般尼'],
    '狼队': ['狼队', '狼'],
    '诺丁汉': ['诺丁汉', '诺定咸'],
    '水晶宫': ['水晶宫', '水晶'],
    '伯恩茅斯': ['伯恩茅斯', '般尼茅夫'],
    '布伦特': ['布伦特', '宾福特'],
    '卢顿': ['卢顿'],
    '谢菲联': ['谢菲联', '锡菲联'],
    # ===== 西甲 =====
    '皇马': ['皇马', '皇家马德里'],
    '巴萨': ['巴萨', '巴塞罗那'],
    '马竞': ['马竞', '马德里竞技'],
    '塞维利亚': ['塞维利亚'],
    '毕尔巴鄂': ['毕尔巴鄂'],
    '皇家社会': ['皇家社会'],
    '比利亚雷亚尔': ['比利亚雷亚尔', '维拉利尔'],
    '瓦伦西亚': ['瓦伦西亚'],
    '赫塔费': ['赫塔费'],
    '奥萨苏纳': ['奥萨苏纳'],
    # ===== 德甲 =====
    '拜仁': ['拜仁', '拜仁慕尼黑'],
    '多特蒙德': ['多特蒙德', '多蒙特'],
    '莱比锡': ['莱比锡', '莱比锡红牛'],
    '勒沃库森': ['勒沃库森', '利华古逊'],
    '法兰克福': ['法兰克福'],
    '沃尔夫斯堡': ['沃尔夫斯堡', '禾夫斯堡'],
    '门兴': ['门兴', '门兴格拉德巴赫'],
    '弗赖堡': ['弗赖堡', '费雷堡'],
    '霍芬海姆': ['霍芬海姆', '贺芬咸'],
    # ===== 意甲 =====
    '国米': ['国米', '国际米兰'],
    '米兰': ['米兰', 'AC米兰'],
    '尤文': ['尤文', '尤文图斯'],
    '那不勒斯': ['那不勒斯', '拿坡里'],
    '罗马': ['罗马'],
    '拉齐奥': ['拉齐奥', '拉素'],
    '亚特兰大': ['亚特兰大', '阿特兰大'],
    '佛罗伦萨': ['佛罗伦萨', '费伦天拿'],
    # ===== 法甲 =====
    '巴黎': ['巴黎', '巴黎圣日耳曼', 'PSG'],
    '摩纳哥': ['摩纳哥'],
    '马赛': ['马赛'],
    '里昂': ['里昂'],
    '里尔': ['里尔'],
    # ===== 瑞典超 =====
    '马尔默': ['马尔默', 'Malmö', '马模'],
    '哈马比': ['哈马比', 'Hammarby'],
    '佐加顿斯': ['佐加顿斯', 'Djurgårdens', '佐加顿'],
    '索尔纳': ['索尔纳', 'AIK'],
    '哥德堡': ['哥德堡', 'Göteborg'],
    '赫根': ['赫根', 'Häcken'],
    '北雪平': ['北雪平', 'Norrköping'],
    '埃尔夫斯堡': ['埃尔夫斯堡', 'Elfsborg'],
    '卡尔马': ['卡尔马', 'Kalmar'],
    '米亚尔比': ['米亚尔比', 'Mjällby'],
    '韦纳穆': ['韦纳穆', 'Värnamo'],
    '天狼星': ['天狼星', 'Sirius'],
    '布洛马波': ['布洛马波', 'Brommapojkarna'],
    '瓦斯特拉': ['瓦斯特拉', 'Västerås'],
    # ===== 挪威超 =====
    '博德闪耀': ['博德闪耀', 'Bodø/Glimt', '波杜基林特'],
    '布兰': ['布兰', 'Brann'],
    '莫尔德': ['莫尔德', 'Molde'],
    '维京': ['维京', 'Viking'],
    '罗森博格': ['罗森博格', 'Rosenborg'],
    '萨普斯堡': ['萨普斯堡', 'Sarpsborg'],
    '腓特烈': ['腓特烈', 'Fredrikstad'],
    '克里斯蒂': ['克里斯蒂', 'Kristiansund'],
    '海于格松': ['海于格松', 'Haugesund'],
    '斯托姆加': ['斯托姆加', 'Strømsgodset'],
    '利勒斯特': ['利勒斯特', 'Lillestrøm'],
    'KFUM奥斯陆': ['KFUM奥斯陆', 'KFUM Oslo'],
    '汉坎': ['汉坎', 'HamKam'],
    '特罗姆瑟': ['特罗姆瑟', 'Tromsø'],
    # ===== 丹麦超 =====
    '哥本哈根': ['哥本哈根', 'København'],
    '中日德兰': ['中日德兰', 'Midtjylland'],
    '布隆德比': ['布隆德比', 'Brøndby'],
    '北西兰': ['北西兰', 'Nordsjælland'],
    '奥尔堡': ['奥尔堡', 'AaB'],
    '锡尔克堡': ['锡尔克堡', 'Silkeborg'],
    '欧登塞': ['欧登塞', 'Odense'],
    '兰讷斯': ['兰讷斯', 'Randers'],
    '奥胡斯': ['奥胡斯', 'AGF'],
    '哈维德夫': ['哈维德夫', 'Hvidovre'],
    '林比': ['林比', 'Lyngby'],
    '维堡': ['维堡', 'Viborg'],
    # ===== 巴西甲 =====
    '帕尔梅拉斯': ['帕尔梅拉斯', 'Palmeiras'],
    '弗拉门戈': ['弗拉门戈', 'Flamengo'],
    '米内罗竞技': ['米内罗竞技', 'Atlético Mineiro'],
    '博塔弗戈': ['博塔弗戈', 'Botafogo'],
    '圣保罗': ['圣保罗', 'São Paulo'],
    '格雷米奥': ['格雷米奥', 'Grêmio'],
    '弗鲁米嫩': ['弗鲁米嫩', 'Fluminense'],
    '布拉甘蒂诺': ['布拉甘蒂诺', 'Bragantino'],
    '国际': ['国际', 'Internacional'],
    '科林蒂安': ['科林蒂安', 'Corinthians'],
    '巴伊亚': ['巴伊亚', 'Bahia'],
    '克鲁塞罗': ['克鲁塞罗', 'Cruzeiro'],
    '福塔雷萨': ['福塔雷萨', 'Fortaleza'],
    '维多利亚': ['维多利亚', 'Vitória'],
    '瓦斯科达': ['瓦斯科达', 'Vasco da Gama'],
    '尤文图德': ['尤文图德', 'Juventude'],
    '库亚巴': ['库亚巴', 'Cuiabá'],
    '米美洲': ['米美洲', 'América Mineiro'],
    '桑托斯': ['桑托斯', 'Santos'],
    # ===== 阿根廷甲 =====
    '河床': ['河床', 'River Plate'],
    '博卡青年': ['博卡青年', 'Boca Juniors', '博卡'],
    '竞技': ['竞技', 'Racing Club'],
    '圣洛伦索': ['圣洛伦索', 'San Lorenzo'],
    '独立': ['独立', 'Independiente'],
    '萨斯菲尔德': ['萨斯菲尔德', 'Vélez Sarsfield'],
    '纽维尔': ['纽维尔', "Newell's Old Boys"],
    '拉普拉塔': ['拉普拉塔', 'Estudiantes'],
    '班菲尔德': ['班菲尔德', 'Banfield'],
    '科尔多瓦': ['科尔多瓦', 'Talleres'],
    '罗萨里奥': ['罗萨里奥', 'Rosario Central'],
    '萨兰迪': ['萨兰迪', 'Arsenal'],
    '戈多伊': ['戈多伊', 'Godoy Cruz'],
    '巴拉卡斯': ['巴拉卡斯', 'Barracas Central'],
    '防卫者': ['防卫者', 'Defensa y Justicia'],
    '飓风': ['飓风', 'Huracán'],
    # ===== 俄超 =====
    '泽尼特': ['泽尼特', 'Zenit'],
    '中央陆军': ['中央陆军', 'CSKA'],
    '斯巴达': ['斯巴达', 'Spartak'],
    '迪纳摩': ['迪纳摩', 'Dynamo'],
    '火车头': ['火车头', 'Lokomotiv'],
    '克拉斯诺': ['克拉斯诺', 'Krasnodar'],
    '索契': ['索契', 'Sochi'],
    '喀山红宝石': ['喀山红宝石', 'Rubin'],
    '罗斯托夫': ['罗斯托夫', 'Rostov'],
    '奥伦堡': ['奥伦堡', 'Orenburg'],
    '沃罗涅日': ['沃罗涅日', 'Fakel'],
    '格罗兹尼': ['格罗兹尼', 'Akhmat'],
    '希姆基': ['希姆基', 'Khimki'],
    '乌拉尔': ['乌拉尔', 'Ural'],
    '下诺夫哥': ['下诺夫哥', 'Nizhny Novgorod'],
    '巴尔蒂卡': ['巴尔蒂卡', 'Baltika'],
    # ===== 美职联 =====
    '迈阿密国际': ['迈阿密国际', 'Inter Miami'],
    '洛杉矶FC': ['洛杉矶FC', 'Los Angeles FC'],
    '银河': ['银河', 'LA Galaxy'],
    '西雅图': ['西雅图', 'Seattle Sounders'],
    '纽约城': ['纽约城', 'New York City FC'],
    '红牛': ['红牛', 'New York Red Bulls'],
    '哥伦布': ['哥伦布', 'Columbus Crew'],
    '辛辛那提': ['辛辛那提', 'Cincinnati'],
    '费城': ['费城', 'Philadelphia Union'],
    '亚特兰大联': ['亚特兰大联', 'Atlanta United'],
    '纳什维尔': ['纳什维尔', 'Nashville SC'],
    '夏洛特': ['夏洛特', 'Charlotte FC'],
    '奥兰多': ['奥兰多', 'Orlando City'],
    '华盛顿': ['华盛顿', 'D.C. United'],
    '新英格兰': ['新英格兰', 'New England Revolution'],
    '蒙特利尔': ['蒙特利尔', 'Montréal'],
    '多伦多': ['多伦多', 'Toronto FC'],
    '芝加哥': ['芝加哥', 'Chicago Fire'],
    '堪萨斯城': ['堪萨斯城', 'Sporting KC'],
    '休斯敦': ['休斯敦', 'Houston Dynamo'],
    '达拉斯': ['达拉斯', 'FC Dallas'],
    '奥斯汀': ['奥斯汀', 'Austin FC'],
    '圣何塞': ['圣何塞', 'San Jose Earthquakes'],
    '波特兰': ['波特兰', 'Portland Timbers'],
    '盐湖城': ['盐湖城', 'Real Salt Lake'],
    '科罗拉多': ['科罗拉多', 'Colorado Rapids'],
    '温哥华': ['温哥华', 'Vancouver Whitecaps'],
    '明尼苏达': ['明尼苏达', 'Minnesota United'],
    # ===== 欧冠/欧联资格赛球队 (sporttery名 vs nowscore名) =====
    '库奥皮奥': ['库奥皮奥', '古比斯', 'KuPS', 'KuPs', 'Kuopio'],
    '格风暴': ['格风暴', '格拉茨风暴', '格拉茨', 'Sturm Graz'],
    '萨格勒布迪纳摩': ['萨格勒布迪纳摩', '萨格勒布', '戴拿模', 'Dinamo Zagreb'],
    '林肯红魔': ['林肯红魔', '林肯', 'Lincoln Red Imps'],
    '图恩': ['图恩', '杜安', 'Thun'],
    '采列': ['采列', 'Celje'],
    '埃格纳蒂亚': ['埃格纳蒂亚', 'Egnatia'],
    '沙姆洛克': ['沙姆洛克', '沙姆洛克流浪', 'Shamrock Rovers'],
    '阿拉特亚美尼亚': ['阿拉特亚美尼亚', 'Alashkert'],
    # 注: '博德闪耀' 已在上面 挪威超 分组中定义 (值相同), 此处删除重复键
    '加拉塔萨': ['加拉塔萨', '加拉塔萨雷', 'Galatasaray'],
    '埃因霍温': ['埃因霍温', 'PSV', '飞燕诺'],
    '布鲁日': ['布鲁日', 'Club Brugge'],
    '圣吉联合': ['圣吉联合', '圣吉尔联合', 'Union SG'],
    '贝蒂斯': ['贝蒂斯', '皇家贝蒂斯', 'Betis'],
    '博莱斯拉': ['博莱斯拉', 'Boleslav'],
    '西班牙人': ['西班牙人', 'Espanyol'],
    '比利亚雷': ['比利亚雷', '比利亚雷亚尔', '维拉利尔'],
}


# ============================================================
# WebBridge 渲染取数 (Ultra-Opt 2026-07-26)
# sc1.js 数据文件被CDN封锁(浏览器直接访问也报 ERR_HTTP2_PROTOCOL_ERROR),
# 但 schedule.aspx 页面在浏览器内可正常渲染完整赛程表。
# 通过 Kimi WebBridge (127.0.0.1:10086) 驱动用户真实浏览器:
#   navigate schedule.aspx?f=sc1/sc2 → evaluate 提取 #table_live 行 → matchID
# 全流程无需人工预取, 与500.com一样自动。
# ============================================================
WEBBRIDGE_URL = 'http://127.0.0.1:10086/command'
WEBBRIDGE_SESSION = 'nowscore-auto'

_SCHEDULE_EXTRACT_JS = (
    "(() => { const rows = [...document.querySelectorAll('#table_live tr')];"
    " const out = []; for (const tr of rows) {"
    " const a = tr.querySelector('a[href*=\"3in1Odds\"]'); if (!a) continue;"
    " const mid = (a.href.match(/id=(\\d+)/)||[])[1]; if (!mid) continue;"
    " const tds = [...tr.querySelectorAll('td')].map(td => td.innerText.trim().replace(/\\s+/g,' '));"
    " out.push({mid, cells: tds}); } return JSON.stringify(out); })()"
)


def _webbridge_cmd(action, args, timeout=40):
    """调用WebBridge守护进程, 返回data字段; 失败返回None"""
    try:
        r = requests.post(WEBBRIDGE_URL,
                          json={'action': action, 'args': args, 'session': WEBBRIDGE_SESSION},
                          timeout=timeout)
        body = r.json()
        if body.get('ok'):
            return body.get('data')
    except Exception:
        pass
    return None


def _strip_rank(name):
    """去除球队名后的排名括号, 如 '克卢日[14]' → '克卢日', '阿蒂斯布尔诺[捷乙3]' → '阿蒂斯布尔诺'"""
    return re.sub(r'\[.*?\]', '', name).strip()


def fetch_schedule_rendered(schedule_type='sc1'):
    """通过WebBridge渲染schedule.aspx提取赛程 (主通道)

    返回: [{mid, home, away, league, time, date}, ...] 与parse_schedule同格式, 失败返回None
    """
    url = f'{NOWSCORE_BASE}/schedule.aspx?f={schedule_type}'
    nav = _webbridge_cmd('navigate', {'url': url})
    if not nav or not nav.get('success'):
        return None
    time.sleep(4)  # 等待页面渲染 (赛程表由JS异步生成)
    data = _webbridge_cmd('evaluate', {'code': _SCHEDULE_EXTRACT_JS})
    if not data or data.get('type') != 'string':
        return None
    try:
        rows = json.loads(data['value'])
    except (json.JSONDecodeError, TypeError):
        return None
    if not rows:
        return None

    date_str = (datetime.now() + timedelta(days=0 if schedule_type == 'sc1' else 1)).strftime('%Y-%m-%d')
    matches = []
    for row in rows:
        cells = row.get('cells', [])
        # 列结构: 选,联赛,时间,状态,主队,比分,客队,半场,让球,大小,数据
        if len(cells) < 7:
            continue
        matches.append({
            'mid': row['mid'],
            'league': cells[1],
            'time': cells[2],
            'home': _strip_rank(cells[4]),
            'away': _strip_rank(cells[6]),
            'league_en': '',
            'home_en': '', 'away_en': '', 'home_tw': '', 'away_tw': '',
            'date': date_str,
        })
    return matches or None


def fetch_all_schedules_rendered():
    """获取sc1(今日)+sc2(明日)渲染赛程, 合并返回"""
    combined = []
    for st in ('sc1', 'sc2'):
        ms = fetch_schedule_rendered(st)
        if ms:
            combined.extend(ms)
    return combined or None


def _fetch_with_retry(url, max_retries=2, timeout=10):
    """带重试的HTTPS请求 (Ultra-Opt: 降超时10s、重试2次)
    
    检测并处理:
    - 404重定向 (www.nowscore.com失效端点会重定向到/Home/Path404)
    - ProxyError (live.nowscore.com/data/路径被代理阻止)
    
    超时策略: 10s/次 × 2次 = 最坏20s/URL (旧版20s×3=60s)
    """
    session = requests.Session()
    session.headers.update(NOWSCORE_HEADERS)
    direct_session = None  # 绕过系统代理的直连会话 (ProxyError时启用)
    for attempt in range(max_retries):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 200:
                # 检测404重定向页面 (www.nowscore.com失效端点)
                if 'Path404' in r.url or '找不到页面' in r.text[:500]:
                    return None
                return r.text
        except requests.exceptions.ProxyError:
            # 代理阻止 (常见于live.nowscore.com/data/路径)
            # Ultra-Opt: ProxyError时立即改用直连 (trust_env=False, 绕过系统代理)
            # M12: 原 `if attempt >= max_retries - 2:` 在 max_retries=2 时恒真, 已删除该判断直接直连
            try:
                if direct_session is None:
                    direct_session = requests.Session()
                    direct_session.headers.update(NOWSCORE_HEADERS)
                    direct_session.trust_env = False
                r = direct_session.get(url, timeout=timeout, allow_redirects=True)
                if r.status_code == 200:
                    if 'Path404' in r.url or '找不到页面' in r.text[:500]:
                        return None
                    return r.text
            except Exception as e:
                print(f"  [WARN] {url} ProxyError后直连失败: {e}")
            if attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))
            else:
                return None
        except Exception as e:
            # M12: 打印一次错误摘要 (最后一次尝试时输出), 不再完全静默
            if attempt == max_retries - 1:
                print(f"  [WARN] {url} 请求失败: {e}")
            if attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))  # 指数退避
            else:
                return None
    return None


def fetch_schedule_bf():
    """通用赛程通道: GET /data/bf1.js (Ultra-Opt 2026-07-26)

    bf1.js 是即时比分数据文件, 与sc1.js同为 A[]/B[] 数组格式,
    单次请求覆盖今日+明日全部比赛 (实测 384+221场),
    纯requests直连可用, 不依赖 WebBridge/浏览器/Node。
    带磁盘缓存(30分钟), 避免重复下载索引。
    返回: js文本 (可直接交给parse_schedule), 失败返回None
    """
    cache_file = os.path.join(BROWSER_CACHE_DIR, 'bf1.js')
    # 磁盘缓存: 30分钟内有效
    if os.path.exists(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if age < 1800:
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    text = f.read()
                if 'A[' in text:
                    return text
            except Exception:
                pass
    url = f'{NOWSCORE_BASE}/data/bf1.js?{int(time.time() * 1000)}'
    text = _fetch_with_retry(url)
    if text and 'A[' in text:
        try:
            os.makedirs(BROWSER_CACHE_DIR, exist_ok=True)
            with open(cache_file, 'w', encoding='utf-8') as f:
                f.write(text)
        except Exception:
            pass
        return text
    return None


_BF_SCHEDULE_CACHE = None

def get_bf_schedules(force_refresh=False):
    """获取bf1.js赛程(解析后, 带进程级缓存)"""
    global _BF_SCHEDULE_CACHE
    if _BF_SCHEDULE_CACHE is not None and not force_refresh:
        return _BF_SCHEDULE_CACHE
    text = fetch_schedule_bf()
    _BF_SCHEDULE_CACHE = parse_schedule(text) if text else None
    return _BF_SCHEDULE_CACHE


def fetch_schedule_js(schedule_type='sc1'):
    """获取nowscore赛程数据 (sc1.js 或 sc2.js)
    
    参数:
        schedule_type: 'sc1' (今日/07-25) 或 'sc2' (07-26)
    
    返回: js文本内容, 或None(失败时)
    
    注意: live.nowscore.com/data/sc1.js 路径被代理阻止 (ProxyError)
    此函数在代理环境下通常返回None, 应优先使用:
    1. 浏览器预取缓存 (BROWSER_CACHE_DIR/sc1.js)
    2. match_id_map.json 映射文件 (load_match_id_map)
    """
    # 1. 尝试从浏览器缓存读取
    cache_file = os.path.join(BROWSER_CACHE_DIR, f'{schedule_type}.js')
    if os.path.exists(cache_file):
        cache_mtime = os.path.getmtime(cache_file)
        cache_age = time.time() - cache_mtime
        if cache_age < 3600:  # 缓存1小时内有效
            with open(cache_file, 'r', encoding='utf-8') as f:
                return f.read()
    
    # 2. 直接HTTPS请求 (live子域名, 可能因代理阻止失败)
    url = f'{NOWSCORE_BASE}/data/{schedule_type}.js?{int(time.time() * 1000)}'
    return _fetch_with_retry(url)


def fetch_all_schedules():
    """获取所有赛程数据 (sc1 + sc2), 合并返回"""
    combined = ''
    for st in ['sc1', 'sc2']:
        text = fetch_schedule_js(st)
        if text:
            combined += text + '\n'
    return combined if combined else None


def load_match_id_map():
    """加载matchID映射文件 (浏览器预取)
    
    M9: 带TTL时效检查 — 文件修改时间超过1小时视为过期, 忽略并返回None,
    避免复用旧赛程的matchID导致盘口错配。
    返回: {sporttery_key: nowscore_matchID} 或None
    """
    map_file = os.path.join(BROWSER_CACHE_DIR, 'match_id_map.json')
    if not os.path.exists(map_file):
        return None
    try:
        if time.time() - os.path.getmtime(map_file) > 3600:
            print("  [WARN] match_id_map.json 超过1小时未更新, 忽略缓存")
            return None
        with open(map_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"  [WARN] match_id_map.json 读取失败: {e}")
        return None


def parse_schedule(sc1_text):
    """解析sc1.js赛程数据, 提取比赛列表
    
    返回: [{mid, home, away, league, leagueEn, time, date}, ...]
    """
    if not sc1_text:
        return []
    
    lines = sc1_text.split('\n')
    
    # 解析B数组 (联赛映射)
    b_data = {}
    for line in lines:
        if line.startswith('B['):
            m = re.match(r'B\[(\d+)\]=\[([^\]]+)\]', line)
            if m:
                fields = m.group(2).split(',')
                fields = [f.strip().strip("'").strip('"') for f in fields]
                b_data[m.group(1)] = fields
    
    # 解析A数组 (比赛数据)
    matches = []
    for line in lines:
        if not line.startswith('A['):
            continue
        m = re.match(r'A\[(\d+)\]=\[([^\]]+)\]', line)
        if not m:
            continue
        
        fields = m.group(2).split(',')
        fields = [f.strip().strip("'").strip('"') for f in fields]
        if len(fields) < 12:
            continue
        
        league_idx = fields[1]
        league_info = b_data.get(league_idx, [])
        league_name = league_info[1] if len(league_info) > 1 else ''
        league_en = league_info[3] if len(league_info) > 3 else ''
        
        matches.append({
            'mid': fields[0],
            'home': fields[4],
            'home_tw': fields[5] if len(fields) > 5 else '',
            'home_en': fields[6] if len(fields) > 6 else '',
            'away': fields[7],
            'away_tw': fields[8] if len(fields) > 8 else '',
            'away_en': fields[9] if len(fields) > 9 else '',
            'league': league_name,
            'league_en': league_en,
            'time': fields[10],
            'date': fields[11],
        })
    
    return matches


def _clean_team_name(name):
    """去除HTML标签和常见后缀，返回干净队名"""
    import re as _re
    clean = _re.sub(r'<[^>]+>', '', str(name)).strip()
    # 去除 "(中)" 等中立场标记
    clean = _re.sub(r'\(.*?\)', '', clean).strip()
    return clean


def _name_similarity(a, b):
    """计算两个队名的字符级相似度 (0~1)
    
    用于别名表无法覆盖时的模糊匹配兜底。
    基于共有字符占比，对中文队名效果较好。
    """
    a = _clean_team_name(a)
    b = _clean_team_name(b)
    if not a or not b:
        return 0.0
    # 完全匹配
    if a == b:
        return 1.0
    # 子串包含
    if a in b or b in a:
        return 0.9
    # 字符级Jaccard相似度 (对中文队名有效)
    set_a = set(a)
    set_b = set(b)
    intersection = set_a & set_b
    union = set_a | set_b
    jaccard = len(intersection) / len(union) if union else 0.0
    # 要求至少2个字符共有，避免单字巧合
    if len(intersection) < 2:
        return 0.0
    return jaccard


def find_match_by_teams(matches, home_name, away_name):
    """通过球队名匹配nowscore比赛
    
    参数:
        matches: parse_schedule()返回的比赛列表
        home_name: sporttery主队名
        away_name: sporttery客队名
    
    返回: matchID字符串, 或None
    
    匹配策略 (三级):
        1. 别名表精确匹配 (子串包含)
        2. 英文名匹配 (别名表中的英文别名 vs nowscore home_en/away_en)
        3. 模糊匹配兜底 (字符相似度 >= 0.6)
    """
    home_aliases = TEAM_NAME_ALIASES.get(home_name, [home_name])
    away_aliases = TEAM_NAME_ALIASES.get(away_name, [away_name])
    
    # Level 1: 别名子串匹配 (原逻辑)
    for match in matches:
        ns_home = _clean_team_name(match['home'])
        ns_away = _clean_team_name(match['away'])
        
        # 检查主队名匹配
        home_match = any(alias in ns_home or ns_home in alias for alias in home_aliases)
        away_match = any(alias in ns_away or ns_away in alias for alias in away_aliases)
        
        if home_match and away_match:
            return match['mid']
        
        # 也尝试反转(以防主客队颠倒)
        home_rev = any(alias in ns_away or ns_away in alias for alias in home_aliases)
        away_rev = any(alias in ns_home or ns_home in alias for alias in away_aliases)
        if home_rev and away_rev:
            return match['mid']
    
    # Level 2: 英文名匹配 (别名表中的英文 vs nowscore home_en/away_en)
    home_en_aliases = [a for a in home_aliases if re.match(r'^[A-Za-z]', a)]
    away_en_aliases = [a for a in away_aliases if re.match(r'^[A-Za-z]', a)]
    if home_en_aliases or away_en_aliases:
        for match in matches:
            ns_home_en = match.get('home_en', '')
            ns_away_en = match.get('away_en', '')
            home_en_match = any(en.lower() in ns_home_en.lower() or ns_home_en.lower() in en.lower()
                                for en in home_en_aliases) if home_en_aliases else False
            away_en_match = any(en.lower() in ns_away_en.lower() or ns_away_en.lower() in en.lower()
                                for en in away_en_aliases) if away_en_aliases else False
            if home_en_match and away_en_match:
                return match['mid']
            # 反转
            home_en_rev = any(en.lower() in ns_away_en.lower() or ns_away_en.lower() in en.lower()
                              for en in home_en_aliases) if home_en_aliases else False
            away_en_rev = any(en.lower() in ns_home_en.lower() or ns_home_en.lower() in en.lower()
                              for en in away_en_aliases) if away_en_aliases else False
            if home_en_rev and away_en_rev:
                return match['mid']
    
    # Level 3: 模糊匹配兜底 (字符相似度)
    # 当别名表未覆盖时，用字符级相似度作为最后手段
    best_score = 0.0
    best_mid = None
    SIM_THRESHOLD = 0.6  # 相似度阈值: 60%共有字符
    for match in matches:
        ns_home = _clean_team_name(match['home'])
        ns_away = _clean_team_name(match['away'])
        
        # 正向: home vs ns_home, away vs ns_away
        h_sim = _name_similarity(home_name, ns_home)
        a_sim = _name_similarity(away_name, ns_away)
        score_fwd = min(h_sim, a_sim)
        
        # 反向: home vs ns_away, away vs ns_home
        h_sim_rev = _name_similarity(home_name, ns_away)
        a_sim_rev = _name_similarity(away_name, ns_home)
        score_rev = min(h_sim_rev, a_sim_rev)
        
        score = max(score_fwd, score_rev)
        if score > best_score:
            best_score = score
            best_mid = match['mid']
    
    if best_score >= SIM_THRESHOLD and best_mid:
        return best_mid
    
    return None


def fetch_3in1_odds(match_id):
    """获取三合一盘口数据 (3in1Odds.aspx)
    
    返回: {asian, overunder, european} 或None
    
    缓存优先级:
    1. JSON缓存 (浏览器预取的结构化数据, 含asian/overunder/european数组)
    2. HTML缓存 (浏览器预取的3in1Odds.aspx原始HTML)
    3. 直接HTTPS请求 (可能因代理超时失败)
    4. 过期缓存兜底 (HTTPS失败时使用)
    """
    # JSON缓存TTL: 30分钟 (与HTML缓存一致, 即时赔率必须定期刷新)
    JSON_CACHE_TTL = 1800

    # 1. 尝试从JSON缓存读取 (TTL过期则跳过)
    json_cache = os.path.join(BROWSER_CACHE_DIR, f'odds_{match_id}.json')
    if os.path.exists(json_cache):
        cache_age = time.time() - os.path.getmtime(json_cache)
        if cache_age < JSON_CACHE_TTL:
            try:
                with open(json_cache, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if 'asian' in data and 'overunder' in data and 'european' in data:
                    # 空缓存壳 (三表全空) 视为未命中, 返回 None 走后续路径, 避免被误判为 nowscore 成功
                    if data['asian'] or data['overunder'] or data['european']:
                        return {
                            'asian': data['asian'],
                            'overunder': data['overunder'],
                            'european': data['european'],
                        }
            except (json.JSONDecodeError, KeyError):
                pass
        # JSON缓存过期, 继续尝试其他途径
    
    # 2. 尝试从HTML缓存读取
    html_cache = os.path.join(BROWSER_CACHE_DIR, f'odds_{match_id}.html')
    if os.path.exists(html_cache):
        cache_mtime = os.path.getmtime(html_cache)
        cache_age = time.time() - cache_mtime
        if cache_age < 1800:  # HTML缓存30分钟内有效
            with open(html_cache, 'r', encoding='utf-8') as f:
                parsed = _parse_3in1_html(f.read())
                if parsed:
                    return parsed
    
    # 3. 直接HTTPS请求
    url = f'{NOWSCORE_BASE}/odds/3in1Odds.aspx?companyid=3&id={match_id}'
    html = _fetch_with_retry(url)
    if html:
        return _parse_3in1_html(html)
    
    # 4. 过期缓存兜底 (HTTPS失败时, 使用过期的HTML缓存)
    if os.path.exists(html_cache):
        try:
            with open(html_cache, 'r', encoding='utf-8') as f:
                parsed = _parse_3in1_html(f.read())
                if parsed:
                    print(f"  [nowscore] ⚠️ 使用过期缓存: {match_id}")
                    return parsed
        except Exception:
            pass
    
    return None


def _parse_3in1_html(html):
    """解析3in1Odds.aspx HTML, 提取三个表格数据"""
    if not html:
        return None
    
    # 提取所有<table>
    tables = re.findall(r'<table[^>]*>(.*?)</table>', html, re.S)
    if len(tables) < 3:
        return None
    
    def parse_table(table_html):
        """解析单个表格, 返回行数据列表"""
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.S)
        result = []
        for row in rows:
            cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.S)
            if cells:
                result.append([re.sub(r'<[^>]+>', '', c).strip() for c in cells])
        return result
    
    asian = parse_table(tables[0])
    overunder = parse_table(tables[1])
    european = parse_table(tables[2])
    
    return {'asian': asian, 'overunder': overunder, 'european': european}


def _parse_handicap(handicap_str):
    """解析亚盘盘口字符串为数值
    
    例如:
        "平手" → 0.0
        "受平/半" → -0.25
        "平/半" → 0.25
        "半球" → 0.5
        "受一球" → -1.0
    """
    handicap_str = handicap_str.strip()
    
    mapping = {
        '平手': 0.0,
        '平/半': 0.25,
        '半球': 0.5,
        '半/一': 0.75,
        '一球': 1.0,
        '一/球半': 1.25,
        '球半': 1.5,
        '球半/两': 1.75,
        '两球': 2.0,
        '两/两半': 2.25,
        '两半': 2.5,
        '两半/三': 2.75,
        '三球': 3.0,
    }
    
    is_receive = handicap_str.startswith('受')
    clean = handicap_str.replace('受', '').strip()
    
    # 直接映射
    if clean in mapping:
        val = mapping[clean]
        return -val if is_receive else val
    
    # 尝试解析 "X/Y" 格式 (如 "2/2.5")
    parts = clean.split('/')
    if len(parts) == 2:
        try:
            a = float(parts[0])
            b = float(parts[1])
            val = (a + b) / 2
            return -val if is_receive else val
        except ValueError:
            pass
    
    # 尝试直接解析为数字
    try:
        val = float(clean)
        return -val if is_receive else val
    except ValueError:
        pass
    
    return 0.0


# ============================================================
# Ultra-Opt: analysisJs/data{mid}.js 近况+对赛+积分数据 (2026-07-26)
# 单文件包含: h_data(主队近况) a_data(客队近况) v_data(交锋)
#            ScoreAll/ScoreHome/ScoreGuest(积分榜, 含场均进失球)
# 纯requests直连, 填补nowscore通道的 form/stats 缺口
# ============================================================

def _parse_js_rows(js_text, var_name):
    """解析 var h_data=[[...],[...]]; 中的行数组 (ast.literal_eval, 容忍null/true/false)"""
    import ast
    m = re.search(r'var\s+' + var_name + r'\s*=\s*(\[.*?\])\s*;\s*var', js_text, re.S)
    if not m:
        # 结尾可能不是 ;var 而是 ;\n 或文件尾
        m = re.search(r'var\s+' + var_name + r'\s*=\s*(\[.*?\])\s*;', js_text, re.S)
    if not m:
        return []
    text = m.group(1)
    text = text.replace('null', 'None').replace('true', 'True').replace('false', 'False')
    try:
        return ast.literal_eval(text)
    except Exception:
        return []


def _parse_js_str_array(js_text, var_name):
    """解析 var ScoreAll=Array("..|..|..", ...); 字符串数组"""
    m = re.search(r'var\s+' + var_name + r'\s*=\s*Array\((.*?)\)\s*;', js_text, re.S)
    if not m:
        return []
    return re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))


def _form_char_for_team(row, team_id):
    """从一行比赛数据计算目标球队的 W/D/L
    行格式: [date, sclassId, ?, color, homeId, homeLabel, awayId, awayLabel, hg, ag, ...]
    """
    try:
        home_id, away_id = int(row[4]), int(row[6])
        hg, ag = int(row[8]), int(row[9])
    except (ValueError, TypeError, IndexError):
        return None
    if team_id == home_id:
        gf, ga = hg, ag
    elif team_id == away_id:
        gf, ga = ag, hg
    else:
        return None
    return 'W' if gf > ga else ('D' if gf == ga else 'L')


def _build_form(rows, team_id, max_matches=10):
    """构造近况字符串 (引擎约定: 最后字符=最近一场)"""
    chars = []
    for row in rows[:max_matches]:  # 数据本身最新在前
        c = _form_char_for_team(row, team_id)
        if c:
            chars.append(c)
    return ''.join(reversed(chars))


def _parse_standings_row(s):
    """解析ScoreAll行: '|排名|队ID|繁名|简名|赛|胜|平|负|得|失|净|胜率|平率|负率|场均进|场均失|分|'"""
    p = s.split('|')
    if len(p) < 18:
        return None
    try:
        played = int(p[5])
        return {
            'rank': int(p[1]) if p[1] else 0,
            'team_id': int(p[2]),
            'name_cn': p[4],
            'played': played,
            'win': int(p[6]), 'draw': int(p[7]), 'lose': int(p[8]),
            'gf': int(p[9]), 'ga': int(p[10]),
            'avg_gf': float(p[15]) if p[15] else (int(p[9]) / played if played else 0),
            'avg_ga': float(p[16]) if p[16] else (int(p[10]) / played if played else 0),
        }
    except (ValueError, IndexError):
        return None


def fetch_analysis_data(match_id):
    """获取 analysisJs/data{mid}.js 的近况/交锋/积分数据

    返回: {
        'form_home': 'WDLWW', 'form_away': 'LWDDW',   # 最后字符=最近
        'h2h': '主队近N次交锋x胜x平x负',
        'stats_home': {avg_gf, avg_ga, rank, played, ...},
        'stats_away': {...},
        'home_id', 'away_id',
    } 或None
    """
    url = f'{NOWSCORE_BASE}/analysisJs/data{match_id}.js'
    js = _fetch_with_retry(url)
    if not js or 'h_data' not in js:
        return None

    # 主客队ID: teamNames JSON 前两个为 [主队, 客队] (分析页按比赛生成)
    home_id = away_id = None
    m = re.search(r'var\s+teamNames\s*=\s*(\[.*?\]);', js, re.S)
    if m:
        try:
            tn = json.loads(m.group(1))
            if len(tn) >= 2:
                home_id, away_id = int(tn[0]['TeamId']), int(tn[1]['TeamId'])
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    h_rows = _parse_js_rows(js, 'h_data')
    a_rows = _parse_js_rows(js, 'a_data')
    v_rows = _parse_js_rows(js, 'v_data')

    # 兜底: 用出现频率推断主队ID (h_data每行都包含主队)
    if home_id is None and h_rows:
        from collections import Counter
        ids = Counter()
        for row in h_rows:
            try:
                ids[int(row[4])] += 1
                ids[int(row[6])] += 1
            except (ValueError, TypeError, IndexError):
                pass
        if ids:
            home_id = ids.most_common(1)[0][0]
    # 客队ID: a_data中出现频率最高且≠home_id
    if away_id is None and a_rows:
        from collections import Counter
        ids = Counter()
        for row in a_rows:
            try:
                for idx in (4, 6):
                    tid = int(row[idx])
                    if tid != home_id:
                        ids[tid] += 1
            except (ValueError, TypeError, IndexError):
                pass
        if ids:
            away_id = ids.most_common(1)[0][0]

    result = {'home_id': home_id, 'away_id': away_id}

    # 近况 (最后字符=最近一场, 与引擎 exponential_decay_form 约定一致)
    if home_id and h_rows:
        result['form_home'] = _build_form(h_rows, home_id)
    if away_id and a_rows:
        result['form_away'] = _build_form(a_rows, away_id)

    # 交锋汇总 (主队视角)
    if home_id and v_rows:
        w = d = l = 0
        for row in v_rows:
            c = _form_char_for_team(row, home_id)
            if c == 'W':
                w += 1
            elif c == 'D':
                d += 1
            elif c == 'L':
                l += 1
        result['h2h'] = f'主队近{w+d+l}次交锋 {w}胜{d}平{l}负'
        result['h2h_detail'] = {'win': w, 'draw': d, 'lose': l, 'total': w + d + l}

    # 积分榜 → 场均进/失球 (λ建模核心输入)
    standings = [_parse_standings_row(s) for s in _parse_js_str_array(js, 'ScoreAll')]
    standings = [s for s in standings if s]
    for s in standings:
        if home_id and s['team_id'] == home_id:
            result['stats_home'] = s
        elif away_id and s['team_id'] == away_id:
            result['stats_away'] = s

    return result


def convert_nowscore_to_500_format(nowscore_odds, match_id=None):
    """将nowscore三合一数据转换为与500.com函数兼容的格式
    
    返回: {
        'ouzhi': {latest_w, latest_d, latest_l, init_w, init_d, init_l, count, ...},
        'daxiao': {goal_line, source, ...},
        'init_ouzhi': {...},
        'init_yazhi': {...},
        'init_daxiao': {...},
        'yazhi': {handicap, home_odds, away_odds, ...},
        'source': 'nowscore',
    }
    """
    if not nowscore_odds:
        return None
    
    # 空壳校验: 三表全空视为无效数据, 返回 None 让调用方触发降级
    if not (nowscore_odds.get('asian') or nowscore_odds.get('overunder') or nowscore_odds.get('european')):
        return None
    
    result = {'source': 'nowscore'}
    
    # ===== European Odds (欧赔) =====
    euro = nowscore_odds.get('european', [])
    if euro and len(euro) >= 2:
        # 第一行是表头, 第二行是最新, 最后一行是最初
        data_rows = [r for r in euro[1:] if len(r) >= 5]
        if data_rows:
            latest = data_rows[0]
            initial = data_rows[-1]
            
            # M8: 欧赔float转换加保护, 任一值非数字则跳过该段 (参考 _parse_handicap 防护风格)
            try:
                result['ouzhi'] = {
                    'latest_w': float(latest[2]),
                    'latest_d': float(latest[3]),
                    'latest_l': float(latest[4]),
                    'init_w': float(initial[2]),
                    'init_d': float(initial[3]),
                    'init_l': float(initial[4]),
                    'count': len(data_rows),
                    'change_w': float(latest[2]) - float(initial[2]),
                    'is_return_rate': False,
                }
                
                result['init_ouzhi'] = {
                    'avg_initial': (float(initial[2]), float(initial[3]), float(initial[4])),
                    'avg_instant': (float(latest[2]), float(latest[3]), float(latest[4])),
                    'initial': {
                        'w': float(initial[2]),
                        'd': float(initial[3]),
                        'l': float(initial[4]),
                    },
                    'instant': {
                        'w': float(latest[2]),
                        'd': float(latest[3]),
                        'l': float(latest[4]),
                    },
                    'num_valid': len(data_rows),
                    'change_w': float(latest[2]) - float(initial[2]),
                }
            except (TypeError, ValueError):
                pass
    
    # ===== Asian Handicap (亚盘) =====
    asian = nowscore_odds.get('asian', [])
    if asian and len(asian) >= 2:
        data_rows = [r for r in asian[1:] if len(r) >= 5]
        if data_rows:
            latest = data_rows[0]
            initial = data_rows[-1]
            
            latest_handicap = _parse_handicap(latest[3])
            init_handicap = _parse_handicap(initial[3])
            
            # M8: 亚盘水位float转换加保护, 失败则跳过该段
            try:
                result['yazhi'] = {
                    'handicap': latest_handicap,
                    'home_odds': float(latest[2]),
                    'away_odds': float(latest[4]),
                    'init_handicap': init_handicap,
                    'init_home_odds': float(initial[2]),
                    'init_away_odds': float(initial[4]),
                    'count': len(data_rows),
                }
                
                result['init_yazhi'] = {
                    'instant': {
                        'handicap_mode': latest_handicap,
                        'over_avg': float(latest[2]),
                        'under_avg': float(latest[4]),
                    },
                    'initial': {
                        'handicap_mode': init_handicap,
                        'over_avg': float(initial[2]),
                        'under_avg': float(initial[4]),
                    },
                    'num_valid': len(data_rows),
                }
            except (TypeError, ValueError):
                pass
    
    # ===== Over/Under (大小球) =====
    ou = nowscore_odds.get('overunder', [])
    if ou and len(ou) >= 2:
        data_rows = [r for r in ou[1:] if len(r) >= 5]
        if data_rows:
            latest = data_rows[0]
            initial = data_rows[-1]
            
            # M8: goal_line/水位float解析加保护 (如 "2.5", "2/2.5"),
            #     任一转换失败则跳过该段 (参考 _parse_handicap 防护风格)
            try:
                # 解析goal line (如 "2.5", "2/2.5")
                gl_str = latest[3]
                gl_parts = gl_str.split('/')
                if len(gl_parts) == 2:
                    goal_line = (float(gl_parts[0]) + float(gl_parts[1])) / 2
                else:
                    goal_line = float(gl_str)
                
                init_gl_str = initial[3]
                init_gl_parts = init_gl_str.split('/')
                if len(init_gl_parts) == 2:
                    init_goal_line = (float(init_gl_parts[0]) + float(init_gl_parts[1])) / 2
                else:
                    init_goal_line = float(init_gl_str)
                
                result['daxiao'] = {
                    'goal_line': goal_line,
                    'source': f'nowscore Crown(id={match_id})',
                    'all_goal_lines': [float(r[3].split('/')[0]) if '/' in r[3] else float(r[3]) for r in data_rows],
                    'num_bookmakers': len(data_rows),
                    'initial_goal_line': init_goal_line,
                    'over_odds': float(latest[2]),
                    'under_odds': float(latest[4]),
                }
                
                result['init_daxiao'] = {
                    'initial': {
                        'goal_line_mode': init_goal_line,
                        'over_avg': float(initial[2]),
                        'under_avg': float(initial[4]),
                    },
                    'instant': {
                        'goal_line_mode': goal_line,
                        'over_avg': float(latest[2]),
                        'under_avg': float(latest[4]),
                    },
                    'num_valid': len(data_rows),
                }
            except (TypeError, ValueError):
                pass
    
    # ===== shuju (近况+统计, nowscore不提供, 仅填充avg_odds) =====
    if 'ouzhi' in result:
        oz = result['ouzhi']
        result['shuju'] = {
            'avg_odds': {'w': oz['latest_w'], 'd': oz['latest_d'], 'l': oz['latest_l']},
            # form_home/away, stats_* 不可用 → predict_match有降级处理
            'source': 'nowscore',
        }
    
    return result


# Ultra-Opt: 渲染赛程进程级缓存 — 避免每场比赛都驱动浏览器翻页2次×4s
_RENDERED_SCHEDULE_CACHE = None

def get_rendered_schedules(force_refresh=False):
    """获取渲染赛程(带进程级缓存, 一次运行只驱动浏览器一次)"""
    global _RENDERED_SCHEDULE_CACHE
    if _RENDERED_SCHEDULE_CACHE is not None and not force_refresh:
        return _RENDERED_SCHEDULE_CACHE
    _RENDERED_SCHEDULE_CACHE = fetch_all_schedules_rendered()
    return _RENDERED_SCHEDULE_CACHE


def fetch_nowscore_match_data(home_name, away_name):
    """获取单场比赛的nowscore数据 (主入口)
    
    参数:
        home_name: sporttery主队名
        away_name: sporttery客队名
    
    返回: 转换后的数据dict (与500.com格式兼容), 或None
    """
    match_id = None
    
    # 0. 优先从matchID映射文件查找 (浏览器预取)
    id_map = load_match_id_map()
    if id_map:
        sporttery_key = f'{home_name}|{away_name}'
        match_id = id_map.get(sporttery_key)
        if not match_id:
            # 尝试反转
            sporttery_key_rev = f'{away_name}|{home_name}'
            match_id = id_map.get(sporttery_key_rev)
    
    # 1. 映射未找到 → 获取赛程解析
    if not match_id:
        # Ultra-Opt 主通道: bf1.js 纯requests直连 (通用, 不依赖WebBridge)
        matches = get_bf_schedules()
        if matches:
            match_id = find_match_by_teams(matches, home_name, away_name)
    
    if not match_id:
        # 降级1: WebBridge渲染赛程页 (bf1.js失效时的备用)
        matches = get_rendered_schedules()
        if matches:
            match_id = find_match_by_teams(matches, home_name, away_name)
    
    if not match_id:
        # 降级2: sc1.js缓存/直取 (旧通道, 当前CDN封锁基本不可用)
        schedule_text = fetch_all_schedules()
        if not schedule_text:
            schedule_text = fetch_schedule_js()
        if not schedule_text:
            return None
        matches = parse_schedule(schedule_text)
        match_id = find_match_by_teams(matches, home_name, away_name)
        if not match_id:
            return None
    
    # 3+5. Ultra-Opt: 3in1Odds 与 analysisJs 并行请求 (两者仅依赖match_id, 无相互依赖)
    #      旧版串行: odds(20s) + analysis(20s) = 40s/场 → 并行后 ≈20s/场
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_odds = pool.submit(fetch_3in1_odds, match_id)
        fut_ana = pool.submit(fetch_analysis_data, match_id)
        odds = fut_odds.result()
        ana = fut_ana.result()

    if not odds:
        return None

    # 4. 转换格式
    result = convert_nowscore_to_500_format(odds, match_id)
    if not result:
        return None

    # 5. 合并analysisJs数据 (近况/交锋/积分, 填补nowscore通道form/stats缺口)
    try:
        if ana:
            shuju = result.setdefault('shuju', {'source': 'nowscore'})
            if ana.get('form_home'):
                shuju['form_home'] = ana['form_home']
            if ana.get('form_away'):
                shuju['form_away'] = ana['form_away']
            if ana.get('h2h'):
                shuju['h2h'] = ana['h2h']
            # 引擎按 stats_<队名> 匹配, 用sporttery队名作键保证命中
            if ana.get('stats_home'):
                shuju[f'stats_{home_name}'] = ana['stats_home']
            if ana.get('stats_away'):
                shuju[f'stats_{away_name}'] = ana['stats_away']
    except Exception:
        pass  # 补充数据失败不影响主流程
    
    return result


def save_browser_cache(match_id, html_content):
    """保存浏览器预取的数据到缓存"""
    os.makedirs(BROWSER_CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(BROWSER_CACHE_DIR, f'odds_{match_id}.html')
    with open(cache_file, 'w', encoding='utf-8') as f:
        f.write(html_content)


def save_browser_cache_schedule(sc1_text):
    """保存浏览器预取的赛程数据到缓存"""
    os.makedirs(BROWSER_CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(BROWSER_CACHE_DIR, 'sc1.js')
    with open(cache_file, 'w', encoding='utf-8') as f:
        f.write(sc1_text)


def list_prefetch_urls(sporttery_matches):
    """根据体彩比赛列表, 生成需要浏览器预取的URL列表
    
    参数:
        sporttery_matches: {key: {home, away, ...}} 格式的比赛列表
    
    返回: [
        {'type': 'schedule', 'url': 'https://live.nowscore.com/data/sc1.js'},
        {'type': 'odds', 'url': 'https://live.nowscore.com/odds/3in1Odds.aspx?...', 'match_id': 'xxx', 'key': '周日201'},
        ...
    ]
    """
    urls = []
    
    # 1. 赛程数据URL (始终需要)
    urls.append({
        'type': 'schedule',
        'url': f'{NOWSCORE_BASE}/data/sc1.js?{int(time.time() * 1000)}',
    })
    
    # 2. 读取已缓存的赛程, 尝试匹配球队
    sc1_text = None
    cache_file = os.path.join(BROWSER_CACHE_DIR, 'sc1.js')
    if os.path.exists(cache_file):
        with open(cache_file, 'r', encoding='utf-8') as f:
            sc1_text = f.read()
    
    if sc1_text:
        matches = parse_schedule(sc1_text)
        for key, mi in sporttery_matches.items():
            mid = find_match_by_teams(matches, mi['home'], mi['away'])
            if mid:
                urls.append({
                    'type': 'odds',
                    'url': f'{NOWSCORE_BASE}/odds/3in1Odds.aspx?companyid=3&id={mid}',
                    'match_id': mid,
                    'key': key,
                    'home': mi['home'],
                    'away': mi['away'],
                })
            else:
                print(f"  [nowscore] 未找到: {key} {mi['home']} vs {mi['away']}")
    else:
        print("  [nowscore] 赛程未缓存, 需先预取sc1.js")
    
    return urls


def has_cache_for_match(match_id):
    """检查某场比赛的盘口数据是否已缓存"""
    json_cache = os.path.join(BROWSER_CACHE_DIR, f'odds_{match_id}.json')
    html_cache = os.path.join(BROWSER_CACHE_DIR, f'odds_{match_id}.html')
    return os.path.exists(json_cache) or os.path.exists(html_cache)


def get_cached_match_id(home_name, away_name):
    """从已缓存的赛程中查找matchID (不触发网络请求)"""
    cache_file = os.path.join(BROWSER_CACHE_DIR, 'sc1.js')
    if not os.path.exists(cache_file):
        return None
    with open(cache_file, 'r', encoding='utf-8') as f:
        sc1_text = f.read()
    matches = parse_schedule(sc1_text)
    return find_match_by_teams(matches, home_name, away_name)


# ============================================================
# 测试
# ============================================================
if __name__ == '__main__':
    print("=== Nowscore数据获取测试 ===")
    
    # 测试1: 获取赛程
    print("\n1. 获取赛程数据...")
    sc1 = fetch_schedule_js()
    if sc1:
        matches = parse_schedule(sc1)
        korean = [m for m in matches if 'KOR' in m.get('league_en', '') or '韩K' in m.get('league', '')]
        print(f"  总比赛数: {len(matches)}")
        print(f"  韩国联赛比赛数: {len(korean)}")
        for m in korean:
            print(f"    [{m['mid']}] {m['home']} vs {m['away']} ({m['league']}) {m['time']} {m['date']}")
    else:
        print("  ❌ 赛程数据获取失败 (HTTPS代理问题)")
        print("  💡 提示: 可通过浏览器预取数据到缓存目录")
    
    # 测试2: 匹配球队
    print("\n2. 球队匹配测试...")
    if sc1:
        matches = parse_schedule(sc1)
        for home, away in [('金泉尚武', '大田市民'), ('浦项制铁', '全北现代'), ('首尔FC', '蔚山现代')]:
            mid = find_match_by_teams(matches, home, away)
            if mid:
                print(f"  ✅ {home} vs {away} → matchID={mid}")
            else:
                print(f"  ❌ {home} vs {away} → 未找到")
    
    # 测试3: 获取盘口数据
    print("\n3. 盘口数据获取测试...")
    if sc1:
        matches = parse_schedule(sc1)
        mid = find_match_by_teams(matches, '金泉尚武', '大田市民')
        if mid:
            odds = fetch_3in1_odds(mid)
            if odds:
                converted = convert_nowscore_to_500_format(odds, mid)
                print(f"  ✅ 转换成功!")
                if converted:
                    print(f"     欧赔: {converted.get('ouzhi', {}).get('init_w', '?')}/{converted.get('ouzhi', {}).get('init_d', '?')}/{converted.get('ouzhi', {}).get('init_l', '?')} → {converted.get('ouzhi', {}).get('latest_w', '?')}/{converted.get('ouzhi', {}).get('latest_d', '?')}/{converted.get('ouzhi', {}).get('latest_l', '?')}")
                    print(f"     亚盘: {converted.get('yazhi', {}).get('init_handicap', '?')} → {converted.get('yazhi', {}).get('handicap', '?')}")
                    print(f"     大小: {converted.get('daxiao', {}).get('initial_goal_line', '?')} → {converted.get('daxiao', {}).get('goal_line', '?')}")
            else:
                print(f"  ❌ 盘口数据获取失败")
