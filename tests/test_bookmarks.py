from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

import main
from privlink import config
from privlink.bookmarks import parse_bookmarks_html, render_bookmarks_html
from privlink.db import db_connect, init_storage


CHROME_STYLE = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
    <DT><H3 ADD_DATE="1690000000" PERSONAL_TOOLBAR_FOLDER="true">书签栏</H3>
    <DL><p>
        <DT><A HREF="https://developer.mozilla.org/" ADD_DATE="1690000000">MDN Web Docs</A>
        <DD>前端参考
        <DT><H3 ADD_DATE="1690000000">开发工具</H3>
        <DL><p>
            <DT><A HREF="https://github.com" ADD_DATE="1690000000">GitHub</A>
        </DL><p>
    </DL><p>
    <DT><A HREF="https://example.com" ADD_DATE="1690000000">Example</A>
</DL><p>
"""

FIREFOX_STYLE = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks Menu</TITLE>
<H1>Bookmarks Menu</H1>
<DL><p>
    <DT><A HREF="https://news.example.com" ICON="data:image/png;base64,AAA">新闻</A>
    <DT><A HREF="javascript:void(0)">脚本书签</A>
    <DT><A HREF="place:folder=BOOKMARKS_MENU">智能书签</A>
    <DT><A HREF="chrome://settings">浏览器设置</A>
    <DT><A HREF="https://example.org/wiki/A_%26_B">A &amp; B</A>
</DL><p>
"""


def _entry_urls(entries: list[dict]) -> list[str]:
    return [entry["url"] for entry in entries]


class ParseBookmarksTest(unittest.TestCase):
    def test_chrome_style_folders_and_nesting(self) -> None:
        entries = parse_bookmarks_html(CHROME_STYLE.encode("utf-8"))
        self.assertEqual(
            _entry_urls(entries),
            ["https://developer.mozilla.org/", "https://github.com", "https://example.com"],
        )
        self.assertEqual(entries[0]["folders"], ["书签栏"])
        # 多级嵌套逐级展开
        self.assertEqual(entries[1]["folders"], ["书签栏", "开发工具"])
        self.assertEqual(entries[1]["title"], "GitHub")
        # 顶层（任何文件夹之外）无文件夹
        self.assertEqual(entries[2]["folders"], [])

    def test_firefox_style_ignores_icons_dd_and_non_http(self) -> None:
        entries = parse_bookmarks_html(FIREFOX_STYLE.encode("utf-8"))
        self.assertEqual(
            _entry_urls(entries),
            ["https://news.example.com", "https://example.org/wiki/A_%26_B"],
        )
        # 标题中的实体已反转义（URL 里的 %26 是编码字符，不反转义）
        self.assertEqual(entries[1]["title"], "A & B")

    def test_empty_title_falls_back_to_url(self) -> None:
        html = b'<DL><p><DT><A HREF="https://notitle.example"></A></DL><p>'
        entries = parse_bookmarks_html(html)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "https://notitle.example")

    def test_utf8_bom_tolerated(self) -> None:
        html = b"\xef\xbb\xbf<DL><p><DT><A HREF=\"https://bom.example\">BOM</A></DL><p>"
        entries = parse_bookmarks_html(html)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "BOM")

    def test_malformed_html_still_returns_entries(self) -> None:
        html = b'<DL><p><DT><A HREF="https://ok.example">OK</A><DT><A HREF="https://broken'
        entries = parse_bookmarks_html(html)
        self.assertEqual(_entry_urls(entries), ["https://ok.example"])

    def test_file_internal_duplicates_kept_by_parser(self) -> None:
        # 去重是导入路由的职责，解析器原样保留
        html = (
            b'<DL><p><DT><A HREF="https://dup.example">A</A>'
            b"<DT><A HREF=\"https://dup.example\">B</A></DL><p>"
        )
        self.assertEqual(len(parse_bookmarks_html(html)), 2)


def _site_row(site_id: int, url: str, name: str, is_public: int, sort_order: int) -> dict:
    return {
        "id": site_id,
        "url": url,
        "site_name": name,
        "icon_rel_path": "",
        "updated_at": "2026-01-01T00:00:00Z",
        "sort_order": sort_order,
        "is_public": is_public,
    }


