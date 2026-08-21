#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性迁移 (改进#6, 2026-08-21): nowscore_fetch.py 硬编码 TEAM_NAME_ALIASES → team_names_db.json

背景: 队名别名曾三路并存 — nowscore_fetch.py 硬编码 dict (57-311行) /
      team_names_db.json 统一库 / match_utils 自学习。本脚本把硬编码 dict
      以 source='nowscore' 合并进统一库, 之后 nowscore_fetch 改为从
      team_names.aliases_of() 读取, 单一来源。

用法: python scripts/migrate_nowscore_aliases.py [--dry-run]
幂等: add_aliases_batch 对已有别名去重, 重复运行安全。
"""
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import team_names  # noqa: E402

NOWSCORE_FETCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'nowscore_fetch.py')


def extract_hardcoded_aliases(path):
    """从 nowscore_fetch.py 源码中用 AST 提取 TEAM_NAME_ALIASES 字面量 dict"""
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == 'TEAM_NAME_ALIASES':
                    return ast.literal_eval(node.value)
    raise RuntimeError('TEAM_NAME_ALIASES 未找到 (可能已完成迁移)')


def main():
    dry = '--dry-run' in sys.argv
    aliases = extract_hardcoded_aliases(NOWSCORE_FETCH)
    pairs = []
    for canon, alias_list in aliases.items():
        for a in alias_list:
            if a != canon:  # add_alias 拒绝 canon==alias, 标准名本身无需入库为别名
                pairs.append((canon, a, 'nowscore'))
    print(f'硬编码表: {len(aliases)} 个标准名, {len(pairs)} 条待迁移别名')
    if dry:
        print('[dry-run] 不写入')
        return
    added = team_names.add_aliases_batch(pairs)
    print(f'迁移完成: 新增 {added} 条 (其余已在库中, 幂等跳过)')


if __name__ == '__main__':
    main()
