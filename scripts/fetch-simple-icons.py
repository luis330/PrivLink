#!/usr/bin/env python3
"""拉取 Simple Icons 完整数据（含 slug），写入共享数据源 + Cloudflare assets。

共享数据源：仓库根目录 simple-icons.json（Python 端启动时读取）。
Cloudflare 副本：deploy/cloudflare/assets/simple-icons.json（Workers Assets 服务）。
"""
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SRC = "https://unpkg.com/simple-icons@latest/icons.json"
DST_SHARED = ROOT / "simple-icons.json"
DST_CF = ROOT / "deploy" / "cloudflare" / "assets" / "simple-icons.json"

req = urllib.request.Request(SRC, headers={"User-Agent": "PrivLink/1.0"})
with urllib.request.urlopen(req, timeout=20) as resp:
    data = json.loads(resp.read())

print(f"Total icons: {len(data)}")

for dst in (DST_SHARED, DST_CF):
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Written to {dst}")