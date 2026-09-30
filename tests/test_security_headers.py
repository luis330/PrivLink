from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import main
from privlink import config
from privlink.app import TokenGuard
from privlink.db import init_storage


class SecurityHeadersTestCase(unittest.TestCase):
    """安全响应头 / CORS / 限速 / 导出收口 / 500 脱敏的回归测试。

    在临时目录中运行应用，token 与 CORS 白名单由 config 属性直接控制
    （与 test_ingest.IsolatedAppTestCase 同款隔离方式）。
    """

    nav_token = "secret-token"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = Path.cwd()
        self.old_db_path = config.DB_PATH
        self.old_icon_dir = config.ICON_DIR
        self.old_frontend_path = config.FRONTEND_PATH
        self.old_token = config.NAV_TOKEN
        self.old_cors_origins = config.CORS_ALLOWED_ORIGINS
        self.old_fail_window = config.NAV_AUTH_FAIL_WINDOW
        self.old_fail_max = config.NAV_AUTH_FAIL_MAX
        self.old_lockout = config.NAV_AUTH_LOCKOUT_SECONDS

        os.chdir(self.temp_dir.name)
        config.DB_PATH = Path("data") / "sites.db"
        config.ICON_DIR = Path("ICON")
        config.FRONTEND_PATH = self.old_cwd / "index.html"
        config.NAV_TOKEN = self.nav_token
        # 限速是类级内存状态，必须逐用例清零，避免测试顺序耦合
        TokenGuard._failures.clear()
        TokenGuard._locked_until.clear()
        init_storage()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        config.DB_PATH = self.old_db_path
        config.ICON_DIR = self.old_icon_dir
        config.FRONTEND_PATH = self.old_frontend_path
        config.NAV_TOKEN = self.old_token
        config.CORS_ALLOWED_ORIGINS = self.old_cors_origins
        config.NAV_AUTH_FAIL_WINDOW = self.old_fail_window
        config.NAV_AUTH_FAIL_MAX = self.old_fail_max
        config.NAV_AUTH_LOCKOUT_SECONDS = self.old_lockout
        TokenGuard._failures.clear()
        TokenGuard._locked_until.clear()
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    # ===== 安全响应头 =====

    def test_security_headers_on_homepage(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(
            response.headers["referrer-policy"], "strict-origin-when-cross-origin"
        )
        self.assertEqual(
            response.headers["permissions-policy"], "camera=(), microphone=(), geolocation=()"
        )
        self.assertEqual(response.headers["x-frame-options"], "SAMEORIGIN")
        self.assertEqual(response.headers["content-security-policy"], config.CSP_PAGE)
        # HSTS 由 Cloudflare 边缘统一下发，应用层不发，避免重复响应头
        self.assertNotIn("strict-transport-security", response.headers)

    def test_security_headers_on_api_and_401(self) -> None:
        response = self.client.get("/api/sites")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-security-policy"], config.CSP_PAGE)
        self.assertEqual(response.headers["x-frame-options"], "SAMEORIGIN")

        # 401 短路响应也必须带安全头（SecurityHeaders 在 TokenGuard 外层的证明）
        response = self.client.get("/api/icons")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["content-security-policy"], config.CSP_PAGE)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        # 401 不再携带通配 CORS 头
        self.assertNotIn("access-control-allow-origin", response.headers)

    # ===== API 文档关闭 =====

    def test_docs_disabled_by_default(self) -> None:
        for path in ("/docs", "/redoc", "/openapi.json"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404, path)

    # ===== CORS =====

    def test_cors_default_blocks_cross_origin(self) -> None:
        # 预检：白名单未配置时不返回任何 CORS 头（无中间件时 OPTIONS 命中 405 亦同）
        response = self.client.options(
            "/api/sites",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "DELETE",
                "Access-Control-Request-Headers": "x-nav-token",
            },
        )
        self.assertNotIn("access-control-allow-origin", response.headers)

        response = self.client.get("/api/sites", headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_cors_allowlist_echoes_configured_origin(self) -> None:
        config.CORS_ALLOWED_ORIGINS = ("https://partner.example",)

        response = self.client.options(
            "/api/sites",
            headers={
                "Origin": "https://partner.example",
                "Access-Control-Request-Method": "DELETE",
                "Access-Control-Request-Headers": "x-nav-token",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "https://partner.example")
        self.assertIn("DELETE", response.headers["access-control-allow-methods"])
        self.assertIn("x-nav-token", response.headers["access-control-allow-headers"].lower())

        response = self.client.get(
            "/api/sites", headers={"Origin": "https://partner.example"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "https://partner.example")

        # 白名单外的 Origin 不获得 ACAO
        response = self.client.get("/api/sites", headers={"Origin": "https://other.example"})
        self.assertNotIn("access-control-allow-origin", response.headers)

    # ===== Token 失败限速 =====

    def test_rate_limit_locks_after_failures_and_valid_token_unlocks(self) -> None:
        config.NAV_AUTH_FAIL_WINDOW = 60
        config.NAV_AUTH_FAIL_MAX = 3
        config.NAV_AUTH_LOCKOUT_SECONDS = 900

        for _ in range(3):
            response = self.client.get("/api/icons", headers={"X-Nav-Token": "wrong"})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"error": "需要访问 token"})

        response = self.client.get("/api/icons", headers={"X-Nav-Token": "wrong"})
        self.assertEqual(response.status_code, 429)
        self.assertIn("retry-after", response.headers)

        # 持有效 token 的请求不受锁定影响，且清零失败计数
        response = self.client.get("/api/icons", headers={"X-Nav-Token": self.nav_token})
        self.assertEqual(response.status_code, 200)

        response = self.client.get("/api/icons", headers={"X-Nav-Token": "wrong"})
        self.assertEqual(response.status_code, 401)

    # ===== 开放模式导出收口 =====

    def test_export_disabled_in_open_mode(self) -> None:
        config.NAV_TOKEN = ""
        response = self.client.get("/api/sites/export")
        self.assertEqual(response.status_code, 403)

    # ===== 500 响应脱敏 =====

    def test_500_response_hides_exception_detail(self) -> None:
        # ServerErrorMiddleware 发送 500 后会向上重抛异常，TestClient 默认跟随抛出，
        # 故用 raise_server_exceptions=False 断言实际发出去的响应体
        client = TestClient(main.app, raise_server_exceptions=False)
        with mock.patch("privlink.app.get_db", side_effect=RuntimeError("boom-db-detail")):
            response = client.get("/api/sites")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("boom-db-detail", response.text)
        self.assertIn("Internal server error", response.text)

    # ===== SVG 收口 =====

    def test_svg_media_served_with_sandbox_csp(self) -> None:
        icon_path = Path("ICON") / "test-icon.svg"
        icon_path.write_text('<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>', encoding="utf-8")
        response = self.client.get("/ICON/test-icon.svg")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/svg+xml")
        self.assertEqual(response.headers["content-security-policy"], config.CSP_SVG)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_svg_upload_with_active_content_rejected(self) -> None:
        malicious = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        response = self.client.post(
            "/api/sites/999/icon",
            files={"icon": ("evil.svg", malicious, "image/svg+xml")},
            headers={"X-Nav-Token": self.nav_token},
        )
        self.assertEqual(response.status_code, 400)

        # 事件属性分支（无 <script> 也必须被拒）
        onerror = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><circle r="4"/></svg>'
        response = self.client.post(
            "/api/sites/999/icon",
            files={"icon": ("evil2.svg", onerror, "image/svg+xml")},
            headers={"X-Nav-Token": self.nav_token},
        )
        self.assertEqual(response.status_code, 400)

        # 良性 SVG 通过内容检测（该路径后续因站点不存在返回 404，证明不是内容被拒）；
        # data-one= 等形似事件属性的良性内容不得误报
        benign = (
            b'<svg xmlns="http://www.w3.org/2000/svg" data-one="1"><circle r="4"/></svg>'
        )
        response = self.client.post(
            "/api/sites/999/icon",
            files={"icon": ("ok.svg", benign, "image/svg+xml")},
            headers={"X-Nav-Token": self.nav_token},
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
