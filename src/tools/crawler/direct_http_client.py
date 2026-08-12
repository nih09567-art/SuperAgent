from __future__ import annotations

import html
import ipaddress
import os
import socket
from urllib.parse import urljoin, urlsplit

import requests


class UnsafeCrawlUrl(ValueError):
    """Raised when a URL could reach a non-public network resource."""


def _positive_float(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _validate_public_url(url: str) -> None:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeCrawlUrl("crawl URL must use HTTP or HTTPS")
    if parsed.username or parsed.password:
        raise UnsafeCrawlUrl("crawl URL must not contain credentials")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeCrawlUrl("crawl URL contains an invalid port") from exc
    if port not in {80, 443}:
        raise UnsafeCrawlUrl("crawl URL must use port 80 or 443")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise requests.ConnectionError("crawl host cannot be resolved") from exc
    if not addresses:
        raise requests.ConnectionError("crawl host did not resolve to an address")
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsafeCrawlUrl("crawl URL resolves to a non-public address")


class DirectHttpClient:
    """Fetch public HTML directly with bounded network and payload budgets."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._connect_timeout = _positive_float(
            "CRAWLER_CONNECT_TIMEOUT_SECONDS", 5.0
        )
        self._read_timeout = _positive_float("CRAWLER_READ_TIMEOUT_SECONDS", 20.0)
        self._max_response_bytes = _positive_int(
            "CRAWLER_MAX_RESPONSE_BYTES", 5_000_000
        )
        self._max_redirects = _positive_int("CRAWLER_MAX_REDIRECTS", 5)

    def crawl(self, url: str) -> str:
        current_url = str(url or "").strip()
        for _redirect in range(self._max_redirects + 1):
            _validate_public_url(current_url)
            response = self._session.get(
                current_url,
                allow_redirects=False,
                stream=True,
                timeout=(self._connect_timeout, self._read_timeout),
                headers={
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
                    "User-Agent": "SuperAgentResearchCrawler/1.0",
                },
            )
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location", "").strip()
                response.close()
                if not location:
                    raise requests.HTTPError("crawl redirect has no Location header")
                current_url = urljoin(current_url, location)
                continue

            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and not any(
                allowed in content_type
                for allowed in ("text/html", "application/xhtml+xml", "text/plain")
            ):
                response.close()
                raise ValueError("crawl response is not readable HTML or text")

            payload = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                payload.extend(chunk)
                if len(payload) > self._max_response_bytes:
                    response.close()
                    raise ValueError("crawl response exceeds the configured size limit")
            encoding = response.encoding or "utf-8"
            response.close()
            text = bytes(payload).decode(encoding, errors="replace")
            if "text/plain" in content_type:
                return f"<html><body><pre>{html.escape(text)}</pre></body></html>"
            return text

        raise requests.TooManyRedirects("crawl response exceeded redirect limit")