class RenderBookmarksTest(unittest.TestCase):
    def test_tags_as_folders_and_untagged_top(self) -> None:
        sites = [
            _site_row(1, "https://a.example", "A站", 1, 1),
            _site_row(2, "https://b.example", "B站", 1, 2),
            _site_row(3, "https://c.example", "C&D <站>", 1, 3),
        ]
        tags_by_site = {1: ["工具"], 2: ["工具", "新闻"]}
        html = render_bookmarks_html(sites, tags_by_site)
        self.assertTrue(html.startswith("<!DOCTYPE NETSCAPE-Bookmark-file-1>"))
        # 标签按名称排序成文件夹（Unicode 码点序，与 tags 接口的 NOCASE 一致）
        self.assertIn("<DT><H3>工具</H3>", html)
        self.assertIn("<DT><H3>新闻</H3>", html)
        self.assertLess(html.index("工具"), html.index("新闻"))
        self.assertIn('<A HREF="https://c.example">C&amp;D &lt;站&gt;</A>', html)

    def test_empty_sites_render_valid_skeleton(self) -> None:
        html = render_bookmarks_html([], {})
        self.assertIn("<!DOCTYPE NETSCAPE-Bookmark-file-1>", html)
        self.assertIn("<DL><p>", html)

    def test_render_parse_roundtrip(self) -> None:
        sites = [
            _site_row(1, "https://a.example", "A站", 1, 1),
            _site_row(2, "https://b.example", "B站", 1, 2),
        ]
        tags_by_site = {1: ["工具"], 2: ["开发/测试"]}
        entries = parse_bookmarks_html(render_bookmarks_html(sites, tags_by_site).encode("utf-8"))
        by_url = {entry["url"]: entry for entry in entries}
        self.assertEqual(by_url["https://a.example"]["folders"], ["工具"])
        self.assertEqual(by_url["https://b.example"]["folders"], ["开发/测试"])
        self.assertEqual(by_url["https://a.example"]["title"], "A站")


class IsolatedAppTestCase(unittest.TestCase):
    """在临时目录中运行应用，token 由 config.NAV_TOKEN 直接控制（env 权威语义）。"""

    nav_token = "secret-token"

    def setUp(self) -> None:
        import os
        import tempfile
        from pathlib import Path

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
        init_storage()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        import os

        config.DB_PATH = self.old_db_path
        config.ICON_DIR = self.old_icon_dir
        config.FRONTEND_PATH = self.old_frontend_path
        config.NAV_TOKEN = self.old_token
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def _seed_site(
        self,
        url: str,
        name: str,
        *,
        is_public: int = 1,
        sort_order: int = 1,
    ) -> None:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO sites (url, site_name, created_at, updated_at, last_status,"
                " sort_order, is_public) VALUES (?, ?, '2026-01-01T00:00:00Z',"
                " '2026-01-01T00:00:00Z', 'ok', ?, ?);",
                (url, name, sort_order, is_public),
            )
            conn.commit()

    def _db_query(self, sql: str) -> list[dict]:
        with db_connect() as conn:
            columns = [desc[0] for desc in conn.execute(sql).description]
            return [dict(zip(columns, row)) for row in conn.execute(sql).fetchall()]

    @staticmethod
    def _bookmark_file(html: str) -> dict:
        return {"file": ("bookmarks.html", html.encode("utf-8"), "text/html")}


