"""Render the original 3D STEP solid for a part as an SVG technical drawing.

Uses OCCT's HLR (Hidden Line Removal) projector to produce three views
(top, front, isometric) with visible edges in solid black and hidden edges
in dashed grey, matching technical-drawing convention.

The renderer is cached: loading and matching solids is expensive, so we keep
the loaded solids keyed on the source STEP path's (mtime_ns, size).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_VIEW_DIRS = {
    "top": ((0.0, 0.0, -1.0), (1.0, 0.0, 0.0)),  # look down -Z, X right
    "front": ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0)),  # look along -Y
    "side": ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),  # look along -X
    "iso": ((1.0, 1.0, 1.0), (1.0, -1.0, 0.0)),  # SW isometric
}

_OUTLINE_COLOR = "#1a1a1a"
_HIDDEN_COLOR = "#999999"
_BG_COLOR = "#ffffff"


@dataclass
class _LoadedAssembly:
    """Cached source STEP -> per-part solid mapping."""

    source_path: Path
    mtime_ns: int
    size: int
    solids: list  # list of TopoDS_Solid
    part_index: dict[str, int]  # product_id -> solid index (best-effort)


_cache: dict[str, _LoadedAssembly] = {}


def _key(path: Path) -> str:
    st = path.stat()
    return f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}"


def _load_assembly(source_path: Path) -> _LoadedAssembly:
    """Load and cache the source STEP's solids, building a product_id -> solid map."""
    key = _key(source_path)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    from manufacturing_pipeline.assembly.graph import build_assembly_graph, iter_leaves
    from manufacturing_pipeline.assembly.matcher import match_parts_to_solids
    from manufacturing_pipeline.geometry.geometry_loader import load_solids
    from manufacturing_pipeline.parsing.step_parser import parse_step

    parts = parse_step(source_path)
    solids = load_solids(source_path)
    root = build_assembly_graph(parts)
    leaves = list(iter_leaves(root))
    matches = match_parts_to_solids(leaves, solids, step_path=source_path)

    # Build product_id -> solid index. Solids are stored as a list; the index
    # is the position in the list.
    part_index: dict[str, int] = {}
    solid_list: list = []
    for m in matches:
        if m.solid is None or m.node is None:
            continue
        idx = len(solid_list)
        solid_list.append(m.solid)
        part_index[m.node.product_id] = idx
        # Also index by stripped/lowered name for convenience.
        if m.node.name:
            part_index.setdefault(m.node.name.lower(), idx)

    st = source_path.stat()
    loaded = _LoadedAssembly(
        source_path=source_path,
        mtime_ns=st.st_mtime_ns,
        size=st.st_size,
        solids=solid_list,
        part_index=part_index,
    )
    _cache[key] = loaded
    return loaded


def find_solid_for_part(source_path: Path, product_id: str):
    """Return the TopoDS_Solid for the given product_id, or None."""
    try:
        loaded = _load_assembly(source_path)
    except Exception as exc:
        logger.warning("could not load %s: %s", source_path, exc)
        return None
    idx = loaded.part_index.get(product_id)
    if idx is None:
        idx = loaded.part_index.get(product_id.lower())
    if idx is None:
        return None
    return loaded.solids[idx]


