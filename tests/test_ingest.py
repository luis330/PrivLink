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


class BrowserIngestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = Path.cwd()
        self.old_db_path = main.DB_PATH
        self.old_icon_dir = main.ICON_DIR
        self.old_token = main.INGEST_TOKEN

        os.chdir(self.temp_dir.name)
        main.DB_PATH = Path("data") / "sites.db"
        main.ICON_DIR = Path("ICON")
        main.INGEST_TOKEN = "secret-token"
        main.init_storage()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        main.DB_PATH = self.old_db_path
        main.ICON_DIR = self.old_icon_dir
        main.INGEST_TOKEN = self.old_token
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def post_ingest(self, payload: dict, token: str = "secret-token"):
        return self.client.post(
            "/api/site/ingest",
            json=payload,
            headers={"X-Nav-Token": token},
        )

    def put_token(self, token: str, origin: str | None = None):
        headers = {"Origin": origin} if origin is not None else {}
        return self.client.put(
            "/api/settings/ingest-token",
            json={"token": token},
            headers=headers,
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

        self.assertEqual(self.put_token("").status_code, 200)
        response = self.post_ingest(self.payload())
        self.assertEqual(response.status_code, 403)

    def test_environment_token_initializes_runtime_setting(self) -> None:
        response = self.client.get("/api/settings/ingest-token")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["token"], "secret-token")
        self.assertTrue(data["configured"])

    def test_updates_token_without_restart(self) -> None:
        response = self.put_token("new-secret-token")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["token"], "new-secret-token")
        self.assertTrue(data["configured"])

        old_token_response = self.post_ingest(self.payload(), token="secret-token")
        self.assertEqual(old_token_response.status_code, 401)

        new_token_response = self.post_ingest(
            self.payload(url="https://example.invalid/updated/"),
            token="new-secret-token",
        )
        self.assertEqual(new_token_response.status_code, 200)

    def test_settings_api_rejects_cross_origin(self) -> None:
        response = self.client.get(
            "/api/settings/ingest-token",
            headers={"Origin": "https://example.com"},
        )
        self.assertEqual(response.status_code, 403)

        response = self.put_token("blocked-token", origin="https://example.com")
        self.assertEqual(response.status_code, 403)

        response = self.put_token("same-origin-token", origin="http://testserver")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["token"], "same-origin-token")

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
