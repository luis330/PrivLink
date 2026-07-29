from __future__ import annotations

import os
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import main


@contextmanager
def serve_text(body: str) -> Iterator[str]:
    payload = body.encode("ascii")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


class DirectPublicIPv4ResolverTest(unittest.TestCase):
    def test_returns_valid_ipv4_from_provider(self) -> None:
        def handle_request(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="8.8.8.8\n")

        resolver = main.DirectPublicIPv4Resolver(
            providers=("https://public-ip.test",),
            transport=httpx.MockTransport(handle_request),
        )

        self.assertEqual(resolver.resolve(), "8.8.8.8")

    def test_uses_backup_provider_when_primary_is_unavailable(self) -> None:
        def handle_request(request: httpx.Request) -> httpx.Response:
            if request.url.host == "primary.test":
                return httpx.Response(503, text="unavailable")
            return httpx.Response(200, text="1.1.1.1\n")

        resolver = main.DirectPublicIPv4Resolver(
            providers=("https://primary.test", "https://backup.test"),
            transport=httpx.MockTransport(handle_request),
        )

        self.assertEqual(resolver.resolve(), "1.1.1.1")

    def test_rejects_non_ipv4_response_and_uses_backup_provider(self) -> None:
        def handle_request(request: httpx.Request) -> httpx.Response:
            if request.url.host == "primary.test":
                return httpx.Response(200, text="2001:db8::1")
            return httpx.Response(200, text="9.9.9.9")

        resolver = main.DirectPublicIPv4Resolver(
            providers=("https://primary.test", "https://backup.test"),
            transport=httpx.MockTransport(handle_request),
        )

        self.assertEqual(resolver.resolve(), "9.9.9.9")

    def test_rejects_non_public_ipv4_response_and_uses_backup_provider(self) -> None:
        def handle_request(request: httpx.Request) -> httpx.Response:
            if request.url.host == "primary.test":
                return httpx.Response(200, text="192.168.1.10")
            return httpx.Response(200, text="8.8.4.4")

        resolver = main.DirectPublicIPv4Resolver(
            providers=("https://primary.test", "https://backup.test"),
            transport=httpx.MockTransport(handle_request),
        )

        self.assertEqual(resolver.resolve(), "8.8.4.4")

    def test_ignores_environment_proxy(self) -> None:
        with serve_text("8.8.4.4") as provider_url:
            with serve_text("1.0.0.1") as proxy_url:
                proxy_environment = {
                    "HTTP_PROXY": proxy_url,
                    "HTTPS_PROXY": proxy_url,
                    "ALL_PROXY": proxy_url,
                    "NO_PROXY": "",
                    "http_proxy": proxy_url,
                    "https_proxy": proxy_url,
                    "all_proxy": proxy_url,
                    "no_proxy": "",
                }
                with patch.dict(os.environ, proxy_environment):
                    resolver = main.DirectPublicIPv4Resolver(providers=(provider_url,))
                    public_ip = resolver.resolve()

        self.assertEqual(public_ip, "8.8.4.4")


class PublicIpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def test_returns_public_ipv4_without_http_caching(self) -> None:
        def handle_request(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="8.8.8.8")

        resolver = main.DirectPublicIPv4Resolver(
            providers=("https://public-ip.test",),
            transport=httpx.MockTransport(handle_request),
        )

        with patch.object(main, "public_ipv4_resolver", resolver, create=True):
            response = self.client.get("/api/network/public-ip")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ip": "8.8.8.8"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_returns_bad_gateway_when_all_providers_fail(self) -> None:
        def handle_request(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="unavailable")

        resolver = main.DirectPublicIPv4Resolver(
            providers=("https://primary.test", "https://backup.test"),
            transport=httpx.MockTransport(handle_request),
        )

        with patch.object(main, "public_ipv4_resolver", resolver):
            response = self.client.get("/api/network/public-ip")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"error": "无法获取直连公网 IPv4"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")


class PublicIpHomepageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def test_refresh_label_does_not_wrap_the_ip_value(self) -> None:
        response = self.client.get("/")
        html = response.text

        refresh_start = html.index('id="public-ip-refresh"')
        refresh_end = html.index("</button>", refresh_start)
        value_start = html.index('id="public-ip-value"')

        self.assertIn("公网IP：", html[refresh_start:refresh_end])
        self.assertLess(refresh_end, value_start)

    def test_fetches_public_ip_on_load_and_refresh_without_cache(self) -> None:
        response = self.client.get("/")
        html = response.text

        self.assertIn('return "/api/network/public-ip";', html)
        self.assertIn('cache: "no-store"', html)
        self.assertIn(
            'publicIpRefreshBtn.addEventListener("click", loadPublicIp);',
            html,
        )
        self.assertIn('publicIpValueEl.textContent = "获取失败";', html)
        self.assertIn("loadPublicIp();", html)


if __name__ == "__main__":
    unittest.main()
