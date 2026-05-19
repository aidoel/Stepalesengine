"""Tests for the analyze pipeline orchestrator.

These tests exercise :func:`manufacturing_pipeline.pipeline.analyze_assembly.analyze`
directly as a library API, independent of the CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _make_box_step(tmp_path: Path, name: str = "box.step") -> Path:
    """Write a single-box STEP file and return its path. Skips if OCP missing."""
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    from manufacturing_pipeline.geometry.geometry_loader import write_step

    solid = BRepPrimAPI_MakeBox(40.0, 30.0, 2.0).Solid()
    out = tmp_path / name
    write_step(solid, out)
    return out


def _make_two_box_step(tmp_path: Path, name: str = "two_box.step") -> Path:
    """Write a STEP file containing two disjoint solids via STEPControl_Writer."""
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    a = BRepPrimAPI_MakeBox(20.0, 15.0, 3.0).Solid()
    b = BRepPrimAPI_MakeBox(gp_Pnt(50.0, 0.0, 0.0), 25.0, 10.0, 4.0).Solid()

    writer = STEPControl_Writer()
    rc1 = writer.Transfer(a, STEPControl_StepModelType.STEPControl_AsIs)
    assert rc1 == IFSelect_ReturnStatus.IFSelect_RetDone
    rc2 = writer.Transfer(b, STEPControl_StepModelType.STEPControl_AsIs)
    assert rc2 == IFSelect_ReturnStatus.IFSelect_RetDone
    out = tmp_path / name
    rc_w = writer.Write(str(out))
    assert rc_w == IFSelect_ReturnStatus.IFSelect_RetDone
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_default_options_produce_a_manifest_with_classified_parts(tmp_path: Path):
    from manufacturing_pipeline.pipeline.analyze_assembly import analyze

    step_path = _make_box_step(tmp_path)
    result = analyze(step_path)

    assert result.manifest is not None
    assert len(result.manifest.parts) >= 1
    for entry in result.manifest.parts:
        assert entry.classification is not None
        assert entry.classification.label in {"plaat", "profiel", "anders", "uncertain"}


def test_no_writes_yields_empty_dxf_paths_and_no_manifest_file(tmp_path: Path):
    from manufacturing_pipeline.pipeline.analyze_assembly import (
        AnalyzeOptions,
        analyze,
    )

    step_path = _make_box_step(tmp_path)
    result = analyze(
        step_path,
        AnalyzeOptions(out_dir=None, write_dxf=False, write_xml=False),
    )

    assert result.manifest is not None
    assert len(result.manifest.parts) >= 1
    assert result.dxf_paths == {}
    assert result.manifest_path is None


def test_write_xml_creates_a_parsable_manifest(tmp_path: Path):
    from manufacturing_pipeline.io.xml_writer import read_xml
    from manufacturing_pipeline.pipeline.analyze_assembly import (
        AnalyzeOptions,
        analyze,
    )

    step_path = _make_box_step(tmp_path)
    out_dir = tmp_path / "out"

    result = analyze(
        step_path,
        AnalyzeOptions(out_dir=out_dir, write_dxf=True, write_xml=True),
    )

    assert result.manifest_path is not None
    assert result.manifest_path.exists()
    assert result.manifest_path == out_dir / "manifest.xml"

    parsed = read_xml(result.manifest_path)
    assert len(parsed.parts) == len(result.manifest.parts) >= 1


def test_nonexistent_path_raises_filenotfound(tmp_path: Path):
    from manufacturing_pipeline.pipeline.analyze_assembly import analyze

    missing = tmp_path / "ghost.step"
    with pytest.raises(FileNotFoundError):
        analyze(missing)


def test_multi_solid_step_yields_multiple_manifest_entries(tmp_path: Path):
    from manufacturing_pipeline.pipeline.analyze_assembly import (
        AnalyzeOptions,
        analyze,
    )

    step_path = _make_two_box_step(tmp_path)
    result = analyze(
        step_path,
        AnalyzeOptions(out_dir=None, write_dxf=False, write_xml=False),
    )

    assert len(result.manifest.parts) >= 2


def test_empty_dxf_only_for_writes_no_dxf(tmp_path: Path):
    from manufacturing_pipeline.pipeline.analyze_assembly import (
        AnalyzeOptions,
        analyze,
    )

    step_path = _make_box_step(tmp_path)
    out_dir = tmp_path / "out"

    result = analyze(
        step_path,
        AnalyzeOptions(
            out_dir=out_dir,
            write_dxf=True,
            write_xml=True,
            dxf_only_for=(),
        ),
    )

    assert result.dxf_paths == {}
    parts_dir = out_dir / "parts"
    if parts_dir.exists():
        assert not any(parts_dir.glob("*.dxf"))


def test_manifest_carries_options_notes(tmp_path: Path):
    from manufacturing_pipeline.pipeline.analyze_assembly import (
        AnalyzeOptions,
        analyze,
    )

    step_path = _make_box_step(tmp_path)
    result = analyze(
        step_path,
        AnalyzeOptions(out_dir=None, write_dxf=False, write_xml=False, notes="hello"),
    )

    assert result.manifest.notes == "hello"


def test_use_cache_false_skips_the_disk_cache(tmp_path: Path):
    from manufacturing_pipeline.cache.disk import PipelineCache
    from manufacturing_pipeline.pipeline.analyze_assembly import (
        MODEL_VERSION,
        AnalyzeOptions,
        analyze,
    )

    step_path = _make_box_step(tmp_path)
    cache_dir = tmp_path / "cache"

    analyze(
        step_path,
        AnalyzeOptions(
            out_dir=None,
            write_dxf=False,
            write_xml=False,
            cache_dir=cache_dir,
            use_cache=False,
        ),
    )

    # No entry must have been written.
    cache = PipelineCache(cache_dir, version=MODEL_VERSION)
    assert cache.get(step_path) is None


def test_cache_dir_is_honoured(tmp_path: Path):
    from manufacturing_pipeline.cache.disk import PipelineCache
    from manufacturing_pipeline.pipeline.analyze_assembly import (
        MODEL_VERSION,
        AnalyzeOptions,
        analyze,
    )

    step_path = _make_box_step(tmp_path)
    cache_dir = tmp_path / "custom_cache"

    analyze(
        step_path,
        AnalyzeOptions(
            out_dir=None,
            write_dxf=False,
            write_xml=False,
            cache_dir=cache_dir,
            use_cache=True,
        ),
    )

    cache = PipelineCache(cache_dir, version=MODEL_VERSION)
    assert cache.get(step_path) is not None
    # And the on-disk layout lives under cache_dir/v<MODEL_VERSION>/.
    assert (cache_dir / f"v{MODEL_VERSION}").is_dir()
