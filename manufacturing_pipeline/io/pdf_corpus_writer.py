"""Corpus-level PDF report writer.

Renders every part across a corpus into one multi-page, shop-floor printable
PDF. One page per part by default (``parts_per_page=1``), with a cover sheet
that summarises the run, the classification label distribution, and the top
anomalies (long durations, errors, zero-part files).

The per-part page contains:

    - title band: part name + product_id, classification label pill, confidence
    - mid-left: flat-DXF preview (embedded vector graphics by reading the
      ``flat_dxf_path`` with ``ezdxf`` -- no external image refs).
    - mid-right: 3-column feature summary (Property / Value / Unit)
    - bottom-left: hole table (count + grouped diameters)
    - bottom-right: bend table (n_bends, angles, radii, flat_area, t_mean)
    - footer: PMI summary line + CAM strategy summary line

The writer only touches data that lives on an :class:`AssemblyManifest` --
no pipeline state, no STEP files, no caches. That means the same code path
serves the CLI (which reloads ``manifest.xml`` from disk) and the tests
(which synthesise manifests in memory).

Pure-Python; depends on ``reportlab`` (PDF) + ``ezdxf`` (optional, only
needed when ``include_dxf_preview=True`` AND the manifest has a
``flat_dxf_path`` pointing at a real file).
"""

from __future__ import annotations

import datetime as _dt
import logging
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reportlab.lib.colors import Color, black, white
from reportlab.lib.pagesizes import A3, A4, landscape, letter, portrait
from reportlab.pdfgen import canvas as _canvas

if TYPE_CHECKING:
    from manufacturing_pipeline.io.xml_writer import AssemblyManifest, PartManifestEntry

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MM_TO_PT = 72.0 / 25.4
BORDER_MM = 8.0

_PAGE_SIZES = {
    "a4": A4,
    "a3": A3,
    "letter": letter,
}

# Label palette mirrors validate.py / web/server.py so the PDF, HTML, and
# Markdown reports stay visually consistent.
_LABEL_COLORS_HEX: dict[str, tuple[str, str]] = {
    "plaat": ("#d9e8ff", "#0b3a82"),
    "profiel": ("#d6f5d6", "#1f5e1f"),
    "anders": ("#fff4c5", "#6b5300"),
    "uncertain": ("#e5e5e5", "#555555"),
}
_DEFAULT_LABEL_COLOR_HEX = ("#e5e5e5", "#555555")

# DXF layer styling for preview rendering (mirrors web.dxf_to_svg).
_DXF_LAYER_STYLES: dict[str, tuple[str, tuple[float, float] | None]] = {
    "OUTER": ("#d62728", None),
    "INNER": ("#2ca02c", None),
    "BEND_UP": ("#1f77b4", (3.0, 2.0)),
    "BEND_DOWN": ("#ff7f0e", (3.0, 2.0)),
}


# ---------------------------------------------------------------------------
# Public options
# ---------------------------------------------------------------------------


@dataclass
class CorpusPDFOptions:
    """Knobs for :func:`write_corpus_pdf`."""

    page_size: str = "A4"  # "A4" | "Letter" | "A3"
    include_dxf_preview: bool = True
    include_pmi_summary: bool = True
    parts_per_page: int = 1  # 1 = full-detail, 2 = compact, 4 = thumbnail
    page_size_orientation: str = "portrait"


# ---------------------------------------------------------------------------
# Helpers (units, colour, layout)
# ---------------------------------------------------------------------------


def _mm(v: float) -> float:
    return v * MM_TO_PT


def _hex_to_color(hex_str: str) -> Color:
    s = hex_str.lstrip("#")
    if len(s) != 6:
        return black
    r = int(s[0:2], 16) / 255.0
    g = int(s[2:4], 16) / 255.0
    b = int(s[4:6], 16) / 255.0
    return Color(r, g, b)


def _label_palette(label: str) -> tuple[Color, Color]:
    bg_hex, fg_hex = _LABEL_COLORS_HEX.get(label, _DEFAULT_LABEL_COLOR_HEX)
    return _hex_to_color(bg_hex), _hex_to_color(fg_hex)


def _resolve_page_size(name: str, orientation: str) -> tuple[float, float]:
    key = (name or "A4").lower()
    if key not in _PAGE_SIZES:
        raise ValueError(f"unknown page_size {name!r}; expected one of {sorted(_PAGE_SIZES)}")
    base = _PAGE_SIZES[key]
    o = (orientation or "portrait").lower()
    if o == "landscape":
        return landscape(base)
    if o == "portrait":
        return portrait(base)
    raise ValueError(
        f"unknown page_size_orientation {orientation!r}; expected 'portrait' or 'landscape'"
    )


def _safe_get(obj: object, *names: str, default=None):
    """Pull the first non-None attribute from ``names`` off ``obj``."""
    for name in names:
        v = getattr(obj, name, None)
        if v is not None:
            return v
    return default


# ---------------------------------------------------------------------------
# Drawing primitives
# ---------------------------------------------------------------------------


