"""Merge helpers that combine FeatureExtractor output with sibling analyzers.

FeatureExtractor.extract() deliberately leaves hole-related fields empty;
HoleAnalyzer / ProfileMatcher fill those in. These helpers compose the
results into a single ManufacturingFeatures instance without mutating
the inputs.

Note: UnfoldResult is not merged here. ManufacturingFeatures has no
unfoldable field; unfold info reaches the scorer via the probe_results
dict in the score classifier.
"""

from __future__ import annotations

import dataclasses

from ..config.classification_variables import PROFILE_MATCH_CONFIDENCE_THRESHOLD
from .types import HolePattern, ManufacturingFeatures, ProfileMatch, UnfoldResult


def merge_holes_into_features(
    features: ManufacturingFeatures, hole_pattern: HolePattern
) -> ManufacturingFeatures:
    """Return a copy of `features` with `hole_count` and `hole_diameters` populated
    from `hole_pattern`. Does not mutate the input. Safe to call with empty pattern."""
    return dataclasses.replace(
        features,
        hole_count=hole_pattern.hole_count,
        hole_diameters=list(hole_pattern.diameters),
    )


def enrich(
    features: ManufacturingFeatures,
    *,
    holes: HolePattern | None = None,
    profile_match: ProfileMatch | None = None,
    unfold: UnfoldResult | None = None,
) -> ManufacturingFeatures:
    """Compose all available analyzer outputs into a single ManufacturingFeatures copy.

    - holes: populates hole_count + hole_diameters.
    - profile_match: sets cross_section_constant=True when confidence > 0.5.
    - unfold: currently a no-op; ManufacturingFeatures has no unfoldable field.
      Unfold info reaches the scorer via probe_results in the score classifier.
    """
    result = features
    if holes is not None:
        result = merge_holes_into_features(result, holes)
    if profile_match is not None and profile_match.confidence > PROFILE_MATCH_CONFIDENCE_THRESHOLD:
        result = dataclasses.replace(result, cross_section_constant=True)
    # unfold intentionally unused; see module docstring.
    _ = unfold
    return result
