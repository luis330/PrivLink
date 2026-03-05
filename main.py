from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib import parse

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_HOST = "0.0.0.0"
APP_PORT = 8000
DB_PATH = Path("data") / "sites.db"
ICON_DIR = Path("ICON")
FRONTEND_PATH = Path("index.html")
ICON_MAX_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 10
MAX_RETRIES = 2
MAX_REDIRECTS = 5
USER_AGENT = "NavLocalBot/1.0 (+https://localhost)"
ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_ICON_EXTENSIONS = {
    ".ico",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".webp",
    ".gif",
    ".bmp",
    ".avif",
}
CONTENT_TYPE_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
}


class SSRFBlockedError(ValueError):
    pass


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


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init_storage() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                site_name TEXT,
                icon_rel_path TEXT,
                icon_source_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_status TEXT NOT NULL,
                last_error TEXT
            );
            """
        )
        conn.commit()


def normalize_url(raw_url: str) -> str:
    value = (raw_url or "").strip()
    parsed = parse.urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError("URL scheme must be http or https")
    if not parsed.netloc:
        raise ValueError("URL host is required")

    netloc = normalize_netloc(parsed.netloc)
    path = parsed.path or ""
    if path == "/":
        path = ""
    elif path:
        path = path.rstrip("/")

    return parse.urlunsplit((scheme, netloc, path, parsed.query, ""))


def normalize_netloc(netloc: str) -> str:
    parsed = parse.urlsplit(f"//{netloc}")
    host = parsed.hostname
    if not host:
        raise ValueError("Invalid host")

    auth = ""
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth = f"{auth}:{parsed.password}"
        auth = f"{auth}@"

    host_lower = host.lower()
    if ":" in host_lower and not host_lower.startswith("["):
        host_lower = f"[{host_lower}]"

    port = ""
    if parsed.port:
        port = f":{parsed.port}"

    return f"{auth}{host_lower}{port}"


def validate_remote_url(target_url: str) -> None:
    parsed = parse.urlsplit(target_url)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SSRFBlockedError("Only http/https URLs are allowed")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise SSRFBlockedError("URL host is missing")
    if host == "localhost" or host.endswith(".local"):
        raise SSRFBlockedError("Local host is not allowed")

    if is_disallowed_ip(host):
        raise SSRFBlockedError(f"Disallowed host: {host}")

    port = parsed.port
    if not port:
        port = 443 if scheme == "https" else 80
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return

    for info in infos:
        address = info[4][0]
        if is_disallowed_ip(address):
            raise SSRFBlockedError(f"Disallowed resolved IP: {address}")


def is_disallowed_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def read_with_limit(byte_stream: Any, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for piece in byte_stream:
        if not piece:
            continue
        total += len(piece)
        if total > max_bytes:
            raise ValueError(f"Response exceeds limit ({max_bytes} bytes)")
        chunks.append(piece)
    return b"".join(chunks)


def format_http_error(response: httpx.Response) -> str:
    reason = (response.reason_phrase or "").strip()
    if not reason:
        return f"HTTP Error {response.status_code}"
    return f"HTTP Error {response.status_code}: {reason}"


def fetch_once_with_client(
    client: httpx.Client,
    target_url: str,
    *,
    max_bytes: int,
    headers: dict[str, str],
) -> tuple[str, bytes, str]:
    current_url = target_url
    for redirect_count in range(MAX_REDIRECTS + 1):
        validate_remote_url(current_url)
        with client.stream("GET", current_url, headers=headers, follow_redirects=False) as response:
            status_code = response.status_code
            if status_code in (301, 302, 303, 307, 308):
                location = (response.headers.get("Location") or "").strip()
                if not location:
                    raise RuntimeError("Redirect response missing Location header")
                next_url = parse.urljoin(current_url, location)
                current_url = normalize_url(next_url)
                continue
            if status_code >= 400:
                raise RuntimeError(format_http_error(response))

            raw_content_type = (response.headers.get("Content-Type") or "").strip().lower()
            content_type = raw_content_type.split(";")[0].strip()
            content_length = (response.headers.get("Content-Length") or "").strip()
            if content_length.isdigit() and int(content_length) > max_bytes:
                raise ValueError(f"Response exceeds limit ({max_bytes} bytes)")

            body = read_with_limit(response.iter_bytes(chunk_size=64 * 1024), max_bytes=max_bytes)
            final_url = normalize_url(str(response.url))
            return final_url, body, content_type

    raise RuntimeError(f"Too many redirects (>{MAX_REDIRECTS})")


def fetch_url(
    target_url: str,
    *,
    max_bytes: int,
    accept: str,
) -> tuple[str, bytes, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, trust_env=True) as client:
                return fetch_once_with_client(
                    client,
                    target_url,
                    max_bytes=max_bytes,
                    headers=headers,
                )
        except (
            httpx.TimeoutException,
            httpx.TransportError,
            ValueError,
            SSRFBlockedError,
            RuntimeError,
        ) as exc:
            last_error = exc
            if attempt >= MAX_RETRIES:
                break
            time.sleep(0.2 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch URL: {last_error}")


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


def choose_extension(icon_url: str, content_type: str) -> str:
    ext = Path(parse.urlsplit(icon_url).path).suffix.lower()
    if ext in ALLOWED_ICON_EXTENSIONS:
        return ext
    if content_type in CONTENT_TYPE_TO_EXT:
        return CONTENT_TYPE_TO_EXT[content_type]
    guessed = CONTENT_TYPE_TO_EXT.get(content_type.split(";")[0].strip().lower(), "")
    if guessed:
        return guessed
    return ".ico"


def icon_filename(normalized_url: str, extension: str) -> str:
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:24]
    return f"{digest}{extension}"


def download_icon(candidates: list[IconCandidate], normalized_url: str) -> tuple[str, str, str]:
    last_error = ""
    for candidate in candidates:
        try:
            validate_remote_url(candidate.url)
            final_icon_url, body, content_type = fetch_url(
                candidate.url,
                max_bytes=ICON_MAX_BYTES,
                accept="image/*,*/*;q=0.5",
            )
            if not content_type.startswith("image/"):
                raise ValueError(f"Invalid content-type: {content_type or 'unknown'}")
            if not body:
                raise ValueError("Empty icon content")

            extension = choose_extension(final_icon_url, content_type)
            filename = icon_filename(normalized_url, extension)
            relative_path = Path("ICON") / filename
            absolute_path = Path.cwd() / relative_path
            absolute_path.write_bytes(body)
            return relative_path.as_posix(), final_icon_url, ""
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            continue
    return "", "", last_error


def upsert_site_record(
    *,
    url: str,
    site_name: str,
    icon_rel_path: str,
    icon_source_url: str,
    status: str,
    error_text: str,
) -> str:
    now = utc_now()
    with sqlite3.connect(DB_PATH) as conn:
        old_row = conn.execute("SELECT icon_rel_path FROM sites WHERE url = ?;", (url,)).fetchone()
        old_icon_path = (old_row[0] or "").strip() if old_row else ""
        conn.execute(
            """
            INSERT INTO sites (
                url,
                site_name,
                icon_rel_path,
                icon_source_url,
                created_at,
                updated_at,
                last_status,
                last_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                site_name = excluded.site_name,
                icon_rel_path = excluded.icon_rel_path,
                icon_source_url = excluded.icon_source_url,
                updated_at = excluded.updated_at,
                last_status = excluded.last_status,
                last_error = excluded.last_error;
            """,
            (
                url,
                site_name,
                icon_rel_path,
                icon_source_url,
                now,
                now,
                status,
                error_text or None,
            ),
        )
        conn.commit()
    return old_icon_path


def maybe_remove_old_icon(old_icon_path: str, new_icon_path: str) -> None:
    old_clean = (old_icon_path or "").strip()
    new_clean = (new_icon_path or "").strip()
    if not old_clean or old_clean == new_clean:
        return
    old_file = Path.cwd() / old_clean
    if old_file.exists() and old_file.is_file():
        old_file.unlink(missing_ok=True)


def process_site_url(raw_url: str) -> dict[str, str]:
    result = {
        "url": (raw_url or "").strip(),
        "final_url": "",
        "site_name": "",
        "icon_rel_path": "",
        "icon_source_url": "",
        "status": "failed",
        "error": "",
        "warning": "",
    }
    errors: list[str] = []

    try:
        normalized_url = normalize_url(raw_url)
        validate_remote_url(normalized_url)
    except Exception as exc:  # noqa: BLE001
        result["status"] = "invalid"
        result["error"] = str(exc)
        result["warning"] = ""
        return result

    final_url = normalized_url
    parser: SiteHTMLParser | None = None
    try:
        final_url, html_body, content_type = fetch_url(
            normalized_url,
            max_bytes=ICON_MAX_BYTES,
            accept="text/html,application/xhtml+xml,*/*;q=0.5",
        )
        if content_type and "html" not in content_type:
            errors.append(f"Unexpected HTML content-type: {content_type}")
        parser = SiteHTMLParser()
        parser.feed(decode_html(html_body))
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))

    site_name = choose_site_name(parser, final_url)
    icon_candidates = build_icon_candidates(parser, final_url)
    icon_rel_path, icon_source_url, icon_error = download_icon(icon_candidates, normalized_url)
    if icon_error:
        errors.append(icon_error)

    has_name = bool(site_name)
    has_icon = bool(icon_rel_path)
    if has_name and has_icon:
        status = "success"
    elif has_name or has_icon:
        status = "partial"
    else:
        status = "failed"

    warning_text = "; ".join(part for part in errors if part)
    error_text = warning_text
    if status == "success":
        error_text = ""
    old_icon = upsert_site_record(
        url=normalized_url,
        site_name=site_name,
        icon_rel_path=icon_rel_path,
        icon_source_url=icon_source_url,
        status=status,
        error_text=error_text,
    )
    maybe_remove_old_icon(old_icon, icon_rel_path)

    result.update(
        {
            "url": normalized_url,
            "final_url": final_url,
            "site_name": site_name,
            "icon_rel_path": icon_rel_path,
            "icon_source_url": icon_source_url,
            "status": status,
            "error": error_text,
            "warning": warning_text if status == "success" else "",
        }
    )
    return result


class ParseRequest(BaseModel):
    url: str = Field(min_length=1)


class ParseResponse(BaseModel):
    url: str
    final_url: str
    site_name: str
    icon_rel_path: str
    icon_source_url: str
    status: str
    error: str
    warning: str


class SiteItem(BaseModel):
    id: int
    url: str
    site_name: str
    icon_rel_path: str
    updated_at: str


class SiteUpdateRequest(BaseModel):
    site_name: str = Field(min_length=1)
    url: str = Field(min_length=1)


class MessageResponse(BaseModel):
    message: str


def error_payload(message: str) -> dict[str, str]:
    return {
        "url": "",
        "final_url": "",
        "site_name": "",
        "icon_rel_path": "",
        "icon_source_url": "",
        "status": "failed",
        "error": message,
        "warning": "",
    }


def to_site_item(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    site_name = (row[2] or "").strip()
    if not site_name:
        site_name = (parse.urlsplit(row[1]).hostname or row[1]).strip()
    return {
        "id": int(row[0]),
        "url": (row[1] or "").strip(),
        "site_name": site_name,
        "icon_rel_path": (row[3] or "").strip(),
        "updated_at": (row[4] or "").strip(),
    }


@asynccontextmanager
async def app_lifespan(_: FastAPI):
    init_storage()
    yield


app = FastAPI(title="Nav Local Service", version="1.0.0", lifespan=app_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/ICON", StaticFiles(directory=str(ICON_DIR), check_dir=False), name="icon")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content=error_payload(f"Invalid request: {exc}"))


@app.exception_handler(Exception)
async def generic_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content=error_payload(f"Internal server error: {exc}"))


@app.get("/", include_in_schema=False, response_model=None)
async def index_page():
    if not FRONTEND_PATH.exists():
        return JSONResponse(status_code=500, content=error_payload("Frontend file is missing"))
    return FileResponse(path=str(FRONTEND_PATH), media_type="text/html")


@app.get("/index.html", include_in_schema=False, response_model=None)
async def index_page_alias():
    return await index_page()


@app.post("/api/site/parse", response_model=ParseResponse)
async def parse_site(payload: ParseRequest) -> JSONResponse:
    raw_url = payload.url.strip()
    if not raw_url:
        return JSONResponse(status_code=400, content=error_payload("Field 'url' is required"))
    result = process_site_url(raw_url)
    status_code = 400 if result["status"] == "invalid" else 200
    return JSONResponse(status_code=status_code, content=result)


@app.get("/api/sites", response_model=list[SiteItem])
async def list_sites() -> list[dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, url, site_name, icon_rel_path, updated_at
            FROM sites
            ORDER BY updated_at DESC, id DESC;
            """
        ).fetchall()

    return [to_site_item(row) for row in rows]


