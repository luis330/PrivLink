from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response as StarletteResponse, StreamingResponse

from privlink import config, fetcher, storage
from privlink.auth import (
    can_view_private,
    is_public_readonly_path,
    resolve_request_identity,
    token_header_matches,
    validate_ingest_token,
)
from privlink.background import (
    background_upload_filename,
    delete_background_image_file,
    list_background_images,
    load_background_setting,
    save_background_setting,
    stored_background_image,
)
from privlink.bookmarks import parse_bookmarks_html, render_bookmarks_html
from privlink.db import (
    SITE_ITEM_COLUMNS,
    TAG_NAME_MAX_LEN,
    D1Database,
    IntegrityConflictError,
    add_plugin_window,
    bind_db,
    delete_app_setting,
    delete_plugin_data,
    delete_plugin_window,
    ensure_remote_schema,
    fetch_all_site_tags,
    fetch_plugin_data,
    fetch_site_row,
    fetch_site_tags,
    fetch_tag_ids_by_casefold,
    get_db,
    get_plugin_state,
    get_plugin_windows,
    init_storage,
    normalize_tag_list,
    normalize_tag_name,
    orphan_tags_statement,
    plugin_data_usage,
    set_plugin_state,
    set_plugin_windows,
    site_tags_statements,
    unbind_db,
    update_plugin_window,
    upsert_plugin_data,
    utc_now,
)
from privlink.fetcher import PublicIPv4LookupError
from privlink.icons import (
    contains_active_svg_content,
    icon_url_for_slug,
    icon_upload_filename,
    list_simple_icons,
)
from privlink.jsinterop import js_field
from privlink.models import (
    AuthStatusResponse,
    BackgroundImageItem,
    BackgroundSettingRequest,
    BackgroundSettingResponse,
    BrowserIngestRequest,
    MessageResponse,
    ParseRequest,
    ParseResponse,
    PluginDataWriteRequest,
    PluginStateRequest,
    PluginWindowItem,
    PluginWindowsRequest,
    PublicIPv4Response,
    ReorderRequest,
    SiteItem,
    SiteUpdateRequest,
    TagItem,
    error_payload,
    to_site_item,
)
from privlink.network import normalize_url
from privlink.sites import (
    maybe_remove_old_icon,
    process_browser_ingest,
    process_site_url,
)
from privlink.staticfiles import conditional_file_response, media_type_for, root_asset_response
from privlink.storage import StorageArea, bind_buckets, unbind_buckets

_icons_cache: list[dict[str, str]] = []


def _icon_catalog() -> list[dict[str, str]]:
    """图标库；lifespan 未执行（如 Workers 未发 lifespan 事件）时按需加载。"""
    global _icons_cache
    if not _icons_cache:
        _icons_cache = list_simple_icons()
    return _icons_cache


@asynccontextmanager
async def app_lifespan(_: FastAPI):
    global _icons_cache
    if config.NAV_MODE != "single":
        config.logger.warning("NAV_MODE=%s 暂未实现，按 single 模式运行", config.NAV_MODE)
    if not config.NAV_TOKEN:
        config.logger.warning("未设置 NAV_TOKEN，API 处于开放模式；公网部署请配置访问 token")
    elif len(config.NAV_TOKEN) < 32:
        config.logger.warning(
            "NAV_TOKEN 长度不足 32 位（当前 %d 位），静态 token 是唯一管理凭证，建议加长",
            len(config.NAV_TOKEN),
        )
    if config.IS_WORKERS:
        # Workers 无持久本地磁盘：DDL 由 PlatformBindings 在首个请求执行
        config.logger.info("Workers 模式：跳过本地目录/SQLite 初始化")
    else:
        init_storage()
    _icons_cache = list_simple_icons()
    config.logger.info("服务启动完成，图标库加载 %d 个图标", len(_icons_cache))
    yield


_docs_kwargs = (
    {}
    if config.NAV_ENABLE_DOCS
    else {"docs_url": None, "redoc_url": None, "openapi_url": None}
)
app = FastAPI(title="PrivLink", version="1.0.0", lifespan=app_lifespan, **_docs_kwargs)
if not config.IS_WORKERS:
    # Workers 内建压缩，重复 GZip 中间件会破坏 Pyodide 响应流
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


