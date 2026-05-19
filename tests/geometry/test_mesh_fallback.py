"""Tests for the mesh-only fallback path in :class:`FeatureExtractor`.

Tessellated-only solids (faces carrying :class:`Poly_Triangulation` but no
:class:`Geom_Surface`) are constructed via the
:func:`tests.fixtures.synthetic_steps.make_tessellated_solid` helper. These
fixtures exercise the ``source == "mesh"`` branch of the extractor, which
is otherwise unreachable from any real B-rep primitive.
"""

from __future__ import annotations

import pytest

pytest.importorskip("OCP.Poly")
pytest.importorskip("OCP.BRep")

from OCP.BRep import BRep_Tool
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

from manufacturing_pipeline.geometry.feature_extractor import FeatureExtractor
from manufacturing_pipeline.geometry.types import ManufacturingFeatures
from tests.fixtures.synthetic_steps import (
    make_tessellated_solid,
    write_tessellated_step,
)


def _all_faces_null_surface(solid) -> tuple[int, int]:
    """Return ``(n_null, n_total)`` for the faces of *solid*."""

    n_null = 0
    n_total = 0
    exp = TopExp_Explorer(solid, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        if BRep_Tool.Surface_s(face) is None:
            n_null += 1
        n_total += 1
        exp.Next()
    return n_null, n_total


# ---------------------------------------------------------------------------
# Fixture sanity
# ---------------------------------------------------------------------------


def test_make_tessellated_solid_has_null_geom_surface():
    """All faces of the octahedron fixture must report a null Geom_Surface,
    which is the property that drives FeatureExtractor onto the mesh path."""

    solid = make_tessellated_solid("octahedron", size=50.0)
    n_null, n_total = _all_faces_null_surface(solid)
    assert n_total == 8, f"expected 8 faces, got {n_total}"
    assert n_null == n_total, f"every face must have a null Geom_Surface, got {n_null}/{n_total}"


# ---------------------------------------------------------------------------
# Mesh-fallback branch
# ---------------------------------------------------------------------------


def test_feature_extractor_reports_source_mesh():
    """A tessellated-only solid must drive ``features.source == 'mesh'``."""

    solid = make_tessellated_solid("octahedron", size=50.0)
    feats = FeatureExtractor(enable_cross_section=False).extract(solid)
    assert isinstance(feats, ManufacturingFeatures)
    assert feats.source == "mesh"


def test_mesh_fallback_populates_bulk_metrics():
    """Mesh fallback must fill volume, surface area, bbox dims, aspect ratio."""

    solid = make_tessellated_solid("cube", size=40.0)
    feats = FeatureExtractor(enable_cross_section=False).extract(solid)

    assert feats.source == "mesh"
    assert feats.volume > 0.0, "volume must be populated from triangulation"
    assert feats.surface_area > 0.0, "surface area must be populated from triangulation"
    L, W, T = feats.bbox_dims_sorted
    assert (L, W, T) != (0.0, 0.0, 0.0), "bbox_dims_sorted must be non-zero"
    assert L >= W >= T
    # Aspect ratio sensible: roughly 1.0 for a cube.
    assert feats.aspect_ratio >= 1.0
    assert feats.aspect_ratio == pytest.approx(1.0, abs=0.05)


# ---------------------------------------------------------------------------
# Volume / surface area accuracy on a polyhedral cube
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", [25.0, 50.0, 100.0])
def test_mesh_cube_volume_within_5_percent(size):
    """A tessellated polyhedral cube should hit ``size**3`` within 5%."""

    solid = make_tessellated_solid("cube", size=size)
    feats = FeatureExtractor(enable_cross_section=False).extract(solid)
    assert feats.source == "mesh"
    expected = size**3
    assert feats.volume == pytest.approx(expected, rel=0.05), (
        f"mesh volume {feats.volume} vs expected {expected}"
    )


@pytest.mark.parametrize("size", [25.0, 50.0, 100.0])
def test_mesh_cube_surface_area_within_5_percent(size):
    """Tessellated cube surface area should hit ``6 * size**2`` within 5%."""

    solid = make_tessellated_solid("cube", size=size)
    feats = FeatureExtractor(enable_cross_section=False).extract(solid)
    assert feats.source == "mesh"
    expected = 6.0 * size * size
    assert feats.surface_area == pytest.approx(expected, rel=0.05), (
        f"mesh surface area {feats.surface_area} vs expected {expected}"
    )


# ---------------------------------------------------------------------------
# Full analyze_assembly pipeline on a tessellated STEP
# ---------------------------------------------------------------------------


def test_analyze_assembly_runs_on_tessellated_step(tmp_path):
    """The full analyze pipeline must ingest a tessellation-only STEP file
    without raising and produce at least one classified part."""

    from manufacturing_pipeline.pipeline.analyze_assembly import (
        AnalyzeOptions,
        analyze,
    )

    step_path = write_tessellated_step(tmp_path / "mesh_cube.step", shape="cube", size=50.0)
    opts = AnalyzeOptions(out_dir=tmp_path / "out", write_dxf=False, write_xml=True)
    result = analyze(step_path, options=opts)

    assert len(result.manifest.parts) >= 1
    entry = result.manifest.parts[0]
    assert entry.classification.label in {"plaat", "profiel", "anders", "uncertain"}
    # The classifier should see the mesh-derived features.
    assert entry.features.source == "mesh"
