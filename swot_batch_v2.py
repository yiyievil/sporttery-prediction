#!/usr/bin/env python3
"""SWOT批量获取优化版 v2

优化策略:
1. 使用requests + WAF自动求解 (替代WebFetch逐页获取)
2. 首次请求获取WAF挑战页, 解析arg1, 生成acw_sc__v2 cookie
3. 后续所有请求复用cookie, 无需再次挑战
4. 并行解析SWOT数据, 大幅提速

预期效果: 9个页面从15分钟降至<30秒
"""

import json
import re
import time
import os
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}

# Ultra-Opt: 通用路径 (旧版硬编码 '/workspace')
_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
SWOT_OUTPUT = os.path.join(_WORKSPACE, 'predictions', 'swot_data_batch.json')


def unsbox(arg1):
    """Alibaba Cloud WAF: 字符重排 (标准算法)"""
    box = [0xF, 0x23, 0x1d, 0x16, 0x1b, 0x1e, 0x08, 0x15, 0x1a, 0x09,
           0x14, 0x1f, 0x07, 0x1c, 0x05, 0x19, 0x03, 0x1a, 0x10, 0x12,
           0x0C, 0x04, 0x11, 0x17, 0x0E, 0x19, 0x0F, 0x1D, 0x18, 0x02,
           0x01, 0x06]
    result = [''] * 48
    for i in range(min(len(arg1), len(box))):
        if box[i] < 48:
            result[box[i]] = arg1[i]
    return ''.join(result)


def hex_xor(s, key):
    """Alibaba Cloud WAF: 十六进制异或"""
    result = ''
    for i in range(min(len(s), len(key))):
        val = int(s[i], 16) ^ int(key[i], 16)
        result += format(val, 'x')
    return result


def solve_waf_challenge(html):
    """从WAF挑战页解析arg1并生成acw_sc__v2 cookie"""
    # 提取arg1
    match = re.search(r"arg1='([a-f0-9]+)'", html)
    if not match:
        return None

    arg1 = match.group(1)

    # 标准算法: unsbox -> hexXor
    rearranged = unsbox(arg1)
    cookie_val = hex_xor(rearranged, arg1)

    return cookie_val


def fetch_with_waf_bypass(url, session, max_retries=3):
    """获取页面, 自动处理WAF挑战"""
    for attempt in range(max_retries):
        resp = session.get(url, headers=HEADERS, timeout=15, allow_redirects=True)

        # 检查是否是WAF挑战页
        if 'acw_sc__v2' in resp.text and 'arg1=' in resp.text:
            cookie_val = solve_waf_challenge(resp.text)
            if cookie_val:
                session.cookies.set('acw_sc__v2', cookie_val, domain='.leisu.com', path='/')
                continue  # 重试, 这次带cookie

        # 检查是否是滑块验证页 (更高级WAF)
        if 'aliyunCaptcha' in resp.text or 'Access Verification' in resp.text:
            return None, 'captcha_required'

        # 正常页面
        if resp.status_code == 200 and 'arg1=' not in resp.text:
            return resp.text, 'ok'

        # 其他情况
        if resp.status_code == 200 and 'arg1=' in resp.text:
            cookie_val = solve_waf_challenge(resp.text)
            if cookie_val:
                session.cookies.set('acw_sc__v2', cookie_val, domain='.leisu.com', path='/')
                continue

    return None, 'max_retries'


