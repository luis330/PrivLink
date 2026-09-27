from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib import parse


@dataclass
class IconCandidate:
    url: str
    is_svg: bool
    size_score: int
    rank: int


class SiteHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.og_site_name = ""
        self.title_parts: list[str] = []
        self._in_title = False
        self.icon_links: list[dict[str, str]] = []

    @property
    def title(self) -> str:
        return " ".join(part.strip() for part in self.title_parts if part.strip()).strip()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        attr_map = {k.lower(): (v or "").strip() for k, v in attrs}
        if tag_lower == "meta":
            name = attr_map.get("name", "").lower()
            prop = attr_map.get("property", "").lower()
            content = attr_map.get("content", "").strip()
            if content and (name == "og:site_name" or prop == "og:site_name") and not self.og_site_name:
                self.og_site_name = content
            return

        if tag_lower == "title":
            self._in_title = True
            return

        if tag_lower != "link":
            return
        rel = attr_map.get("rel", "").lower()
        href = attr_map.get("href", "").strip()
        if not rel or not href:
            return
        if "icon" not in rel:
            return
        self.icon_links.append(
            {
                "rel": rel,
                "href": href,
                "sizes": attr_map.get("sizes", ""),
                "type": attr_map.get("type", "").lower(),
            }
        )

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


def decode_html(body: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="ignore")


def parse_sizes(sizes_value: str) -> int:
    if not sizes_value:
        return 0
    sizes = sizes_value.lower().strip()
    if "any" in sizes:
        return 100_000_000
    best = 0
    for token in sizes.split():
        if "x" not in token:
            continue
        left, right = token.split("x", 1)
        if not left.isdigit() or not right.isdigit():
            continue
        best = max(best, int(left) * int(right))
    return best


def build_icon_candidates(parser: SiteHTMLParser | None, base_url: str) -> list[IconCandidate]:
    candidates: list[IconCandidate] = []
    rank = 0
    if parser:
        for link in parser.icon_links:
            href = link.get("href", "")
            if not href:
                continue
            icon_url = parse.urljoin(base_url, href)
            parsed_icon = parse.urlsplit(icon_url)
            extension = Path(parsed_icon.path).suffix.lower()
            mime_type = link.get("type", "").lower()
            is_svg = extension == ".svg" or "svg" in mime_type
            size_score = parse_sizes(link.get("sizes", ""))
            candidates.append(
                IconCandidate(
                    url=icon_url,
                    is_svg=is_svg,
                    size_score=size_score,
                    rank=rank,
                )
            )
            rank += 1

    fallback = parse.urljoin(base_url, "/favicon.ico")
    candidates.append(
        IconCandidate(
            url=fallback,
            is_svg=False,
            size_score=0,
            rank=rank + 1,
        )
    )

    seen: set[str] = set()
    unique: list[IconCandidate] = []
    for candidate in sorted(candidates, key=lambda item: (not item.is_svg, -item.size_score, item.rank)):
        normalized = parse.urlunsplit(parse.urlsplit(candidate.url))
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(candidate)
    return unique


def choose_site_name(parser: SiteHTMLParser | None, fallback_url: str) -> str:
    if parser and parser.og_site_name.strip():
        return parser.og_site_name.strip()
    if parser and parser.title.strip():
        return parser.title.strip()
    return (parse.urlsplit(fallback_url).hostname or "").strip()
