#!/usr/bin/env python3
"""从 stdin 读取 `wrangler d1 list --json` 输出，按名称查找 D1 数据库 uuid。

供 .github/workflows/deploy-cloudflare.yml 的 preCommands 使用（GitHub Actions 无 grep -P，
且 shell 内联多行 python 易受 YAML block scalar 缩进影响，故独立成脚本）。

用法：
  wrangler d1 list --json | python3 scripts/parse-d1-id.py
输出：匹配数据库的 uuid；未找到或输入非法时输出空串。
"""
import json
import sys

raw = sys.stdin.read()
try:
    dbs = json.loads(raw) if raw.strip() else []
except json.JSONDecodeError:
    dbs = []

db = next((d for d in dbs if d.get("name") == "privlink"), None)
print(db.get("uuid", "") if db else "")