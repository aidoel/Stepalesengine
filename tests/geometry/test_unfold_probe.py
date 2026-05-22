"""Tests for the sheet-metal UnfoldProbe."""

from __future__ import annotations

import math

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeWire,
)
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakePrism,
)
from OCP.GC import GC_MakeArcOfCircle, GC_MakeSegment
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.TopAbs import TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

from manufacturing_pipeline.geometry.types import UnfoldStatus
from manufacturing_pipeline.geometry.unfold_probe import (
    Bend,
    BendDetector,
    UnfoldProbe,
    _classify_faces,
    _identify_sheet,
    _PlanarFace,
    _select_base_face,
)

# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------


def _first_solid(shape):
    if shape.ShapeType() == TopAbs_SOLID:
        return shape
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    if not exp.More():
        raise AssertionError("no solid found in shape")
    return TopoDS.Solid_s(exp.Current())


def _build_l_bracket(
    t: float = 2.0,
    R: float = 3.0,
    L1: float = 50.0,
    L2: float = 30.0,
    width: float = 40.0,
):
    cx = L1 - t - R
    cz = t + R
    ro = R + t
    sq2 = math.sqrt(2)

    def pnt(x, z):
        return gp_Pnt(x, 0.0, z)

    p_a = pnt(0, 0)
    p_b = pnt(cx, 0)
    p_c = pnt(L1, cz)
    p_d = pnt(L1, cz + L2)
    p_e = pnt(L1 - t, cz + L2)
    p_f = pnt(L1 - t, cz)
    p_g = pnt(cx, t)
    p_h = pnt(0, t)
    mid_outer = pnt(cx + ro / sq2, cz - ro / sq2)
    mid_inner = pnt(cx + R / sq2, cz - R / sq2)

    edges = [
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_a, p_b).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(p_b, mid_outer, p_c).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_c, p_d).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_d, p_e).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_e, p_f).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(p_f, mid_inner, p_g).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_g, p_h).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_h, p_a).Value()).Edge(),
    ]
    mw = BRepBuilderAPI_MakeWire()
    for e in edges:
        mw.Add(e)
    face = BRepBuilderAPI_MakeFace(mw.Wire()).Face()
    prism = BRepPrimAPI_MakePrism(face, gp_Vec(0, width, 0)).Shape()
    return _first_solid(prism)


def _build_u_channel(
    t: float = 2.0,
    R: float = 3.0,
    base_length: float = 60.0,
    leg_length: float = 20.0,
    width: float = 40.0,
):
    ro = R + t
    cx_L = ro
    cx_R = base_length - ro
    cz_bend = ro
    sq2 = math.sqrt(2)

    def pnt(x, z):
        return gp_Pnt(x, 0.0, z)

    p_bl_start = pnt(cx_L, 0)
    p_br_end = pnt(cx_R, 0)
    p_br_top = pnt(base_length, cz_bend)
    p_top_r = pnt(base_length, cz_bend + leg_length)
    p_top_r_inner = pnt(base_length - t, cz_bend + leg_length)
    p_inner_r_bot = pnt(base_length - t, cz_bend)
    p_inner_bot_r = pnt(cx_R, t)
    p_inner_bot_l = pnt(cx_L, t)
    p_inner_l_bot = pnt(t, cz_bend)
    p_top_l_inner = pnt(t, cz_bend + leg_length)
    p_top_l = pnt(0, cz_bend + leg_length)
    p_bl_top = pnt(0, cz_bend)
    edges = [
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_bl_start, p_br_end).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(
            GC_MakeArcOfCircle(p_br_end, pnt(cx_R + ro / sq2, cz_bend - ro / sq2), p_br_top).Value()
        ).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_br_top, p_top_r).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_top_r, p_top_r_inner).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_top_r_inner, p_inner_r_bot).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(
            GC_MakeArcOfCircle(
                p_inner_r_bot, pnt(cx_R + R / sq2, cz_bend - R / sq2), p_inner_bot_r
            ).Value()
        ).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_inner_bot_r, p_inner_bot_l).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(
            GC_MakeArcOfCircle(
                p_inner_bot_l, pnt(cx_L - R / sq2, cz_bend - R / sq2), p_inner_l_bot
            ).Value()
        ).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_inner_l_bot, p_top_l_inner).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_top_l_inner, p_top_l).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_top_l, p_bl_top).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(
            GC_MakeArcOfCircle(
                p_bl_top, pnt(cx_L - ro / sq2, cz_bend - ro / sq2), p_bl_start
            ).Value()
        ).Edge(),
    ]
    mw = BRepBuilderAPI_MakeWire()
    for e in edges:
        mw.Add(e)
    face = BRepBuilderAPI_MakeFace(mw.Wire()).Face()
    prism = BRepPrimAPI_MakePrism(face, gp_Vec(0, width, 0)).Shape()
    return _first_solid(prism)


def _build_wedge(
    length: float = 100.0, t_left: float = 1.0, t_right: float = 5.0, width: float = 40.0
):
    p1 = gp_Pnt(0, 0, 0)
    p2 = gp_Pnt(length, 0, 0)
    p3 = gp_Pnt(length, 0, t_right)
    p4 = gp_Pnt(0, 0, t_left)
    edges = [
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p1, p2).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p2, p3).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p3, p4).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p4, p1).Value()).Edge(),
    ]
    mw = BRepBuilderAPI_MakeWire()
    for e in edges:
        mw.Add(e)
    face = BRepBuilderAPI_MakeFace(mw.Wire()).Face()
    return _first_solid(BRepPrimAPI_MakePrism(face, gp_Vec(0, width, 0)).Shape())


