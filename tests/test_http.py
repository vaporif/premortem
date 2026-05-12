"""Regression tests for ``safe_head``: SSRF-pinned HEAD requests for cheap liveness probes."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from slopmortem.http import SSRFBlockedError, safe_head


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:6333/",
        "http://10.0.0.1/admin",
        "http://metadata.google.internal/",
        "file:///etc/passwd",
    ],
)
async def test_safe_head_blocks_ssrf(url: str) -> None:
    """``safe_head`` refuses the same hosts/schemes as ``safe_get`` / ``safe_post``."""
    with pytest.raises(SSRFBlockedError):
        await safe_head(url)


async def test_safe_head_returns_status_for_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 from the upstream HEAD bubbles up untouched."""
    captured: dict[str, object] = {}
    fake_response = httpx.Response(200)

    async def fake_head(
        self: object,
        url: str,
        **kwargs: object,
    ) -> httpx.Response:
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return fake_response

    def _stub(_host: str) -> list[str]:
        return ["1.2.3.4"]

    monkeypatch.setattr("slopmortem.http._resolve_all", _stub)
    monkeypatch.setattr(httpx.AsyncClient, "head", fake_head)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", MagicMock)

    resp = await safe_head("https://api.example.com/healthz")

    assert resp.status_code == 200
    assert captured["url"] == "https://api.example.com/healthz"
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["Host"] == "api.example.com"


async def test_safe_head_returns_404_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """4xx is reported via ``status_code`` — ``safe_head`` itself does not raise."""
    fake_response = httpx.Response(404)

    async def fake_head(
        self: object,
        url: str,
        **kwargs: object,
    ) -> httpx.Response:
        del self, url, kwargs
        return fake_response

    def _stub(_host: str) -> list[str]:
        return ["1.2.3.4"]

    monkeypatch.setattr("slopmortem.http._resolve_all", _stub)
    monkeypatch.setattr(httpx.AsyncClient, "head", fake_head)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", MagicMock)

    resp = await safe_head("https://example.com/missing")
    assert resp.status_code == 404


async def test_safe_head_rejects_non_http_scheme() -> None:
    """Non-http schemes raise SSRFBlockedError mentioning the scheme."""
    with pytest.raises(SSRFBlockedError, match="non-http"):
        await safe_head("file:///etc/passwd")


async def test_safe_head_rejects_loopback_via_dns_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostname that resolves to 127.0.0.1 is rejected even with an https URL."""

    def _stub(_host: str) -> list[str]:
        return ["127.0.0.1"]

    monkeypatch.setattr("slopmortem.http._resolve_all", _stub)
    with pytest.raises(SSRFBlockedError, match="blocked address"):
        await safe_head("https://evil.example.com/path")