def _set_text(
    c: _canvas.Canvas,
    x_pt: float,
    y_pt: float,
    text: str,
    *,
    font: str = "Helvetica",
    size: float = 8.0,
    color: Color = black,
) -> None:
    c.setFont(font, size)
    c.setFillColor(color)
    c.drawString(x_pt, y_pt, text)


def _draw_rect(
    c: _canvas.Canvas,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
    *,
    stroke: Color = black,
    fill: Color | None = None,
    line_pt: float = 0.4,
) -> None:
    c.setStrokeColor(stroke)
    c.setLineWidth(line_pt)
    if fill is None:
        c.rect(_mm(x_mm), _mm(y_mm), _mm(w_mm), _mm(h_mm), stroke=1, fill=0)
    else:
        c.setFillColor(fill)
        c.rect(_mm(x_mm), _mm(y_mm), _mm(w_mm), _mm(h_mm), stroke=1, fill=1)


def _label_pill(
    c: _canvas.Canvas,
    x_mm: float,
    y_mm: float,
    text: str,
    label: str,
    *,
    pad_x: float = 3.0,
    height: float = 5.5,
    font_size: float = 8.5,
) -> float:
    """Render a coloured rounded label pill. Returns the right edge x (mm)."""
    bg, fg = _label_palette(label)
    text_w_pt = c.stringWidth(text, "Helvetica-Bold", font_size)
    text_w_mm = text_w_pt / MM_TO_PT
    pill_w = text_w_mm + 2 * pad_x

    c.setFillColor(bg)
    c.setStrokeColor(bg)
    c.roundRect(
        _mm(x_mm),
        _mm(y_mm),
        _mm(pill_w),
        _mm(height),
        _mm(1.2),
        stroke=1,
        fill=1,
    )
    _set_text(
        c,
        _mm(x_mm + pad_x),
        _mm(y_mm + 1.5),
        text,
        font="Helvetica-Bold",
        size=font_size,
        color=fg,
    )
    return x_mm + pill_w


def _normalise_widths(target: list[float], total: float) -> list[float]:
    s = sum(target)
    if s <= 0:
        return target
    factor = total / s
    return [w * factor for w in target]


def _draw_table(
    c: _canvas.Canvas,
    x_mm: float,
    y_top_mm: float,
    col_widths_mm: list[float],
    header: list[str],
    rows: list[list[str]],
    *,
    row_h_mm: float = 4.5,
    title: str | None = None,
    title_size: float = 9.0,
    body_size: float = 7.0,
) -> float:
    """Draw a header + grid table starting at top-left, growing downward.

    Returns the y_mm of the bottom edge.
    """
    table_w = sum(col_widths_mm)
    y = y_top_mm

    if title:
        _set_text(
            c,
            _mm(x_mm),
            _mm(y - 3.0),
            title,
            font="Helvetica-Bold",
            size=title_size,
        )
        y -= 4.5

    c.setStrokeColor(black)
    c.setLineWidth(0.35)

    # Header band: lightly tinted background for readability.
    c.setFillColor(_hex_to_color("#eef0f3"))
    c.rect(
        _mm(x_mm),
        _mm(y - row_h_mm),
        _mm(table_w),
        _mm(row_h_mm),
        stroke=1,
        fill=1,
    )
    cx = x_mm
    for label, w in zip(header, col_widths_mm):
        _set_text(
            c,
            _mm(cx + 0.8),
            _mm(y - row_h_mm + 1.2),
            label,
            font="Helvetica-Bold",
            size=body_size,
        )
        cx += w
        if cx < x_mm + table_w - 1e-6:
            c.line(_mm(cx), _mm(y - row_h_mm), _mm(cx), _mm(y))
    y -= row_h_mm

    for row in rows:
        c.setFillColor(white)
        c.rect(
            _mm(x_mm),
            _mm(y - row_h_mm),
            _mm(table_w),
            _mm(row_h_mm),
            stroke=1,
            fill=0,
        )
        cx = x_mm
        for value, w in zip(row, col_widths_mm):
            _set_text(
                c,
                _mm(cx + 0.8),
                _mm(y - row_h_mm + 1.2),
                str(value),
                size=body_size,
            )
            cx += w
            if cx < x_mm + table_w - 1e-6:
                c.line(_mm(cx), _mm(y - row_h_mm), _mm(cx), _mm(y))
        y -= row_h_mm

    return y


# ---------------------------------------------------------------------------
# DXF preview rendering (embedded vector graphics)
# ---------------------------------------------------------------------------


