"""Offline calibration utilities for the score classifier.

Exposes a grid-sweep over softmax temperature, margin/confidence thresholds,
and optional weight perturbations against a labelled corpus. The winning
cell minimises a cost-weighted confusion-matrix score.

Re-exports the public surface so callers can do::

    from manufacturing_pipeline.calibration import (
        LabelledPart, SweepResult, sweep, load_labels, cost_weighted_score,
    )
"""

from __future__ import annotations

from .sweep import (
    LabelledPart,
    SweepResult,
    cost_weighted_score,
    load_labels,
    sweep,
)

__all__ = [
    "LabelledPart",
    "SweepResult",
    "cost_weighted_score",
    "load_labels",
    "sweep",
]
