"""Cross-section slicing, signature extraction, and constancy detection.

See plan §4.2.

Slice a `TopoDS_Solid` perpendicular to its OBB principal axis at the seven
normalised positions ``[0.10, 0.25, 0.40, 0.50, 0.60, 0.75, 0.90]``. Each slice
is reduced to a :class:`CrossSection` record carrying area, perimeter,
segment counts, 2D bbox, ordered polyline, and a rotation/reflection-invariant
``shape_hash``.

``is_constant`` returns True when the per-slice coefficients of variation for
area / perimeter / bbox-area are all below ``cv_limit`` AND every slice's
``shape_hash`` is equal modulo rotation and reflection.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable

import numpy as np
from OCP.Bnd import Bnd_Box, Bnd_OBB
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRepGProp import BRepGProp
from OCP.BRepTools import BRepTools_WireExplorer
from OCP.GeomAbs import GeomAbs_CurveType
from OCP.gp import gp_Dir, gp_Pln, gp_Pnt
from OCP.GProp import GProp_GProps
from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_HSequenceOfShape

from ..config.classification_variables import (
    CIRCULARITY_STDDEV_RATIO,
    CROSS_SECTION_CURVE_SAMPLES_PER_EDGE,
    CROSS_SECTION_CV_LIMIT,
    CROSS_SECTION_N_SLICES,
    FILLET_RUN_DEG_MIN,
    FILLET_VERTEX_DEG_MAX,
    OBB_PRINCIPAL_AXIS_MIN_RATIO,
    PROFILE_FILLET_COLLAPSE_RATIO,
    SYMMETRY_HAUSDORFF_RATIO,
    TURNING_ANGLE_DEG_MIN,
)
from .types import CrossSection

logger = logging.getLogger(__name__)

# Normalised slice positions, skipping the very endpoints where slicing on
# the OBB face is degenerate.
DEFAULT_SLICE_POSITIONS: tuple[float, ...] = (0.10, 0.25, 0.40, 0.50, 0.60, 0.75, 0.90)

_HASH_QUANT = 100  # quantise normalised segment lengths and angles to 0.01.
_HASH_PRESNAP = 1e-4  # collapse values within 1e-4 onto the same float before
# bucketing, so two polylines that differ only by
# floating-point noise produce identical hashes.


# ---------------------------------------------------------------------------
# OBB / AABB principal axis
# ---------------------------------------------------------------------------


def principal_axis_obb(solid) -> tuple[gp_Pnt, gp_Dir, tuple[float, float, float]]:
    """Return ``(center, principal_axis_direction, (long, mid, short))``.

    Tries :class:`Bnd_OBB` first. If the OBB's longest / second-longest
    half-extent ratio is below :data:`OBB_PRINCIPAL_AXIS_MIN_RATIO` (i.e. the
    part is near-cubic and the OBB orientation is unstable) we fall back to a
    plain AABB and return its longest dimension as the principal axis.
    """

    try:
        obb = Bnd_OBB()
        BRepBndLib.AddOBB_s(solid, obb)
        center = obb.Center()
        dirs = (obb.XDirection(), obb.YDirection(), obb.ZDirection())
        halves = (obb.XHSize(), obb.YHSize(), obb.ZHSize())
        order = sorted(range(3), key=lambda i: halves[i], reverse=True)
        long_h, mid_h, short_h = (halves[i] for i in order)
        long_d = dirs[order[0]]
        ratio = (long_h / mid_h) if mid_h > 1e-12 else float("inf")
        if ratio >= OBB_PRINCIPAL_AXIS_MIN_RATIO:
            return (
                gp_Pnt(center.X(), center.Y(), center.Z()),
                gp_Dir(long_d.X(), long_d.Y(), long_d.Z()),
                (long_h * 2.0, mid_h * 2.0, short_h * 2.0),
            )
        logger.debug(
            "principal_axis_obb: OBB ratio %.3f < %.3f, falling back to AABB",
            ratio,
            OBB_PRINCIPAL_AXIS_MIN_RATIO,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("principal_axis_obb: OBB failed: %s; falling back to AABB", exc)

    # AABB fallback.
    bbox = Bnd_Box()
    BRepBndLib.Add_s(solid, bbox)
    xmin, ymin, zmin, xmax, ymax, zmax = bbox.Get()
    dims = (xmax - xmin, ymax - ymin, zmax - zmin)
    center = gp_Pnt((xmin + xmax) / 2.0, (ymin + ymax) / 2.0, (zmin + zmax) / 2.0)
    axis_unit_vecs = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    order = sorted(range(3), key=lambda i: dims[i], reverse=True)
    sorted_dims = tuple(dims[i] for i in order)
    long_axis = axis_unit_vecs[order[0]]
    return center, gp_Dir(*long_axis), sorted_dims


def _build_plane(center: gp_Pnt, normal: gp_Dir, long_extent: float, t: float) -> gp_Pln:
    """Construct a slicing plane at normalised position ``t in [0, 1]``."""

    offset = (t - 0.5) * long_extent
    p = gp_Pnt(
        center.X() + normal.X() * offset,
        center.Y() + normal.Y() * offset,
        center.Z() + normal.Z() * offset,
    )
    return gp_Pln(p, normal)


# ---------------------------------------------------------------------------
# Wire → 2D polyline / signature
# ---------------------------------------------------------------------------


def _project_to_2d(p3: gp_Pnt, plane: gp_Pln) -> tuple[float, float]:
    ax = plane.Position()
    loc = ax.Location()
    xd = ax.XDirection()
    yd = ax.YDirection()
    dx, dy, dz = p3.X() - loc.X(), p3.Y() - loc.Y(), p3.Z() - loc.Z()
    u = dx * xd.X() + dy * xd.Y() + dz * xd.Z()
    v = dx * yd.X() + dy * yd.Y() + dz * yd.Z()
    return (u, v)


def _wire_polyline_2d(wire, plane: gp_Pln) -> list[tuple[float, float]]:
    """Ordered 2D polyline along *wire*.

    Straight (line) edges contribute a single vertex; curved edges (circle,
    ellipse, BSpline, ...) are sampled at
    :data:`CROSS_SECTION_CURVE_SAMPLES_PER_EDGE` intermediate points so that
    the polyline carries enough geometric information for area/bbox
    computation and a meaningful shape hash. Consecutive duplicate points are
    collapsed.
    """

    pts: list[tuple[float, float]] = []
    try:
        we = BRepTools_WireExplorer(wire)
        while we.More():
            edge = we.Current()
            try:
                bac = BRepAdaptor_Curve(edge)
                ctype = bac.GetType()
                fp, lp = bac.FirstParameter(), bac.LastParameter()
            except Exception:
                # Fall back to vertex location.
                v = we.CurrentVertex()
                p = BRep_Tool.Pnt_s(v)
                pts.append(_project_to_2d(p, plane))
                we.Next()
                continue

            if ctype == GeomAbs_CurveType.GeomAbs_Line:
                v = we.CurrentVertex()
                p = BRep_Tool.Pnt_s(v)
                pts.append(_project_to_2d(p, plane))
            else:
                # Sample curve excluding the last parameter (the next edge's
                # starting vertex provides it). For a closed single circle
                # this yields N samples and no duplicate.
                for i in range(CROSS_SECTION_CURVE_SAMPLES_PER_EDGE):
                    u = fp + (lp - fp) * i / CROSS_SECTION_CURVE_SAMPLES_PER_EDGE
                    p3 = bac.Value(u)
                    pts.append(_project_to_2d(p3, plane))
            we.Next()
    except Exception as exc:  # pragma: no cover
        logger.debug("_wire_polyline_2d failed: %s", exc)

    # Collapse runs of (almost) coincident points.
    deduped: list[tuple[float, float]] = []
    for u, v in pts:
        if deduped and abs(deduped[-1][0] - u) < 1e-9 and abs(deduped[-1][1] - v) < 1e-9:
            continue
        deduped.append((u, v))
    return deduped


def _wire_n_segments(wire) -> int:
    """Count canonical segments along a wire: a circle counts as a single
    segment, lines are 1 each, BSpline/ellipse 1 each. This is the
    'n_outer_segments' field used by the profile-family matcher."""

    n = 0
    try:
        we = BRepTools_WireExplorer(wire)
        while we.More():
            we.Current()
            n += 1
            we.Next()
    except Exception as exc:  # pragma: no cover
        logger.debug("_wire_n_segments failed: %s", exc)
    return n


def _wire_perimeter(wire) -> float:
    try:
        props = GProp_GProps()
        BRepGProp.LinearProperties_s(wire, props)
        return float(props.Mass())
    except Exception:  # pragma: no cover
        return 0.0


def _wire_face_area(wire, plane: gp_Pln) -> float:
    try:
        mk = BRepBuilderAPI_MakeFace(plane, wire, True)
        if not mk.IsDone():
            return 0.0
        face = mk.Face()
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        return float(props.Mass())
    except Exception:  # pragma: no cover
        return 0.0


def _polyline_bbox_2d(poly: list[tuple[float, float]]) -> tuple[float, float]:
    if not poly:
        return (0.0, 0.0)
    arr = np.asarray(poly)
    w = float(arr[:, 0].max() - arr[:, 0].min())
    h = float(arr[:, 1].max() - arr[:, 1].min())
    return (w, h)


# ---------------------------------------------------------------------------
# Rotation / reflection-invariant shape hash
# ---------------------------------------------------------------------------


def _normalise_lengths(seg_lens: np.ndarray) -> np.ndarray:
    total = float(seg_lens.sum())
    if total <= 1e-12:
        return np.zeros_like(seg_lens)
    return seg_lens / total


def _turning_angles(poly: list[tuple[float, float]]) -> np.ndarray:
    if len(poly) < 3:
        return np.zeros(len(poly))
    arr = np.asarray(poly, dtype=float)
    n = len(arr)
    angles = np.zeros(n)
    for i in range(n):
        prev_p = arr[(i - 1) % n]
        cur = arr[i]
        nxt = arr[(i + 1) % n]
        v1 = cur - prev_p
        v2 = nxt - cur
        # signed turning angle (atan2 of cross, dot) — sign flips under reflection.
        ang = math.atan2(v1[0] * v2[1] - v1[1] * v2[0], v1[0] * v2[0] + v1[1] * v2[1])
        angles[i] = ang
    return angles


# ---------------------------------------------------------------------------
# Fillet collapse
# ---------------------------------------------------------------------------


# Fillet-collapse thresholds. A vertex is treated as part of a fillet if its
# turning-angle magnitude sits in the small but non-trivial band between
# FILLET_NOISE_DEG and FILLET_VERTEX_DEG_MAX (both from
# classification_variables). A run is only collapsed once its cumulative
# turning angle exceeds FILLET_RUN_DEG_MIN (so a single noisy vertex on a
# near-straight edge is left alone).


def _line_line_intersection(
    p1: np.ndarray, d1: np.ndarray, p2: np.ndarray, d2: np.ndarray
) -> np.ndarray | None:
    """Intersect line ``p1 + s*d1`` with ``p2 + t*d2``. Return ``None`` if
    parallel or singular."""

    cross = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(cross) < 1e-9:
        return None
    diff = p2 - p1
    s = (diff[0] * d2[1] - diff[1] * d2[0]) / cross
    return p1 + s * d1


def collapse_fillets(
    polyline: list[tuple[float, float]],
    ratio: float = PROFILE_FILLET_COLLAPSE_RATIO,
    scale_ref: float | None = None,
) -> list[tuple[float, float]]:
    """Replace small fillet arcs in *polyline* with their corner intersection.

    Algorithm
    ---------
    1. Compute every edge length and every vertex turning angle.
    2. Mark each edge as "short" when its length is below a small fraction of
       ``scale_ref``. ``scale_ref`` defaults to ``min(bbox_h, bbox_b)`` of
       the polyline.
    3. Group contiguous short edges into runs. Each such run is a candidate
       fillet: the vertices inside the run carry the small turning angles of
       a discretised arc, while the long edges before/after the run play the
       role of the original sharp corner's flanks.
    4. For each run, verify the cumulative turning angle of its interior
       vertices exceeds :data:`FILLET_RUN_DEG_MIN` (~60 deg) and each
       individual vertex turn is below :data:`FILLET_VERTEX_DEG_MAX`
       (~20 deg). If so, extend the flanking long edges and replace the run
       with their intersection point.

    A run is skipped when:
      * the flanking edges are missing or parallel (no intersection exists);
      * the run's chord length exceeds ``ratio * scale_ref`` — this keeps
        large radii (curved profile types like CHS) intact.

    The function is deterministic: identical input yields identical output.
    """

    if not polyline or len(polyline) < 4:
        return [(p[0], p[1]) for p in polyline]

    arr = np.asarray(polyline, dtype=float)
    n = len(arr)

    if scale_ref is None:
        dx = float(arr[:, 0].max() - arr[:, 0].min())
        dy = float(arr[:, 1].max() - arr[:, 1].min())
        scale_ref = min(dx, dy)
    chord_limit = max(0.0, float(ratio) * float(scale_ref))

    # Edge lengths: edge ``e[k]`` connects vertex k and vertex (k+1) mod n.
    edges = np.roll(arr, -1, axis=0) - arr
    edge_lens = np.linalg.norm(edges, axis=1)
    angles = _turning_angles([tuple(p) for p in arr.tolist()])
    abs_deg = np.degrees(np.abs(angles))

    # Short-edge threshold: well below the smallest sensible profile dim.
    short_limit = max(chord_limit, 1e-6)
    is_short = edge_lens < short_limit

    if not is_short.any() or is_short.all():
        # No flanking long edges -> nothing to collapse.
        return [tuple(p) for p in arr.tolist()]

    # Find runs of consecutive short edges on the cyclic edge ring. Start
    # scanning at the first long edge so we never split a run across the
    # array boundary.
    start_edge = int(np.argmax(~is_short))  # first long edge
    edge_order = [(start_edge + i) % n for i in range(n)]
    short_ordered = is_short[edge_order]

    runs: list[tuple[int, int]] = []  # (first_edge_idx, last_edge_idx) in edge_order
    i = 0
    while i < n:
        if not short_ordered[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and short_ordered[j + 1]:
            j += 1
        runs.append((i, j))
        i = j + 1

    if not runs:
        return [tuple(p) for p in arr.tolist()]

    drop = np.zeros(n, dtype=bool)
    insert_after: dict[int, tuple[float, float]] = {}

    for first_e_ord, last_e_ord in runs:
        # Vertices inside the fillet run: between the first short edge's
        # starting vertex and the last short edge's ending vertex, exclusive
        # of those two anchor points. Anchor points stay; interior dies.
        v_start = edge_order[first_e_ord]  # vertex at start of run
        v_end = (edge_order[last_e_ord] + 1) % n  # vertex at end of run

        # Interior vertex indices (the ones whose turning angles should be
        # small and whose cumulative sum represents the arc sweep).
        interior: list[int] = []
        k = (v_start + 1) % n
        while k != v_end:
            interior.append(k)
            k = (k + 1) % n
        if not interior:
            continue

        max_turn = float(abs_deg[interior].max())
        cum_turn = float(abs_deg[interior].sum())
        if max_turn >= FILLET_VERTEX_DEG_MAX:
            continue
        if cum_turn < FILLET_RUN_DEG_MIN:
            continue

        # Flanking long edges: the edge BEFORE v_start (long) and the edge
        # AFTER v_end (long).
        pre_edge = (v_start - 1) % n
        post_edge = v_end
        if edge_lens[pre_edge] < short_limit or edge_lens[post_edge] < short_limit:
            continue

        chord = float(np.linalg.norm(arr[v_end] - arr[v_start]))
        if chord_limit > 0 and chord > chord_limit:
            continue

        p_pre = arr[(v_start - 1) % n]
        d_in = arr[v_start] - p_pre
        p_post = arr[v_end]
        d_out = arr[(v_end + 1) % n] - p_post
        if np.linalg.norm(d_in) < 1e-9 or np.linalg.norm(d_out) < 1e-9:
            continue
        intersection = _line_line_intersection(p_pre, d_in, p_post, d_out)
        if intersection is None:
            continue

        # Drop both anchor vertices and every interior vertex; insert the
        # intersection point in their place (anchored after the pre-edge).
        drop[v_start] = True
        drop[v_end] = True
        for k in interior:
            drop[k] = True
        insert_after[(v_start - 1) % n] = (
            float(intersection[0]),
            float(intersection[1]),
        )

    out: list[tuple[float, float]] = []
    for k in range(n):
        if not drop[k]:
            out.append((float(arr[k, 0]), float(arr[k, 1])))
        if k in insert_after:
            out.append(insert_after[k])

    # Drop runs of (almost) coincident points produced by the collapse,
    # including a wrap-around duplicate.
    deduped: list[tuple[float, float]] = []
    for u, v in out:
        if deduped and abs(deduped[-1][0] - u) < 1e-7 and abs(deduped[-1][1] - v) < 1e-7:
            continue
        deduped.append((u, v))
    if (
        len(deduped) >= 2
        and abs(deduped[0][0] - deduped[-1][0]) < 1e-7
        and abs(deduped[0][1] - deduped[-1][1]) < 1e-7
    ):
        deduped.pop()
    return deduped


# ---------------------------------------------------------------------------
# Signature dict (consumed by ProfileShapeMatcher)
# ---------------------------------------------------------------------------


def _polyline_centroid(arr: np.ndarray) -> np.ndarray:
    """Geometric centroid (vertex mean) of a 2D polyline."""

    if arr.size == 0:
        return np.zeros(2)
    return arr.mean(axis=0)


def _is_circular(polyline: Iterable[tuple[float, float]]) -> bool:
    """True if ``polyline`` is well-approximated by a circle.

    Uses the std-dev of distances from the centroid: a circle has zero
    radial variance, so any polyline whose normalised std-dev is below
    :data:`CIRCULARITY_STDDEV_RATIO` is treated as circular.
    """

    poly = list(polyline)
    if len(poly) < 6:
        return False
    arr = np.asarray(poly, dtype=float)
    c = _polyline_centroid(arr)
    radii = np.linalg.norm(arr - c, axis=1)
    mean = float(radii.mean())
    if mean < 1e-9:
        return False
    return float(radii.std() / mean) < CIRCULARITY_STDDEV_RATIO


def _count_corners(polyline: Iterable[tuple[float, float]]) -> int:
    """Count vertices whose turning angle exceeds :data:`TURNING_ANGLE_DEG_MIN`."""

    poly = list(polyline)
    if len(poly) < 3:
        return 0
    angles = _turning_angles(poly)
    threshold = math.radians(TURNING_ANGLE_DEG_MIN)
    return int(np.sum(np.abs(angles) > threshold))


def _polyline_bbox_diag(arr: np.ndarray) -> float:
    if arr.size == 0:
        return 0.0
    dx = float(arr[:, 0].max() - arr[:, 0].min())
    dy = float(arr[:, 1].max() - arr[:, 1].min())
    return math.hypot(dx, dy)


def _hausdorff_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Symmetric Hausdorff distance between two 2D point sets."""

    if a.size == 0 or b.size == 0:
        return float("inf")
    # Pairwise distances; cheap for the small polylines we handle.
    diff = a[:, None, :] - b[None, :, :]
    d2 = np.einsum("ijk,ijk->ij", diff, diff)
    d_ab = float(np.sqrt(d2.min(axis=1)).max())
    d_ba = float(np.sqrt(d2.min(axis=0)).max())
    return max(d_ab, d_ba)


