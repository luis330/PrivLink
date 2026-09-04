#!/usr/bin/env python3
"""同步前端与共享数据文件到 Cloudflare assets 目录。

将仓库根目录的 index.html、品牌图标（favicon.ico / favicon-*.png /
apple-touch-icon.png / android-chrome-*.png）、manifest.json 与 simple-icons.json
复制到 deploy/cloudflare/assets/（Workers Assets 服务目录），并输出差异说明。
图标与 manifest 由 Workers Assets 按文件名直接服务，TS 端无需显式路由。

用法：
  python scripts/sync-frontend.py            # 复制（默认）
  python scripts/sync-frontend.py --check    # 仅检查差异，不复制；有差异时退出码 1
"""
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "deploy" / "cloudflare" / "assets"

# 需要同步到 assets 的文件：源路径 -> 目标相对路径
FILES = {
    ROOT / "index.html": "index.html",
    ROOT / "favicon.ico": "favicon.ico",
    ROOT / "favicon-16x16.png": "favicon-16x16.png",
    ROOT / "favicon-32x32.png": "favicon-32x32.png",
    ROOT / "apple-touch-icon.png": "apple-touch-icon.png",
    ROOT / "android-chrome-192x192.png": "android-chrome-192x192.png",
    ROOT / "android-chrome-512x512.png": "android-chrome-512x512.png",
    ROOT / "manifest.json": "manifest.json",
    ROOT / "simple-icons.json": "simple-icons.json",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    check_only = "--check" in sys.argv
    changed = False
    for src, rel in FILES.items():
        dst = ASSETS / rel
        if not src.is_file():
            print(f"SKIP {rel}: 源文件不存在 {src}")
            continue
        if dst.is_file() and _sha256(src) == _sha256(dst):
            print(f"OK   {rel}: 已同步")
            continue
        print(f"{'DIFF' if check_only else 'SYNC'} {rel}: {src.name} -> {dst}")
        changed = True
        if not check_only:
            ASSETS.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    if changed and check_only:
        print("有文件待同步，请先运行 python scripts/sync-frontend.py")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())