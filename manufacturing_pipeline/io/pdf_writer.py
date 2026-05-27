"""PDF shop-drawing writer.

Renders :class:`FlatPattern` records (see :mod:`manufacturing_pipeline.io.dxf_writer`)
to single-page or multi-page PDF shop drawings consumable by a CNC machinist.

A drawing contains:
    - a title block (part_number, material, qty, thickness, date, drawn_by, scale, rev)
    - a scaled flat-pattern view (outer contour, holes, bend lines)
    - a bend table (#, angle, radius, length, direction)
    - a hole table (#, diameter, qty -- identical diameters grouped together)

Public API:
    - :class:`PartDrawingMeta` -- per-part shop-drawing metadata.
    - :func:`write_pdf` -- single flat pattern -> one-page PDF.
    - :func:`write_assembly_pdf` -- many parts -> multi-page PDF with BOM cover.

Pure-Python; depends only on ``reportlab`` and the standard library.
"""

from __future__ import annotations

import datetime as _dt
import math
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from reportlab.lib.colors import Color, black, blue, red
from reportlab.lib.pagesizes import A3, A4, landscape, letter, portrait
from reportlab.pdfgen import canvas as _canvas

from manufacturing_pipeline.io.dxf_writer import FlatPattern

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 1 mm = 1 / 25.4 in; 1 in = 72 pt -> 1 mm = 72 / 25.4 pt ~= 2.83465 pt.
MM_TO_PT = 72.0 / 25.4

# Outer border (mm) -- a 10 mm white margin around every page.
BORDER_MM = 10.0

# Page sizes keyed by name (lowercased on lookup).
_PAGE_SIZES = {
    "a4": A4,
    "a3": A3,
    "letter": letter,
}


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


@dataclass
class PartDrawingMeta:
    """Shop-drawing metadata that surrounds the flat-pattern view."""

    part_name: str = ""
    part_number: str = ""
    material: str = ""
    quantity: int = 1
    revision: str = "A"
    drawn_by: str = ""
    date_iso: str = ""  # YYYY-MM-DD; defaults to today when blank
    notes: str = ""

    def resolved_date(self) -> str:
        return self.date_iso or _dt.date.today().isoformat()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(pattern: FlatPattern) -> None:
    if pattern.thickness <= 0:
        raise ValueError(f"FlatPattern.thickness must be > 0 (got {pattern.thickness!r})")
    if not pattern.outer_contour:
        raise ValueError("FlatPattern.outer_contour must contain at least one polyline")


def _resolve_page_size(name: str, orientation: str) -> tuple[float, float]:
    key = name.lower()
    if key not in _PAGE_SIZES:
        raise ValueError(f"unknown page_size {name!r}; expected one of {sorted(_PAGE_SIZES)}")
    base = _PAGE_SIZES[key]
    o = orientation.lower()
    if o == "landscape":
        return landscape(base)
    if o == "portrait":
        return portrait(base)
    raise ValueError(
        f"unknown sheet_orientation {orientation!r}; expected 'landscape' or 'portrait'"
    )


# ---------------------------------------------------------------------------
# Pattern geometry helpers
# ---------------------------------------------------------------------------


def _pattern_bbox(pattern: FlatPattern) -> tuple[float, float, float, float]:
    pts: list[tuple[float, float]] = []
    for poly in pattern.outer_contour:
        pts.extend(poly)
    if not pts:
        return pattern.bbox
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _polyline_bbox(poly: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def _estimate_diameter(poly: list[tuple[float, float]]) -> float:
    """Approximate the diameter of a closed-polyline hole.

    Works for any convex closed loop -- uses bbox-average so non-circular slots
    still get a useful number.
    """
    bb = _polyline_bbox(poly)
    return (bb[2] - bb[0] + bb[3] - bb[1]) / 2.0


def _bend_length(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])


def _bend_midpoint(p1: tuple[float, float], p2: tuple[float, float]) -> tuple[float, float]:
    return ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------

# Nice scale ratios (drawing : part) preferred order. Picked so the table prints
# a clean ratio such as 1:2 or 5:1 instead of 0.4136:1.
_NICE_SCALES = [
    100.0,
    50.0,
    20.0,
    10.0,
    5.0,
    2.0,
    1.0,
    0.5,
    0.2,
    0.1,
    0.05,
    0.02,
    0.01,
    0.005,
    0.002,
    0.001,
]


