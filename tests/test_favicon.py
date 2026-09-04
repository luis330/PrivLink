from __future__ import annotations

import json
import struct
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# 路径 -> (期望 Content-Type, 期望像素尺寸；None 表示不是单张 PNG)
ROOT_ASSETS = {
    "/favicon.ico": ("image/x-icon", None),
    "/favicon-16x16.png": ("image/png", (16, 16)),
    "/favicon-32x32.png": ("image/png", (32, 32)),
    "/apple-touch-icon.png": ("image/png", (180, 180)),
    "/android-chrome-192x192.png": ("image/png", (192, 192)),
    "/android-chrome-512x512.png": ("image/png", (512, 512)),
    "/manifest.json": ("application/manifest+json", None),
}


class RootAssetRouteTest(unittest.TestCase):
    """根路径品牌资源。

    /favicon.ico 与 /apple-touch-icon.png 存在的意义是覆盖隐式请求——浏览器、
    爬虫和 iOS 不解析 <link rel="icon"> 就直接拉这两个根路径，缺路由就是常驻 404。
    """

    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def test_all_root_assets_are_served_with_long_cache(self) -> None:
        for path, (media_type, _) in ROOT_ASSETS.items():
            with self.subTest(path=path):
                response = self.client.get(path)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Content-Type"], media_type)
                self.assertEqual(response.headers["Cache-Control"], "public, max-age=86400")
                self.assertTrue(response.content)

    def test_png_assets_have_the_declared_pixel_size(self) -> None:
        # 尺寸声明与实际不符会让浏览器挑错图（如 iOS 主屏用到模糊的小图）
        for path, (_, size) in ROOT_ASSETS.items():
            if size is None:
                continue
            with self.subTest(path=path):
                data = self.client.get(path).content

                self.assertEqual(data[:8], PNG_MAGIC)
                self.assertEqual(struct.unpack(">II", data[16:24]), size)

    def test_favicon_ico_is_a_real_icon_container(self) -> None:
        # 生成器导出的 .ico 常是裸 PNG 改扩展名，与 image/x-icon 名实不符：
        # 隐式请求方多半是不认 PNG favicon 的老客户端
        data = self.client.get("/favicon.ico").content

        self.assertEqual(data[:4], b"\x00\x00\x01\x00")
        count = int.from_bytes(data[4:6], "little")
        self.assertGreaterEqual(count, 1)
        sizes = {data[6 + i * 16] or 256 for i in range(count)}
        self.assertIn(16, sizes)
        self.assertIn(32, sizes)

    def test_manifest_describes_this_app_and_resolvable_icons(self) -> None:
        manifest = self.client.get("/manifest.json").json()

        self.assertEqual(manifest["name"], "PrivLink")
        self.assertEqual(manifest["start_url"], "/")
        for icon in manifest["icons"]:
            with self.subTest(icon=icon["src"]):
                # manifest 里的图标路径必须真的能取到，否则 PWA 安装时静默失败
                self.assertEqual(self.client.get(icon["src"]).status_code, 200)

    def test_root_assets_answer_304_for_matching_etag(self) -> None:
        for path in ROOT_ASSETS:
            with self.subTest(path=path):
                etag = self.client.get(path).headers["ETag"]

                response = self.client.get(path, headers={"If-None-Match": etag})

                self.assertEqual(response.status_code, 304)
                self.assertEqual(response.headers["ETag"], etag)

    def test_homepage_declares_the_icon_set(self) -> None:
        html = self.client.get("/").text

        self.assertIn('<link rel="icon" href="/favicon.ico" sizes="16x16 32x32" />', html)
        self.assertIn('href="/favicon-32x32.png"', html)
        self.assertIn('rel="apple-touch-icon"', html)
        self.assertIn('<link rel="manifest" href="/manifest.json" />', html)
        # 前端不再引用 SVG 图标，避免与位图版本出现两套视觉
        self.assertNotIn("favicon.svg", html)

    def test_root_assets_are_public_under_token_guard(self) -> None:
        # 门禁 middleware 只拦 /api/，图标不能被挡在 token 后面（否则访客页面无图标）
        with patch.object(main, "NAV_TOKEN", "secret-token"):
            for path in ROOT_ASSETS:
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 200)

    def test_manifest_is_valid_json_on_disk(self) -> None:
        # 与线上响应同源校验，避免只在内存里改对
        with open("manifest.json", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["short_name"], "PrivLink")


if __name__ == "__main__":
    unittest.main()
