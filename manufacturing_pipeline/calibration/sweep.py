"""Grid-sweep calibration of the score classifier against a labelled corpus.

Approach
--------
For each labelled part:
  1. Run :func:`analyze` once with telemetry/IO disabled, collecting the
     per-part :class:`ManufacturingFeatures`, profile match, and unfold
     result.
  2. Re-build the flat classifier-feature dict via
     :func:`_classifier_features`.
  3. Cache the dict; cheaper to re-classify than to re-run the geometry
     probes on every sweep cell.

For each cell ``(temperature, margin_thr, conf_thr, weight_jitter)`` in
the cartesian grid:
  * Instantiate a :class:`ScoreClassifier` with the cell parameters.
  * Classify every cached feature dict against its expected label.
  * Aggregate into a confusion matrix keyed on ``(true, pred)``.
  * Score with :func:`cost_weighted_score`.

Return the lowest-cost cell. Empty corpora short-circuit to a degenerate
:class:`SweepResult` with ``best_cost=inf``.

Cost rationale
--------------
The default cost matrix punishes commercial-impact mistakes most: labelling
a purchased ``anders`` profile as a manufactured ``plaat`` would trigger
useless CAM work. A ``profiel`` misclassified as ``plaat`` is cheaper
(both go to the workshop, just to different stations). Failing to commit
(``uncertain``) is the cheapest mistake because a human reviewer can step
in without anything being wasted.
"""

from __future__ import annotations

import csv
import itertools
from dataclasses import dataclass, field
from pathlib import Path

from ..classification.score_classifier import ScoreClassifier
from ..classification.scorers import ScorerSpec, default_scorers
from ..classification.tiebreakers import (
    cross_section_tiebreaker,
    profile_match_tiebreaker,
    unfold_tiebreaker,
)
from ..pipeline.analyze_assembly import (
    AnalyzeOptions,
    _classifier_features,
    analyze,
)

# Labels the classifier and corpus speak in. The classifier may also output
# "uncertain" when confidence drops below threshold; we treat it as its own
# bucket in the confusion matrix.
_LABELS = ("plaat", "profiel", "anders", "uncertain")


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LabelledPart:
    """One STEP file with its ground-truth label.

    Attributes
    ----------
    step_path:
        Filesystem path to a STEP (Part 21) file.
    product_id:
        Selects which part of a multi-part STEP file the label applies to.
        When non-empty only the part whose parsed product id matches is
        labelled; when empty every part in the file is labelled with
        ``expected_label`` (back-compat for single-part files).
    expected_label:
        Ground-truth class: ``"plaat"``, ``"profiel"`` or ``"anders"``.
    """

    step_path: Path
    product_id: str
    expected_label: str  # plaat | profiel | anders


@dataclass
class SweepResult:
    """Outcome of a full grid sweep.

    Attributes
    ----------
    best_weights:
        The :class:`ScorerSpec` dict that produced the lowest-cost cell.
        Equal to :func:`default_scorers` if weight perturbation was off.
    best_margin_thr, best_conf_thr, best_temperature:
        Winning hyperparameter values.
    best_cost:
        Sum of per-cell cost-weighted confusion-matrix entries. Lower is
        better. ``inf`` when the corpus is empty.
    confusion:
        ``{(true_label, pred_label): count}`` for the winning cell.
    grid:
        Every cell evaluated, sorted ascending by cost. Each entry is a
        dict with keys ``temperature``, ``margin_thr``, ``conf_thr``,
        ``cost``, ``confusion``, ``weight_jitter``.
    """

    best_weights: dict = field(default_factory=dict)
    best_margin_thr: float = 0.0
    best_conf_thr: float = 0.0
    best_temperature: float = 0.0
    best_cost: float = float("inf")
    confusion: dict = field(default_factory=dict)
    grid: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def cost_weighted_score(
    confusion: dict,
    *,
    cost_false_plate: float = 5.0,
    cost_false_profile: float = 1.0,
    cost_uncertain: float = 0.1,
) -> float:
    """Compute a cost from a confusion matrix.

    ``confusion`` is keyed ``(true, pred) -> count``. Correct predictions
    (``true == pred``) cost zero. Predicting ``plaat`` when the truth was
    something else costs ``cost_false_plate`` per occurrence (the most
    expensive mistake because manufacturing work would start on the wrong
    raw stock). Predicting ``profiel`` incorrectly is cheaper. Predicting
    ``uncertain`` is cheapest because the part lands on a review queue
    rather than going to production.

    Any other off-diagonal cell (e.g. ``profiel -> anders``) costs
    ``cost_false_profile`` by default.
    """
    total = 0.0
    for (true, pred), count in confusion.items():
        if count <= 0:
            continue
        if true == pred:
            continue
        if pred == "uncertain":
            total += cost_uncertain * count
        elif pred == "plaat":
            total += cost_false_plate * count
        elif pred == "profiel":
            total += cost_false_profile * count
        else:
            # pred == "anders" or any other label
            total += cost_false_profile * count
    return float(total)


