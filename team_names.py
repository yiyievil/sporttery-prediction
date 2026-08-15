#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一队名对应数据库 (Ultra 13.6) — 以 sporttery 中文译名为准

目标: 不同网站(nowscore / 500.com / leisu / qiumiwu)队名译名不一, 统一以
      sporttery 中文译名为标准名, 建立跨站别名库, 实现又快又准的匹配。

数据: predictions/team_names_db.json
  结构:
    {
      "_meta": {"canonical": "sporttery", "version": 1, "updated": "..."},
      "teams": {
        "<sporttery标准名>": {
          "aliases": ["所有别名(跨站)"],
          "sources": {"nowscore": [...], "500": [...], "leisu": [...],
                      "qiumiwu": [...], "en": [...], "short": [...]}
        }
      }
    }

匹配策略 (又快又准):
  1. 精确命中 (快): 启动时构建反向索引 alias → 标准名, O(1) 判定同队/异队
  2. 模糊兜底 (准): SequenceMatcher(译名差异) + 字符重叠(简称/全称), 仅精确未命中时
  3. 歧义保护: 同一别名映射多个标准名时(如"水原")不进反向索引, 交给模糊+上下文

维护: team_names.add_alias(标准名, 别名, source='leisu') 或命令行
      python team_names.py add <标准名> <别名> <source>