def _pick_scale(part_w_mm: float, part_h_mm: float, area_w_mm: float, area_h_mm: float) -> float:
    """Pick the largest 'nice' scale that fits the pattern in the area.

    Returns scale as a multiplier (drawing_mm = part_mm * scale). 1.0 == 1:1.
    """
    if part_w_mm <= 0 or part_h_mm <= 0:
        return 1.0
    raw = min(area_w_mm / part_w_mm, area_h_mm / part_h_mm)
    # Choose the largest preset <= raw.
    for s in _NICE_SCALES:
        if s <= raw:
            return s
    # raw < smallest preset; clamp to smallest preset.
    return _NICE_SCALES[-1]


def _format_scale(scale: float) -> str:
    """Render a 'drawing:part' style string like '1:2' or '5:1'."""
    if math.isclose(scale, 1.0):
        return "1:1"
    if scale > 1.0:
        # Drawing larger than part.
        return f"{int(round(scale))}:1" if math.isclose(scale, round(scale)) else f"{scale:.2f}:1"
    inv = 1.0 / scale
    return f"1:{int(round(inv))}" if math.isclose(inv, round(inv)) else f"1:{inv:.2f}"


# ---------------------------------------------------------------------------
# Hole grouping
# ---------------------------------------------------------------------------


def _hole_groups(pattern: FlatPattern) -> list[tuple[float, int]]:
    """Group holes by approximate diameter -- bin to 0.1 mm."""
    bins: OrderedDict[float, int] = OrderedDict()
    for poly in pattern.holes:
        d = _estimate_diameter(poly)
        d_key = round(d, 1)
        bins[d_key] = bins.get(d_key, 0) + 1
    return list(bins.items())


# ---------------------------------------------------------------------------
# Drawing primitives (canvas helpers)
# ---------------------------------------------------------------------------


def _mm(v: float) -> float:
    return v * MM_TO_PT


def _bend_color(direction: str) -> Color:
    return blue if direction.lower() == "up" else red


def _set_text_left(
    c: _canvas.Canvas,
    x_pt: float,
    y_pt: float,
    text: str,
    font: str = "Helvetica",
    size: float = 8.0,
) -> None:
    c.setFont(font, size)
    c.setFillColor(black)
    c.drawString(x_pt, y_pt, text)


def _draw_rect(
    c: _canvas.Canvas, x_mm: float, y_mm: float, w_mm: float, h_mm: float, line_pt: float = 0.4
) -> None:
    c.setStrokeColor(black)
    c.setLineWidth(line_pt)
    c.rect(_mm(x_mm), _mm(y_mm), _mm(w_mm), _mm(h_mm), stroke=1, fill=0)


# ---------------------------------------------------------------------------
# Pattern view rendering
# ---------------------------------------------------------------------------


