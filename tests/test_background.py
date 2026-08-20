from __future__ import annotations

import base64
import hashlib
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
PNG_BYTES = base64.b64decode(PNG_BASE64)


class IsolatedBackgroundTestCase(unittest.TestCase):
    """在临时目录中运行应用（门禁模式），隔离 DB / ICON / background 目录。"""

    nav_token = "secret-token"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = Path.cwd()
        self.old_db_path = main.DB_PATH
        self.old_icon_dir = main.ICON_DIR
        self.old_background_dir = main.BACKGROUND_DIR
        self.old_frontend_path = main.FRONTEND_PATH
        self.old_token = main.NAV_TOKEN
        self.old_upload_max = main.BACKGROUND_UPLOAD_MAX_BYTES

        os.chdir(self.temp_dir.name)
        main.DB_PATH = Path("data") / "sites.db"
        main.ICON_DIR = Path("ICON")
        main.BACKGROUND_DIR = Path("background")
        main.FRONTEND_PATH = self.old_cwd / "index.html"
        main.NAV_TOKEN = self.nav_token
        main.init_storage()
        self.client = TestClient(main.app)
        self.auth_headers = {"X-Nav-Token": self.nav_token}

    def tearDown(self) -> None:
        main.DB_PATH = self.old_db_path
        main.ICON_DIR = self.old_icon_dir
        main.BACKGROUND_DIR = self.old_background_dir
        main.FRONTEND_PATH = self.old_frontend_path
        main.NAV_TOKEN = self.old_token
        main.BACKGROUND_UPLOAD_MAX_BYTES = self.old_upload_max
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def upload_png(self, name: str = "bg.png") -> dict:
        response = self.client.post(
            "/api/appearance/background/images",
            files={"image": (name, PNG_BYTES, "image/png")},
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class BackgroundSettingTest(IsolatedBackgroundTestCase):
    def test_get_is_public_and_defaults(self) -> None:
        # 白名单内的公开端点：匿名（无 token）可读，默认值为 default
        response = self.client.get("/api/appearance/background")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"type": "default", "color": "", "image": "", "image_url": ""},
        )

    def test_write_endpoints_require_token(self) -> None:
        response = self.client.put(
            "/api/appearance/background", json={"type": "color", "color": "#123456"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "需要访问 token"})

        response = self.client.post(
            "/api/appearance/background/images",
            files={"image": ("bg.png", PNG_BYTES, "image/png")},
        )
        self.assertEqual(response.status_code, 401)

        response = self.client.get("/api/appearance/background/images")
        self.assertEqual(response.status_code, 401)

        response = self.client.delete("/api/appearance/background/images/bg-" + "a" * 24 + ".png")
        self.assertEqual(response.status_code, 401)

    def test_color_roundtrip(self) -> None:
        response = self.client.put(
            "/api/appearance/background",
            json={"type": "color", "color": "#123456"},
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["type"], "color")

        # 匿名回读一致（全局设置对访客可见）
        response = self.client.get("/api/appearance/background")
        self.assertEqual(response.json()["color"], "#123456")

    def test_put_default_clears_fields(self) -> None:
        self.upload_png()
        response = self.client.put(
            "/api/appearance/background",
            json={"type": "default"},
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"type": "default", "color": "", "image": "", "image_url": ""})

    def test_put_rejects_invalid_payloads(self) -> None:
        cases = [
            {"type": "color", "color": "123456"},  # 缺 #
            {"type": "color", "color": "#xyzxyz"},  # 非 hex
            {"type": "color", "color": ""},  # 缺颜色
            {"type": "video"},  # 未知类型
            {"type": "image", "image": "bg-" + "a" * 24 + ".png"},  # 文件不存在
            {"type": "image", "image": "../../main.py"},  # 文件名不合法
        ]
        for payload in cases:
            response = self.client.put(
                "/api/appearance/background", json=payload, headers=self.auth_headers
            )
            self.assertEqual(response.status_code, 400, payload)

    def test_load_self_heals_when_image_file_missing(self) -> None:
        setting = self.upload_png()
        stored = setting["image"]
        (main.BACKGROUND_DIR / stored).unlink()
        response = self.client.get("/api/appearance/background")
        self.assertEqual(response.json()["type"], "default")