"""
import json
import os
import re
import sys
from difflib import SequenceMatcher

WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(WORKSPACE, 'predictions', 'team_names_db.json')

_SOURCES = ('nowscore', '500', 'leisu', 'qiumiwu', 'en', 'short')

_cache = {
    'loaded': False,
    'teams': {},       # 标准名 → {'aliases': [...], 'sources': {...}}
    'reverse': {},     # 别名 → 标准名 (不含歧义别名)
    'ambiguous': set(),# 歧义别名 (一个别名映射多个标准名)
    'canonicals': set(),  # 标准名集合
}


def _is_chinese(s):
    return any('\u4e00' <= c <= '\u9fff' for c in str(s))


def _load(force=False):
    """惰性加载数据库 + 构建反向索引"""
    if _cache['loaded'] and not force:
        return
    _cache['loaded'] = True
    _cache['teams'] = {}
    _cache['reverse'] = {}
    _cache['ambiguous'] = set()
    _cache['canonicals'] = set()

    if not os.path.exists(DB_PATH):
        return
    try:
        with open(DB_PATH, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return

    teams = data.get('teams', {})
    _cache['teams'] = teams

    alias_to_canons = {}
    for canon, info in teams.items():
        canon = str(canon).strip()
        if not canon:
            continue
        _cache['canonicals'].add(canon)
        alias_to_canons.setdefault(canon, set()).add(canon)
        for a in (info.get('aliases') or []):
            a = str(a).strip()
            if a:
                alias_to_canons.setdefault(a, set()).add(canon)

    for alias, canons in alias_to_canons.items():
        if len(canons) == 1:
            _cache['reverse'][alias] = next(iter(canons))
        else:
            _cache['ambiguous'].add(alias)


def canonicalize(name):
    """返回标准名(sporttery); 不在库中或歧义时返回 None"""
    if not name:
        return None
    name = str(name).strip()
    _load()
    if name in _cache['canonicals']:
        return name
    if name in _cache['ambiguous']:
        return None
    return _cache['reverse'].get(name)


def is_known(name):
    """该名称(标准名或别名)是否在库中"""
    return canonicalize(name) is not None


def aliases_of(name):
    """返回某队名的所有别名(含标准名), 未知则返回 [name]"""
    canon = canonicalize(name) or str(name).strip()
    _load()
    info = _cache['teams'].get(canon)
    if info:
        return [canon] + list(info.get('aliases') or [])
    return [canon]


def team_similarity(a, b):
    """精确判定两队名是否同队:
       同队(精确命中) → 1.0
       明确异队(两者都在库且不同) → 0.0
       无法精确判定(至少一方不在库或歧义) → None (交模糊)
    """
    if not a or not b:
        return None
    ca = canonicalize(a)
    cb = canonicalize(b)
    if ca is not None and cb is not None:
        return 1.0 if ca == cb else 0.0
    return None


def fuzzy_similarity(a, b):
    """模糊兜底 (0~1): SequenceMatcher(译名差异) 与字符重叠(简称/全称) 取较高者"""
    if not a or not b:
        return 0.0
    a, b = str(a).strip(), str(b).strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # 1) SequenceMatcher: 对 埃夫斯堡/埃尔夫斯堡 这类译名差异有效 (0.89)
    seq = SequenceMatcher(None, a, b).ratio()
    # 2) 字符重叠: 对 金泉/金泉尚武 这类简称/全称有效
    set_a, set_b = set(a), set(b)
    overlap = len(set_a & set_b) / max(1, min(len(set_a), len(set_b))) if set_a and set_b else 0.0
    return round(max(seq, overlap * 0.9), 4)


def add_alias(canon, alias, source='leisu'):
    """添加/更新一条别名并持久化。返回 True=成功, False=标准名已存在但来源不合法等"""
    canon = str(canon).strip()
    alias = str(alias).strip()
    source = str(source).strip() or 'short'
    if not canon or not alias or canon == alias:
        return False
    if source not in _SOURCES:
        source = 'short'
    _load()
    teams = _cache['teams']
    info = teams.setdefault(canon, {'aliases': [], 'sources': {}})
    if alias not in info['aliases']:
        info['aliases'].append(alias)
    src = info.setdefault('sources', {}).setdefault(source, [])
    if alias not in src:
        src.append(alias)
    _save()
    _load(force=True)
    return True


def remove_alias(canon, alias):
    """删除一条别名并持久化; 若该标准名已无任何别名, 一并删除该标准名条目"""
    canon = str(canon).strip()
    alias = str(alias).strip()
    _load()
    info = _cache['teams'].get(canon)
    if not info:
        return False
    if alias in info.get('aliases', []):
        info['aliases'].remove(alias)
    for src in info.get('sources', {}).values():
        if alias in src:
            src.remove(alias)
    # 清理空的标准名条目
    has_any = bool(info.get('aliases')) or any(v for v in info.get('sources', {}).values())
    if not has_any:
        _cache['teams'].pop(canon, None)
    _save()
    _load(force=True)
    return True


def add_aliases_batch(pairs):
    """批量添加 [(标准名, 别名, source), ...], 一次性持久化, 返回新增条数"""
    _load()
    added = 0
    for canon, alias, source in pairs:
        canon = str(canon).strip()
        alias = str(alias).strip()
        source = str(source).strip() or 'leisu'
        if not canon or not alias or canon == alias:
            continue
        if source not in _SOURCES:
            source = 'short'
        info = _cache['teams'].setdefault(canon, {'aliases': [], 'sources': {}})
        if alias not in info['aliases']:
            info['aliases'].append(alias)
            added += 1
        src = info.setdefault('sources', {}).setdefault(source, [])
        if alias not in src:
            src.append(alias)
    if added:
        _save()
        _load(force=True)
    return added


def _save():
    _load()
    data = {
        '_meta': {
            'canonical': 'sporttery',
            'version': 1,
            'updated': __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        },
        'teams': _cache['teams'],
    }
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with open(DB_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def migrate_from_existing():
    """迁移现有分散别名到统一库:
      1. nowscore_fetch.TEAM_NAME_ALIASES → source 'nowscore' (英文别名归 'en')
      2. predictions/team_alias.json → source 'short'
      3. src.config.TEAM_NAME_MAP (中文→英文) → source 'en'
    返回迁移统计 dict
    """
    stats = {'nowscore': 0, 'team_alias': 0, 'en_map': 0, 'teams': 0}

    # 1. nowscore 别名表
    try:
        import nowscore_fetch
        for canon, aliases in (nowscore_fetch.TEAM_NAME_ALIASES or {}).items():
            if canon not in _cache['teams']:
                _cache['teams'][canon] = {'aliases': [], 'sources': {}}
            info = _cache['teams'][canon]
            for a in aliases:
                if a == canon:
                    continue
                src = 'en' if (not _is_chinese(a)) else 'nowscore'
                if a not in info['aliases']:
                    info['aliases'].append(a)
                info.setdefault('sources', {}).setdefault(src, [])
                if a not in info['sources'][src]:
                    info['sources'][src].append(a)
                stats['nowscore'] += 1
    except Exception as e:
        print(f'  [迁移] nowscore 别名表失败: {e}')

    # 2. team_alias.json
    try:
        _tp = os.path.join(WORKSPACE, 'predictions', 'team_alias.json')
        if os.path.exists(_tp):
            with open(_tp, encoding='utf-8') as f:
                ta = json.load(f)
            for canon, aliases in ta.items():
                if canon not in _cache['teams']:
                    _cache['teams'][canon] = {'aliases': [], 'sources': {}}
                info = _cache['teams'][canon]
                for a in aliases:
                    if a == canon or a in info['aliases']:
                        continue
                    info['aliases'].append(a)
                    info.setdefault('sources', {}).setdefault('short', []).append(a)
                    stats['team_alias'] += 1
    except Exception as e:
        print(f'  [迁移] team_alias.json 失败: {e}')

    # 3. src.config.TEAM_NAME_MAP (中文→英文)
    try:
        from src.config import TEAM_NAME_MAP
        for cn, en in (TEAM_NAME_MAP or {}).items():
            if cn not in _cache['teams']:
                _cache['teams'][cn] = {'aliases': [], 'sources': {}}
            info = _cache['teams'][cn]
            if en not in info['aliases']:
                info['aliases'].append(en)
            info.setdefault('sources', {}).setdefault('en', []).append(en)
            stats['en_map'] += 1
    except Exception as e:
        print(f'  [迁移] TEAM_NAME_MAP 失败: {e}')

    stats['teams'] = len(_cache['teams'])
    _save()
    _load(force=True)
    return stats


def stats():
    """返回库统计"""
    _load()
    n_alias = sum(len(v.get('aliases', [])) for v in _cache['teams'].values())
    return {
        'teams': len(_cache['teams']),
        'aliases': n_alias,
        'reverse_keys': len(_cache['reverse']),
        'ambiguous': len(_cache['ambiguous']),
    }


if __name__ == '__main__':
    _load()
    if len(sys.argv) >= 2 and sys.argv[1] == 'migrate':
        print('[队名库] 迁移现有别名...')
        s = migrate_from_existing()
        print(f'[队名库] 迁移完成: {s}')
    elif len(sys.argv) >= 2 and sys.argv[1] == 'add':
        if len(sys.argv) < 4:
            print('用法: python team_names.py add <标准名> <别名> [source=leisu]')
            sys.exit(1)
        canon, alias = sys.argv[2], sys.argv[3]
        src = sys.argv[4] if len(sys.argv) >= 5 else 'leisu'
        ok = add_alias(canon, alias, src)
        print(f'[队名库] {"已添加" if ok else "添加失败"}: {canon} ← {alias} ({src})')
    elif len(sys.argv) >= 2 and sys.argv[1] == 'stats':
        print(f'[队名库] {stats()}')
    elif len(sys.argv) >= 3 and sys.argv[1] == 'lookup':
        name = sys.argv[2]
        print(f'[队名库] {name!r} → 标准名 {canonicalize(name)!r}')
    else:
        print('[队名库] 用法: migrate | add <标准名> <别名> [source] | lookup <名> | stats')
        print(f'[队名库] 当前: {stats()}')
