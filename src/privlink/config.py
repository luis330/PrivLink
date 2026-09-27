from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path


def load_env_file(path: str = ".env") -> None:
    """加载项目 .env 文件（KEY=VALUE 格式）；已存在的真实环境变量优先。"""
    env_path = Path(path)
    if not env_path.is_file():
        return
    try:
        content = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()

APP_HOST = "0.0.0.0"
APP_PORT = 8000
DB_PATH = Path("data") / "sites.db"
ICON_DIR = Path("ICON")
BACKGROUND_DIR = Path("background")
FRONTEND_PATH = Path("index.html")
# 根路径品牌资源（图标 + PWA manifest）所在目录，与 index.html 同级
ROOT_ASSET_DIR = Path(".")
ICON_MAX_BYTES = 2 * 1024 * 1024
ICON_UPLOAD_MAX_BYTES = 1024 * 1024
BACKGROUND_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 10
MAX_RETRIES = 2
MAX_REDIRECTS = 5
MAX_RETRY_AFTER_SECONDS = 5
PUBLIC_IPV4_PROVIDERS = (
    "https://ip.3322.net",
    "https://api-ipv4.ip.sb/ip",
)
PUBLIC_IPV4_TIMEOUT_SECONDS = 3.0
PUBLIC_IPV4_MAX_BYTES = 128
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
USER_AGENT = (os.environ.get("NAV_USER_AGENT") or DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT
ACCEPT_LANGUAGE = (os.environ.get("NAV_ACCEPT_LANGUAGE") or "zh-CN,zh;q=0.9,en;q=0.8").strip()
NAV_MODE = (os.environ.get("NAV_MODE") or "single").strip().lower() or "single"
# 管理 token：部署时预配置，NAV_TOKEN 优先，兼容旧变量名 NAV_INGEST_TOKEN。
# 为空 = 开放模式（API 无门禁，浏览器采集接口禁用）；非空 = 全部 /api/ 需 X-Nav-Token。
NAV_TOKEN = (os.environ.get("NAV_TOKEN") or os.environ.get("NAV_INGEST_TOKEN") or "").strip()
# 运行平台：Pyodide（Cloudflare Python Workers）下 sys.platform 为 emscripten；
# 本地（uvicorn）为 win32 / linux 等。用于 DNS 跳过、GZip 关闭、CF-Connecting-IP 分支。
IS_WORKERS = sys.platform == "emscripten"
PROXY_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
ALLOWED_SCHEMES = {"http", "https"}
# SSRF 白名单默认全禁内网；内网站点建议改用浏览器采集器上报，或在 .env 中显式放行网段
DEFAULT_ALLOWED_PRIVATE_NETWORKS = ""
HOST_ALIASES_ENV = "NAV_HOST_ALIASES"
ALLOWED_ICON_EXTENSIONS = {
    ".ico",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".webp",
    ".gif",
    ".bmp",
    ".avif",
}
CONTENT_TYPE_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
}
ALLOWED_UPLOAD_ICON_EXTENSIONS = {".ico", ".png", ".svg"}
ALLOWED_BACKGROUND_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
BACKGROUND_SETTING_KEY = "background"
# 背景图文件名只承认本服务生成的内容寻址名（bg-<sha256[:24]><ext>），杜绝路径穿越与任意文件删除
BACKGROUND_FILENAME_RE = re.compile(r"^bg-[0-9a-f]{24}\.(?:jpg|jpeg|png|webp)$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("privlink")