def _build_closed_tube(r_outer: float = 10.0, r_inner: float = 8.0, height: float = 30.0):
    outer = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), r_outer, height
    ).Solid()
    inner = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), r_inner, height
    ).Solid()
    return _first_solid(BRepAlgoAPI_Cut(outer, inner).Shape())


def _build_rounded_corner_plate(
    length: float = 80.0,
    width: float = 50.0,
    t: float = 2.0,
    corner_r: float = 6.0,
    holes: tuple[tuple[float, float, float], ...] = (),
):
    """A flat plate whose four vertical corner edges are rounded.

    This is the geometry that exposed the corner-fillet bug: each rounded
    corner is a vertical quarter-cylinder whose axis runs along the sheet
    normal. The probe must not mistake those cylinders for bends.
    """

    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.GeomAbs import GeomAbs_Line
    from OCP.TopAbs import TopAbs_EDGE

    box = BRepPrimAPI_MakeBox(length, width, t).Solid()
    fil = BRepFilletAPI_MakeFillet(box)
    exp = TopExp_Explorer(box, TopAbs_EDGE)
    while exp.More():
        edge = TopoDS.Edge_s(exp.Current())
        try:
            curve = BRepAdaptor_Curve(edge)
            if curve.GetType() == GeomAbs_Line:
                p0 = curve.Value(curve.FirstParameter())
                p1 = curve.Value(curve.LastParameter())
                # A vertical edge varies only in Z.
                if (
                    abs(p1.Z() - p0.Z()) > 1e-6
                    and abs(p1.X() - p0.X()) < 1e-6
                    and abs(p1.Y() - p0.Y()) < 1e-6
                ):
                    fil.Add(corner_r, edge)
        except Exception:
            pass
        exp.Next()
    fil.Build()
    current = _first_solid(fil.Shape())
    for cx, cy, dia in holes:
        axis = gp_Ax2(gp_Pnt(cx, cy, -1.0), gp_Dir(0, 0, 1))
        cyl = BRepPrimAPI_MakeCylinder(axis, dia / 2.0, t + 2.0).Solid()
        current = _first_solid(BRepAlgoAPI_Cut(current, cyl).Shape())
    return current


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_flat_plate_unfolds_to_itself():
    plate = BRepPrimAPI_MakeBox(100.0, 60.0, 2.0).Solid()
    res = UnfoldProbe().run(plate)
    assert res.status == UnfoldStatus.SUCCESS
    assert res.n_bends == 0
    assert res.flat_area == pytest.approx(100.0 * 60.0, rel=1e-3)
    assert res.thickness_mean == pytest.approx(2.0, abs=1e-6)


def test_l_bracket_one_bend():
    solid = _build_l_bracket(t=2.0, R=3.0, L1=50.0, L2=30.0, width=40.0)
    res = UnfoldProbe().run(solid)
    assert res.status == UnfoldStatus.SUCCESS
    assert res.n_bends == 1
    # The flat portions of the outer surface have length L1 - (R+t) = 45 and
    # L2 - (R+t)? No - the OUTER flat sections of the L-bracket extend the
    # full L1 and L2 dimensions because the bend's outer arc starts at the
    # corner of the outer faces. So the outer flats are length L1 and L2.
    # Verify: BA = pi/2 * (R + K*t) = pi/2 * (3 + 0.44*2) = pi/2 * 3.88
    k = 0.44
    BA = (math.pi / 2.0) * (3.0 + k * 2.0)
    expected_flat = (45.0 * 40.0) + (BA * 40.0) + (30.0 * 40.0)
    assert res.flat_area == pytest.approx(expected_flat, rel=5e-2)


def test_u_channel_two_bends():
    solid = _build_u_channel()
    res = UnfoldProbe().run(solid)
    assert res.status == UnfoldStatus.SUCCESS
    assert res.n_bends == 2


def test_thick_walled_tube_fails_as_cyclic_or_closed():
    """A thick-walled tube (wall outside the sheet-metal range) is solid stock,
    not a rolled sheet, and must keep failing as a closed body."""

    solid = _build_closed_tube(r_outer=30.0, r_inner=8.0, height=200.0)
    res = UnfoldProbe().run(solid)
    assert res.status == UnfoldStatus.FAILURE
    assert res.reason.startswith("cyclic") or "closed" in (res.reason or "")


def test_thin_walled_tube_unfolds_as_rolled_section():
    """A sheet rolled smoothly into a thin-walled tube is still developable.

    The cylindrical area dwarfs the planar area, so the closed-body heuristic
    flags it - but a single coaxial inner/outer wall pair one sheet thickness
    apart is a rolled section: slit the seam and it flattens to a rectangle.
    The probe reports PARTIAL with seamed_section and rolled_tube flags.
    """

    solid = _build_closed_tube(r_outer=10.0, r_inner=8.0, height=30.0)
    res = UnfoldProbe().run(solid)
    assert res.status == UnfoldStatus.PARTIAL, (
        f"thin-walled rolled tube should unfold as a seamed section, "
        f"got {res.status} ({res.reason})"
    )
    assert res.flags.get("rolled_tube") is True
    assert res.flags.get("seamed_section") is True
    assert "seamed_section" in (res.reason or "")
    assert math.isclose(res.thickness_mean, 2.0, abs_tol=0.05)
    # Flat blank: unrolled circumference (2*pi*mean_radius) by the length.
    expected_area = 2.0 * math.pi * 9.0 * 30.0
    assert math.isclose(res.flat_area, expected_area, rel_tol=0.02)


