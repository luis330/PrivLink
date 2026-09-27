from __future__ import annotations

from collections.abc import AsyncIterator
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from privlink import config
from privlink.jsinterop import js_field, maybe_await
from privlink.staticfiles import etag_matches, media_type_for

# R2 绑定注入（Workers 由中间件从 request.scope["env"] 注入；本地为 None 走文件系统）
_buckets_var: ContextVar[tuple[Any, Any] | None] = ContextVar("privlink_buckets", default=None)


def bind_buckets(icons: Any, backgrounds: Any) -> Token:
    return _buckets_var.set((icons, backgrounds))


def unbind_buckets(token: Token) -> None:
    _buckets_var.reset(token)


@dataclass(frozen=True)
class StorageArea:
    """一类媒体文件的存放位置：Workers 上是 R2 桶，本地是 config 中的目录。

    R2 key 带目录前缀（ICON/<file>、background/<file>，即 URL 路径去掉开头的 "/"），
    与 TS 实现写入的线上数据一致，不可改动；对外接口一律收发裸文件名。
    """

    binding: str  # wrangler.jsonc 中的 binding 名，仅用于报错
    slot: int  # 在 bind_buckets() 元组中的位置
    key_prefix: str
    dir_attr: str  # config 中本地目录的属性名（按属性读取，测试可重绑）

    def bucket(self) -> Any | None:
        """R2 桶；本地返回 None。Workers 上缺少 binding 直接报错。"""
        buckets = _buckets_var.get()
        bucket = buckets[self.slot] if buckets else None
        if bucket is None and config.IS_WORKERS:
            # Workers 没有持久文件系统：静默回落本地目录会让读取判「不存在」（进而改错设置）、
            # 写入看似成功实则丢失，必须显式失败
            raise RuntimeError(f"R2 binding {self.binding} 未注入")
        return bucket

    def local_dir(self) -> Path:
        return getattr(config, self.dir_attr)


ICONS = StorageArea(binding="ICON_BUCKET", slot=0, key_prefix="ICON/", dir_attr="ICON_DIR")
BACKGROUNDS = StorageArea(
    binding="BACKGROUND_BUCKET", slot=1, key_prefix="background/", dir_attr="BACKGROUND_DIR"
)


@dataclass(frozen=True)
class StoredObject:
    """R2 对象读取结果；body 为 None 表示 If-None-Match 命中（304，未读取正文）。"""

    content_type: str
    etag: str
    body: AsyncIterator[bytes] | None


def _object_meta(obj: Any, filename: str) -> tuple[str, str]:
    metadata = js_field(obj, "httpMetadata")
    content_type = js_field(metadata, "contentType") or media_type_for(filename)
    etag = js_field(obj, "httpEtag") or js_field(obj, "etag") or ""
    return str(content_type), str(etag)


async def _iter_body(obj: Any) -> AsyncIterator[bytes]:
    """逐块转发 R2 正文（ReadableStream），不把整个对象读进 Worker 内存。"""
    get_reader = js_field(js_field(obj, "body"), "getReader")
    if not callable(get_reader):
        # 无流式正文的对象（测试替身）：一次性读取
        yield bytes(await maybe_await(obj.arrayBuffer()))
        return
    reader = get_reader()
    while True:
        chunk = await maybe_await(reader.read())
        if js_field(chunk, "done", False):
            break
        value = js_field(chunk, "value")
        if value is not None:
            # Workers 上是 Uint8Array（JsProxy.to_bytes）；替身直接给 bytes
            to_bytes = getattr(value, "to_bytes", None)
            yield to_bytes() if callable(to_bytes) else bytes(value)


async def put(area: StorageArea, filename: str, data: bytes) -> None:
    bucket = area.bucket()
    if bucket is not None:
        await maybe_await(
            bucket.put(area.key_prefix + filename, data, httpMetadata={"contentType": media_type_for(filename)})
        )
        return
    path = area.local_dir() / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


async def get(area: StorageArea, filename: str, if_none_match: str = "") -> StoredObject | None:
    """仅 R2 使用（本地走 conditional_file_response）；对象缺失时返回 None。"""
    bucket = area.bucket()
    if bucket is None:
        return None
    key = area.key_prefix + filename
    if if_none_match:
        # 先 head 比对 ETag：命中 304 时不拉取正文
        meta = await maybe_await(bucket.head(key))
        if meta is None:
            return None
        content_type, etag = _object_meta(meta, filename)
        if etag_matches(if_none_match, etag):
            return StoredObject(content_type, etag, None)
    obj = await maybe_await(bucket.get(key))
    if obj is None:
        return None
    content_type, etag = _object_meta(obj, filename)
    return StoredObject(content_type, etag, _iter_body(obj))


async def exists(area: StorageArea, filename: str) -> bool:
    bucket = area.bucket()
    if bucket is None:
        return (area.local_dir() / filename).is_file()
    return await maybe_await(bucket.head(area.key_prefix + filename)) is not None


async def delete(area: StorageArea, filename: str) -> None:
    bucket = area.bucket()
    if bucket is not None:
        await maybe_await(bucket.delete(area.key_prefix + filename))
        return
    (area.local_dir() / filename).unlink(missing_ok=True)


def _uploaded_timestamp(uploaded: Any) -> float:
    """R2 的 uploaded 是 JS Date：getTime() 为毫秒。"""
    get_time = js_field(uploaded, "getTime")
    return float(get_time()) / 1000 if callable(get_time) else 0.0


async def list_entries(area: StorageArea) -> list[tuple[str, int, float]]:
    """返回 (文件名, 大小, 排序时间戳)，按时间倒序；本地按 mtime，R2 按 uploaded。"""
    bucket = area.bucket()
    entries: list[tuple[str, int, float]] = []
    if bucket is None:
        directory = area.local_dir()
        if directory.is_dir():
            for file in directory.iterdir():
                if file.is_file():
                    stat = file.stat()
                    entries.append((file.name, stat.st_size, stat.st_mtime))
    else:
        result = await maybe_await(bucket.list(prefix=area.key_prefix))
        for obj in js_field(result, "objects") or []:
            key = str(js_field(obj, "key") or "")
            if not key.startswith(area.key_prefix):
                continue
            entries.append((
                key[len(area.key_prefix):],
                int(js_field(obj, "size", 0) or 0),
                _uploaded_timestamp(js_field(obj, "uploaded")),
            ))
    entries.sort(key=lambda item: item[2], reverse=True)
    return entries
