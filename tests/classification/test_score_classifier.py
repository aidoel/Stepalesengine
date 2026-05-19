"""Score classifier tests — uses the default scorers wired in scorers.py."""

from __future__ import annotations

from manufacturing_pipeline.classification.score_classifier import ScoreClassifier
from manufacturing_pipeline.classification.tiebreakers import (
    cross_section_tiebreaker,
    unfold_tiebreaker,
)


def test_clear_plaat():
    clf = ScoreClassifier(conf_thr=0.0)
    features = {
        "top1_face_pct": 0.9,
        "unfoldable": True,
        "aspect_ratio": 2.0,
        "cross_section_constant": False,
        "name_profile_hit": 0.0,
        "name_din_hit": 0.0,
        "vendor_code_present": 0.0,
        "bspline_pct": 0.0,
    }
    res = clf.classify(features)
    assert res.label == "plaat"
    assert res.trace.scores["plaat"] > res.trace.scores["profiel"]
    assert res.trace.scores["plaat"] > res.trace.scores["anders"]


def test_clear_profiel():
    clf = ScoreClassifier(conf_thr=0.0)
    features = {
        "top1_face_pct": 0.2,
        "unfoldable": False,
        "aspect_ratio": 9.0,
        "cross_section_constant": True,
        "name_profile_hit": 1.0,
        "name_din_hit": 0.0,
        "vendor_code_present": 0.0,
        "bspline_pct": 0.0,
    }
    res = clf.classify(features)
    assert res.label == "profiel"


def test_din_name_does_not_veto_plaat_geometry():
    """Regression: legacy first-match would mark this 'anders' on the DIN name."""
    clf = ScoreClassifier(conf_thr=0.0)
    features = {
        "top1_face_pct": 0.85,
        "unfoldable": True,
        "aspect_ratio": 2.5,
        "cross_section_constant": False,
        "name_profile_hit": 0.0,
        "name_din_hit": 1.0,  # name says "DIN-bracket-12mm"
        "vendor_code_present": 0.0,
        "bspline_pct": 0.0,
    }
    res = clf.classify(features)
    assert res.label == "plaat", res.trace.scores


def test_ambiguous_triggers_tiebreaker():
    clf = ScoreClassifier(margin_thr=10.0, conf_thr=0.0)
    # margin_thr=10 forces ambiguous → tiebreaker pipeline runs.
    features = {
        "top1_face_pct": 0.5,
        "unfoldable": True,
        "aspect_ratio": 5.0,
        "cross_section_constant": False,
        "name_profile_hit": 0.0,
        "name_din_hit": 0.0,
        "vendor_code_present": 0.0,
        "bspline_pct": 0.0,
    }
    res = clf.classify(
        features,
        tiebreakers=[("unfold", unfold_tiebreaker), ("xsec", cross_section_tiebreaker)],
    )
    assert res.trace.ambiguous
    assert "unfold" in res.trace.tiebreakers_run


def test_decision_trace_populated():
    import math

    clf = ScoreClassifier()
    features = {"top1_face_pct": 0.9, "unfoldable": True}
    res = clf.classify(features)
    assert res.trace.scores
    assert res.trace.probabilities
    assert res.trace.contributions
    # Probabilities must sum to 1.0 within float rounding tolerance.
    assert math.isclose(sum(res.trace.probabilities.values()), 1.0, abs_tol=1e-6)