class TokenGuard:
    """纯 ASGI 令牌守卫（不走 BaseHTTPMiddleware，避免流式响应问题）。

    内置按来源 IP 的 401 失败滑动窗口限速：仅统计 token 无效的失败请求，
    持有效 token 的请求不受影响并清零该 IP 计数；窗口内失败超过阈值后，
    锁定期内该 IP 的无效请求直接 429。Workers isolate 内存计数是尽力而为，
    公网部署的权威限速应配置在 Cloudflare WAF Rate Limiting（见 docs/security-ops.md）。
    """

    _failures: dict[str, deque[float]] = {}
    _locked_until: dict[str, float] = {}
    _MAX_TRACKED_IPS = 4096
    # 锁定被关闭时的单 IP 窗口条目上限（防高频攻击下的内存膨胀）
    _MAX_WINDOW_ENTRIES = 1000

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            path: str = scope["path"]
            method: str = scope["method"]
            if (
                config.NAV_TOKEN
                and path.startswith("/api/")
                and method != "OPTIONS"
                and not (method == "GET" and is_public_readonly_path(path))
            ):
                headers = {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in scope["headers"]
                }
                if token_header_matches(headers.get("x-nav-token")):
                    self._clear_client(headers, scope)
                else:
                    retry_after = self._reject_or_count(headers, scope)
                    if retry_after is not None:
                        await self._send_json(
                            send,
                            429,
                            {"error": "请求过于频繁，请稍后重试"},
                            extra_headers=[(b"retry-after", str(retry_after).encode("latin-1"))],
                        )
                        return
                    await self._send_json(send, 401, {"error": "需要访问 token"})
                    return
        await self.app(scope, receive, send)

    @staticmethod
    def _client_ip(headers: dict[str, str], scope: dict[str, Any]) -> str:
        # 只信 Workers 边缘注入的 cf-connecting-ip 与 socket 对端地址；
        # X-Forwarded-For 可被客户端伪造，不采信（否则限速可被绕过）
        ip = (headers.get("cf-connecting-ip") or "").strip()
        if ip:
            return ip
        client = scope.get("client")
        return str(client[0]) if client else "unknown"

    @classmethod
    def _clear_client(cls, headers: dict[str, str], scope: dict[str, Any]) -> None:
        ip = cls._client_ip(headers, scope)
        cls._failures.pop(ip, None)
        cls._locked_until.pop(ip, None)

    @classmethod
    def _reject_or_count(cls, headers: dict[str, str], scope: dict[str, Any]) -> int | None:
        """失败请求的限速判定：命中锁定返回 Retry-After 秒数，否则记一次失败并放行为 401。"""
        now = time.monotonic()
        ip = cls._client_ip(headers, scope)
        lock_until = cls._locked_until.get(ip, 0.0)
        if lock_until > now:
            return max(1, int(lock_until - now))

        window = float(config.NAV_AUTH_FAIL_WINDOW)
        cutoff = now - window
        stamps = cls._failures.setdefault(ip, deque())
        while stamps and stamps[0] < cutoff:
            stamps.popleft()
        stamps.append(now)
        if len(stamps) > int(config.NAV_AUTH_FAIL_MAX) and config.NAV_AUTH_LOCKOUT_SECONDS > 0:
            cls._failures.pop(ip, None)
            cls._locked_until[ip] = now + float(config.NAV_AUTH_LOCKOUT_SECONDS)
            return int(config.NAV_AUTH_LOCKOUT_SECONDS)
        if len(stamps) > cls._MAX_WINDOW_ENTRIES:
            # 锁定关闭（LOCKOUT=0）时的兜底：只 401 不锁定，但窗口条目不无界增长
            stamps.clear()

        # 计数表防膨胀：超限时清理已过期条目
        if len(cls._failures) > cls._MAX_TRACKED_IPS:
            for key in [k for k, v in cls._failures.items() if not v or v[-1] < cutoff]:
                cls._failures.pop(key, None)
            for key in [k for k, v in cls._locked_until.items() if v <= now]:
                cls._locked_until.pop(key, None)
        return None

    @staticmethod
    async def _send_json(
        send: Any,
        status: int,
        payload: dict[str, str],
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response_headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("latin-1")),
        ]
        if extra_headers:
            response_headers.extend(extra_headers)
        await send({"type": "http.response.start", "status": status, "headers": response_headers})
        await send({"type": "http.response.body", "body": body})


_SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"x-frame-options", b"SAMEORIGIN"),
)


class SecurityHeadersMiddleware:
    """为全部响应补基础安全头（含 401/429 等短路响应）。

    响应已带同名头时不覆盖——媒体路由会为 SVG 发更严格的沙箱 CSP。
    HSTS 有意不在应用层下发：Cloudflare 区域设置统一下发，避免重复响应头。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {key.decode("latin-1").lower() for key, _ in headers}
                if "content-security-policy" not in present:
                    headers.append(
                        (b"content-security-policy", config.CSP_PAGE.encode("latin-1"))
                    )
                for key, value in _SECURITY_HEADERS:
                    if key.decode("latin-1") not in present:
                        headers.append((key, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class CorsGuard:
    """纯 ASGI CORS 守卫：仅当请求 Origin 命中 config.CORS_ALLOWED_ORIGINS 时放行跨域。

    白名单默认为空 → 不产生任何 CORS 头（同源部署零跨域面；浏览器同源请求本就
    不需要 CORS）。预检在本层直接应答，其余请求透传并在响应上补 ACAO。
    白名单来自 env（NAV_CORS_ORIGINS），Workers 下由 PlatformBindings 逐请求注入。
    """

    _ALLOW_METHODS = "GET, HEAD, POST, PUT, DELETE, OPTIONS"
    _ALLOW_HEADERS = "X-Nav-Token, Content-Type"

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        origin = self._origin(scope)
        if not origin or not self._allowed(origin):
            await self.app(scope, receive, send)
            return
        if scope["method"] == "OPTIONS" and self._is_preflight(scope):
            await self._send_preflight(send, origin)
            return

        async def send_with_cors(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"access-control-allow-origin", origin.encode("latin-1")))
                headers.append((b"vary", b"origin"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_cors)

    @staticmethod
    def _origin(scope: dict[str, Any]) -> str:
        for key, value in scope["headers"]:
            if key == b"origin":
                return value.decode("latin-1").strip()
        return ""

    @staticmethod
    def _allowed(origin: str) -> bool:
        clean = origin.rstrip("/")
        return bool(clean) and clean in config.CORS_ALLOWED_ORIGINS

    @staticmethod
    def _is_preflight(scope: dict[str, Any]) -> bool:
        return any(key == b"access-control-request-method" for key, _ in scope["headers"])

    async def _send_preflight(self, send: Any, origin: str) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-length", b"0"),
                    (b"access-control-allow-origin", origin.encode("latin-1")),
                    (b"access-control-allow-methods", self._ALLOW_METHODS.encode("latin-1")),
                    (b"access-control-allow-headers", self._ALLOW_HEADERS.encode("latin-1")),
                    (b"access-control-max-age", b"600"),
                    (b"vary", b"origin"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})


class PlatformBindings:
    """Workers：从 request.scope["env"] 注入 D1/R2 bindings 并执行首次 DDL。

    lifespan 拿不到 env bindings（POC 结论），故放到每个请求的上下文里。
    本地运行时 scope 无 env，透传。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        env = scope.get("env")
        if env is not None:
            # wrangler vars/secrets 不进 os.environ：逐请求从 env 回写 config（缺省即重置，
            # 不沿用上一请求的值），供 TokenGuard/CorsGuard/auth 属性访问读取。
            # 本中间件须注册在最外层。
            token = js_field(env, "NAV_TOKEN") or js_field(env, "NAV_INGEST_TOKEN")
            config.NAV_TOKEN = token.strip() if isinstance(token, str) else ""
            mode = js_field(env, "NAV_MODE")
            config.NAV_MODE = (mode.strip().lower() if isinstance(mode, str) else "") or "single"
            origins = js_field(env, "NAV_CORS_ORIGINS")
            config.CORS_ALLOWED_ORIGINS = config.parse_cors_origins(
                origins if isinstance(origins, str) else ""
            )
        cleanup: list[tuple[Any, Any]] = []
        try:
            d1 = js_field(env, "DB")
            if d1 is not None:
                cleanup.append((unbind_db, bind_db(D1Database(d1))))
                await ensure_remote_schema()
            # binding 名与 wrangler.jsonc 一致（沿用 TS 时代的线上资源，勿改名）
            icons = js_field(env, "ICON_BUCKET")
            backgrounds = js_field(env, "BACKGROUND_BUCKET")
            if icons is not None or backgrounds is not None:
                cleanup.append((unbind_buckets, bind_buckets(icons, backgrounds)))
            await self.app(scope, receive, send)
        finally:
            for unbind, token in reversed(cleanup):
                unbind(token)


