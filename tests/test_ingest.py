from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import main


PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class CollectorScriptTest(unittest.TestCase):
    def test_tampermonkey_collector_runs_only_in_top_frame(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "collectors" / "nav-local.user.js"
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("// @noframes", script)
        self.assertIn("window.top !== window.self", script)
        self.assertLess(
            script.index("window.top !== window.self"),
            script.index('GM_registerMenuCommand("保存当前页到 Nav Local"'),
        )


class IsolatedAppTestCase(unittest.TestCase):
    """在临时目录中运行应用，token 由 main.NAV_TOKEN 直接控制（env 权威语义）。"""

    nav_token = "secret-token"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = Path.cwd()
        self.old_db_path = main.DB_PATH
        self.old_icon_dir = main.ICON_DIR
        self.old_frontend_path = main.FRONTEND_PATH
        self.old_token = main.NAV_TOKEN

        os.chdir(self.temp_dir.name)
        main.DB_PATH = Path("data") / "sites.db"
        main.ICON_DIR = Path("ICON")
        main.FRONTEND_PATH = self.old_cwd / "index.html"
        main.NAV_TOKEN = self.nav_token
        Path("icons").mkdir(exist_ok=True)
        main.init_storage()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        main.DB_PATH = self.old_db_path
        main.ICON_DIR = self.old_icon_dir
        main.FRONTEND_PATH = self.old_frontend_path
        main.NAV_TOKEN = self.old_token
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()


class TokenGuardTest(IsolatedAppTestCase):
    def test_api_requires_token_when_configured(self) -> None:
        response = self.client.get("/api/sites")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "需要访问 token"})

        response = self.client.get("/api/sites", headers={"X-Nav-Token": "wrong"})
        self.assertEqual(response.status_code, 401)

        response = self.client.get("/api/sites", headers={"X-Nav-Token": "secret-token"})
        self.assertEqual(response.status_code, 200)

    def test_open_mode_allows_api_without_token(self) -> None:
        main.NAV_TOKEN = ""
        response = self.client.get("/api/sites")
        self.assertEqual(response.status_code, 200)

    def test_homepage_and_static_stay_public(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

        # 静态挂载不经过 /api/ 门禁（404 也证明未被 401 拦截）
        response = self.client.get("/icons/nonexistent.svg")
        self.assertNotEqual(response.status_code, 401)
        response = self.client.get("/ICON/nonexistent.png")
        self.assertNotEqual(response.status_code, 401)

    def test_options_requests_pass_through(self) -> None:
        response = self.client.options("/api/sites")
        self.assertNotEqual(response.status_code, 401)

    def test_settings_endpoints_removed(self) -> None:
        response = self.client.get(
            "/api/settings/ingest-token",
            headers={"X-Nav-Token": "secret-token"},
        )
        self.assertEqual(response.status_code, 404)


class BrowserIngestTest(IsolatedAppTestCase):
    def post_ingest(self, payload: dict, token: str = "secret-token"):
        return self.client.post(
            "/api/site/ingest",
            json=payload,
            headers={"X-Nav-Token": token},
        )

    def payload(self, **overrides: object) -> dict:
        data = {
            "url": "https://example.invalid/app/",
            "final_url": "https://example.invalid/app/",
            "site_name": "Example App",
            "icon": {
                "source_url": "https://example.invalid/favicon.png",
                "content_type": "image/png",
                "filename": "favicon.png",
                "data_base64": PNG_BASE64,
            },
        }
        data.update(overrides)
        return data

    def test_rejects_disabled_or_invalid_token(self) -> None:
        response = self.post_ingest(self.payload(), token="wrong-token")
        self.assertEqual(response.status_code, 401)

        main.NAV_TOKEN = ""
        response = self.post_ingest(self.payload())
        self.assertEqual(response.status_code, 403)

    def test_creates_site_with_browser_icon(self) -> None:
        response = self.post_ingest(self.payload())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["url"], "https://example.invalid/app")
        self.assertTrue(data["icon_rel_path"].startswith("ICON/"))
        self.assertTrue(Path(data["icon_rel_path"]).is_file())

        with main.db_connect() as conn:
            row = conn.execute(
                "SELECT site_name, icon_rel_path, icon_source_url FROM sites WHERE url = ?",
                ("https://example.invalid/app",),
            ).fetchone()
        self.assertEqual(row[0], "Example App")
        self.assertEqual(row[1], data["icon_rel_path"])
        self.assertEqual(row[2], "https://example.invalid/favicon.png")

    def test_preserves_existing_icon_when_icon_is_missing(self) -> None:
        first = self.post_ingest(self.payload())
        self.assertEqual(first.status_code, 200)
        old_icon_path = first.json()["icon_rel_path"]

        second = self.post_ingest(
            self.payload(site_name="Renamed App", icon=None)
        )
        self.assertEqual(second.status_code, 200)
        data = second.json()
        self.assertEqual(data["site_name"], "Renamed App")
        self.assertEqual(data["icon_rel_path"], old_icon_path)
        self.assertTrue(Path(old_icon_path).is_file())

    def test_accepts_octet_stream_when_icon_extension_is_supported(self) -> None:
        response = self.post_ingest(
            self.payload(
                icon={
                    "source_url": "https://example.invalid/favicon.ico",
                    "content_type": "application/octet-stream",
                    "filename": "favicon.ico",
                    "data_base64": PNG_BASE64,
                }
            )
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["icon_rel_path"].endswith(".ico"))


if __name__ == "__main__":
    unittest.main()
