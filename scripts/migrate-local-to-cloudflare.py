#!/usr/bin/env python3
"""把本地部署的数据（SQLite + 图标/背景图文件）导入 Cloudflare D1/R2。

数据源（仓库根目录，本地 uvicorn 部署的运行时数据）：
  data/sites.db   → D1 数据库 privlink（保留原 id，D1 的 AUTOINCREMENT 序列随之推进）
  ICON/*          → R2 桶 privlink-icons（key 为 ICON/<文件名>，即 storage.ICONS.key_prefix）
  background/*    → R2 桶 privlink-backgrounds（key 为 background/<文件名>）

目标选择：
  --local   写入 `pywrangler dev` 使用的本地 miniflare 状态（.wrangler/state），用于演练
  --remote  写入线上 D1/R2（需已 `wrangler login` 或设置 CLOUDFLARE_API_TOKEN）

默认只演练：生成 SQL 并打印计划，不执行任何写入；加 --apply 才真正导入。
目标库任一业务表已有数据时拒绝导入，加 --replace 先清空四张业务表再导入（R2 对象按同名覆盖）。

用法（仓库根目录）：
  uv run python scripts/migrate-local-to-cloudflare.py --local
  uv run python scripts/migrate-local-to-cloudflare.py --local --apply
  uv run python scripts/migrate-local-to-cloudflare.py --remote --apply [--replace]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from privlink import storage
from privlink.db import D1_SCHEMA_STATEMENTS
from privlink.staticfiles import media_type_for

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "wrangler.jsonc"
DATABASE = "privlink"
ICON_BUCKET = "privlink-icons"
BACKGROUND_BUCKET = "privlink-backgrounds"

# 按外键依赖排序：插入正序，清空逆序
TABLES = ["app_settings", "sites", "tags", "site_tags"]


def sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def build_sql(db_path: Path, replace: bool) -> tuple[str, dict[str, int]]:
    statements = [sql for sql, _ in D1_SCHEMA_STATEMENTS]
    if replace:
        statements += [f"DELETE FROM {table};" for table in reversed(TABLES)]
    counts: dict[str, int] = {}
    conn = sqlite3.connect(db_path)
    try:
        for table in TABLES:
            cursor = conn.execute(f"SELECT * FROM {table}")
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            counts[table] = len(rows)
            for row in rows:
                values = ", ".join(sql_literal(v) for v in row)
                statements.append(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({values});")
    finally:
        conn.close()
    return "\n".join(statements) + "\n", counts


def wrangler_cmd() -> list[str]:
    npx = shutil.which("npx")
    if npx is None:
        raise SystemExit("找不到 npx：请先安装 Node.js")
    return [npx, "--yes", "wrangler"]


def run_wrangler(args: list[str], target: str, capture: bool = False) -> str:
    cmd = wrangler_cmd() + args + [f"--{target}", "--config", str(CONFIG)]
    print("  $ wrangler " + " ".join(args + [f"--{target}"]))
    result = subprocess.run(
        cmd, cwd=ROOT, check=False, text=True, encoding="utf-8",
        capture_output=capture,
    )
    if result.returncode != 0:
        if capture:
            sys.stderr.write(result.stdout or "")
            sys.stderr.write(result.stderr or "")
        raise SystemExit(f"wrangler 执行失败（退出码 {result.returncode}）")
    return result.stdout or ""


def query_count(sql: str, target: str) -> int:
    output = run_wrangler(["d1", "execute", DATABASE, "--json", "--command", sql], target, capture=True)
    # --json 输出为语句结果数组：[{"results": [{"n": 0}], "success": true, ...}]
    payload = json.loads(output[output.index("["):])
    return int(payload[0]["results"][0]["n"])


def existing_row_count(target: str) -> int:
    """目标库四张业务表的总行数（任一表有数据，不带 --replace 的 INSERT 都会主键冲突）。"""
    # 表可能尚不存在（全新库）：先确保 schema 再计数
    for sql, _ in D1_SCHEMA_STATEMENTS:
        run_wrangler(["d1", "execute", DATABASE, "--command", sql], target, capture=True)
    total = " + ".join(f"(SELECT COUNT(*) FROM {table})" for table in TABLES)
    return query_count(f"SELECT {total} AS n", target)


def media_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument("--local", action="store_const", const="local", dest="target")
    target_group.add_argument("--remote", action="store_const", const="remote", dest="target")
    parser.add_argument("--apply", action="store_true", help="真正执行导入（默认只演练）")
    parser.add_argument("--replace", action="store_true", help="目标库已有数据时先清空再导入")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "sites.db")
    parser.add_argument("--icons", type=Path, default=ROOT / "ICON")
    parser.add_argument("--backgrounds", type=Path, default=ROOT / "background")
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"本地数据库不存在: {args.db}", file=sys.stderr)
        return 1

    sql, counts = build_sql(args.db, args.replace)
    icons = media_files(args.icons)
    backgrounds = media_files(args.backgrounds)

    print(f"目标：{args.target}（D1 {DATABASE} / R2 {ICON_BUCKET}, {BACKGROUND_BUCKET}）")
    print("D1 行数：" + "，".join(f"{t} {n}" for t, n in counts.items()))
    print(f"R2 对象：图标 {len(icons)} 个，背景 {len(backgrounds)} 个")

    sql_file = Path(tempfile.gettempdir()) / "privlink-migrate.sql"
    sql_file.write_text(sql, encoding="utf-8")
    print(f"SQL 已生成：{sql_file}")

    if not args.apply:
        print("演练模式：未写入任何数据。确认无误后加 --apply 执行。")
        return 0

    existing = existing_row_count(args.target)
    if existing and not args.replace:
        print(f"目标 D1 业务表已有 {existing} 行数据，拒绝覆盖；确认要清空后导入请加 --replace", file=sys.stderr)
        return 1

    print("导入 D1 ...")
    run_wrangler(["d1", "execute", DATABASE, "--file", str(sql_file), "-y"], args.target)

    for bucket, area, files in (
        (ICON_BUCKET, storage.ICONS, icons),
        (BACKGROUND_BUCKET, storage.BACKGROUNDS, backgrounds),
    ):
        print(f"上传 R2 {bucket} ...")
        for path in files:
            run_wrangler(
                ["r2", "object", "put", f"{bucket}/{area.key_prefix}{path.name}", "--file", str(path),
                 "--content-type", media_type_for(path.name)],
                args.target,
            )

    imported = query_count("SELECT COUNT(*) AS n FROM sites", args.target)
    print(f"完成：目标 D1 现有站点 {imported} 个（本地 {counts['sites']} 个）")
    return 0 if imported == counts["sites"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