def _read_dxf_polylines(
    dxf_path: Path,
) -> tuple[
    list[tuple[str, list[tuple[float, float]], bool]], list[tuple[str, list[tuple[float, float]]]]
]:
    """Read a DXF flat-pattern into closed polylines + open line segments.

    Returns a tuple ``(closed_polys, open_polys)`` where each closed entry is
    ``(layer, points, closed_flag)`` and each open entry is ``(layer, points)``.
    Filters to the same OUTER/INNER/BEND_UP/BEND_DOWN layers the SVG viewer
    uses, so the rendered preview matches what the user sees in the web app.
    """
    try:
        import ezdxf  # local import: keeps the module importable when ezdxf absent
    except Exception:  # pragma: no cover - safety net
        return [], []

    try:
        doc = ezdxf.readfile(str(dxf_path))
    except Exception:
        return [], []

    msp = doc.modelspace()
    closed_polys: list[tuple[str, list[tuple[float, float]], bool]] = []
    open_polys: list[tuple[str, list[tuple[float, float]]]] = []

    renderable = set(_DXF_LAYER_STYLES.keys())
    for entity in msp.query("LWPOLYLINE LINE"):
        entity_any: Any = entity
        layer = entity_any.dxf.layer
        if layer not in renderable:
            continue
        if entity_any.dxftype() == "LWPOLYLINE":
            try:
                pts = [(float(x), float(y)) for (x, y, *_rest) in entity_any.get_points("xy")]
            except Exception:
                continue
            if not pts:
                continue
            closed_polys.append((layer, pts, bool(entity_any.closed)))
        else:  # LINE
            try:
                start = entity.dxf.start
                end = entity.dxf.end
                pts = [
                    (float(start[0]), float(start[1])),
                    (float(end[0]), float(end[1])),
                ]
            except Exception:
                continue
            open_polys.append((layer, pts))

    return closed_polys, open_polys


