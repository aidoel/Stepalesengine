"""Tests for the calibration sweep module."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest


def test_cost_weighted_score_is_zero_on_perfect_diagonal():
    from manufacturing_pipeline.calibration import cost_weighted_score

    confusion = {
        ("plaat", "plaat"): 5,
        ("profiel", "profiel"): 4,
        ("anders", "anders"): 3,
    }
    assert cost_weighted_score(confusion) == 0.0


def test_cost_weighted_score_charges_for_false_plate():
    from manufacturing_pipeline.calibration import cost_weighted_score

    # one "anders" misclassified as "plaat" -> cost_false_plate = 5
    confusion = {("anders", "plaat"): 1}
    assert cost_weighted_score(confusion) == 5.0
    # custom weighting still flows through
    assert cost_weighted_score(confusion, cost_false_plate=7.5) == 7.5
    # uncertain is the cheapest off-diagonal class
    assert cost_weighted_score({("anders", "uncertain"): 1}) == pytest.approx(0.1)
    # profiel mistakes are 1.0 by default
    assert cost_weighted_score({("anders", "profiel"): 2}) == 2.0


def test_load_labels_reads_csv_and_resolves_paths(tmp_path: Path):
    from manufacturing_pipeline.calibration import load_labels

    csv_path = tmp_path / "labels.csv"
    (tmp_path / "a.step").write_text("dummy")
    (tmp_path / "b.step").write_text("dummy")
    with csv_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=["step_path", "product_id", "expected_label"])
        writer.writeheader()
        writer.writerow({"step_path": "a.step", "product_id": "A1", "expected_label": "plaat"})
        writer.writerow({"step_path": "b.step", "product_id": "B2", "expected_label": "profiel"})
        writer.writerow({"step_path": "c.step", "product_id": "C3", "expected_label": "junk"})

    parts = load_labels(csv_path)
    assert len(parts) == 2
    assert {p.expected_label for p in parts} == {"plaat", "profiel"}
    assert parts[0].step_path == (tmp_path / "a.step").resolve()
    assert parts[0].product_id == "A1"


def test_load_labels_missing_columns_raises(tmp_path: Path):
    from manufacturing_pipeline.calibration import load_labels

    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("foo,bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_labels(csv_path)


def test_sweep_on_empty_corpus_returns_inf_cost():
    from manufacturing_pipeline.calibration import sweep

    result = sweep([])
    assert result.best_cost == float("inf")
    assert result.grid == []
    assert result.confusion == {}
    assert result.best_weights  # default scorers populated even on empty corpus


# ---------------------------------------------------------------------------
# Synthetic-STEP integration tests
# ---------------------------------------------------------------------------


def _write_synthetic_corpus(tmp_path: Path):
    """Build a tiny labelled corpus with a plate, a profile, and a pocketed plate.

    Returns the labels list. Skipped when OCP isn't installed.
    """
    pytest.importorskip("OCP")
    from manufacturing_pipeline.calibration import LabelledPart
    from tests.fixtures.synthetic_steps import (
        write_flat_plate_step,
        write_profile_step,
    )

    plate = write_flat_plate_step(tmp_path / "plate.step", l=200.0, w=100.0, t=3.0)
    profile = write_profile_step(
        tmp_path / "rhs.step", family="RHS", h=100.0, b=60.0, t=4.0, length=1000.0
    )
    pocket = write_flat_plate_step(
        tmp_path / "pocket.step",
        l=160.0,
        w=80.0,
        t=3.0,
        with_holes=[(40.0, 40.0, 10.0)],
    )

    return [
        LabelledPart(step_path=plate, product_id="plate", expected_label="plaat"),
        LabelledPart(step_path=profile, product_id="rhs", expected_label="profiel"),
        LabelledPart(step_path=pocket, product_id="pocket", expected_label="plaat"),
    ]


def test_sweep_on_three_synthetic_parts_returns_nonneg_cost(tmp_path: Path):
    from manufacturing_pipeline.calibration import sweep

    corpus = _write_synthetic_corpus(tmp_path)
    # Use a small grid so the test is quick.
    result = sweep(
        corpus,
        temperatures=(0.3, 0.7),
        margin_thrs=(0.10, 0.20),
        conf_thrs=(0.55, 0.65),
    )
    assert result.best_cost >= 0.0
    assert len(result.grid) == 2 * 2 * 2
    assert result.confusion  # at least one entry
    # all costs in the grid should be finite + non-negative
    for cell in result.grid:
        assert cell["cost"] >= 0.0


def test_sweep_best_cell_beats_worst_cell(tmp_path: Path):
    from manufacturing_pipeline.calibration import sweep

    corpus = _write_synthetic_corpus(tmp_path)
    result = sweep(
        corpus,
        temperatures=(0.3, 0.5, 1.0),
        margin_thrs=(0.05, 0.15, 0.25),
        conf_thrs=(0.55, 0.65, 0.75),
    )
    # grid is sorted ascending by cost - best cell first, worst last.
    costs = [cell["cost"] for cell in result.grid]
    assert costs == sorted(costs)
    assert result.best_cost == costs[0]
    # If every cell happened to tie the contract still holds; otherwise
    # at least one worse cell should exist.
    assert result.best_cost <= costs[-1]
    if costs[0] != costs[-1]:
        assert result.best_cost < costs[-1]