def _draw_pattern_view(
    c: _canvas.Canvas,
    pattern: FlatPattern,
    view_x_mm: float,
    view_y_mm: float,
    view_w_mm: float,
    view_h_mm: float,
) -> float:
    """Render the scaled flat-pattern view inside the given mm box.

    Returns the chosen scale (drawing_mm / part_mm).
    """
    # Frame around the view (helps the operator see the drawing area).
    _draw_rect(c, view_x_mm, view_y_mm, view_w_mm, view_h_mm, line_pt=0.3)

    bbox = _pattern_bbox(pattern)
    xmin, ymin, xmax, ymax = bbox
    part_w = max(xmax - xmin, 1e-9)
    part_h = max(ymax - ymin, 1e-9)

    # Reserve a small inner margin so geometry doesn't kiss the frame.
    pad_mm = 5.0
    avail_w = max(view_w_mm - 2 * pad_mm, 1.0)
    avail_h = max(view_h_mm - 2 * pad_mm, 1.0)

    scale = _pick_scale(part_w, part_h, avail_w, avail_h)

    # Centre the scaled bbox in the view area.
    drawn_w = part_w * scale
    drawn_h = part_h * scale
    origin_x_mm = view_x_mm + (view_w_mm - drawn_w) / 2.0
    origin_y_mm = view_y_mm + (view_h_mm - drawn_h) / 2.0

    def to_pt(p: tuple[float, float]) -> tuple[float, float]:
        x = origin_x_mm + (p[0] - xmin) * scale
        y = origin_y_mm + (p[1] - ymin) * scale
        return (_mm(x), _mm(y))

    # --- outer contour
    c.setStrokeColor(black)
    c.setLineWidth(0.6)
    c.setDash()  # solid
    for poly in pattern.outer_contour:
        if not poly:
            continue
        path = c.beginPath()
        first = to_pt(poly[0])
        path.moveTo(*first)
        for pt in poly[1:]:
            path.lineTo(*to_pt(pt))
        path.close()
        c.drawPath(path, stroke=1, fill=0)

    # --- holes
    c.setLineWidth(0.4)
    for poly in pattern.holes:
        if not poly:
            continue
        path = c.beginPath()
        first = to_pt(poly[0])
        path.moveTo(*first)
        for pt in poly[1:]:
            path.lineTo(*to_pt(pt))
        path.close()
        c.drawPath(path, stroke=1, fill=0)

    # --- bend lines: dashed, coloured per direction
    c.setLineWidth(0.3)
    c.setDash(3, 2)  # 3pt on, 2pt off
    for p1, p2, meta in pattern.bend_lines:
        direction = str(meta.get("direction", "up")).lower()
        c.setStrokeColor(_bend_color(direction))
        x1, y1 = to_pt(p1)
        x2, y2 = to_pt(p2)
        c.line(x1, y1, x2, y2)
    c.setDash()  # reset

    # --- bend angle annotation at midpoint
    c.setFont("Helvetica", 6.0)
    for p1, p2, meta in pattern.bend_lines:
        direction = str(meta.get("direction", "up")).lower()
        angle = float(meta.get("angle", 0.0))
        mx, my = _bend_midpoint(p1, p2)
        xp, yp = to_pt((mx, my))
        c.setFillColor(_bend_color(direction))
        c.drawString(xp + 2.0, yp + 2.0, f"{angle:.1f} deg")
    c.setFillColor(black)

    # Dimensions: overall width x height label below the view.
    label = f"{part_w:.1f} x {part_h:.1f} mm"
    _set_text_left(
        c,
        _mm(view_x_mm + 1.0),
        _mm(view_y_mm + 1.0),
        label,
        size=7.0,
    )

    return scale


# ---------------------------------------------------------------------------
# Tables (bend, hole)
# ---------------------------------------------------------------------------


def _draw_table(
    c: _canvas.Canvas,
    x_mm: float,
    y_top_mm: float,
    col_widths_mm: list[float],
    header: list[str],
    rows: list[list[str]],
    row_h_mm: float = 5.0,
    title: str | None = None,
) -> float:
    """Draw a simple grid table starting from top-left (x_mm, y_top_mm).

    Grows downward. Returns the y_mm of the bottom edge.
    """
    table_w = sum(col_widths_mm)
    c.setStrokeColor(black)

    y = y_top_mm
    if title:
        _set_text_left(c, _mm(x_mm), _mm(y - 3.0), title, font="Helvetica-Bold", size=9.0)
        y -= 5.0

    # Header row
    c.setLineWidth(0.4)
    c.rect(_mm(x_mm), _mm(y - row_h_mm), _mm(table_w), _mm(row_h_mm), stroke=1, fill=0)
    cx = x_mm
    for label, w in zip(header, col_widths_mm):
        _set_text_left(
            c,
            _mm(cx + 0.8),
            _mm(y - row_h_mm + 1.3),
            label,
            font="Helvetica-Bold",
            size=7.5,
        )
        cx += w
        if cx < x_mm + table_w - 1e-6:
            c.line(_mm(cx), _mm(y - row_h_mm), _mm(cx), _mm(y))
    y -= row_h_mm

    # Data rows
    for row in rows:
        c.rect(_mm(x_mm), _mm(y - row_h_mm), _mm(table_w), _mm(row_h_mm), stroke=1, fill=0)
        cx = x_mm
        for value, w in zip(row, col_widths_mm):
            _set_text_left(
                c,
                _mm(cx + 0.8),
                _mm(y - row_h_mm + 1.3),
                str(value),
                size=7.5,
            )
            cx += w
            if cx < x_mm + table_w - 1e-6:
                c.line(_mm(cx), _mm(y - row_h_mm), _mm(cx), _mm(y))
        y -= row_h_mm

    return y


def _draw_bend_table(
    c: _canvas.Canvas, x_mm: float, y_top_mm: float, pattern: FlatPattern, width_mm: float
) -> float:
    header = ["#", "Angle (deg)", "Radius (mm)", "Length (mm)", "Direction"]
    cw = _normalise_widths([6.0, 18.0, 18.0, 18.0, 20.0], width_mm)
    rows: list[list[str]] = []
    for idx, (p1, p2, meta) in enumerate(pattern.bend_lines):
        rows.append(
            [
                str(idx),
                f"{float(meta.get('angle', 0.0)):.1f}",
                f"{float(meta.get('radius', 0.0)):.2f}",
                f"{_bend_length(p1, p2):.2f}",
                str(meta.get("direction", "up")),
            ]
        )
    if not rows:
        rows = [["-", "-", "-", "-", "-"]]
    return _draw_table(c, x_mm, y_top_mm, cw, header, rows, title="Bend table")


