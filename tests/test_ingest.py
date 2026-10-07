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
from privlink.db import db_connect, init_storage


PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class CollectorScriptTest(unittest.TestCase):
    def test_tampermonkey_collector_runs_only_in_top_frame(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "collectors" / "privlink.user.js"
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("// @noframes", script)
        self.assertIn("window.top !== window.self", script)
        self.assertLess(
            script.index("window.top !== window.self"),
            script.index('GM_registerMenuCommand("保存当前页到 PrivLink"'),
        )


class IsolatedAppTestCase(unittest.TestCase):
    """在临时目录中运行应用，token 由 config.NAV_TOKEN 直接控制（env 权威语义）。"""

    nav_token = "secret-token"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = Path.cwd()
        self.old_db_path = config.DB_PATH
        self.old_icon_dir = config.ICON_DIR
        self.old_frontend_path = config.FRONTEND_PATH
        self.old_token = config.NAV_TOKEN

        os.chdir(self.temp_dir.name)
        config.DB_PATH = Path("data") / "sites.db"
        config.ICON_DIR = Path("ICON")
        config.FRONTEND_PATH = self.old_cwd / "index.html"
        config.NAV_TOKEN = self.nav_token
        # 限速是 TokenGuard 类级内存状态，逐用例清零避免跨测试文件累积
        TokenGuard._failures.clear()
        TokenGuard._locked_until.clear()
        init_storage()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        config.DB_PATH = self.old_db_path
        config.ICON_DIR = self.old_icon_dir
        config.FRONTEND_PATH = self.old_frontend_path
        config.NAV_TOKEN = self.old_token
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

        # 公网 IP 端点在本地部署下必须留在门禁内：它返回的是服务端出口 IP，反代 /
        # 隧道部署下属于源站敏感信息。Workers 部署返回访客自己的 IP，对本人不构成
        # 泄露，故仅在 Workers 下放进公开只读清单（见 test_public_ip_is_public_on_workers）。
        response = self.client.get("/api/network/public-ip")
        self.assertEqual(response.status_code, 401)

        response = self.client.get("/api/icons", headers={"X-Nav-Token": "secret-token"})
        self.assertEqual(response.status_code, 200)

    def test_public_ip_is_public_on_workers(self) -> None:
        with mock.patch.object(config, "IS_WORKERS", True):
            response = self.client.get(
                "/api/network/public-ip", headers={"CF-Connecting-IP": "203.0.113.9"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"ip": "203.0.113.9", "kind": "client"})

            # 放开的只有 public-ip，其余管理接口仍需 token
            self.assertEqual(self.client.get("/api/icons").status_code, 401)

    def test_open_mode_allows_api_without_token(self) -> None:
        config.NAV_TOKEN = ""
        response = self.client.get("/api/sites")
        self.assertEqual(response.status_code, 200)

    def test_homepage_and_static_stay_public(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

        # 静态挂载不经过 /api/ 门禁（404 也证明未被 401 拦截）
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
        config.NAV_TOKEN = ""
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
        config.NAV_TOKEN = ""
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

        config.NAV_TOKEN = ""
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

        with db_connect() as conn:
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


class TagsIntegrityTest(IsolatedAppTestCase):
    """标签增删改的准确性与防线：孤儿清理、数量上限、不可见字符、Unicode 重名。"""

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

    def sites(self):
        return self.client.get("/api/sites", headers={"X-Nav-Token": "secret-token"}).json()

    def owner_tags(self):
        return self.client.get("/api/tags", headers={"X-Nav-Token": "secret-token"}).json()

    def test_orphan_tags_cleaned_on_site_delete(self) -> None:
        self.ingest("https://a.invalid/", "站点A")
        site = self.sites()[0]
        self.assertEqual(
            self.put_site(
                site["id"], site_name="站点A", url=site["url"], tags=["临时", "保留"], is_public=True
            ).status_code,
            200,
        )
        self.assertEqual({t["name"] for t in self.owner_tags()}, {"临时", "保留"})
        # 删站点 → 它独占的标签成孤儿 → 同批清理
        self.client.delete(f"/api/sites/{site['id']}", headers={"X-Nav-Token": "secret-token"})
        self.assertEqual(self.owner_tags(), [])

    def test_orphan_tags_cleaned_on_tag_removal(self) -> None:
        self.ingest("https://b.invalid/", "站点B")
        site = self.sites()[0]
        self.put_site(site["id"], site_name="站点B", url=site["url"], tags=["甲", "乙"], is_public=True)
        # 移除"甲"后不再被任何站点引用 → 死标签清理
        self.put_site(site["id"], site_name="站点B", url=site["url"], tags=["乙"], is_public=True)
        self.assertEqual([t["name"] for t in self.owner_tags()], ["乙"])

    def test_tag_count_limit_rejected(self) -> None:
        self.ingest("https://c.invalid/", "站点C")
        site = self.sites()[0]
        response = self.put_site(
            site["id"],
            site_name="站点C",
            url=site["url"],
            tags=[f"标签{i}" for i in range(51)],
            is_public=True,
        )
        self.assertEqual(response.status_code, 400)

    def test_invisible_characters_stripped_from_tag(self) -> None:
        self.ingest("https://d.invalid/", "站点D")
        site = self.sites()[0]
        response = self.put_site(
            site["id"], site_name="站点D", url=site["url"], tags=["可\u200b见"], is_public=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([t["name"] for t in self.owner_tags()], ["可见"])

    def test_unicode_case_variants_merge_into_one_tag(self) -> None:
        self.ingest("https://e1.invalid/", "站点E1")
        self.put_site(self.sites()[0]["id"], site_name="站点E1", url="https://e1.invalid/", tags=["Ñandu"], is_public=True)
        self.ingest("https://e2.invalid/", "站点E2")
        self.put_site(self.sites()[1]["id"], site_name="站点E2", url="https://e2.invalid/", tags=["ñandu"], is_public=True)
        tags = self.owner_tags()
        # ñandu 吸附到既有 Ñandu 行（DB 的 NOCASE 折叠不到非 ASCII），而不是另起一行
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0]["name"], "Ñandu")
        self.assertEqual(tags[0]["count"], 2)


if __name__ == "__main__":
    unittest.main()
