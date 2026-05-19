"""Tests for the parallel batch runner."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("OCP")

from manufacturing_pipeline.batch import BatchResult, analyze_one, batch_analyze
from manufacturing_pipeline.pipeline.analyze_assembly import AnalyzeOptions, analyze
from tests.fixtures.synthetic_steps import (
    write_flat_plate_step,
    write_lbracket_step,
    write_profile_step,
)


def _write_three_steps(tmp_path: Path) -> list[Path]:
    """Generate three distinct synthetic STEPs in ``tmp_path``."""
    a = write_flat_plate_step(tmp_path / "plate.step", l=120, w=60, t=3.0)
    b = write_lbracket_step(tmp_path / "bracket.step", leg_a=60, leg_b=50, t=2.0)
    c = write_profile_step(tmp_path / "rhs.step", family="RHS", h=80, b=40, t=3.0, length=400)
    return [a, b, c]


def test_analyze_one_returns_ok_for_synthetic_step(tmp_path):
    step = write_flat_plate_step(tmp_path / "plate.step", l=120, w=60, t=3.0)
    out_root = tmp_path / "out"

    result = analyze_one(step, out_root, write_dxf=True, write_xml=True, use_cache=False)

    assert isinstance(result, BatchResult)
    assert result.ok is True
    assert result.error is None
    assert result.parts >= 1
    assert result.manifest_path is not None
    assert result.manifest_path.exists()
    assert result.manifest_path.is_relative_to(out_root)


def test_batch_analyze_three_files_all_ok(tmp_path):
    steps = _write_three_steps(tmp_path)
    out_root = tmp_path / "out"

    results = batch_analyze(
        steps, out_root, workers=2, write_dxf=False, write_xml=True, use_cache=False
    )

    assert len(results) == 3
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]
    assert all(r.parts > 0 for r in results)
    # Each STEP got its own subdirectory.
    sub_dirs = [p for p in out_root.iterdir() if p.is_dir()]
    assert len(sub_dirs) == 3


def test_batch_analyze_one_bad_path_does_not_kill_others(tmp_path):
    good_a = write_flat_plate_step(tmp_path / "good_a.step", l=80, w=40, t=2.0)
    good_b = write_profile_step(
        tmp_path / "good_b.step", family="SHS", h=60, b=60, t=3.0, length=300
    )
    bad = tmp_path / "does_not_exist.step"
    out_root = tmp_path / "out"

    results = batch_analyze(
        [good_a, bad, good_b],
        out_root,
        workers=2,
        write_dxf=False,
        write_xml=True,
        use_cache=False,
    )

    assert len(results) == 3
    by_path = {r.file.name: r for r in results}
    assert by_path["good_a.step"].ok is True
    assert by_path["good_b.step"].ok is True
    assert by_path["does_not_exist.step"].ok is False
    assert by_path["does_not_exist.step"].error is not None


def test_batch_analyze_workers_one_matches_sequential_analyze(tmp_path):
    """With workers=1 results must mirror a sequential analyze() loop."""
    steps = _write_three_steps(tmp_path)

    out_seq = tmp_path / "seq"
    seq_parts: list[int] = []
    seq_labels: list[dict[str, int]] = []
    for step in steps:
        result = analyze(
            step,
            AnalyzeOptions(
                out_dir=out_seq / step.stem,
                write_dxf=False,
                write_xml=True,
                use_cache=False,
            ),
        )
        seq_parts.append(len(result.manifest.parts))
        counts: dict[str, int] = {}
        for entry in result.manifest.parts:
            counts[entry.classification.label] = counts.get(entry.classification.label, 0) + 1
        seq_labels.append(counts)

    out_batch = tmp_path / "batch"
    results = batch_analyze(
        steps, out_batch, workers=1, write_dxf=False, write_xml=True, use_cache=False
    )

    # batch_analyze with workers=1 preserves input order.
    assert [r.file for r in results] == steps
    assert [r.parts for r in results] == seq_parts
    assert [r.label_counts for r in results] == seq_labels
    assert all(r.ok for r in results)


def test_cli_batch_smoke(tmp_path):
    """End-to-end CLI smoke: run `batch` over a 2-file dir via subprocess."""
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    write_flat_plate_step(input_dir / "plate.step", l=100, w=50, t=2.0)
    write_profile_step(input_dir / "rhs.step", family="RHS", h=80, b=40, t=3.0, length=300)
    out_dir = tmp_path / "out"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "manufacturing_pipeline.cli",
            "batch",
            str(input_dir),
            "--out-dir",
            str(out_dir),
            "--workers",
            "2",
            "--no-dxf",
            "--no-cache",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    assert "Batch summary" in proc.stdout
    assert "ok:       2" in proc.stdout
    assert "failed:   0" in proc.stdout
    # Per-STEP subfolders exist with a manifest.xml.
    assert (out_dir / "plate" / "manifest.xml").exists()
    assert (out_dir / "rhs" / "manifest.xml").exists()
