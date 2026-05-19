"""Standard-profile cross-section matcher.

Loads YAML tables from manufacturing_pipeline/data/profiles/ and matches a
canonicalised :class:`CrossSection` against the nearest standard entry.

The matcher is intentionally pure-Python: it only uses :mod:`yaml`, the
standard library and (optionally) :mod:`numpy`. See ROBUST_FEATURE_DETECTION_PLAN.md
section 6 for the design.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from pathlib import Path

import yaml

from ..config.classification_variables import (
    PROFILE_CONFIDENCE_DECAY_MM,
    PROFILE_RESIDUAL_REJECT_MM,
    PROFILE_RESIDUAL_TOLERANCE_MM,
)
from .cross_section import collapse_fillets
from .types import CrossSection, ProfileMatch

logger = logging.getLogger(__name__)


# Per-family dimension subspaces used for Euclidean distance scoring and for
# validation of catalogue entries.
_FAMILY_DIMS: dict[str, tuple[str, ...]] = {
    "I": ("h", "b", "t_w", "t_f"),
    "U": ("h", "b", "t_w", "t_f"),
    "T": ("h", "b", "t_w", "t_f"),
    "Z": ("h", "b", "t_w", "t_f"),
    "L": ("h", "b", "t"),
    "RHS": ("h", "b", "t"),
    "SHS": ("h", "b", "t"),
    "CHS": ("d", "t"),
}

_VALID_FAMILIES = frozenset(_FAMILY_DIMS.keys())


def _default_data_dir() -> Path:
    """Return the bundled data directory ``manufacturing_pipeline/data/profiles``."""
    return Path(__file__).resolve().parent.parent / "data" / "profiles"


class ProfileShapeMatcher:
    """Identify a structural profile family + designation from a cross-section.

    Public API:

    - :meth:`match`            - main entry point, never raises on bad input.
    - :meth:`list_designations` - enumeration helper for tests / debugging.
    - :meth:`fit_dimensions`   - geometry-only dimension fitting per family.
    """

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._data_dir = Path(data_dir) if data_dir is not None else _default_data_dir()
        # family -> list[entry_dict] (entry already validated; only entries with
        # all required dim fields make it in).
        self._tables: dict[str, list[dict]] = {fam: [] for fam in _VALID_FAMILIES}
        self._load_tables()

    # ------------------------------------------------------------------ loading

    def _load_tables(self) -> None:
        if not self._data_dir.is_dir():
            logger.warning("profile data dir not found: %s", self._data_dir)
            return
        for path in sorted(self._data_dir.glob("*.yaml")):
            if path.name in ("INDEX.yaml", "_schema.yaml"):
                continue
            try:
                with path.open("r", encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh)
            except (yaml.YAMLError, OSError) as exc:
                logger.warning("could not load %s: %s", path.name, exc)
                continue
            self._ingest_doc(doc, source=path.name)

    def _ingest_doc(self, doc: object, *, source: str) -> None:
        if not isinstance(doc, dict):
            logger.warning("%s: top-level is not a mapping", source)
            return
        entries = doc.get("profiles")
        if not isinstance(entries, list):
            logger.warning("%s: missing 'profiles' list", source)
            return
        file_standard = doc.get("standard", "") or ""
        file_family = doc.get("family", "") or ""
        for raw in entries:
            entry = self._validate_entry(raw, file_standard, file_family, source)
            if entry is None:
                continue
            self._tables[entry["family"]].append(entry)

    @staticmethod
    def _validate_entry(
        raw: object,
        file_standard: str,
        file_family: str,
        source: str,
    ) -> dict | None:
        if not isinstance(raw, dict):
            logger.warning("%s: skipping non-mapping entry", source)
            return None
        designation = raw.get("designation")
        if not isinstance(designation, str) or not designation:
            logger.warning("%s: entry without designation skipped", source)
            return None
        family = raw.get("family") or file_family
        if family not in _VALID_FAMILIES:
            logger.warning("%s: entry %r has unknown family %r", source, designation, family)
            return None
        required = _FAMILY_DIMS[family]
        dims: dict[str, float] = {}
        for key in required:
            val = raw.get(key)
            if not isinstance(val, (int, float)):
                logger.warning("%s: entry %r missing required dim %r", source, designation, key)
                return None
            dims[key] = float(val)
        return {
            "designation": designation,
            "family": family,
            "standard": raw.get("standard") or file_standard,
            "dims": dims,
        }

    # --------------------------------------------------------------- public API

    def list_designations(self, family: str | None = None) -> list[str]:
        """Return designations across all families, or limited to ``family``."""
        if family is None:
            out: list[str] = []
            for fam in sorted(self._tables):
                out.extend(e["designation"] for e in self._tables[fam])
            return out
        if family not in _VALID_FAMILIES:
            return []
        return [e["designation"] for e in self._tables[family]]

    def fit_dimensions(self, section: CrossSection, family: str) -> dict:
        """Fit family-specific nominal dimensions from the polyline geometry.

        Returns an empty dict if ``family`` is unknown or the section has no
        usable geometry. Tolerant of missing inputs.
        """
        if family not in _VALID_FAMILIES:
            return {}
        h, b = _bbox_hb(section)
        if h == 0.0 and b == 0.0:
            return {}
        sig = _section_signature(section)
        if family == "CHS":
            d = max(h, b) if max(h, b) > 0 else 0.0
            t = _hollow_wall_thickness(sig, d, d)
            return {"d": d, "t": t}
        if family in ("RHS", "SHS"):
            if family == "SHS":
                h = b = max(h, b)
            t = _hollow_wall_thickness(sig, h, b)
            return {"h": h, "b": b, "t": t}
        if family == "L":
            t = _solid_wall_thickness(section, fallback=0.10 * max(h, b, 1.0))
            return {"h": h, "b": b, "t": t}
        # I / U / T / Z all carry (h, b, t_w, t_f). The web orientation defines h.
        h_oriented, b_oriented, t_w, t_f = _wall_and_flange_thickness_oriented(section)
        if h_oriented == 0.0:
            h_oriented, b_oriented = h, b
        return {"h": h_oriented, "b": b_oriented, "t_w": t_w, "t_f": t_f}

    def match(
        self,
        section: CrossSection,
        *,
        family_hint: str | None = None,
    ) -> ProfileMatch:
        """Identify family + nearest catalogue entry. Never raises on bad input."""
        sig = _section_signature(section)
        if not sig and family_hint is None:
            return ProfileMatch(
                family="unknown",
                standard="",
                designation=None,
                fitted_dims={},
                residual_mm=float("inf"),
                confidence=0.0,
            )
        family = family_hint if family_hint in _VALID_FAMILIES else _identify_family(sig)
        if family is None:
            return ProfileMatch(
                family="unknown",
                standard="",
                designation=None,
                fitted_dims={},
                residual_mm=float("inf"),
                confidence=0.0,
            )

        fitted = self.fit_dimensions(section, family)
        candidates = self._tables.get(family, [])
        if not fitted or not candidates:
            return ProfileMatch(
                family=family,
                standard="",
                designation=None,
                fitted_dims=fitted,
                residual_mm=float("inf"),
                confidence=0.0,
            )

        best_entry, best_residual = _nearest_entry(fitted, candidates)
        confidence = _confidence(best_residual)
        designation = (
            best_entry["designation"] if best_residual <= PROFILE_RESIDUAL_REJECT_MM else None
        )
        return ProfileMatch(
            family=family,
            standard=best_entry["standard"],
            designation=designation,
            fitted_dims=fitted,
            residual_mm=best_residual,
            confidence=confidence,
        )


# ===================================================================== helpers


def _section_signature(section: CrossSection) -> dict:
    """Resolve the signature dict the matcher should consume.

    Prefers :attr:`CrossSection.signature` (populated by ``slice_solid``); falls
    back to a dict-form ``shape_hash`` so older callers that pass a signature
    via the legacy field still work.
    """
    sig = getattr(section, "signature", None)
    if isinstance(sig, dict) and sig:
        return sig
    legacy = getattr(section, "shape_hash", None)
    if isinstance(legacy, dict):
        return legacy
    return {}


def _identify_family(sig: dict) -> str | None:
    """Map a signature dict to a family using the heuristic from §6."""
    if not isinstance(sig, dict) or not sig:
        return None
    explicit = sig.get("family")
    if isinstance(explicit, str) and explicit in _VALID_FAMILIES:
        return explicit
    n_inner = int(sig.get("n_inner", 0) or 0)
    n_seg = int(sig.get("n_seg", sig.get("n_seg_outer", 0)) or 0)
    n_seg_outer = int(sig.get("n_seg_outer", n_seg) or 0)
    orth_ratio = float(sig.get("orth_ratio", 0.0) or 0.0)
    sym_x = bool(sig.get("sym_x", False))
    sym_y = bool(sig.get("sym_y", False))
    point_sym = bool(sig.get("point_sym", False))
    circular = bool(sig.get("outer_circular", False) and sig.get("inner_circular", False))

    if n_inner == 1 and circular:
        return "CHS"
    if n_inner == 1 and n_seg_outer == 4 and orth_ratio >= 0.95:
        h = float(sig.get("bbox_h", 0.0) or 0.0)
        b = float(sig.get("bbox_b", 0.0) or 0.0)
        if h > 0 and abs(h - b) / h < 0.02:
            return "SHS"
        return "RHS"
    if n_inner != 0:
        return None
    if n_seg == 12 and sym_x and sym_y:
        return "I"
    if n_seg == 8 and sym_x and not sym_y:
        return "U"
    if n_seg == 6 and not sym_x and not sym_y:
        return "L"
    if n_seg == 8 and sym_y and not sym_x:
        return "T"
    if n_seg == 8 and point_sym and not sym_x and not sym_y:
        return "Z"
    return None


def _bbox_hb(section: CrossSection) -> tuple[float, float]:
    """Return (h, b) from the polyline bbox; falls back to ``bbox_2d`` if needed.

    ``h`` is the larger dimension, ``b`` the smaller.
    """
    poly = section.polyline if isinstance(section.polyline, (list, tuple)) else []
    xs: list[float] = []
    ys: list[float] = []
    for pt in poly:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            try:
                xs.append(float(pt[0]))
                ys.append(float(pt[1]))
            except (TypeError, ValueError):
                continue
    if xs and ys:
        dx = max(xs) - min(xs)
        dy = max(ys) - min(ys)
        return (max(dx, dy), min(dx, dy))
    bb = section.bbox_2d if isinstance(section.bbox_2d, (list, tuple)) else None
    if bb and len(bb) >= 2:
        try:
            a = float(bb[0])
            c = float(bb[1])
            return (max(a, c), min(a, c))
        except (TypeError, ValueError):
            return (0.0, 0.0)
    return (0.0, 0.0)


def _hollow_wall_thickness(sig: dict, h: float, b: float) -> float:
    """Wall thickness for RHS/SHS/CHS via inner bbox if exposed, else fallback."""
    if isinstance(sig, dict):
        inner_bb = sig.get("inner_bbox")
        if isinstance(inner_bb, (list, tuple)) and len(inner_bb) >= 2:
            try:
                ih = float(inner_bb[0])
                ib = float(inner_bb[1])
                if ih > 0 and ib > 0:
                    return max((max(h, b) - max(ih, ib)) / 2.0, (min(h, b) - min(ih, ib)) / 2.0)
            except (TypeError, ValueError):
                pass
        t = sig.get("wall_thickness")
        if isinstance(t, (int, float)) and t > 0:
            return float(t)
    return 0.05 * max(h, b, 1.0)


def _solid_wall_thickness(section: CrossSection, *, fallback: float) -> float:
    """Estimate L-leg thickness as the shortest axis-aligned polyline segment."""
    h_segs, v_segs = _axis_aligned_segments(section)
    edge_lengths = [L for L, _ in h_segs] + [L for L, _ in v_segs]
    if not edge_lengths:
        return fallback
    return min(edge_lengths)


def _wall_and_flange_thickness_oriented(
    section: CrossSection,
) -> tuple[float, float, float, float]:
    """Orient an I/U/T/Z polyline so the web is vertical, then fit (h, b, t_w, t_f).

    Returns (h, b, t_w, t_f). Falls back to (0, 0, 0, 0) when the polyline has
    no axis-aligned segments at all.
    """
    poly = section.polyline if isinstance(section.polyline, (list, tuple)) else []
    if not poly:
        return 0.0, 0.0, 0.0, 0.0
    xs: list[float] = []
    ys: list[float] = []
    for pt in poly:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            try:
                xs.append(float(pt[0]))
                ys.append(float(pt[1]))
            except (TypeError, ValueError):
                continue
    if not xs:
        return 0.0, 0.0, 0.0, 0.0
    dx = max(xs) - min(xs)
    dy = max(ys) - min(ys)
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)

    h_segs, v_segs = _axis_aligned_segments(section)
    # The web is the two parallel long segments NOT at the bbox extremity.
    # If two such interior verticals exist, the web is vertical; otherwise horizontal.
    interior_v = [
        (L, c) for L, c in v_segs if min(abs(c - xmin), abs(c - xmax)) > max(dx * 0.05, 0.5)
    ]
    interior_h = [
        (L, c) for L, c in h_segs if min(abs(c - ymin), abs(c - ymax)) > max(dy * 0.05, 0.5)
    ]
    longest_interior_v = max((L for L, _ in interior_v), default=0.0)
    longest_interior_h = max((L for L, _ in interior_h), default=0.0)
    web_vertical = longest_interior_v >= longest_interior_h
    if web_vertical:
        height, breadth = dy, dx
        t_w, t_f = _wall_and_flange_thickness(section, height, breadth)
    else:
        # Pretend the polyline is rotated 90 degrees: swap horiz/vert roles.
        height, breadth = dx, dy
        rotated = _rotate_section_90(section)
        t_w, t_f = _wall_and_flange_thickness(rotated, height, breadth)
    return height, breadth, t_w, t_f


def _rotate_section_90(section: CrossSection) -> CrossSection:
    """Return a copy of ``section`` with its polyline rotated 90 degrees CCW."""
    poly = section.polyline if isinstance(section.polyline, (list, tuple)) else []
    rotated_poly = []
    for pt in poly:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            try:
                rotated_poly.append((-float(pt[1]), float(pt[0])))
            except (TypeError, ValueError):
                continue
    return CrossSection(
        position=section.position,
        area=section.area,
        perimeter=section.perimeter,
        n_outer_segments=section.n_outer_segments,
        n_inner_wires=section.n_inner_wires,
        bbox_2d=section.bbox_2d,
        polyline=rotated_poly,
        shape_hash=section.shape_hash,
    )


def _wall_and_flange_thickness(section: CrossSection, h: float, b: float) -> tuple[float, float]:
    """Estimate (t_w, t_f) for I / U / T / Z from axis-aligned polyline segments.

    Strategy: the bounding box gives (h, b). Vertical segments in the polyline
    fall into two groups - long ones (web sides, length ~ h - 2*t_f or h) and
    short ones (flange-side edges, length ~ t_f). Similarly horizontal segments
    split into long (flange tops/bottoms ~ b) and short (flange-inner offsets
    ~ (b - t_w)/2 for I, (b - t_w) for U). We infer:

        t_f = mean(short vertical segment lengths)
        t_w = b - 2 * mean(short horizontal segment lengths)   [I-section]
        t_w = b - mean(short horizontal segment lengths)        [U-section]

    For symmetric T-sections (n_seg=8) the same I-section formula applies.
    Falls back to conservative fractions of (h, b) if the polyline does not
    expose enough segments.
    """
    fallback_tw = max(0.04 * h, 0.5) if h > 0 else 1.0
    fallback_tf = max(0.06 * b, 0.5) if b > 0 else 1.0

    h_segs, v_segs = _axis_aligned_segments(section)
    if not h_segs or not v_segs:
        return fallback_tw, fallback_tf

    h_lengths = sorted(L for L, _ in h_segs)
    v_lengths = sorted(L for L, _ in v_segs)

    # Flange thickness: take vertical segments much shorter than the bbox height.
    short_v = [L for L in v_lengths if L < h * 0.45]
    if not short_v:
        return fallback_tw, fallback_tf
    t_f = _cluster_mean(short_v)

    # Web inference depends on whether the section has flanges on both flange
    # ends (I-like, count of horizontal short inner edges is 4) or only on one
    # back wall (U-like, count is 2).
    short_h = [L for L in h_lengths if L < b * 0.95]
    if not short_h:
        return fallback_tw, t_f

    short_h_cluster = _cluster_mean(short_h)
    n_short_h = len(short_h)
    # Distinguish I/T (4 inner-flange segments) from U (2 inner-flange segments).
    if n_short_h >= 4:
        t_w = b - 2.0 * short_h_cluster
    else:
        t_w = b - short_h_cluster
    if t_w <= 0:
        t_w = fallback_tw
    return t_w, t_f


def _cluster_mean(values: list[float], tol_ratio: float = 0.15) -> float:
    """Mean of the cluster containing the smallest value, within ``tol_ratio``.

    ``values`` must be sorted ascending and non-empty.
    """
    base = values[0]
    cluster = [v for v in values if abs(v - base) <= max(base * tol_ratio, 0.5)]
    return sum(cluster) / len(cluster)


def _matcher_polyline(section: CrossSection) -> list[tuple[float, float]]:
    """Return the section polyline with fillet arcs collapsed to sharp corners.

    The fitter inspects axis-aligned segments; a discretised fillet arc
    contributes many short non-axis-aligned edges that confuse the
    short-segment clustering. Collapsing them first restores the canonical
    profile geometry.
    """
    poly = section.polyline if isinstance(section.polyline, (list, tuple)) else []
    if not poly:
        return []
    return collapse_fillets(list(poly))


def _axis_aligned_segments(
    section: CrossSection,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Return (horizontal_segments, vertical_segments) as (length, coord) tuples.

    Horizontal segments carry their y-coordinate (constant along the edge);
    vertical segments carry their x-coordinate.
    """
    poly = _matcher_polyline(section)
    if not poly or len(poly) < 2:
        return [], []
    horiz: list[tuple[float, float]] = []
    vert: list[tuple[float, float]] = []
    n = len(poly)
    for i in range(n):
        a = poly[i]
        b = poly[(i + 1) % n]
        if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))):
            continue
        try:
            ax = float(a[0])
            ay = float(a[1])
            bx = float(b[0])
            by = float(b[1])
        except (TypeError, ValueError, IndexError):
            continue
        dx = bx - ax
        dy = by - ay
        L = math.hypot(dx, dy)
        if L <= 0:
            continue
        cos_x = abs(dx) / L
        cos_y = abs(dy) / L
        if cos_x >= 0.99:
            horiz.append((L, (ay + by) / 2.0))
        elif cos_y >= 0.99:
            vert.append((L, (ax + bx) / 2.0))
    return horiz, vert


def _nearest_entry(fitted: dict, candidates: Iterable[dict]) -> tuple[dict, float]:
    """Return (best_entry, residual_mm) by Euclidean distance in shared dims."""
    best_entry: dict | None = None
    best_res = float("inf")
    for entry in candidates:
        dims = entry["dims"]
        sq = 0.0
        used = 0
        for key, ref in dims.items():
            if key in fitted:
                sq += (fitted[key] - ref) ** 2
                used += 1
        if used == 0:
            continue
        residual = math.sqrt(sq)
        if residual < best_res:
            best_res = residual
            best_entry = entry
    assert best_entry is not None, "candidates list must be non-empty"
    return best_entry, best_res


def _confidence(residual_mm: float) -> float:
    """``exp(-residual / PROFILE_CONFIDENCE_DECAY_MM)`` clamped to [0, 1]."""
    if not math.isfinite(residual_mm):
        return 0.0
    return max(0.0, min(1.0, math.exp(-residual_mm / PROFILE_CONFIDENCE_DECAY_MM)))


__all__ = [
    "ProfileShapeMatcher",
    "PROFILE_RESIDUAL_TOLERANCE_MM",
    "PROFILE_RESIDUAL_REJECT_MM",
]
