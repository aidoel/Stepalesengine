"""Tests for the per-solid prefilter + duplicate cache in analyze_assembly.

The prefilter skips expensive probes (UnfoldProbe, slice_solid +
ProfileShapeMatcher) on solids the cheap features prove cannot succeed.
The duplicate cache reuses the per-solid classification for identical
solids within a single ``analyze()`` call. Both are gated by
``AnalyzeOptions.prefilter`` (default True).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# Skip the whole module when OCP is missing - every test below builds
# real geometry via the synthetic_steps fixture helpers.
pytest.importorskip("OCP.STEPCAFControl")

from manufacturing_pipeline.geometry.types import (
    ManufacturingFeatures,
    UnfoldStatus,
)
from manufacturing_pipeline.pipeline.analyze_assembly import (
    _SOLID_RESULT_CACHE,
    AnalyzeOptions,
    _is_likely_profile,
    _is_likely_unfoldable,
    _solid_signature,
    analyze,
)
from tests.fixtures.synthetic_steps import (
    build_plate_solid,
    translate_solid,
    write_assembly_step,
    write_flat_plate_step,
)

# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _thin_plate_features() -> ManufacturingFeatures:
    """Synthetic features for a 100x60x2 mm flat plate (1.0 fill ratio)."""
    feats = ManufacturingFeatures(
        volume=100.0 * 60.0 * 2.0,
        surface_area=2 * (100 * 60 + 100 * 2 + 60 * 2),
        bbox_dims_sorted=(100.0, 60.0, 2.0),
        aspect_ratio=100.0 / 60.0,
        thickness_ratio=2.0 / 100.0,
        face_area_top=[0.45, 0.45, 0.02],
    )
    feats.surface_pct = {
        "planar": 1.0,
        "cylindrical": 0.0,
        "conical": 0.0,
        "toroidal": 0.0,
        "spherical": 0.0,
        "bspline": 0.0,
        "other": 0.0,
    }
    return feats


def _thick_block_features() -> ManufacturingFeatures:
    """Synthetic features for a 30x30x30 mm cube (chunky bar / fastener body)."""
    feats = ManufacturingFeatures(
        volume=30.0 ** 3,
        surface_area=6 * 30.0 ** 2,
        bbox_dims_sorted=(30.0, 30.0, 30.0),
        aspect_ratio=1.0,
        thickness_ratio=1.0,
    )
    feats.surface_pct = {
        "planar": 1.0,
        "cylindrical": 0.0,
        "conical": 0.0,
        "toroidal": 0.0,
        "spherical": 0.0,
        "bspline": 0.0,
        "other": 0.0,
    }
    return feats


def _elongated_profile_features() -> ManufacturingFeatures:
    """Synthetic features for a 1000x60x40 mm constant-section profile."""
    feats = ManufacturingFeatures(
        volume=1000.0 * 60.0 * 40.0 * 0.3,  # hollow profile, 30% fill
        surface_area=2 * (1000 * 60 + 1000 * 40 + 60 * 40),
        bbox_dims_sorted=(1000.0, 60.0, 40.0),
        aspect_ratio=1000.0 / 60.0,
        thickness_ratio=40.0 / 1000.0,
    )
    feats.surface_pct = {
        "planar": 0.95,
        "cylindrical": 0.05,
        "conical": 0.0,
        "toroidal": 0.0,
        "spherical": 0.0,
        "bspline": 0.0,
        "other": 0.0,
    }
    return feats


def _cube_features() -> ManufacturingFeatures:
    """Cubic 30x30x30 solid is neither thin nor elongated."""
    return _thick_block_features()


# ---------------------------------------------------------------------------
# 1. Helper behaviour
# ---------------------------------------------------------------------------


def test_is_likely_unfoldable_accepts_thin_plate():
    assert _is_likely_unfoldable(_thin_plate_features()) is True


def test_is_likely_unfoldable_rejects_thick_block():
    assert _is_likely_unfoldable(_thick_block_features()) is False


def test_is_likely_unfoldable_rejects_cylindrical_blob():
    # >5% b-spline OR <50% planar -> obviously not developable sheet.
    feats = _thin_plate_features()
    feats.surface_pct = {**feats.surface_pct, "planar": 0.10, "cylindrical": 0.90}
    assert _is_likely_unfoldable(feats) is False


def test_is_likely_profile_accepts_elongated_constant_section():
    assert _is_likely_profile(_elongated_profile_features()) is True


def test_is_likely_profile_rejects_cube():
    assert _is_likely_profile(_cube_features()) is False


def test_is_likely_profile_rejects_high_bspline_part():
    feats = _elongated_profile_features()
    feats.surface_pct = {**feats.surface_pct, "bspline": 0.5}
    assert _is_likely_profile(feats) is False


# ---------------------------------------------------------------------------
# 2. End-to-end: duplicate cache skips UnfoldProbe on repeated solids
# ---------------------------------------------------------------------------


def _build_assembly_with_n_identical_plates(path: Path, n: int = 10) -> Path:
    """Write an assembly STEP with ``n`` translated copies of the same plate."""
    parts = []
    for i in range(n):
        solid = translate_solid(build_plate_solid(l=40.0, w=30.0, t=2.0), dx=i * 50.0)
        parts.append((f"plate_{i:02d}", solid))
    return write_assembly_step(path, parts=parts)


def test_duplicate_cache_skips_unfold_probe_on_repeated_solids(tmp_path: Path):
    """The UnfoldProbe must run AT MOST ONCE for 10 identical plates."""
    from manufacturing_pipeline.geometry.unfold_probe import UnfoldProbe

    step = _build_assembly_with_n_identical_plates(tmp_path / "ten_plates.step", n=10)

    real_run = UnfoldProbe.run
    call_count = {"n": 0}

    def _spy(self, solid):
        call_count["n"] += 1
        return real_run(self, solid)

    with patch.object(UnfoldProbe, "run", _spy):
        result = analyze(
            step,
            AnalyzeOptions(
                out_dir=None, write_dxf=False, write_xml=False, use_cache=False
            ),
        )

    # Every plate should match a leaf in the manifest.
    assert len(result.manifest.parts) == 10
    # All 10 are identical, so the cache hits 9 times; the probe runs once
    # (and the DXF-emission codepath may invoke it once more during the run
    # of the FIRST plate, so we allow up to 2 calls).
    assert call_count["n"] <= 2, f"expected <=2 UnfoldProbe.run calls, got {call_count['n']}"


def test_no_prefilter_runs_unfold_on_every_solid(tmp_path: Path):
    """With prefilter=False the cache is bypassed and UnfoldProbe runs N times."""
    from manufacturing_pipeline.geometry.unfold_probe import UnfoldProbe

    step = _build_assembly_with_n_identical_plates(tmp_path / "ten_plates_full.step", n=10)

    real_run = UnfoldProbe.run
    call_count = {"n": 0}

    def _spy(self, solid):
        call_count["n"] += 1
        return real_run(self, solid)

    with patch.object(UnfoldProbe, "run", _spy):
        result = analyze(
            step,
            AnalyzeOptions(
                out_dir=None,
                write_dxf=False,
                write_xml=False,
                use_cache=False,
                prefilter=False,
            ),
        )

    assert len(result.manifest.parts) == 10
    # No cache -> at least one UnfoldProbe call per solid.
    assert call_count["n"] >= 10, f"expected >=10 UnfoldProbe.run calls, got {call_count['n']}"


# ---------------------------------------------------------------------------
# 3. Cache scoping: per-analyze() call, not cross-file
# ---------------------------------------------------------------------------


def test_cache_is_cleared_between_analyze_calls(tmp_path: Path):
    """``_SOLID_RESULT_CACHE`` must be empty at the start of every analyze() call.

    We poison the cache before the call, then assert that the call did NOT
    return the poisoned classification.
    """
    from manufacturing_pipeline.classification.types import (
        ClassificationResult,
        DecisionTrace,
    )
    from manufacturing_pipeline.geometry.types import HolePattern, UnfoldResult

    step1 = write_flat_plate_step(tmp_path / "p1.step", l=80.0, w=50.0, t=2.0)
    step2 = write_flat_plate_step(tmp_path / "p2.step", l=100.0, w=60.0, t=2.0)

    # Run #1 to populate the cache with the real plate-1 signature.
    r1 = analyze(
        step1,
        AnalyzeOptions(out_dir=None, write_dxf=False, write_xml=False, use_cache=False),
    )
    assert len(r1.manifest.parts) == 1
    assert len(_SOLID_RESULT_CACHE) >= 1

    # Now POISON every cache entry with a sentinel classification.
    poison = ClassificationResult(
        label="uncertain", confidence=0.0, trace=DecisionTrace(model_version="poison")
    )
    fake_unfold = UnfoldResult(status=UnfoldStatus.FAILURE, reason="poison")
    _SOLID_RESULT_CACHE.update(
        {k: (None, fake_unfold, HolePattern(), poison) for k in list(_SOLID_RESULT_CACHE)}
    )

    # Run #2 on a DIFFERENT plate; the cache should be wiped so the poison
    # cannot leak through. The model_version must be the real one.
    r2 = analyze(
        step2,
        AnalyzeOptions(out_dir=None, write_dxf=False, write_xml=False, use_cache=False),
    )
    assert len(r2.manifest.parts) == 1
    entry = r2.manifest.parts[0]
    assert entry.classification.trace.model_version != "poison"


# ---------------------------------------------------------------------------
# 4. Signature stability
# ---------------------------------------------------------------------------


def test_solid_signature_is_stable_for_equivalent_features():
    """Two ManufacturingFeatures that round to the same buckets must collide."""
    a = ManufacturingFeatures(
        volume=123.45,
        surface_area=678.90,
        bbox_dims_sorted=(100.04, 60.05, 2.04),
        hole_count=3,
    )
    b = ManufacturingFeatures(
        volume=123.47,
        surface_area=678.92,
        bbox_dims_sorted=(100.05, 60.04, 2.05),
        hole_count=3,
    )
    assert _solid_signature(a) == _solid_signature(b)


def test_solid_signature_differs_for_different_hole_counts():
    """Two same-volume blocks with different hole counts must NOT collide."""
    a = ManufacturingFeatures(
        volume=100.0,
        surface_area=200.0,
        bbox_dims_sorted=(10.0, 10.0, 1.0),
        hole_count=0,
    )
    b = ManufacturingFeatures(
        volume=100.0,
        surface_area=200.0,
        bbox_dims_sorted=(10.0, 10.0, 1.0),
        hole_count=4,
    )
    assert _solid_signature(a) != _solid_signature(b)


# ---------------------------------------------------------------------------
# 5. Performance: prefilter speeds up a 30-bolt assembly
# ---------------------------------------------------------------------------


def _build_thirty_box_assembly(path: Path) -> Path:
    """A 30-body assembly using two distinct body sizes (fastener mock).

    This is the synthetic stand-in for the 626 s outlier file: many small,
    repeated, non-sheet bodies. We want the prefilter run to be CLEARLY
    faster than the no-prefilter run.
    """
    parts = []
    for i in range(20):
        # M8x40 bolt body mock: 8x8x40, cube-like fastener.
        solid = translate_solid(build_plate_solid(l=8.0, w=8.0, t=40.0), dx=i * 20.0)
        parts.append((f"bolt_{i:02d}", solid))
    for i in range(10):
        # M16x16 washer mock: 16x16x2.
        solid = translate_solid(
            build_plate_solid(l=16.0, w=16.0, t=2.0), dx=i * 25.0, dy=100.0
        )
        parts.append((f"washer_{i:02d}", solid))
    return write_assembly_step(path, parts=parts)


@pytest.mark.slow
def test_prefilter_cuts_unfold_probe_calls_on_thirty_body_assembly(tmp_path: Path):
    """End-to-end: the prefilter must spare the expensive UnfoldProbe most of
    its calls on a fastener-heavy assembly.

    Wall-time on a 30-body synthetic fixture is too noisy to assert on - it is
    dominated by OCP solid loading, which both modes pay equally. What matters
    is the mechanism, so we count UnfoldProbe.run invocations instead. With the
    prefilter on, the cube-like bolt bodies are rejected by the cheap pre-check
    and the duplicate-solid cache collapses the rest, so the probe runs a
    handful of times; with it off, the probe runs at least once per solid.
    """
    from manufacturing_pipeline.geometry.unfold_probe import UnfoldProbe

    step = _build_thirty_box_assembly(tmp_path / "thirty.step")

    def _count_unfold_calls(prefilter: bool) -> int:
        real_run = UnfoldProbe.run
        calls = {"n": 0}

        def _spy(self, solid):
            calls["n"] += 1
            return real_run(self, solid)

        with patch.object(UnfoldProbe, "run", _spy):
            result = analyze(
                step,
                AnalyzeOptions(
                    out_dir=None,
                    write_dxf=False,
                    write_xml=False,
                    use_cache=False,
                    prefilter=prefilter,
                ),
            )
        assert len(result.manifest.parts) == 30
        return calls["n"]

    n_fast = _count_unfold_calls(prefilter=True)
    n_full = _count_unfold_calls(prefilter=False)

    # No prefilter, no cache: UnfoldProbe runs at least once per solid.
    assert n_full >= 30, f"expected >=30 UnfoldProbe.run calls, got {n_full}"
    # With the prefilter the 20 bolt bodies are skipped and the identical
    # washers collapse to a single cached run - far below the no-prefilter run.
    assert n_fast * 2 < n_full, (
        f"prefilter expected to at least halve UnfoldProbe.run calls; "
        f"full={n_full}, prefilter={n_fast}"
    )


# ---------------------------------------------------------------------------
# 6. Prefilter records the synthetic reason on a rejected solid
# ---------------------------------------------------------------------------


def test_prefilter_records_synthetic_reason_on_thick_block(tmp_path: Path):
    """A solid the prefilter rejects gets an UnfoldResult(FAILURE, reason='prefilter:...').

    L-bracket has high fill_ratio inversion (it's bent so its bbox is sparse)
    and stays unfoldable. We need a TRULY chunky solid - use a 30x30x30 cube
    which has thickness_ratio = 1.0 AND fill_ratio = 1.0 -> rejected.
    """
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    from manufacturing_pipeline.geometry.geometry_loader import write_step

    cube = BRepPrimAPI_MakeBox(30.0, 30.0, 30.0).Solid()
    step = tmp_path / "cube.step"
    write_step(cube, step)

    result = analyze(
        step,
        AnalyzeOptions(out_dir=None, write_dxf=False, write_xml=False, use_cache=False),
    )
    assert len(result.manifest.parts) == 1
    entry = result.manifest.parts[0]
    assert entry.unfold is not None
    assert entry.unfold.status == UnfoldStatus.FAILURE
    assert entry.unfold.reason.startswith("prefilter:"), (
        f"expected reason to start with 'prefilter:', got {entry.unfold.reason!r}"
    )
