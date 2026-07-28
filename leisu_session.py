#!/usr/bin/env python3
"""leisu 会话工具 — WAF自动求解 + 页面获取 (供探索与生产共用)"""
import requests, re, json, subprocess, os

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Referer': 'https://www.leisu.com/',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}
WORK_DIR = os.path.dirname(os.path.abspath(__file__))

def solve_waf(session, challenge_html):
    rd_m = re.search(r'<textarea[^>]*id="renderData"[^>]*>(.*?)</textarea>', challenge_html, re.DOTALL)
    if not rd_m:
        return False
    rd_path = os.path.join(WORK_DIR, 'renderData.json')
    with open(rd_path, 'w', encoding='utf-8') as f:
        f.write(rd_m.group(1).replace('&quot;', '"'))
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', challenge_html, re.DOTALL)
    paths = []
    for i, sc in enumerate(scripts):
        p = os.path.join(WORK_DIR, f'waf_script_{i}.js')
        with open(p, 'w', encoding='utf-8') as f:
            f.write(sc)
        paths.append(p)
    res = subprocess.run(['node', os.path.join(WORK_DIR, 'solve_waf_jsdom_v2.js'), rd_path] + paths,
                         capture_output=True, text=True, timeout=30)
    cookie = res.stdout.strip()
    if 'acw_sc__v2=' not in cookie:
        return False
    session.cookies.set('acw_sc__v2', cookie.split('acw_sc__v2=')[1].split(';')[0])
    return True

def leisu_get(session, url):
    """获取leisu页面, 自动过WAF. 返回 (html, ok)"""
    r = session.get(url, headers=HEADERS, timeout=15)
    if 'arg1=' in r.text and 'renderData' in r.text:
        if not solve_waf(session, r.text):
            return r.text, False
        r = session.get(url, headers=HEADERS, timeout=15)
        if 'arg1=' in r.text:
            return r.text, False
    return r.text, True

if __name__ == '__main__':
    s = requests.Session()
    for u in ['https://www.leisu.com/guide', 'https://www.leisu.com/news',
              'https://www.leisu.com/live/zuqiu', 'https://www.leisu.com/']:
        html, ok = leisu_get(s, u)
        links = re.findall(r'href="([^"]*swot[^"]*)"', html)
        print(u, 'ok=', ok, 'bytes=', len(html), 'swot_links=', links[:8])
