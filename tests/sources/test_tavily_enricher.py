"""TavilyEnricher recovers article bodies via Tavily's /extract API."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx

from slopmortem.corpus.sources import TavilyEnricher
from slopmortem.models import RawEntry


def _entry(
    *,
    raw_html: str | None = None,
    markdown_text: str | None = None,
    url: str | None = "https://example.com/x",
) -> RawEntry:
    return RawEntry(
        source="hn_algolia",
        source_id="abc123",
        url=url,
        raw_html=raw_html,
        markdown_text=markdown_text,
        fetched_at=datetime.now(UTC),
    )


async def test_skips_when_raw_html_already_populated(monkeypatch):
    """If raw_html is non-empty, the enricher returns the entry unchanged."""
    mock_post = AsyncMock()
    monkeypatch.setattr("slopmortem.corpus.sources.tavily.safe_post", mock_post)

    entry = _entry(raw_html="<html>already there</html>")
    result = await TavilyEnricher(api_key="tv-test-key").enrich(entry)
    assert result is entry
    mock_post.assert_not_called()


async def test_skips_when_markdown_text_already_populated(monkeypatch):
    """tavily_news yields markdown_text with raw_html=None — don't re-fetch via /extract."""
    mock_post = AsyncMock()
    monkeypatch.setattr("slopmortem.corpus.sources.tavily.safe_post", mock_post)

    entry = _entry(markdown_text="already extracted body text")
    result = await TavilyEnricher(api_key="tv-test-key").enrich(entry)
    assert result is entry
    mock_post.assert_not_called()


async def test_skips_when_url_missing(monkeypatch):
    """If url is None, the enricher returns the entry unchanged."""
    mock_post = AsyncMock()
    monkeypatch.setattr("slopmortem.corpus.sources.tavily.safe_post", mock_post)

    entry = _entry(url=None, raw_html=None)
    result = await TavilyEnricher(api_key="tv-test-key").enrich(entry)
    assert result is entry
    mock_post.assert_not_called()


async def test_populates_raw_html_and_markdown_on_success(monkeypatch):
    """On a 200 with ``results[0].raw_content``, both ``raw_html`` and ``markdown_text`` fill."""
    # Body needs to clear ``extract.LENGTH_FLOOR`` (500 chars) for trafilatura
    # to return a non-empty extraction.
    body_paragraph = (
        "Recovered article body discussing the post-mortem in great detail. "
        "The team reflected on the failure modes that emerged during the outage "
        "and documented every contributing factor that the on-call engineers "
        "encountered while trying to mitigate the user-visible impact."
    )
    raw_content = (
        "<html><head><title>Recovered Post-mortem</title></head><body>"
        f"<article><h1>Recovered article body</h1><p>{body_paragraph}</p>"
        f"<p>{body_paragraph}</p></article></body></html>"
    )
    fake_resp = httpx.Response(
        200,
        json={
            "results": [
                {
                    "url": "https://example.com/x",
                    "raw_content": raw_content,
                }
            ]
        },
    )
    mock_post = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr("slopmortem.corpus.sources.tavily.safe_post", mock_post)

    entry = _entry(raw_html=None)
    result = await TavilyEnricher(api_key="tv-test-key").enrich(entry)
    assert result is not entry  # immutable update
    assert result.raw_html == raw_content
    assert result.markdown_text  # extract_clean filled this


async def test_returns_entry_unchanged_on_http_error(monkeypatch):
    """A non-200 from Tavily is logged and the entry passes through unchanged."""
    fake_resp = httpx.Response(429, json={"detail": "rate limited"})
    monkeypatch.setattr(
        "slopmortem.corpus.sources.tavily.safe_post", AsyncMock(return_value=fake_resp)
    )

    entry = _entry(raw_html=None)
    result = await TavilyEnricher(api_key="tv-test-key").enrich(entry)
    assert result is entry
    assert result.raw_html is None
