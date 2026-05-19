"""Product Manufacturing Information (PMI) extraction.

Pure-Python probe that walks the tokenised STEP entities and surfaces
semantic GD&T / tolerance / datum / surface-finish annotations from AP242
files. AP203 geometry-only files and AP242 ``-tg`` (tessellated graphical)
variants produce an empty :class:`PMIRecord` with ``has_semantic=False``.

The extractor never raises - malformed entities are skipped and counted
in :attr:`PMIRecord.n_annotations`.
"""

from __future__ import annotations

from .extractor import extract_pmi
from .types import (
    Datum,
    DimensionalTolerance,
    GeometricTolerance,
    PMIRecord,
    SurfaceFinish,
)

__all__ = [
    "Datum",
    "DimensionalTolerance",
    "GeometricTolerance",
    "PMIRecord",
    "SurfaceFinish",
    "extract_pmi",
]
