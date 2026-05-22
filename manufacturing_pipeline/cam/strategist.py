"""Heuristic CAM strategy recommender.

The probe consumes the analyzer outputs and produces a
:class:`MachiningStrategy`. Routing is keyed on the classifier label, then
refined with hole-pattern and PMI signals. The probe MUST NOT raise -
unexpected input degrades to ``primary_process="unknown"`` with a single
``inspect`` operation.

Time estimates are intentionally crude; downstream consumers are expected
to treat them as planning hints, not quotes.
"""

from __future__ import annotations

import logging
import math

from ..classification.types import ClassificationResult
from ..geometry.types import HolePattern, ManufacturingFeatures, ProfileMatch, UnfoldResult
from ..pmi.types import PMIRecord
from .types import MachiningStrategy, Operation

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (per-operation rough time costs, in minutes)
# ---------------------------------------------------------------------------

TIME_LASER_CUT_PER_M = 0.5
TIME_DRILL_PER_HOLE = 0.3
TIME_REAM_PER_HOLE = 0.5
TIME_MILL_POCKET_PER_POCKET = 5.0
TIME_PRESS_BRAKE_PER_BEND = 0.5
TIME_FINISH_PASS_FLAT = 5.0
TIME_INSPECT_PER_DATUM = 2.0

# PMI thresholds that flip optional operations on.
TIGHT_POSITION_TOL_MM = 0.05
FINE_SURFACE_FINISH_RA = 1.6
MIRROR_SURFACE_FINISH_RA = 0.8

# A convex-hull volume ratio well below 1.0 means the part has internal
# pockets / cutouts. The threshold is the same the classifier already
# uses to label "anders" (see classification/scorers.py).
HULL_CONCAVITY_POCKET_THRESHOLD = 0.4

# Reasonable upper bound on a single laser cut path length (m). If the
# perimeter cannot be inferred we fall back to this.
LASER_CUT_FALLBACK_M = 2.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _holes_iter(holes: HolePattern) -> list[dict]:
    """Yield per-hole dicts. Falls back to diameter-only entries when the
    analyzer only populated the flat ``diameters`` list.
    """
    out: list[dict] = []
    if getattr(holes, "holes", None):
        for h in holes.holes:
            if isinstance(h, dict):
                out.append(dict(h))
    if out:
        return out
    for d in holes.diameters or []:
        out.append({"diameter": float(d), "depth": 0.0, "through": False})
    return out


def _estimate_perimeter_m(features: ManufacturingFeatures, holes: HolePattern) -> float:
    """Approximate the total laser-cut path length in metres.

    Outer rectangle perimeter (from the bbox) + sum of hole circumferences.
    Conservative: real outer contours are usually a little longer than the
    bbox perimeter, but the resulting time error sits inside the
    heuristic's noise floor.
    """
    bb = features.bbox_dims_sorted or (0.0, 0.0, 0.0)
    length_mm, width_mm, _ = sorted(bb, reverse=True)
    outer_mm = 2.0 * (length_mm + width_mm)
    inner_mm = sum(math.pi * float(d) for d in (holes.diameters or []))
    total_mm = outer_mm + inner_mm
    if total_mm <= 0.0:
        return LASER_CUT_FALLBACK_M
    return total_mm / 1000.0


def _hull_concavity(features: ManufacturingFeatures) -> float:
    """``1 - convex_hull_volume_ratio`` clamped to ``[0, 1]``. Matches the
    derivation used by the classifier so the pocket threshold is consistent
    with the routing it implies.
    """
    ratio = float(getattr(features, "convex_hull_volume_ratio", 0.0) or 0.0)
    return max(0.0, min(1.0, 1.0 - ratio))


