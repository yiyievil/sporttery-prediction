# -*- coding: utf-8 -*-
"""注入首回合赛果情报到 swot_data_refreshed.json (次回合形势修正)"""
import json, os
from datetime import datetime

WS = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(WS, 'predictions', 'swot_data_refreshed.json')

with open(PATH, 'r', encoding='utf-8') as f:
    data = json.load(f)

FIRST_LEG = {
    "周二001": {
        "home_name": "库奥皮奥", "away_name": "萨巴赫", "source": "manual_first_leg",
        "home_strengths": [
            "库奥皮奥首回合客场仅0-1小负，回到熟悉的主场人工草场地，联赛领跑士气尚可，保留翻盘希望",
        ],
        "home_weaknesses": [
            "首回合0-1落后意味着本场必须净胜两球才能直接晋级，只能被迫全线压上，后场空档巨大，极易被反击偷袭",
            "主力中卫布尔基因伤缺阵、中场萨洛停赛，防线重组，双线作战体能处于劣势",
        ],
        "away_strengths": [
            "萨巴赫首回合1-0取胜占得先机，本场打平即可晋级，战术主动权完全在手，可安心低位稳守反击",
            "萨巴赫正式比赛客场连续20场保持不败，最近5个客场全胜，客场韧性极强",
            "主力中卫索尔维特解禁复出，防线比首回合更完整，且球队单线备战体能充沛",
        ],
        "away_weaknesses": [],
    },
    "周二002": {
        "home_name": "哈茨", "away_name": "格风暴", "source": "manual_first_leg",
        "home_strengths": [
            "哈茨首回合虽0-4惨败但全场19脚射门5次绝佳机会，主场连续8场取胜，狂热氛围下为荣誉而战战意十足",
            "格风暴手握4球优势大概率大幅轮换留力，哈茨有望借对手松懈打出体面结果",
        ],
        "home_weaknesses": [],
        "away_strengths": [
            "格风暴首回合4-0完胜，实力与心理双重碾压，即便轮换阵容深度也占优",
        ],
        "away_weaknesses": [
            "格风暴领先4球晋级几无悬念，客场战意明显下降，可能轮换主力避免伤病，不会全力投入",
        ],
    },
    "周三001": {
        "home_name": "阿拉木图", "away_name": "奥莫尼亚", "source": "manual_first_leg",
        "home_strengths": [
            "阿拉木图首回合控球57%、危险进攻99次、角球11-1全面压制，只因临门一脚欠佳0-1惜败，回到高原主场战力加成明显",
            "球队各项赛事七连胜且每场至少进2球，主场必须赢球的绝境战意拉满，首回合停赛的马丁诺维奇与奥克萨宁解禁归队",
            "奥莫尼亚联赛尚未开赛仅靠热身维持状态，正式比赛节奏存疑，且需长途跋涉远赴中亚客场",
        ],
        "home_weaknesses": [
            "首回合0-1落后必须主动强攻，核心小将萨特帕耶夫转会切尔西后锋线终结能力下降，久攻不下易被打反击",
        ],
        "away_strengths": [
            "奥莫尼亚首回合1-0领先且上半场就少打一人仍守住胜果，防守韧性经过验证，本场打平即可晋级",
        ],
        "away_weaknesses": [
            "奥莫尼亚两个月无正式比赛，状态节奏成疑，主力左后卫基佐斯长期伤缺，左路防守存在短板",
        ],
    },
    "周三002": {
        "home_name": "波兹南", "away_name": "奥胡斯", "source": "manual_first_leg",
        "home_strengths": [
            "波兹南首回合客场4-1大胜，实力优势明显，回到主场晋级形势极为有利",
        ],
        "home_weaknesses": [
            "波兹南总比分领先3球晋级几乎锁定，本场无需冒险争胜，可能轮换留力四天后的联赛，比赛强度或下降",
        ],
        "away_strengths": [
            "奥胡斯首回合1-4惨败已无任何包袱，只剩全力进攻一条路，反而可能放手一搏打出开放局面",
        ],
        "away_weaknesses": [
            "奥胡斯需净胜3球才能翻盘希望渺茫，士气受挫，首回合主场都溃败客场更难组织有效抵抗",
        ],
    },
}

for k, v in FIRST_LEG.items():
    v['trend'] = None
    v['swot_url'] = ''
    data['matches'][k] = v

data['refreshed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
with open(PATH, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=1)
print('injected:', list(FIRST_LEG.keys()))
