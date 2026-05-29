"""Unit tests for the single-pass FeatureExtractor and shape healing.

Synthetic OCP solids only -- no STEP fixtures required.
"""

from __future__ import annotations

import pytest

pytest.importorskip("OCP")

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakeSphere,
)
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopoDS import TopoDS_Solid

from manufacturing_pipeline.geometry.feature_extractor import FeatureExtractor
from manufacturing_pipeline.geometry.shape_health import heal_shape, validity_report
from manufacturing_pipeline.geometry.types import ManufacturingFeatures

# ---------------------------------------------------------------------------
# shape_health
# ---------------------------------------------------------------------------


def test_heal_shape_returns_usable_shape_for_valid_box():
    box = BRepPrimAPI_MakeBox(100.0, 60.0, 2.0).Solid()
    healed = heal_shape(box)
    assert healed is not None
    assert not healed.IsNull()


def test_heal_shape_safe_on_none():
    """heal_shape must never raise, even on None / null shapes."""
    assert heal_shape(None) is None


def test_validity_report_clean_box():
    box = BRepPrimAPI_MakeBox(100.0, 60.0, 2.0).Solid()
    report = validity_report(box)
    assert report["is_valid"] is True
    assert report["n_faults"] == 0


def test_validity_report_handles_null_shape():
    report = validity_report(TopoDS_Solid())
    assert report["is_valid"] is False


# ---------------------------------------------------------------------------
# FeatureExtractor — plate
# ---------------------------------------------------------------------------


def test_extract_plate_box():
    """A 100x60x2 plate has near-1.0 planar pct, tiny thickness_ratio, and
    a dominant top1 face fraction."""

    box = BRepPrimAPI_MakeBox(100.0, 60.0, 2.0).Solid()
    feats = FeatureExtractor().extract(box)

    assert isinstance(feats, ManufacturingFeatures)
    assert feats.source == "brep"
    assert feats.volume == pytest.approx(100.0 * 60.0 * 2.0, rel=1e-3)
    L, W, T = feats.bbox_dims_sorted
    assert L >= W >= T
    assert L == pytest.approx(100.0, rel=1e-3)
    assert W == pytest.approx(60.0, rel=1e-3)
    assert T == pytest.approx(2.0, rel=1e-3)
    assert feats.thickness_ratio == pytest.approx(0.02, rel=5e-2)
    assert feats.surface_pct["planar"] == pytest.approx(1.0, abs=1e-6)
    # Largest face is one of the 100x60 = 6000 mm^2 sides → ~0.47 of 12640.
    assert feats.face_area_top[0] > 0.4
    # Hole defaults come from the HoleAnalyzer (separate module).
    assert feats.hole_count == 0
    assert feats.hole_diameters == []
    # No bends counted at this layer (the unfold probe owns that).


def test_bbox_dims_sorted_descending_invariant():
    """No matter how we orient the input box, bbox_dims_sorted returns
    extents in descending order."""

    for dims in [(100, 60, 2), (2, 100, 60), (60, 2, 100)]:
        box = BRepPrimAPI_MakeBox(*[float(d) for d in dims]).Solid()
        L, W, T = FeatureExtractor().extract(box).bbox_dims_sorted
        assert L >= W >= T


# ---------------------------------------------------------------------------
# FeatureExtractor — cylinder (profile-like)
# ---------------------------------------------------------------------------


def test_extract_cylinder_long():
    """10mm-radius x 100mm cylinder: cylindrical_pct dominates and the
    aspect ratio is ~5 (axial length vs diameter cross-section)."""

    cyl = BRepPrimAPI_MakeCylinder(10.0, 100.0).Solid()
    feats = FeatureExtractor().extract(cyl)

    assert feats.source == "brep"
    assert feats.surface_pct["cylindrical"] > feats.surface_pct["planar"]
    # Long axis = 100; cross-section diameter = 20. aspect_ratio = L/W = 100/20.
    assert feats.aspect_ratio == pytest.approx(5.0, rel=5e-2)
    # Cylindrical solids must yield a constant cross-section.
    assert feats.cross_section_constant is True