def _has_reflective_symmetry(polyline: Iterable[tuple[float, float]], *, axis: str) -> bool:
    """Reflective symmetry about the centroid-aligned ``x`` or ``y`` axis."""

    poly = list(polyline)
    if len(poly) < 3:
        return False
    arr = np.asarray(poly, dtype=float)
    centred = arr - _polyline_centroid(arr)
    mirrored = centred.copy()
    if axis == "x":
        mirrored[:, 1] = -mirrored[:, 1]
    elif axis == "y":
        mirrored[:, 0] = -mirrored[:, 0]
    else:
        return False
    diag = _polyline_bbox_diag(centred)
    if diag <= 1e-9:
        return False
    return _hausdorff_distance(centred, mirrored) < SYMMETRY_HAUSDORFF_RATIO * diag


def _has_point_symmetry(polyline: Iterable[tuple[float, float]]) -> bool:
    """180-degree rotation symmetry about the centroid (Z-section test)."""

    poly = list(polyline)
    if len(poly) < 3:
        return False
    arr = np.asarray(poly, dtype=float)
    centred = arr - _polyline_centroid(arr)
    diag = _polyline_bbox_diag(centred)
    if diag <= 1e-9:
        return False
    return _hausdorff_distance(centred, -centred) < SYMMETRY_HAUSDORFF_RATIO * diag