def test_tapered_part_reports_thickness_variation():
    solid = _build_wedge(length=100.0, t_left=1.0, t_right=5.0)
    res = UnfoldProbe().run(solid)
    assert res.status in (UnfoldStatus.PARTIAL, UnfoldStatus.FAILURE)
    assert "thickness" in (res.reason or "").lower()


def test_compute_flat_pattern_on_l_bracket():
    solid = _build_l_bracket()
    pattern = UnfoldProbe().compute_flat_pattern(solid)
    assert pattern  # non-empty dict
    assert pattern["outer_contour"]
    # outer_contour is list[list[(x,y)]] - at least one polygon with >= 3 points
    assert len(pattern["outer_contour"][0]) >= 3
    assert pattern["thickness"] == pytest.approx(2.0, abs=1e-6)


def test_compute_flat_pattern_returns_empty_on_failure():
    solid = _build_closed_tube(r_outer=30.0, r_inner=8.0, height=200.0)
    pattern = UnfoldProbe().compute_flat_pattern(solid)
    assert pattern == {}


def test_probe_never_raises_on_bogus_input():
    """The probe must never raise; it should report FAILURE on garbage input."""

    res = UnfoldProbe().run(None)
    assert res.status == UnfoldStatus.FAILURE


def test_bend_detector_returns_bends_list():
    solid = _build_l_bracket()
    bends = BendDetector().detect(solid)
    assert isinstance(bends, list)
    assert len(bends) >= 1
    for b in bends:
        assert isinstance(b, Bend)
        assert 0.0 <= b.angle_deg <= 180.0
        assert b.length > 0


# ---------------------------------------------------------------------------
# True flat-pattern tests (Phase 5.2 of the master plan)
# ---------------------------------------------------------------------------


def _polygon_area(poly):
    """Shoelace area of a closed 2D polygon (not closed; returns positive area)."""

    a = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def _build_plate_with_hole(
    L: float = 100.0,
    W: float = 60.0,
    t: float = 2.0,
    r: float = 5.0,
):
    """Box L x W x t with a through cylindrical hole of radius r centered."""
    plate = BRepPrimAPI_MakeBox(L, W, t).Solid()
    hole = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(L / 2.0, W / 2.0, -1.0), gp_Dir(0, 0, 1)), r, t + 2.0
    ).Solid()
    cut = BRepAlgoAPI_Cut(plate, hole).Shape()
    return _first_solid(cut)


def test_flat_pattern_flat_plate():
    """Test 1: flat plate (100 x 60 x 2) -> one outer contour, bbox ~ 100x60."""

    plate = BRepPrimAPI_MakeBox(100.0, 60.0, 2.0).Solid()
    pattern = UnfoldProbe().compute_flat_pattern(plate)

    assert pattern, "flat plate should produce a non-empty pattern"
    assert len(pattern["outer_contour"]) == 1
    assert pattern["bend_lines"] == []
    assert pattern["thickness"] == pytest.approx(2.0, abs=1e-6)

    xmin, ymin, xmax, ymax = pattern["bbox"]
    assert (xmax - xmin) == pytest.approx(100.0, rel=1e-3)
    assert (ymax - ymin) == pytest.approx(60.0, rel=1e-3)

    area = _polygon_area(pattern["outer_contour"][0])
    assert area == pytest.approx(100.0 * 60.0, rel=1e-3)


def test_flat_pattern_l_bracket_area_and_bend_line():
    """Test 2: L-bracket area within 1% of A1 + A2 + BA*L, 1 bend line."""

    t = 2.0
    R = 3.0
    L1 = 50.0
    L2 = 30.0
    width = 40.0
    solid = _build_l_bracket(t=t, R=R, L1=L1, L2=L2, width=width)
    pattern = UnfoldProbe().compute_flat_pattern(solid)

    assert pattern, "L-bracket should produce a non-empty pattern"
    assert pattern["thickness"] == pytest.approx(t, abs=1e-6)
    assert len(pattern["bend_lines"]) == 1, "L-bracket has exactly one bend"

    # A1 (outer of horizontal flange) and A2 (outer of vertical leg).
    A1 = (L1 - t - R) * width  # 45 * 40
    A2 = L2 * width  # 30 * 40
    k = 0.44
    BA = (math.pi / 2.0) * (R + k * t)
    expected = A1 + A2 + BA * width

    outer_area = sum(_polygon_area(p) for p in pattern["outer_contour"])
    hole_area = sum(_polygon_area(h) for h in pattern["holes"])
    total_area = outer_area - hole_area

    assert total_area >= (A1 + A2) - 1e-6, (
        f"unfolded area {total_area:.2f} must be >= flat areas {A1 + A2:.2f}"
    )
    assert total_area == pytest.approx(expected, rel=1e-2), (
        f"unfolded area {total_area:.2f} != A1+A2+BA*L = {expected:.2f}"
    )


