"""Unit tests for cross-section slicing, shape hashing, and constancy."""

from __future__ import annotations

import math

import pytest
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakeWedge,
)

from manufacturing_pipeline.geometry.cross_section import (
    DEFAULT_SLICE_POSITIONS,
    collapse_fillets,
    compute_signature,
    is_constant,
    principal_axis_obb,
    shape_hash_from_polyline,
    slice_solid,
)

# ---------------------------------------------------------------------------
# shape_hash_from_polyline -- rotation / reflection invariance
# ---------------------------------------------------------------------------


def _square():
    return [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]


def test_shape_hash_rotation_invariance():
    """Rotating (cyclically shifting) the vertex list of a closed polyline
    must not change its hash."""

    square = _square()
    rotated = square[1:] + square[:1]
    assert shape_hash_from_polyline(square) == shape_hash_from_polyline(rotated)


def test_shape_hash_reflection_invariance():
    """Reversing the vertex order (a mirror reflection) must not change the
    hash."""

    square = _square()
    reflected = list(reversed(square))
    assert shape_hash_from_polyline(square) == shape_hash_from_polyline(reflected)


def test_shape_hash_distinguishes_different_shapes():
    square = _square()
    triangle = [(0.0, 0.0), (10.0, 0.0), (5.0, 8.66)]
    assert shape_hash_from_polyline(square) != shape_hash_from_polyline(triangle)


# ---------------------------------------------------------------------------
# principal_axis_obb
# ---------------------------------------------------------------------------


def test_principal_axis_obb_long_box():
    """For a 100x10x10 box, the principal axis is the 100-mm direction and
    extents come back sorted descending."""

    box = BRepPrimAPI_MakeBox(100.0, 10.0, 10.0).Solid()
    _center, _axis, dims = principal_axis_obb(box)
    assert dims[0] >= dims[1] >= dims[2]
    assert dims[0] == pytest.approx(100.0, rel=1e-3)


# ---------------------------------------------------------------------------
# slice_solid + is_constant
# ---------------------------------------------------------------------------


def test_slice_solid_returns_seven_sections_on_prismatic_box():
    box = BRepPrimAPI_MakeBox(100.0, 60.0, 8.0).Solid()
    sections = slice_solid(box, 7)
    assert len(sections) == len(DEFAULT_SLICE_POSITIONS) == 7
    areas = [s.area for s in sections]
    # Prismatic box -> all slice areas equal (within slicing tolerance).
    mean_a = sum(areas) / len(areas)
    for a in areas:
        assert a == pytest.approx(mean_a, rel=1e-3)


def test_is_constant_true_for_prismatic_box():
    box = BRepPrimAPI_MakeBox(100.0, 60.0, 8.0).Solid()
    sections = slice_solid(box, 7)
    assert is_constant(sections) is True


def test_is_constant_false_for_tapered_wedge():
    """A wedge has a varying cross-section along its principal axis."""

    # MakeWedge(dx, dy, dz, ltx): a box of size dx x dy x dz whose top face is
    # offset by ltx in X — i.e. tapered along Z.
    wedge = BRepPrimAPI_MakeWedge(100.0, 60.0, 20.0, 30.0).Solid()
    sections = slice_solid(wedge, 7)
    assert len(sections) >= 2
    assert is_constant(sections) is False


def test_slice_solid_cylinder_circular_section():
    cyl = BRepPrimAPI_MakeCylinder(10.0, 100.0).Solid()
    sections = slice_solid(cyl, 7)
    assert len(sections) == 7
    for s in sections:
        # Area of a 10-mm radius circle = pi * 100.
        assert s.area == pytest.approx(math.pi * 100.0, rel=1e-2)
    assert is_constant(sections) is True


def test_is_constant_needs_at_least_two_sections():
    assert is_constant([]) is False


def test_shape_hash_empty_polyline():
    assert shape_hash_from_polyline([]) == tuple()


# ---------------------------------------------------------------------------
# compute_signature -- per-family fingerprints
# ---------------------------------------------------------------------------