app.add_middleware(TokenGuard)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CorsGuard)
# 后注册 = 最外层：必须先于 TokenGuard/CorsGuard 注入 vars/config
app.add_middleware(PlatformBindings)


def _media_not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


def _safe_media_name(file_path: str) -> bool:
    # 单层纯文件名：拒绝分隔符、上跳与盘符（Windows 下 "C:x" 会脱离基目录）
    if not file_path or any(ch in file_path for ch in '/\\:\0') or ".." in file_path:
        return False
    return True


async def _media_response(request: Request, file_path: str, area: StorageArea):
    """/ICON 与 /background 共用：本地走带 ETag 的文件响应，Workers 流式转发 R2 正文。"""
    if not _safe_media_name(file_path):
        return _media_not_found()
    cache_control = "public, max-age=86400"
    # SVG 以独立文档直接打开时会执行内嵌脚本：沙箱 CSP 收口。<img> 引用不受影响
    # （图像上下文本就不执行脚本），存量 SVG 图标无需迁移；nosniff 由安全头中间件统一补。
    extra_headers = (
        {"Content-Security-Policy": config.CSP_SVG}
        if file_path.lower().endswith(".svg")
        else None
    )
    if area.bucket() is None:
        response = conditional_file_response(
            request,
            area.local_dir() / file_path,
            media_type_for(file_path),
            cache_control,
            extra_headers,
        )
        return response if response is not None else _media_not_found()
    entry = await storage.get(area, file_path, request.headers.get("if-none-match", ""))
    if entry is None:
        return _media_not_found()
    headers = {"Cache-Control": cache_control}
    if extra_headers:
        headers.update(extra_headers)
    if entry.etag:
        headers["ETag"] = entry.etag
    if entry.body is None:
        return StarletteResponse(status_code=304, headers=headers)
    return StreamingResponse(entry.body, media_type=entry.content_type, headers=headers)


@app.get("/ICON/{file_path:path}", include_in_schema=False, response_model=None)
async def serve_icon_file(request: Request, file_path: str):
    return await _media_response(request, file_path, storage.ICONS)


@app.get("/background/{file_path:path}", include_in_schema=False, response_model=None)
async def serve_background_file(request: Request, file_path: str):
    return await _media_response(request, file_path, storage.BACKGROUNDS)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content=error_payload(f"Invalid request: {exc}"))


@app.exception_handler(Exception)
async def generic_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    config.logger.error("未捕获异常: %s", exc, exc_info=True)
    # 原始异常文本可能携带内部路径/SQL/配置细节，不回显给客户端
    return JSONResponse(status_code=500, content=error_payload("Internal server error"))


@app.get("/", include_in_schema=False, response_model=None)
async def index_page(request: Request):
    response = conditional_file_response(request, config.FRONTEND_PATH, "text/html", "no-cache")
    if response is None:
        return JSONResponse(status_code=500, content=error_payload("Frontend file is missing"))
    return response


@app.get("/index.html", include_in_schema=False, response_model=None)
async def index_page_alias(request: Request):
    return await index_page(request)


@app.get("/favicon.ico", include_in_schema=False, response_model=None)
async def favicon_ico(request: Request):
    """多尺寸 ICO。覆盖浏览器与爬虫对根路径的隐式请求——它们不解析 <link rel="icon">。"""
    return root_asset_response(request, "favicon.ico", "image/x-icon")


@app.get("/favicon-32x32.png", include_in_schema=False, response_model=None)
async def favicon_png_32(request: Request):
    return root_asset_response(request, "favicon-32x32.png", "image/png")


@app.get("/favicon-16x16.png", include_in_schema=False, response_model=None)
async def favicon_png_16(request: Request):
    return root_asset_response(request, "favicon-16x16.png", "image/png")


@app.get("/apple-touch-icon.png", include_in_schema=False, response_model=None)
async def apple_touch_icon(request: Request):
    """iOS 主屏图标。同样存在隐式请求：Safari 未见 link 声明时直接拉这个根路径。"""
    return root_asset_response(request, "apple-touch-icon.png", "image/png")


@app.get("/android-chrome-192x192.png", include_in_schema=False, response_model=None)
async def android_chrome_192(request: Request):
    return root_asset_response(request, "android-chrome-192x192.png", "image/png")


@app.get("/android-chrome-512x512.png", include_in_schema=False, response_model=None)
async def android_chrome_512(request: Request):
    return root_asset_response(request, "android-chrome-512x512.png", "image/png")


@app.get("/manifest.json", include_in_schema=False, response_model=None)
async def web_app_manifest(request: Request):
    return root_asset_response(request, "manifest.json", "application/manifest+json")


@app.get("/api/auth/status", response_model=AuthStatusResponse)
async def read_auth_status(request: Request, response: Response) -> AuthStatusResponse:
    """公开状态接口：token_required=是否门禁模式；authorized=本请求是否具备管理身份（开放模式恒 True）。"""
    response.headers["Cache-Control"] = "no-store"
    return AuthStatusResponse(
        token_required=bool(config.NAV_TOKEN),
        authorized=(not config.NAV_TOKEN) or (resolve_request_identity(request) is not None),
    )


