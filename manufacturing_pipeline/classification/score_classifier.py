"""ScoreClassifier: per-class additive scorers + margin + tiebreakers + softmax."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from ..config.classification_variables import (
    CONFIDENCE_THRESHOLD,
    MARGIN_THRESHOLD,
    SOFTMAX_TEMPERATURE,
)
from .calibration import softmax
from .scorers import ScorerSpec, default_scorers
from .types import ClassificationResult, Contribution, DecisionTrace

Tiebreaker = tuple[str, Callable[[dict], str | None]]


class ScoreClassifier:
    def __init__(
        self,
        scorers: ScorerSpec | None = None,
        margin_thr: float | None = None,
        conf_thr: float | None = None,
        T: float | None = None,
        model_version: str = "rules-0.1.0",
    ):
        self.scorers = scorers if scorers is not None else default_scorers()
        self.margin_thr = margin_thr if margin_thr is not None else MARGIN_THRESHOLD
        self.conf_thr = conf_thr if conf_thr is not None else CONFIDENCE_THRESHOLD
        self.T = T if T is not None else SOFTMAX_TEMPERATURE
        self.model_version = model_version

    def classify(
        self,
        features: dict,
        tiebreakers: Iterable[Tiebreaker] = (),
        probe_results: dict | None = None,
    ) -> ClassificationResult:
        scores: dict = {}
        contribs: list[Contribution] = []
        for cls, rules in self.scorers.items():
            s = 0.0
            for feat, fn, w in rules:
                if isinstance(feat, tuple):
                    # Cross-term rule: gather one value per named feature.
                    values = tuple(features.get(name, 0.0) for name in feat)
                    d = w * fn(values)
                    feat_label = ",".join(feat)
                    value_repr: float | str = str(values)
                else:
                    values = features.get(feat, 0.0)
                    d = w * fn(values)
                    feat_label = feat
                    value_repr = values
                s += d
                contribs.append(Contribution(feat_label, cls, value_repr, d))
            scores[cls] = s

        ranked = sorted(scores.items(), key=lambda x: -x[1])
        margin = ranked[0][1] - ranked[1][1] if len(ranked) > 1 else float("inf")
        ambiguous = margin < self.margin_thr
        winner = ranked[0][0]
        run: list[str] = []

        if ambiguous:
            for name, probe in tiebreakers:
                run.append(name)
                hit = probe(features)
                if hit is not None:
                    winner = hit
                    break

        probs = softmax(scores, T=self.T)
        conf = probs[winner]
        label = winner if conf >= self.conf_thr else "uncertain"

        trace = DecisionTrace(
            scores=scores,
            probabilities=probs,
            margin=margin,
            ambiguous=ambiguous,
            contributions=contribs,
            tiebreakers_run=run,
            probe_results=probe_results or {},
            model_version=self.model_version,
        )
        return ClassificationResult(label=label, confidence=conf, trace=trace)