def _circle_polyline(radius: float, n: int = 24, *, cx: float = 0.0, cy: float = 0.0):
    return [
        (cx + radius * math.cos(2 * math.pi * i / n), cy + radius * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


def test_compute_signature_square_outer():
    square = [(-50.0, -50.0), (50.0, -50.0), (50.0, 50.0), (-50.0, 50.0)]
    sig = compute_signature(square, inner_polylines=[])
    assert sig["n_seg"] == 4
    assert sig["n_seg_outer"] == 4
    assert sig["sym_x"] is True
    assert sig["sym_y"] is True
    assert sig["orth_ratio"] == pytest.approx(1.0, abs=1e-6)
    assert sig["outer_circular"] is False
    assert sig["n_inner"] == 0


def test_compute_signature_circle_outer():
    poly = _circle_polyline(radius=20.0, n=24)
    sig = compute_signature(poly, inner_polylines=[])
    assert sig["outer_circular"] is True
    # 24-gon should expose at least 12 corners after the turning-angle cull.
    assert sig["n_seg"] >= 12
    assert sig["n_inner"] == 0


def test_compute_signature_hollow_square_with_circular_inner():
    outer = [(-50.0, -50.0), (50.0, -50.0), (50.0, 50.0), (-50.0, 50.0)]
    inner = _circle_polyline(radius=40.0, n=24)
    sig = compute_signature(outer, inner_polylines=[inner])
    assert sig["n_inner"] == 1
    assert sig["inner_circular"] is True
    assert sig["inner_bbox"] is not None
    iw, ih = sig["inner_bbox"]
    assert iw == pytest.approx(80.0, rel=1e-2)
    assert ih == pytest.approx(80.0, rel=1e-2)
    # Wall thickness = (100 - 80) / 2 = 10 (both axes).
    assert sig["wall_thickness"] == pytest.approx(10.0, rel=1e-2)


def test_compute_signature_z_section_has_point_symmetry():
    """A canonical Z-section is 180-degree symmetric but neither mirror axis
    matches it. Top flange extends to +x; bottom flange extends to -x."""

    h, b, t = 100.0, 50.0, 8.0
    hh, bb, tt = h / 2.0, b / 2.0, t / 2.0
    z_poly = [
        (-tt, hh),
        (bb, hh),
        (bb, hh - t),
        (tt, hh - t),
        (tt, -hh),
        (-bb, -hh),
        (-bb, -hh + t),
        (-tt, -hh + t),
    ]
    sig = compute_signature(z_poly, inner_polylines=[])
    assert sig["point_sym"] is True
    assert sig["sym_x"] is False
    assert sig["sym_y"] is False


def test_compute_signature_t_section_symmetry():
    # T-section: web pointing DOWN. Symmetric about the y-axis only.
    h, b, t_w, t_f = 100.0, 80.0, 8.0, 10.0
    hh, bb, tw = h / 2.0, b / 2.0, t_w / 2.0
    t_poly = [
        (-bb, hh),
        (bb, hh),
        (bb, hh - t_f),
        (tw, hh - t_f),
        (tw, -hh),
        (-tw, -hh),
        (-tw, hh - t_f),
        (-bb, hh - t_f),
    ]
    sig = compute_signature(t_poly, inner_polylines=[])
    # Mirror about the y-axis (x -> -x) maps the section onto itself.
    assert sig["sym_y"] is True
    # Mirror about the x-axis would flip top flange / bottom web - asymmetric.
    assert sig["sym_x"] is False


def test_compute_signature_empty_polyline():
    sig = compute_signature([], inner_polylines=[])
    assert sig["n_seg"] == 0
    assert sig["n_inner"] == 0
    assert sig["sym_x"] is False
    assert sig["sym_y"] is False


# ---------------------------------------------------------------------------
# slice_solid attaches the signature to each CrossSection
# ---------------------------------------------------------------------------


def test_slice_solid_attaches_signature_to_each_section():
    """The signature dict must be populated on every CrossSection produced
    by slicing -- this is the field the profile matcher consumes."""

    box = BRepPrimAPI_MakeBox(100.0, 60.0, 8.0).Solid()
    sections = slice_solid(box, 7)
    assert sections, "slicing returned no sections"
    for sec in sections:
        assert isinstance(sec.signature, dict) and sec.signature
        assert "n_seg" in sec.signature
        assert "sym_x" in sec.signature
        assert sec.signature["n_inner"] == 0  # solid box has no inner wires


# ---------------------------------------------------------------------------
# collapse_fillets -- discretised-arc handling on real profile silhouettes
# ---------------------------------------------------------------------------


def _arc_points(cx, cy, r, ang_start_deg, ang_end_deg, n):
    """Sample an arc inclusive of both endpoints (n + 1 points)."""
    pts = []
    for i in range(n + 1):
        t = i / n
        a = math.radians(ang_start_deg + (ang_end_deg - ang_start_deg) * t)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _dedup(pts):
    out = []
    for p in pts:
        if out and abs(out[-1][0] - p[0]) < 1e-9 and abs(out[-1][1] - p[1]) < 1e-9:
            continue
        out.append(p)
    return out


def _i_with_fillets(h=200.0, b=100.0, t_w=5.6, t_f=8.5, r=2.0, n_arc=6):
    """Synthesised I-section polyline with concave fillets at the four
    web/flange transitions. CCW orientation, centred on origin."""
    hh, bb, tw = h / 2.0, b / 2.0, t_w / 2.0
    pts: list[tuple[float, float]] = []
    pts.append((-bb, hh))
    pts.append((bb, hh))
    pts.append((bb, hh - t_f))
    pts.extend(_arc_points(tw + r, hh - t_f - r, r, 90, 180, n_arc))
    pts.append((tw, -hh + t_f + r))
    pts.extend(_arc_points(tw + r, -hh + t_f + r, r, 180, 270, n_arc))
    pts.append((bb, -hh + t_f))
    pts.append((bb, -hh))
    pts.append((-bb, -hh))
    pts.append((-bb, -hh + t_f))
    pts.extend(_arc_points(-tw - r, -hh + t_f + r, r, 270, 360, n_arc))
    pts.append((-tw, hh - t_f - r))
    pts.extend(_arc_points(-tw - r, hh - t_f - r, r, 0, 90, n_arc))
    pts.append((-bb, hh - t_f))
    return _dedup(pts)


def _u_with_fillets(h=100.0, b=50.0, t_w=6.0, t_f=8.5, r=2.0, n_arc=6):
    """U-channel polyline opening to +x, with concave fillets where the
    flanges meet the web. Symmetric about y=0."""
    hh = h / 2.0
    pts: list[tuple[float, float]] = []
    pts.append((0.0, hh))
    pts.append((b, hh))
    pts.append((b, hh - t_f))
    pts.extend(_arc_points(t_w + r, hh - t_f - r, r, 90, 180, n_arc))
    pts.append((t_w, -hh + t_f + r))
    pts.extend(_arc_points(t_w + r, -hh + t_f + r, r, 180, 270, n_arc))
    pts.append((b, -hh + t_f))
    pts.append((b, -hh))
    pts.append((0.0, -hh))
    return _dedup(pts)


def _l_with_fillet(h=50.0, b=50.0, t=5.0, r=2.0, n_arc=6):
    """Equal-leg L polyline with a concave fillet at the inner corner."""
    pts: list[tuple[float, float]] = []
    pts.append((0.0, 0.0))
    pts.append((b, 0.0))
    pts.append((b, t))
    pts.extend(_arc_points(t + r, t + r, r, 270, 180, n_arc))  # CCW sweep
    pts.append((t, h))
    pts.append((0.0, h))
    return _dedup(pts)


def _t_with_fillets(h=100.0, b=80.0, t_w=8.0, t_f=10.0, r=2.0, n_arc=6):
    """T-section with flange on top, web pointing down, concave fillets at
    the two inner corners. Symmetric about the y-axis."""
    hh = h / 2.0
    bb = b / 2.0
    tw = t_w / 2.0
    pts: list[tuple[float, float]] = []
    pts.append((-bb, hh))
    pts.append((bb, hh))
    pts.append((bb, hh - t_f))
    pts.extend(_arc_points(tw + r, hh - t_f - r, r, 90, 180, n_arc))
    pts.append((tw, -hh))
    pts.append((-tw, -hh))
    pts.extend(_arc_points(-tw - r, hh - t_f - r, r, 0, 90, n_arc))
    pts.append((-bb, hh - t_f))
    return _dedup(pts)


def test_collapse_fillets_i_section_collapses_to_twelve_corners():
    poly = _i_with_fillets(h=200.0, b=100.0, t_w=5.6, t_f=8.5, r=2.0, n_arc=6)
    sig = compute_signature(poly, inner_polylines=[])
    assert sig["n_seg_pre_collapse"] > 16
    assert sig["n_seg_post_collapse"] == 12
    assert sig["n_seg"] == 12
    assert sig["sym_x"] is True
    assert sig["sym_y"] is True


def test_collapse_fillets_u_section_collapses_to_eight_corners():
    poly = _u_with_fillets(h=100.0, b=50.0, t_w=6.0, t_f=8.5, r=2.0, n_arc=6)
    sig = compute_signature(poly, inner_polylines=[])
    assert sig["n_seg_post_collapse"] == 8
    assert sig["sym_x"] is True
    assert sig["sym_y"] is False


def test_collapse_fillets_l_section_collapses_to_six_corners():
    poly = _l_with_fillet(h=50.0, b=50.0, t=5.0, r=2.0, n_arc=6)
    sig = compute_signature(poly, inner_polylines=[])
    assert sig["n_seg_post_collapse"] == 6


def test_collapse_fillets_t_section_collapses_to_eight_corners():
    poly = _t_with_fillets(h=100.0, b=80.0, t_w=8.0, t_f=10.0, r=2.0, n_arc=6)
    sig = compute_signature(poly, inner_polylines=[])
    assert sig["n_seg_post_collapse"] == 8
    assert sig["sym_y"] is True
    assert sig["sym_x"] is False


def test_collapse_fillets_circle_is_not_degenerated():
    """A 24-gon approximation of a circle must survive fillet collapse: the
    chord-length skip protects large-radius profiles like CHS, so the
    outer_circular flag stays True and the polyline keeps enough vertices
    for the matcher."""

    poly = _circle_polyline(radius=50.0, n=24)
    sig = compute_signature(poly, inner_polylines=[])
    assert sig["outer_circular"] is True
    # Collapse must not degenerate the polyline into a point/line.
    collapsed = collapse_fillets(poly)
    assert len(collapsed) >= 3
    # n_seg should remain non-trivial -- either equal-ish or larger.
    assert sig["n_seg"] >= 3


def test_collapse_fillets_rectangle_is_unchanged():
    """A polyline that has no fillet runs (all sharp 90 deg corners, all
    long edges) must come back identical to the input."""

    rect = [(-50.0, -30.0), (50.0, -30.0), (50.0, 30.0), (-50.0, 30.0)]
    collapsed = collapse_fillets(rect)
    assert collapsed == [tuple(p) for p in rect]


def test_collapse_fillets_is_deterministic():
    """Repeated invocations on the same input must yield byte-identical
    results."""

    poly = _i_with_fillets()
    a = collapse_fillets(poly)
    b = collapse_fillets(poly)
    assert a == b


def test_compute_signature_records_pre_and_post_collapse_counts():
    """The signature dict exposes both vertex counts for diagnostics."""

    poly = _i_with_fillets(r=2.0, n_arc=6)
    sig = compute_signature(poly, inner_polylines=[])
    assert "n_seg_pre_collapse" in sig and "n_seg_post_collapse" in sig
    assert sig["n_seg_pre_collapse"] > sig["n_seg_post_collapse"]
    assert sig["n_seg"] == sig["n_seg_post_collapse"]
