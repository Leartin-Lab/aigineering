"""Bounded public HTTPS acquisition with explicit host scope and pinned DNS.

This tool returns bytes and a receipt, never facts. It does not use ambient
cookies, proxies, credentials or redirects outside the caller's host scope.
Socket timeouts are not a process-level execution deadline.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

from aigineering import __version__
from aigineering.business.artifacts import MAX_ATTACHMENT_BYTES


def _target(url: str, allowed_hosts: tuple[str, ...]) -> tuple[str, str]:
    p = urlsplit(url)
    host = p.hostname
    if (
        p.scheme != "https"
        or not host
        or p.username is not None
        or p.password is not None
        or p.port not in (None, 443)
        or p.fragment
        or any(ord(c) < 33 for c in url)
    ):
        raise ValueError("acquisition requires a credential-free HTTPS URL on port 443")
    if host not in allowed_hosts:
        raise ValueError("acquisition host is outside explicit allowed_hosts")
    # Authentication is deliberately not accepted by this public acquisition
    # adapter. A separately configured institutional adapter can own it later.
    from aigineering.business.fulltext import _safe_url

    if _safe_url(url) != url:
        raise ValueError(
            "acquisition URL must not contain credentials or unsafe metadata"
        )
    return host, urlunsplit(("", "", p.path or "/", p.query, ""))


class _PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        if not addresses:
            raise ValueError("acquisition host has no address")
        # Reject mixed public/private answers, then connect to the exact checked
        # address without a second hostname lookup (including after redirects).
        for address in addresses:
            if not ipaddress.ip_address(address[4][0]).is_global:
                raise ValueError("acquisition host resolves to a non-public address")
        sock = socket.socket(addresses[0][0], addresses[0][1], addresses[0][2])
        try:
            sock.settimeout(self.timeout)
            sock.connect(addresses[0][4])
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def fetch_public(
    url: str,
    *,
    allowed_hosts: tuple[str, ...],
    timeout: float = 20,
    max_bytes: int = MAX_ATTACHMENT_BYTES,
) -> tuple[bytes, dict]:
    """Acquire one public resource, with at most three explicit-scope redirects."""
    if (
        not 0 < timeout <= 120
        or type(max_bytes) is not int
        or not 0 < max_bytes <= MAX_ATTACHMENT_BYTES
    ):
        raise ValueError("invalid acquisition timeout or byte limit")
    initial = url
    hops = []
    for _ in range(4):
        host, target = _target(url, allowed_hosts)
        connection = _PinnedHTTPS(
            host, timeout=timeout, context=ssl.create_default_context()
        )
        try:
            connection.request(
                "GET",
                target,
                headers={
                    "User-Agent": f"Aigineering/{__version__}",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
            hops.append({"url": url, "status": response.status})
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise ValueError("acquisition redirect has no location")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f"acquisition HTTP status {response.status}")
            encoding = response.getheader("Content-Encoding", "identity")
            if encoding.lower() != "identity":
                raise ValueError("compressed transfer representation is unsupported")
            length = response.getheader("Content-Length")
            if length is not None and (int(length) < 0 or int(length) > max_bytes):
                raise ValueError("acquisition exceeds byte limit")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("acquisition exceeds byte limit")
            if length is not None and len(data) != int(length):
                raise ValueError("acquisition response was truncated")
            media = (
                response.getheader("Content-Type", "application/octet-stream")
                .split(";", 1)[0]
                .strip()
            )
            return data, {
                "source_uri": url,
                "requested_uri": initial,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "media_type": media,
                "hops": hops,
                "tool": "public-https",
                "version": "1",
                "license": "unknown",
            }
        finally:
            connection.close()
    raise ValueError("acquisition exceeded redirect limit")