def _tight_position_holes(pmi: PMIRecord | None, hole_refs: list[str]) -> dict[str, float]:
    """Map ``hole_ref`` -> tightest position-tolerance magnitude that
    applies to it (or to "part" globally) and is below
    :data:`TIGHT_POSITION_TOL_MM`.
    """
    if pmi is None or not pmi.tolerances:
        return {}
    matches: dict[str, float] = {}
    for tol in pmi.tolerances:
        if (tol.type or "").lower() != "position":
            continue
        if tol.magnitude_mm >= TIGHT_POSITION_TOL_MM:
            continue
        target = (tol.applied_to or "").strip()
        if target and target in hole_refs:
            matches[target] = min(matches.get(target, math.inf), tol.magnitude_mm)
        elif not target or target == "part":
            for ref in hole_refs:
                matches[ref] = min(matches.get(ref, math.inf), tol.magnitude_mm)
    return matches


def _thread_callout_holes(pmi: PMIRecord | None, hole_refs: list[str]) -> set[str]:
    """Hole refs whose dimension or geometric-tolerance ``applied_to`` mentions
    a thread (``M``-style or ``UNC``/``UNF`` callout). Best-effort string match.
    """
    if pmi is None:
        return set()
    hits: set[str] = set()
    fragments = ("M3", "M4", "M5", "M6", "M8", "M10", "M12", "UNC", "UNF", "thread")
    for d in pmi.dimensions:
        target = (d.applied_to or "").strip()
        if any(frag.lower() in target.lower() for frag in fragments):
            if target in hole_refs:
                hits.add(target)
    for t in pmi.tolerances:
        target = (t.applied_to or "").strip()
        if any(frag.lower() in target.lower() for frag in fragments):
            if target in hole_refs:
                hits.add(target)
    return hits


def _has_fine_surface(pmi: PMIRecord | None, threshold: float) -> bool:
    if pmi is None or not pmi.finishes:
        return False
    return any(float(f.value) < threshold for f in pmi.finishes)


def _datum_list(pmi: PMIRecord | None) -> list[str]:
    if pmi is None or not pmi.datums:
        return []
    seen: list[str] = []
    for d in pmi.datums:
        ident = (d.identifier or "").strip()
        if ident and ident not in seen:
            seen.append(ident)
    return seen


# ---------------------------------------------------------------------------
# Per-routing builders
# ---------------------------------------------------------------------------


def _plan_sheet_metal(
    features: ManufacturingFeatures,
    holes: HolePattern,
    unfold: UnfoldResult | None,
) -> MachiningStrategy:
    perim_m = _estimate_perimeter_m(features, holes)
    n_holes = int(holes.hole_count or len(holes.diameters or []))
    n_bends = int(unfold.n_bends) if unfold is not None else 0
    thickness = float(unfold.thickness_mean) if unfold is not None else 0.0

    ops: list[Operation] = []
    ops.append(
        Operation(
            op="laser_cut",
            feature_ref="outer_contour",
            params={
                "path_length_m": round(perim_m, 3),
                "hole_count": n_holes,
                "thickness_mm": round(thickness, 3),
            },
            priority=100,
            reason="flat blank cut with hole pattern",
        )
    )
    for i in range(n_bends):
        ops.append(
            Operation(
                op="press_brake_bend",
                feature_ref=f"bend_{i}",
                params={"index": i},
                priority=120,
                reason="press-brake bend from unfold",
            )
        )
    ops.append(
        Operation(
            op="deburr",
            feature_ref="part",
            params={},
            priority=200,
            reason="post-cut edge finish",
        )
    )

    time_min = (
        perim_m * TIME_LASER_CUT_PER_M
        + n_bends * TIME_PRESS_BRAKE_PER_BEND
        + 1.0  # deburr flat fee
    )
    return MachiningStrategy(
        primary_process="sheet_metal",
        operations=ops,
        setup_count=1,
        estimated_time_min=round(time_min, 2),
    )