@app.get("/api/network/public-ip", response_model=PublicIPv4Response)
async def read_public_ipv4(request: Request) -> JSONResponse:
    if config.IS_WORKERS:
        # Workers 出口是 Cloudflare 任播边缘，探测服务端出口 IP 无意义：改答访问者 IP。
        # CF-Connecting-IP 由边缘注入（客户端同名头会被覆盖）；访客可能走 IPv6，原样返回。
        client_ip = (request.headers.get("cf-connecting-ip") or "").strip()
        if not client_ip:
            return JSONResponse(
                status_code=502,
                content={"error": "无法获取访问者公网 IP"},
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            status_code=200,
            content={"ip": client_ip, "kind": "client"},
            headers={"Cache-Control": "no-store"},
        )
    try:
        public_ip = await fetcher.public_ipv4_resolver.resolve()
    except PublicIPv4LookupError as exc:
        config.logger.warning("获取直连公网 IPv4 失败: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"error": "无法获取直连公网 IPv4"},
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        status_code=200,
        content={"ip": public_ip, "kind": "server"},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/appearance/background", response_model=BackgroundSettingResponse)
async def read_background_setting() -> dict[str, str]:
    """公开只读：匿名访客与管理员看到同一全局背景设置。"""
    return await load_background_setting()


@app.put("/api/appearance/background", response_model=BackgroundSettingResponse)
async def update_background_setting(payload: BackgroundSettingRequest) -> JSONResponse | dict[str, str]:
    try:
        return await save_background_setting({"type": payload.type, "color": payload.color, "image": payload.image})
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@app.get("/api/appearance/background/images", response_model=list[BackgroundImageItem])
async def read_background_images() -> list[dict[str, Any]]:
    return await list_background_images()


@app.post("/api/appearance/background/images", response_model=BackgroundSettingResponse)
async def upload_background_image(image: UploadFile = File(...)) -> JSONResponse | dict[str, str]:
    filename = (image.filename or "").strip()
    if not filename:
        return JSONResponse(status_code=400, content={"error": "请上传背景图片文件"})
    if Path(filename).suffix.lower() not in config.ALLOWED_BACKGROUND_EXTENSIONS:
        return JSONResponse(status_code=400, content={"error": "仅支持 jpg、png、webp 格式图片"})

    content = await image.read()
    if not content:
        return JSONResponse(status_code=400, content={"error": "图片内容为空"})
    if len(content) > config.BACKGROUND_UPLOAD_MAX_BYTES:
        return JSONResponse(status_code=400, content={"error": "图片大小不能超过 5MB"})

    try:
        stored_name = background_upload_filename(content, filename)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    # 内容寻址文件名：同内容重传幂等覆盖；上传即生效，直接设为当前背景
    await storage.put(storage.BACKGROUNDS, stored_name, content)
    return await save_background_setting({"type": "image", "image": stored_name})


@app.delete("/api/appearance/background/images/{filename}", response_model=BackgroundSettingResponse)
async def delete_background_image(filename: str) -> JSONResponse | dict[str, str]:
    # 删除前读取数据库中的记录：删除后再读，文件已不存在，无法判断删的是否是当前背景
    in_use = await stored_background_image() == filename
    try:
        await delete_background_image_file(filename)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    if in_use:
        # 删除的是当前背景：重置为默认并返回新设置供前端直接应用
        return await save_background_setting({"type": "default"})
    return await load_background_setting()


@app.post("/api/site/parse", response_model=ParseResponse)
async def parse_site(payload: ParseRequest) -> JSONResponse:
    raw_url = payload.url.strip()
    if not raw_url:
        return JSONResponse(status_code=400, content=error_payload("Field 'url' is required"))
    result = await process_site_url(raw_url)
    status_code = 400 if result["status"] == "invalid" else 200
    return JSONResponse(status_code=status_code, content=result)


@app.get("/api/sites", response_model=list[SiteItem])
async def list_sites(request: Request) -> list[dict[str, Any]]:
    show_private = can_view_private(request)
    where_clause = "" if show_private else "WHERE is_public = 1"
    db = get_db()
    rows = await db.fetch_all(
        f"""
        SELECT {SITE_ITEM_COLUMNS}
        FROM sites
        {where_clause}
        ORDER BY sort_order ASC, id ASC;
        """
    )
    tags_by_site = await fetch_all_site_tags()

    return [to_site_item(row, tags_by_site.get(int(row["id"]), [])) for row in rows]


@app.get("/api/icons")
async def list_icons(q: str = "") -> list[dict[str, str]]:
    query = q.strip().lower()
    icons = _icon_catalog()
    if not query:
        return icons
    return [
        item
        for item in icons
        if query in item["name"].lower() or query in item["slug"].lower()
    ]


@app.get("/api/tags", response_model=list[TagItem])
async def list_tags(request: Request) -> list[dict[str, Any]]:
    show_private = can_view_private(request)
    if show_private:
        sql = """
            SELECT tags.name, COUNT(site_tags.site_id) AS usage_count
            FROM tags
            LEFT JOIN site_tags ON site_tags.tag_id = tags.id
            GROUP BY tags.id
            HAVING COUNT(site_tags.site_id) > 0
            ORDER BY tags.name COLLATE NOCASE ASC;
            """
    else:
        # 公开视图：只统计公开站点，且不展示仅私有站点使用的标签名
        sql = """
            SELECT tags.name, COUNT(sites.id) AS usage_count
            FROM tags
            LEFT JOIN site_tags ON site_tags.tag_id = tags.id
            LEFT JOIN sites ON sites.id = site_tags.site_id AND sites.is_public = 1
            GROUP BY tags.id
            HAVING COUNT(sites.id) > 0
            ORDER BY tags.name COLLATE NOCASE ASC;
            """
    rows = await get_db().fetch_all(sql)
    return [{"name": row["name"], "count": int(row["usage_count"])} for row in rows]


@app.post("/api/site/ingest", response_model=ParseResponse)
async def ingest_site(request: Request, payload: BrowserIngestRequest) -> JSONResponse:
    auth_error = validate_ingest_token(request)
    if auth_error:
        return auth_error
    result = await process_browser_ingest(payload)
    status_code = 400 if result["status"] == "invalid" else 200
    return JSONResponse(status_code=status_code, content=result)


@app.put("/api/sites/reorder", response_model=MessageResponse)
async def reorder_sites(payload: ReorderRequest) -> JSONResponse:
    site_ids = payload.site_ids
    db = get_db()
    existing_rows = await db.fetch_all("SELECT id FROM sites")
    existing_ids = {row["id"] for row in existing_rows}
    for sid in site_ids:
        if sid not in existing_ids:
            return JSONResponse(
                status_code=400,
                content={"error": f"网站 ID {sid} 不存在"},
            )
    await db.batch(
        [
            ("UPDATE sites SET sort_order = ? WHERE id = ?", (idx, sid))
            for idx, sid in enumerate(site_ids, start=1)
        ]
    )
    return JSONResponse(status_code=200, content={"message": "ok"})


@app.put("/api/sites/{site_id}", response_model=SiteItem)
async def update_site(site_id: int, payload: SiteUpdateRequest) -> JSONResponse | dict[str, Any]:
    next_name = (payload.site_name or "").strip()
    raw_url = (payload.url or "").strip()
    if not next_name or not raw_url:
        return JSONResponse(status_code=400, content={"error": "名称和网址不能为空"})
    try:
        normalized_url = normalize_url(raw_url)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(exc)})

    icon_file = (payload.icon_file or "").strip()
    new_icon_rel_path = ""
    if icon_file:
        if "/" in icon_file or "\\" in icon_file or ".." in icon_file:
            return JSONResponse(status_code=400, content={"error": "无效的图标文件名"})
        # 验证 slug 是否为已知图标
        slug_lower = icon_file.lower()
        if not any(item["slug"].lower() == slug_lower for item in _icon_catalog()):
            return JSONResponse(status_code=400, content={"error": "图标不存在"})
        new_icon_rel_path = icon_url_for_slug(icon_file)

    try:
        normalized_tags = (
            normalize_tag_list(payload.tags) if payload.tags is not None else None
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    now = utc_now()
    db = get_db()
    row = await fetch_site_row(site_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "网站不存在"})
    old_icon_path = (row["icon_rel_path"] or "").strip()
    # 全部写入合并为一个 batch（SQLite 单事务 / D1 batch 原子），中途失败不会留下半截状态
    statements: list[tuple[str, tuple[Any, ...]]] = []
    if new_icon_rel_path:
        statements.append((
            """
            UPDATE sites
            SET url = ?, site_name = ?, icon_rel_path = ?,
                icon_source_url = ?, updated_at = ?
            WHERE id = ?;
            """,
            (normalized_url, next_name, new_icon_rel_path,
             icon_url_for_slug(icon_file), now, site_id),
        ))
    else:
        statements.append((
            """
            UPDATE sites
            SET url = ?, site_name = ?, updated_at = ?
            WHERE id = ?;
            """,
            (normalized_url, next_name, now, site_id),
        ))
    if payload.is_public is not None:
        statements.append((
            "UPDATE sites SET is_public = ? WHERE id = ?;",
            (1 if payload.is_public else 0, site_id),
        ))
    if normalized_tags is not None:
        existing_tag_ids = await fetch_tag_ids_by_casefold()
        statements.extend(site_tags_statements(site_id, normalized_tags, now, existing_tag_ids))
    # 清理无引用的孤儿标签（含历史残留）；与站点/标签写入同批保证原子
    statements.append(orphan_tags_statement())
    try:
        await db.batch(statements)
    except IntegrityConflictError:
        return JSONResponse(status_code=409, content={"error": "该网址已存在"})

    updated_row = await fetch_site_row(site_id)
    updated_tags = await fetch_site_tags(site_id) if updated_row else []

    if new_icon_rel_path:
        await maybe_remove_old_icon(old_icon_path, new_icon_rel_path)
    if not updated_row:
        return JSONResponse(status_code=404, content={"error": "网站不存在"})
    return to_site_item(updated_row, updated_tags)


