from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import main
from privlink import config
from privlink.app import TokenGuard
from privlink.db import init_storage


class PluginsTestCase(unittest.TestCase):
    """插件市场回归测试。

    覆盖：静态路由的沙箱 CSP 与穿越防护、registry 成员门禁、安装状态与数据端点的
    鉴权/隔离/配额、内置插件清单与 HTML 的静态审查（准入合规）。
    在临时目录中运行应用，plugins/ 与 index.html 取自仓库（与 test_security_headers
    的 FRONTEND_PATH 处理同款隔离方式）。
    """

    nav_token = "secret-token"

    # 静态审查违禁模式：插件为单 HTML 自包含、零网络、零嵌套浏览上下文
    FORBIDDEN_PLUGIN_PATTERNS = (
        "external_script_src",
        rb"<script[^>]*\bsrc\s*=",
        "external_src_url",
        rb"\bsrc\s*=\s*[\"']\s*(?:https?:)?//",
        "external_href_url",
        rb"\bhref\s*=\s*[\"']\s*(?:https?:)?//",
        "fetch_call",
        rb"\bfetch\s*\(",
        "xml_http_request",
        rb"XMLHttpRequest",
        "web_socket",
        rb"WebSocket\s*\(",
        "event_source",
        rb"EventSource\s*\(",
        "dynamic_import",
        rb"\bimport\s*\(",
        "iframe_element",
        rb"<\s*iframe",
        "form_element",
        rb"<\s*form[\s>]",
        "send_beacon",
        rb"sendBeacon",
        "blank_target",
        rb"target\s*=\s*[\"']_blank",
    )

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = Path.cwd()
        self.old_db_path = config.DB_PATH
        self.old_icon_dir = config.ICON_DIR
        self.old_background_dir = config.BACKGROUND_DIR
        self.old_frontend_path = config.FRONTEND_PATH
        self.old_plugins_dir = config.PLUGINS_DIR
        self.old_token = config.NAV_TOKEN
        self.old_max_keys = config.PLUGIN_DATA_MAX_KEYS
        self.old_max_bytes = config.PLUGIN_DATA_MAX_BYTES

        os.chdir(self.temp_dir.name)
        config.DB_PATH = Path("data") / "sites.db"
        config.ICON_DIR = Path("ICON")
        config.BACKGROUND_DIR = Path("background")
        config.FRONTEND_PATH = self.old_cwd / "index.html"
        config.PLUGINS_DIR = Path("plugins")
        config.NAV_TOKEN = self.nav_token
        shutil.copytree(self.old_cwd / "plugins", Path("plugins"))
        # 限速是类级内存状态，逐用例清零避免顺序耦合
        TokenGuard._failures.clear()
        TokenGuard._locked_until.clear()
        init_storage()
        self.client = TestClient(main.app)
        self.auth = {"X-Nav-Token": self.nav_token}

    def tearDown(self) -> None:
        config.DB_PATH = self.old_db_path
        config.ICON_DIR = self.old_icon_dir
        config.BACKGROUND_DIR = self.old_background_dir
        config.FRONTEND_PATH = self.old_frontend_path
        config.PLUGINS_DIR = self.old_plugins_dir
        config.NAV_TOKEN = self.old_token
        config.PLUGIN_DATA_MAX_KEYS = self.old_max_keys
        config.PLUGIN_DATA_MAX_BYTES = self.old_max_bytes
        TokenGuard._failures.clear()
        TokenGuard._locked_until.clear()
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    # ===== 静态路由：沙箱 CSP 与门禁 =====

    def test_plugin_page_served_with_sandbox_csp(self) -> None:
        response = self.client.get("/plugins/sticky-notes/index.html")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertEqual(response.headers["content-security-policy"], config.CSP_PLUGIN)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        # no-cache + ETag：插件升级即时生效
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertIn("etag", response.headers)
        self.assertIn("postMessage", response.text)

    def test_plugin_registry_served(self) -> None:
        response = self.client.get("/plugins/registry.json")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("plugins", data)
        self.assertTrue(any(p.get("id") == "sticky-notes" for p in data["plugins"]))

    def test_plugin_entry_served_at_directory_root(self) -> None:
        response = self.client.get("/plugins/sticky-notes/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-security-policy"], config.CSP_PLUGIN)

    def test_traversal_and_invalid_ids_rejected(self) -> None:
        for path in (
            "/plugins/sticky-notes/..%2F..%2Fmain.py",
            "/plugins/sticky-notes/..%5C..%5Cmain.py",
            "/plugins/registry.json/index.html",  # id 含点：registry.json 不是插件
            "/plugins/Upper-Case/index.html",  # id 必须小写
            "/plugins/no-such-plugin/index.html",  # 未收录目录不可加载（审查门禁）
            "/plugins/sticky-notes/sub/../../../main.py",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404, path)

    def test_unregistered_plugin_dir_rejected_everywhere(self) -> None:
        rogue_dir = Path("plugins") / "rogue-plugin"
        rogue_dir.mkdir()
        (rogue_dir / "index.html").write_text("<p>rogue</p>", encoding="utf-8")
        self.assertEqual(self.client.get("/plugins/rogue-plugin/index.html").status_code, 404)
        response = self.client.put(
            "/api/plugins/rogue-plugin/installed",
            json={"enabled": True},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 404)

    # ===== 安装状态端点 =====

    def test_installed_endpoints_require_token(self) -> None:
        self.assertEqual(self.client.get("/api/plugins/installed").status_code, 401)
        self.assertEqual(
            self.client.put(
                "/api/plugins/sticky-notes/installed", json={"enabled": True}
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get("/api/plugins/sticky-notes/data?key=notes").status_code, 401
        )

    def test_install_enable_disable_flow(self) -> None:
        response = self.client.put(
            "/api/plugins/sticky-notes/installed", json={"enabled": True}, headers=self.auth
        )
        self.assertEqual(response.status_code, 200)
        installed = self.client.get("/api/plugins/installed", headers=self.auth).json()
        self.assertIn("sticky-notes", installed)
        self.assertTrue(installed["sticky-notes"]["enabled"])
        installed_at = installed["sticky-notes"]["installedAt"]

        response = self.client.put(
            "/api/plugins/sticky-notes/installed", json={"enabled": False}, headers=self.auth
        )
        self.assertEqual(response.status_code, 200)
        installed = self.client.get("/api/plugins/installed", headers=self.auth).json()
        self.assertFalse(installed["sticky-notes"]["enabled"])
        # 重复操作不重置安装时间
        self.assertEqual(installed["sticky-notes"]["installedAt"], installed_at)

        # 非法 id 格式
        response = self.client.put(
            "/api/plugins/BAD_ID/installed", json={"enabled": True}, headers=self.auth
        )
        self.assertEqual(response.status_code, 400)

    def test_uninstall_purge_and_keep(self) -> None:
        self.client.put(
            "/api/plugins/sticky-notes/installed", json={"enabled": True}, headers=self.auth
        )
        self.client.put(
            "/api/plugins/sticky-notes/data",
            json={"key": "notes", "value": "[]"},
            headers=self.auth,
        )
        # 不带 purge：状态清除、数据保留
        self.client.delete("/api/plugins/sticky-notes/installed", headers=self.auth)
        installed = self.client.get("/api/plugins/installed", headers=self.auth).json()
        self.assertNotIn("sticky-notes", installed)
        self.assertEqual(
            self.client.get(
                "/api/plugins/sticky-notes/data?key=notes", headers=self.auth
            ).status_code,
            200,
        )
        # purge=1：数据一并清除
        self.client.put(
            "/api/plugins/sticky-notes/installed", json={"enabled": True}, headers=self.auth
        )
        self.client.delete("/api/plugins/sticky-notes/installed?purge=1", headers=self.auth)
        self.assertEqual(
            self.client.get(
                "/api/plugins/sticky-notes/data?key=notes", headers=self.auth
            ).status_code,
            404,
        )

    # ===== 插件数据端点：CRUD / 隔离 =====

    def test_plugin_data_crud_and_isolation(self) -> None:
        response = self.client.put(
            "/api/plugins/sticky-notes/data",
            json={"key": "notes", "value": '["a"]'},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.get(
            "/api/plugins/sticky-notes/data?key=notes", headers=self.auth
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["value"], '["a"]')
        self.assertEqual(response.json()["key"], "notes")

        self.assertEqual(
            self.client.get(
                "/api/plugins/sticky-notes/data?key=missing", headers=self.auth
            ).status_code,
            404,
        )

        # 命名空间隔离：另一插件的同名键互不影响
        self.client.put(
            "/api/plugins/other-plugin/data",
            json={"key": "notes", "value": '["b"]'},
            headers=self.auth,
        )
        self.assertEqual(
            self.client.get(
                "/api/plugins/sticky-notes/data?key=notes", headers=self.auth
            ).json()["value"],
            '["a"]',
        )
        self.client.delete(
            "/api/plugins/other-plugin/data?key=notes", headers=self.auth
        )
        self.assertEqual(
            self.client.get(
                "/api/plugins/sticky-notes/data?key=notes", headers=self.auth
            ).json()["value"],
            '["a"]',
        )

        # 清空单插件全部数据
        self.client.delete("/api/plugins/sticky-notes/data", headers=self.auth)
        self.assertEqual(
            self.client.get(
                "/api/plugins/sticky-notes/data?key=notes", headers=self.auth
            ).status_code,
            404,
        )

    # ===== 配额 =====

    def test_plugin_data_key_quota(self) -> None:
        config.PLUGIN_DATA_MAX_KEYS = 2
        for key in ("k1", "k2"):
            response = self.client.put(
                "/api/plugins/sticky-notes/data",
                json={"key": key, "value": "v"},
                headers=self.auth,
            )
            self.assertEqual(response.status_code, 200, key)
        response = self.client.put(
            "/api/plugins/sticky-notes/data",
            json={"key": "k3", "value": "v"},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 400)
        # 覆盖已有键不受键数配额影响
        response = self.client.put(
            "/api/plugins/sticky-notes/data",
            json={"key": "k1", "value": "v2"},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 200)

    def test_plugin_data_size_quota(self) -> None:
        config.PLUGIN_DATA_MAX_BYTES = 10
        response = self.client.put(
            "/api/plugins/sticky-notes/data",
            json={"key": "big", "value": "x" * 20},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 413)
        # 已有数据之上累加也受限：覆盖写超限 / 新键累加超限
        self.client.put(
            "/api/plugins/sticky-notes/data",
            json={"key": "small", "value": "12345"},
            headers=self.auth,
        )
        response = self.client.put(
            "/api/plugins/sticky-notes/data",
            json={"key": "small", "value": "12345678901"},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 413)
        response = self.client.put(
            "/api/plugins/sticky-notes/data",
            json={"key": "another", "value": "654321"},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 413)

    # ===== 浮动窗口布局注册表 =====

    def test_plugin_windows_layout_roundtrip(self) -> None:
        # 键不存在 = 从未有过实例（宿主据此自动浮出首张空白便签）
        self.assertEqual(
            self.client.get(
                "/api/plugins/sticky-notes/windows", headers=self.auth
            ).status_code,
            404,
        )
        windows = [{"id": "w1", "x": 10, "y": 20, "w": 240, "h": 220}]
        response = self.client.put(
            "/api/plugins/sticky-notes/windows", json={"windows": windows}, headers=self.auth
        )
        self.assertEqual(response.status_code, 200)
        data = self.client.get("/api/plugins/sticky-notes/windows", headers=self.auth).json()
        self.assertEqual(data["windows"], windows)

    def test_plugin_windows_layout_validation(self) -> None:
        invalid_windows = [
            {"id": "w1", "x": "bad", "y": 0, "w": 240, "h": 220},  # 非数值
            {"id": "w1", "x": 0, "y": 0, "w": 10, "h": 220},  # 宽度低于下限
            {"id": "w1", "x": 0, "y": 0, "w": 240, "h": 999999},  # 超上限
            {"id": "Bad Id", "x": 0, "y": 0, "w": 240, "h": 220},  # id 字符集
        ]
        for windows in invalid_windows:
            response = self.client.put(
                "/api/plugins/sticky-notes/windows",
                json={"windows": [windows]},
                headers=self.auth,
            )
            # 本项目自定义 RequestValidationError 处理器，校验失败统一 400
            self.assertEqual(response.status_code, 400, windows)
        too_many = [
            {"id": "w%d" % i, "x": 0, "y": 0, "w": 240, "h": 220}
            for i in range(config.PLUGIN_MAX_WINDOWS + 1)
        ]
        response = self.client.put(
            "/api/plugins/sticky-notes/windows", json={"windows": too_many}, headers=self.auth
        )
        self.assertEqual(response.status_code, 400)

    def test_plugin_windows_require_token(self) -> None:
        self.assertEqual(
            self.client.get("/api/plugins/sticky-notes/windows").status_code, 401
        )
        self.assertEqual(
            self.client.put("/api/plugins/sticky-notes/windows", json={"windows": []}).status_code,
            401,
        )

    def test_uninstall_purge_clears_windows_layout(self) -> None:
        self.client.put(
            "/api/plugins/sticky-notes/windows",
            json={"windows": [{"id": "w1", "x": 1, "y": 2, "w": 240, "h": 220}]},
            headers=self.auth,
        )
        self.client.delete("/api/plugins/sticky-notes/installed?purge=1", headers=self.auth)
        self.assertEqual(
            self.client.get(
                "/api/plugins/sticky-notes/windows", headers=self.auth
            ).status_code,
            404,
        )

    # ===== 实例前缀删除（浮动便签关闭单实例） =====

    def test_plugin_data_prefix_delete(self) -> None:
        for key in ("w1:content", "w2:content", "notes"):
            self.client.put(
                "/api/plugins/sticky-notes/data",
                json={"key": key, "value": "v-" + key},
                headers=self.auth,
            )
        response = self.client.delete(
            "/api/plugins/sticky-notes/data?prefix=w1:", headers=self.auth
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get(
                "/api/plugins/sticky-notes/data?key=w1:content", headers=self.auth
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                "/api/plugins/sticky-notes/data?key=w2:content", headers=self.auth
            ).json()["value"],
            "v-w2:content",
        )
        self.assertEqual(
            self.client.get(
                "/api/plugins/sticky-notes/data?key=notes", headers=self.auth
            ).json()["value"],
            "v-notes",
        )
        # LIKE 通配符注入被拒
        response = self.client.delete(
            "/api/plugins/sticky-notes/data?prefix=w%25", headers=self.auth
        )
        self.assertEqual(response.status_code, 400)

    # ===== 内置插件静态审查（准入合规） =====

    def _load_builtin_registry(self) -> dict:
        registry_path = Path("plugins") / "registry.json"
        return json.loads(registry_path.read_text(encoding="utf-8"))

    def test_builtin_registry_schema(self) -> None:
        registry = self._load_builtin_registry()
        self.assertIsInstance(registry.get("bridgeVersion"), int)
        self.assertGreaterEqual(registry["bridgeVersion"], 1)
        plugins = registry.get("plugins")
        self.assertIsInstance(plugins, list)
        self.assertGreater(len(plugins), 0)
        seen_ids = set()
        for entry in plugins:
            plugin_id = entry.get("id", "")
            self.assertRegex(plugin_id, config.PLUGIN_ID_RE, plugin_id)
            self.assertNotIn(plugin_id, seen_ids, "插件 id 重复")
            seen_ids.add(plugin_id)
            self.assertTrue(str(entry.get("name", "")).strip(), plugin_id)
            self.assertRegex(str(entry.get("version", "")), r"^\d+\.\d+\.\d+$", plugin_id)
            self.assertIn(entry.get("slot"), ("inline", "float"), plugin_id)
            self.assertTrue(str(entry.get("description", "")).strip(), plugin_id)
            permissions = entry.get("permissions")
            self.assertIsInstance(permissions, list, plugin_id)
            self.assertTrue(set(permissions) <= {"storage"}, plugin_id)
            # entry 指向的文件必须真实存在
            entry_file = Path("plugins") / plugin_id / str(entry.get("entry", ""))
            self.assertTrue(entry_file.is_file(), f"{plugin_id}: entry 缺失")

    def test_builtin_bridge_version_matches_host(self) -> None:
        registry = self._load_builtin_registry()
        frontend = (self.old_cwd / "index.html").read_text(encoding="utf-8")
        match = re.search(r"const PLUGIN_BRIDGE_VERSION = (\d+);", frontend)
        self.assertIsNotNone(match, "宿主桥版本常量缺失")
        self.assertEqual(registry["bridgeVersion"], int(match.group(1)))

    def test_builtin_plugin_html_static_review(self) -> None:
        registry = self._load_builtin_registry()
        for entry in registry["plugins"]:
            plugin_id = entry["id"]
            html = (Path("plugins") / plugin_id / entry["entry"]).read_bytes()
            for label, pattern in zip(
                self.FORBIDDEN_PLUGIN_PATTERNS[0::2], self.FORBIDDEN_PLUGIN_PATTERNS[1::2]
            ):
                self.assertIsNone(
                    re.search(pattern, html, re.IGNORECASE),
                    f"{plugin_id}: 静态审查未通过（{label}）",
                )
            # 桥胶水必须存在：插件离开宿主桥即惰性，postMessage 是唯一通信面
            self.assertIn(b"postMessage", html, f"{plugin_id}: 缺少桥胶水代码")


    # ===== 窗口注册表增量端点 =====

    def test_plugin_windows_incremental_add_patch_delete(self) -> None:
        base = "/api/plugins/sticky-notes/windows"
        self.assertEqual(
            self.client.post(
                base + "/add", json={"id": "w1", "x": 1, "y": 2, "w": 240, "h": 220}, headers=self.auth
            ).status_code,
            200,
        )
        # add 幂等：同 id 更新坐标而非追加
        self.assertEqual(
            self.client.post(
                base + "/add", json={"id": "w1", "x": 9, "y": 9, "w": 240, "h": 220}, headers=self.auth
            ).status_code,
            200,
        )
        self.client.post(
            base + "/add", json={"id": "w2", "x": 5, "y": 5, "w": 300, "h": 260}, headers=self.auth
        )
        data = self.client.get(base, headers=self.auth).json()["windows"]
        self.assertEqual([w["id"] for w in data], ["w1", "w2"])
        self.assertEqual(data[0]["x"], 9)

        # patch 更新坐标；未知实例 404；路径与正文 id 不一致 400
        self.assertEqual(
            self.client.patch(
                base + "/w2", json={"id": "w2", "x": 50, "y": 60, "w": 300, "h": 260}, headers=self.auth
            ).status_code,
            200,
        )
        w2 = next(
            w for w in self.client.get(base, headers=self.auth).json()["windows"] if w["id"] == "w2"
        )
        self.assertEqual((w2["x"], w2["y"]), (50, 60))
        self.assertEqual(
            self.client.patch(
                base + "/nope", json={"id": "nope", "x": 0, "y": 0, "w": 240, "h": 220}, headers=self.auth
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.patch(
                base + "/w2", json={"id": "w1", "x": 0, "y": 0, "w": 240, "h": 220}, headers=self.auth
            ).status_code,
            400,
        )

        # delete：删除与幂等；删空后键仍存在（空数组 ≠ 从未有过实例）
        self.assertEqual(self.client.delete(base + "/w1", headers=self.auth).status_code, 200)
        data = self.client.get(base, headers=self.auth).json()["windows"]
        self.assertEqual([w["id"] for w in data], ["w2"])
        self.assertEqual(self.client.delete(base + "/w1", headers=self.auth).status_code, 200)
        self.assertEqual(self.client.delete(base + "/w2", headers=self.auth).status_code, 200)
        self.assertEqual(self.client.get(base, headers=self.auth).json()["windows"], [])

    def test_plugin_windows_add_limit_and_auth(self) -> None:
        base = "/api/plugins/sticky-notes/windows"
        for i in range(config.PLUGIN_MAX_WINDOWS):
            response = self.client.post(
                base + "/add",
                json={"id": f"w{i}", "x": 0, "y": 0, "w": 240, "h": 220},
                headers=self.auth,
            )
            self.assertEqual(response.status_code, 200)
        response = self.client.post(
            base + "/add", json={"id": "overflow", "x": 0, "y": 0, "w": 240, "h": 220}, headers=self.auth
        )
        self.assertEqual(response.status_code, 409)
        # 无 token 一律 401
        self.assertEqual(
            self.client.post(
                base + "/add", json={"id": "wx", "x": 0, "y": 0, "w": 240, "h": 220}
            ).status_code,
            401,
        )


if __name__ == "__main__":
    unittest.main()
