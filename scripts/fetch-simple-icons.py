#!/usr/bin/env python3
"""拉取 Simple Icons 数据（含 slug），按允许清单过滤后写入共享数据源 + 内嵌模块。

共享数据源：仓库根目录 simple-icons.json（本地源码 / Docker 部署启动时读取）。
内嵌模块：src/privlink/data/simple_icons_data.py —— Workers bundle 只收 .py/.text
不收 JSON 文件，故生成 .py 模块供 import（wheel 安装同样读它）。

允许清单：scripts/simple-icons-allowlist.json（slug 数组）。存在时只保留清单内的
品牌——完整数据集 3400+ 条会撑大 Workers 内嵌模块与启动开销，实际用不到；想恢复
全量删除该文件重新生成本脚本即可。清单里不存在的 slug 会被静默丢弃。

用法：
    python scripts/fetch-simple-icons.py            # 从 unpkg 拉取（默认）
    python scripts/fetch-simple-icons.py --offline  # 用仓库根已有 json 重新生成
"""
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SRC = "https://unpkg.com/simple-icons@latest/icons.json"
DST_SHARED = ROOT / "simple-icons.json"
DST_MODULE = ROOT / "src" / "privlink" / "data" / "simple_icons_data.py"
ALLOWLIST = ROOT / "scripts" / "simple-icons-allowlist.json"

if "--offline" in sys.argv:
    if not DST_SHARED.is_file():
        raise SystemExit(f"offline 模式需要已存在: {DST_SHARED}")
    data = json.loads(DST_SHARED.read_text(encoding="utf-8"))
    print(f"Loaded from {DST_SHARED}")
else:
    req = urllib.request.Request(SRC, headers={"User-Agent": "PrivLink/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())

print(f"Total icons: {len(data)}")

if ALLOWLIST.is_file():
    allowed = set(json.loads(ALLOWLIST.read_text(encoding="utf-8")))
    known = {item.get("slug") for item in data if isinstance(item, dict)}
    missing = sorted(allowed - known)
    data = [item for item in data if isinstance(item, dict) and item.get("slug") in allowed]
    print(f"Allowlist: kept {len(data)}, dropped {len(allowed - known)} unknown slugs")
    if missing:
        print(f"  不在数据集中的清单项(已忽略): {', '.join(missing)}")

with open(DST_SHARED, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print(f"Written to {DST_SHARED}")

pairs = [
    (item["title"], item["slug"])
    for item in data
    if isinstance(item, dict) and "title" in item and "slug" in item
]
DST_MODULE.parent.mkdir(parents=True, exist_ok=True)
with open(DST_MODULE, "w", encoding="utf-8", newline="\n") as f:
    f.write(
        "#!/usr/bin/env python3\n"
        '"""Simple Icons 内嵌数据（由 scripts/fetch-simple-icons.py 生成，勿手改）。\n\n'
        "按 scripts/simple-icons-allowlist.json 允许清单裁剪，Workers bundle 只收 .py\n"
        "不收 JSON 文件，运行时靠 import 本模块获取图标。\n"
        '项为 (title, slug) 元组，url 由 icon_url_for_slug(slug) 派生。\n"""\n\n'
    )
    f.write(f"# generated from simple-icons, {len(pairs)} icons\n")
    f.write(f"ICONS: list[tuple[str, str]] = {pairs!r}\n")
print(f"Written to {DST_MODULE}")
