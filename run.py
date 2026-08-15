#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键执行器: 先自动 git 同步, 再运行目标脚本 (省去每个客户端手动 pull)

用法:
  python run.py sync                           # 仅同步
  python run.py verify 260814                  # 同步 + 验证
  python run.py predict 260815 001,002         # 同步 + 预测
  python run.py update 2026-08-15 001,002      # 同步 + 更新
  python run.py simulate pred_xxx.json         # 同步 + 模拟投注
  python run.py guide [pred.json]              # 同步 + 生成投注指南
  python run.py drift / recalibrate / cusum    # 同步 + 漂移/重标定/复检
  python run.py teamnames add <标准名> <别名> <来源>  # 同步 + 维护队名库
  python run.py -- <任意脚本> <参数...>         # 同步 + 任意命令

说明:
  - 同步用 --ff-only 快进, 本地有未提交改动/远端分叉时安全跳过执行并提示
  - 任何客户端只跑 `python run.py <命令>`, 无需手动 git pull
"""
import os
import subprocess
import sys

# Windows 控制台 GBK 编码兼容: 输出含 emoji, 强制 UTF-8 (失败则忽略)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')
    except Exception:
        pass

import sync as _sync

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 命令 → 脚本映射
COMMANDS = {
    'verify': 'v215_verify.py',
    'predict': 'v215_e2e.py',
    'update': 'v215_update.py',
    'simulate': 'v215_simulate.py',
    'guide': 'gen_bet_guide_html.py',
    'drift': 'gen_drift_state.py',
    'recalibrate': 'recalibrate_model.py',
    'cusum': 'cusum_recheck.py',
    'teamnames': 'team_names.py',
}


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0

    # 1) 先同步
    rc = _sync.sync()
    if rc != 0:
        print('[run] ⚠️ 同步未完成, 跳过执行 (避免在过期代码上操作)')
        return rc

    # 2) 解析命令
    first = args[0]
    if first == 'sync':
        return 0  # 已同步完成

    if first == '--':
        # 任意命令透传: python run.py -- gen_pred_pdf.py x.json out.pdf
        cmd = args[1:]
        if not cmd:
            print('[run] 缺少命令')
            return 2
    elif first in COMMANDS:
        script = os.path.join(SCRIPT_DIR, COMMANDS[first])
        cmd = [sys.executable, script] + args[1:]
    else:
        # 未映射: 当作脚本名透传
        script = os.path.join(SCRIPT_DIR, first)
        if os.path.isfile(script):
            cmd = [sys.executable, script] + args[1:]
        else:
            print(f'[run] 未知命令: {first}')
            print(__doc__)
            return 2

    print(f'[run] $ {" ".join(cmd)}', flush=True)
    r = subprocess.run(cmd, cwd=SCRIPT_DIR)
    return r.returncode


if __name__ == '__main__':
    sys.exit(main())