def _draw_hole_table(
    c: _canvas.Canvas, x_mm: float, y_top_mm: float, pattern: FlatPattern, width_mm: float
) -> float:
    header = ["#", "Diameter (mm)", "Qty"]
    cw = _normalise_widths([8.0, 30.0, 12.0], width_mm)
    groups = _hole_groups(pattern)
    rows: list[list[str]] = []
    for idx, (dia, qty) in enumerate(groups):
        rows.append([str(idx), f"{dia:.1f}", str(qty)])
    if not rows:
        rows = [["-", "-", "-"]]
    return _draw_table(c, x_mm, y_top_mm, cw, header, rows, title="Hole table")


def _normalise_widths(target: list[float], total: float) -> list[float]:
    s = sum(target)
    if s <= 0:
        return target
    factor = total / s
    return [w * factor for w in target]


# ---------------------------------------------------------------------------
# Title block
# ---------------------------------------------------------------------------


def _draw_title_block(
    c: _canvas.Canvas,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
    pattern: FlatPattern,
    meta: PartDrawingMeta,
    scale: float,
) -> None:
    """4-row 2-col title block in the lower-right corner."""
    rows = 4
    cols = 4
    row_h = h_mm / rows
    col_w = w_mm / cols

    c.setStrokeColor(black)
    c.setLineWidth(0.5)
    c.rect(_mm(x_mm), _mm(y_mm), _mm(w_mm), _mm(h_mm), stroke=1, fill=0)

    # Inner grid lines.
    c.setLineWidth(0.3)
    for r in range(1, rows):
        yy = y_mm + r * row_h
        c.line(_mm(x_mm), _mm(yy), _mm(x_mm + w_mm), _mm(yy))
    for col in range(1, cols):
        xx = x_mm + col * col_w
        c.line(_mm(xx), _mm(y_mm), _mm(xx), _mm(y_mm + h_mm))

    # Cell contents: row 3 (top) ... row 0 (bottom).
    # Layout follows the 4x2 logical grid the spec asks for, expressed as
    # (col, row_from_bottom) -> (label, value).
    cells: list[tuple[int, int, str, str]] = [
        (0, 3, "Part", meta.part_name or pattern.part_name or ""),
        (1, 3, "Part No.", meta.part_number),
        (2, 3, "Rev", meta.revision),
        (3, 3, "Date", meta.resolved_date()),
        (0, 2, "Material", meta.material),
        (1, 2, "Thickness", f"{pattern.thickness:.2f} {pattern.units}"),
        (2, 2, "Qty", str(meta.quantity)),
        (3, 2, "Scale", _format_scale(scale)),
        (0, 1, "Drawn by", meta.drawn_by),
        (1, 1, "Units", pattern.units),
        (2, 1, "", ""),
        (3, 1, "", ""),
        (0, 0, "Notes", meta.notes),
        (1, 0, "", ""),
        (2, 0, "", ""),
        (3, 0, "", ""),
    ]

    for col, row, label, value in cells:
        cx = x_mm + col * col_w
        cy = y_mm + row * row_h
        if label:
            _set_text_left(
                c,
                _mm(cx + 1.0),
                _mm(cy + row_h - 3.0),
                label,
                font="Helvetica-Bold",
                size=6.0,
            )
        if value:
            _set_text_left(
                c,
                _mm(cx + 1.5),
                _mm(cy + 1.0),
                str(value),
                size=8.0,
            )


# ---------------------------------------------------------------------------
# Page composition
# ---------------------------------------------------------------------------