def _orthogonality_ratio(polyline: Iterable[tuple[float, float]]) -> float:
    """Fraction of edges whose direction is within 5 deg of an axis.

    The fraction is length-weighted -- short fillet chords don't dilute the
    score for a clearly orthogonal box.
    """

    poly = list(polyline)
    if len(poly) < 2:
        return 0.0
    arr = np.asarray(poly, dtype=float)
    edges = np.roll(arr, -1, axis=0) - arr
    lengths = np.linalg.norm(edges, axis=1)
    total_length = float(lengths.sum())
    if total_length <= 1e-9:
        return 0.0
    aligned = 0.0
    threshold = math.cos(math.radians(5.0))  # within 5 deg of axis-aligned
    for edge, L in zip(edges, lengths):
        if L <= 1e-9:
            continue
        cx = abs(edge[0]) / L
        cy = abs(edge[1]) / L
        if cx >= threshold or cy >= threshold:
            aligned += float(L)
    return aligned / total_length


def compute_signature(
    polyline: list[tuple[float, float]],
    inner_polylines: list[list[tuple[float, float]]] | None = None,
) -> dict:
    """Produce the signature dict consumed by :class:`ProfileShapeMatcher`.

    Keys
    ----
    family            : optional explicit family override (always absent here)
    n_inner           : count of inner polylines (interior wires)
    n_seg             : corner count after collapsing fillets
    n_seg_outer       : same as ``n_seg`` but restricted to the outer wire
    sym_x, sym_y      : reflective symmetry about the centroid axes
    point_sym         : 180-degree rotation symmetry
    orth_ratio        : length-weighted fraction of axis-aligned edges
    outer_circular    : outer polyline well-approximated by a circle
    inner_circular    : first inner polyline circular (if any)
    inner_bbox        : (w, h) of the first inner polyline, or None
    wall_thickness    : half-difference of outer / inner bbox dims, when hollow
    bbox_h, bbox_b    : outer bbox larger / smaller dim (handy for the matcher)
    """

    inner_polylines = list(inner_polylines or [])
    out: dict = {
        "n_inner": len(inner_polylines),
        "n_seg": 0,
        "n_seg_outer": 0,
        "n_seg_pre_collapse": 0,
        "n_seg_post_collapse": 0,
        "sym_x": False,
        "sym_y": False,
        "point_sym": False,
        "orth_ratio": 0.0,
        "outer_circular": False,
        "inner_circular": False,
        "inner_bbox": None,
        "wall_thickness": None,
        "bbox_h": 0.0,
        "bbox_b": 0.0,
    }
    if not polyline:
        return out

    arr = np.asarray(polyline, dtype=float)
    dx = float(arr[:, 0].max() - arr[:, 0].min())
    dy = float(arr[:, 1].max() - arr[:, 1].min())
    out["bbox_h"] = max(dx, dy)
    out["bbox_b"] = min(dx, dy)

    # Circularity is measured on the raw outer polyline -- fillet collapse
    # would distort the radial std-dev test for tubular profiles.
    outer_circular = _is_circular(polyline)
    out["outer_circular"] = outer_circular

    # ``n_seg_pre_collapse`` is the polyline's vertex count BEFORE fillet
    # collapse -- a discretised arc inflates this far beyond the canonical
    # corner count, which is the symptom we use the collapse to fix.
    out["n_seg_pre_collapse"] = len(polyline)

    scale_ref = min(out["bbox_h"], out["bbox_b"])
    collapsed_outer = collapse_fillets(polyline, scale_ref=scale_ref)
    collapsed_inner = [collapse_fillets(p, scale_ref=scale_ref) for p in inner_polylines]

    n_corners_post = _count_corners(collapsed_outer)
    if outer_circular and n_corners_post == 0:
        # For circular outer polylines no vertex clears the >30 deg turn
        # threshold, but the matcher still wants a non-zero n_seg to
        # distinguish CHS from straight-edged families. Fall back to the
        # sample count.
        n_corners_post = len(collapsed_outer)
    out["n_seg_post_collapse"] = n_corners_post
    out["n_seg_outer"] = n_corners_post
    out["n_seg"] = n_corners_post

    out["sym_x"] = _has_reflective_symmetry(collapsed_outer, axis="x")
    out["sym_y"] = _has_reflective_symmetry(collapsed_outer, axis="y")
    out["point_sym"] = _has_point_symmetry(collapsed_outer)
    out["orth_ratio"] = _orthogonality_ratio(collapsed_outer)

    if collapsed_inner:
        first_inner = collapsed_inner[0]
        out["inner_circular"] = _is_circular(inner_polylines[0])
        if first_inner:
            inner_arr = np.asarray(first_inner, dtype=float)
            iw = float(inner_arr[:, 0].max() - inner_arr[:, 0].min())
            ih = float(inner_arr[:, 1].max() - inner_arr[:, 1].min())
            out["inner_bbox"] = (max(iw, ih), min(iw, ih))
            # Wall thickness = half the bbox-dim difference (outer minus inner).
            out["wall_thickness"] = max(
                (out["bbox_h"] - out["inner_bbox"][0]) / 2.0,
                (out["bbox_b"] - out["inner_bbox"][1]) / 2.0,
            )
    return out


