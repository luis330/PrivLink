#!/usr/bin/env python3
"""按网络环境选择依赖源安装，锁文件始终保持官方 PyPI。

uv 会把索引地址写进 uv.lock：CI 与镜像构建跑在境外 runner 上，锁里一旦是境内镜像就会
超时；而国内直连 pypi.org 往往很慢。本脚本只在本地安装期间临时把锁指向镜像，装完立即
还原，避免镜像地址被提交进仓库（tests/test_lock_sources.py 会拦截这类提交）。

用法：
  python scripts/uv-sync.py                     # 官方源（等价于 uv sync --frozen）
  python scripts/uv-sync.py --mirror cn         # 清华
  python scripts/uv-sync.py --mirror cn-aliyun  # 阿里云
  python scripts/uv-sync.py --mirror cn-ustc    # 中科大
  python scripts/uv-sync.py --mirror cn -- --no-dev   # -- 之后的参数透传给 uv sync
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = ROOT / "uv.lock"

MIRRORS = {
    "cn": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "cn-aliyun": "https://mirrors.aliyun.com/pypi/simple",
    "cn-ustc": "https://mirrors.ustc.edu.cn/pypi/simple",
}


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def lock_is_dirty() -> bool:
    """锁文件有未提交改动时拒绝运行，否则还原会把别人的修改冲掉。"""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", "uv.lock"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return bool(result.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="按网络环境选择依赖源安装（锁文件保持官方源）")
    parser.add_argument(
        "--mirror", default="official", choices=["official", *MIRRORS],
        help="official=官方 PyPI（默认）；cn / cn-aliyun / cn-ustc 走境内镜像",
    )
    parser.add_argument(
        "uv_args", nargs="*", metavar="--",
        help="透传给 uv sync 的参数，例如 -- --no-dev",
    )
    args = parser.parse_args()

    if shutil.which("uv") is None:
        print("未找到 uv，请先安装：https://docs.astral.sh/uv/", file=sys.stderr)
        return 1
    if not LOCK_FILE.is_file():
        print("未找到 uv.lock，请先在项目根目录运行 uv lock", file=sys.stderr)
        return 1
    if args.mirror != "official" and lock_is_dirty():
        print("uv.lock 有未提交改动，请先提交或还原后再运行（避免还原时覆盖你的修改）", file=sys.stderr)
        return 1

    original = LOCK_FILE.read_bytes()
    url = MIRRORS.get(args.mirror)
    try:
        if url:
            print(f"临时切换到镜像源：{url}")
            run(["uv", "lock", "--default-index", url])
        run(["uv", "sync", "--frozen", *args.uv_args])
    finally:
        if url and LOCK_FILE.read_bytes() != original:
            LOCK_FILE.write_bytes(original)
            print("已还原 uv.lock：镜像地址不会留在仓库里")
    return 0


if __name__ == "__main__":
    sys.exit(main())