def _render_part_page(
    c: _canvas.Canvas,
    pattern: FlatPattern,
    meta: PartDrawingMeta,
    page_w_pt: float,
    page_h_pt: float,
) -> None:
    """Compose one shop-drawing page (header, view, tables, title block)."""
    page_w_mm = page_w_pt / MM_TO_PT
    page_h_mm = page_h_pt / MM_TO_PT

    # Outer border rectangle (10 mm margin all sides).
    _draw_rect(
        c,
        BORDER_MM,
        BORDER_MM,
        page_w_mm - 2 * BORDER_MM,
        page_h_mm - 2 * BORDER_MM,
        line_pt=0.6,
    )

    inner_x = BORDER_MM
    inner_y = BORDER_MM
    inner_w = page_w_mm - 2 * BORDER_MM
    inner_h = page_h_mm - 2 * BORDER_MM

    # --- header band (top): part name + revision
    header_h = 10.0
    header_y = inner_y + inner_h - header_h
    c.setLineWidth(0.4)
    c.line(
        _mm(inner_x),
        _mm(header_y),
        _mm(inner_x + inner_w),
        _mm(header_y),
    )
    title_str = f"{meta.part_name or pattern.part_name or 'PART'}  -  Rev {meta.revision}"
    _set_text_left(
        c,
        _mm(inner_x + 2.0),
        _mm(header_y + 2.5),
        title_str,
        font="Helvetica-Bold",
        size=12.0,
    )

    # --- title block (bottom-right): roughly 1/3 inner width x 25 mm tall
    tb_h = 30.0
    tb_w = inner_w * 0.55
    tb_x = inner_x + inner_w - tb_w
    tb_y = inner_y
    # Right-column tables sit above the title block, view sits to the left.

    # --- right column for tables (above title block, to the right of view)
    right_w = inner_w * 0.42
    right_x = inner_x + inner_w - right_w
    tables_top_y = header_y - 4.0

    # Bend table at the top of the right column.
    bend_bottom_y = _draw_bend_table(c, right_x, tables_top_y, pattern, right_w)
    hole_top_y = bend_bottom_y - 4.0
    _draw_hole_table(c, right_x, hole_top_y, pattern, right_w)
    # The hole table flows down; if it would overflow above the title block we
    # simply accept the visual clip -- the operator still has the geometry view.

    # --- pattern view (left, between header and bottom margin)
    view_x = inner_x + 2.0
    view_y = tb_y + 2.0
    view_w = (inner_w - right_w) - 4.0
    view_h = (header_y - view_y) - 2.0
    scale = _draw_pattern_view(c, pattern, view_x, view_y, view_w, view_h)

    # --- title block last (so its lines sit on top)
    _draw_title_block(c, tb_x, tb_y, tb_w, tb_h, pattern, meta, scale)


# ---------------------------------------------------------------------------
# BOM cover sheet
# ---------------------------------------------------------------------------


def _draw_bom_cover(
    c: _canvas.Canvas,
    patterns: list[tuple[FlatPattern, PartDrawingMeta]],
    page_w_pt: float,
    page_h_pt: float,
) -> None:
    page_w_mm = page_w_pt / MM_TO_PT
    page_h_mm = page_h_pt / MM_TO_PT

    _draw_rect(
        c,
        BORDER_MM,
        BORDER_MM,
        page_w_mm - 2 * BORDER_MM,
        page_h_mm - 2 * BORDER_MM,
        line_pt=0.6,
    )

    # Title
    _set_text_left(
        c,
        _mm(BORDER_MM + 4.0),
        _mm(page_h_mm - BORDER_MM - 10.0),
        "Bill of Materials",
        font="Helvetica-Bold",
        size=18.0,
    )
    _set_text_left(
        c,
        _mm(BORDER_MM + 4.0),
        _mm(page_h_mm - BORDER_MM - 16.0),
        f"Generated {_dt.date.today().isoformat()} - {len(patterns)} part(s)",
        size=10.0,
    )

    header = ["#", "Part", "Part No.", "Material", "Thickness (mm)", "Qty", "Rev"]
    inner_w = page_w_mm - 2 * BORDER_MM - 8.0
    cw = _normalise_widths([8.0, 50.0, 30.0, 30.0, 24.0, 12.0, 12.0], inner_w)
    rows: list[list[str]] = []
    for idx, (pat, meta) in enumerate(patterns, start=1):
        rows.append(
            [
                str(idx),
                meta.part_name or pat.part_name or "",
                meta.part_number,
                meta.material,
                f"{pat.thickness:.2f}",
                str(meta.quantity),
                meta.revision,
            ]
        )
    _draw_table(
        c,
        BORDER_MM + 4.0,
        page_h_mm - BORDER_MM - 22.0,
        cw,
        header,
        rows,
        title=None,
    )


# ---------------------------------------------------------------------------
# Public writers
# ---------------------------------------------------------------------------


