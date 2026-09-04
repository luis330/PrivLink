#!/usr/bin/env python3
"""
API 端点对齐检查脚本。
从 main.py 提取所有 FastAPI 路由，
与 deploy/cloudflare/src/index.ts 中的 Hono 路由对比，
输出缺失或多余的端点。

用法：python scripts/check-api-alignment.py
"""

import ast
import re
import sys
from pathlib import Path

# Windows 控制台默认 GBK，输出中的 ✅/❌ 会触发 UnicodeEncodeError。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = ROOT / "main.py"
TS_INDEX = ROOT / "deploy/cloudflare" / "src" / "index.ts"

# 两端刻意不一致的端点（方法, 路径）。
# / 与 /index.html：Python 显式路由返回 HTML；TS 端由 Workers Assets
# 自动服务 index.html，无需显式路由。
# /favicon.svg 与 /favicon.ico 同理：两个文件由 scripts/sync-frontend.py 同步进
# deploy/cloudflare/assets/，Workers Assets 按文件名直接命中并推断 Content-Type，
# 请求不会进入 Worker；Python 端没有根目录静态挂载，才需要显式路由。
#
# 注意 /api/network/public-ip 两端都有、但语义与鉴权刻意不同（本脚本只比对路由
# 存在性，查不出这类差异）：Python 端返回服务端出口 IP、需 token；TS 端返回访客
# 自己的 IP（CF-Connecting-IP）、在公开只读清单中。这条差异由两端测试钉死——
# 见 tests/test_ingest.py::TokenGuardTest 与 deploy/cloudflare/tests/api.spec.ts。
EXEMPT = {
    ("GET", "/"),
    ("GET", "/index.html"),
    ("GET", "/favicon.svg"),
    ("GET", "/favicon.ico"),
}


def extract_fastapi_routes(path: Path) -> list[tuple[str, str]]:
    """返回 [(method, path), ...]（含 async 函数与 app.mount 静态挂载）"""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    routes: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                method, route_path = _parse_decorator(dec)
                if method and route_path:
                    routes.append((method.upper(), route_path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Attribute) and func.attr == "mount" and node.value.args:
                arg = node.value.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    routes.append(("GET", arg.value + "/*"))
    return routes


def _parse_decorator(dec: ast.expr) -> tuple[str | None, str | None]:
    """解析 @app.get('/path') 等装饰器"""
    if isinstance(dec, ast.Call):
        func = dec.func
        args = dec.args
        kwargs = dec.keywords
    elif isinstance(dec, ast.Attribute):
        func = dec
        args = []
        kwargs = []
    else:
        return None, None

    if not isinstance(func, ast.Attribute):
        return None, None
    if func.attr not in ("get", "post", "put", "delete", "patch", "options"):
        return None, None
    method = func.attr.lower()

    # 路径是第一个位置参数，或 keyword path=...
    path_val: str | None = None
    if args:
        if isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
            path_val = args[0].value
    for kw in kwargs:
        if kw.arg == "path" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            path_val = kw.value.value

    return method, path_val or "/"


def extract_hono_routes(path: Path) -> list[tuple[str, str]]:
    """从 TS 源提取 app.get/post/put/delete(...) 路由"""
    text = path.read_text(encoding="utf-8")
    routes: list[tuple[str, str]] = []
    pattern = re.compile(r'app\.(get|post|put|delete|patch|options)\(\s*"([^"]+)"', re.IGNORECASE)
    for m in pattern.finditer(text):
        method = m.group(1).upper()
        route_path = m.group(2)
        routes.append((method, route_path))
    return routes


def normalize_path(p: str) -> str:
    """标准化路径：去尾部斜杠，{param} 与 :param 统一为 {param}"""
    p = p.rstrip("/") or "/"
    p = re.sub(r"\{[^}]+\}", "{param}", p)
    p = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", "{param}", p)
    return p


def main() -> int:
    if not MAIN_PY.exists():
        print(f"ERROR: {MAIN_PY} not found", file=sys.stderr)
        return 1
    if not TS_INDEX.exists():
        print(f"ERROR: {TS_INDEX} not found", file=sys.stderr)
        return 1

    py_routes = set((m, normalize_path(p)) for m, p in extract_fastapi_routes(MAIN_PY))
    ts_routes = set((m, normalize_path(p)) for m, p in extract_hono_routes(TS_INDEX))

    missing_in_ts = py_routes - ts_routes - EXEMPT
    extra_in_ts = ts_routes - py_routes - EXEMPT

    ok = True
    if missing_in_ts:
        print("=== Python 有 / TypeScript 缺失 ===")
        for m, p in sorted(missing_in_ts):
            print(f"  {m:6s} {p}")
        ok = False
    if extra_in_ts:
        print("=== TypeScript 有 / Python 缺失 ===")
        for m, p in sorted(extra_in_ts):
            print(f"  {m:6s} {p}")
        ok = False

    if ok:
        print("✅ 端点对齐：Python 与 TypeScript 路由一致")
    if EXEMPT:
        exempt_desc = ", ".join(f"{m} {p}" for m, p in sorted(EXEMPT))
        print(f"ℹ️  豁免 {len(EXEMPT)} 个端点（TS 端由 Workers Assets 直接服务）：{exempt_desc}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