def _project_shape(shape, view: str) -> tuple[list, list, tuple[float, float, float, float]]:
    """Run HLR projection on ``shape``. Returns (visible_polylines, hidden_polylines, bbox)."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GCPnts import GCPnts_QuasiUniformDeflection
    from OCP.GeomAbs import GeomAbs_Line
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.HLRAlgo import HLRAlgo_Projector
    from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    direction, x_dir = _VIEW_DIRS.get(view, _VIEW_DIRS["iso"])
    # The projector looks from `direction` towards origin. Build a coordinate
    # frame whose -Z axis is the view direction.
    look = gp_Dir(*direction)
    x_axis = gp_Dir(*x_dir)
    ax = gp_Ax2(gp_Pnt(0, 0, 0), look, x_axis)
    projector = HLRAlgo_Projector(ax)

    hlr = HLRBRep_Algo()
    hlr.Add(shape)
    hlr.Projector(projector)
    hlr.Update()
    hlr.Hide()

    to_shape = HLRBRep_HLRToShape(hlr)
    visible_compound = to_shape.VCompound()
    outline_visible = to_shape.OutLineVCompound()
    hidden_compound = to_shape.HCompound()
    outline_hidden = to_shape.OutLineHCompound()

    def _walk_edges(compound):
        out: list[list[tuple[float, float]]] = []
        if compound is None or compound.IsNull():
            return out
        explorer = TopExp_Explorer(compound, TopAbs_EDGE)
        while explorer.More():
            edge_shape = explorer.Current()
            try:
                edge = TopoDS.Edge_s(edge_shape)
                curve = BRepAdaptor_Curve(edge)
                if curve.GetType() == GeomAbs_Line:
                    pts = [
                        curve.Value(curve.FirstParameter()),
                        curve.Value(curve.LastParameter()),
                    ]
                else:
                    sampler = GCPnts_QuasiUniformDeflection(curve, 0.2)
                    pts = [sampler.Value(i) for i in range(1, sampler.NbPoints() + 1)]
                # The HLR projector emits 3D edges in the projection plane;
                # X = projected X, Y = projected Y, Z is depth (~0 for visible)
                poly = [(p.X(), p.Y()) for p in pts]
                if len(poly) >= 2:
                    out.append(poly)
            except Exception as exc:  # noqa: BLE001
                logger.debug("HLR edge walk skipped one: %s", exc)
            explorer.Next()
        return out

    visible = _walk_edges(visible_compound) + _walk_edges(outline_visible)
    hidden = _walk_edges(hidden_compound) + _walk_edges(outline_hidden)

    all_pts = [pt for poly in visible + hidden for pt in poly]
    if not all_pts:
        bbox = (0.0, 0.0, 1.0, 1.0)
    else:
        xs = [p[0] for p in all_pts]
        ys = [p[1] for p in all_pts]
        bbox = (min(xs), min(ys), max(xs), max(ys))
    return visible, hidden, bbox


def _polylines_to_svg(visible, hidden, bbox, *, width, height, view_label) -> str:
    xmin, ymin, xmax, ymax = bbox
    w = max(xmax - xmin, 1e-6)
    h = max(ymax - ymin, 1e-6)
    pad = 0.04 * max(w, h)
    xmin -= pad
    ymin -= pad
    w += 2 * pad
    h += 2 * pad
    stroke_w = 0.0035 * max(w, h)

    def _poly_str(poly):
        return " ".join(f"{x:.3f},{y:.3f}" for x, y in poly)

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{xmin:.3f} {ymin:.3f} {w:.3f} {h:.3f}" '
        f'width="{width}" height="{height}" preserveAspectRatio="xMidYMid meet" '
        f'style="background:{_BG_COLOR}">'
    )
    # Flip Y so CAD +Y-up renders as +Y-up in SVG (SVG natively +Y-down).
    parts.append(f'<g transform="translate(0,{ymin + ymax:.3f}) scale(1,-1)">')
    for poly in hidden:
        parts.append(
            f'<polyline points="{_poly_str(poly)}" fill="none" '
            f'stroke="{_HIDDEN_COLOR}" stroke-width="{stroke_w:.4f}" '
            f'stroke-dasharray="{stroke_w * 4:.4f},{stroke_w * 2:.4f}"/>'
        )
    for poly in visible:
        parts.append(
            f'<polyline points="{_poly_str(poly)}" fill="none" '
            f'stroke="{_OUTLINE_COLOR}" stroke-width="{stroke_w:.4f}" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
        )
    parts.append("</g>")
    parts.append(
        f'<text x="{xmin + 0.02 * w:.3f}" y="{ymin + 0.06 * h:.3f}" '
        f'font-family="system-ui,sans-serif" font-size="{0.04 * max(w, h):.3f}" '
        f'fill="#555">{view_label.upper()}</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts)


def render_part_views(
    source_path: Path,
    product_id: str,
    *,
    views: tuple[str, ...] = ("top", "front", "iso"),
    width: int = 360,
    height: int = 280,
) -> dict[str, str]:
    """Render the original solid as SVG in each requested view.

    Returns a dict of ``{view_name: svg_string}``. Empty dict if the part's
    solid cannot be found or projection fails.
    """
    solid = find_solid_for_part(source_path, product_id)
    if solid is None:
        return {}

    out: dict[str, str] = {}
    for view in views:
        try:
            visible, hidden, bbox = _project_shape(solid, view)
            svg = _polylines_to_svg(
                visible,
                hidden,
                bbox,
                width=width,
                height=height,
                view_label=view,
            )
            out[view] = svg
        except Exception as exc:  # noqa: BLE001
            logger.warning("HLR projection failed for %s view=%s: %s", product_id, view, exc)
            continue
    return out


__all__ = ["find_solid_for_part", "render_part_views"]
