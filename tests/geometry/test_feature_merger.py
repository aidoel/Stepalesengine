"""Tests for feature_merger: combining FeatureExtractor output with sibling analyzers."""

from __future__ import annotations

from manufacturing_pipeline.geometry.feature_merger import (
    enrich,
    merge_holes_into_features,
)
from manufacturing_pipeline.geometry.types import (
    HolePattern,
    ManufacturingFeatures,
    ProfileMatch,
)


def _base_features() -> ManufacturingFeatures:
    return ManufacturingFeatures(volume=1000.0, surface_area=600.0)


def test_merge_empty_hole_pattern_leaves_count_zero():
    features = _base_features()
    empty = HolePattern()

    merged = merge_holes_into_features(features, empty)

    assert merged.hole_count == 0
    assert merged.hole_diameters == []


def test_merge_three_holes_populates_count_and_diameters():
    features = _base_features()
    pattern = HolePattern(hole_count=3, diameters=[5.0, 6.0, 8.0])

    merged = merge_holes_into_features(features, pattern)

    assert merged.hole_count == 3
    assert merged.hole_diameters == [5.0, 6.0, 8.0]


def test_merge_does_not_mutate_input_features():
    features = _base_features()
    pattern = HolePattern(hole_count=2, diameters=[10.0, 12.0])

    merged = merge_holes_into_features(features, pattern)

    assert features.hole_count == 0
    assert features.hole_diameters == []
    # And the returned diameters list is independent of the source.
    merged.hole_diameters.append(99.0)
    assert pattern.diameters == [10.0, 12.0]


def test_merge_returns_new_instance():
    features = _base_features()
    pattern = HolePattern(hole_count=1, diameters=[4.0])

    merged = merge_holes_into_features(features, pattern)

    assert merged is not features


def test_enrich_with_high_confidence_profile_sets_cross_section_constant():
    features = _base_features()
    pattern = HolePattern(hole_count=2, diameters=[5.0, 7.0])
    high_conf = ProfileMatch(
        family="I",
        standard="EN10025",
        designation="IPE200",
        fitted_dims={},
        residual_mm=0.1,
        confidence=0.9,
    )

    enriched = enrich(features, holes=pattern, profile_match=high_conf)

    assert enriched.cross_section_constant is True
    assert enriched.hole_count == 2
    assert enriched.hole_diameters == [5.0, 7.0]
    # Input untouched.
    assert features.cross_section_constant is False
    assert features.hole_count == 0


def test_enrich_with_low_confidence_profile_leaves_cross_section_constant_false():
    features = _base_features()
    low_conf = ProfileMatch(
        family="U",
        standard="EN10025",
        designation=None,
        fitted_dims={},
        residual_mm=2.0,
        confidence=0.3,
    )

    enriched = enrich(features, profile_match=low_conf)

    assert enriched.cross_section_constant is False