def shape_hash_from_polyline(polyline: Iterable[tuple[float, float]]) -> tuple:
    """Compute a rotation/reflection-invariant signature from a 2D polyline.

    Procedure:
      1. Compute normalised edge lengths (sum to 1).
      2. Compute unsigned turning angles at each vertex.
      3. Reflection invariance: angles enter the hash as absolute values
         (sign would flip under mirror).
      4. Rotation invariance: we sort the resulting per-vertex tuples
         canonically so any cyclic rotation of vertex order yields the
         same multiset.
      5. Quantise to int64 buckets (1/100 of the normalised length and
         1/100 of pi for angles) to absorb floating-point noise.
    """

    poly: list[tuple[float, float]] = [(p[0], p[1]) for p in polyline]
    if len(poly) < 2:
        return tuple()

    arr = np.asarray(poly, dtype=float)
    seg = np.linalg.norm(np.roll(arr, -1, axis=0) - arr, axis=1)
    norm_lens = _normalise_lengths(seg)
    angles = np.abs(_turning_angles(poly))

    # Pre-snap to 1e-4 before bucketing. This collapses values that drift by
    # a few epsilons across slices onto the same float, so floor() does not
    # tip them into adjacent buckets.
    q_len = np.floor(
        np.round(norm_lens / _HASH_PRESNAP) * _HASH_PRESNAP * _HASH_QUANT + 0.5
    ).astype(np.int64)
    q_ang = np.floor(
        np.round(angles / _HASH_PRESNAP) * _HASH_PRESNAP / math.pi * _HASH_QUANT + 0.5
    ).astype(np.int64)

    # Pair each vertex with its outgoing segment + incoming turning angle.
    pairs = sorted(zip(q_len.tolist(), q_ang.tolist()))
    return tuple((int(a), int(b)) for a, b in pairs)


