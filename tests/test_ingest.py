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
        # 公开只读接口：无 token 也可访问（仅返回公开站点）
        response = self.client.get("/api/sites")
        self.assertEqual(response.status_code, 200)
        response = self.client.get("/api/tags")
        self.assertEqual(response.status_code, 200)

        # 其余接口未带 / 带错 token 一律 401
        response = self.client.get("/api/icons")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "需要访问 token"})

        response = self.client.get("/api/icons", headers={"X-Nav-Token": "wrong"})
        self.assertEqual(response.status_code, 401)

        response = self.client.put(
            "/api/sites/reorder",
            json={"site_ids": [1]},
            headers={"X-Nav-Token": "wrong"},
        )
        self.assertEqual(response.status_code, 401)

        response = self.client.get("/api/icons", headers={"X-Nav-Token": "secret-token"})
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


class AuthStatusTest(IsolatedAppTestCase):
    def test_gated_mode_reports_required_and_authorized(self) -> None:
        # 匿名可访问（在公开只读清单中），而非 401
        response = self.client.get("/api/auth/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"token_required": True, "authorized": False})

        response = self.client.get("/api/auth/status", headers={"X-Nav-Token": "wrong"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"token_required": True, "authorized": False})

        response = self.client.get(
            "/api/auth/status", headers={"X-Nav-Token": "secret-token"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"token_required": True, "authorized": True})

    def test_open_mode_reports_not_required(self) -> None:
        main.NAV_TOKEN = ""
        response = self.client.get("/api/auth/status")
        self.assertEqual(response.json(), {"token_required": False, "authorized": True})

        response = self.client.get("/api/auth/status", headers={"X-Nav-Token": "anything"})
        self.assertEqual(response.json(), {"token_required": False, "authorized": True})


class VisibilityTest(IsolatedAppTestCase):
    def ingest(self, url: str, name: str):
        return self.client.post(
            "/api/site/ingest",
            json={"url": url, "final_url": url, "site_name": name, "icon": None},
            headers={"X-Nav-Token": "secret-token"},
        )

    def put_site(self, site_id: int, **fields):
        return self.client.put(
            f"/api/sites/{site_id}",
            json=fields,
            headers={"X-Nav-Token": "secret-token"},
        )

    def test_private_sites_hidden_from_anonymous(self) -> None:
        self.assertEqual(self.ingest("https://public.invalid/", "Public Site").status_code, 200)
        self.assertEqual(self.ingest("https://secret.invalid/", "Secret Site").status_code, 200)

        with_token = self.client.get(
            "/api/sites", headers={"X-Nav-Token": "secret-token"}
        ).json()
        self.assertEqual(len(with_token), 2)
        self.assertTrue(all(item["is_public"] for item in with_token))

        secret = next(item for item in with_token if item["site_name"] == "Secret Site")
        response = self.put_site(
            secret["id"],
            site_name="Secret Site",
            url=secret["url"],
            tags=["隐私"],
            is_public=False,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["is_public"])

        anonymous = self.client.get("/api/sites").json()
        self.assertEqual([item["site_name"] for item in anonymous], ["Public Site"])

        # 私有站点独有的标签名不对匿名访客泄露
        self.assertEqual(self.client.get("/api/tags").json(), [])
        owner_tags = self.client.get(
            "/api/tags", headers={"X-Nav-Token": "secret-token"}
        ).json()
        self.assertEqual(owner_tags, [{"name": "隐私", "count": 1}])

        full = self.client.get(
            "/api/sites", headers={"X-Nav-Token": "secret-token"}
        ).json()
        self.assertEqual(len(full), 2)

    def test_open_mode_shows_all_sites(self) -> None:
        self.assertEqual(self.ingest("https://secret.invalid/", "Secret Site").status_code, 200)
        rows = self.client.get(
            "/api/sites", headers={"X-Nav-Token": "secret-token"}
        ).json()
        self.put_site(
            rows[0]["id"],
            site_name="Secret Site",
            url=rows[0]["url"],
            is_public=False,
        )
        main.NAV_TOKEN = ""
        rows = self.client.get("/api/sites").json()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["is_public"])


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
