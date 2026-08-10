#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
version_archive.py — 预测版本全量归档模块 (Ultra 11.20)

背景: 260809周日 001-024 曾因 predict 模式重跑(001-011)覆盖导致 24 场丢失,
      history 只存元数据不存完整 results → 旧版本无法恢复。

机制:
  1. 全量归档: 每次保存预测文件前, 把当前文件完整 {meta, results, cache} 快照
     归档为 archive/versions/pred_{name}__v{N}.json (N 为单调递增序号)
  2. 完整性标记: 每个版本记录 match_keys 覆盖集合 + is_complete 标记
     (完整版 = 覆盖场次 >= 该基名历史最大覆盖, 或达到 expected_keys)
  3. 验证锚定: 提供 find_last_complete 定位"最后一个完整版", 供验证脚本只用它比对

文件结构:
  predictions/archive/versions/manifest.json  — 版本清单(每个基名→版本列表)
  predictions/archive/versions/pred_{name}__v{N}.json — 各版本完整快照

用法 (在 v215_e2e.py / v215_update.py 写文件前调用):
    from version_archive import archive_before_save
    archive_before_save(pred_file, pred_data, expected_keys=None)
"""
import os, json, glob, re, time
from datetime import datetime, timezone, timedelta

_BEIJING_TZ = timezone(timedelta(hours=8))
def _bjnow():
    return datetime.now(_BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')

# 归档根目录 (与 predictions_dir 同级下的 archive/versions)
def _versions_dir(predictions_dir):
    d = os.path.join(predictions_dir, 'archive', 'versions')
    os.makedirs(d, exist_ok=True)
    return d

def _manifest_path(predictions_dir):
    return os.path.join(_versions_dir(predictions_dir), 'manifest.json')

def _load_manifest(predictions_dir):
    p = _manifest_path(predictions_dir)
    if os.path.exists(p):
        try:
            with open(p, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_manifest(predictions_dir, manifest):
    p = _manifest_path(predictions_dir)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)

def archive_before_save(pred_file, pred_data, expected_keys=None):
    """在覆盖写出 pred_file 之前, 把当前文件内容归档为一个版本快照。

    参数:
      pred_file:   即将被覆盖的预测文件路径 (若不存在则跳过)
      pred_data:   即将写入的新数据 (用于记录下一版本信息)
      expected_keys: 该批次期望覆盖的完整场次集合(如 24 场编号), 用于判断完整性
    """
    predictions_dir = os.path.dirname(pred_file)
    base = os.path.basename(pred_file).replace('.json', '')

    # 读取当前磁盘上的旧版本 (若存在)
    old_data = None
    if os.path.exists(pred_file):
        try:
            with open(pred_file, 'r', encoding='utf-8') as f:
                old_data = json.load(f)
        except Exception:
            old_data = None

    if old_data is None:
        # 无旧文件: 首次归档仅登记 manifest 基名, 不生成快照
        manifest = _load_manifest(predictions_dir)
        if base not in manifest:
            manifest[base] = {'versions': [], 'latest_seq': 0}
            _save_manifest(predictions_dir, manifest)
        return

    # 计算本版本(旧数据)的场次覆盖
    old_results = old_data.get('results', {}) or {}
    old_keys = sorted(old_results.keys())

    # 读取 manifest, 确定下一序号
    manifest = _load_manifest(predictions_dir)
    entry = manifest.get(base, {'versions': [], 'latest_seq': 0})
    seq = entry.get('latest_seq', 0) + 1

    # 完整性判据 (Ultra 11.20 修正): 必须同时满足"达到本批次期望覆盖" 且
    # "覆盖场次 >= 历史该基名最大覆盖"。仅用 expected_keys 会把部分重跑(如只
    # 重跑11场、expected只传11场)误判为完整, 覆盖掉真正完整的24场版本。
    # → 用 max(历史最大覆盖, 期望覆盖) 作为完整下限, 保证部分版本永远追不上完整版。
    expected_set = set(expected_keys) if expected_keys else None
    hist_max = entry.get('max_covered', 0)
    # 完整下限 = 历史最大覆盖 与 本批次期望覆盖 中的较大者
    lower_bound = hist_max
    if expected_set is not None:
        covered = [k for k in old_keys if k in expected_set or k[-3:] in expected_set]
        exp_n = len(expected_set)
        lower_bound = max(lower_bound, exp_n)
        covered_n = len(covered)
    else:
        covered_n = len(old_keys)
    is_complete = covered_n >= lower_bound and covered_n > 0

    # 记录版本快照 (完整 meta/results/cache)
    version = {
        'seq': seq,
        'base': base,
        'archived_at': _bjnow(),
        'mode': old_data.get('mode', 'unknown'),
        'update_count': old_data.get('update_count', 0),
        'saved_at': old_data.get('saved_at', ''),
        'match_count': len(old_keys),
        'match_keys': old_keys,
        'is_complete': is_complete,
        'expected_keys': list(expected_set) if expected_set else None,
        'snapshot': {
            'meta': old_data.get('meta', {}),
            'results': old_data.get('results', {}),
            'cache': old_data.get('cache', {}),
        },
    }

    # 写版本快照文件
    vpath = os.path.join(_versions_dir(predictions_dir), f'{base}__v{seq}.json')
    tmp = vpath + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(version, f, ensure_ascii=False, indent=1)
    os.replace(tmp, vpath)

    # 更新 manifest
    entry['latest_seq'] = seq
    entry['max_covered'] = max(entry.get('max_covered', 0), len(old_keys))
    entry.setdefault('versions', []).append({
        'seq': seq, 'file': f'{base}__v{seq}.json', 'archived_at': version['archived_at'],
        'mode': version['mode'], 'update_count': version['update_count'],
        'match_count': version['match_count'], 'is_complete': version['is_complete'],
    })
    entry['last_complete_seq'] = seq if is_complete else entry.get('last_complete_seq', 0)
    manifest[base] = entry
    _save_manifest(predictions_dir, manifest)

    print(f"  [版本归档] v{seq} 已归档({len(old_keys)}场, 完整={is_complete}): {os.path.basename(vpath)}")
    return version


def find_last_complete(predictions_dir, base, expected_keys=None):
    """定位"最后一个完整版"的完整快照数据。

    返回: (version_dict, vfile_path) 或 (None, None)
    完整判据: manifest 中 last_complete_seq 指向的版本;
            若无记录, 回溯扫描所有版本文件取覆盖场次最多的。
    """
    manifest = _load_manifest(predictions_dir)
    entry = manifest.get(base)
    if not entry:
        return None, None

    # 优先用 manifest 记录的 last_complete_seq
    lc = entry.get('last_complete_seq')
    if lc:
        vfile = os.path.join(_versions_dir(predictions_dir), f'{base}__v{lc}.json')
        if os.path.exists(vfile):
            try:
                with open(vfile, 'r', encoding='utf-8') as f:
                    return json.load(f), vfile
            except Exception:
                pass

    # 回溯: 扫描所有版本文件, 取覆盖场次最多(且 >= expected)的
    best = None
    best_count = -1
    for vfile in sorted(glob.glob(os.path.join(_versions_dir(predictions_dir), f'{base}__v*.json'))):
        try:
            with open(vfile, 'r', encoding='utf-8') as f:
                v = json.load(f)
        except Exception:
            continue
        n = v.get('match_count', 0)
        if expected_keys:
            exp = set(expected_keys)
            covered = [k for k in v.get('match_keys', []) if k in exp or k[-3:] in exp]
            n = len(covered)
        if n > best_count:
            best_count = n
            best = (v, vfile)
    return best if best_count >= 0 else (None, None)


def list_versions(predictions_dir, base=None):
    """列出归档版本 (调试用)"""
    manifest = _load_manifest(predictions_dir)
    if base:
        return manifest.get(base, {})
    return manifest


if __name__ == '__main__':
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else '/workspace/sporttery/predictions'
    print(json.dumps(list_versions(d), ensure_ascii=False, indent=1))