def _polyline_bbox(
    closed_polys: list[tuple[str, list[tuple[float, float]], bool]],
    open_polys: list[tuple[str, list[tuple[float, float]]]],
) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for _, pts, _c in closed_polys:
        for x, y in pts:
            xs.append(x)
            ys.append(y)
    for _, pts in open_polys:
        for x, y in pts:
            xs.append(x)
            ys.append(y)
    if not xs:
        return (0.0, 0.0, 1.0, 1.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _draw_dxf_preview(
    c: _canvas.Canvas,
    dxf_path: Path,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
) -> bool:
    """Render the DXF flat pattern into the given mm box. Returns True on success."""
    closed_polys, open_polys = _read_dxf_polylines(dxf_path)
    if not closed_polys and not open_polys:
        return False

    xmin, ymin, xmax, ymax = _polyline_bbox(closed_polys, open_polys)
    part_w = max(xmax - xmin, 1e-9)
    part_h = max(ymax - ymin, 1e-9)

    pad_mm = 3.0
    avail_w = max(w_mm - 2 * pad_mm, 1.0)
    avail_h = max(h_mm - 2 * pad_mm, 1.0)
    scale = min(avail_w / part_w, avail_h / part_h)

    drawn_w = part_w * scale
    drawn_h = part_h * scale
    origin_x = x_mm + (w_mm - drawn_w) / 2.0
    origin_y = y_mm + (h_mm - drawn_h) / 2.0

    def to_pt(p: tuple[float, float]) -> tuple[float, float]:
        return (
            _mm(origin_x + (p[0] - xmin) * scale),
            _mm(origin_y + (p[1] - ymin) * scale),
        )

    # Closed polys (OUTER + INNER, also BEND_* when present as closed)
    for layer, pts, closed in closed_polys:
        stroke_hex, dash = _DXF_LAYER_STYLES.get(layer, ("#666666", None))
        c.setStrokeColor(_hex_to_color(stroke_hex))
        c.setLineWidth(0.4 if layer == "OUTER" else 0.3)
        if dash is not None:
            c.setDash(*dash)
        else:
            c.setDash()
        path = c.beginPath()
        first = to_pt(pts[0])
        path.moveTo(*first)
        for p in pts[1:]:
            path.lineTo(*to_pt(p))
        if closed:
            path.close()
        c.drawPath(path, stroke=1, fill=0)
    c.setDash()

    # Open line segments (bend lines emitted as LINE)
    for layer, pts in open_polys:
        stroke_hex, dash = _DXF_LAYER_STYLES.get(layer, ("#666666", None))
        c.setStrokeColor(_hex_to_color(stroke_hex))
        c.setLineWidth(0.25)
        if dash is not None:
            c.setDash(*dash)
        else:
            c.setDash()
        a = to_pt(pts[0])
        b = to_pt(pts[1])
        c.line(a[0], a[1], b[0], b[1])
    c.setDash()
    c.setStrokeColor(black)
    return True


def _draw_dxf_placeholder(
    c: _canvas.Canvas,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
    note: str,
) -> None:
    _draw_rect(c, x_mm, y_mm, w_mm, h_mm, line_pt=0.3)
    # Diagonal slash to clearly mark "no preview".
    c.setStrokeColor(_hex_to_color("#cccccc"))
    c.setLineWidth(0.3)
    c.line(_mm(x_mm), _mm(y_mm), _mm(x_mm + w_mm), _mm(y_mm + h_mm))
    c.line(_mm(x_mm + w_mm), _mm(y_mm), _mm(x_mm), _mm(y_mm + h_mm))
    c.setStrokeColor(black)
    _set_text(
        c,
        _mm(x_mm + 3.0),
        _mm(y_mm + h_mm / 2.0),
        note,
        font="Helvetica-Oblique",
        size=8.0,
        color=_hex_to_color("#555555"),
    )


# ---------------------------------------------------------------------------
# Per-part section builders
# ---------------------------------------------------------------------------


def _feature_rows(entry: PartManifestEntry) -> list[list[str]]:
    """Build the ``Property / Value / Unit`` table rows for one part."""
    rows: list[list[str]] = []
    f = entry.features
    u = entry.unfold
    h = entry.holes
    pm = entry.profile_match

    def add(prop: str, val: str, unit: str) -> None:
        rows.append([prop, val, unit])

    if f is not None:
        add("volume", f"{f.volume:.1f}", "mm^3")
        add("surface_area", f"{f.surface_area:.1f}", "mm^2")
        bb = f.bbox_dims_sorted or (0.0, 0.0, 0.0)
        add("bbox L x W x T", f"{bb[0]:.1f} x {bb[1]:.1f} x {bb[2]:.1f}", "mm")
        add("thickness", f"{bb[2]:.2f}", "mm")
        add("hole_count", str(int(f.hole_count)), "")
        add("pocket_complexity", f"{getattr(f, 'pocket_complexity', 0.0):.2f}", "0..1")
        hull_concavity = max(0.0, 1.0 - float(getattr(f, "convex_hull_volume_ratio", 1.0) or 0.0))
        add("hull_concavity", f"{hull_concavity:.2f}", "0..1")
    else:
        add("features", "-", "")

    if u is not None:
        add("n_bends", str(int(u.n_bends)), "")
    elif f is None:
        add("n_bends", "-", "")

    if pm is not None and pm.designation:
        add("profile_designation", str(pm.designation), "")

    if h is not None and h.hole_count and (f is None or f.hole_count == 0):
        # When features didn't carry hole_count, fall back to HolePattern.
        add("hole_count", str(int(h.hole_count)), "")

    return rows


def _hole_groups(diameters: list[float]) -> list[tuple[float, int]]:
    """Group diameters by 0.1 mm bins, preserving first-seen order."""
    bins: OrderedDict[float, int] = OrderedDict()
    for d in diameters:
        key = round(float(d), 1)
        bins[key] = bins.get(key, 0) + 1
    return list(bins.items())


def _hole_table_rows(entry: PartManifestEntry) -> tuple[list[str], list[list[str]]]:
    header = ["#", "Diameter (mm)", "Qty"]
    diameters: list[float] = []
    if entry.holes is not None and entry.holes.diameters:
        diameters = list(entry.holes.diameters)
    elif entry.features is not None and getattr(entry.features, "hole_diameters", None):
        diameters = list(entry.features.hole_diameters)
    if not diameters:
        return header, [["-", "-", "-"]]
    rows: list[list[str]] = []
    for idx, (dia, qty) in enumerate(_hole_groups(diameters), start=0):
        rows.append([str(idx), f"{dia:.1f}", str(qty)])
    return header, rows


def _bend_table_rows(entry: PartManifestEntry) -> tuple[list[str], list[list[str]]]:
    header = ["Property", "Value", "Unit"]
    u = entry.unfold
    if u is None:
        return header, [["bends", "-", ""]]
    bbox_str = (
        f"{u.blank_bbox[0]:.1f} x {u.blank_bbox[1]:.1f}"
        if u.blank_bbox is not None and len(u.blank_bbox) >= 2
        else "-"
    )
    rows = [
        ["n_bends", str(int(u.n_bends)), ""],
        ["flat_area", f"{u.flat_area:.1f}", "mm^2"],
        ["blank_bbox", bbox_str, "mm"],
        ["thickness_mean", f"{u.thickness_mean:.2f}", "mm"],
        ["thickness_cv", f"{u.thickness_cv:.3f}", ""],
        ["status", u.status.value, ""],
    ]
    return header, rows


def _pmi_summary_line(entry: PartManifestEntry) -> str:
    p = entry.pmi
    if p is None:
        return "PMI: none"
    n_tols = len(p.tolerances)
    n_datums = len(p.datums)
    return (
        f"PMI: annotations={p.n_annotations} tolerances={n_tols} datums={n_datums} "
        f"semantic={'yes' if p.has_semantic else 'no'}"
    )


def _strategy_summary_line(entry: PartManifestEntry) -> str:
    s = entry.strategy
    if s is None:
        return "CAM: not planned"
    n_ops = len(s.operations)
    return (
        f"CAM: process={s.primary_process} setups={s.setup_count} ops={n_ops} "
        f"est={s.estimated_time_min:.1f} min"
    )


# ---------------------------------------------------------------------------
# Per-part page (1-up)
# ---------------------------------------------------------------------------


def _render_part_page_full(
    c: _canvas.Canvas,
    entry: PartManifestEntry,
    manifest: AssemblyManifest,
    options: CorpusPDFOptions,
    page_w_pt: float,
    page_h_pt: float,
) -> None:
    page_w_mm = page_w_pt / MM_TO_PT
    page_h_mm = page_h_pt / MM_TO_PT

    inner_x = BORDER_MM
    inner_y = BORDER_MM
    inner_w = page_w_mm - 2 * BORDER_MM
    inner_h = page_h_mm - 2 * BORDER_MM

    _draw_rect(c, inner_x, inner_y, inner_w, inner_h, line_pt=0.5)

    # --- header band ----------------------------------------------------
    header_h = 14.0
    header_y = inner_y + inner_h - header_h
    c.setStrokeColor(_hex_to_color("#cccccc"))
    c.setLineWidth(0.3)
    c.line(
        _mm(inner_x),
        _mm(header_y),
        _mm(inner_x + inner_w),
        _mm(header_y),
    )
    c.setStrokeColor(black)

    part = entry.part
    title_text = (part.name or part.product_id or "PART").strip()
    _set_text(
        c,
        _mm(inner_x + 3.0),
        _mm(header_y + 6.5),
        title_text,
        font="Helvetica-Bold",
        size=13.5,
    )
    sub = f"product_id: {part.product_id or '-'}"
    _set_text(
        c,
        _mm(inner_x + 3.0),
        _mm(header_y + 1.8),
        sub,
        size=8.5,
        color=_hex_to_color("#555555"),
    )

    cls = entry.classification
    pill_text = f"{cls.label}"
    pill_x = inner_x + inner_w - 70.0
    right_of_pill = _label_pill(c, pill_x, header_y + 4.5, pill_text, cls.label)
    conf_text = f"{cls.confidence * 100:.1f}% confidence"
    _set_text(
        c,
        _mm(right_of_pill + 2.0),
        _mm(header_y + 6.0),
        conf_text,
        size=8.5,
        color=_hex_to_color("#444444"),
    )

    # --- body layout ----------------------------------------------------
    body_top = header_y - 2.0
    footer_h = 10.0
    footer_top = inner_y + footer_h
    body_h = body_top - footer_top - 2.0

    mid_h = body_h * 0.62
    bottom_h = body_h - mid_h - 2.0

    mid_y = footer_top + bottom_h + 2.0
    mid_x_left = inner_x + 3.0
    mid_w_left = inner_w * 0.58
    mid_x_right = mid_x_left + mid_w_left + 3.0
    mid_w_right = inner_w - mid_w_left - 9.0

    # DXF preview (mid-left).
    dxf_path_str = entry.flat_dxf_path or ""
    dxf_drawn = False
    if options.include_dxf_preview and dxf_path_str:
        candidate = Path(dxf_path_str)
        if candidate.is_file():
            try:
                dxf_drawn = _draw_dxf_preview(c, candidate, mid_x_left, mid_y, mid_w_left, mid_h)
            except Exception:  # noqa: BLE001 - never crash the whole report
                _logger.exception("dxf preview failed for %s", candidate)
                dxf_drawn = False
    if not dxf_drawn:
        note = "as-modelled (no flat DXF)" if not dxf_path_str else "DXF unavailable"
        _draw_dxf_placeholder(c, mid_x_left, mid_y, mid_w_left, mid_h, note)
    else:
        _draw_rect(c, mid_x_left, mid_y, mid_w_left, mid_h, line_pt=0.3)
    _set_text(
        c,
        _mm(mid_x_left + 1.0),
        _mm(mid_y + mid_h - 4.0),
        "Flat-DXF preview",
        font="Helvetica-Bold",
        size=8.5,
    )

    # Feature summary (mid-right).
    feat_header = ["Property", "Value", "Unit"]
    feat_rows = _feature_rows(entry)
    feat_cw = _normalise_widths([30.0, 32.0, 14.0], mid_w_right)
    _draw_table(
        c,
        mid_x_right,
        mid_y + mid_h - 1.0,
        feat_cw,
        feat_header,
        feat_rows,
        title="Feature summary",
        row_h_mm=4.2,
    )

    # Hole table (bottom-left), bend table (bottom-right).
    bottom_y = mid_y - 2.0
    bot_x_left = inner_x + 3.0
    bot_w_left = inner_w * 0.46
    bot_x_right = bot_x_left + bot_w_left + 3.0
    bot_w_right = inner_w - bot_w_left - 9.0

    hole_header, hole_rows = _hole_table_rows(entry)
    hole_cw = _normalise_widths([8.0, 28.0, 12.0], bot_w_left)
    _draw_table(
        c,
        bot_x_left,
        bottom_y,
        hole_cw,
        hole_header,
        hole_rows,
        title="Hole table",
        row_h_mm=4.2,
    )

    bend_header, bend_rows = _bend_table_rows(entry)
    bend_cw = _normalise_widths([22.0, 24.0, 12.0], bot_w_right)
    _draw_table(
        c,
        bot_x_right,
        bottom_y,
        bend_cw,
        bend_header,
        bend_rows,
        title="Bend table",
        row_h_mm=4.2,
    )

    # --- footer summary lines ------------------------------------------
    footer_y = inner_y + 2.0
    if options.include_pmi_summary:
        _set_text(
            c,
            _mm(inner_x + 3.0),
            _mm(footer_y + 4.5),
            _pmi_summary_line(entry),
            size=7.5,
            color=_hex_to_color("#333333"),
        )
    _set_text(
        c,
        _mm(inner_x + 3.0),
        _mm(footer_y),
        _strategy_summary_line(entry),
        size=7.5,
        color=_hex_to_color("#333333"),
    )

    # Source label (manifest path) on the right edge of the footer.
    source = Path(manifest.source_path).name if manifest.source_path else ""
    if source:
        _set_text(
            c,
            _mm(inner_x + inner_w - 80.0),
            _mm(footer_y),
            f"source: {source}",
            size=7.0,
            color=_hex_to_color("#666666"),
        )


# ---------------------------------------------------------------------------
# Per-part compact (2-up / 4-up) renderer
# ---------------------------------------------------------------------------


def _render_part_card(
    c: _canvas.Canvas,
    entry: PartManifestEntry,
    manifest: AssemblyManifest,
    options: CorpusPDFOptions,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
) -> None:
    """Compact per-part card used for 2-up / 4-up layouts."""
    _draw_rect(c, x_mm, y_mm, w_mm, h_mm, line_pt=0.4)

    part = entry.part
    cls = entry.classification

    # Title bar.
    title_h = 9.0
    title_y = y_mm + h_mm - title_h
    c.setStrokeColor(_hex_to_color("#cccccc"))
    c.setLineWidth(0.3)
    c.line(_mm(x_mm), _mm(title_y), _mm(x_mm + w_mm), _mm(title_y))
    c.setStrokeColor(black)

    _set_text(
        c,
        _mm(x_mm + 2.0),
        _mm(title_y + 4.0),
        (part.name or part.product_id or "PART").strip()[:40],
        font="Helvetica-Bold",
        size=9.5,
    )
    _set_text(
        c,
        _mm(x_mm + 2.0),
        _mm(title_y + 0.8),
        f"{part.product_id or '-'}",
        size=7.0,
        color=_hex_to_color("#555555"),
    )
    _label_pill(
        c,
        x_mm + w_mm - 32.0,
        title_y + 2.0,
        cls.label,
        cls.label,
        height=5.0,
        font_size=7.5,
    )

    # DXF preview occupies the left half; right half holds key numbers.
    preview_w = w_mm * 0.55
    preview_h = h_mm - title_h - 14.0
    preview_x = x_mm + 1.5
    preview_y = y_mm + 12.0

    dxf_drawn = False
    if options.include_dxf_preview and entry.flat_dxf_path:
        candidate = Path(entry.flat_dxf_path)
        if candidate.is_file():
            try:
                dxf_drawn = _draw_dxf_preview(
                    c, candidate, preview_x, preview_y, preview_w, preview_h
                )
            except Exception:  # noqa: BLE001
                dxf_drawn = False
    if not dxf_drawn:
        _draw_dxf_placeholder(c, preview_x, preview_y, preview_w, preview_h, "no preview")
    else:
        _draw_rect(c, preview_x, preview_y, preview_w, preview_h, line_pt=0.2)

    # Right column: compact stats.
    stats_x = preview_x + preview_w + 2.0
    stats_y_top = title_y - 2.0
    f = entry.features
    u = entry.unfold
    items: list[tuple[str, str]] = []
    if f is not None:
        bb = f.bbox_dims_sorted or (0.0, 0.0, 0.0)
        items.append(("bbox", f"{bb[0]:.0f}x{bb[1]:.0f}x{bb[2]:.1f}"))
        items.append(("holes", str(int(f.hole_count))))
    if u is not None:
        items.append(("bends", str(int(u.n_bends))))
    items.append(("conf", f"{cls.confidence * 100:.1f}%"))

    yy = stats_y_top
    for k, v in items:
        _set_text(c, _mm(stats_x), _mm(yy - 3.5), k, size=7.0, color=_hex_to_color("#666666"))
        _set_text(c, _mm(stats_x), _mm(yy - 7.5), v, font="Helvetica-Bold", size=9.0)
        yy -= 9.0

    # Footer summary lines along the bottom of the card.
    _set_text(
        c,
        _mm(x_mm + 2.0),
        _mm(y_mm + 5.5),
        _pmi_summary_line(entry),
        size=6.5,
        color=_hex_to_color("#333333"),
    )
    _set_text(
        c,
        _mm(x_mm + 2.0),
        _mm(y_mm + 1.5),
        _strategy_summary_line(entry),
        size=6.5,
        color=_hex_to_color("#333333"),
    )


# ---------------------------------------------------------------------------
# Cover page
# ---------------------------------------------------------------------------


def _label_distribution_for_manifests(
    manifests: list[AssemblyManifest],
) -> dict[str, int]:
    dist: dict[str, int] = {}
    for m in manifests:
        for entry in m.parts:
            lbl = entry.classification.label or "uncertain"
            dist[lbl] = dist.get(lbl, 0) + 1
    return dist


def _collect_anomalies(manifests: list[AssemblyManifest]) -> list[str]:
    """Best-effort anomaly list when only manifests are available.

    Flags: zero-part assemblies; assemblies with any uncertain parts.
    The richer per-corpus anomaly list (durations, pipeline errors) lives in
    :class:`CorpusReport`; we surface the manifest-derived subset here so the
    PDF cover stays useful when called outside the CLI.
    """
    out: list[str] = []
    for m in manifests:
        src = Path(m.source_path).name if m.source_path else "<unknown>"
        if not m.parts:
            out.append(f"{src}: zero parts")
            continue
        labels = [e.classification.label for e in m.parts]
        n_uncertain = sum(1 for lbl in labels if lbl == "uncertain")
        if n_uncertain == len(labels) and len(labels) > 0:
            out.append(f"{src}: all {n_uncertain} parts uncertain")
        elif n_uncertain > 0:
            out.append(f"{src}: {n_uncertain}/{len(labels)} uncertain parts")
    return out[:10]


def _draw_distribution_bars(
    c: _canvas.Canvas,
    x_mm: float,
    y_top_mm: float,
    w_mm: float,
    distribution: dict[str, int],
) -> float:
    """Horizontal bars for label distribution. Returns the bottom y_mm."""
    total = sum(distribution.values())
    if total <= 0:
        _set_text(
            c,
            _mm(x_mm),
            _mm(y_top_mm - 4.0),
            "no parts classified",
            size=9.0,
            color=_hex_to_color("#666666"),
        )
        return y_top_mm - 6.0

    bar_h = 6.0
    gap = 3.0
    label_col = 22.0
    count_col = 18.0
    bar_w = max(w_mm - label_col - count_col - 4.0, 10.0)

    y = y_top_mm
    sorted_items = sorted(distribution.items(), key=lambda kv: -kv[1])
    for label, count in sorted_items:
        bg, fg = _label_palette(label)
        pct = count / total
        # Label cell.
        _set_text(
            c,
            _mm(x_mm),
            _mm(y - bar_h + 1.6),
            label,
            font="Helvetica-Bold",
            size=8.5,
            color=fg,
        )
        # Background rail.
        c.setFillColor(_hex_to_color("#eef0f3"))
        c.setStrokeColor(_hex_to_color("#dcdfe3"))
        c.setLineWidth(0.2)
        c.rect(_mm(x_mm + label_col), _mm(y - bar_h), _mm(bar_w), _mm(bar_h), stroke=1, fill=1)
        # Filled bar.
        c.setFillColor(bg)
        c.setStrokeColor(bg)
        c.rect(
            _mm(x_mm + label_col), _mm(y - bar_h), _mm(bar_w * pct), _mm(bar_h), stroke=1, fill=1
        )
        # Count + percent.
        _set_text(
            c,
            _mm(x_mm + label_col + bar_w + 2.0),
            _mm(y - bar_h + 1.6),
            f"{count}  {pct * 100:.1f}%",
            size=8.0,
            color=_hex_to_color("#333333"),
        )
        y -= bar_h + gap
    return y


def _render_cover_page(
    c: _canvas.Canvas,
    manifests: list[AssemblyManifest],
    page_w_pt: float,
    page_h_pt: float,
    *,
    total_parts: int,
    label_distribution: dict[str, int],
    anomalies: list[str],
    extra_summary: dict | None = None,
) -> None:
    page_w_mm = page_w_pt / MM_TO_PT
    page_h_mm = page_h_pt / MM_TO_PT

    inner_x = BORDER_MM
    inner_y = BORDER_MM
    inner_w = page_w_mm - 2 * BORDER_MM
    inner_h = page_h_mm - 2 * BORDER_MM

    _draw_rect(c, inner_x, inner_y, inner_w, inner_h, line_pt=0.5)

    # Title.
    title_y = inner_y + inner_h - 18.0
    _set_text(
        c,
        _mm(inner_x + 4.0),
        _mm(title_y),
        f"{total_parts} parts processed by stepalesengine",
        font="Helvetica-Bold",
        size=18.0,
    )
    sub_y = title_y - 6.0
    date_str = _dt.date.today().isoformat()
    extra_summary = extra_summary or {}
    wall = extra_summary.get("wall_s")
    mean = extra_summary.get("mean_per_file_s")
    n_files = extra_summary.get("n_files", len(manifests))
    sub_text = f"Generated {date_str}  -  files: {n_files}  -  parts: {total_parts}"
    if wall is not None:
        sub_text += f"  -  wall: {wall:.1f}s"
    if mean is not None:
        sub_text += f"  -  mean/file: {mean:.2f}s"
    _set_text(
        c,
        _mm(inner_x + 4.0),
        _mm(sub_y),
        sub_text,
        size=10.0,
        color=_hex_to_color("#444444"),
    )

    # Label distribution chart.
    section_y = sub_y - 12.0
    _set_text(
        c,
        _mm(inner_x + 4.0),
        _mm(section_y),
        "LABEL DISTRIBUTION",
        font="Helvetica-Bold",
        size=10.0,
        color=_hex_to_color("#555555"),
    )
    bars_bottom = _draw_distribution_bars(
        c, inner_x + 4.0, section_y - 4.0, inner_w - 8.0, label_distribution
    )

    # Top anomalies.
    anomalies_top = bars_bottom - 10.0
    _set_text(
        c,
        _mm(inner_x + 4.0),
        _mm(anomalies_top),
        f"TOP ANOMALIES ({len(anomalies)})",
        font="Helvetica-Bold",
        size=10.0,
        color=_hex_to_color("#555555"),
    )
    yy = anomalies_top - 5.0
    if not anomalies:
        _set_text(
            c,
            _mm(inner_x + 4.0),
            _mm(yy),
            "no anomalies detected",
            size=9.0,
            color=_hex_to_color("#1f5e1f"),
        )
        yy -= 5.0
    else:
        for line in anomalies[:10]:
            _set_text(
                c,
                _mm(inner_x + 4.0),
                _mm(yy),
                "- " + line[:120],
                size=8.5,
                color=_hex_to_color("#821818"),
            )
            yy -= 4.5

    # Footnote.
    _set_text(
        c,
        _mm(inner_x + 4.0),
        _mm(inner_y + 3.0),
        "generated by stepalesengine - one page per part follows",
        size=7.5,
        color=_hex_to_color("#777777"),
    )


# ---------------------------------------------------------------------------
# Public writer
# ---------------------------------------------------------------------------


def write_corpus_pdf(
    manifests: list[AssemblyManifest],
    out_path: Path,
    options: CorpusPDFOptions | None = None,
    *,
    extra_summary: dict | None = None,
    anomalies: list[str] | None = None,
) -> Path:
    """Render every part across the corpus into one PDF.

    Layout
    ------
        - Page 1 (cover): "{N} parts processed by stepalesengine", date,
          wall time, mean per file, horizontal-bar label distribution,
          top 10 anomalies.
        - Pages 2..M (per-part): title + classification pill + confidence,
          flat-DXF preview (or placeholder), feature summary table, hole
          table, bend table, PMI summary line, CAM strategy summary line.

    Parameters
    ----------
    manifests:
        Already-parsed :class:`AssemblyManifest` records. The writer never
        touches the STEP files or the cache; everything it draws lives on
        the manifest.
    out_path:
        Output PDF path. Parent directory is created if missing.
    options:
        Optional rendering knobs. Defaults to ``CorpusPDFOptions()``.
    extra_summary:
        Optional dict carrying ``wall_s``, ``mean_per_file_s``, ``n_files``
        for the cover page. The CLI feeds this from :class:`CorpusReport`;
        callers that don't have those numbers may pass ``None``.
    anomalies:
        Optional list of human-readable anomaly lines (top 10 shown on the
        cover). When ``None`` the writer derives a best-effort list from
        the manifests themselves.

    Returns
    -------
    Path
        The resolved output path.
    """
    options = options or CorpusPDFOptions()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    page_w_pt, page_h_pt = _resolve_page_size(options.page_size, options.page_size_orientation)
    c = _canvas.Canvas(str(out_path), pagesize=(page_w_pt, page_h_pt), pageCompression=0)
    c.setTitle("stepalesengine corpus report")
    c.setAuthor("stepalesengine")

    total_parts = sum(len(m.parts) for m in manifests)
    distribution = _label_distribution_for_manifests(manifests)
    anomaly_lines = anomalies if anomalies is not None else _collect_anomalies(manifests)

    # --- cover ---------------------------------------------------------
    _render_cover_page(
        c,
        manifests,
        page_w_pt,
        page_h_pt,
        total_parts=total_parts,
        label_distribution=distribution,
        anomalies=anomaly_lines,
        extra_summary=extra_summary,
    )
    c.showPage()

    # --- per-part pages ------------------------------------------------
    ppp = max(1, int(options.parts_per_page or 1))
    if ppp == 1:
        for manifest in manifests:
            for entry in manifest.parts:
                _render_part_page_full(c, entry, manifest, options, page_w_pt, page_h_pt)
                c.showPage()
    else:
        # Compact grid: 2 = one column two rows; 4 = 2x2 grid.
        page_w_mm = page_w_pt / MM_TO_PT
        page_h_mm = page_h_pt / MM_TO_PT
        inner_w = page_w_mm - 2 * BORDER_MM
        inner_h = page_h_mm - 2 * BORDER_MM

        if ppp == 2:
            cols, rows = 1, 2
        else:
            cols, rows = 2, 2
        cell_w = inner_w / cols
        cell_h = inner_h / rows

        cells_per_page = cols * rows
        idx_in_page = 0
        flat_entries: list[tuple[AssemblyManifest, PartManifestEntry]] = [
            (m, e) for m in manifests for e in m.parts
        ]
        for m, entry in flat_entries:
            col = idx_in_page % cols
            row = idx_in_page // cols
            x_mm = BORDER_MM + col * cell_w
            y_mm = BORDER_MM + inner_h - (row + 1) * cell_h
            _render_part_card(
                c,
                entry,
                m,
                options,
                x_mm + 1.5,
                y_mm + 1.5,
                cell_w - 3.0,
                cell_h - 3.0,
            )
            idx_in_page += 1
            if idx_in_page >= cells_per_page:
                c.showPage()
                idx_in_page = 0
        if idx_in_page > 0:
            c.showPage()

    c.save()
    return out_path


__all__ = ["CorpusPDFOptions", "write_corpus_pdf"]