def parse_swot_data(html, url):
    """从SWOT页面HTML解析数据"""
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

    # 尝试从JSON数据中提取 (leisu.com通常有内嵌JSON)
    # 方法1: 查找页面中的数据块
    try:
        # 查找优势/劣势标题块
        # leisu.com SWOT页面结构: 包含 "优势" "劣势" 等标题

        # 主队优势
        home_s_match = re.findall(r'(?:主队|主场)[^<]*优势[^>]*>(.*?)(?:<|$)', html, re.DOTALL)
        # 简化: 直接查找文本内容

        # 方法2: 查找所有包含 "优势" "劣势" 的段落
        # leisu.com的SWOT数据通常在class中标记

        # 查找home strengths
        hs_pattern = r'class="[^"]*home[^"]*strength[^"]*"[^>]*>(.*?)</div>'
        hs_matches = re.findall(hs_pattern, html, re.DOTALL | re.IGNORECASE)
        for m in hs_matches:
            clean = re.sub(r'<[^>]+>', '', m).strip()
            if clean:
                swot['home_strengths'].append(clean)

        # 查找home weaknesses
        hw_pattern = r'class="[^"]*home[^"]*weak[^"]*"[^>]*>(.*?)</div>'
        hw_matches = re.findall(hw_pattern, html, re.DOTALL | re.IGNORECASE)
        for m in hw_matches:
            clean = re.sub(r'<[^>]+>', '', m).strip()
            if clean:
                swot['home_weaknesses'].append(clean)

        # 方法3: 使用更通用的解析方式
        # leisu.com SWOT页面有特定的数据结构
        # 查找所有li/p标签中的文本
        all_items = re.findall(r'<(?:li|p)[^>]*>(.*?)</(?:li|p)>', html, re.DOTALL)
        
        # 方法4: 查找JSON数据块
        json_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', html, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                return parse_swot_from_json(data, swot, url)
            except:
                pass

        # 方法5: 查找所有包含特定关键词的文本
        text_blocks = re.findall(r'>([^<]{10,})<', html)
        current_section = None
        for block in text_blocks:
            block = block.strip()
            if not block:
                continue
            # 检测区域标题
            if '主队优势' in block or '主场优势' in block:
                current_section = 'home_strengths'
            elif '主队劣势' in block or '主场劣势' in block:
                current_section = 'home_weaknesses'
            elif '客队优势' in block or '客场优势' in block:
                current_section = 'away_strengths'
            elif '客队劣势' in block or '客场劣势' in block:
                current_section = 'away_weaknesses'
            elif current_section and len(block) > 15:
                # 这是一个SWOT条目
                swot[current_section].append(block)

    except Exception as e:
        print(f"  解析错误: {e}")

    return swot


def parse_swot_from_json(data, swot, url):
    """从JSON数据中解析SWOT"""
    try:
        # 尝试不同的JSON路径
        paths = [
            ['swot', 'home', 'strengths'],
            ['data', 'swot', 'home', 'strengths'],
            ['match', 'swot', 'home', 'strengths'],
        ]
        # ... 具体路径需要看实际JSON结构
    except:
        pass
    return swot