@app.post("/api/sites/{site_id}/icon", response_model=SiteItem)
async def upload_site_icon(site_id: int, icon: UploadFile = File(...)) -> JSONResponse | dict[str, Any]:
    filename = (icon.filename or "").strip()
    if not filename:
        return JSONResponse(status_code=400, content={"error": "请上传图标文件"})
    if Path(filename).suffix.lower() not in config.ALLOWED_UPLOAD_ICON_EXTENSIONS:
        return JSONResponse(status_code=400, content={"error": "仅支持 ico、png、svg 格式图标"})

    content = await icon.read()
    if not content:
        return JSONResponse(status_code=400, content={"error": "图标内容为空"})
    if len(content) > config.ICON_UPLOAD_MAX_BYTES:
        return JSONResponse(status_code=400, content={"error": "图标大小不能超过 1MB"})
    if Path(filename).suffix.lower() == ".svg" and contains_active_svg_content(content):
        return JSONResponse(
            status_code=400,
            content={"error": "SVG 图标包含活动内容（script/事件属性），已拒绝"},
        )

    relative_path = Path("ICON") / icon_upload_filename(content, filename)

    now = utc_now()
    row = await fetch_site_row(site_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "网站不存在"})
    # 站点确认存在后再落盘，避免 404 时留下孤儿文件
    await storage.put(storage.ICONS, relative_path.name, content)
    old_icon_path = (row["icon_rel_path"] or "").strip()
    await get_db().run(
        """
        UPDATE sites
        SET icon_rel_path = ?, icon_source_url = ?, updated_at = ?
        WHERE id = ?;
        """,
        (relative_path.as_posix(), f"upload://{relative_path.name}", now, site_id),
    )
    updated_row = await fetch_site_row(site_id)
    updated_tags = await fetch_site_tags(site_id) if updated_row else []

    await maybe_remove_old_icon(old_icon_path, relative_path.as_posix())
    if not updated_row:
        return JSONResponse(status_code=404, content={"error": "网站不存在"})
    return to_site_item(updated_row, updated_tags)


@app.delete("/api/sites/{site_id}", response_model=MessageResponse)
async def delete_site(site_id: int) -> JSONResponse:
    icon_rel_path = ""
    db = get_db()
    row = await db.fetch_one("SELECT icon_rel_path FROM sites WHERE id = ?;", (site_id,))
    if not row:
        return JSONResponse(status_code=404, content={"error": "网站不存在"})
    icon_rel_path = (row["icon_rel_path"] or "").strip()
    # 站点删除与孤儿标签清理同批：不再留下 count=0 的死标签
    await db.batch([
        ("DELETE FROM sites WHERE id = ?;", (site_id,)),
        orphan_tags_statement(),
    ])

    await maybe_remove_old_icon(icon_rel_path, "")
    config.logger.info("删除网站: id=%d", site_id)
    return JSONResponse(status_code=200, content={"message": "ok"})


