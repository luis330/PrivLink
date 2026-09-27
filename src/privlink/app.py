from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
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
from privlink.db import (
    SITE_ITEM_COLUMNS,
    D1Database,
    IntegrityConflictError,
    bind_db,
    ensure_remote_schema,
    fetch_all_site_tags,
    fetch_site_row,
    fetch_site_tags,
    get_db,
    init_storage,
    normalize_tag_list,
    site_tags_statements,
    unbind_db,
    utc_now,
)
from privlink.fetcher import PublicIPv4LookupError
from privlink.icons import icon_url_for_slug, icon_upload_filename, list_simple_icons
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
    if config.IS_WORKERS:
        # Workers 无持久本地磁盘：DDL 由 PlatformBindings 在首个请求执行
        config.logger.info("Workers 模式：跳过本地目录/SQLite 初始化")
    else:
        init_storage()
    _icons_cache = list_simple_icons()
    config.logger.info("服务启动完成，图标库加载 %d 个图标", len(_icons_cache))
    yield


app = FastAPI(title="PrivLink", version="1.0.0", lifespan=app_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
if not config.IS_WORKERS:
    # Workers 内建压缩，重复 GZip 中间件会破坏 Pyodide 响应流
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


class TokenGuard:
    """纯 ASGI 令牌守卫（不走 BaseHTTPMiddleware，避免流式响应问题）。"""

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
                if not token_header_matches(headers.get("x-nav-token")):
                    body = json.dumps(
                        {"error": "需要访问 token"},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 401,
                            "headers": [
                                (b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode("latin-1")),
                                (b"access-control-allow-origin", b"*"),
                            ],
                        }
                    )
                    await send({"type": "http.response.body", "body": body})
                    return
        await self.app(scope, receive, send)


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
            # 不沿用上一请求的值），供 TokenGuard/auth 属性访问读取。本中间件须注册在 TokenGuard 外层。
            token = js_field(env, "NAV_TOKEN") or js_field(env, "NAV_INGEST_TOKEN")
            config.NAV_TOKEN = token.strip() if isinstance(token, str) else ""
            mode = js_field(env, "NAV_MODE")
            config.NAV_MODE = (mode.strip().lower() if isinstance(mode, str) else "") or "single"
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
# 后注册 = 最外层：必须先于 TokenGuard 注入 vars/config
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
    if area.bucket() is None:
        response = conditional_file_response(
            request, area.local_dir() / file_path, media_type_for(file_path), cache_control
        )
        return response if response is not None else _media_not_found()
    entry = await storage.get(area, file_path, request.headers.get("if-none-match", ""))
    if entry is None:
        return _media_not_found()
    headers = {"Cache-Control": cache_control}
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
    return JSONResponse(status_code=500, content=error_payload(f"Internal server error: {exc}"))


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
        statements.extend(site_tags_statements(site_id, normalized_tags, now))
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
    await db.run("DELETE FROM sites WHERE id = ?;", (site_id,))

    await maybe_remove_old_icon(icon_rel_path, "")
    config.logger.info("删除网站: id=%d", site_id)
    return JSONResponse(status_code=200, content={"message": "ok"})
