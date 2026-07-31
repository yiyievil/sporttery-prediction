#!/usr/bin/env python3
"""SWOT快速批量获取 v3 - 优化版

核心优化:
1. WAF自动求解: requests获取挑战页 -> Node.js/jsdom执行脚本 -> 获取acw_sc__v2 cookie
2. Cookie复用: 一次求解, 全部9页复用
3. HTML解析: 直接从响应HTML提取SWOT数据 (无需等待captcha渲染)

预期效果: 9个页面从15分钟降至<15秒
"""

import requests
import re
import json
import subprocess
import os
import time
import shutil
import tempfile
from datetime import datetime

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Referer': 'https://www.leisu.com/',
}

# Ultra-Opt: 通用路径 (旧版硬编码 '/data/user/work' 和 '/workspace')
WORK_DIR = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
SOLVER_JS = os.path.join(WORK_DIR, 'solve_waf_jsdom_v2.js')
SWOT_OUTPUT = os.path.join(WORK_DIR, 'predictions', 'swot_data_fast.json')


# ============ WAF Solver ============

def solve_waf(session, url):
    """解决Alibaba Cloud WAF挑战, 返回cookie"""
    # 1. 获取WAF挑战页面
    resp = session.get(url, headers=HEADERS, timeout=15)

    if 'arg1=' not in resp.text:
        if 'aliyunCaptcha' in resp.text and 'children good' not in resp.text:
            return None, 'captcha_no_content'
        return resp.text, 'ok_no_waf'

    # 2. 提取renderData和脚本
    rd_match = re.search(r'<textarea[^>]*id="renderData"[^>]*>(.*?)</textarea>', resp.text, re.DOTALL)
    if not rd_match:
        return None, 'no_renderData'

    rd_text = rd_match.group(1).replace('&quot;', '"')
    try:
        renderData = json.loads(rd_text)
    except:
        return None, 'renderData_parse_error'

    arg1 = renderData.get('l1', '')[10:60]
    print(f"  WAF arg1: {arg1}")

    # 保存脚本与renderData到临时目录, 用后清理 (M22: 不再写入工作目录)
    tmp_dir = tempfile.mkdtemp(prefix='waf_solver_')
    try:
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', resp.text, re.DOTALL)
        for i, script in enumerate(scripts):
            path = os.path.join(tmp_dir, f'waf_script_{i}.js')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(script)

        # 保存renderData
        rd_path = os.path.join(tmp_dir, 'renderData.json')
        with open(rd_path, 'w', encoding='utf-8') as f:
            json.dump(renderData, f)

        # 3. 用Node.js/jsdom执行WAF脚本
        result = subprocess.run(
            ['node', SOLVER_JS, rd_path],
            capture_output=True, text=True, timeout=15,
            cwd=WORK_DIR
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    cookie = result.stdout.strip()
    if not cookie or len(cookie) < 10:
        print(f"  WAF solve failed: {result.stderr[-200:]}")
        return None, 'waf_solve_failed'

    print(f"  WAF cookie: {cookie[:30]}...")
    session.cookies.set('acw_sc__v2', cookie, domain='.leisu.com', path='/')
    return None, 'waf_solved'


# ============ SWOT HTML Parser ============

def clean_html(text):
    """清理HTML标签"""
    return re.sub(r'<[^>]+>', '', text).strip()


def extract_team_names(html):
    """从页面提取主队和客队名称"""
    # 方法1: 从title提取 "代格福什vs尤尔加登比赛优劣势分析"
    title_match = re.search(r'<title>(.*?)</title>', html)
    if title_match:
        title = title_match.group(1)
        # 提取 "A vs B" 或 "AvsB"
        vs_match = re.search(r'(.+?)vs(.+?)(?:比赛|优劣|_独家)', title)
        if vs_match:
            home = vs_match.group(1).strip()
            away = vs_match.group(2).strip()
            return home, away

    # 方法2: 从页面内容提取
    # 查找第一个和第二个出现的队伍名
    spans = re.findall(r'<span[^>]*class="[^"]*"[^>]*>([^<]{2,10})</span>', html)
    # 过滤掉非队伍名的span
    for s in spans:
        if len(s) >= 2 and not any(c in s for c in 'vsVS胜负平'):
            # 检查是否在bar-list区域
            idx = html.find(s)
            context = html[max(0,idx-200):idx+200]
            if 'bar' in context.lower() or 'team' in context.lower():
                pass

    return None, None


def parse_swot_from_html(html, url):
    """从SWOT页面HTML解析数据

    页面结构:
      <span class="name">主队</span>
      <div class="children good">...有利情报...</div>
      <div class="children harmful">...不利情报...</div>
      ...
      <span class="name">客队</span>
      <div class="children good">...有利情报...</div>
      <div class="children harmful">...不利情报...</div>
    """
    swot = {
        'home_strengths': [],
        'home_weaknesses': [],
        'away_strengths': [],
        'away_weaknesses': [],
        'trend': {},
        'swot_url': url,
    }

    if not html:
        return swot

    # 提取队伍名称
    home_name, away_name = extract_team_names(html)

    # 找到所有 <span class="name">队伍名</span> 的位置
    team_spans = [(m.start(), m.group(1)) for m in re.finditer(r'<span class="name">([^<]+)</span>', html)]

    # 找到所有 children good / children harmful 区块的位置
    sections = []
    for m in re.finditer(r'class="[^"]*children (good|harmful)[^"]*"[^>]*>(.*?)</ul>', html, re.DOTALL):
        sections.append((m.start(), m.group(1), m.group(2)))

    # 将每个区块归属到最近的上方队伍名
    home_good, home_harm, away_good, away_harm = [], [], [], []
    for sec_pos, sec_type, sec_content in sections:
        # 找到该区块上方最近的 team_span
        owner = None
        for sp_pos, sp_name in team_spans:
            if sp_pos < sec_pos:
                owner = sp_name
            else:
                break

        items = [clean_html(li) for li in re.findall(r'<li[^>]*>(.*?)</li>', sec_content, re.DOTALL)]
        items = [it for it in items if it]

        if owner and home_name and owner == home_name:
            if sec_type == 'good':
                home_good.extend(items)
            else:
                home_harm.extend(items)
        elif owner and away_name and owner == away_name:
            if sec_type == 'good':
                away_good.extend(items)
            else:
                away_harm.extend(items)
        else:
            # 回退: 按出现顺序, 第1个good归主队, 第2个归客队
            if sec_type == 'good':
                if not home_good:
                    home_good.extend(items)
                else:
                    away_good.extend(items)
            else:
                if not home_harm:
                    home_harm.extend(items)
                else:
                    away_harm.extend(items)

    swot['home_strengths'] = home_good
    swot['home_weaknesses'] = home_harm
    swot['away_strengths'] = away_good
    swot['away_weaknesses'] = away_harm

    # 提取走势数据
    # 先去除HTML标签, 再搜索 "历史交锋 X胜 Y平 Z负"
    text_only = re.sub(r'<[^>]+>', ' ', html)
    text_only = re.sub(r'\s+', ' ', text_only)
    trend_match = re.search(r'历史交锋\s*(\d+)\s*胜\s*(\d+)\s*平\s*(\d+)\s*负', text_only)
    if trend_match:
        home_wins = int(trend_match.group(1))
        draws = int(trend_match.group(2))
        away_wins = int(trend_match.group(3))
        total = home_wins + draws + away_wins
        swot['trend'] = {
            'total': total,
            'home_win_pct': f"{home_wins*100//total}%" if total > 0 else "0%",
            'draw_pct': f"{draws*100//total}%" if total > 0 else "0%",
            'away_win_pct': f"{away_wins*100//total}%" if total > 0 else "0%",
        }

    # 存储队伍名
    if home_name:
        swot['home_name'] = home_name
    if away_name:
        swot['away_name'] = away_name

    return swot


# ============ Batch Fetcher ============

def fetch_swot_batch_fast(swot_urls, match_keys=None):
    """快速批量获取SWOT数据

    优化: WAF解决一次, cookie复用全部页面
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    start_time = time.time()

    print(f"SWOT快速批量获取: {len(swot_urls)}个页面")
    print(f"[1/3] 解决WAF挑战...")

    # 第一步: 解决WAF
    first_url = swot_urls[0]
    html, status = solve_waf(session, first_url)

    if status == 'ok_no_waf' and html:
        # 没有WAF, 直接解析
        first_key = match_keys[0] if match_keys else "url_0"
        results = {first_key: parse_swot_from_html(html, first_url)}
        print(f"  ✅ {first_key}: 已获取")
    elif status == 'waf_solved':
        # WAF已解决, 需要重新获取页面
        results = {}
        html, status = solve_waf(session, first_url)
        if html:
            first_key = match_keys[0] if match_keys else "url_0"
            results[first_key] = parse_swot_from_html(html, first_url)
            print(f"  ✅ {first_key}: 已获取")
    else:
        # WAF解决失败, 尝试直接获取 (可能部分页面不需要WAF)
        print(f"  ⚠️ WAF解决: {status}, 尝试直接获取...")
        results = {}

    # 第二步: 批量获取剩余页面
    print(f"\n[2/3] 批量获取剩余页面 (复用cookie)...")
    if results:
        # 第一个URL已获取, 处理剩余的
        remaining_urls = swot_urls[1:]
        if match_keys:
            remaining_keys = match_keys[1:]
        else:
            remaining_keys = [f"url_{i}" for i in range(1, len(swot_urls))]
    else:
        # 第一个URL未获取, 处理全部
        remaining_urls = swot_urls
        remaining_keys = match_keys if match_keys else [f"url_{i}" for i in range(len(swot_urls))]

    for url, key in zip(remaining_urls, remaining_keys):
        try:
            resp = session.get(url, timeout=15)
            html = resp.text

            # 检查是否需要重新解决WAF
            if 'arg1=' in html:
                print(f"  🔄 {key}: WAF重新挑战, 重新解决...")
                html, status = solve_waf(session, url)
                if html:
                    resp_html = html
                else:
                    resp = session.get(url, timeout=15)
                    resp_html = resp.text
            else:
                resp_html = html

            # 解析SWOT数据
            swot_data = parse_swot_from_html(resp_html, url)
            results[key] = swot_data

            item_count = (len(swot_data['home_strengths']) + len(swot_data['home_weaknesses']) +
                         len(swot_data['away_strengths']) + len(swot_data['away_weaknesses']))
            print(f"  ✅ {key}: {item_count}条情报 (主优{len(swot_data['home_strengths'])}/主劣{len(swot_data['home_weaknesses'])}/客优{len(swot_data['away_strengths'])}/客劣{len(swot_data['away_weaknesses'])})")

        except Exception as e:
            print(f"  ❌ {key}: {e}")
            results[key] = {
                'home_strengths': [], 'home_weaknesses': [],
                'away_strengths': [], 'away_weaknesses': [],
                'trend': {}, 'swot_url': url, 'error': str(e)
            }

    elapsed = time.time() - start_time

    # 第三步: 保存
    print(f"\n[3/3] 保存结果...")
    output = {
        'fetched_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'method': 'WAF_solve + cookie_reuse + HTML_parse',
        'total_urls': len(swot_urls),
        'success_count': sum(1 for v in results.values() if 'error' not in v),
        'elapsed_seconds': round(elapsed, 1),
        'matches': results,
    }

    with open(SWOT_OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"✅ SWOT快速获取完成!")
    print(f"  成功: {output['success_count']}/{len(swot_urls)}")
    print(f"  耗时: {elapsed:.1f}秒 (对比之前: ~900秒/15分钟)")
    if elapsed > 0:
        print(f"  提速: {900/elapsed:.0f}x")
    print(f"  输出: {SWOT_OUTPUT}")
    print(f"{'='*60}")

    return results


# ============ Main ============

# 9个SWOT URL对应 周六203-211 (韩K联201-202用已有数据)
SWOT_URLS_203_211 = [
    "https://www.leisu.com/guide/swot-4467105",  # 203 代格福什vs佐加顿斯
    "https://www.leisu.com/guide/swot-4468154",  # 204 玛丽港vsAC奥卢
    "https://www.leisu.com/guide/swot-4467612",  # 205 克里斯蒂vs斯达
    "https://www.leisu.com/guide/swot-4468155",  # 206 库奥皮奥vs瓦萨
    "https://www.leisu.com/guide/swot-4467108",  # 207 卡尔马vs米亚尔比
    "https://www.leisu.com/guide/swot-4465718",  # 209 桑托斯vs沙佩科
    "https://www.leisu.com/guide/swot-4465753",  # 208 巴竞技vs巴西国际
    "https://www.leisu.com/guide/swot-4460389",  # 210 圣迭戈FCvs达拉斯
    "https://www.leisu.com/guide/swot-4460394",  # 211 圣何塞vs洛城银河
]
SWOT_KEYS_203_211 = ["周六203", "周六204", "周六205", "周六206", "周六207", "周六209", "周六208", "周六210", "周六211"]


if __name__ == '__main__':
    results = fetch_swot_batch_fast(SWOT_URLS_203_211, match_keys=SWOT_KEYS_203_211)

    # 预览结果
    print(f"\n--- 解析结果预览 ---")
    for key, data in results.items():
        hn = data.get('home_name', '?')
        an = data.get('away_name', '?')
        hs = len(data.get('home_strengths', []))
        hw = len(data.get('home_weaknesses', []))
        as_ = len(data.get('away_strengths', []))
        aw = len(data.get('away_weaknesses', []))
        trend = data.get('trend', {})
        err = data.get('error', '')
        print(f"  {key}: {hn} vs {an} | 主优{hs}/主劣{hw}/客优{as_}/客劣{aw} | 走势:{trend.get('total','N/A')} {'错误:'+err if err else ''}")
