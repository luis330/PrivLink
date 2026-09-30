from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from privlink import config
from privlink.models import error_payload

_static_file_cache: dict[Path, tuple[float, bytes, str]] = {}


def load_cached_file(path: Path) -> tuple[bytes, str] | None:
    # 缓存小体积静态文件的内容与内容 ETag，mtime 变化时自动重新加载
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _static_file_cache.get(path)
    if cached is None or cached[0] != mtime:
        body = path.read_bytes()
        cached = (mtime, body, f'"{hashlib.md5(body).hexdigest()}"')
        _static_file_cache[path] = cached
    return cached[1], cached[2]


def media_type_for(filename: str) -> str:
    """按扩展名推断 Content-Type。

    .ico 固定为 image/x-icon：mimetypes 在 Windows 读注册表得 image/x-icon，在
    Linux / Pyodide 用内置表得 image/vnd.microsoft.icon，两端写入 R2 的元数据会不一致。
    """
    if filename.lower().endswith(".ico"):
        return "image/x-icon"
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def etag_matches(if_none_match: str, etag: str) -> bool:
    """If-None-Match 弱比较（忽略 W/ 前缀与引号），支持逗号列表与 *。"""
    if not if_none_match or not etag:
        return False
    received = {tag.strip().removeprefix("W/").strip('"') for tag in if_none_match.split(",")}
    return "*" in received or etag.strip().removeprefix("W/").strip('"') in received


def conditional_file_response(
    request: Request,
    path: Path,
    media_type: str,
    cache_control: str,
    extra_headers: dict[str, str] | None = None,
) -> Response | None:
    """带 ETag 协商的静态文件响应；文件缺失时返回 None，由调用方决定错误语义。"""
    cached = load_cached_file(path)
    if cached is None:
        return None
    body, etag = cached
    headers = {"ETag": etag, "Cache-Control": cache_control}
    if extra_headers:
        headers.update(extra_headers)
    if etag_matches(request.headers.get("if-none-match", ""), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type=media_type, headers=headers)


def root_asset_response(request: Request, filename: str, media_type: str):
    """服务根路径的品牌静态资源（图标与 manifest）。

    本地部署专用：Workers 部署下这些文件由 Workers Assets（assets/ 目录）按文件名
    直接命中，不会进到这里。Python 端没有根目录静态挂载（挂 "/" 会截获全部路由），
    因此逐个显式声明。
    """
    response = conditional_file_response(
        request, config.ROOT_ASSET_DIR / filename, media_type, "public, max-age=86400"
    )
    if response is None:
        return JSONResponse(status_code=404, content=error_payload(f"{filename} is missing"))
    return response
