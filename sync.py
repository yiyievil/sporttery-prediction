#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键安全同步 — git pull --ff-only (只快进, 绝不覆盖本地未提交改动)

用法:
  python sync.py             # 同步到远端最新
  python sync.py --check     # 只检查有无更新(不拉取)

特点:
  - --ff-only: 本地有未提交改动会被安全拦截, 不会丢任何东西
  - 自动定位 git (Windows 常见安装路径 / PATH)
  - 无网络 / 无上游时给出清晰提示, 返回非0退出码
"""
import os
import shutil
import subprocess
import sys

# Windows 控制台 GBK 编码兼容: 输出含 emoji, 强制 UTF-8 (失败则忽略)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')
    except Exception:
        pass


def find_git():
    """定位 git 可执行文件 (Windows 下可能不在 PATH)"""
    candidates = ['git'] + [
        r'C:\Program Files\Git\cmd\git.exe',
        r'C:\Program Files\Git\bin\git.exe',
        r'C:\Program Files (x86)\Git\cmd\git.exe',
        os.path.expanduser(r'~\AppData\Local\Programs\Git\cmd\git.exe'),
    ]
    for p in candidates:
        if p == 'git':
            if shutil.which('git'):
                return 'git'
        elif os.path.isfile(p):
            return p
    return 'git'


def _run(git, args):
    return subprocess.run([git] + args, capture_output=True, text=True)


def sync(check_only=False):
    """同步到远端最新。返回退出码: 0=已最新/已更新, 非0=失败(未做任何覆盖)"""
    git = find_git()
    repo = os.path.dirname(os.path.abspath(__file__))

    # 1) 先 fetch (轻量, 不触碰工作区)
    r = _run(git, ['-C', repo, 'fetch', 'origin'])
    if r.returncode != 0:
        print('[sync] ⚠️ fetch 失败 (可能无网络):')
        print((r.stderr or '').strip()[-300:])
        return 2

    # 2) 比较本地 HEAD 与远端
    r = _run(git, ['-C', repo, 'rev-parse', 'HEAD', 'origin/master'])
    if r.returncode != 0:
        print('[sync] ⚠️ 无法读取分支状态 (可能无远端分支):')
        print((r.stderr or '').strip()[-200:])
        return 2
    shas = (r.stdout or '').split()
    if len(shas) < 2:
        print('[sync] ⚠️ 无法读取分支状态')
        return 2
    local, remote = shas[0], shas[1]

    if local == remote:
        print('[sync] ✅ 已是最新 (无需操作)')
        return 0

    if check_only:
        ahead_behind = _run(git, ['-C', repo, 'rev-list', '--left-right', '--count',
                                  'HEAD...origin/master'])
        print(f'[sync] 有更新可拉取 (本地 {local[:7]} → 远端 {remote[:7]})')
        if ahead_behind.returncode == 0:
            print(f'[sync] {ahead_behind.stdout.strip()}')
        return 0

    # 3) 快进拉取 (只快进, 安全)
    r = _run(git, ['-C', repo, 'pull', '--ff-only', 'origin', 'master'])
    if r.returncode == 0:
        print('[sync] ✅ 已同步到最新')
        out = (r.stdout or '').strip()
        if out:
            # 打印关键行(Updating / Fast-forward / Already up to date)
            for line in out.splitlines():
                if any(k in line for k in ('Updating', 'Fast-forward', 'Already')):
                    print(f'[sync]   {line.strip()}')
        return 0
    print('[sync] ⚠️ 同步失败 (本地有未提交改动或远端分叉), 未做任何覆盖:')
    print((r.stderr or '').strip()[-500:])
    return 1


def main():
    check_only = '--check' in sys.argv
    return sync(check_only=check_only)


if __name__ == '__main__':
    sys.exit(main())
