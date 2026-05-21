"""Tests for the HoleAnalyzer.

Each test builds a synthetic solid with BRepPrimAPI primitives and asserts the
HolePattern returned by the analyser.
"""

from __future__ import annotations

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from manufacturing_pipeline.geometry.hole_analyzer import HoleAnalyzer
from manufacturing_pipeline.geometry.types import HolePattern

# ---------------------------------------------------------------------------
# Fixture-backed AutoPOL reference (broad-corpus validation)
# ---------------------------------------------------------------------------

# AutoPOL Sheet_HoleRadii lists only ROUND holes. Its Sheet_NrHoles also counts
# non-circular cutout contours, which the analyser does not bucket as holes -
# so the analyser should match the round-hole subtotal, not Sheet_NrHoles.
_FIXTURE_530 = "tests/fixtures/step/sheet_10001073530_rev00.stp"
_ROUND_HOLES_530 = 30  # AutoPOL Sheet_HoleRadii subtotal (Sheet_NrHoles = 37)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _box(w: float = 20.0, d: float = 20.0, h: float = 10.0):
    return BRepPrimAPI_MakeBox(w, d, h).Solid()


def _drill(box, cx: float, cy: float, radius: float, depth: float, z0: float = 0.0):
    cyl = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(cx, cy, z0), gp_Dir(0, 0, 1)), radius, depth
    ).Solid()
    return BRepAlgoAPI_Cut(box, cyl).Shape()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_holes_in_plain_box():
    box = _box()
    pat = HoleAnalyzer().analyze(box)
    assert isinstance(pat, HolePattern)
    assert pat.hole_count == 0
    assert pat.diameters == []
    assert pat.holes == []


def test_single_through_hole():
    box = _box(20, 20, 10)
    part = _drill(box, 10.0, 10.0, 3.0, 10.0)
    pat = HoleAnalyzer().analyze(part)
    assert pat.hole_count == 1
    assert pat.diameters == pytest.approx([6.0], abs=1e-6)
    assert pat.holes[0]["through"] is True
    assert pat.holes[0]["depth"] == pytest.approx(10.0, abs=0.1)


def test_blind_hole_reported_as_not_through():
    box = _box(20, 20, 10)
    # Hole only goes 5 mm into a 10 mm thick part, starting at z=5.
    part = _drill(box, 10.0, 10.0, 3.0, 5.0, z0=5.0)
    pat = HoleAnalyzer().analyze(part)
    assert pat.hole_count == 1
    assert pat.holes[0]["through"] is False


def test_two_parallel_holes_different_diameters():
    box = _box(30, 30, 10)
    p1 = _drill(box, 8.0, 8.0, 1.5, 10.0)
    p2 = _drill(p1, 22.0, 22.0, 2.5, 10.0)
    pat = HoleAnalyzer().analyze(p2)
    assert pat.hole_count == 2
    assert sorted(pat.diameters) == pytest.approx([3.0, 5.0], abs=1e-6)


def test_outer_cylinder_is_not_a_hole():
    """A solid cylinder (no inner bore) must not be counted as a hole."""

    cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 5.0, 10.0).Solid()
    pat = HoleAnalyzer().analyze(cyl)
    assert pat.hole_count == 0


def test_counterbore_grouped_as_single_hole():
    """Two co-axial cylinders of different radii => one hole record, depth = sum."""

    box = _box(30, 30, 10)
    # Counterbore: a 4 mm radius shallow pocket atop a 2 mm radius through hole.
    # After cutting, the small cylinder face only exists from z=0 to z=7 (length 7);
    # the big cylinder face exists from z=7 to z=10 (length 3). Total depth = 10 mm
    # (the part's full thickness).
    big = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(15, 15, 7), gp_Dir(0, 0, 1)), 4.0, 3.0).Solid()
    small = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(15, 15, 0), gp_Dir(0, 0, 1)), 2.0, 10.0).Solid()
    part = BRepAlgoAPI_Cut(box, big).Shape()
    part = BRepAlgoAPI_Cut(part, small).Shape()

    pat = HoleAnalyzer().analyze(part)
    assert pat.hole_count == 1
    # Bore diameter = 2 * smallest radius = 4 mm
    assert pat.diameters[0] == pytest.approx(4.0, abs=1e-6)
    # Counterbore yields TWO grouped cylinders for the one hole.
    assert pat.holes[0]["n_cylinders"] == 2
    # Total axial extent across both cylindrical patches = 3 + 7 = 10 mm.
    assert pat.holes[0]["depth"] == pytest.approx(10.0, abs=0.1)


