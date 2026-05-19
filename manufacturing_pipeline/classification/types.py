"""Public types for the classification layer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Contribution:
    feature: str
    cls: str
    # ``value`` is a float for single-feature rules and a stringified tuple
    # (e.g. ``"(0.45, True)"``) for cross-term rules where ``feature`` is the
    # comma-joined list of names. The schema slots stay the same; only the
    # representation widens.
    value: float | str
    delta: float


@dataclass
class DecisionTrace:
    scores: dict = field(default_factory=dict)
    probabilities: dict = field(default_factory=dict)
    margin: float = 0.0
    ambiguous: bool = False
    contributions: list[Contribution] = field(default_factory=list)
    tiebreakers_run: list[str] = field(default_factory=list)
    probe_results: dict = field(default_factory=dict)
    model_version: str = "rules-0.0.0"


@dataclass
class ClassificationResult:
    label: str  # plaat | profiel | anders | uncertain
    confidence: float
    trace: DecisionTrace