# ---------------------------------------------------------------------------
# FeatureExtractor — sphere
# ---------------------------------------------------------------------------


def test_extract_sphere_spherical_dominant():
    sphere = BRepPrimAPI_MakeSphere(50.0).Solid()
    feats = FeatureExtractor().extract(sphere)

    assert feats.surface_pct["spherical"] == pytest.approx(1.0, abs=1e-6)
    # No planar faces on a sphere.
    assert feats.face_area_top[0] == 0.0


# ---------------------------------------------------------------------------
# Hollow part
# ---------------------------------------------------------------------------


def test_extract_hollow_box_with_internal_pocket():
    """A box with a *blind* cylindrical pocket forms an internal void shell.
    Such a part must be flagged hollow and report ≥ 1 inner shell."""

    box = BRepPrimAPI_MakeBox(100.0, 60.0, 30.0).Solid()
    pocket_ax = gp_Ax2(gp_Pnt(50.0, 30.0, 5.0), gp_Dir(0.0, 0.0, 1.0))
    pocket = BRepPrimAPI_MakeCylinder(pocket_ax, 10.0, 20.0).Solid()
    cut = BRepAlgoAPI_Cut(box, pocket)
    cut.Build()
    hollow = cut.Shape()

    feats = FeatureExtractor().extract(hollow)
    assert feats.inner_shell_count >= 1
    assert feats.is_hollow is True


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_extract_never_raises_on_empty_solid():
    """A default-constructed TopoDS_Solid is null; extract returns defaults
    and never raises."""

    empty = TopoDS_Solid()
    feats = FeatureExtractor().extract(empty)
    assert isinstance(feats, ManufacturingFeatures)
    # Defaults preserved.
    assert feats.volume == 0.0
    assert feats.surface_area == 0.0
    assert feats.bbox_dims_sorted == (0.0, 0.0, 0.0)
    assert feats.face_area_top == [0.0, 0.0, 0.0]


def test_extract_never_raises_on_none():
    feats = FeatureExtractor().extract(None)
    assert isinstance(feats, ManufacturingFeatures)


def test_extract_disable_cross_section():
    """When cross-section computation is disabled, the signature is empty
    and constancy stays at its default False."""

    box = BRepPrimAPI_MakeBox(100.0, 60.0, 2.0).Solid()
    feats = FeatureExtractor(enable_cross_section=False).extract(box)
    assert feats.cross_section_constant is False
    assert feats.cross_section_signature == {}


# ---------------------------------------------------------------------------
# FeatureExtractor — convex_hull_volume_ratio
# ---------------------------------------------------------------------------


def test_convex_hull_ratio_solid_cube_is_one():
    """A solid 50x50x50 box equals its own convex hull."""

    cube = BRepPrimAPI_MakeBox(50.0, 50.0, 50.0).Solid()
    feats = FeatureExtractor(enable_cross_section=False).extract(cube)
    assert feats.convex_hull_volume_ratio == pytest.approx(1.0, abs=0.05)


def test_convex_hull_ratio_pocketed_box_below_one():
    """A box with a large rectangular pocket has substantially less material
    than its convex hull (the hull still spans the original outer box)."""

    box = BRepPrimAPI_MakeBox(100.0, 60.0, 30.0).Solid()
    pocket = BRepPrimAPI_MakeBox(gp_Pnt(10.0, 10.0, 5.0), 80.0, 40.0, 25.0).Solid()
    cut = BRepAlgoAPI_Cut(box, pocket)
    cut.Build()
    pocketed = cut.Shape()

    feats = FeatureExtractor(enable_cross_section=False).extract(pocketed)
    assert feats.convex_hull_volume_ratio < 0.95
    # And it must remain inside the clamp.
    assert 0.0 < feats.convex_hull_volume_ratio <= 1.5


