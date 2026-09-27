#!/usr/bin/env python3
"""把 scripts/pywrangler-sync-patched.py 覆盖进当前 .venv 的 pywrangler。

背景：pywrangler 1.17.4 的 sync.py 需要两处修正才能在本仓库工作
（--target + --python 指向 Pyodide trampoline，绕开 pylock requires-python
与宿主 .venv 版本不匹配）。补丁只存在于 site-packages 内，任何
`uv sync` / `uv pip install workers-py` 重装都会还原，需在每次 sync 之后重打。
仅 Windows 需要；其他平台直接跳过。

用法（仓库根目录）：
    uv run python scripts/patch-pywrangler.py
"""
from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "scripts" / "pywrangler-sync-patched.py"
DST = ROOT / ".venv" / "Lib" / "site-packages" / "pywrangler" / "sync.py"


def main() -> int:
    if sys.platform != "win32":
        # 补丁针对 Windows（Scripts\python.exe trampoline）；Linux CI 以 Python 3.13
        # 建 .venv，pylock requires-python 校验本就通过，官方 sync.py 即可工作
        print("非 Windows 平台，无需补丁")
        return 0
    if not SRC.is_file():
        print(f"补丁源不存在: {SRC}", file=sys.stderr)
        return 1
    if not DST.is_file():
        print(f"目标不存在（.venv 未安装 workers-py?）: {DST}", file=sys.stderr)
        return 1
    if filecmp.cmp(SRC, DST, shallow=False):
        print("pywrangler sync.py 已是补丁版本，无需处理")
        return 0
    shutil.copyfile(SRC, DST)
    print(f"已打补丁: {DST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
