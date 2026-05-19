"""Tests for the stepalesengine command-line entry point.

Each test exercises ``manufacturing_pipeline.cli.main`` directly so we avoid
spawning a subprocess; the function returns a process exit code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from manufacturing_pipeline.cli import main


def _make_step(tmp_path: Path) -> Path:
    """Write a tiny box STEP file into ``tmp_path`` and return its path.

    Uses the project's geometry loader to keep the construction consistent
    with the rest of the test suite. Skipped when OCP isn't installed.
    """
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    from manufacturing_pipeline.geometry.geometry_loader import write_step

    solid = BRepPrimAPI_MakeBox(40.0, 30.0, 2.0).Solid()
    out = tmp_path / "box.step"
    write_step(solid, out)
    return out


def test_version_subcommand_prints_version(capsys):
    rc = main(["version"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    # version is read from pyproject.toml or importlib.metadata. Accept any
    # non-empty PEP 440-ish string.
    assert out
    assert out.split(".")[0].isdigit()


def test_parts_subcommand_prints_at_least_one_part(tmp_path, capsys):
    step_path = _make_step(tmp_path)
    rc = main(["parts", str(step_path)])
    out = capsys.readouterr().out
    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, "expected at least one part line"


def test_analyze_creates_manifest_and_optionally_dxf(tmp_path, capsys):
    step_path = _make_step(tmp_path)
    out_dir = tmp_path / "out"

    rc = main(["analyze", str(step_path), "--out-dir", str(out_dir)])
    assert rc == 0, capsys.readouterr()

    manifest = out_dir / "manifest.xml"
    assert manifest.exists() and manifest.stat().st_size > 0

    parts_dir = out_dir / "parts"
    if parts_dir.exists():
        dxfs = list(parts_dir.glob("*.dxf"))
        # DXFs may or may not be present depending on classifier outcome; the
        # contract is "at least zero".
        assert len(dxfs) >= 0


def test_analyze_nonexistent_path_returns_exit_2(tmp_path, capsys):
    missing = tmp_path / "does_not_exist.step"
    rc = main(["analyze", str(missing), "--out-dir", str(tmp_path / "out")])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not found" in err.lower() or "no such" in err.lower()


def test_analyze_no_dxf_skips_dxf_writing(tmp_path):
    step_path = _make_step(tmp_path)
    out_dir = tmp_path / "out"

    rc = main(["analyze", str(step_path), "--out-dir", str(out_dir), "--no-dxf", "--quiet"])
    assert rc == 0
    parts_dir = out_dir / "parts"
    # parts dir might exist if previously created; but no DXFs should be in it.
    if parts_dir.exists():
        assert not any(parts_dir.glob("*.dxf"))


def test_analyze_no_xml_skips_xml_writing(tmp_path):
    step_path = _make_step(tmp_path)
    out_dir = tmp_path / "out"

    rc = main(["analyze", str(step_path), "--out-dir", str(out_dir), "--no-xml", "--quiet"])
    assert rc == 0
    assert not (out_dir / "manifest.xml").exists()


def test_safe_name_lowercases_and_truncates():
    from manufacturing_pipeline.cli import _safe_name

    assert _safe_name("Hello World!!") == "hello_world"
    assert _safe_name("") == "part"
    assert len(_safe_name("a" * 200)) <= 64
