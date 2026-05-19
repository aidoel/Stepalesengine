"""Cross-term scorer tests.

The ScoreClassifier accepts two rule shapes:

* ``(feature_name: str, transform_fn, weight)`` - single-feature rule.
* ``(feature_names: tuple[str, ...], transform_fn, weight)`` - cross-term rule
  where ``transform_fn`` receives a tuple of feature values.

These tests pin both shapes and verify the failure mode the cross-terms exist
to fix: a part that is plate-looking by ``top1_face_pct`` alone but is not
unfoldable must score lower as ``plaat`` than the same shape with
``unfoldable=True``.
"""

from __future__ import annotations

from manufacturing_pipeline.classification.score_classifier import ScoreClassifier
from manufacturing_pipeline.classification.scorers import default_scorers


def _classifier(spec):
    return ScoreClassifier(scorers=spec, conf_thr=0.0)


def test_single_feature_rule_still_works():
    """Backward-compat: classic ``(name, fn, w)`` rules behave unchanged."""
    spec = {
        "plaat": [("top1_face_pct", lambda x: x, 2.0)],
        "profiel": [("aspect_ratio", lambda x: x / 10.0, 1.0)],
        "anders": [("bspline_pct", lambda x: x, 1.0)],
    }
    res = _classifier(spec).classify(
        {"top1_face_pct": 0.5, "aspect_ratio": 4.0, "bspline_pct": 0.1}
    )
    assert res.trace.scores["plaat"] == 1.0
    assert res.trace.scores["profiel"] == 0.4
    assert res.trace.scores["anders"] == 0.1


def test_tuple_feature_rule_multiplies_two_values():
    """Cross-term: ``transform_fn`` receives a tuple of feature values."""
    spec = {
        "plaat": [(("top1_face_pct", "unfoldable"), lambda v: v[0] * (1.0 if v[1] else 0.0), 1.0)],
        "profiel": [],
        "anders": [],
    }
    res = _classifier(spec).classify({"top1_face_pct": 0.6, "unfoldable": True})
    assert res.trace.scores["plaat"] == 0.6
    # Same top1 but not unfoldable: cross-term collapses to zero.
    res_off = _classifier(spec).classify({"top1_face_pct": 0.6, "unfoldable": False})
    assert res_off.trace.scores["plaat"] == 0.0


def test_tuple_feature_rule_missing_feature_defaults_to_zero():
    """Missing tuple-feature keys must fall back to 0.0, not raise KeyError."""
    spec = {
        "plaat": [(("top1_face_pct", "unfoldable"), lambda v: v[0] + (1.0 if v[1] else 0.0), 1.0)],
        "profiel": [],
        "anders": [],
    }
    # Pass no features at all - both lookups must default cleanly.
    res = _classifier(spec).classify({})
    assert res.trace.scores["plaat"] == 0.0
    # Only top1 present, unfoldable missing - the bool branch sees 0.0 (falsy).
    res_partial = _classifier(spec).classify({"top1_face_pct": 0.7})
    assert res_partial.trace.scores["plaat"] == 0.7


def test_cross_term_contribution_records_joined_feature_name():
    """Contribution's ``feature`` slot must be the comma-joined names; the
    ``value`` slot carries the stringified tuple of values."""
    spec = {
        "plaat": [(("top1_face_pct", "unfoldable"), lambda v: v[0] * (1.0 if v[1] else 0.0), 1.0)],
        "profiel": [],
        "anders": [],
    }
    res = _classifier(spec).classify({"top1_face_pct": 0.42, "unfoldable": True})
    plaat_contribs = [c for c in res.trace.contributions if c.cls == "plaat"]
    assert len(plaat_contribs) == 1
    c = plaat_contribs[0]
    assert c.feature == "top1_face_pct,unfoldable"
    # Stringified tuple - shape is deterministic, exact format is repr().
    assert c.value == str((0.42, True))
    assert c.delta == 0.42


def test_default_plaat_cross_term_punishes_non_unfoldable_lookalikes():
    """The default scorers must score (top1=0.45, unfoldable=False) strictly
    lower for ``plaat`` than (top1=0.45, unfoldable=True). This is the failure
    mode that motivated adding cross-terms.
    """
    clf = ScoreClassifier(scorers=default_scorers(), conf_thr=0.0)
    base = {
        "aspect_ratio": 1.5,
        "cross_section_constant": False,
        "name_profile_hit": 0.0,
        "name_din_hit": 0.0,
        "vendor_code_present": 0.0,
        "bspline_pct": 0.0,
        "hole_density": 0.0,
        "hull_concavity": 0.0,
        "profile_match_designation": 0.0,
    }
    fake_plaat = clf.classify({**base, "top1_face_pct": 0.45, "unfoldable": False})
    real_plaat = clf.classify({**base, "top1_face_pct": 0.45, "unfoldable": True})
    assert real_plaat.trace.scores["plaat"] > fake_plaat.trace.scores["plaat"], (
        f"real plaat score {real_plaat.trace.scores['plaat']:.3f} not greater "
        f"than fake plaat score {fake_plaat.trace.scores['plaat']:.3f}"
    )
    # And the fake-plaat case should be classified as ``anders``, not ``plaat``.
    assert fake_plaat.label == "anders", fake_plaat.trace.scores
    assert real_plaat.label == "plaat", real_plaat.trace.scores