def test_flat_pattern_u_channel():
    """Test 3: U-channel -> 2 bend lines, bbox longer than base flange."""

    base_length = 60.0
    leg = 20.0
    width = 40.0
    solid = _build_u_channel(t=2.0, R=3.0, base_length=base_length, leg_length=leg, width=width)
    pattern = UnfoldProbe().compute_flat_pattern(solid)

    assert pattern
    assert len(pattern["bend_lines"]) == 2

    xmin, _ymin, xmax, _ymax = pattern["bbox"]
    bbox_length = xmax - xmin
    # When unfolded, the U should have length > base flange length.
    assert bbox_length > base_length, (
        f"unfolded U bbox length {bbox_length:.2f} should exceed base {base_length:.2f}"
    )


def test_flat_pattern_plate_with_circular_hole():
    """Test 4: flat plate WITH a circular hole -> outer = 1, holes = 1, hole area ~ pi r^2."""

    r = 5.0
    solid = _build_plate_with_hole(L=100.0, W=60.0, t=2.0, r=r)
    pattern = UnfoldProbe().compute_flat_pattern(solid)

    assert pattern, "plate-with-hole should produce a pattern"
    assert len(pattern["outer_contour"]) == 1
    assert len(pattern["holes"]) == 1

    hole_area = _polygon_area(pattern["holes"][0])
    expected_hole_area = math.pi * r * r
    # 0.5 mm chord tol on a 5 mm circle leaves a ~2.5% area shortfall; allow 5%.
    assert hole_area == pytest.approx(expected_hole_area, rel=5e-2)


def test_flat_pattern_closed_tube_returns_empty():
    """Test 5: a thick-walled closed tube fails the unfold -> empty dict."""

    solid = _build_closed_tube(r_outer=30.0, r_inner=8.0, height=200.0)
    pattern = UnfoldProbe().compute_flat_pattern(solid)
    assert pattern == {}


# ---------------------------------------------------------------------------
# Regression: base-face selection when areas tie (issue: L-bracket n_bends=0)
# ---------------------------------------------------------------------------


def test_lbracket_base_face_tie_break():
    """An L-bracket with equal-area inner/outer flange faces still finds the bend.

    Choose ``L1 == L2`` so the longer flange's inner and outer flats are
    perfectly symmetric. Pre-fix the BFS base tied to the inner-top face,
    whose neighbours never reach the outer-cylinder bend, so the probe
    reported ``n_bends=0``. With the bend-participation tie-breaker the
    base should land on a face that reaches the bend.
    """

    solid = _build_l_bracket(t=2.0, R=3.0, L1=40.0, L2=40.0, width=40.0)
    probe = UnfoldProbe()
    res = probe.run(solid)
    assert res.status == UnfoldStatus.SUCCESS
    assert res.n_bends == 1, (
        f"L-bracket with symmetric legs must yield n_bends=1, got {res.n_bends}"
    )
    # Sanity: the unfolded flat area exceeds the area of a single flange.
    assert res.flat_area > 40.0 * 40.0, (
        f"flat_area {res.flat_area:.2f} should exceed single-flange area 1600"
    )


def test_base_face_prefers_bend_participation():
    """Among three near-equal-area planars, prefer the one that bends.

    Unit-test the ``_select_base_face`` helper with three synthetic planar
    faces of (nearly) identical area where only one participates in a
    detected bend. The selector must pick that face.
    """

    p0 = _PlanarFace(
        face=object(), index=0, normal=(0.0, 0.0, 1.0), centroid=(0.0, 0.0, 0.0), area=100.0
    )
    p1 = _PlanarFace(
        face=object(), index=1, normal=(0.0, 0.0, -1.0), centroid=(1.0, 0.0, 0.0), area=100.0
    )
    # Slight area perturbation well within the 0.5% tie-band.
    p2 = _PlanarFace(
        face=object(), index=2, normal=(1.0, 0.0, 0.0), centroid=(2.0, 0.0, 0.0), area=99.8
    )
    planars = [p0, p1, p2]
    # Only p1 participates in the bend.
    bend = Bend(
        p_in_index=1,
        p_out_index=2,
        cyl_index=0,
        axis_dir=(0.0, 1.0, 0.0),
        axis_loc=(0.0, 0.0, 0.0),
        angle_rad=math.pi / 2.0,
        radius=1.0,
        length=10.0,
    )
    chosen = _select_base_face(planars, [bend])
    assert chosen == 1, (
        f"base selector should pick the bend-participating face (idx=1), got {chosen}"
    )

    # End-to-end: build a U-channel and confirm the probe's chosen base
    # participates in a bend by checking that n_bends >= 1 and the flat
    # area exceeds the largest single face area (i.e. the BFS actually
    # traversed at least one bend strip + a second face).
    u_solid = _build_u_channel(t=2.0, R=3.0, base_length=60.0, leg_length=20.0, width=40.0)
    u_probe = UnfoldProbe()
    u_res = u_probe.run(u_solid)
    assert u_res.n_bends >= 1
    # Largest face area lower bound: the U-channel's outer base is
    # ``base_length * width = 60 * 40 = 2400``. The unfold must add
    # more than that (legs + bend allowances).
    assert u_res.flat_area > 60.0 * 40.0
    # Debug introspection: the chosen base must be a face that
    # participates in at least one bend.
    assert u_probe._last_base_face_id >= 0


# ---------------------------------------------------------------------------
# Branching (T-bracket) and joggle (Z-bend) detection
# ---------------------------------------------------------------------------


