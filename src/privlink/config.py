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
# 内置插件源目录（仓库根，Docker 与本地部署由此读取；Workers 由 Assets 服务 assets/plugins/）
PLUGINS_DIR = Path("plugins")
# 插件 id 同时用作 URL 段与数据命名空间：小写字母数字与连字符，禁止点（registry.json 不是插件）
PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
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
# 开发开关：默认关闭 /docs /redoc /openapi.json（生产不暴露 API 结构）。
# 本地开发需要时设 NAV_ENABLE_DOCS=1；Workers 部署恒关闭（路由在构建时注册）。
NAV_ENABLE_DOCS = (os.environ.get("NAV_ENABLE_DOCS") or "").strip().lower() in {"1", "true", "yes", "on"}


def parse_cors_origins(raw: str) -> tuple[str, ...]:
    """解析逗号分隔的 CORS 白名单；去空白与结尾斜杠。"""
    origins: list[str] = []
    for part in (raw or "").split(","):
        origin = part.strip().rstrip("/")
        if origin:
            origins.append(origin)
    return tuple(origins)


# CORS 白名单：默认空 = 仅同源（不返回任何 CORS 头）。已知跨域调用方均不依赖服务端
# CORS：油猴采集器走 GM_xmlhttpRequest（特权 API），浏览器扩展持 host_permissions
# （MV3 豁免 CORS）；确有其他跨域消费方时再显式加白。
CORS_ALLOWED_ORIGINS = parse_cors_origins(os.environ.get("NAV_CORS_ORIGINS") or "")

# Token 防爆破限速（TokenGuard 内滑动窗口，仅统计 token 无效的失败请求；
# Workers isolate 内存计数为尽力而为，公网部署的权威限速配置在 Cloudflare WAF，
# 见 docs/security-ops.md）
NAV_AUTH_FAIL_WINDOW = max(1, int(os.environ.get("NAV_AUTH_FAIL_WINDOW") or "60"))
NAV_AUTH_FAIL_MAX = max(1, int(os.environ.get("NAV_AUTH_FAIL_MAX") or "10"))
NAV_AUTH_LOCKOUT_SECONDS = max(0, int(os.environ.get("NAV_AUTH_LOCKOUT_SECONDS") or "900"))
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

# 页面 CSP：与仓库根 _headers 保持一致（Workers Assets 静态路径由 _headers 覆盖，
# 其余响应由 SecurityHeadersMiddleware 下发）。script-src 的 unsafe-inline 是务实选择：
# 前端为单文件内联脚本，Workers 上首页由静态 Assets 直接服务、nonce 无法注入；
# DOM 注入向量已由前端 textContent/createElement 卫生关闭。
# 前端事实依据：img 需要 blob:（上传预览）与 https:（cdn.simpleicons.org 及存量外链图标）。
CSP_PAGE = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' blob: https:; connect-src 'self'; object-src 'none'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'self'"
)
# SVG 以独立文档直接打开时执行内嵌脚本的收口：<img> 引用不受影响（图像上下文本就不执行
# 脚本），直接导航时 sandbox 使脚本失效。
CSP_SVG = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
# 插件文档 CSP：插件运行在 sandbox="allow-scripts"（无 allow-same-origin）iframe 里，
# 属不透明源，拿不到主页面 DOM/localStorage/token。default-src 'none' 断网断外链，
# img 仅 data:/blob:（堵死图片外传信道）；sandbox allow-scripts 使插件被直接导航打开时
# 同样进沙箱（不可照抄 CSP_SVG 的裸 sandbox——那会连 iframe 内脚本一起禁掉）。
# 有意不设 frame-ancestors：沙箱文档离开宿主桥即惰性，且避免 'self' 与不透明源的匹配歧义。
CSP_PLUGIN = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; sandbox allow-scripts"
)
# 插件数据配额：每插件键数与键值字节总量上限（防存储滥用；超限写入整体拒绝）
PLUGIN_DATA_MAX_KEYS = 200
PLUGIN_DATA_MAX_BYTES = 256 * 1024
# 浮动窗口注册表：单插件实例数上限（防异常累积）
PLUGIN_MAX_WINDOWS = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("privlink")
