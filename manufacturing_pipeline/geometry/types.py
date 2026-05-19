"""Public types for the geometry layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass
class ManufacturingFeatures:
    volume: float = 0.0
    surface_area: float = 0.0
    bbox_dims_sorted: tuple[float, float, float] = (0.0, 0.0, 0.0)
    aspect_ratio: float = 0.0
    thickness_ratio: float = 0.0
    face_area_top: list[float] = field(default_factory=list)  # top1, top2, top3 pct
    surface_pct: dict = field(default_factory=dict)
    edge_counts: dict = field(default_factory=dict)
    edge_radius: tuple[float, float] = (0.0, 0.0)
    hole_count: int = 0
    hole_diameters: list[float] = field(default_factory=list)
    sa_v_ratio: float = 0.0
    bounding_cylinder_fit_pct: float = 0.0
    convex_hull_volume_ratio: float = 0.0
    cross_section_constant: bool = False
    cross_section_signature: dict = field(default_factory=dict)
    is_hollow: bool = False
    inner_shell_count: int = 0
    pocket_complexity: float = 0.0   # 0..1 score: high = lots of small planar facets (pocketed)
    source: str = "brep"  # "brep" | "mesh"
    material_guess: str = ""


@dataclass
class CrossSection:
    position: float
    area: float
    perimeter: float
    n_outer_segments: int
    n_inner_wires: int
    bbox_2d: tuple[float, float]
    polyline: list
    shape_hash: tuple
    signature: dict = field(default_factory=dict)
    inner_polylines: list[list] = field(default_factory=list)


@dataclass
class ProfileMatch:
    family: str  # "I" | "U" | "L" | "T" | "Z" | "RHS" | "SHS" | "CHS"
    standard: str
    designation: str | None
    fitted_dims: dict
    residual_mm: float
    confidence: float


class UnfoldStatus(Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"


@dataclass
class UnfoldResult:
    status: UnfoldStatus
    n_bends: int = 0
    flat_area: float = 0.0
    blank_bbox: tuple | None = None
    thickness_mean: float = 0.0
    thickness_cv: float = 0.0
    flags: dict = field(default_factory=dict)
    reason: str = ""


@dataclass
class HolePattern:
    hole_count: int = 0
    diameters: list[float] = field(default_factory=list)
    holes: list = field(default_factory=list)  # detailed (diameter, depth, axis, position)