def test_convex_hull_ratio_sphere_near_one():
    """A sphere IS approximately its own convex hull. Triangulation
    under-shoots the curved hull volume slightly, so the ratio can sit just
    over 1.0 — the helper clamps to <= 1.5. Empirically: ratio ~= 1.05 for
    R=50 with our default deflection."""

    sphere = BRepPrimAPI_MakeSphere(50.0).Solid()
    feats = FeatureExtractor(enable_cross_section=False).extract(sphere)
    # Observation: ~1.05 with current deflection — clamp guarantees <= 1.5.
    assert 0.9 <= feats.convex_hull_volume_ratio <= 1.5


def test_convex_hull_ratio_cylinder_near_one():
    """A cylinder is convex; its hull volume ~= its own volume."""

    cyl = BRepPrimAPI_MakeCylinder(10.0, 100.0).Solid()
    feats = FeatureExtractor(enable_cross_section=False).extract(cyl)
    # Triangulation can push slightly over 1.0; clamp keeps it bounded.
    assert 0.9 <= feats.convex_hull_volume_ratio <= 1.1


def test_convex_hull_ratio_slotted_cylinder_below_one():
    """A through-slot cut into a cylinder removes material that the
    convex hull still encloses, so the ratio drops noticeably."""

    cyl = BRepPrimAPI_MakeCylinder(20.0, 100.0).Solid()
    slot = BRepPrimAPI_MakeBox(gp_Pnt(-30.0, -3.0, 30.0), 60.0, 6.0, 40.0).Solid()
    cut = BRepAlgoAPI_Cut(cyl, slot)
    cut.Build()
    slotted = cut.Shape()

    feats = FeatureExtractor(enable_cross_section=False).extract(slotted)
    # Slot removes ~7% of the convex hull volume; ratio should be < 0.97 and
    # comfortably above 0.5.
    assert 0.5 < feats.convex_hull_volume_ratio < 0.97


def test_convex_hull_ratio_tiny_box_is_stable():
    """A 1x1x1 mm box still yields a sensible ratio (~1.0) — the deflection
    clamp must not collapse the triangulation."""

    tiny = BRepPrimAPI_MakeBox(1.0, 1.0, 1.0).Solid()
    feats = FeatureExtractor(enable_cross_section=False).extract(tiny)
    assert feats.convex_hull_volume_ratio == pytest.approx(1.0, abs=0.05)


# ---------------------------------------------------------------------------
# FeatureExtractor — pocket_complexity
# ---------------------------------------------------------------------------


def test_pocket_complexity_simple_box_is_zero():
    """A 6-face box has fewer than 8 planar faces, so pocket_complexity is 0.0."""

    box = BRepPrimAPI_MakeBox(50.0, 30.0, 20.0).Solid()
    feats = FeatureExtractor(enable_cross_section=False).extract(box)
    assert feats.pocket_complexity == 0.0


def test_pocket_complexity_pocketed_box_above_threshold():
    """A box with many small rectangular pockets cut out has many small planar
    facets, so pocket_complexity should rise above 0.3."""

    box = BRepPrimAPI_MakeBox(100.0, 60.0, 30.0).Solid()
    shape = box
    # Cut six 10x10x10 pockets into the top face along a 3x2 grid.
    for ix in range(3):
        for iy in range(2):
            x0 = 15.0 + ix * 25.0
            y0 = 15.0 + iy * 20.0
            pocket = BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, 20.0), 10.0, 10.0, 12.0).Solid()
            cut = BRepAlgoAPI_Cut(shape, pocket)
            cut.Build()
            shape = cut.Shape()

    feats = FeatureExtractor(enable_cross_section=False).extract(shape)
    assert (
        feats.pocket_complexity > 0.3
    ), f"pocketed box pocket_complexity={feats.pocket_complexity:.3f} not > 0.3"
    assert 0.0 <= feats.pocket_complexity <= 1.0


def test_pocket_complexity_sphere_is_zero():
    """A sphere has zero planar faces, so pocket_complexity must be 0.0."""

    sphere = BRepPrimAPI_MakeSphere(50.0).Solid()
    feats = FeatureExtractor(enable_cross_section=False).extract(sphere)
    assert feats.pocket_complexity == 0.0
