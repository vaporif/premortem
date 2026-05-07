"""Source adapters and enrichers that produce ``RawEntry`` for ingest."""

from __future__ import annotations

from slopmortem.corpus.sources.base import Enricher as Enricher
from slopmortem.corpus.sources.base import Source as Source
from slopmortem.corpus.sources.crunchbase_csv import CrunchbaseCsvSource as CrunchbaseCsvSource
from slopmortem.corpus.sources.curated import CuratedSource as CuratedSource
from slopmortem.corpus.sources.hn_algolia import HNAlgoliaSource as HNAlgoliaSource
from slopmortem.corpus.sources.tavily import TavilyEnricher as TavilyEnricher
from slopmortem.corpus.sources.tavily_news import TavilyNewsSource as TavilyNewsSource
from slopmortem.corpus.sources.wayback import WaybackEnricher as WaybackEnricher

__all__ = [
    "CrunchbaseCsvSource",
    "CuratedSource",
    "Enricher",
    "HNAlgoliaSource",
    "Source",
    "TavilyEnricher",
    "TavilyNewsSource",
    "WaybackEnricher",
]
