#!/usr/bin/env python3
"""同步前端静态文件到 Workers Assets 目录。

将仓库根目录的 index.html、品牌图标（favicon.ico / favicon-*.png /
apple-touch-icon.png / android-chrome-*.png）与 manifest.json
复制到 assets/（wrangler.jsonc 的 Workers Assets 目录），并输出差异说明；
plugins/ 目录整体镜像到 assets/plugins/（多出的目标文件会被删除）。
Workers 部署下这些文件由 Workers Assets 按文件名直接服务，不经过 Python 路由；
本地部署仍由 FastAPI 从仓库根目录读取原文件。

用法：
  python scripts/sync-frontend.py            # 复制（默认）
  python scripts/sync-frontend.py --check    # 仅检查差异，不复制；有差异时退出码 1
"""
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
PLUGINS = ROOT / "plugins"

# 需要同步到 assets 的文件：源路径 -> 目标相对路径
FILES = {
    ROOT / "index.html": "index.html",
    ROOT / "_headers": "_headers",
    ROOT / "favicon.ico": "favicon.ico",
    ROOT / "favicon-16x16.png": "favicon-16x16.png",
    ROOT / "favicon-32x32.png": "favicon-32x32.png",
    ROOT / "apple-touch-icon.png": "apple-touch-icon.png",
    ROOT / "android-chrome-192x192.png": "android-chrome-192x192.png",
    ROOT / "android-chrome-512x512.png": "android-chrome-512x512.png",
    ROOT / "manifest.json": "manifest.json",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sync_tree(check_only: bool) -> bool:
    """镜像 plugins/ -> assets/plugins/，返回是否有差异。"""
    changed = False
    src_files = sorted(p for p in PLUGINS.rglob("*") if p.is_file())
    dst_root = ASSETS / "plugins"
    for src in src_files:
        dst = dst_root / src.relative_to(PLUGINS)
        if dst.is_file() and _sha256(src) == _sha256(dst):
            print(f"OK   plugins/{src.relative_to(PLUGINS).as_posix()}: 已同步")
            continue
        print(
            f"{'DIFF' if check_only else 'SYNC'} plugins/{src.relative_to(PLUGINS).as_posix()}"
        )
        changed = True
        if not check_only:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    if dst_root.is_dir():
        for dst in sorted(dst_root.rglob("*"), reverse=True):
            if not dst.is_file():
                continue
            src = PLUGINS / dst.relative_to(dst_root)
            if src.is_file():
                continue
            print(f"{'DIFF' if check_only else 'SYNC'} 删除多余 plugins/{dst.relative_to(dst_root).as_posix()}")
            changed = True
            if not check_only:
                dst.unlink()
    return changed


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
    if PLUGINS.is_dir():
        changed = _sync_tree(check_only) or changed
    if changed and check_only:
        print("有文件待同步，请先运行 python scripts/sync-frontend.py")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())