# ===== 书签导入/导出 =====

BOOKMARKS_IMPORT_MAX_BYTES = 5 * 1024 * 1024
IMPORT_BATCH_SITES = 50  # 每批提交的站点数，规避 D1 单请求语句数上限


def _normalize_import_tags(folders: list[str]) -> tuple[list[str], list[str]]:
    """书签文件夹名转标签：压缩空白、截断到标签长度上限、casefold 去重。

    返回 (标签列表, 被截断的原始名列表)——截断不再静默，由调用方并入响应 warning。
    """
    tags: list[str] = []
    seen: set[str] = set()
    truncated: list[str] = []
    for folder in folders:
        name = normalize_tag_name(folder)
        if len(name) > TAG_NAME_MAX_LEN:
            truncated.append(name)
            name = name[:TAG_NAME_MAX_LEN].rstrip()
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        tags.append(name)
    return tags, truncated


def _import_site_statements(
    url: str,
    site_name: str,
    tags: list[str],
    now: str,
    sort_order: int,
    is_public: bool,
    tag_ids_by_casefold: dict[str, int] | None = None,
) -> list[tuple[str, tuple[Any, ...]]]:
    """单个导入站点的写入语句组；site_id 用 URL 子查询取，不依赖插入返回值。

    tag_ids_by_casefold 提供库内已有标签索引：命中的标签直接挂既有行，
    非 ASCII 大小写变体也能合并（与站点更新路径同规则）。
    """
    tag_ids_by_casefold = tag_ids_by_casefold or {}
    statements: list[tuple[str, tuple[Any, ...]]] = [
        (
            "INSERT INTO sites ("
            "url, site_name, icon_rel_path, icon_source_url, "
            "created_at, updated_at, last_status, sort_order, is_public"
            ") VALUES (?, ?, '', '', ?, ?, 'imported', ?, ?)",
            (url, site_name, now, now, sort_order, 1 if is_public else 0),
        )
    ]
    for name in tags:
        existing_id = tag_ids_by_casefold.get(name.casefold())
        if existing_id is not None:
            statements.append((
                "INSERT OR IGNORE INTO site_tags (site_id, tag_id) "
                "SELECT (SELECT id FROM sites WHERE url = ?), ?",
                (url, existing_id),
            ))
            continue
        statements.append(
            ("INSERT OR IGNORE INTO tags (name, created_at) VALUES (?, ?)", (name, now))
        )
        statements.append(
            (
                "INSERT OR IGNORE INTO site_tags (site_id, tag_id) "
                "SELECT (SELECT id FROM sites WHERE url = ?), id "
                "FROM tags WHERE name = ? COLLATE NOCASE",
                (url, name),
            )
        )
    return statements


@app.get("/api/sites/export", response_model=None)
async def export_sites() -> Response:
    """导出全部站点（含私有）为可回灌浏览器的书签 HTML。

    导出的是全量数据（SQL 不做 is_public 过滤），开放模式（无 token）下一律 403，
    与浏览器采集接口同模式——它是唯一能静默读走全部私有数据的接口。
    """
    if not config.NAV_TOKEN:
        return JSONResponse(
            status_code=403,
            content={"error": "导出功能仅在门禁模式下可用，请先配置 NAV_TOKEN"},
        )
    rows = await get_db().fetch_all(
        f"SELECT {SITE_ITEM_COLUMNS} FROM sites ORDER BY sort_order ASC, id ASC;"
    )
    tags_by_site = await fetch_all_site_tags()
    filename = "nav-bookmarks-" + utc_now()[:10].replace("-", "") + ".html"
    return Response(
        content=render_bookmarks_html(rows, tags_by_site),
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/sites/import", response_model=None)
async def import_sites(
    file: UploadFile = File(...),
    is_public: bool = Form(False),
) -> JSONResponse:
    """导入浏览器导出的书签 HTML；重复 URL 跳过，文件夹转为标签。"""
    content = await file.read()
    if not content:
        return JSONResponse(status_code=400, content={"error": "请上传书签 HTML 文件"})
    if len(content) > BOOKMARKS_IMPORT_MAX_BYTES:
        return JSONResponse(status_code=400, content={"error": "书签文件大小不能超过 5MB"})
    entries = parse_bookmarks_html(content)
    total = len(entries)
    if not entries:
        return JSONResponse(
            status_code=400,
            content={"error": "未在文件中找到可导入的网站，请确认是浏览器导出的书签 HTML 文件"},
        )

    db = get_db()
    existing_rows = await db.fetch_all("SELECT url FROM sites")
    # 同一集合同时覆盖库内重复与文件内重复
    existing_urls = {(row["url"] or "").strip() for row in existing_rows}
    max_row = await db.fetch_one("SELECT COALESCE(MAX(sort_order), 0) AS max_sort FROM sites")
    next_sort = int((max_row or {}).get("max_sort") or 0) + 1
    now = utc_now()
    tags_by_casefold = await fetch_tag_ids_by_casefold()
    truncations: list[str] = []

    batch: list[tuple[str, tuple[Any, ...]]] = []
    staged = 0  # 本批已暂存、待提交确认的站点数
    imported = 0
    skipped = 0

    async def flush() -> None:
        nonlocal batch, staged, imported
        if not batch:
            return
        await db.batch(batch)
        imported += staged
        batch = []
        staged = 0

    try:
        for entry in entries:
            url = (entry["url"] or "").strip()
            if not url or url in existing_urls:
                skipped += 1
                continue
            existing_urls.add(url)
            site_tags, truncated = _normalize_import_tags(entry["folders"])
            truncations.extend(truncated)
            batch.extend(
                _import_site_statements(
                    url,
                    entry["title"],
                    site_tags,
                    now,
                    next_sort,
                    is_public,
                    tags_by_casefold,
                )
            )
            next_sort += 1
            staged += 1
            if staged >= IMPORT_BATCH_SITES:
                await flush()
        await flush()
    except Exception as exc:
        config.logger.error("书签导入失败: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error": f"导入中断：已写入 {imported} 个网站，其余未完成",
                "total": total,
                "imported": imported,
                "skipped": skipped,
            },
        )
    result: dict[str, Any] = {"total": total, "imported": imported, "skipped": skipped}
    if truncations:
        # 截断不再静默：去重后随响应告知（最多列 10 条）
        result["warnings"] = [f"标签超长已截断：{name}" for name in dict.fromkeys(truncations)][:10]
    return JSONResponse(content=result)


