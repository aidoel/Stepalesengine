"""Confidence-target tests for the calibrated ScoreClassifier.

These tests pin the calibration: obvious flat plates, obvious profiles and
obvious freeform bodies must clear ``CONFIDENCE_THRESHOLD`` (>= 0.70) so the
downstream DXF writer is not skipped. Genuinely ambiguous geometry must stay
below 0.65 so it falls back to ``label='uncertain'``.
"""

from __future__ import annotations

from manufacturing_pipeline.classification.score_classifier import ScoreClassifier
from manufacturing_pipeline.classification.tiebreakers import (
    cross_section_tiebreaker,
    profile_match_tiebreaker,
    unfold_tiebreaker,
)

TIEBREAKERS = [
    ("unfold", unfold_tiebreaker),
    ("cross_section", cross_section_tiebreaker),
    ("profile_match", profile_match_tiebreaker),
]


def _classify(features: dict):
    return ScoreClassifier().classify(features, tiebreakers=TIEBREAKERS)


def test_ideal_plaat_clears_confidence_threshold():
    """A 100x60x2 flat plate: top+bottom face dominate, unfoldable, low aspect."""
    features = {
        "top1_face_pct": 0.45,
        "unfoldable": True,
        "aspect_ratio": 1.67,
        "cross_section_constant": False,
        "name_profile_hit": 0.0,
        "name_din_hit": 0.0,
        "vendor_code_present": 0.0,
        "bspline_pct": 0.0,
        "profile_match_designation": None,
    }
    res = _classify(features)
    assert res.label == "plaat", res.trace.probabilities
    assert (
        res.confidence >= 0.70
    ), f"plaat conf={res.confidence:.3f} below 0.70; probs={res.trace.probabilities}"


def test_ideal_profiel_clears_confidence_threshold():
    """A clean RHS profile: constant cross-section, high aspect ratio."""
    features = {
        "top1_face_pct": 0.25,
        "unfoldable": False,
        "aspect_ratio": 10.0,
        "cross_section_constant": True,
        "name_profile_hit": 0.0,
        "name_din_hit": 0.0,
        "vendor_code_present": 0.0,
        "bspline_pct": 0.0,
        "profile_match_designation": None,
    }
    res = _classify(features)
    assert res.label == "profiel", res.trace.probabilities
    assert (
        res.confidence >= 0.70
    ), f"profiel conf={res.confidence:.3f} below 0.70; probs={res.trace.probabilities}"


def test_ideal_anders_clears_confidence_threshold():
    """A freeform body with dominant b-spline surface fraction."""
    features = {
        "top1_face_pct": 0.0,
        "unfoldable": False,
        "aspect_ratio": 2.0,
        "cross_section_constant": False,
        "name_profile_hit": 0.0,
        "name_din_hit": 0.0,
        "vendor_code_present": 0.0,
        "bspline_pct": 0.6,
        "profile_match_designation": None,
    }
    res = _classify(features)
    assert res.label == "anders", res.trace.probabilities
    assert (
        res.confidence >= 0.70
    ), f"anders conf={res.confidence:.3f} below 0.70; probs={res.trace.probabilities}"


def test_ambiguous_case_stays_uncertain_or_low_confidence():
    """No dominant signal: classifier must either bail to 'uncertain' or stay <0.65."""
    features = {
        "top1_face_pct": 0.4,
        "unfoldable": False,
        "aspect_ratio": 3.0,
        "cross_section_constant": False,
        "name_profile_hit": 0.0,
        "name_din_hit": 0.0,
        "vendor_code_present": 0.0,
        "bspline_pct": 0.1,
        "profile_match_designation": None,
    }
    res = _classify(features)
    assert res.label == "uncertain" or res.confidence < 0.65, (
        f"ambiguous case became label={res.label!r} conf={res.confidence:.3f}; "
        f"probs={res.trace.probabilities}"
    )


def test_ideal_plaat_requires_both_top1_and_unfoldable():
    """Cross-term guard (ADR 0008): top1_face_pct alone is no longer enough to
    pass the plaat confidence floor. Drop ``unfoldable`` and the same geometry
    must NOT come back as a high-confidence plate; it should resolve to
    ``anders`` (the machined-part-with-a-flat-top failure mode).
    """
    features = {
        "top1_face_pct": 0.45,
        "unfoldable": False,
        "aspect_ratio": 1.67,
        "cross_section_constant": False,
        "name_profile_hit": 0.0,
        "name_din_hit": 0.0,
        "vendor_code_present": 0.0,
        "bspline_pct": 0.0,
        "profile_match_designation": None,
    }
    res = _classify(features)
    assert (
        res.label != "plaat"
    ), f"non-unfoldable top1=0.45 leaked into plaat: probs={res.trace.probabilities}"


def test_lbracket_clears_relaxed_confidence_threshold():
    """An L-bracket: top1 only ~0.40 but still unfoldable; must reach >= 0.65 as plaat."""
    features = {
        "top1_face_pct": 0.4,
        "unfoldable": True,
        "aspect_ratio": 1.2,
        "cross_section_constant": False,
        "name_profile_hit": 0.0,
        "name_din_hit": 0.0,
        "vendor_code_present": 0.0,
        "bspline_pct": 0.0,
        "profile_match_designation": None,
    }
    res = _classify(features)
    assert res.label == "plaat", res.trace.probabilities
    assert (
        res.confidence >= 0.65
    ), f"L-bracket conf={res.confidence:.3f} below 0.65; probs={res.trace.probabilities}"