class BackgroundUploadTest(IsolatedBackgroundTestCase):
    def test_upload_saves_content_hashed_file_and_applies(self) -> None:
        setting = self.upload_png()
        digest = hashlib.sha256(PNG_BYTES).hexdigest()[:24]
        stored = f"bg-{digest}.png"
        self.assertEqual(setting["type"], "image")
        self.assertEqual(setting["image"], stored)
        self.assertEqual(setting["image_url"], f"/background/{stored}")
        self.assertTrue((main.BACKGROUND_DIR / stored).is_file())

        # 上传即生效：匿名回读同值
        response = self.client.get("/api/appearance/background")
        self.assertEqual(response.json()["image"], stored)

        # 静态文件公开可访问（不经过 /api/ 门禁）
        response = self.client.get(f"/background/{stored}")
        self.assertEqual(response.status_code, 200)

    def test_upload_rejects_bad_extension(self) -> None:
        for name in ("bg.gif", "bg.txt", "bg.svg"):
            response = self.client.post(
                "/api/appearance/background/images",
                files={"image": (name, PNG_BYTES, "application/octet-stream")},
                headers=self.auth_headers,
            )
            self.assertEqual(response.status_code, 400, name)

    def test_upload_rejects_oversize(self) -> None:
        main.BACKGROUND_UPLOAD_MAX_BYTES = 10
        response = self.client.post(
            "/api/appearance/background/images",
            files={"image": ("bg.png", b"x" * 20, "image/png")},
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 400)

    def test_upload_rejects_empty_content(self) -> None:
        response = self.client.post(
            "/api/appearance/background/images",
            files={"image": ("bg.png", b"", "image/png")},
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 400)


class BackgroundLibraryTest(IsolatedBackgroundTestCase):
    def test_list_and_delete_roundtrip(self) -> None:
        setting = self.upload_png()
        stored = setting["image"]

        response = self.client.get(
            "/api/appearance/background/images", headers=self.auth_headers
        )
        self.assertEqual(response.status_code, 200)
        items = response.json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["file"], stored)
        self.assertEqual(items[0]["url"], f"/background/{stored}")
        self.assertEqual(items[0]["size"], len(PNG_BYTES))

        # 删除当前使用中的背景 → 自动重置为默认并返回新设置
        response = self.client.delete(
            f"/api/appearance/background/images/{stored}", headers=self.auth_headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["type"], "default")
        self.assertFalse((main.BACKGROUND_DIR / stored).exists())

        # 列表已空
        response = self.client.get(
            "/api/appearance/background/images", headers=self.auth_headers
        )
        self.assertEqual(response.json(), [])

    def test_delete_keeps_setting_when_other_image(self) -> None:
        first = self.upload_png()
        # 换成纯色后再删图片文件，设置不应被重置
        self.client.put(
            "/api/appearance/background",
            json={"type": "color", "color": "#0a46b6"},
            headers=self.auth_headers,
        )
        response = self.client.delete(
            f"/api/appearance/background/images/{first['image']}", headers=self.auth_headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["type"], "color")

    def test_delete_rejects_invalid_filename(self) -> None:
        for bad in ("notes.txt", "bg-xyz.png", "upload-abcdef.png"):
            response = self.client.delete(
                f"/api/appearance/background/images/{bad}", headers=self.auth_headers
            )
            self.assertEqual(response.status_code, 400, bad)


class HomepageBackgroundTest(unittest.TestCase):
    """首页 HTML 逐字断言：背景设置入口与模态框必须存在（风格同 test_public_ip）。"""

    def test_homepage_contains_background_ui(self) -> None:
        client = TestClient(main.app)
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('id="background-settings-btn"', html)
        self.assertIn('id="background-modal"', html)
        self.assertIn('id="bg-image-grid"', html)
        self.assertIn('id="bg-upload-input"', html)
        # 背景本地缓存键与遮罩层
        self.assertIn("nav_background_v1", html)
        self.assertIn('id="bg-overlay"', html)
        # 纯色模式必须同时覆盖样式表渐变 background-image，否则纯色被不透明渐变遮住（回归：2026-08-18）
        self.assertIn('document.body.style.backgroundImage = "none";', html)


if __name__ == "__main__":
    unittest.main()