def _build_l_bracket_yz(
    t: float = 2.0,
    R: float = 3.0,
    L1: float = 40.0,
    L2: float = 15.0,
    width: float = 20.0,
    x_origin: float = 0.0,
    y_origin: float = 0.0,
):
    """L-bracket whose hinge axis lies along the X axis.

    Profile sits in the YZ plane and is prismed along +X for ``width``. The
    base extends y ∈ [y_origin, y_origin + L1] at z ∈ [0, t]; the leg sticks
    up at the far Y edge.
    """
    cy = L1 - t - R
    cz = t + R
    ro = R + t
    sq2 = math.sqrt(2)

    def pnt(y, z):
        return gp_Pnt(x_origin, y_origin + y, z)

    p_a = pnt(0, 0)
    p_b = pnt(cy, 0)
    p_c = pnt(L1, cz)
    p_d = pnt(L1, cz + L2)
    p_e = pnt(L1 - t, cz + L2)
    p_f = pnt(L1 - t, cz)
    p_g = pnt(cy, t)
    p_h = pnt(0, t)
    mid_outer = pnt(cy + ro / sq2, cz - ro / sq2)
    mid_inner = pnt(cy + R / sq2, cz - R / sq2)
    edges = [
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_a, p_b).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(p_b, mid_outer, p_c).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_c, p_d).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_d, p_e).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_e, p_f).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(p_f, mid_inner, p_g).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_g, p_h).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p_h, p_a).Value()).Edge(),
    ]
    mw = BRepBuilderAPI_MakeWire()
    for e in edges:
        mw.Add(e)
    face = BRepBuilderAPI_MakeFace(mw.Wire()).Face()
    prism = BRepPrimAPI_MakePrism(face, gp_Vec(width, 0, 0)).Shape()
    return _first_solid(prism)


def _build_t_bracket():
    """T-shaped bracket: U-channel base fused with a perpendicular flange.

    A U-channel supplies two bends (left and right legs) whose hinge axis is
    along Y. A third flange is added by fusing in a perpendicular L-bracket
    whose hinge axis lies along X. The L's base is offset in Y so its bend
    axis sits at the far edge of the U-channel base, which gives the third
    bend its full 90 deg fillet (no truncation against the central plate)
    and registers the shared base face as having three bend neighbours.
    """
    t = 2.0
    R = 3.0
    u = _build_u_channel(t=t, R=R, base_length=60.0, leg_length=20.0, width=40.0)
    # Place the L-bracket so its bend axis aligns with the U-channel base's
    # far edge (y = W = 40). For y_origin + (L1 - t - R) = 40 with L1 = 15
    # the offset is 30.
    lyz = _build_l_bracket_yz(
        t=t,
        R=R,
        L1=15.0,
        L2=15.0,
        width=20.0,
        x_origin=20.0,
        y_origin=30.0,
    )
    fused = BRepAlgoAPI_Fuse(u, lyz).Shape()
    upg = ShapeUpgrade_UnifySameDomain(fused, True, True, True)
    upg.Build()
    return _first_solid(upg.Shape())


def _build_z_bend(
    t: float = 2.0,
    R: float = 3.0,
    L_lower: float = 30.0,
    H_vert: float = 15.0,
    L_upper: float = 30.0,
    width: float = 40.0,
):
    """Serial Z-bend / joggle profile in XZ plane, prismed along +Y.

    The strip goes: lower flat (z=0..t) -> bend 1 (90 deg up) -> vertical
    middle (height ``H_vert``) -> bend 2 (90 deg right, opposite sense to
    bend 1) -> upper flat at z = z2..(z2+t). Both bends share the central
    vertical strip as a flat face, putting them in series along the unfold
    chain - exactly the topology a joggle detector needs to see.
    """
    sq2 = math.sqrt(2)
    a1 = L_lower
    z_bend2 = t + R + H_vert

    def pnt(x, z):
        return gp_Pnt(x, 0.0, z)

    p1 = pnt(0, 0)
    p2 = pnt(a1, 0)
    mid1 = pnt(a1 + (R + t) / sq2, (t + R) - (R + t) / sq2)
    p3 = pnt(a1 + R + t, t + R)
    p4 = pnt(a1 + R + t, z_bend2)
    mid2 = pnt(a1 + (R + t) / sq2, z_bend2 + (R + t) / sq2)
    p5 = pnt(a1, z_bend2 + R + t)
    p6 = pnt(a1 + L_upper, z_bend2 + R + t)
    p7 = pnt(a1 + L_upper, z_bend2 + R)
    p8 = pnt(a1, z_bend2 + R)
    mid2_inner = pnt(a1 + R / sq2, z_bend2 + R / sq2)
    p9 = pnt(a1 + R, z_bend2)
    p10 = pnt(a1 + R, t + R)
    mid1_inner = pnt(a1 + R / sq2, (t + R) - R / sq2)
    p11 = pnt(a1, t)
    p12 = pnt(0, t)

    edges = [
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p1, p2).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(p2, mid1, p3).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p3, p4).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(p4, mid2, p5).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p5, p6).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p6, p7).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p7, p8).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(p8, mid2_inner, p9).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p9, p10).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(p10, mid1_inner, p11).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p11, p12).Value()).Edge(),
        BRepBuilderAPI_MakeEdge(GC_MakeSegment(p12, p1).Value()).Edge(),
    ]
    mw = BRepBuilderAPI_MakeWire()
    for e in edges:
        mw.Add(e)
    face = BRepBuilderAPI_MakeFace(mw.Wire()).Face()
    prism = BRepPrimAPI_MakePrism(face, gp_Vec(0, width, 0)).Shape()
    return _first_solid(prism)


