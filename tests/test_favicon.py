from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class FaviconRouteTest(unittest.TestCase):
    """站点默认图标的两条路由。

    /favicon.ico 存在的意义是覆盖浏览器与爬虫对根路径的隐式请求——它们不解析
    <link rel="icon">，直接拉 /favicon.ico，缺这条路由就是常驻 404。
    """

    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def test_serves_svg_favicon_with_long_cache(self) -> None:
        response = self.client.get("/favicon.svg")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "image/svg+xml")
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=86400")
        self.assertIn(b"<svg", response.content)

    def test_serves_ico_favicon_for_implicit_requests(self) -> None:
        response = self.client.get("/favicon.ico")

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.status_code, 404)
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=86400")
        self.assertTrue(response.content)

    def test_ico_is_a_real_icon_container(self) -> None:
        # 位图版本必须是真正的 ICO，不能拿 SVG 冒充：隐式请求方多半不支持 SVG 图标
        response = self.client.get("/favicon.ico")

        self.assertEqual(response.headers["Content-Type"], "image/x-icon")
        # ICO 文件头：reserved=0, type=1, 图像张数 >= 1
        header = response.content[:6]
        self.assertEqual(header[:4], b"\x00\x00\x01\x00")
        self.assertGreaterEqual(int.from_bytes(header[4:6], "little"), 1)

    def test_favicons_answer_304_for_matching_etag(self) -> None:
        for path in ("/favicon.svg", "/favicon.ico"):
            with self.subTest(path=path):
                first = self.client.get(path)
                etag = first.headers["ETag"]

                second = self.client.get(path, headers={"If-None-Match": etag})

                self.assertEqual(second.status_code, 304)
                self.assertEqual(second.headers["ETag"], etag)

    def test_homepage_references_both_favicons(self) -> None:
        html = self.client.get("/").text

        self.assertIn('<link rel="icon" type="image/svg+xml" href="/favicon.svg" />', html)
        self.assertIn('href="/favicon.ico"', html)

    def test_favicons_are_public_under_token_guard(self) -> None:
        # 门禁 middleware 只拦 /api/，图标不能被挡在 token 后面（否则访客页面无图标）
        with patch.object(main, "NAV_TOKEN", "secret-token"):
            for path in ("/favicon.svg", "/favicon.ico"):
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 200)


if __name__ == "__main__":
    unittest.main()