def fetch_swot_batch(swot_urls, match_keys=None):
    """批量获取SWOT数据

    Args:
        swot_urls: SWOT页面URL列表
        match_keys: 对应的比赛key列表 (可选)

    Returns:
        dict: {match_key or url: swot_data}
    """
    session = requests.Session()
    results = {}
    waf_solved = False
    start_time = time.time()

    print(f"SWOT批量获取: {len(swot_urls)}个页面")
    print(f"URL列表:")
    for i, url in enumerate(swot_urls):
        key = match_keys[i] if match_keys else f"url_{i}"
        print(f"  {key}: {url}")

    # 第一步: 获取第一个页面来解决WAF挑战
    print(f"\n[1/3] 解决WAF挑战...")
    first_url = swot_urls[0]
    html, status = fetch_with_waf_bypass(first_url, session)

    if status == 'captcha_required':
        print("  ⚠️ WAF需要滑块验证, 尝试备用方案...")
        # 尝试不带cookie直接访问 (有时WAF不会每次都触发)
        session2 = requests.Session()
        session2.headers.update(HEADERS)
        html, status = fetch_with_waf_bypass(first_url, session2)

    if status == 'ok':
        waf_solved = True
        print(f"  ✅ WAF已解决 (cookie已缓存)")
        # 解析第一个页面
        first_key = match_keys[0] if match_keys else f"url_0"
        swot_data = parse_swot_data(html, first_url)
        results[first_key] = swot_data
        print(f"  ✅ {first_key}: 已获取")
    else:
        print(f"  ❌ WAF解决失败: {status}")
        # 尝试使用cloudscraper作为后备
        print("  尝试使用cloudscraper后备方案...")
        import cloudscraper
        scraper = cloudscraper.create_scraper()
        resp = scraper.get(first_url, timeout=15)
        if 'arg1=' in resp.text:
            cookie_val = solve_waf_challenge(resp.text)
            if cookie_val:
                session.cookies.set('acw_sc__v2', cookie_val, domain='.leisu.com', path='/')
                waf_solved = True
                print(f"  ✅ WAF已解决 (cloudscraper后备)")
            else:
                print(f"  ❌ cloudscraper也无法解决WAF")
        else:
            print(f"  ⚠️ cloudscraper返回非WAF页面, 状态={resp.status_code}")

    # 第二步: 批量获取剩余页面 (复用cookie)
    print(f"\n[2/3] 批量获取剩余页面...")
    remaining_urls = swot_urls[1:] if waf_solved else swot_urls
    remaining_keys = match_keys[1:] if match_keys else [f"url_{i}" for i in range(1, len(swot_urls))]

    for url, key in zip(remaining_urls, remaining_keys):
        try:
            html, status = fetch_with_waf_bypass(url, session)
            if status == 'ok':
                swot_data = parse_swot_data(html, url)
                results[key] = swot_data
                print(f"  ✅ {key}: 已获取 ({len(swot_data['home_strengths'])+len(swot_data['home_weaknesses'])+len(swot_data['away_strengths'])+len(swot_data['away_weaknesses'])}条)")
            elif status == 'captcha_required':
                print(f"  ⚠️ {key}: 需要验证码, 跳过")
                results[key] = {'home_strengths': [], 'home_weaknesses': [], 'away_strengths': [], 'away_weaknesses': [], 'trend': {}, 'swot_url': url, 'error': 'captcha'}
            else:
                print(f"  ❌ {key}: 获取失败 ({status})")
                results[key] = {'home_strengths': [], 'home_weaknesses': [], 'away_strengths': [], 'away_weaknesses': [], 'trend': {}, 'swot_url': url, 'error': status}
        except Exception as e:
            print(f"  ❌ {key}: 异常 {e}")
            results[key] = {'home_strengths': [], 'home_weaknesses': [], 'away_strengths': [], 'away_weaknesses': [], 'trend': {}, 'swot_url': url, 'error': str(e)}

    elapsed = time.time() - start_time

    # 第三步: 保存结果
    print(f"\n[3/3] 保存结果...")
    output = {
        'fetched_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_urls': len(swot_urls),
        'success_count': sum(1 for v in results.values() if 'error' not in v),
        'elapsed_seconds': round(elapsed, 1),
        'matches': results,
    }

    with open(SWOT_OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"SWOT批量获取完成")
    print(f"  成功: {output['success_count']}/{len(swot_urls)}")
    print(f"  耗时: {elapsed:.1f}秒")
    print(f"  输出: {SWOT_OUTPUT}")
    print(f"{'='*60}")

    return results


# 测试用SWOT URL列表
TEST_SWOT_URLS = [
    "https://www.leisu.com/guide/swot-4467105",
    "https://www.leisu.com/guide/swot-4468154",
    "https://www.leisu.com/guide/swot-4467612",
    "https://www.leisu.com/guide/swot-4468155",
    "https://www.leisu.com/guide/swot-4467108",
    "https://www.leisu.com/guide/swot-4465718",
    "https://www.leisu.com/guide/swot-4465753",
    "https://www.leisu.com/guide/swot-4460389",
    "https://www.leisu.com/guide/swot-4460394",
]


if __name__ == '__main__':
    results = fetch_swot_batch(TEST_SWOT_URLS)

    # 打印解析结果
    print(f"\n--- 解析结果预览 ---")
    for key, data in results.items():
        hs = len(data.get('home_strengths', []))
        hw = len(data.get('home_weaknesses', []))
        as_ = len(data.get('away_strengths', []))
        aw = len(data.get('away_weaknesses', []))
        err = data.get('error', '')
        print(f"  {key}: 主优{hs}/主劣{hw}/客优{as_}/客劣{aw} {'错误:'+err if err else ''}")