def _build_z_purlin(
    t: float = 3.0,
    R: float = 4.0,
    flange: float = 50.0,
    web_h: float = 80.0,
    width: float = 240.0,
):
    """A Z-section / Z-purlin: bottom flange one way, web, top flange the other.

    The two bends fold in *opposite* directions, so they attach to opposite
    faces of the middle web and the bend graph splits into components a single
    BFS cannot span. Pre-fix the flat pattern only covered the base face's
    component, dropping the far flange.

    The cross-section is a constant-thickness closed polygon in the XZ plane;
    the inner (concave) corner of each fold is filleted with ``R`` and the
    matching outer (convex) corner with ``R + t`` so the sheet stays constant
    thickness. The face is then prismed ``width`` mm along +Y. This mirrors
    the proven Z-purlin recipe in ``corpus_steps`` (``build_zbracket`` plus
    ``sheet_metal_profile``); it is inlined here because ``corpus_steps/`` is
    gitignored and must not be imported.
    """
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet2d
    from OCP.TopAbs import TopAbs_VERTEX

    # Web vertical strip: outer face at x=0, inner face at x=t. Bottom flange
    # extends in -X from the web bottom; top flange extends in +X from the
    # web top.
    xb = -flange  # far end of bottom flange
    xt = flange + t  # far end of top flange
    z0 = 0.0
    z1 = t  # top of bottom flange
    z2 = web_h  # bottom of top flange
    z3 = web_h + t  # top of top flange

    # Closed CCW polygon (first vertex not repeated).
    section = [
        (xb, z0),  # 0  outer corner, bottom flange far bottom
        (t, z0),  # 1  outer corner, bottom flange right bottom (BEND A outer)
        (t, z2),  # 2  inner corner, web meets top flange (BEND B inner)
        (xt, z2),  # 3  inner corner, top flange far underside
        (xt, z3),  # 4  outer corner, top flange far top
        (0.0, z3),  # 5  outer corner, top flange / web (BEND B outer)
        (0.0, z1),  # 6  inner corner, web meets bottom flange (BEND A inner)
        (xb, z1),  # 7  inner corner, bottom flange far top
    ]
    # Fold A (bottom flange / web): outer convex corner 1 -> R+t, inner 6 -> R.
    # Fold B (web / top flange): outer convex corner 5 -> R+t, inner 2 -> R.
    bends = [(1, R + t), (6, R), (5, R + t), (2, R)]

    poly = BRepBuilderAPI_MakePolygon()
    for x, z in section:
        poly.Add(gp_Pnt(float(x), 0.0, float(z)))
    poly.Close()
    if not poly.IsDone():
        raise AssertionError("Z-purlin cross-section polygon build failed")
    face = BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()

    targets = {
        (round(float(section[i][0]), 3), round(float(section[i][1]), 3)): float(rad)
        for i, rad in bends
    }
    mf = BRepFilletAPI_MakeFillet2d(face)
    seen: set[tuple[float, float]] = set()
    exp = TopExp_Explorer(face, TopAbs_VERTEX)
    while exp.More():
        vtx = TopoDS.Vertex_s(exp.Current())
        pnt = BRep_Tool.Pnt_s(vtx)
        key = (round(pnt.X(), 3), round(pnt.Z(), 3))
        radius = targets.get(key)
        if radius is not None and key not in seen:
            seen.add(key)
            mf.AddFillet(vtx, radius)
        exp.Next()
    mf.Build()
    if not mf.IsDone():
        raise AssertionError("fillet2d on Z-purlin cross-section failed")
    face = TopoDS.Face_s(mf.Shape())

    prism = BRepPrimAPI_MakePrism(face, gp_Vec(0.0, float(width), 0.0)).Shape()
    return _first_solid(prism)


def test_z_section_fully_unfolds_spanning_all_three_segments():
    """A Z-purlin develops to the full flat blank: flange + web + flange.

    Its two bends fold in opposite directions, so they attach to opposite
    faces of the middle web and the bend graph splits into two components.
    Pre-fix a single unfold BFS could not bridge them and the far flange was
    dropped. The fix bridges an unreached component through the twin face of
    an already-placed segment, so the developed blank now spans all three
    segments.
    """
    flange = 50.0
    web_h = 80.0
    width = 240.0
    solid = _build_z_purlin(t=3.0, R=4.0, flange=flange, web_h=web_h, width=width)

    pattern = UnfoldProbe().compute_flat_pattern(solid)
    assert pattern, "Z-purlin should produce a non-empty flat pattern"
    assert len(pattern["bend_lines"]) == 2, (
        f"Z-purlin has exactly two bend lines, got {len(pattern['bend_lines'])}"
    )

    # The developed extent must span flange + web + flange. The blank's
    # in-section dimension runs perpendicular to the prism (+Y) axis; the
    # other axis is just the prism width. Take whichever side is NOT the
    # ~width axis as the developed (unrolled section) extent.
    xmin, ymin, xmax, ymax = pattern["bbox"]
    dx = xmax - xmin
    dy = ymax - ymin
    width_side = min((dx, dy), key=lambda d: abs(d - width))
    developed = dx if width_side is dy else dy

    # Pre-fix the far flange was dropped, so the developed extent covered at
    # most web + one flange. A fully-unfolded Z spans web + both flanges
    # (minus a little bend-allowance shortfall), which clearly exceeds it.
    assert developed > web_h + flange, (
        f"developed extent {developed:.2f} mm should exceed web + one flange "
        f"({web_h + flange:.2f} mm); the far flange looks dropped"
    )


