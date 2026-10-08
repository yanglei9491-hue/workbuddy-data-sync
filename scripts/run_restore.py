#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 账号数据恢复 —— 跨平台启动器
============================================
供 macOS / Linux 用户使用（Windows 用户也可用，但 .bat 更方便）。

用法：
    python run_restore.py              # 查看账号分布 + 交互式恢复
    python run_restore.py detect       # 只看账号分布
    python run_restore.py restore      # 交互式恢复
    python run_restore.py rollback     # 回滚
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

# 定位脚本所在目录（跨平台，不依赖 cwd）
SCRIPT_DIR = Path(__file__).resolve().parent
NEXT = SCRIPT_DIR / "wb_account_restore.py"

if not NEXT.exists():
    print(f"[错误] 找不到核心脚本: {NEXT}")
    sys.exit(1)

# 自动探测 Python 解释器：优先 python3（macOS/Linux 常见），
# 否则回退 python（Windows 常见）；两者皆无 → 明确中文报错。
PYTHON = shutil.which("python3") or shutil.which("python")
if not PYTHON:
    print("[错误] 未检测到 Python 解释器（python3 / python 均不在 PATH 中）。"
          "请先安装 Python 3.8+，并确保可在终端中直接调用 python3 或 python。")
    sys.exit(1)

# 透传命令与参数（默认进引导）
args = sys.argv[1:]
cmd = [PYTHON, str(NEXT)] + (args if args else ["guide"])

sys.exit(subprocess.call(cmd))
