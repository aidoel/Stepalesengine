"""DXF flat-pattern writer.

Renders :class:`FlatPattern` records to DXF files using laser-cutter
industry-standard layer conventions. Pure-Python; depends only on ``ezdxf``
and the standard library.

Public API:
    - ``FlatPattern`` — geometric description of an unfolded blank.
    - ``write_dxf(pattern, out_path, ...)`` — single pattern -> one DXF file.
    - ``write_assembly_dxf(patterns, out_path, ...)`` — many patterns in one DXF,
      optionally laid out in a simple linear nest.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import ezdxf

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Layer specification
# ---------------------------------------------------------------------------

# (color_aci, linetype, lineweight_mm)
# ezdxf encodes lineweight in 1/100 mm (integer enum). 50 == 0.50 mm.
_LAYER_SPEC: dict[str, dict] = {
    "OUTER": {"color": 1, "linetype": "Continuous", "lineweight": 50},
    "INNER": {"color": 3, "linetype": "Continuous", "lineweight": 30},
    "BEND_UP": {"color": 5, "linetype": "DASHED2", "lineweight": 15},
    "BEND_DOWN": {"color": 4, "linetype": "DASHED2", "lineweight": 15},
    "ANNOTATION": {"color": 7, "linetype": "Continuous", "lineweight": 13},
    "INFO": {"color": 7, "linetype": "Continuous", "lineweight": 13},
}

# Public so callers can introspect / override.
LAYER_SPEC = _LAYER_SPEC


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class FlatPattern:
    """Geometric description of an unfolded sheet-metal blank.

    All coordinates are 2D in millimetres (unless ``units`` overridden).
    Polylines are interpreted as closed contours; the writer joins the last
    vertex back to the first automatically.
    """

    outer_contour: list[list[tuple[float, float]]] = field(default_factory=list)
    holes: list[list[tuple[float, float]]] = field(default_factory=list)
    bend_lines: list[tuple[tuple[float, float], tuple[float, float], dict]] = field(
        default_factory=list
    )
    annotations: list[tuple[tuple[float, float], str]] = field(default_factory=list)
    thickness: float = 0.0
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    units: str = "mm"
    part_name: str = ""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _distinct_points(pts: list[tuple[float, float]], tol: float = 1e-9) -> int:
    """Count unique 2D points within tolerance."""
    seen: list[tuple[float, float]] = []
    for p in pts:
        if not any(abs(p[0] - q[0]) < tol and abs(p[1] - q[1]) < tol for q in seen):
            seen.append(p)
    return len(seen)


def _validate(pattern: FlatPattern) -> None:
    if pattern.thickness <= 0:
        raise ValueError(f"FlatPattern.thickness must be > 0 (got {pattern.thickness!r})")
    if not pattern.outer_contour:
        raise ValueError("FlatPattern.outer_contour must contain at least one polyline")
    for idx, poly in enumerate(pattern.outer_contour):
        if _distinct_points(poly) < 3:
            raise ValueError(f"outer_contour[{idx}] needs >= 3 distinct points (got {len(poly)})")
    for idx, poly in enumerate(pattern.holes):
        if _distinct_points(poly) < 3:
            raise ValueError(f"holes[{idx}] needs >= 3 distinct points (got {len(poly)})")
    for idx, (p1, p2, _meta) in enumerate(pattern.bend_lines):
        if math.isclose(p1[0], p2[0], abs_tol=1e-9) and math.isclose(p1[1], p2[1], abs_tol=1e-9):
            raise ValueError(f"bend_lines[{idx}] endpoints are coincident: {p1!r} == {p2!r}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_layers(doc, needed: Iterable[str]) -> None:
    for name in needed:
        if name in doc.layers:
            continue
        spec = _LAYER_SPEC[name]
        doc.layers.add(
            name,
            color=spec["color"],
            linetype=spec["linetype"],
            lineweight=spec["lineweight"],
        )


def _pattern_bbox(pattern: FlatPattern) -> tuple[float, float, float, float]:
    """Compute bbox from outer contour points (falls back to declared bbox)."""
    pts: list[tuple[float, float]] = []
    for poly in pattern.outer_contour:
        pts.extend(poly)
    if not pts:
        return pattern.bbox
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_size(bbox: tuple[float, float, float, float]) -> float:
    dx = bbox[2] - bbox[0]
    dy = bbox[3] - bbox[1]
    return math.hypot(dx, dy) or 1.0


def _translate(pts: list[tuple[float, float]], dx: float, dy: float) -> list[tuple[float, float]]:
    return [(x + dx, y + dy) for (x, y) in pts]


def _translate_pattern(pattern: FlatPattern, dx: float, dy: float) -> FlatPattern:
    return FlatPattern(
        outer_contour=[_translate(p, dx, dy) for p in pattern.outer_contour],
        holes=[_translate(p, dx, dy) for p in pattern.holes],
        bend_lines=[
            ((p1[0] + dx, p1[1] + dy), (p2[0] + dx, p2[1] + dy), dict(meta))
            for (p1, p2, meta) in pattern.bend_lines
        ],
        annotations=[((p[0] + dx, p[1] + dy), txt) for (p, txt) in pattern.annotations],
        thickness=pattern.thickness,
        bbox=(
            pattern.bbox[0] + dx,
            pattern.bbox[1] + dy,
            pattern.bbox[2] + dx,
            pattern.bbox[3] + dy,
        ),
        units=pattern.units,
        part_name=pattern.part_name,
    )


def _bend_layer(meta: dict) -> str:
    direction = str(meta.get("direction", "up")).lower()
    return "BEND_DOWN" if direction == "down" else "BEND_UP"


def _bend_length(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])


# ---------------------------------------------------------------------------
# Core drawing routine
# ---------------------------------------------------------------------------


def _draw_pattern(doc, msp, pattern: FlatPattern, ltscale: float) -> None:
    """Draw a single pattern into the modelspace of ``doc``.

    Assumes layers already exist. Adds title-block + bend-table text positioned
    relative to the pattern's outer-contour bbox.
    """
    # --- outer contour(s)
    for poly in pattern.outer_contour:
        msp.add_lwpolyline(poly, close=True, dxfattribs={"layer": "OUTER"})

    # --- holes
    for poly in pattern.holes:
        msp.add_lwpolyline(poly, close=True, dxfattribs={"layer": "INNER"})

    # --- bend lines
    for p1, p2, meta in pattern.bend_lines:
        layer = _bend_layer(meta)
        msp.add_line(
            p1,
            p2,
            dxfattribs={"layer": layer, "ltscale": ltscale},
        )

    # --- bbox for text placement (use actual geometry, not declared bbox)
    bbox = _pattern_bbox(pattern)
    xmin, ymin, xmax, ymax = bbox
    height = max((ymax - ymin), 1.0)
    text_h = max(height * 0.025, 2.5)

    # --- INFO header at upper-left
    header_txt = f"{pattern.part_name} | t={pattern.thickness:.2f}{pattern.units}"
    info_text = msp.add_text(
        header_txt,
        dxfattribs={"layer": "INFO", "height": text_h},
    )
    info_text.set_placement((xmin, ymax + text_h * 1.5))

    # --- bend-table block below part bbox
    if pattern.bend_lines:
        lines = ["BEND TABLE:"]
        for idx, (p1, p2, meta) in enumerate(pattern.bend_lines):
            angle = float(meta.get("angle", 0.0))
            radius = float(meta.get("radius", 0.0))
            direction = str(meta.get("direction", "up"))
            length = _bend_length(p1, p2)
            lines.append(
                f"  #{idx} dir={direction} angle={angle:.1f} "
                f"radius={radius:.2f} length={length:.2f}"
            )
        bend_block = "\n".join(lines)
        bt_text = msp.add_mtext(
            bend_block,
            dxfattribs={"layer": "ANNOTATION", "char_height": text_h},
        )
        bt_text.set_location(insert=(xmin, ymin - text_h * 2.0))

    # --- user annotations
    for pos, txt in pattern.annotations:
        ann = msp.add_text(
            txt,
            dxfattribs={"layer": "ANNOTATION", "height": text_h},
        )
        ann.set_placement(pos)


# ---------------------------------------------------------------------------
# Public writers
# ---------------------------------------------------------------------------


def _new_doc(version: str):
    doc = ezdxf.new(dxfversion=version, setup=True)
    # Millimetres
    doc.header["$INSUNITS"] = 4
    # Ensure DASHED2 is present (setup=True does this but be defensive).
    if "DASHED2" not in doc.linetypes:
        doc.linetypes.add(
            "DASHED2",
            pattern="A,.5,-.25",
            description="Dashed (.5x) __  __  __  __  __  __  __  __",
        )
    return doc


def write_dxf(
    pattern: FlatPattern,
    out_path: str | Path,
    *,
    version: str = "R2010",
    layer_naming: str = "default",  # noqa: ARG001 - reserved for future schemes
) -> Path:
    """Render a single :class:`FlatPattern` to a DXF file.

    Returns the path written. Raises :class:`ValueError` on invalid input.
    """
    _validate(pattern)

    out_path = Path(out_path)
    doc = _new_doc(version)
    _ensure_layers(doc, _LAYER_SPEC.keys())

    msp = doc.modelspace()
    bbox = _pattern_bbox(pattern)
    ltscale = max(1.0, _bbox_size(bbox) / 100.0)

    _draw_pattern(doc, msp, pattern, ltscale)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(out_path)
    return out_path


def _compute_utilization(
    placed_bboxes: list[tuple[float, float, float, float]],
    sheet_size: tuple[float, float],
) -> float:
    """Return packing density = sum(pattern bbox areas) / sheet area.

    ``placed_bboxes`` are the *original* (pre-placement) bbox tuples of every
    pattern placed on a single sheet. Result is in [0, 1] for non-overlapping
    placements that fit within the sheet.
    """
    sheet_w, sheet_h = sheet_size
    sheet_area = sheet_w * sheet_h
    if sheet_area <= 0:
        return 0.0
    used = sum(max(0.0, bb[2] - bb[0]) * max(0.0, bb[3] - bb[1]) for bb in placed_bboxes)
    return used / sheet_area


def _shelf_binpack(
    patterns: list[FlatPattern],
    sheet_size: tuple[float, float],
    margin: float,
) -> list[tuple[int, float, float, FlatPattern]]:
    """Shelf-fill bottom-left bin-packer.

    Sort by bbox height descending, then greedily place onto the current shelf
    if the remaining width admits ``part_width + margin``. Otherwise open a
    new shelf above; if the new shelf overflows sheet height, start a new
    sheet. No rotation is performed.

    Returns list of (sheet_index, dx, dy, original_pattern) tuples. The
    translation ``(dx, dy)`` shifts the pattern so its bbox-min lands at the
    intended sheet-local placement origin.
    """
    sheet_w, sheet_h = sheet_size

    # Index-tagged so the caller can recover original order if needed.
    items: list[tuple[FlatPattern, tuple[float, float, float, float]]] = [
        (p, _pattern_bbox(p)) for p in patterns
    ]
    # Tallest first; tie-break by width descending for stability.
    items.sort(key=lambda it: (it[1][3] - it[1][1], it[1][2] - it[1][0]), reverse=True)

    results: list[tuple[int, float, float, FlatPattern]] = []
    sheet_idx = 0
    shelf_y = margin  # bottom of current shelf
    shelf_h = 0.0  # tallest part in the current shelf
    cursor_x = margin  # next x slot on this shelf

    for pat, bb in items:
        pw = bb[2] - bb[0]
        ph = bb[3] - bb[1]

        # Oversized for any sheet: place alone on a fresh sheet anyway.
        if pw + 2 * margin > sheet_w or ph + 2 * margin > sheet_h:
            # Close out current sheet if it has content, then place on its own.
            if results and (cursor_x > margin or shelf_y > margin):
                sheet_idx += 1
            dx = margin - bb[0]
            dy = margin - bb[1]
            results.append((sheet_idx, dx, dy, pat))
            sheet_idx += 1
            shelf_y = margin
            shelf_h = 0.0
            cursor_x = margin
            continue

        # Try current shelf.
        if cursor_x + pw + margin <= sheet_w:
            dx = cursor_x - bb[0]
            dy = shelf_y - bb[1]
            results.append((sheet_idx, dx, dy, pat))
            cursor_x += pw + margin
            if ph > shelf_h:
                shelf_h = ph
            continue

        # New shelf above.
        new_shelf_y = shelf_y + shelf_h + margin
        if new_shelf_y + ph + margin > sheet_h:
            # Overflow sheet height: open a new sheet.
            sheet_idx += 1
            shelf_y = margin
            shelf_h = 0.0
            cursor_x = margin
        else:
            shelf_y = new_shelf_y
            shelf_h = 0.0
            cursor_x = margin

        dx = cursor_x - bb[0]
        dy = shelf_y - bb[1]
        results.append((sheet_idx, dx, dy, pat))
        cursor_x += pw + margin
        if ph > shelf_h:
            shelf_h = ph

    return results


def write_assembly_dxf(
    patterns: list[FlatPattern],
    out_path: str | Path,
    *,
    nesting: str = "none",
    sheet_size: tuple[float, float] | None = None,
    margin: float = 10.0,
) -> Path:
    """Write multiple flat patterns into a single DXF.

    ``nesting`` modes:
        - ``"none"``: every pattern is drawn at its own coordinates.
        - ``"strip"``: patterns are translated so their bboxes are laid out in
          a row along +X with a fixed margin between them, never overlapping.
        - ``"binpack"``: shelf-fill bottom-left bin-pack into rectangles of
          ``sheet_size`` (mm). When more than one sheet is needed, each sheet
          is drawn offset by ``(sheet_index * (sheet_width + 50), 0)``. When
          ``sheet_size`` is None the sheet side is inferred as 1.5x the
          largest pattern bbox dimension (square).
    """
    if not patterns:
        raise ValueError("write_assembly_dxf requires at least one pattern")
    for p in patterns:
        _validate(p)
    if nesting not in {"none", "strip", "binpack"}:
        raise ValueError(f"unknown nesting mode: {nesting!r}")
    if sheet_size is not None:
        sw, sh = sheet_size
        if sw <= 0 or sh <= 0:
            raise ValueError(f"sheet_size must be positive in both dimensions (got {sheet_size!r})")

    out_path = Path(out_path)
    doc = _new_doc("R2010")
    _ensure_layers(doc, _LAYER_SPEC.keys())
    msp = doc.modelspace()

    # Determine placement transforms.
    placed: list[FlatPattern] = []
    if nesting == "none":
        placed = list(patterns)
    elif nesting == "strip":
        cursor_x = 0.0
        strip_margin = 10.0
        for p in patterns:
            bb = _pattern_bbox(p)
            dx = cursor_x - bb[0]
            dy = -bb[1]  # align baselines to y=0
            placed.append(_translate_pattern(p, dx, dy))
            cursor_x += (bb[2] - bb[0]) + strip_margin
    else:  # binpack
        # Infer sheet side as 1.5x the largest pattern bbox dimension (square).
        if sheet_size is None:
            biggest = 0.0
            for p in patterns:
                bb = _pattern_bbox(p)
                biggest = max(biggest, bb[2] - bb[0], bb[3] - bb[1])
            side = 1.5 * biggest
            sheet_size = (side, side)

        sheet_w, sheet_h = sheet_size
        placements = _shelf_binpack(patterns, sheet_size, margin)

        # Per-sheet X offset so sheets render side-by-side without overlap.
        sheet_gap = 50.0
        per_sheet_bboxes: dict[int, list[tuple[float, float, float, float]]] = {}
        for sheet_idx, dx, dy, pat in placements:
            offset_x = sheet_idx * (sheet_w + sheet_gap)
            placed.append(_translate_pattern(pat, dx + offset_x, dy))
            per_sheet_bboxes.setdefault(sheet_idx, []).append(_pattern_bbox(pat))

        n_sheets = max(per_sheet_bboxes) + 1 if per_sheet_bboxes else 0
        utils = [_compute_utilization(per_sheet_bboxes[i], sheet_size) for i in range(n_sheets)]
        avg = sum(utils) / len(utils) if utils else 0.0
        _log.info(
            "binpack: %d patterns on %d sheet(s) of %.1fx%.1f mm; "
            "utilization per sheet=%s mean=%.3f",
            len(patterns),
            n_sheets,
            sheet_w,
            sheet_h,
            [round(u, 3) for u in utils],
            avg,
        )

    # Compute global ltscale from union bbox so dashes are visible at any size.
    all_bbox = _pattern_bbox(placed[0])
    for p in placed[1:]:
        bb = _pattern_bbox(p)
        all_bbox = (
            min(all_bbox[0], bb[0]),
            min(all_bbox[1], bb[1]),
            max(all_bbox[2], bb[2]),
            max(all_bbox[3], bb[3]),
        )
    ltscale = max(1.0, _bbox_size(all_bbox) / 100.0)

    for p in placed:
        _draw_pattern(doc, msp, p, ltscale)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(out_path)
    return out_path


__all__ = [
    "FlatPattern",
    "LAYER_SPEC",
    "write_dxf",
    "write_assembly_dxf",
]