def test_t_bracket_is_partial_with_branching_flag():
    """T-shaped bracket: a planar face has 3 bend neighbours -> PARTIAL."""
    solid = _build_t_bracket()
    res = UnfoldProbe().run(solid)
    assert res.status == UnfoldStatus.PARTIAL
    assert res.flags.get("branching") is True
    assert res.flags.get("branches_found", 0) >= 3
    # n_bends and a partial flat_area must still be reported.
    assert res.n_bends >= 3
    assert res.flat_area > 0.0
    # The reason token must contain ``branching``; it may be joined with
    # other reasons (e.g. thickness_variation) by ``; ``.
    assert "branching" in (res.reason or "")


def test_l_bracket_does_not_branch():
    """A single-bend L-bracket must NOT raise the branching flag."""
    solid = _build_l_bracket()
    res = UnfoldProbe().run(solid)
    assert res.flags.get("branching") is not True
    assert "branching" not in (res.reason or "")
    assert res.status == UnfoldStatus.SUCCESS


def test_u_channel_does_not_branch():
    """A 2-bend U-channel: max bend neighbours = 2, not branching."""
    solid = _build_u_channel()
    res = UnfoldProbe().run(solid)
    assert res.flags.get("branching") is not True
    assert "branching" not in (res.reason or "")
    assert res.status == UnfoldStatus.SUCCESS


def test_z_bend_flags_joggle():
    """Z-shape (serial joggle): two opposite-sign bends in a row -> joggle."""
    solid = _build_z_bend()
    res = UnfoldProbe().run(solid)
    assert res.status == UnfoldStatus.SUCCESS, (
        f"Z-bend should unfold cleanly, got {res.status} ({res.reason})"
    )
    assert res.flags.get("has_joggle") is True
    assert res.flags.get("n_joggles", 0) >= 1
    # Both bends must have been traversed.
    assert res.n_bends == 2


def test_u_channel_has_no_joggle():
    """U-channel: same-direction sibling bends -> NOT a joggle."""
    solid = _build_u_channel()
    res = UnfoldProbe().run(solid)
    assert res.flags.get("has_joggle") is not True
    assert res.status == UnfoldStatus.SUCCESS


# ---------------------------------------------------------------------------
# Rounded corners, sheet identification, thickness gate
# ---------------------------------------------------------------------------


def test_rounded_corner_plate_unfolds_with_no_bends():
    """Regression: a flat plate with rounded corners unfolds as a flat plate.

    Each rounded corner is a vertical cylinder whose axis is parallel to the
    sheet normal. Before the sheet-model rewrite these were detected as bends,
    formed a loop, and the plate failed as ``cyclic_graph``.
    """

    solid = _build_rounded_corner_plate(length=80.0, width=50.0, t=2.0, corner_r=6.0)
    res = UnfoldProbe().run(solid)
    assert res.status == UnfoldStatus.SUCCESS, res.reason
    assert res.n_bends == 0
    assert res.thickness_mean == pytest.approx(2.0, abs=1e-6)


def test_rounded_corner_plate_with_holes_unfolds():
    """Rounded corners plus through-holes: still a no-bend flat plate."""

    solid = _build_rounded_corner_plate(
        length=80.0,
        width=50.0,
        t=2.0,
        corner_r=6.0,
        holes=((20.0, 25.0, 8.0), (60.0, 25.0, 8.0)),
    )
    res = UnfoldProbe().run(solid)
    assert res.status == UnfoldStatus.SUCCESS, res.reason
    assert res.n_bends == 0
    assert res.thickness_mean == pytest.approx(2.0, abs=1e-6)


def test_thick_block_is_not_sheet_metal():
    """A chunky machined block is too thick to be sheet metal."""

    block = BRepPrimAPI_MakeBox(120.0, 90.0, 35.0).Solid()
    res = UnfoldProbe().run(block)
    assert res.status == UnfoldStatus.FAILURE
    assert res.reason == "too_thick"


def test_identify_sheet_thickness_on_u_channel():
    """The two facing flanges of a U-channel straddle an air gap; the sheet
    model must report the material thickness, not the channel width."""

    solid = _build_u_channel(t=2.0, R=3.0, base_length=60.0, leg_length=20.0, width=40.0)
    planars, _cyls, _others, _ta, _pa, _oa = _classify_faces(solid)
    model = _identify_sheet(planars)
    assert model.thickness == pytest.approx(2.0, abs=0.2)


def test_identify_sheet_ignores_thin_sliver():
    """A flat plate with a tiny chamfer must still report the real thickness:
    the chamfer's faces are low-area and must not set the thickness."""

    from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer
    from OCP.TopAbs import TopAbs_EDGE

    box = BRepPrimAPI_MakeBox(80.0, 50.0, 3.0).Solid()
    cham = BRepFilletAPI_MakeChamfer(box)
    exp = TopExp_Explorer(box, TopAbs_EDGE)
    if exp.More():
        cham.Add(0.3, TopoDS.Edge_s(exp.Current()))
    cham.Build()
    solid = _first_solid(cham.Shape())
    planars, _cyls, _others, _ta, _pa, _oa = _classify_faces(solid)
    model = _identify_sheet(planars)
    assert model.thickness == pytest.approx(3.0, abs=0.3)


# ---------------------------------------------------------------------------
# Branched / star-shaped flange parts and rolled tubular sections
# ---------------------------------------------------------------------------


