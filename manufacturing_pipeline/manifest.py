"""Assembly manifest types — the boundary between `analyze()` and every writer.

`analyze()` produces an `AssemblyManifest`; downstream consumers (DXF, XML, PDF,
web viewer, corpus diff, validate) read it back. The XML format defined in
:mod:`manufacturing_pipeline.io.xml_writer` is one of several serializers, not
the source of truth — hence these types live here, outside `io/`.

See ADR 0005 (output writers) and ADR 0006 (explicit assembly graph).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from manufacturing_pipeline.cam.types import MachiningStrategy
from manufacturing_pipeline.classification.types import ClassificationResult
from manufacturing_pipeline.geometry.types import (
    HolePattern,
    ManufacturingFeatures,
    ProfileMatch,
    UnfoldResult,
)
from manufacturing_pipeline.parsing.types import StepPart
from manufacturing_pipeline.pmi.types import PMIRecord


class ManifestParseError(ValueError):
    """Raised when a serialized manifest cannot be parsed."""


@dataclass
class PartManifestEntry:
    part: StepPart
    classification: ClassificationResult
    features: ManufacturingFeatures | None = None
    profile_match: ProfileMatch | None = None
    unfold: UnfoldResult | None = None
    holes: HolePattern | None = None
    quantity: int = 1
    flat_dxf_path: str | None = None
    pmi: PMIRecord | None = None
    strategy: MachiningStrategy | None = None


@dataclass
class AssemblyManifest:
    source_path: str
    source_mtime: float
    source_size: int
    model_version: str
    generated_at: str
    parts: list[PartManifestEntry] = field(default_factory=list)
    notes: str = ""
