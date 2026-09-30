"""书签 HTML（Netscape 格式）的解析与渲染。

Edge/Chrome/Firefox 的"导出收藏夹"均为此格式，故一份解析器通吃；
只用标准库 html.parser，保持 Pyodide/Workers 打包零额外依赖。
"""

from __future__ import annotations

from html import escape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit


class _BookmarkParser(HTMLParser):
    """Netscape 书签结构解析：<H3> 名称 + 其后首个 <DL> 开文件夹，<A HREF> 收条目。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[dict[str, Any]] = []
        # 栈底为根容器（空名），文件夹路径 = 栈内非空名
        self._folder_stack: list[str] = []
        self._pending_folder = ""
        self._in_h3 = False
        self._h3_parts: list[str] = []
        self._in_a = False
        self._a_href = ""
        self._a_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h3":
            self._in_h3 = True
            self._h3_parts = []
        elif tag == "dl":
            self._folder_stack.append(self._pending_folder)
            self._pending_folder = ""
        elif tag == "a":
            href = ""
            for key, value in attrs:
                if key == "href" and value:
                    href = value.strip()
                    break
            self._in_a = True
            self._a_href = href
            self._a_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "h3":
            self._in_h3 = False
            self._pending_folder = " ".join("".join(self._h3_parts).split())
        elif tag == "dl":
            if self._folder_stack:
                self._folder_stack.pop()
        elif tag == "a":
            self._in_a = False
            if self._a_href:
                self._append_entry()

    def handle_data(self, data: str) -> None:
        if self._in_h3:
            self._h3_parts.append(data)
        elif self._in_a:
            self._a_parts.append(data)

    def _append_entry(self) -> None:
        url = self._a_href.strip()
        try:
            scheme = urlsplit(url).scheme.lower()
        except ValueError:
            return
        if scheme not in ("http", "https"):
            return
        title = " ".join("".join(self._a_parts).split()) or url
        self.entries.append(
            {
                "url": url,
                "title": title,
                "folders": [name for name in self._folder_stack if name],
            }
        )


def parse_bookmarks_html(data: bytes) -> list[dict[str, Any]]:
    """解析书签 HTML，返回 [{url, title, folders}]。

    UTF-8（含 BOM）解码容错；仅收 http/https 链接（javascript:/place: 等丢弃）；
    空标题回退为 URL；多级嵌套文件夹展开为逐级名称列表。
    畸形 HTML 尽力解析：已收集的条目仍然返回。
    """
    parser = _BookmarkParser()
    try:
        parser.feed(data.decode("utf-8-sig", errors="replace"))
        parser.close()
    except Exception:  # 畸形文件不整体失败
        pass
    return parser.entries


def _bookmark_line(row: dict[str, Any]) -> str:
    url = escape((row.get("url") or "").strip(), quote=True)
    name = escape((row.get("site_name") or "").strip() or url)
    return f'    <DT><A HREF="{url}">{name}</A>'


def render_bookmarks_html(
    sites: list[dict[str, Any]],
    tags_by_site: dict[int, list[str]],
) -> str:
    """渲染为可回灌浏览器的书签 HTML：每个标签一个文件夹，无标签站点放顶层。

    站点在各自标签的文件夹内各出现一次；文件夹间与站点内顺序保持入参顺序
    （调用方按 sort_order ASC, id ASC 传入）。多标签站点会出现在多个文件夹。
    """
    lines = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        "<!-- PrivLink 自动导出，可导入 Edge / Chrome / Firefox -->",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        "<TITLE>PrivLink 导航书签</TITLE>",
        "<H1>PrivLink 导航书签</H1>",
        "<DL><p>",
    ]
    tag_names = sorted(
        {name for names in tags_by_site.values() for name in names},
        key=str.lower,
    )
    for tag in tag_names:
        lines.append(f"    <DT><H3>{escape(tag)}</H3>")
        lines.append("    <DL><p>")
        for row in sites:
            if tag in tags_by_site.get(int(row["id"]), []):
                lines.append(_bookmark_line(row))
        lines.append("    </DL><p>")
    for row in sites:
        if not tags_by_site.get(int(row["id"])):
            lines.append(_bookmark_line(row))
    lines.append("</DL><p>")
    return "\n".join(lines) + "\n"