def _build_star_bracket():
    """A star-shaped flange part: one base plate with four bent flanges.

    Built by fusing two perpendicular U-channels that share the same base
    plate region. The base flange ends up incident to several bends, so the
    bend graph branches. The part is genuinely developable - it must NOT fail.
    """

    u1 = _build_u_channel(t=2.0, R=3.0, base_length=60.0, leg_length=20.0, width=40.0)
    # Second U-channel: base 40 x width 60 - its base overlaps u1's base in
    # the x[0,40], y[0,40] region, and its two legs run along the X-aligned
    # hinge axis, perpendicular to u1's legs.
    u2 = _build_u_channel(t=2.0, R=3.0, base_length=40.0, leg_length=20.0, width=60.0)
    fused = BRepAlgoAPI_Fuse(u1, u2).Shape()
    upg = ShapeUpgrade_UnifySameDomain(fused, True, True, True)
    upg.Build()
    return _first_solid(upg.Shape())


def _build_rect_tube(
    W: float = 60.0,
    H: float = 40.0,
    t: float = 3.0,
    length: float = 120.0,
    R: float = 3.0,
):
    """A sheet rolled / folded into a closed rectangular tube section.

    Outer box minus inner box gives a four-walled tube open at both ends; the
    four long corner edges are filleted into bends. The bend graph is a single
    simple cycle. The part is still developable: slit one bend (the seam) and
    flatten the rest - exactly what AutoPOL does. The probe must report this
    as PARTIAL with a ``seamed_section`` flag, not FAILURE.
    """

    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.GeomAbs import GeomAbs_Line
    from OCP.TopAbs import TopAbs_EDGE

    outer = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), W, H, length).Solid()
    inner = BRepPrimAPI_MakeBox(
        gp_Pnt(t, t, -1.0), W - 2 * t, H - 2 * t, length + 2.0
    ).Solid()
    tube = _first_solid(BRepAlgoAPI_Cut(outer, inner).Shape())

    fil = BRepFilletAPI_MakeFillet(tube)
    exp = TopExp_Explorer(tube, TopAbs_EDGE)
    while exp.More():
        edge = TopoDS.Edge_s(exp.Current())
        try:
            curve = BRepAdaptor_Curve(edge)
            if curve.GetType() == GeomAbs_Line:
                p0 = curve.Value(curve.FirstParameter())
                p1 = curve.Value(curve.LastParameter())
                # A long corner edge varies only in Z.
                if (
                    abs(p1.Z() - p0.Z()) > 1e-6
                    and abs(p1.X() - p0.X()) < 1e-6
                    and abs(p1.Y() - p0.Y()) < 1e-6
                ):
                    fil.Add(R, edge)
        except Exception:
            pass
        exp.Next()
    fil.Build()
    return _first_solid(fil.Shape())


def test_star_bracket_does_not_fail():
    """A star-shaped flange part (one base, several bent flanges) must not
    FAIL: branching is informational and downgrades only to PARTIAL."""

    solid = _build_star_bracket()
    res = UnfoldProbe().run(solid)
    assert res.status != UnfoldStatus.FAILURE, (
        f"star-shaped flange part should not fail, got {res.reason}"
    )
    # A base flange with three or more bend neighbours: branching flag set.
    assert res.flags.get("branching") is True
    assert res.n_bends >= 3
    assert res.flat_area > 0.0


def test_rect_tube_unfolds_as_seamed_section():
    """A sheet rolled into a closed rectangular tube is still developable.

    The bend graph is a single simple cycle; the probe slits one bend (the
    seam) and flattens the rest -> PARTIAL with a ``seamed_section`` flag.
    Before the seam fix this failed hard as ``cyclic_graph``.
    """

    solid = _build_rect_tube()
    res = UnfoldProbe().run(solid)
    assert res.status == UnfoldStatus.PARTIAL, (
        f"rolled rectangular tube should unfold as a seamed section, "
        f"got {res.status} ({res.reason})"
    )
    assert res.flags.get("seamed_section") is True
    assert "seamed_section" in (res.reason or "")
    # Four corner bends; all are reported even though one is the slit seam.
    assert res.n_bends == 4
    assert res.flat_area > 0.0


def test_closed_box_still_fails():
    """A genuinely closed thin-walled box is not a single simple cycle and
    must keep failing - the seam fix must not rescue non-developable bodies."""

    outer = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 60.0, 40.0, 30.0).Solid()
    inner = BRepPrimAPI_MakeBox(gp_Pnt(3, 3, 3), 54.0, 34.0, 24.0).Solid()
    closed = _first_solid(BRepAlgoAPI_Cut(outer, inner).Shape())
    res = UnfoldProbe().run(closed)
    assert res.status == UnfoldStatus.FAILURE


def test_solid_round_bar_still_fails():
    """A solid round bar has a single cylindrical wall and no inner bore, so it
    is not a rolled sheet. The rolled-tube rescue must not flatten solid stock."""

    bar = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 12.0, 80.0
    ).Solid()
    res = UnfoldProbe().run(bar)
    assert res.status == UnfoldStatus.FAILURE
    assert res.flags.get("rolled_tube") is None


def test_thick_walled_pipe_still_fails():
    """A pipe whose wall exceeds the sheet-metal range is plate / machining
    stock, not a rolled sheet, and must keep failing as a closed body."""

    pipe = _build_closed_tube(r_outer=60.0, r_inner=10.0, height=40.0)
    res = UnfoldProbe().run(pipe)
    assert res.status == UnfoldStatus.FAILURE
    assert res.flags.get("rolled_tube") is None
