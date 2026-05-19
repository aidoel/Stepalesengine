"""Tests for the STEP geometry loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from manufacturing_pipeline.geometry.geometry_loader import (
    detect_step_units,
    load_solids,
    write_step,
)


def test_load_solids_raises_filenotfound_on_missing_path(tmp_path: Path):
    missing = tmp_path / "definitely_does_not_exist.step"
    with pytest.raises(FileNotFoundError):
        load_solids(missing)


def test_round_trip_box_through_step(tmp_path: Path):
    """Round-trip a unit box through a STEP file and back."""

    box = BRepPrimAPI_MakeBox(20.0, 15.0, 5.0).Solid()
    out_path = tmp_path / "box.step"
    write_step(box, out_path)
    assert out_path.exists() and out_path.stat().st_size > 0

    solids = load_solids(out_path)
    assert len(solids) >= 1
    # Each returned shape should be a SOLID by topological type.
    from OCP.TopAbs import TopAbs_SOLID

    for s in solids:
        assert s.ShapeType() == TopAbs_SOLID


def test_detect_step_units_mm_for_default_export(tmp_path: Path):
    """Default OCP export writes SI_UNIT(.MILLI.,.METRE.) - should detect 'mm'."""

    box = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Solid()
    out_path = tmp_path / "mm.step"
    write_step(box, out_path)
    assert detect_step_units(out_path) == "mm"


def test_detect_step_units_returns_unknown_on_missing_file(tmp_path: Path):
    p = tmp_path / "nope.step"
    assert detect_step_units(p) == "unknown"


def test_load_solids_no_heal(tmp_path: Path):
    """heal=False path still returns the same number of solids."""

    box = BRepPrimAPI_MakeBox(8.0, 8.0, 8.0).Solid()
    out_path = tmp_path / "noheal.step"
    write_step(box, out_path)
    solids = load_solids(out_path, heal=False)
    assert len(solids) == 1