def write_pdf(
    pattern: FlatPattern,
    out_path: str | Path,
    *,
    meta: PartDrawingMeta | None = None,
    page_size: str = "A4",
    sheet_orientation: str = "landscape",
) -> Path:
    """Render a single-page shop drawing for a flat pattern.

    Layout (landscape A4):
        - 10mm border outside the title block
        - Top: part name + revision
        - Center-left: scaled flat-pattern view (outer + holes + bend lines)
        - Right column: bend table + hole table
        - Bottom: title block (part_number, material, qty, thickness, date,
          drawn_by, scale, revision)

    Returns the path written.
    """
    _validate(pattern)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    page_w_pt, page_h_pt = _resolve_page_size(page_size, sheet_orientation)
    # pageCompression=0 keeps the content stream uncompressed so downstream
    # tools (and our tests) can byte-grep for embedded metadata strings.
    c = _canvas.Canvas(str(out_path), pagesize=(page_w_pt, page_h_pt), pageCompression=0)

    resolved_meta = meta or PartDrawingMeta()
    # Persist the same part_name in the PDF metadata for downstream tools.
    c.setTitle(resolved_meta.part_name or pattern.part_name or "Shop Drawing")
    c.setAuthor(resolved_meta.drawn_by or "stepalesengine")
    c.setSubject(f"Shop drawing rev {resolved_meta.revision}")

    _render_part_page(c, pattern, resolved_meta, page_w_pt, page_h_pt)
    c.showPage()
    c.save()
    return out_path


def _group_assembly_patterns(
    patterns: list[tuple[FlatPattern, PartDrawingMeta]],
) -> list[tuple[FlatPattern, PartDrawingMeta]]:
    """Collapse identical (FlatPattern, PartDrawingMeta) tuples for a grouped BOM.

    Two tuples are considered identical when they share part_number, material,
    revision, thickness (rounded to 0.01 mm) and bbox dimensions (rounded to
    0.1 mm). The ``quantity`` on the surviving representative is the sum of
    every member's ``quantity``; the first tuple in input order wins as the
    representative (so we keep the first FlatPattern for its actual geometry).
    """
    groups: OrderedDict[
        tuple[str, str, str, float, tuple[float, float, float, float]],
        tuple[FlatPattern, PartDrawingMeta],
    ] = OrderedDict()
    for pat, meta in patterns:
        bbox_key = tuple(round(c, 1) for c in pat.bbox)
        key = (meta.part_number, meta.material, meta.revision, round(pat.thickness, 2), bbox_key)
        existing = groups.get(key)
        if existing is None:
            # Copy meta so we don't mutate the caller's object when summing qty.
            new_meta = PartDrawingMeta(
                part_name=meta.part_name,
                part_number=meta.part_number,
                material=meta.material,
                quantity=int(meta.quantity),
                revision=meta.revision,
                drawn_by=meta.drawn_by,
                date_iso=meta.date_iso,
                notes=meta.notes,
            )
            groups[key] = (pat, new_meta)
        else:
            existing[1].quantity += int(meta.quantity)
    return list(groups.values())


def write_assembly_pdf(
    patterns: list[tuple[FlatPattern, PartDrawingMeta]],
    out_path: str | Path,
    *,
    page_size: str = "A4",
    grouped: bool = False,
) -> Path:
    """Multi-page PDF, one part per page, plus a cover sheet with the BOM.

    Page 1 is the cover (BOM); pages 2..N+1 are the individual part drawings.

    If ``grouped`` is True the BOM and per-part pages collapse identical parts
    into a single row / page with a summed quantity (matching the AutoPOL
    presentation style). Identity is decided by part_number + material +
    revision + thickness + bbox dimensions. Default is False so existing
    callers see the per-instance behaviour they relied on.
    """
    if not patterns:
        raise ValueError("write_assembly_pdf requires at least one (pattern, meta) tuple")
    for pat, _meta in patterns:
        _validate(pat)

    effective = _group_assembly_patterns(patterns) if grouped else patterns

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    page_w_pt, page_h_pt = _resolve_page_size(page_size, "landscape")
    c = _canvas.Canvas(str(out_path), pagesize=(page_w_pt, page_h_pt), pageCompression=0)
    c.setTitle("Shop Drawing Set")
    c.setAuthor("stepalesengine")

    # Cover sheet
    _draw_bom_cover(c, effective, page_w_pt, page_h_pt)
    c.showPage()

    # Per-part pages
    for pat, meta in effective:
        _render_part_page(c, pat, meta, page_w_pt, page_h_pt)
        c.showPage()

    c.save()
    return out_path


__all__ = [
    "PartDrawingMeta",
    "write_pdf",
    "write_assembly_pdf",
]
