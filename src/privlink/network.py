from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from typing import Any
from urllib import parse

from privlink import config


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
    raw_value = (os.environ.get(config.HOST_ALIASES_ENV) or "").strip()
    aliases: list[HostAlias] = []
    for token in raw_value.split(","):
        value = token.strip()
        if not value:
            continue
        if "=" not in value:
            config.logger.warning("忽略无效主机解析映射: %s", value)
            continue

        pattern, target_ip = (part.strip().lower() for part in value.split("=", 1))
        if not pattern or not target_ip:
            config.logger.warning("忽略无效主机解析映射: %s", value)
            continue
        try:
            normalized_ip = str(ipaddress.ip_address(target_ip.strip("[]")))
        except ValueError:
            config.logger.warning("忽略无效主机解析目标 IP: %s", value)
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
    raw_value = (os.environ.get("NAV_ALLOWED_PRIVATE_NETWORKS") or config.DEFAULT_ALLOWED_PRIVATE_NETWORKS).strip()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for token in raw_value.split(","):
        value = token.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            config.logger.warning("忽略无效内网白名单网段: %s", value)
    return tuple(networks)


ALLOWED_PRIVATE_NETWORKS = load_allowed_private_networks()
HOST_ALIASES = load_host_aliases()
# Pyodide 的 socket 模块不保证提供 getaddrinfo；缺失时不做主机别名接管
ORIGINAL_GETADDRINFO = getattr(socket, "getaddrinfo", None)


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


if HOST_ALIASES and ORIGINAL_GETADDRINFO is not None:
    socket.getaddrinfo = getaddrinfo_with_aliases  # type: ignore[assignment]
    config.logger.info(
        "启用主机解析映射: %s",
        ", ".join(f"{alias.pattern}->{alias.target_ip}" for alias in HOST_ALIASES),
    )


class SSRFBlockedError(ValueError):
    pass


def normalize_url(raw_url: str) -> str:
    value = (raw_url or "").strip()
    parsed = parse.urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in config.ALLOWED_SCHEMES:
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
    if scheme not in config.ALLOWED_SCHEMES:
        raise SSRFBlockedError("Only http/https URLs are allowed")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise SSRFBlockedError("URL host is missing")
    if host == "localhost" or host.endswith(".local"):
        raise SSRFBlockedError("Local host is not allowed")

    if is_disallowed_ip(host):
        raise SSRFBlockedError(f"Disallowed host: {host}")

    if config.IS_WORKERS:
        # Workers 不做 DNS 解析：Pyodide 无可用 getaddrinfo，
        # 且解析结果由平台 fetch 自行处理（Workers fetch 本身无法访问内网）
        return

    port = parsed.port
    if not port:
        port = 443 if scheme == "https" else 80
    resolver = getattr(socket, "getaddrinfo", None)
    if resolver is None:
        return
    try:
        infos = resolver(host, port, type=socket.SOCK_STREAM)
    except OSError:
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


def target_resolves_to_allowed_private(target_url: str) -> bool:
    if config.IS_WORKERS:
        return False
    parsed = parse.urlsplit(target_url)
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    resolver = getattr(socket, "getaddrinfo", None)
    if resolver is None:
        return False
    try:
        infos = resolver(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if any(ip in network for network in ALLOWED_PRIVATE_NETWORKS):
            return True
    return False
