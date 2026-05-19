"""Public types for the PMI layer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GeometricTolerance:
    """A GD&T callout: type (e.g. 'position', 'flatness'), magnitude_mm,
    datums_referenced, applied_to (entity ref or 'part')."""

    type: str
    magnitude_mm: float
    unit: str = "mm"
    datums: list[str] = field(default_factory=list)
    applied_to: str = ""
    modifier: str = ""  # MMC | LMC | RFS | None


@dataclass
class DimensionalTolerance:
    """A linear / angular tolerance: nominal +X / -Y."""

    nominal: float
    upper: float
    lower: float
    unit: str = "mm"
    applied_to: str = ""


@dataclass
class Datum:
    """A datum feature: identifier (A/B/C), type."""

    identifier: str
    applied_to: str = ""


@dataclass
class SurfaceFinish:
    """A surface finish callout: Ra value in um or unit."""

    value: float
    unit: str = "um"
    applied_to: str = ""


@dataclass
class PMIRecord:
    """Bag of PMI features for one part."""

    tolerances: list[GeometricTolerance] = field(default_factory=list)
    dimensions: list[DimensionalTolerance] = field(default_factory=list)
    datums: list[Datum] = field(default_factory=list)
    finishes: list[SurfaceFinish] = field(default_factory=list)
    n_annotations: int = 0  # total raw PMI entities counted
    has_semantic: bool = False
