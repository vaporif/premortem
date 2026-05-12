"""SSRF-aware HTTP fetch. Refuses loopback, link-local, private, and IMDS targets."""

from __future__ import annotations

import ipaddress
import socket
from typing import Final
from urllib.parse import urlparse

import httpx

USER_AGENT: Final[str] = "slopmortem/0.1.0 (+https://github.com/vaporif/slopmortem)"

_BLOCKED_IMDS_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata",
        "instance-data",
        "metadata.azure.com",
    }
)


class SSRFBlockedError(RuntimeError):
    """``safe_get`` / ``safe_post`` refused a URL on SSRF-policy grounds."""


def _is_blocked_address(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        return ip in ipaddress.ip_network("100.64.0.0/10") or ip in ipaddress.ip_network(
            "169.254.0.0/16"
        )
    # ip_address() returns IPv4Address | IPv6Address, so this branch is exhaustive.
    return ip in ipaddress.ip_network("fc00::/7") or ip in ipaddress.ip_network("fe80::/10")


def _resolve_all(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        msg = f"cannot resolve host {host!r}: {e}"
        raise SSRFBlockedError(msg) from e
    # getaddrinfo returns sockaddr tuples whose [0] is a host string. The cast
    # keeps mypy from widening the set element type to ``str | int``.
    return list({str(info[4][0]) for info in infos})


def _resolve_and_validate(url: str) -> str:
    # One enforcement path for both safe_get and safe_post; returns the parsed
    # host for the ``Host`` header.
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        msg = f"refusing non-http(s) scheme: {parsed.scheme!r}"
        raise SSRFBlockedError(msg)
    host = parsed.hostname
    if not host:
        msg = f"missing host in {url!r}"
        raise SSRFBlockedError(msg)
    if host in _BLOCKED_IMDS_HOSTS:
        msg = f"refusing IMDS host {host!r}"
        raise SSRFBlockedError(msg)
    addrs = _resolve_all(host)
    if not addrs:
        msg = f"no addresses resolved for {host!r}"
        raise SSRFBlockedError(msg)
    for a in addrs:
        if _is_blocked_address(a):
            msg = f"refusing blocked address {a} for host {host!r}"
            raise SSRFBlockedError(msg)
    return host


async def safe_get(
    url: str,
    *,
    user_agent: str = USER_AGENT,
    timeout: float = 30.0,  # noqa: ASYNC109 - caller-controlled timeout is part of the public API
) -> httpx.Response:
    host = _resolve_and_validate(url)
    transport = httpx.AsyncHTTPTransport()
    async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
        return await client.get(url, headers={"Host": host, "User-Agent": user_agent})


async def safe_post(
    url: str,
    *,
    json: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    user_agent: str = USER_AGENT,
    timeout: float = 30.0,  # noqa: ASYNC109 - caller-controlled timeout is part of the public API
) -> httpx.Response:
    host = _resolve_and_validate(url)
    merged_headers: dict[str, str] = {"Host": host, "User-Agent": user_agent}
    if headers:
        merged_headers.update(headers)
    transport = httpx.AsyncHTTPTransport()
    async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
        return await client.post(url, json=json, headers=merged_headers)


async def safe_head(
    url: str,
    *,
    user_agent: str = USER_AGENT,
    timeout: float = 30.0,  # noqa: ASYNC109 - caller-controlled timeout is part of the public API
) -> httpx.Response:
    """SSRF-pinned HEAD for cheap liveness probes.

    Verifier L2 uses this to drop dead URLs before paying for the body GET.
    """
    host = _resolve_and_validate(url)
    transport = httpx.AsyncHTTPTransport()
    async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
        return await client.head(url, headers={"Host": host, "User-Agent": user_agent})
