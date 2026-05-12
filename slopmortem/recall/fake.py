"""Fake recall callable for tests that exercise the pipeline seam without driving L0-L5.

Returns a pre-baked list of ``VerifiedEntry`` records and records the
invocation for assertion. Pipeline tests checking dedup / floor / re-retrieve
inject one of these instead of wiring four fakes (LLM + Tavily search +
extract + Wayback) end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slopmortem.models import Facets
    from slopmortem.recall._brainstorm import PriorCandidateHint
    from slopmortem.recall._models import RecallConfig, RecallDeps
    from slopmortem.recall._verify import VerifiedEntry


@dataclass
class _RecallCall:
    pitch: str
    facets: Facets | None
    prior_hints: list[PriorCandidateHint] | None


@dataclass
class FakeRecaller:
    """Callable shaped like ``recall(...)``. Returns ``verified`` and records calls."""

    verified: list[VerifiedEntry] = field(default_factory=list)
    calls: list[_RecallCall] = field(default_factory=list)

    async def __call__(
        self,
        pitch: str,
        *,
        facets: Facets | None = None,
        prior_hints: list[PriorCandidateHint] | None = None,
        deps: RecallDeps,  # noqa: ARG002 - present for signature parity
        config: RecallConfig,  # noqa: ARG002 - present for signature parity
    ) -> list[VerifiedEntry]:
        self.calls.append(_RecallCall(pitch=pitch, facets=facets, prior_hints=prior_hints))
        return list(self.verified)
