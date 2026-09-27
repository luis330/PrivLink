#!/usr/bin/env python3
"""拉取 Simple Icons 完整数据（含 slug），写入共享数据源 + 内嵌模块。

共享数据源：仓库根目录 simple-icons.json（本地源码 / Docker 部署启动时读取）。
内嵌模块：src/privlink/data/simple_icons_data.py —— Workers bundle 只收 .py/.text
不收 JSON 文件，故生成 .py 模块供 import（wheel 安装同样读它）。

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

if "--offline" not in sys.argv:
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
        "Workers bundle 不收 JSON 文件，运行时靠 import 本模块获取全量图标。\n"
        '项为 (title, slug) 元组，url 由 icon_url_for_slug(slug) 派生。\n"""\n\n'
    )
    f.write(f"# generated from simple-icons, {len(pairs)} icons\n")
    f.write(f"ICONS: list[tuple[str, str]] = {pairs!r}\n")
print(f"Written to {DST_MODULE}")
