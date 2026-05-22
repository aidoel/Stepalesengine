"""Per-class additive scoring functions.

Every feature contributes to every class. Name is a contributor, never a gate.
Weights are tunable via the YAML config at
``manufacturing_pipeline/data/scorer_weights.yaml``; a custom path can be
passed to :func:`load_scorers_from_yaml`.

Rule forms
----------
A rule is a 3-tuple ``(feature_ref, transform_fn, weight)``. ``feature_ref``
may be either:

* a single feature name (``str``) - the classic single-feature rule. The
  transform receives one scalar: ``transform_fn(features[name])``.
* a tuple of feature names (``tuple[str, ...]``) - a cross-term rule. The
  transform receives a tuple of values, one per name, in the order given:
  ``transform_fn((features[a], features[b], ...))``. Missing features fall
  back to ``0.0``.

Cross-terms exist to encode that two features together carry information the
sum of their single-feature rules cannot. The default scorers use this to
distinguish a real sheet-metal plate (high ``top1_face_pct`` AND ``unfoldable``)
from a machined part that happens to have one large planar face but is not
unfoldable: the latter is rewarded as ``anders`` instead of leaking into
``plaat``. See ``docs/adr/0008-scorer-cross-terms.md``.

YAML loader
-----------
The bundled :func:`default_scorers` reads
``manufacturing_pipeline/data/scorer_weights.yaml`` so weights can be tuned
without code changes. The YAML names a transform from the small
:data:`_TRANSFORMS` registry below; nothing in the YAML is ever ``eval``-ed,
so the file is safe to share. To add a transform, register it here and use
its name from YAML.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

# scorers schema: {class_name: [(feature_ref, transform_fn, weight), ...]}
# feature_ref is either a single feature name (str) or a tuple of names for a
# cross-term rule.
ScorerRule = tuple[str | tuple[str, ...], Callable[..., float], float]
ScorerSpec = dict[str, list[ScorerRule]]


def linear(x: float) -> float:
    return float(x)


def boolf(x: Any) -> float:
    return 1.0 if x else 0.0


def _neg_clamp10(x: float) -> float:
    """-min(x, 10) / 10 - penalises large aspect ratios in plate scoring."""
    return -min(x, 10) / 10


def _clamp10(x: float) -> float:
    """min(x, 10) / 10 - rewards large aspect ratios in profile scoring."""
    return min(x, 10) / 10


def _top1_times_unfoldable(v: tuple[Any, ...]) -> float:
    """top1_face_pct * 1[unfoldable] - the real-plate cross-term."""
    return v[0] * (1.0 if v[1] else 0.0)


def _top1_times_not_unfoldable(v: tuple[Any, ...]) -> float:
    """top1_face_pct * 1[not unfoldable] - the machined-lookalike cross-term."""
    return v[0] * (0.0 if v[1] else 1.0)


def _and_not(v: tuple[Any, ...]) -> float:
    """1.0 when ``v[0]`` is truthy AND ``v[1]`` is falsy, else 0.0.

    Used to gate a sheet-metal reward (``unfoldable`` / ``has_bends``) off the
    ``seamed_tube`` flag: a seamed hollow section unfolds with bends like a
    bent plate, but it is a constant-section profile, so the plate reward must
    not fire for it.
    """
    return 1.0 if (v[0] and not v[1]) else 0.0


# Registry of named transforms allowed from YAML. Keep small and well-known;
# never eval anything from the YAML file.
_TRANSFORMS: dict[str, Callable[..., float]] = {
    "linear": linear,
    "bool": boolf,
    "neg_clamp10": _neg_clamp10,
    "clamp10": _clamp10,
    "top1_times_unfoldable": _top1_times_unfoldable,
    "top1_times_not_unfoldable": _top1_times_not_unfoldable,
    "and_not": _and_not,
}


def _default_yaml_path() -> Path:
    """Path to the bundled scorer_weights.yaml."""
    return Path(__file__).resolve().parent.parent / "data" / "scorer_weights.yaml"


def _coerce_feature_ref(raw: Any) -> str | tuple[str, ...]:
    """Map a YAML ``feature:`` entry onto the in-memory feature_ref type."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list) and all(isinstance(name, str) for name in raw):
        if len(raw) == 0:
            raise ValueError("feature list cannot be empty")
        return tuple(raw)
    raise ValueError(f"feature must be a string or a list of strings, got: {raw!r}")


def load_scorers_from_yaml(path: str | Path) -> ScorerSpec:
    """Parse a YAML scorer-weights config into a :class:`ScorerSpec`.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    ValueError
        If the YAML is malformed, references an unknown transform, or is
        missing the required ``classes`` mapping.
    """
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed YAML in {p}: {exc}") from exc

    if not isinstance(doc, dict):
        raise ValueError(f"{p}: top-level YAML must be a mapping")

    classes = doc.get("classes")
    if not isinstance(classes, dict) or not classes:
        raise ValueError(f"{p}: missing or empty 'classes' mapping")

    spec: ScorerSpec = {}
    for cls_name, rules in classes.items():
        if not isinstance(cls_name, str):
            raise ValueError(f"{p}: class name must be a string, got {cls_name!r}")
        if not isinstance(rules, list):
            raise ValueError(f"{p}: class '{cls_name}' must hold a list of rules")
        parsed: list[ScorerRule] = []
        for idx, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise ValueError(f"{p}: class '{cls_name}' rule #{idx} must be a mapping")
            try:
                feature_ref = _coerce_feature_ref(rule["feature"])
                transform_name = rule["transform"]
                weight = float(rule["weight"])
            except KeyError as exc:
                raise ValueError(f"{p}: class '{cls_name}' rule #{idx} missing key {exc}") from exc
            if not isinstance(transform_name, str):
                raise ValueError(f"{p}: class '{cls_name}' rule #{idx} transform must be a string")
            if transform_name not in _TRANSFORMS:
                raise ValueError(
                    f"{p}: class '{cls_name}' rule #{idx} references unknown "
                    f"transform '{transform_name}'. Known: {sorted(_TRANSFORMS)}"
                )
            parsed.append((feature_ref, _TRANSFORMS[transform_name], weight))
        spec[cls_name] = parsed
    return spec


# Module-level cache for the bundled default. Loaded lazily so tests can stub
# the YAML file via load_scorers_from_yaml(path) without paying the load cost
# on import.
_default_spec_cache: ScorerSpec | None = None


def default_scorers() -> ScorerSpec:
    """Load the bundled default scorer spec from YAML, cached on first call.

    The returned spec is identical to what was previously hardcoded in this
    module. See ``manufacturing_pipeline/data/scorer_weights.yaml`` for the
    canonical weights and ``docs/adr/0008-scorer-cross-terms.md`` for the
    cross-term rationale.

    Weights are scaled so that ideal cases (a clean flat plate, a clear
    RHS/CHS profile, an obvious b-spline freeform body) clear the
    ``CONFIDENCE_THRESHOLD`` once softmax is applied with the calibrated
    ``SOFTMAX_TEMPERATURE``.
    """
    global _default_spec_cache
    if _default_spec_cache is None:
        _default_spec_cache = load_scorers_from_yaml(_default_yaml_path())
    return _default_spec_cache
