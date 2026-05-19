"""Tests for the corpus validation CLI + module."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("OCP")

from manufacturing_pipeline.validate import (
    CorpusFile,
    CorpusReport,
    render_html_report,
    render_markdown_report,
    validate_corpus,
)
from tests.fixtures.synthetic_steps import (
    write_flat_plate_step,
    write_profile_step,
)


def _seed_two_steps(directory: Path) -> list[Path]:
    """Write two distinct synthetic STEPs into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    a = write_flat_plate_step(directory / "plate.step", l=100, w=50, t=2.0)
    b = write_profile_step(
        directory / "rhs.step", family="RHS", h=80, b=40, t=3.0, length=300
    )
    return [a, b]


# ---------------------------------------------------------------------------
# validate_corpus
# ---------------------------------------------------------------------------


def test_validate_corpus_two_files_returns_ok_report(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    _seed_two_steps(corpus)

    report = validate_corpus(corpus, workers=1, write_outputs=False)

    assert isinstance(report, CorpusReport)
    assert report.total_files == 2
    assert report.ok_count == 2
    assert report.failed_count == 0
    assert report.parts_total >= 2
    assert {f.relative_path for f in report.files} == {"plate.step", "rhs.step"}
    assert all(isinstance(f, CorpusFile) for f in report.files)
    # Without --write-outputs we should not have left manifests around.
    assert not (tmp_path / "manifest.xml").exists()


def test_validate_corpus_on_empty_dir_returns_empty_report(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    report = validate_corpus(empty, workers=1, write_outputs=False)

    assert isinstance(report, CorpusReport)
    assert report.total_files == 0
    assert report.ok_count == 0
    assert report.failed_count == 0
    assert report.parts_total == 0
    assert report.files == []
    assert report.anomalies == []
    assert report.label_distribution == {}


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def test_render_html_report_contains_path_pills_and_table(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    _seed_two_steps(corpus)

    report = validate_corpus(corpus, workers=1, write_outputs=False)
    html_path = render_html_report(report, tmp_path / "report.html")

    text = html_path.read_text(encoding="utf-8")
    assert html_path.exists()
    assert str(corpus) in text  # corpus path
    assert "<table" in text
    assert "label-plaat" in text or "label-profiel" in text or "label-uncertain" in text
    assert "STEP corpus validation" in text


def test_render_markdown_report_contains_table_and_counts(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    _seed_two_steps(corpus)

    report = validate_corpus(corpus, workers=1, write_outputs=False)
    md_path = render_markdown_report(report, tmp_path / "report.md")

    text = md_path.read_text(encoding="utf-8")
    assert md_path.exists()
    assert "| " in text  # markdown table syntax
    assert "Files: **2**" in text
    assert "plate.step" in text
    assert "rhs.step" in text


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


def test_cli_validate_corpus_smoke(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    _seed_two_steps(corpus)
    html_path = tmp_path / "report.html"
    md_path = tmp_path / "report.md"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "manufacturing_pipeline.cli",
            "validate-corpus",
            str(corpus),
            "--workers",
            "1",
            "--html",
            str(html_path),
            "--md",
            str(md_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    assert "Corpus validation summary" in proc.stdout
    assert "files:     2" in proc.stdout
    assert html_path.exists()
    assert md_path.exists()
    assert "<table" in html_path.read_text(encoding="utf-8")
    assert "| " in md_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------


def test_validate_corpus_flags_zero_parts_as_anomaly(tmp_path: Path) -> None:
    """A non-STEP-but-named-.step file parses to zero parts and gets flagged."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    write_flat_plate_step(corpus / "good.step", l=80, w=40, t=2.0)
    (corpus / "bogus.step").write_text("not a STEP file at all", encoding="utf-8")

    report = validate_corpus(corpus, workers=1, write_outputs=False)

    assert report.total_files == 2
    bad = next(f for f in report.files if f.relative_path == "bogus.step")
    assert bad.parts == 0
    assert bad.warnings  # anomaly reasons captured (zero parts at minimum)
    assert any("bogus.step" in line for line in report.anomalies)
    assert any("zero parts" in line for line in report.anomalies)
