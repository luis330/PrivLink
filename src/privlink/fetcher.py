from __future__ import annotations

import asyncio
import ipaddress
import os
from typing import Any, AsyncIterator
from urllib import parse

import httpx

from privlink import config, network


class FetchHTTPStatusError(RuntimeError):
    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        self.retry_after_seconds = parse_retry_after(response.headers.get("Retry-After"))
        super().__init__(format_http_error(response))


class PublicIPv4LookupError(RuntimeError):
    pass


async def read_with_limit(byte_stream: AsyncIterator[bytes], max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for piece in byte_stream:
        if not piece:
            continue
        total += len(piece)
        if total > max_bytes:
            raise ValueError(f"Response exceeds limit ({max_bytes} bytes)")
        chunks.append(piece)
    return b"".join(chunks)


class DirectPublicIPv4Resolver:
    def __init__(
        self,
        providers: tuple[str, ...] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # None = 调用时读取 config.PUBLIC_IPV4_PROVIDERS（模块级单例创建于导入时，
        # 默认参数若直接绑定该常量，之后对 config 的重绑将不生效）
        self.providers = providers
        self.transport = transport

    async def resolve(self) -> str:
        providers = self.providers if self.providers is not None else config.PUBLIC_IPV4_PROVIDERS
        async with httpx.AsyncClient(
            timeout=config.PUBLIC_IPV4_TIMEOUT_SECONDS,
            trust_env=False,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            for provider in providers:
                try:
                    async with client.stream(
                        "GET",
                        provider,
                        headers={"Accept": "text/plain"},
                    ) as response:
                        response.raise_for_status()
                        body = await read_with_limit(
                            response.aiter_bytes(chunk_size=config.PUBLIC_IPV4_MAX_BYTES + 1),
                            max_bytes=config.PUBLIC_IPV4_MAX_BYTES,
                        )
                    address = ipaddress.IPv4Address(body.decode("ascii").strip())
                    if not address.is_global:
                        raise ValueError(f"IPv4 address is not globally routable: {address}")
                    public_ip = str(address)
                except (httpx.HTTPError, UnicodeDecodeError, ValueError) as exc:
                    config.logger.warning("直连公网 IPv4 查询源失败: %s (%s)", provider, exc)
                    continue
                return public_ip
        raise PublicIPv4LookupError("所有直连公网 IPv4 查询源均不可用")


public_ipv4_resolver = DirectPublicIPv4Resolver()


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
    if seconds < 0 or seconds > config.MAX_RETRY_AFTER_SECONDS:
        return None
    return seconds


def proxy_env_configured() -> bool:
    return any((os.environ.get(name) or "").strip() for name in config.PROXY_ENV_NAMES)


def fetch_modes(target_url: str) -> list[tuple[bool, str]]:
    if not proxy_env_configured():
        return [(True, "env")]
    if network.target_resolves_to_allowed_private(target_url):
        return [(False, "direct"), (True, "proxy/env")]
    return [(True, "proxy/env"), (False, "direct")]


def build_fetch_headers(accept: str, referer: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": accept,
        "Accept-Language": config.ACCEPT_LANGUAGE,
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


async def fetch_once_with_client(
    client: httpx.AsyncClient,
    target_url: str,
    *,
    max_bytes: int,
    headers: dict[str, str],
) -> tuple[str, bytes, str]:
    current_url = target_url
    for redirect_count in range(config.MAX_REDIRECTS + 1):
        network.validate_remote_url(current_url)
        async with client.stream("GET", current_url, headers=headers, follow_redirects=False) as response:
            status_code = response.status_code
            if status_code in (301, 302, 303, 307, 308):
                location = (response.headers.get("Location") or "").strip()
                if not location:
                    raise RuntimeError("Redirect response missing Location header")
                next_url = parse.urljoin(current_url, location)
                current_url = network.normalize_url(next_url)
                continue
            if status_code >= 400:
                raise FetchHTTPStatusError(response)

            raw_content_type = (response.headers.get("Content-Type") or "").strip().lower()
            content_type = raw_content_type.split(";")[0].strip()
            content_length = (response.headers.get("Content-Length") or "").strip()
            if content_length.isdigit() and int(content_length) > max_bytes:
                raise ValueError(f"Response exceeds limit ({max_bytes} bytes)")

            body = await read_with_limit(response.aiter_bytes(chunk_size=64 * 1024), max_bytes=max_bytes)
            final_url = network.normalize_url(str(response.url))
            return final_url, body, content_type

    raise RuntimeError(f"Too many redirects (>{config.MAX_REDIRECTS})")


async def fetch_url(
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
        for attempt in range(config.MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT_SECONDS, trust_env=trust_env) as client:
                    return await fetch_once_with_client(
                        client,
                        target_url,
                        max_bytes=max_bytes,
                        headers=headers,
                    )
            except (
                httpx.TimeoutException,
                httpx.TransportError,
                ValueError,
                network.SSRFBlockedError,
                RuntimeError,
            ) as exc:
                last_error = exc
                if attempt >= config.MAX_RETRIES or not should_retry_fetch_error(exc):
                    break
                config.logger.warning(
                    "请求 %s 使用 %s 第 %d 次失败: %s，重试中...",
                    target_url,
                    mode_label,
                    attempt + 1,
                    exc,
                )
                await asyncio.sleep(retry_delay(exc, attempt))
        if last_error:
            config.logger.warning("请求 %s 使用 %s 失败: %s", target_url, mode_label, last_error)
            mode_errors.append(f"{mode_label}: {last_error}")

    error_text = "; ".join(mode_errors) or "unknown error"
    config.logger.error("请求 %s 最终失败: %s", target_url, error_text)
    raise RuntimeError(f"Failed to fetch URL: {error_text}")