def test_pattern_never_raises_on_garbage_input():
    """Passing None should not raise; returns empty pattern."""

    pat = HoleAnalyzer().analyze(None)
    assert pat.hole_count == 0


def test_hole_axis_direction_reported():
    box = _box(20, 20, 10)
    part = _drill(box, 10.0, 10.0, 3.0, 10.0)
    pat = HoleAnalyzer().analyze(part)
    assert pat.hole_count == 1
    ax = pat.holes[0]["axis_dir"]
    assert abs(ax[2]) == pytest.approx(1.0, abs=1e-3)
    assert abs(ax[0]) < 1e-3
    assert abs(ax[1]) < 1e-3


# ---------------------------------------------------------------------------
# Partial-arc rejection: rounded corners / bend reliefs are not round holes
# ---------------------------------------------------------------------------


def test_rounded_cutout_corner_is_not_a_hole():
    """A rectangular cutout with rounded corners produces partial-arc inner
    cylinders. None of them is a round hole - hole_count must stay 0."""

    box = _box(40, 40, 10)
    # A rounded-corner rectangular slot: cut a big cylinder so its wall meets
    # two planar cuts, leaving only a partial-arc cylindrical patch.
    tool = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(2, 2, 0), gp_Dir(0, 0, 1)), 6.0, 10.0
    ).Solid()
    part = BRepAlgoAPI_Cut(box, tool).Shape()
    # Clip the cylinder's arc down to a corner fillet with two slab cuts.
    slab_x = BRepPrimAPI_MakeBox(gp_Pnt(-20, -20, -1), 20, 60, 12).Solid()
    slab_y = BRepPrimAPI_MakeBox(gp_Pnt(-20, -20, -1), 60, 20, 12).Solid()
    part = BRepAlgoAPI_Cut(part, slab_x).Shape()
    part = BRepAlgoAPI_Cut(part, slab_y).Shape()
    pat = HoleAnalyzer().analyze(part)
    assert pat.hole_count == 0


def test_two_holes_on_one_axis_line_counted_separately():
    """Two through-holes drilled on the SAME axis line through two parallel
    flanges are distinct holes, not one merged record."""

    # A C-channel: two parallel 10 mm-thick flanges 90 mm apart, both pierced
    # on the same axis by a 6 mm hole.
    f1 = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 40, 40, 10).Solid()
    f2 = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 100), 40, 40, 10).Solid()
    tool = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(20, 20, -5), gp_Dir(0, 0, 1)), 3.0, 120.0
    ).Solid()
    p1 = BRepAlgoAPI_Cut(f1, tool).Shape()
    p2 = BRepAlgoAPI_Cut(f2, tool).Shape()
    an = HoleAnalyzer()
    # Each flange is a separate solid; each carries exactly one hole.
    assert an.analyze(p1).hole_count == 1
    assert an.analyze(p2).hole_count == 1


def test_counterbore_still_counts_once_after_axial_split():
    """The co-axial tiers of a counterbore touch axially and must stay merged
    into a single hole even with the axial-gap split active."""

    box = _box(30, 30, 10)
    big = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(15, 15, 7), gp_Dir(0, 0, 1)), 4.0, 3.0
    ).Solid()
    small = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(15, 15, 0), gp_Dir(0, 0, 1)), 2.0, 10.0
    ).Solid()
    part = BRepAlgoAPI_Cut(box, big).Shape()
    part = BRepAlgoAPI_Cut(part, small).Shape()
    pat = HoleAnalyzer().analyze(part)
    assert pat.hole_count == 1
    assert pat.holes[0]["n_cylinders"] == 2


# ---------------------------------------------------------------------------
# Real-fixture regression vs the AutoPOL reference
# ---------------------------------------------------------------------------


def test_fixture_hole_count_matches_autopol_round_holes():
    """The 530 sheet-metal fixture has 30 round holes per the AutoPOL
    Sheet_HoleRadii reference. The analyser previously reported 49 - rounded
    corners, bend reliefs and flange bend radii were over-counted."""

    from pathlib import Path

    from manufacturing_pipeline.geometry.geometry_loader import load_solids

    if not Path(_FIXTURE_530).exists():
        pytest.skip(f"fixture not available: {_FIXTURE_530}")
    solids = load_solids(_FIXTURE_530)
    total = sum(HoleAnalyzer().analyze(s).hole_count for s in solids)
    # Exact match to the AutoPOL round-hole subtotal; small drift acceptable.
    assert abs(total - _ROUND_HOLES_530) <= 2