# ---------------------------------------------------------------------------
# Corpus IO
# ---------------------------------------------------------------------------


def load_labels(csv_path: str | Path) -> list[LabelledPart]:
    """Read a labels CSV.

    Expected columns: ``step_path``, ``product_id``, ``expected_label``.
    Relative ``step_path`` values are resolved against the CSV's parent
    directory so corpus directories stay portable. Rows whose
    ``expected_label`` is not one of ``plaat``/``profiel``/``anders`` are
    silently skipped; rows missing any required column raise
    :class:`ValueError`.
    """
    csv_path = Path(csv_path).expanduser().resolve()
    base = csv_path.parent
    out: list[LabelledPart] = []
    with csv_path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        required = {"step_path", "product_id", "expected_label"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"labels CSV missing columns: {sorted(missing)}")
        for row in reader:
            label = (row.get("expected_label") or "").strip()
            if label not in ("plaat", "profiel", "anders"):
                continue
            raw_path = (row.get("step_path") or "").strip()
            if not raw_path:
                continue
            step_path = Path(raw_path)
            if not step_path.is_absolute():
                step_path = (base / step_path).resolve()
            out.append(
                LabelledPart(
                    step_path=step_path,
                    product_id=(row.get("product_id") or "").strip(),
                    expected_label=label,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


def _collect_features(parts: list[LabelledPart]) -> list[tuple[str, dict]]:
    """Run :func:`analyze` on each part and harvest classifier-feature dicts.

    Returns a list of ``(expected_label, clf_features)`` tuples. Parts that
    fail to parse or produce zero manifest entries are dropped with a
    no-op (the sweep cares about aggregate behaviour, not individual
    failures).

    A :class:`LabelledPart` with a non-empty ``product_id`` only labels the
    manifest entry whose parsed product id matches; one with an empty
    ``product_id`` labels every entry in the file.
    """
    opts = AnalyzeOptions(out_dir=None, write_dxf=False, write_xml=False)
    samples: list[tuple[str, dict]] = []
    for lp in parts:
        try:
            result = analyze(lp.step_path, opts)
        except Exception:
            continue
        for entry in result.manifest.parts:
            if lp.product_id and entry.part.product_id != lp.product_id:
                continue
            features = entry.features
            if features is None:
                continue
            profile_match = entry.profile_match
            unfold = entry.unfold
            try:
                clf = _classifier_features(features, profile_match, unfold)
            except Exception:
                continue
            samples.append((lp.expected_label, clf))
    return samples


def _scale_weights(spec: ScorerSpec, factor: float) -> ScorerSpec:
    """Return a deep copy of ``spec`` with all weights multiplied by ``factor``."""
    out: ScorerSpec = {}
    for cls, rules in spec.items():
        out[cls] = [(feat, fn, w * factor) for feat, fn, w in rules]
    return out


def _classify_corpus(
    samples: list[tuple[str, dict]],
    *,
    scorers: ScorerSpec,
    margin_thr: float,
    conf_thr: float,
    T: float,
) -> dict:
    """Classify the cached corpus and return a confusion dict."""
    clf = ScoreClassifier(
        scorers=scorers,
        margin_thr=margin_thr,
        conf_thr=conf_thr,
        T=T,
    )
    tiebreakers = [
        ("unfold", unfold_tiebreaker),
        ("cross_section", cross_section_tiebreaker),
        ("profile_match", profile_match_tiebreaker),
    ]
    confusion: dict = {}
    for true_label, clf_features in samples:
        result = clf.classify(
            clf_features,
            tiebreakers=tiebreakers,
            probe_results={
                "unfoldable": "true" if clf_features.get("unfoldable") else "false",
            },
        )
        key = (true_label, result.label)
        confusion[key] = confusion.get(key, 0) + 1
    return confusion


def sweep(
    parts: list[LabelledPart],
    *,
    temperatures: tuple = (0.3, 0.5, 0.7, 1.0),
    margin_thrs: tuple = (0.05, 0.10, 0.15, 0.20, 0.25),
    conf_thrs: tuple = (0.55, 0.60, 0.65, 0.70, 0.75),
    weight_jitters: tuple = (1.0,),
    cost_kwargs: dict | None = None,
) -> SweepResult:
    """Grid sweep over ``T x margin x conf x weight-jitter``.

    For each cell, run the classifier on the cached corpus, build the
    confusion matrix, and compute the cost with :func:`cost_weighted_score`.
    The cell with the lowest cost wins ties broken by the first cell
    visited in iteration order.

    ``weight_jitters`` is a tuple of multiplicative factors applied to
    every weight in :func:`default_scorers`. ``(1.0,)`` (the default) skips
    perturbation. Pass e.g. ``(0.8, 1.0, 1.2)`` for a +-20% sweep as the
    spec suggests.

    An empty corpus or a corpus that yields zero samples returns a
    :class:`SweepResult` with ``best_cost=inf`` and ``confusion={}``;
    downstream code can detect this and avoid persisting a meaningless
    result.
    """
    base_scorers = default_scorers()

    if not parts:
        return SweepResult(
            best_weights=base_scorers,
            best_cost=float("inf"),
            grid=[],
        )

    samples = _collect_features(parts)
    if not samples:
        return SweepResult(
            best_weights=base_scorers,
            best_cost=float("inf"),
            grid=[],
        )

    grid: list[dict] = []
    cost_kwargs = cost_kwargs or {}

    for jitter, T, margin_thr, conf_thr in itertools.product(
        weight_jitters, temperatures, margin_thrs, conf_thrs
    ):
        scorers = base_scorers if jitter == 1.0 else _scale_weights(base_scorers, jitter)
        confusion = _classify_corpus(
            samples,
            scorers=scorers,
            margin_thr=margin_thr,
            conf_thr=conf_thr,
            T=T,
        )
        cost = cost_weighted_score(confusion, **cost_kwargs)
        grid.append(
            {
                "temperature": float(T),
                "margin_thr": float(margin_thr),
                "conf_thr": float(conf_thr),
                "weight_jitter": float(jitter),
                "cost": cost,
                "confusion": _serialise_confusion(confusion),
            }
        )

    grid.sort(key=lambda c: c["cost"])
    best = grid[0]
    best_scorers = (
        base_scorers
        if best["weight_jitter"] == 1.0
        else _scale_weights(base_scorers, best["weight_jitter"])
    )
    # Re-run the winning cell to keep the confusion matrix as tuples
    # (the serialised grid form is JSON-friendly strings).
    best_confusion = _classify_corpus(
        samples,
        scorers=best_scorers,
        margin_thr=best["margin_thr"],
        conf_thr=best["conf_thr"],
        T=best["temperature"],
    )

    return SweepResult(
        best_weights=_weights_summary(best_scorers),
        best_margin_thr=best["margin_thr"],
        best_conf_thr=best["conf_thr"],
        best_temperature=best["temperature"],
        best_cost=best["cost"],
        confusion=best_confusion,
        grid=grid,
    )


def _serialise_confusion(confusion: dict) -> dict:
    """Turn ``{(true, pred): count}`` into a JSON-friendly ``{"true->pred": count}``."""
    return {f"{t}->{p}": int(c) for (t, p), c in confusion.items()}


def _weights_summary(spec: ScorerSpec) -> dict:
    """Reduce a ScorerSpec to a JSON-serialisable summary.

    The original spec carries callables (the transform functions) which are
    not serialisable. The summary keeps the class name, feature name and
    weight - enough to feed back into a tuning iteration.
    """
    out: dict = {}
    for cls, rules in spec.items():
        out[cls] = [{"feature": feat, "weight": float(w)} for feat, _fn, w in rules]
    return out


__all__ = [
    "LabelledPart",
    "SweepResult",
    "cost_weighted_score",
    "load_labels",
    "sweep",
]
