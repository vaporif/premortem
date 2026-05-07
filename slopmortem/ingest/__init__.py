"""Ingest pipeline: gather → slop classify → fan-out → embed → upsert."""

from __future__ import annotations

from slopmortem.ingest._impls import (
    FakeSlopClassifier as FakeSlopClassifier,
)
from slopmortem.ingest._impls import (
    HaikuSlopClassifier as HaikuSlopClassifier,
)
from slopmortem.ingest._impls import (
    InMemoryCorpus as InMemoryCorpus,
)
from slopmortem.ingest._ingest import ingest as ingest
from slopmortem.ingest._pitch_filler import HaikuPitchFiller as HaikuPitchFiller
from slopmortem.ingest._ports import (
    INGEST_PHASE_LABELS as INGEST_PHASE_LABELS,
)
from slopmortem.ingest._ports import (
    Corpus as Corpus,
)
from slopmortem.ingest._ports import (
    IngestPhase as IngestPhase,
)
from slopmortem.ingest._ports import (
    IngestResult as IngestResult,
)
from slopmortem.ingest._ports import (
    SlopClassifier as SlopClassifier,
)
from slopmortem.ingest._ports import (
    _Point as _Point,
)

__all__ = [
    "INGEST_PHASE_LABELS",
    "Corpus",
    "FakeSlopClassifier",
    "HaikuPitchFiller",
    "HaikuSlopClassifier",
    "InMemoryCorpus",
    "IngestPhase",
    "IngestResult",
    "SlopClassifier",
    "_Point",
    "ingest",
]
