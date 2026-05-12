"""Ingest pipeline: gather → slop classify → fan-out → embed → upsert."""

from __future__ import annotations

from slopmortem.ingest._fan_out import _facet_summarize_fanout as facet_summarize_fanout
from slopmortem.ingest._impls import (
    FakeSlopClassifier as FakeSlopClassifier,
)
from slopmortem.ingest._impls import (
    HaikuSlopClassifier as HaikuSlopClassifier,
)
from slopmortem.ingest._impls import (
    InMemoryCorpus as InMemoryCorpus,
)
from slopmortem.ingest._ingest import _classify_phase as classify_phase
from slopmortem.ingest._ingest import _write_phase as write_phase
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
    IngestProgress as IngestProgress,
)
from slopmortem.ingest._ports import (
    IngestResult as IngestResult,
)
from slopmortem.ingest._ports import (
    NullProgress as NullProgress,
)
from slopmortem.ingest._ports import (
    SlopClassifier as SlopClassifier,
)
from slopmortem.ingest._ports import (
    SparseEncoder as SparseEncoder,
)
from slopmortem.ingest._ports import (
    _Point as _Point,
)
from slopmortem.ingest._title_pre_filter import HaikuTitlePreFilter as HaikuTitlePreFilter

__all__ = [
    "INGEST_PHASE_LABELS",
    "Corpus",
    "FakeSlopClassifier",
    "HaikuPitchFiller",
    "HaikuSlopClassifier",
    "HaikuTitlePreFilter",
    "InMemoryCorpus",
    "IngestPhase",
    "IngestProgress",
    "IngestResult",
    "NullProgress",
    "SlopClassifier",
    "SparseEncoder",
    "_Point",
    "classify_phase",
    "facet_summarize_fanout",
    "ingest",
    "write_phase",
]