def _plan_purchased() -> MachiningStrategy:
    return MachiningStrategy(
        primary_process="purchased",
        operations=[
            Operation(
                op="inspect",
                feature_ref="part",
                params={},
                priority=200,
                reason="purchased profile - inspect only",
            )
        ],
        setup_count=0,
        estimated_time_min=round(TIME_INSPECT_PER_DATUM, 2),
    )


def _plan_machining(
    features: ManufacturingFeatures,
    pmi: PMIRecord | None,
    holes: HolePattern,
) -> MachiningStrategy:
    hole_dicts = _holes_iter(holes)
    hole_refs = [f"hole_{i}" for i in range(len(hole_dicts))]

    ops: list[Operation] = []
    ops.append(
        Operation(
            op="mill_contour",
            feature_ref="outer_contour",
            params={
                "length_mm": round(float(features.bbox_dims_sorted[0]), 3)
                if features.bbox_dims_sorted
                else 0.0,
                "width_mm": round(float(features.bbox_dims_sorted[1]), 3)
                if features.bbox_dims_sorted
                else 0.0,
            },
            priority=100,
            reason="rough outer contour",
        )
    )

    for i, h in enumerate(hole_dicts):
        diameter = float(h.get("diameter") or 0.0)
        depth = float(h.get("depth") or 0.0)
        ops.append(
            Operation(
                op="drill",
                feature_ref=hole_refs[i],
                params={
                    "diameter_mm": round(diameter, 3),
                    "depth_mm": round(depth, 3),
                    "through": bool(h.get("through", False)),
                },
                priority=110,
                reason="cylindrical hole",
            )
        )

    tight = _tight_position_holes(pmi, hole_refs)
    for ref, mag in tight.items():
        # Find diameter for the matching hole index.
        try:
            idx = hole_refs.index(ref)
            diameter = float(hole_dicts[idx].get("diameter") or 0.0)
        except (ValueError, IndexError):
            diameter = 0.0
        ops.append(
            Operation(
                op="ream",
                feature_ref=ref,
                params={
                    "diameter_mm": round(diameter, 3),
                    "position_tol_mm": round(mag, 4),
                },
                priority=140,
                reason=f"position tolerance {mag:.3f} mm requires reamed bore",
            )
        )
        ops.append(
            Operation(
                op="inspect",
                feature_ref=ref,
                params={"position_tol_mm": round(mag, 4)},
                priority=210,
                reason="verify tight position tolerance",
            )
        )

    pocket_detected = _hull_concavity(features) > HULL_CONCAVITY_POCKET_THRESHOLD
    n_pockets = 1 if pocket_detected else 0
    if pocket_detected:
        ops.append(
            Operation(
                op="mill_pocket",
                feature_ref="pocket_0",
                params={"hull_concavity": round(_hull_concavity(features), 3)},
                priority=130,
                reason="hull concavity > 0.4 suggests a pocketed feature",
            )
        )

    tap_targets = _thread_callout_holes(pmi, hole_refs)
    for ref in sorted(tap_targets):
        ops.append(
            Operation(
                op="tap",
                feature_ref=ref,
                params={"thread_callout": "see PMI"},
                priority=150,
                reason="thread callout in PMI",
            )
        )

    fine_surface = _has_fine_surface(pmi, FINE_SURFACE_FINISH_RA)
    mirror_surface = _has_fine_surface(pmi, MIRROR_SURFACE_FINISH_RA)
    if fine_surface or mirror_surface:
        # finish_pass priority is 200 by design - groups at end of milling.
        ops.append(
            Operation(
                op="finish_pass",
                feature_ref="part",
                params={
                    "ra_threshold_um": MIRROR_SURFACE_FINISH_RA
                    if mirror_surface
                    else FINE_SURFACE_FINISH_RA,
                },
                priority=200,
                reason=(
                    "Ra < 0.8 um requires finish pass"
                    if mirror_surface
                    else "Ra < 1.6 um requires finish pass"
                ),
            )
        )

    datums = _datum_list(pmi)
    # Inspect once per datum at end of plan.
    for ident in datums:
        ops.append(
            Operation(
                op="inspect",
                feature_ref=f"datum_{ident}",
                params={"datum": ident},
                priority=220,
                reason=f"verify datum {ident}",
            )
        )
    if not datums:
        ops.append(
            Operation(
                op="inspect",
                feature_ref="part",
                params={},
                priority=220,
                reason="final part inspection",
            )
        )

    # Setup count: 1 by default, bump to 2 if holes use more than one axis.
    setup_count = 1
    if len(hole_dicts) >= 2:
        axes_seen: set[tuple[int, int, int]] = set()
        for h in hole_dicts:
            axis = h.get("axis_dir")
            if isinstance(axis, (tuple, list)) and len(axis) == 3:
                axes_seen.add(tuple(round(float(a), 1) for a in axis))  # type: ignore[arg-type]
                axes_seen.add(tuple(round(float(-a), 1) for a in axis))  # type: ignore[arg-type]
        # Each physical axis appears twice (once and its negation).
        if len(axes_seen) > 2:
            setup_count = 2

    time_min = (
        2.0  # mill_contour flat fee
        + len(hole_dicts) * TIME_DRILL_PER_HOLE
        + len(tight) * TIME_REAM_PER_HOLE
        + n_pockets * TIME_MILL_POCKET_PER_POCKET
        + len(tap_targets) * TIME_DRILL_PER_HOLE  # tap ~= drill rate
        + (TIME_FINISH_PASS_FLAT if (fine_surface or mirror_surface) else 0.0)
        + (len(datums) if datums else 1) * TIME_INSPECT_PER_DATUM
        + len(tight) * TIME_INSPECT_PER_DATUM
    )

    notes = ""
    if datums:
        notes = f"setup against datums {','.join(datums)}"

    return MachiningStrategy(
        primary_process="machining",
        operations=ops,
        setup_count=setup_count,
        estimated_time_min=round(time_min, 2),
        notes=notes,
    )