# ---------------------------------------------------------------------------
# Slice extraction
# ---------------------------------------------------------------------------


def _section_wires(solid, plane: gp_Pln):
    """Run BRepAlgoAPI_Section and connect edges into wires."""

    section = BRepAlgoAPI_Section(solid, plane)
    section.Build()
    if not section.IsDone():
        return []
    sec_shape = section.Shape()
    edges = TopTools_HSequenceOfShape()
    exp = TopExp_Explorer(sec_shape, TopAbs_EDGE)
    while exp.More():
        edges.Append(exp.Current())
        exp.Next()
    if edges.Length() == 0:
        return []
    wires_seq = TopTools_HSequenceOfShape()
    ShapeAnalysis_FreeBounds.ConnectEdgesToWires_s(edges, 1e-6, False, wires_seq)
    out = []
    for i in range(1, wires_seq.Length() + 1):
        out.append(TopoDS.Wire_s(wires_seq.Value(i)))
    return out


def _slice_one(solid, plane: gp_Pln, t: float) -> CrossSection | None:
    wires = _section_wires(solid, plane)
    if not wires:
        return None

    # Outer wire = largest area; inner wires count toward n_inner_wires.
    wire_areas = [(w, _wire_face_area(w, plane)) for w in wires]
    wire_areas.sort(key=lambda x: x[1], reverse=True)
    outer_wire, outer_area = wire_areas[0]
    inner_wires = [(w, a) for w, a in wire_areas[1:] if a > 1e-9]
    inner_count = len(inner_wires)

    polyline = _wire_polyline_2d(outer_wire, plane)
    inner_polylines = [_wire_polyline_2d(w, plane) for w, _ in inner_wires]
    perim = _wire_perimeter(outer_wire)
    n_seg = _wire_n_segments(outer_wire)
    # Net area = outer minus inner pockets.
    inner_area = sum(a for _, a in inner_wires)
    net_area = max(outer_area - inner_area, 0.0)
    bbox2d = _polyline_bbox_2d(polyline)
    sig_hash = shape_hash_from_polyline(polyline)
    signature = compute_signature(polyline, inner_polylines)

    return CrossSection(
        position=t,
        area=net_area,
        perimeter=perim,
        n_outer_segments=n_seg,
        n_inner_wires=inner_count,
        bbox_2d=bbox2d,
        polyline=polyline,
        shape_hash=sig_hash,
        signature=signature,
        inner_polylines=inner_polylines,
    )


