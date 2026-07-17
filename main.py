from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import shutil
import socket
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterator
from urllib import parse

import httpx
from fastapi import FastAPI, File, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_HOST = "0.0.0.0"
APP_PORT = 8000
DB_PATH = Path("data") / "sites.db"
ICON_DIR = Path("ICON")
ICONS_LIB_DIR = Path("icons")
FRONTEND_PATH = Path("index.html")
ICON_MAX_BYTES = 2 * 1024 * 1024
ICON_UPLOAD_MAX_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 10
MAX_RETRIES = 2
MAX_REDIRECTS = 5
MAX_RETRY_AFTER_SECONDS = 5
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
USER_AGENT = (os.environ.get("NAV_USER_AGENT") or DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT
ACCEPT_LANGUAGE = (os.environ.get("NAV_ACCEPT_LANGUAGE") or "zh-CN,zh;q=0.9,en;q=0.8").strip()
INGEST_TOKEN = (os.environ.get("NAV_INGEST_TOKEN") or "").strip()
INGEST_TOKEN_SETTING_KEY = "ingest_token"
PROXY_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
ALLOWED_SCHEMES = {"http", "https"}
DEFAULT_ALLOWED_PRIVATE_NETWORKS = "192.168.50.0/24"
HOST_ALIASES_ENV = "NAV_HOST_ALIASES"
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
ALLOWED_UPLOAD_ICON_EXTENSIONS = {".ico", ".png", ".svg"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("nav-local")


@dataclass(frozen=True)
class HostAlias:
    pattern: str
    target_ip: str
    wildcard_suffix: str

    def matches(self, host: str) -> bool:
        host_lower = host.strip().strip("[]").lower()
        if not host_lower:
            return False
        if self.wildcard_suffix:
            return (
                host_lower.endswith(self.wildcard_suffix)
                and host_lower != self.wildcard_suffix.lstrip(".")
            )
        return host_lower == self.pattern


def load_host_aliases() -> tuple[HostAlias, ...]:
    raw_value = (os.environ.get(HOST_ALIASES_ENV) or "").strip()
    aliases: list[HostAlias] = []
    for token in raw_value.split(","):
        value = token.strip()
        if not value:
            continue
        if "=" not in value:
            logger.warning("忽略无效主机解析映射: %s", value)
            continue

        pattern, target_ip = (part.strip().lower() for part in value.split("=", 1))
        if not pattern or not target_ip:
            logger.warning("忽略无效主机解析映射: %s", value)
            continue
        try:
            normalized_ip = str(ipaddress.ip_address(target_ip.strip("[]")))
        except ValueError:
            logger.warning("忽略无效主机解析目标 IP: %s", value)
            continue

        wildcard_suffix = ""
        if pattern.startswith("*."):
            wildcard_suffix = pattern[1:]
        elif pattern.startswith("."):
            wildcard_suffix = pattern
        aliases.append(
            HostAlias(pattern=pattern, target_ip=normalized_ip, wildcard_suffix=wildcard_suffix)
        )
    return tuple(aliases)


def load_allowed_private_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    raw_value = (os.environ.get("NAV_ALLOWED_PRIVATE_NETWORKS") or DEFAULT_ALLOWED_PRIVATE_NETWORKS).strip()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for token in raw_value.split(","):
        value = token.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            logger.warning("忽略无效内网白名单网段: %s", value)
    return tuple(networks)


ALLOWED_PRIVATE_NETWORKS = load_allowed_private_networks()
HOST_ALIASES = load_host_aliases()
ORIGINAL_GETADDRINFO = socket.getaddrinfo


def resolve_host_alias(host: str) -> str | None:
    for alias in HOST_ALIASES:
        if alias.matches(host):
            return alias.target_ip
    return None


def getaddrinfo_with_aliases(
    host: bytes | str | None,
    port: str | int | None,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> list[tuple[Any, ...]]:
    if isinstance(host, str):
        target_ip = resolve_host_alias(host)
        if target_ip:
            return ORIGINAL_GETADDRINFO(target_ip, port, family, type, proto, flags)
    return ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)


if HOST_ALIASES:
    socket.getaddrinfo = getaddrinfo_with_aliases  # type: ignore[assignment]
    logger.info(
        "启用主机解析映射: %s",
        ", ".join(f"{alias.pattern}->{alias.target_ip}" for alias in HOST_ALIASES),
    )


class SSRFBlockedError(ValueError):
    pass


class FetchHTTPStatusError(RuntimeError):
    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        self.retry_after_seconds = parse_retry_after(response.headers.get("Retry-After"))
        super().__init__(format_http_error(response))


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


TAG_NAME_MAX_LEN = 20


@contextmanager
def db_connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def normalize_tag_name(raw: str) -> str:
    collapsed = " ".join((raw or "").split())
    return collapsed


def normalize_tag_list(raw_tags: list[str] | None) -> list[str]:
    if not raw_tags:
        return []
    seen: dict[str, str] = {}
    for item in raw_tags:
        name = normalize_tag_name(str(item))
        if not name:
            continue
        if len(name) > TAG_NAME_MAX_LEN:
            raise ValueError(f"标签长度不能超过 {TAG_NAME_MAX_LEN} 个字符")
        key = name.lower()
        if key not in seen:
            seen[key] = name
    return list(seen.values())


def init_storage() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        if INGEST_TOKEN:
            conn.execute(
                """
                INSERT OR IGNORE INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?);
                """,
                (INGEST_TOKEN_SETTING_KEY, INGEST_TOKEN, utc_now()),
            )
        conn.commit()

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

        # 迁移：添加 sort_order 列
        try:
            conn.execute("ALTER TABLE sites ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 列已存在

        # 初始化已有数据的 sort_order（仅所有值都为 0 时执行）
        all_zero = conn.execute("SELECT COUNT(*) FROM sites WHERE sort_order != 0").fetchone()[0]
        if all_zero == 0:
            rows = conn.execute(
                "SELECT id FROM sites ORDER BY updated_at DESC, id DESC"
            ).fetchall()
            for idx, row in enumerate(rows, start=1):
                conn.execute("UPDATE sites SET sort_order = ? WHERE id = ?", (idx, row[0]))
            if rows:
                conn.commit()

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS site_tags (
                site_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (site_id, tag_id),
                FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_site_tags_tag ON site_tags(tag_id);"
        )
        conn.commit()


def get_app_setting(key: str) -> str | None:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?;",
            (key,),
        ).fetchone()
    if not row:
        return None
    return str(row[0] or "")


def set_app_setting(key: str, value: str) -> None:
    now = utc_now()
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at;
            """,
            (key, value, now),
        )
        conn.commit()


def get_ingest_token() -> str:
    value = get_app_setting(INGEST_TOKEN_SETTING_KEY)
    if value is not None:
        return value.strip()
    return INGEST_TOKEN


def set_ingest_token(token: str) -> None:
    set_app_setting(INGEST_TOKEN_SETTING_KEY, token.strip())


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
    if any(ip in network for network in ALLOWED_PRIVATE_NETWORKS):
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


def parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized.isdigit():
        return None
    seconds = int(normalized)
    if seconds < 0 or seconds > MAX_RETRY_AFTER_SECONDS:
        return None
    return seconds


def proxy_env_configured() -> bool:
    return any((os.environ.get(name) or "").strip() for name in PROXY_ENV_NAMES)


def target_resolves_to_allowed_private(target_url: str) -> bool:
    parsed = parse.urlsplit(target_url)
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if any(ip in network for network in ALLOWED_PRIVATE_NETWORKS):
            return True
    return False


def fetch_modes(target_url: str) -> list[tuple[bool, str]]:
    if not proxy_env_configured():
        return [(True, "env")]
    if target_resolves_to_allowed_private(target_url):
        return [(False, "direct"), (True, "proxy/env")]
    return [(True, "proxy/env"), (False, "direct")]


def build_fetch_headers(accept: str, referer: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Language": ACCEPT_LANGUAGE,
    }
    if referer:
        headers["Referer"] = referer
    return headers


def should_retry_fetch_error(exc: Exception) -> bool:
    if isinstance(exc, FetchHTTPStatusError):
        if exc.status_code == 429:
            return exc.retry_after_seconds is not None
        return exc.status_code in {408, 500, 502, 503, 504}
    return True


def retry_delay(exc: Exception, attempt: int) -> float:
    if isinstance(exc, FetchHTTPStatusError) and exc.retry_after_seconds is not None:
        return float(exc.retry_after_seconds)
    return 0.2 * (attempt + 1)


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
                raise FetchHTTPStatusError(response)

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
    referer: str | None = None,
) -> tuple[str, bytes, str]:
    headers = build_fetch_headers(accept, referer)
    mode_errors: list[str] = []
    for trust_env, mode_label in fetch_modes(target_url):
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, trust_env=trust_env) as client:
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
                if attempt >= MAX_RETRIES or not should_retry_fetch_error(exc):
                    break
                logger.warning(
                    "请求 %s 使用 %s 第 %d 次失败: %s，重试中...",
                    target_url,
                    mode_label,
                    attempt + 1,
                    exc,
                )
                time.sleep(retry_delay(exc, attempt))
        if last_error:
            logger.warning("请求 %s 使用 %s 失败: %s", target_url, mode_label, last_error)
            mode_errors.append(f"{mode_label}: {last_error}")

    error_text = "; ".join(mode_errors) or "unknown error"
    logger.error("请求 %s 最终失败: %s", target_url, error_text)
    raise RuntimeError(f"Failed to fetch URL: {error_text}")


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
    if content_type in CONTENT_TYPE_TO_EXT:
        return CONTENT_TYPE_TO_EXT[content_type]
    guessed = CONTENT_TYPE_TO_EXT.get(content_type.split(";")[0].strip().lower(), "")
    if guessed:
        return guessed
    if ext in ALLOWED_ICON_EXTENSIONS:
        return ext
    return ".ico"


def icon_filename(normalized_url: str, extension: str) -> str:
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:24]
    return f"{digest}{extension}"


def icon_upload_filename(content: bytes, original_name: str) -> str:
    ext = Path(original_name).suffix.lower() or ".ico"
    digest = hashlib.sha256(content).hexdigest()[:24]
    return f"upload-{digest}{ext}"


def icon_extension_from_payload(source_url: str, filename: str, content_type: str) -> str:
    clean_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    payload_ext = ""
    for value in (filename, parse.urlsplit(source_url or "").path):
        ext = Path(value).suffix.lower()
        if ext in ALLOWED_ICON_EXTENSIONS:
            payload_ext = ext
            break

    if clean_content_type:
        mapped = CONTENT_TYPE_TO_EXT.get(clean_content_type)
        if mapped:
            return mapped
        if not clean_content_type.startswith("image/"):
            if payload_ext:
                return payload_ext
            raise ValueError(f"Invalid icon content-type: {clean_content_type}")

    if payload_ext:
        return payload_ext

    if clean_content_type.startswith("image/"):
        return ".ico"
    raise ValueError("Icon content-type or filename is required")


def decode_base64_icon(raw_data: str) -> bytes:
    data = (raw_data or "").strip()
    if not data:
        raise ValueError("Icon data is empty")
    if data.startswith("data:"):
        _, _, data = data.partition(",")
    try:
        content = base64.b64decode("".join(data.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid base64 icon data") from exc
    if not content:
        raise ValueError("Icon content is empty")
    if len(content) > ICON_UPLOAD_MAX_BYTES:
        raise ValueError("Icon size cannot exceed 1MB")
    return content


def write_browser_icon(
    *,
    normalized_url: str,
    source_url: str,
    filename: str,
    content_type: str,
    data_base64: str,
) -> tuple[str, str]:
    content = decode_base64_icon(data_base64)
    extension = icon_extension_from_payload(source_url, filename, content_type)
    filename_to_store = icon_filename(normalized_url, extension)
    relative_path = Path("ICON") / filename_to_store
    absolute_path = Path.cwd() / relative_path
    absolute_path.write_bytes(content)
    return relative_path.as_posix(), (source_url or f"browser-upload://{filename_to_store}").strip()


def copy_library_icon(icon_file: str) -> str:
    source = ICONS_LIB_DIR / icon_file
    key = "icon-lib:" + icon_file
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    filename = f"{digest}.svg"
    dest = ICON_DIR / filename
    shutil.copy2(str(source), str(dest))
    return (Path("ICON") / filename).as_posix()


def download_icon(
    candidates: list[IconCandidate],
    normalized_url: str,
    *,
    referer: str | None = None,
) -> tuple[str, str, str]:
    last_error = ""
    for candidate in candidates:
        try:
            validate_remote_url(candidate.url)
            final_icon_url, body, content_type = fetch_url(
                candidate.url,
                max_bytes=ICON_MAX_BYTES,
                accept="image/*,*/*;q=0.5",
                referer=referer,
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
            logger.info("图标下载成功: %s -> %s", candidate.url, relative_path)
            return relative_path.as_posix(), final_icon_url, ""
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            logger.debug("图标候选 %s 下载失败: %s", candidate.url, last_error)
            continue
    logger.warning("所有图标候选均失败 (%s): %s", normalized_url, last_error)
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
    with db_connect() as conn:
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
                last_error,
                sort_order
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
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
        logger.info("数据库写入成功: %s (状态: %s)", url, status)
    return old_icon_path


def maybe_remove_old_icon(old_icon_path: str, new_icon_path: str) -> None:
    old_clean = (old_icon_path or "").strip()
    new_clean = (new_icon_path or "").strip()
    if not old_clean or old_clean == new_clean:
        return
    old_file = Path.cwd() / old_clean
    if old_file.exists() and old_file.is_file():
        old_file.unlink(missing_ok=True)


def fetch_existing_icon(url: str) -> tuple[str, str]:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT icon_rel_path, icon_source_url FROM sites WHERE url = ?;",
            (url,),
        ).fetchone()
    if not row:
        return "", ""
    return (row[0] or "").strip(), (row[1] or "").strip()


def process_site_url(raw_url: str) -> dict[str, str]:
    logger.info("开始处理网站: %s", raw_url)
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
        logger.warning("URL 验证失败 (%s): %s", raw_url, exc)
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
        logger.warning("抓取页面失败 (%s): %s", normalized_url, exc)

    site_name = choose_site_name(parser, final_url)
    icon_candidates = build_icon_candidates(parser, final_url)
    icon_rel_path, icon_source_url, icon_error = download_icon(
        icon_candidates,
        normalized_url,
        referer=final_url,
    )
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
    logger.info("网站处理完成: %s [%s] 名称=%s 图标=%s", normalized_url, status, site_name, icon_rel_path or "无")

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


class BrowserIconPayload(BaseModel):
    source_url: str = ""
    content_type: str = ""
    filename: str = ""
    data_base64: str = Field(default="", max_length=ICON_UPLOAD_MAX_BYTES * 2)


class BrowserIngestRequest(BaseModel):
    url: str = Field(min_length=1)
    final_url: str = ""
    site_name: str = ""
    icon: BrowserIconPayload | None = None


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
    sort_order: int
    tags: list[str] = []


class SiteUpdateRequest(BaseModel):
    site_name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    icon_file: str | None = None
    tags: list[str] | None = None


class IngestTokenUpdateRequest(BaseModel):
    token: str = ""


class IngestTokenStatus(BaseModel):
    token: str
    configured: bool


class MessageResponse(BaseModel):
    message: str


def parse_origin_tuple(origin: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = parse.urlsplit(origin)
        if not parsed.scheme or not parsed.hostname:
            return None
        port = parsed.port
    except ValueError:
        return None

    scheme = parsed.scheme.lower()
    if port is None:
        if scheme == "http":
            port = 80
        elif scheme == "https":
            port = 443
    return (scheme, parsed.hostname.lower(), port)


def validate_settings_origin(request: Request) -> JSONResponse | None:
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return None

    host = (request.headers.get("host") or request.url.netloc).strip()
    current_origin = f"{request.url.scheme}://{host}"
    if parse_origin_tuple(origin) == parse_origin_tuple(current_origin):
        return None
    return JSONResponse(status_code=403, content={"error": "仅允许同源页面管理 token"})


def validate_ingest_token(request: Request) -> JSONResponse | None:
    ingest_token = get_ingest_token()
    if not ingest_token:
        return JSONResponse(status_code=403, content=error_payload("浏览器采集接口未启用"))

    provided = (request.headers.get("X-Nav-Token") or "").strip()
    if not provided or not secrets.compare_digest(provided, ingest_token):
        return JSONResponse(status_code=401, content=error_payload("浏览器采集 token 无效"))
    return None


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


def to_site_item(
    row: sqlite3.Row | tuple[Any, ...],
    tags: list[str] | None = None,
) -> dict[str, Any]:
    site_name = (row[2] or "").strip()
    if not site_name:
        site_name = (parse.urlsplit(row[1]).hostname or row[1]).strip()
    return {
        "id": int(row[0]),
        "url": (row[1] or "").strip(),
        "site_name": site_name,
        "icon_rel_path": (row[3] or "").strip(),
        "updated_at": (row[4] or "").strip(),
        "sort_order": int(row[5]),
        "tags": list(tags) if tags else [],
    }


def fetch_site_tags(conn: sqlite3.Connection, site_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT tags.name
        FROM site_tags
        JOIN tags ON tags.id = site_tags.tag_id
        WHERE site_tags.site_id = ?
        ORDER BY tags.name COLLATE NOCASE ASC;
        """,
        (site_id,),
    ).fetchall()
    return [row[0] for row in rows]


def fetch_all_site_tags(conn: sqlite3.Connection) -> dict[int, list[str]]:
    rows = conn.execute(
        """
        SELECT site_tags.site_id, tags.name
        FROM site_tags
        JOIN tags ON tags.id = site_tags.tag_id
        ORDER BY tags.name COLLATE NOCASE ASC;
        """
    ).fetchall()
    result: dict[int, list[str]] = {}
    for site_id, name in rows:
        result.setdefault(int(site_id), []).append(name)
    return result


def replace_site_tags(
    conn: sqlite3.Connection,
    site_id: int,
    names: list[str],
    now: str,
) -> None:
    conn.execute("DELETE FROM site_tags WHERE site_id = ?", (site_id,))
    if not names:
        return
    for name in names:
        conn.execute(
            "INSERT OR IGNORE INTO tags (name, created_at) VALUES (?, ?)",
            (name, now),
        )
    placeholders = ",".join("?" for _ in names)
    tag_rows = conn.execute(
        f"SELECT id, name FROM tags WHERE name IN ({placeholders}) COLLATE NOCASE",
        names,
    ).fetchall()
    for tag_id, _ in tag_rows:
        conn.execute(
            "INSERT OR IGNORE INTO site_tags (site_id, tag_id) VALUES (?, ?)",
            (site_id, tag_id),
        )


_icons_cache: list[dict[str, str]] = []


def scan_icons_library() -> list[dict[str, str]]:
    if not ICONS_LIB_DIR.is_dir():
        return []
    items: list[dict[str, str]] = []
    for f in sorted(ICONS_LIB_DIR.iterdir()):
        if f.suffix.lower() != ".svg" or not f.is_file():
            continue
        stem = f.stem
        if "_" in stem:
            name, keyword = stem.split("_", 1)
        else:
            name, keyword = stem, stem
        items.append({"name": name, "keyword": keyword, "file": f.name})
    return items


@asynccontextmanager
async def app_lifespan(_: FastAPI):
    global _icons_cache
    init_storage()
    _icons_cache = scan_icons_library()
    logger.info("服务启动完成，图标库加载 %d 个图标", len(_icons_cache))
    yield


app = FastAPI(title="Nav Local Service", version="1.0.0", lifespan=app_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


class CachedStaticFiles(StaticFiles):
    def __init__(self, *args: Any, cache_control: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cache_control = cache_control

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", self.cache_control)
        return response


app.mount(
    "/ICON",
    CachedStaticFiles(directory=str(ICON_DIR), check_dir=False, cache_control="public, max-age=86400"),
    name="icon",
)
app.mount(
    "/icons",
    CachedStaticFiles(directory=str(ICONS_LIB_DIR), cache_control="public, max-age=604800"),
    name="icon-lib",
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content=error_payload(f"Invalid request: {exc}"))


@app.exception_handler(Exception)
async def generic_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.error("未捕获异常: %s", exc, exc_info=True)
    return JSONResponse(status_code=500, content=error_payload(f"Internal server error: {exc}"))


_index_cache: tuple[float, bytes, str] | None = None


def load_index_page() -> tuple[bytes, str] | None:
    # 缓存 index.html 内容与内容 ETag，mtime 变化时自动重新加载
    global _index_cache
    try:
        mtime = FRONTEND_PATH.stat().st_mtime
    except OSError:
        return None
    if _index_cache is None or _index_cache[0] != mtime:
        body = FRONTEND_PATH.read_bytes()
        etag = f'"{hashlib.md5(body).hexdigest()}"'
        _index_cache = (mtime, body, etag)
    return _index_cache[1], _index_cache[2]


@app.get("/", include_in_schema=False, response_model=None)
async def index_page(request: Request):
    cached = load_index_page()
    if cached is None:
        return JSONResponse(status_code=500, content=error_payload("Frontend file is missing"))
    body, etag = cached
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if_none_match = request.headers.get("if-none-match", "")
    if if_none_match:
        received = {tag.strip().removeprefix("W/").strip('"') for tag in if_none_match.split(",")}
        if "*" in received or etag.strip('"') in received:
            return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="text/html", headers=headers)


@app.get("/index.html", include_in_schema=False, response_model=None)
async def index_page_alias(request: Request):
    return await index_page(request)


@app.get("/api/settings/ingest-token", response_model=IngestTokenStatus)
def read_ingest_token_setting(request: Request) -> JSONResponse | dict[str, Any]:
    auth_error = validate_settings_origin(request)
    if auth_error:
        return auth_error
    token = get_ingest_token()
    return {"token": token, "configured": bool(token)}


@app.put("/api/settings/ingest-token", response_model=IngestTokenStatus)
def update_ingest_token_setting(
    request: Request,
    payload: IngestTokenUpdateRequest,
) -> JSONResponse | dict[str, Any]:
    auth_error = validate_settings_origin(request)
    if auth_error:
        return auth_error
    token = payload.token.strip()
    set_ingest_token(token)
    return {"token": token, "configured": bool(token)}


@app.post("/api/site/parse", response_model=ParseResponse)
def parse_site(payload: ParseRequest) -> JSONResponse:
    raw_url = payload.url.strip()
    if not raw_url:
        return JSONResponse(status_code=400, content=error_payload("Field 'url' is required"))
    result = process_site_url(raw_url)
    status_code = 400 if result["status"] == "invalid" else 200
    return JSONResponse(status_code=status_code, content=result)


@app.get("/api/sites", response_model=list[SiteItem])
def list_sites() -> list[dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, url, site_name, icon_rel_path, updated_at, sort_order
            FROM sites
            ORDER BY sort_order ASC, id ASC;
            """
        ).fetchall()
        tags_by_site = fetch_all_site_tags(conn)

    return [to_site_item(row, tags_by_site.get(int(row[0]), [])) for row in rows]


@app.get("/api/icons")
async def list_icons(q: str = "") -> list[dict[str, str]]:
    query = q.strip().lower()
    if not query:
        return _icons_cache
    return [
        item
        for item in _icons_cache
        if query in item["name"].lower() or query in item["keyword"].lower()
    ]


class TagItem(BaseModel):
    name: str
    count: int


@app.get("/api/tags", response_model=list[TagItem])
def list_tags() -> list[dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT tags.name, COUNT(site_tags.site_id) AS usage_count
            FROM tags
            LEFT JOIN site_tags ON site_tags.tag_id = tags.id
            GROUP BY tags.id
            ORDER BY tags.name COLLATE NOCASE ASC;
            """
        ).fetchall()
    return [{"name": row[0], "count": int(row[1])} for row in rows]


def process_browser_ingest(payload: BrowserIngestRequest) -> dict[str, str]:
    logger.info("开始处理浏览器上报网站: %s", payload.url)
    result = {
        "url": (payload.url or "").strip(),
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
        normalized_url = normalize_url(payload.url)
        validate_remote_url(normalized_url)
        final_url = normalize_url(payload.final_url) if payload.final_url.strip() else normalized_url
        validate_remote_url(final_url)
    except Exception as exc:  # noqa: BLE001
        result["status"] = "invalid"
        result["error"] = str(exc)
        logger.warning("浏览器上报 URL 验证失败 (%s): %s", payload.url, exc)
        return result

    site_name = " ".join((payload.site_name or "").split()).strip()
    if not site_name:
        site_name = (parse.urlsplit(final_url).hostname or parse.urlsplit(normalized_url).hostname or "").strip()

    icon_rel_path = ""
    icon_source_url = ""
    if payload.icon and payload.icon.data_base64.strip():
        try:
            icon_rel_path, icon_source_url = write_browser_icon(
                normalized_url=normalized_url,
                source_url=payload.icon.source_url.strip(),
                filename=payload.icon.filename.strip(),
                content_type=payload.icon.content_type.strip(),
                data_base64=payload.icon.data_base64,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            logger.warning("浏览器上报图标写入失败 (%s): %s", normalized_url, exc)

    old_icon_rel_path = ""
    if not icon_rel_path:
        old_icon_rel_path, old_icon_source_url = fetch_existing_icon(normalized_url)
        icon_rel_path = old_icon_rel_path
        icon_source_url = old_icon_source_url

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
    if status == "success" and not warning_text:
        error_text = ""

    old_icon = upsert_site_record(
        url=normalized_url,
        site_name=site_name,
        icon_rel_path=icon_rel_path,
        icon_source_url=icon_source_url,
        status=status,
        error_text=error_text,
    )
    if icon_rel_path and icon_rel_path != old_icon_rel_path:
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


@app.post("/api/site/ingest", response_model=ParseResponse)
def ingest_site(request: Request, payload: BrowserIngestRequest) -> JSONResponse:
    auth_error = validate_ingest_token(request)
    if auth_error:
        return auth_error
    result = process_browser_ingest(payload)
    status_code = 400 if result["status"] == "invalid" else 200
    return JSONResponse(status_code=status_code, content=result)


class ReorderRequest(BaseModel):
    site_ids: list[int] = Field(min_length=1)


@app.put("/api/sites/reorder", response_model=MessageResponse)
def reorder_sites(payload: ReorderRequest) -> JSONResponse:
    site_ids = payload.site_ids
    with db_connect() as conn:
        existing_ids = {
            row[0]
            for row in conn.execute("SELECT id FROM sites").fetchall()
        }
        for sid in site_ids:
            if sid not in existing_ids:
                return JSONResponse(
                    status_code=400,
                    content={"error": f"网站 ID {sid} 不存在"},
                )
        for idx, sid in enumerate(site_ids, start=1):
            conn.execute(
                "UPDATE sites SET sort_order = ? WHERE id = ?",
                (idx, sid),
            )
        conn.commit()
    return JSONResponse(status_code=200, content={"message": "ok"})


@app.put("/api/sites/{site_id}", response_model=SiteItem)
def update_site(site_id: int, payload: SiteUpdateRequest) -> JSONResponse | dict[str, Any]:
    next_name = (payload.site_name or "").strip()
    raw_url = (payload.url or "").strip()
    if not next_name or not raw_url:
        return JSONResponse(status_code=400, content={"error": "名称和网址不能为空"})
    try:
        normalized_url = normalize_url(raw_url)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(exc)})

    icon_file = (payload.icon_file or "").strip()
    new_icon_rel_path = ""
    if icon_file:
        if "/" in icon_file or "\\" in icon_file or ".." in icon_file:
            return JSONResponse(status_code=400, content={"error": "无效的图标文件名"})
        if not (ICONS_LIB_DIR / icon_file).is_file():
            return JSONResponse(status_code=400, content={"error": "图标文件不存在"})
        new_icon_rel_path = copy_library_icon(icon_file)

    try:
        normalized_tags = (
            normalize_tag_list(payload.tags) if payload.tags is not None else None
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    now = utc_now()
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT id, url, site_name, icon_rel_path, updated_at, sort_order
            FROM sites
            WHERE id = ?;
            """,
            (site_id,),
        ).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": "网站不存在"})
        old_icon_path = (row[3] or "").strip()
        try:
            if new_icon_rel_path:
                conn.execute(
                    """
                    UPDATE sites
                    SET url = ?, site_name = ?, icon_rel_path = ?,
                        icon_source_url = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (normalized_url, next_name, new_icon_rel_path,
                     f"icon-lib://{icon_file}", now, site_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE sites
                    SET url = ?, site_name = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (normalized_url, next_name, now, site_id),
                )
            if normalized_tags is not None:
                replace_site_tags(conn, site_id, normalized_tags, now)
            conn.commit()
        except sqlite3.IntegrityError:
            return JSONResponse(status_code=409, content={"error": "该网址已存在"})

        updated_row = conn.execute(
            """
            SELECT id, url, site_name, icon_rel_path, updated_at, sort_order
            FROM sites
            WHERE id = ?;
            """,
            (site_id,),
        ).fetchone()
        updated_tags = fetch_site_tags(conn, site_id) if updated_row else []

    if new_icon_rel_path:
        maybe_remove_old_icon(old_icon_path, new_icon_rel_path)
    if not updated_row:
        return JSONResponse(status_code=404, content={"error": "网站不存在"})
    return to_site_item(updated_row, updated_tags)


@app.post("/api/sites/{site_id}/icon", response_model=SiteItem)
async def upload_site_icon(site_id: int, icon: UploadFile = File(...)) -> JSONResponse | dict[str, Any]:
    filename = (icon.filename or "").strip()
    if not filename:
        return JSONResponse(status_code=400, content={"error": "请上传图标文件"})
    if Path(filename).suffix.lower() not in ALLOWED_UPLOAD_ICON_EXTENSIONS:
        return JSONResponse(status_code=400, content={"error": "仅支持 ico、png、svg 格式图标"})

    content = await icon.read()
    if not content:
        return JSONResponse(status_code=400, content={"error": "图标内容为空"})
    if len(content) > ICON_UPLOAD_MAX_BYTES:
        return JSONResponse(status_code=400, content={"error": "图标大小不能超过 1MB"})

    relative_path = Path("ICON") / icon_upload_filename(content, filename)
    absolute_path = Path.cwd() / relative_path
    absolute_path.write_bytes(content)

    now = utc_now()
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT id, url, site_name, icon_rel_path, updated_at, sort_order
            FROM sites
            WHERE id = ?;
            """,
            (site_id,),
        ).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": "网站不存在"})
        old_icon_path = (row[3] or "").strip()
        conn.execute(
            """
            UPDATE sites
            SET icon_rel_path = ?, icon_source_url = ?, updated_at = ?
            WHERE id = ?;
            """,
            (relative_path.as_posix(), f"upload://{relative_path.name}", now, site_id),
        )
        conn.commit()
        updated_row = conn.execute(
            """
            SELECT id, url, site_name, icon_rel_path, updated_at, sort_order
            FROM sites
            WHERE id = ?;
            """,
            (site_id,),
        ).fetchone()
        updated_tags = fetch_site_tags(conn, site_id) if updated_row else []

    maybe_remove_old_icon(old_icon_path, relative_path.as_posix())
    if not updated_row:
        return JSONResponse(status_code=404, content={"error": "网站不存在"})
    return to_site_item(updated_row, updated_tags)


@app.delete("/api/sites/{site_id}", response_model=MessageResponse)
def delete_site(site_id: int) -> JSONResponse:
    icon_rel_path = ""
    with db_connect() as conn:
        row = conn.execute("SELECT icon_rel_path FROM sites WHERE id = ?;", (site_id,)).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": "网站不存在"})
        icon_rel_path = (row[0] or "").strip()
        conn.execute("DELETE FROM sites WHERE id = ?;", (site_id,))
        conn.commit()

    maybe_remove_old_icon(icon_rel_path, "")
    logger.info("删除网站: id=%d", site_id)
    return JSONResponse(status_code=200, content={"message": "ok"})


def main() -> None:
    import uvicorn

    init_storage()
    uvicorn.run("main:app", host=APP_HOST, port=APP_PORT, workers=1)


if __name__ == "__main__":
    main()
