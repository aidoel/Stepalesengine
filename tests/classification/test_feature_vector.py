"""Tests for the typed FeatureVector producer.

The classifier still consumes a flat dict, so :meth:`FeatureVector.as_dict`
is the boundary under test: the dataclass form lets callers add features
without typos, the dict form keeps the classifier untouched.
"""

from __future__ import annotations

import dataclasses

from manufacturing_pipeline.classification.feature_vector import FeatureVector
from manufacturing_pipeline.geometry.types import (
    ManufacturingFeatures,
    ProfileMatch,
    UnfoldResult,
    UnfoldStatus,
)


def test_default_feature_vector_as_dict_has_all_keys():
    fv = FeatureVector()
    d = fv.as_dict()
    expected = {
        "top1_face_pct",
        "unfoldable",
        "has_bends",
        "aspect_ratio",
        "cross_section_constant",
        "name_profile_hit",
        "name_din_hit",
        "vendor_code_present",
        "bspline_pct",
        "profile_match_designation",
        "hole_density",
        "hull_concavity",
        "pocket_complexity",
    }
    assert set(d.keys()) == expected
    assert len(d) == 13


def test_round_trip_fv_to_dict_and_back_preserves_every_field():
    fv = FeatureVector(
        top1_face_pct=0.75,
        unfoldable=True,
        has_bends=True,
        aspect_ratio=4.2,
        cross_section_constant=True,
        name_profile_hit=1.0,
        name_din_hit=0.5,
        vendor_code_present=1.0,
        bspline_pct=0.1,
        profile_match_designation=1.0,
        hole_density=0.3,
        hull_concavity=0.6,
        pocket_complexity=0.8,
    )
    d = fv.as_dict()
    rebuilt = FeatureVector(**d)
    assert rebuilt == fv
    # And dataclasses.asdict gives the same shape.
    assert dataclasses.asdict(rebuilt) == d


def test_from_features_derives_hole_density_and_hull_concavity():
    # surface_area = 200 cm^2 worth (in mm^2 -> /100 cm^2 -> raw =
    # hole_count / 2 -> normalised by /5 -> clamp [0,1])
    features = ManufacturingFeatures(
        surface_area=20000.0,
        hole_count=10,
        convex_hull_volume_ratio=0.7,
    )
    fv = FeatureVector.from_features(features)
    # raw = 10 / (20000/100) = 0.05; norm = 0.05 / 5.0 = 0.01
    assert fv.hole_density == 0.01
    # 1 - 0.7 = 0.3
    assert abs(fv.hull_concavity - 0.3) < 1e-9


def test_from_features_with_profile_designation_sets_designation_hit_to_one():
    features = ManufacturingFeatures()
    profile_match = ProfileMatch(
        family="I",
        standard="EN 10025",
        designation="IPE 200",
        fitted_dims={},
        residual_mm=0.5,
        confidence=0.9,
    )
    fv = FeatureVector.from_features(features, profile_match=profile_match)
    assert fv.profile_match_designation == 1.0


def test_from_features_with_successful_unfold_sets_unfoldable_true():
    features = ManufacturingFeatures()
    unfold = UnfoldResult(status=UnfoldStatus.SUCCESS, n_bends=2)
    fv = FeatureVector.from_features(features, unfold=unfold)
    assert fv.unfoldable is True
    # Without unfold, default is False.
    fv2 = FeatureVector.from_features(features, unfold=None)
    assert fv2.unfoldable is False


def test_from_features_partial_unfold_counts_as_unfoldable():
    """A PARTIAL unfold is genuine sheet metal (thickness variation, branching
    flanges, seamed tubes), so it must still set ``unfoldable``. A FAILURE must
    not."""
    features = ManufacturingFeatures()
    partial = FeatureVector.from_features(
        features, unfold=UnfoldResult(status=UnfoldStatus.PARTIAL, n_bends=0)
    )
    assert partial.unfoldable is True
    failed = FeatureVector.from_features(
        features, unfold=UnfoldResult(status=UnfoldStatus.FAILURE, n_bends=0)
    )
    assert failed.unfoldable is False


def test_from_features_matches_legacy_classifier_features_dict():
    """The dict produced by FeatureVector.from_features(...).as_dict() must
    match the keys the legacy ``_classifier_features`` produced."""
    from manufacturing_pipeline.pipeline.analyze_assembly import (
        _classifier_features,
    )

    features = ManufacturingFeatures(
        surface_area=15000.0,
        hole_count=4,
        face_area_top=[0.7, 0.2, 0.05],
        surface_pct={"bspline": 0.05},
        aspect_ratio=3.0,
        cross_section_constant=False,
        convex_hull_volume_ratio=0.85,
        pocket_complexity=0.2,
    )
    profile_match = ProfileMatch(
        family="I",
        standard="EN 10025",
        designation="IPE 100",
        fitted_dims={},
        residual_mm=0.1,
        confidence=0.95,
    )
    unfold = UnfoldResult(status=UnfoldStatus.SUCCESS)
    fv_dict = FeatureVector.from_features(
        features, profile_match=profile_match, unfold=unfold
    ).as_dict()
    legacy = _classifier_features(features, profile_match, unfold)
    assert fv_dict == legacy