def _shape_has_tessellation_only_face(shape) -> bool:
    """True if *shape* contains a face without an attached Geom_Surface.

    ``BRepAlgoAPI_Section`` segfaults inside OCCT when fed a tessellation-only
    face. Callers use this pre-flight to bail before invoking the slicer.
    """

    try:
        exp = TopExp_Explorer(shape, TopAbs_FACE)
    except Exception:  # pragma: no cover - defensive
        return False
    while exp.More():
        try:
            face = TopoDS.Face_s(exp.Current())
            if BRep_Tool.Surface_s(face) is None:
                return True
        except Exception:  # pragma: no cover - defensive
            pass
        exp.Next()
    return False


def slice_solid(
    solid,
    n_slices: int = CROSS_SECTION_N_SLICES,
    *,
    axis: gp_Dir | None = None,
) -> list[CrossSection]:
    """Slice *solid* into ``n_slices`` cross-sections perpendicular to its
    principal axis (or the supplied ``axis``).

    Returns an empty list when slicing fails entirely. Individual slice
    failures are skipped silently with a debug log.

    Tessellation-only solids (any face missing a Geom_Surface) are short-
    circuited and return an empty list — :class:`BRepAlgoAPI_Section` would
    segfault on such input.
    """

    if _shape_has_tessellation_only_face(solid):
        logger.debug("slice_solid: input has tessellation-only faces, skipping slicer")
        return []

    try:
        center, principal, dims = principal_axis_obb(solid)
    except Exception as exc:  # pragma: no cover - belt-and-braces
        logger.warning("slice_solid: principal axis failed: %s", exc)
        return []
    if axis is not None:
        principal = axis
    long_extent = dims[0]
    if long_extent <= 1e-9:
        return []

    positions = _choose_positions(n_slices)
    out: list[CrossSection] = []
    for t in positions:
        try:
            plane = _build_plane(center, principal, long_extent, t)
            sec = _slice_one(solid, plane, t)
            if sec is not None:
                out.append(sec)
        except Exception as exc:  # pragma: no cover
            logger.debug("slice_solid: slice at t=%.2f failed: %s", t, exc)
    return out