class BookmarkApiTest(IsolatedAppTestCase):
    def test_import_export_require_token(self) -> None:
        response = self.client.post(
            "/api/sites/import", files=self._bookmark_file(CHROME_STYLE)
        )
        self.assertEqual(response.status_code, 401)
        response = self.client.get("/api/sites/export")
        self.assertEqual(response.status_code, 401)

    def test_import_creates_private_sites_with_folder_tags(self) -> None:
        response = self.client.post(
            "/api/sites/import",
            files=self._bookmark_file(CHROME_STYLE),
            headers={"X-Nav-Token": self.nav_token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"total": 3, "imported": 3, "skipped": 0}
        )

        rows = self._db_query(
            "SELECT url, site_name, is_public, sort_order FROM sites ORDER BY sort_order;"
        )
        # 默认私有，sort_order 追加在现有最大值之后（空库从 1 开始）
        self.assertEqual(
            [(row["url"], row["is_public"]) for row in rows],
            [
                ("https://developer.mozilla.org/", 0),
                ("https://github.com", 0),
                ("https://example.com", 0),
            ],
        )
        self.assertEqual([row["sort_order"] for row in rows], [1, 2, 3])

        tag_rows = self._db_query(
            "SELECT sites.url, tags.name FROM site_tags"
            " JOIN tags ON tags.id = site_tags.tag_id"
            " JOIN sites ON sites.id = site_tags.site_id"
            " ORDER BY sites.url, tags.name COLLATE NOCASE;"
        )
        self.assertEqual(
            [(row["url"], row["name"]) for row in tag_rows],
            [
                ("https://developer.mozilla.org/", "书签栏"),
                ("https://github.com", "书签栏"),
                ("https://github.com", "开发工具"),
            ],
        )

    def test_import_skips_db_duplicates_and_keeps_existing(self) -> None:
        self._seed_site("https://github.com", "我的GitHub", sort_order=5)
        response = self.client.post(
            "/api/sites/import",
            files=self._bookmark_file(CHROME_STYLE),
            data={"is_public": "true"},
            headers={"X-Nav-Token": self.nav_token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"total": 3, "imported": 2, "skipped": 1})

        rows = self._db_query(
            "SELECT site_name, is_public, sort_order FROM sites WHERE url = 'https://github.com';"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site_name"], "我的GitHub")
        self.assertEqual(rows[0]["is_public"], 1)
        # 新导入站点追加在现有 sort_order 最大值之后
        fresh = self._db_query(
            "SELECT url, is_public, sort_order FROM sites ORDER BY sort_order;"
        )
        self.assertEqual(fresh[0]["url"], "https://github.com")
        self.assertTrue(all(row["is_public"] == 1 for row in fresh[1:]))

    def test_import_empty_and_oversized_file_rejected(self) -> None:
        response = self.client.post(
            "/api/sites/import",
            files={"file": ("empty.html", b"", "text/html")},
            headers={"X-Nav-Token": self.nav_token},
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/sites/import",
            files={"file": ("big.html", b"\xff" * (5 * 1024 * 1024 + 1), "text/html")},
            headers={"X-Nav-Token": self.nav_token},
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/sites/import",
            files={"file": ("plain.html", b"<html><body>not bookmarks</body></html>", "text/html")},
            headers={"X-Nav-Token": self.nav_token},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._db_query("SELECT COUNT(*) AS n FROM sites;")[0]["n"], 0)

    def test_export_contains_all_sites_in_order(self) -> None:
        self._seed_site("https://pub.example", "公开站", is_public=1, sort_order=1)
        self._seed_site("https://priv.example", "私有站", is_public=0, sort_order=2)
        response = self.client.get(
            "/api/sites/export", headers={"X-Nav-Token": self.nav_token}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["Content-Disposition"].startswith("attachment;"))
        self.assertIn("nav-bookmarks-", response.headers["Content-Disposition"])
        body = response.text
        self.assertIn("https://priv.example", body)  # 持 token 导出包含私有站点
        self.assertIn("https://pub.example", body)
        self.assertLess(body.index("https://pub.example"), body.index("https://priv.example"))
        # 导出结果可被解析器原样读回
        entries = parse_bookmarks_html(response.content)
        self.assertEqual(
            _entry_urls(entries), ["https://pub.example", "https://priv.example"]
        )


    def test_import_truncates_long_folder_names_with_warning(self) -> None:
        long_folder = "这是一个超过二十个字符的很长很长很长的文件夹名称"
        html = (
            "<!DOCTYPE NETSCAPE-Bookmark-file-1><H1>书签</H1><DL><p>"
            f"<DT><H3>{long_folder}</H3><DL><p>"
            '<DT><A HREF="https://long-folder.invalid/">长目录站点</A>'
            "</DL><p></DL><p>"
        )
        response = self.client.post(
            "/api/sites/import",
            files=self._bookmark_file(html),
            headers={"X-Nav-Token": self.nav_token},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["imported"], 1)
        self.assertTrue(any("标签超长已截断" in w for w in data.get("warnings", [])))
        # 标签按截断后的名字入库
        tags = self.client.get("/api/tags", headers={"X-Nav-Token": self.nav_token}).json()
        self.assertEqual([t["name"] for t in tags], [long_folder[:20].rstrip()])


if __name__ == "__main__":
    unittest.main()
