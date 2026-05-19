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