def _plan_unknown() -> MachiningStrategy:
    return MachiningStrategy(
        primary_process="unknown",
        operations=[
            Operation(
                op="inspect",
                feature_ref="part",
                params={},
                priority=200,
                reason="uncertain classification - manual review",
            )
        ],
        setup_count=0,
        estimated_time_min=round(TIME_INSPECT_PER_DATUM, 2),
        notes="classification uncertain; strategy is a placeholder",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def recommend(
    features: ManufacturingFeatures,
    pmi: PMIRecord | None,
    classification: ClassificationResult,
    holes: HolePattern,
    unfold: UnfoldResult | None = None,
    profile_match: ProfileMatch | None = None,  # noqa: ARG001 - reserved for future routing
) -> MachiningStrategy:
    """Produce a :class:`MachiningStrategy` from the analyzer outputs.

    Routing
    -------
    - ``classification.label == "plaat"`` -> sheet metal: laser_cut +
      press_brake_bend per bend + deburr.
    - ``classification.label == "profiel"`` -> purchased: inspect only.
    - ``classification.label == "anders"`` -> machining: mill_contour,
      drill per hole, optional ream / mill_pocket / tap / finish_pass,
      inspect per datum.
    - anything else (``"uncertain"``, missing, etc) -> unknown: inspect.

    The probe never raises; any internal exception degrades to the
    ``unknown`` plan.
    """
    try:
        label = (
            classification.label.lower().strip()
            if classification is not None and classification.label
            else ""
        )
        if features is None:
            features = ManufacturingFeatures()
        if holes is None:
            holes = HolePattern()

        if label == "plaat":
            return _plan_sheet_metal(features, holes, unfold)
        if label == "profiel":
            return _plan_purchased()
        if label == "anders":
            return _plan_machining(features, pmi, holes)
        return _plan_unknown()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("cam recommend failed: %s", exc)
        return _plan_unknown()
