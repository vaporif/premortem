"""Corpus protocol: read-side interface for querying stored docs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from slopmortem.models import Candidate, Facets


@runtime_checkable
class Corpus(Protocol):
    """Read-side protocol over the persisted candidate corpus (real Qdrant or fake)."""

    async def query(  # noqa: PLR0913 — Protocol method signature is the public contract
        self,
        *,
        dense: list[float],
        sparse: dict[int, float],
        facets: Facets,
        cutoff_iso: str | None,
        strict_deaths: bool,
        k_retrieve: int,
        strict_sector_filter: bool = False,
        strict_sector_filter_excludes_other: bool = False,
    ) -> list[Candidate]: ...

    async def get_post_mortem(self, canonical_id: str) -> str: ...

    async def search_corpus(
        self, q: str, facets: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:  # pyright: ignore[reportExplicitAny]  # Protocol — implementations vary
        ...
