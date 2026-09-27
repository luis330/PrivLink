from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from privlink import config, storage


class FakeR2Object:
    def __init__(self, key: str, data: bytes, content_type: str | None) -> None:
        self.key = key
        self.size = len(data)
        self.uploaded = None
        self.httpEtag = f'"{key}-etag"'
        self.httpMetadata = {"contentType": content_type} if content_type else None
        self._data = data

    async def arrayBuffer(self) -> bytes:
        return self._data


class FakeR2Bucket:
    """内存版 R2：只实现 storage 用到的 put/get/head/delete/list；记录 get 次数。"""

    def __init__(self) -> None:
        self.objects: dict[str, FakeR2Object] = {}
        self.get_calls = 0

    async def put(self, key: str, data: bytes, httpMetadata: dict | None = None) -> None:
        self.objects[key] = FakeR2Object(key, data, (httpMetadata or {}).get("contentType"))

    async def get(self, key: str) -> FakeR2Object | None:
        self.get_calls += 1
        return self.objects.get(key)

    async def head(self, key: str) -> FakeR2Object | None:
        return self.objects.get(key)

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def list(self, prefix: str = "") -> dict:
        return {"objects": [o for k, o in self.objects.items() if k.startswith(prefix)]}


async def read_body(obj: storage.StoredObject) -> bytes:
    assert obj.body is not None
    return b"".join([chunk async for chunk in obj.body])


class R2KeyLayoutTestCase(unittest.TestCase):
    """R2 key 带 ICON/、background/ 前缀：与 TS 端写入的线上数据保持一致。"""

    def setUp(self) -> None:
        self.icons = FakeR2Bucket()
        self.backgrounds = FakeR2Bucket()
        self.token = storage.bind_buckets(self.icons, self.backgrounds)

    def tearDown(self) -> None:
        storage.unbind_buckets(self.token)

    def test_icon_keys_are_prefixed(self) -> None:
        asyncio.run(storage.put(storage.ICONS, "a.png", b"png-bytes"))
        self.assertEqual(list(self.icons.objects), ["ICON/a.png"])

        obj = asyncio.run(storage.get(storage.ICONS, "a.png"))
        self.assertIsNotNone(obj)
        self.assertEqual(asyncio.run(read_body(obj)), b"png-bytes")
        self.assertEqual(obj.content_type, "image/png")

        asyncio.run(storage.delete(storage.ICONS, "a.png"))
        self.assertEqual(self.icons.objects, {})

    def test_ico_content_type_is_fixed(self) -> None:
        # 与 TS 端、本地路由一致；不随平台 mimetypes 表变化
        asyncio.run(storage.put(storage.ICONS, "a.ico", b"ico"))
        self.assertEqual(self.icons.objects["ICON/a.ico"].httpMetadata, {"contentType": "image/x-icon"})

    def test_existing_ts_icon_is_readable(self) -> None:
        # 线上 TS 端写入的对象：key 即 icon_rel_path（"ICON/<file>"）
        asyncio.run(self.icons.put("ICON/legacy.svg", b"<svg/>", {"contentType": "image/svg+xml"}))
        obj = asyncio.run(storage.get(storage.ICONS, "legacy.svg"))
        self.assertIsNotNone(obj)
        self.assertEqual(asyncio.run(read_body(obj)), b"<svg/>")

    def test_conditional_get_skips_body(self) -> None:
        asyncio.run(storage.put(storage.ICONS, "a.png", b"png-bytes"))
        etag = asyncio.run(storage.get(storage.ICONS, "a.png")).etag
        self.icons.get_calls = 0

        obj = asyncio.run(storage.get(storage.ICONS, "a.png", etag))
        self.assertIsNone(obj.body)
        self.assertEqual(obj.etag, etag)
        self.assertEqual(self.icons.get_calls, 0)  # 只 head，不拉正文

    def test_missing_object_returns_none(self) -> None:
        self.assertIsNone(asyncio.run(storage.get(storage.ICONS, "nope.png")))
        self.assertIsNone(asyncio.run(storage.get(storage.ICONS, "nope.png", '"x"')))

    def test_background_keys_are_prefixed(self) -> None:
        name = "bg-0123456789abcdef01234567.png"
        asyncio.run(storage.put(storage.BACKGROUNDS, name, b"bg"))
        self.assertEqual(list(self.backgrounds.objects), [f"background/{name}"])
        self.assertTrue(asyncio.run(storage.exists(storage.BACKGROUNDS, name)))

        # 前缀外的对象（如早期误写的裸 key）不出现在列表里
        asyncio.run(self.backgrounds.put("stray.png", b"x"))
        entries = asyncio.run(storage.list_entries(storage.BACKGROUNDS))
        self.assertEqual([e[0] for e in entries], [name])

        asyncio.run(storage.delete(storage.BACKGROUNDS, name))
        self.assertFalse(asyncio.run(storage.exists(storage.BACKGROUNDS, name)))


class StreamingBodyTestCase(unittest.TestCase):
    """Workers 上 R2 正文是 ReadableStream：逐块转发，不整体读入内存。"""

    def test_reads_stream_chunks(self) -> None:
        class Chunk:
            def __init__(self, value: bytes | None) -> None:
                self.done = value is None
                self.value = value

        class Reader:
            def __init__(self, parts: list[bytes]) -> None:
                self._parts = list(parts)

            async def read(self) -> Chunk:
                return Chunk(self._parts.pop(0) if self._parts else None)

        class StreamObject:
            httpEtag = '"e"'
            httpMetadata = {"contentType": "image/png"}

            class body:  # noqa: N801 - 模拟 JS 属性
                @staticmethod
                def getReader() -> Reader:
                    return Reader([b"ab", b"cd"])

        class Bucket(FakeR2Bucket):
            async def get(self, key: str):
                return StreamObject()

        token = storage.bind_buckets(Bucket(), None)
        try:
            obj = asyncio.run(storage.get(storage.ICONS, "a.png"))
            chunks = asyncio.run(self._collect(obj))
        finally:
            storage.unbind_buckets(token)
        self.assertEqual(chunks, [b"ab", b"cd"])

    @staticmethod
    async def _collect(obj: storage.StoredObject) -> list[bytes]:
        return [chunk async for chunk in obj.body]


class MissingBindingTestCase(unittest.TestCase):
    def test_workers_without_binding_fails_loudly(self) -> None:
        # 静默回落 Pyodide 临时文件系统会让读取判「不存在」、写入假成功（§0.1 事故路径）
        token = storage.bind_buckets(FakeR2Bucket(), None)
        try:
            with mock.patch.object(config, "IS_WORKERS", True):
                with self.assertRaises(RuntimeError):
                    asyncio.run(storage.exists(storage.BACKGROUNDS, "x.png"))
                with self.assertRaises(RuntimeError):
                    asyncio.run(storage.put(storage.BACKGROUNDS, "x.png", b"x"))
        finally:
            storage.unbind_buckets(token)


if __name__ == "__main__":
    unittest.main()