# ===== 插件市场（内置精选） =====
#
# 信任模型：插件永远运行在 sandbox="allow-scripts"（无 allow-same-origin）的 iframe
# 不透明源中，经 postMessage 桥使用宿主代理的能力；即使插件完全恶意，也接触不到
# token、书签与主页面 DOM。registry 成员校验是审查门禁的落地：未收录的插件不可加载。


def load_plugin_registry() -> list[dict[str, Any]]:
    """读取内置插件清单 plugins/registry.json（每次读取，不缓存：编辑清单即时生效）。

    Workers 上 Python 侧无文件系统（插件静态文件由 Workers Assets 服务，响应头见
    根 _headers 的 /plugins/* 段），返回空列表：API 端点跳过成员校验——数据端点已有
    TokenGuard 门禁且调用方本就是 owner，成员校验只在本地静态路由上强制。
    """
    try:
        data = json.loads((config.PLUGINS_DIR / "registry.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, list):
        return []
    return [entry for entry in plugins if isinstance(entry, dict)]


def _plugin_registry_entry(plugin_id: str) -> dict[str, Any] | None:
    for entry in load_plugin_registry():
        if entry.get("id") == plugin_id:
            return entry
    return None


def _valid_plugin_id(plugin_id: str) -> bool:
    return bool(config.PLUGIN_ID_RE.fullmatch(plugin_id))


@app.get("/plugins/registry.json", include_in_schema=False, response_model=None)
async def serve_plugin_registry(request: Request):
    """内置插件清单（公开静态：仅元数据，无敏感信息）。本地部署专用；Workers 由 Assets 服务。"""
    response = conditional_file_response(
        request, config.PLUGINS_DIR / "registry.json", "application/json", "no-cache"
    )
    if response is None:
        return _media_not_found()
    return response


@app.get("/plugins/{plugin_id}/{file_path:path}", include_in_schema=False, response_model=None)
async def serve_plugin_file(request: Request, plugin_id: str, file_path: str):
    """内置插件静态文件。

    三道门禁：id 格式、registry 成员、resolve 后必须仍位于该插件目录内
    （嵌套穿越与绝对路径替换均被拒绝，_safe_media_name 只适用单层文件名故不沿用）。
    插件文档运行在沙箱 iframe：显式下发 CSP_PLUGIN（安全头中间件不覆盖已存在的
    CSP）；no-cache + ETag 保证插件升级即时生效。
    """
    if not _valid_plugin_id(plugin_id):
        return _media_not_found()
    entry = _plugin_registry_entry(plugin_id)
    if entry is None:
        return _media_not_found()
    relative = file_path.strip()
    if not relative:
        relative = str(entry.get("entry") or "index.html").strip()
    plugin_root = (config.PLUGINS_DIR / plugin_id).resolve()
    target = (config.PLUGINS_DIR / plugin_id / relative).resolve()
    if not target.is_relative_to(plugin_root):
        return _media_not_found()
    response = conditional_file_response(
        request,
        target,
        media_type_for(target.name),
        "no-cache",
        {"Content-Security-Policy": config.CSP_PLUGIN},
    )
    if response is None:
        return _media_not_found()
    return response


@app.get("/api/plugins/installed", response_model=None)
async def list_installed_plugins() -> dict[str, dict[str, Any]]:
    """已安装插件状态（owner-only：不在 TokenGuard 公开只读白名单）。"""
    return await get_plugin_state()


@app.put("/api/plugins/{plugin_id}/installed", response_model=MessageResponse)
async def set_plugin_installed(plugin_id: str, payload: PluginStateRequest) -> JSONResponse:
    if not _valid_plugin_id(plugin_id):
        return JSONResponse(status_code=400, content={"error": "无效的插件 id"})
    if load_plugin_registry() and _plugin_registry_entry(plugin_id) is None:
        # registry 可读（本地部署）时强制成员校验；Workers 无文件系统，
        # 由 Assets 部署范围（_headers/静态目录）兜底
        return JSONResponse(status_code=404, content={"error": "插件不在内置清单中"})
    state = await get_plugin_state()
    record = state.get(plugin_id) if isinstance(state.get(plugin_id), dict) else {}
    state[plugin_id] = {
        "enabled": bool(payload.enabled),
        "installedAt": record.get("installedAt") or utc_now(),
    }
    await set_plugin_state(state)
    return JSONResponse(status_code=200, content={"message": "ok"})


@app.delete("/api/plugins/{plugin_id}/installed", response_model=MessageResponse)
async def uninstall_plugin(plugin_id: str, purge: bool = False) -> JSONResponse:
    if not _valid_plugin_id(plugin_id):
        return JSONResponse(status_code=400, content={"error": "无效的插件 id"})
    state = await get_plugin_state()
    state.pop(plugin_id, None)
    await set_plugin_state(state)
    if purge:
        await delete_plugin_data(plugin_id)
        # 浮动窗口布局注册表一并清除，避免重装后旧位置"复活"
        await delete_app_setting(f"plugin_windows:{plugin_id}")
    return JSONResponse(status_code=200, content={"message": "ok"})


@app.get("/api/plugins/{plugin_id}/data", response_model=None)
async def read_plugin_data(plugin_id: str, key: str) -> JSONResponse:
    """桥专用键值读取：宿主代持 token 调用，插件自身无法直接触达。"""
    if not _valid_plugin_id(plugin_id):
        return JSONResponse(status_code=400, content={"error": "无效的插件 id"})
    clean_key = key.strip()
    value = await fetch_plugin_data(plugin_id, clean_key)
    if value is None:
        return JSONResponse(status_code=404, content={"error": "数据不存在"})
    return JSONResponse(status_code=200, content={"key": clean_key, "value": value})


@app.put("/api/plugins/{plugin_id}/data", response_model=MessageResponse)
async def write_plugin_data(plugin_id: str, payload: PluginDataWriteRequest) -> JSONResponse:
    if not _valid_plugin_id(plugin_id):
        return JSONResponse(status_code=400, content={"error": "无效的插件 id"})
    key = payload.key.strip()
    if not key:
        return JSONResponse(status_code=400, content={"error": "数据键不能为空"})
    existing = await fetch_plugin_data(plugin_id, key)
    usage = await plugin_data_usage(plugin_id)
    next_count = usage["key_count"] + (0 if existing is not None else 1)
    if next_count > config.PLUGIN_DATA_MAX_KEYS:
        return JSONResponse(status_code=400, content={"error": "插件数据键数超出配额"})
    next_chars = usage["value_chars"] + len(payload.value) - len(existing or "")
    if next_chars > config.PLUGIN_DATA_MAX_BYTES:
        return JSONResponse(status_code=413, content={"error": "插件数据总量超出配额"})
    await upsert_plugin_data(plugin_id, key, payload.value)
    return JSONResponse(status_code=200, content={"message": "ok"})


@app.delete("/api/plugins/{plugin_id}/data", response_model=MessageResponse)
async def remove_plugin_data(plugin_id: str, key: str = "", prefix: str = "") -> JSONResponse:
    """删除插件数据；key=单键，prefix=实例前缀（浮动便签关单实例），都不带=清空全部。"""
    if not _valid_plugin_id(plugin_id):
        return JSONResponse(status_code=400, content={"error": "无效的插件 id"})
    clean_prefix = prefix.strip()
    if clean_prefix and not config.PLUGIN_ID_RE.fullmatch(clean_prefix.rstrip(":")):
        # 前缀将拼进 LIKE 模式：只放行 id 字符集与分隔符，杜绝通配符注入
        return JSONResponse(status_code=400, content={"error": "无效的数据前缀"})
    if key.strip():
        await delete_plugin_data(plugin_id, key.strip())
    elif clean_prefix:
        await delete_plugin_data(plugin_id, prefix=clean_prefix)
    else:
        await delete_plugin_data(plugin_id)
    return JSONResponse(status_code=200, content={"message": "ok"})


@app.get("/api/plugins/{plugin_id}/windows", response_model=None)
async def read_plugin_windows(plugin_id: str) -> JSONResponse:
    """浮动窗口布局注册表（宿主所有；插件经桥不可达）。键不存在返回 404=从未有过实例。"""
    if not _valid_plugin_id(plugin_id):
        return JSONResponse(status_code=400, content={"error": "无效的插件 id"})
    windows = await get_plugin_windows(plugin_id)
    if windows is None:
        return JSONResponse(status_code=404, content={"error": "暂无窗口布局"})
    return JSONResponse(status_code=200, content={"windows": windows})


@app.put("/api/plugins/{plugin_id}/windows", response_model=MessageResponse)
async def write_plugin_windows(plugin_id: str, payload: PluginWindowsRequest) -> JSONResponse:
    if not _valid_plugin_id(plugin_id):
        return JSONResponse(status_code=400, content={"error": "无效的插件 id"})
    windows = [item.model_dump() for item in payload.windows]
    await set_plugin_windows(plugin_id, windows)
    return JSONResponse(status_code=200, content={"message": "ok"})


# 增量端点的读-改-写靠每插件一把进程内写锁串行化（本地单进程 / Workers 单隔离体内
# 有效），替代旧全量 PUT 的"过期快照整键覆盖"竞态。
_window_write_locks: dict[str, asyncio.Lock] = {}


def _window_write_lock(plugin_id: str) -> asyncio.Lock:
    lock = _window_write_locks.get(plugin_id)
    if lock is None:
        lock = asyncio.Lock()
        _window_write_locks[plugin_id] = lock
    return lock


@app.post("/api/plugins/{plugin_id}/windows/add", response_model=None)
async def add_plugin_window_endpoint(plugin_id: str, payload: PluginWindowItem) -> JSONResponse:
    """新增（或按 id 幂等更新）单个窗口实例。"""
    if not _valid_plugin_id(plugin_id):
        return JSONResponse(status_code=400, content={"error": "无效的插件 id"})
    item = payload.model_dump()
    async with _window_write_lock(plugin_id):
        windows, rejected = await add_plugin_window(plugin_id, item)
    if rejected:
        return JSONResponse(status_code=409, content={"error": "窗口数量已达上限"})
    return JSONResponse(status_code=200, content={"windows": windows})


@app.patch("/api/plugins/{plugin_id}/windows/{inst_id}", response_model=None)
async def patch_plugin_window_endpoint(
    plugin_id: str, inst_id: str, payload: PluginWindowItem
) -> JSONResponse:
    """更新单个窗口实例的布局（拖拽/缩放结束时宿主调用）。"""
    if not _valid_plugin_id(plugin_id):
        return JSONResponse(status_code=400, content={"error": "无效的插件 id"})
    item = payload.model_dump()
    if item["id"] != inst_id:
        return JSONResponse(status_code=400, content={"error": "实例 id 与路径不一致"})
    async with _window_write_lock(plugin_id):
        windows = await update_plugin_window(plugin_id, item)
    if windows is None:
        return JSONResponse(status_code=404, content={"error": "窗口实例不存在"})
    return JSONResponse(status_code=200, content={"windows": windows})


@app.delete("/api/plugins/{plugin_id}/windows/{inst_id}", response_model=None)
async def delete_plugin_window_endpoint(plugin_id: str, inst_id: str) -> JSONResponse:
    """删除单个窗口实例；id 不存在视为幂等成功。键不存在（从未有过实例）返回 404。"""
    if not _valid_plugin_id(plugin_id):
        return JSONResponse(status_code=400, content={"error": "无效的插件 id"})
    async with _window_write_lock(plugin_id):
        windows = await delete_plugin_window(plugin_id, inst_id)
    if windows is None:
        return JSONResponse(status_code=404, content={"error": "暂无窗口布局"})
    return JSONResponse(status_code=200, content={"windows": windows})