@app.put("/api/sites/{site_id}", response_model=SiteItem)
async def update_site(site_id: int, payload: SiteUpdateRequest) -> JSONResponse | dict[str, Any]:
    next_name = (payload.site_name or "").strip()
    raw_url = (payload.url or "").strip()
    if not next_name or not raw_url:
        return JSONResponse(status_code=400, content={"error": "名称和网址不能为空"})
    try:
        normalized_url = normalize_url(raw_url)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(exc)})

    now = utc_now()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT id, url, site_name, icon_rel_path, updated_at
            FROM sites
            WHERE id = ?;
            """,
            (site_id,),
        ).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": "网站不存在"})
        try:
            conn.execute(
                """
                UPDATE sites
                SET url = ?, site_name = ?, updated_at = ?
                WHERE id = ?;
                """,
                (normalized_url, next_name, now, site_id),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return JSONResponse(status_code=409, content={"error": "该网址已存在"})

        updated_row = conn.execute(
            """
            SELECT id, url, site_name, icon_rel_path, updated_at
            FROM sites
            WHERE id = ?;
            """,
            (site_id,),
        ).fetchone()
    if not updated_row:
        return JSONResponse(status_code=404, content={"error": "网站不存在"})
    return to_site_item(updated_row)


@app.delete("/api/sites/{site_id}", response_model=MessageResponse)
async def delete_site(site_id: int) -> JSONResponse:
    icon_rel_path = ""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT icon_rel_path FROM sites WHERE id = ?;", (site_id,)).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": "网站不存在"})
        icon_rel_path = (row[0] or "").strip()
        conn.execute("DELETE FROM sites WHERE id = ?;", (site_id,))
        conn.commit()

    maybe_remove_old_icon(icon_rel_path, "")
    return JSONResponse(status_code=200, content={"message": "ok"})


def main() -> None:
    import uvicorn

    init_storage()
    uvicorn.run("main:app", host=APP_HOST, port=APP_PORT, workers=1)


if __name__ == "__main__":
    main()