def _choose_positions(n_slices: int) -> tuple[float, ...]:
    if n_slices == len(DEFAULT_SLICE_POSITIONS):
        return DEFAULT_SLICE_POSITIONS
    if n_slices <= 1:
        return (0.5,)
    # Even sampling avoiding the endpoints.
    return tuple(np.linspace(0.10, 0.90, n_slices))


# ---------------------------------------------------------------------------
# Constancy test
# ---------------------------------------------------------------------------


def _cv(values: Iterable[float]) -> float:
    arr = np.asarray(list(values), dtype=float)
    if arr.size == 0:
        return float("inf")
    mean = float(arr.mean())
    if abs(mean) < 1e-9:
        return float("inf")
    return float(arr.std() / mean)


def is_constant(
    sections: list[CrossSection],
    cv_limit: float = CROSS_SECTION_CV_LIMIT,
) -> bool:
    """True if a solid's cross-section is invariant along the principal axis.

    Both of these must hold:
      * The coefficients of variation of (area, perimeter, bbox-area) are all
        below ``cv_limit``.
      * Every slice's ``shape_hash`` is equal modulo rotation/reflection
        (rotation/reflection invariance is already baked into the hash).
    """

    if len(sections) < 2:
        return False
    if any(s.area <= 1e-9 for s in sections):
        return False

    cv_a = _cv(s.area for s in sections)
    cv_p = _cv(s.perimeter for s in sections)
    cv_bbox = _cv(s.bbox_2d[0] * s.bbox_2d[1] for s in sections)
    if max(cv_a, cv_p, cv_bbox) >= cv_limit:
        return False

    first = sections[0].shape_hash
    return all(s.shape_hash == first for s in sections[1:])
