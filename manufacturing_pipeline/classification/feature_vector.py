"""Typed feature vector consumed by the score classifier.

The classifier walks its scorers by name, so a flat dict is its native
input. This module wraps that dict in a dataclass so producers can't
accidentally introduce typos when adding a new feature.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..geometry.types import ManufacturingFeatures, ProfileMatch, UnfoldResult, UnfoldStatus

if TYPE_CHECKING:
    pass


@dataclass
class FeatureVector:
    """Typed feature vector consumed by the score classifier. Each field
    maps directly to a scorer rule's feature_ref."""

    top1_face_pct: float = 0.0
    unfoldable: bool = False
    has_bends: bool = False
    seamed_tube: bool = False
    aspect_ratio: float = 0.0
    cross_section_constant: bool = False
    name_profile_hit: float = 0.0
    name_din_hit: float = 0.0
    vendor_code_present: float = 0.0
    bspline_pct: float = 0.0
    profile_match_designation: float = 0.0  # 1.0 if matched, else 0.0
    hole_density: float = 0.0
    hull_concavity: float = 0.0
    pocket_complexity: float = 0.0
    # Fraction of total surface area carried by cylindrical faces. Round bars,
    # turned/machined shafts and seamed pipe walls all run high here even when
    # the cross-section is not constant enough for the matcher to lock onto a
    # standard profile. Plates and freeform bspline bodies stay low.
    cylindrical_pct: float = 0.0

    def as_dict(self) -> dict[str, object]:
        """Return the dict form ScoreClassifier expects. The classifier
        reads features by name, so a flat dict is its native input."""
        return dataclasses.asdict(self)

    @classmethod
    def from_features(
        cls,
        features: ManufacturingFeatures,
        profile_match: ProfileMatch | None = None,
        unfold: UnfoldResult | None = None,
    ) -> FeatureVector:
        """Construct from analyzer outputs (replaces _classifier_features)."""
        top1 = features.face_area_top[0] if features.face_area_top else 0.0
        bspline_pct = features.surface_pct.get("bspline", 0.0)
        cylindrical_pct = features.surface_pct.get("cylindrical", 0.0)
        # A PARTIAL unfold (thickness variation, branching flanges, rolled or
        # folded seamed tubes) is still genuine sheet metal: the probe produced
        # a flat pattern, just not a fully clean one. Machined and freeform
        # parts emit FAILURE, so counting PARTIAL as ``unfoldable`` keeps this
        # feature consistent with ``has_bends`` below (also ``!= FAILURE``)
        # without leaking machined parts into ``plaat``.
        unfoldable = unfold is not None and unfold.status != UnfoldStatus.FAILURE
        # A part that unfolds *with bends* is unambiguously bent sheet metal:
        # machined and freeform parts do not unfold, and a flat plate has no
        # bends. PARTIAL still counts - the bends were detected.
        has_bends = (
            unfold is not None and unfold.status != UnfoldStatus.FAILURE and unfold.n_bends > 0
        )
        # A hollow / seamed structural section (RHS, SHS, CHS, welded channel)
        # is a closed tube wall: the UnfoldProbe rolls it open and reports a
        # PARTIAL unfold with ``seamed_section`` set, so ``unfoldable`` and
        # ``has_bends`` both fire. Geometrically that is a constant-section
        # extrusion - a profile, not bent sheet metal. This flag lets the
        # scorer gate the sheet-metal rewards off seamed tubes without losing
        # the unfold signal a genuine bent plate still needs.
        seamed_tube = (
            unfold is not None
            and unfold.status != UnfoldStatus.FAILURE
            and bool(unfold.flags.get("seamed_section", False))
        )
        profile_designation_hit = (
            1.0 if (profile_match is not None and profile_match.designation) else 0.0
        )
        # Machined-part signals: high hole density relative to surface area,
        # and a convex-hull ratio well below 1.0 (lots of pockets / cutouts)
        # both point to "anders" (purchased / machined complex part).
        hole_density_norm = 0.0
        if features.surface_area > 0:
            # holes per cm^2 of skin, clamped to [0, 1] for scoring stability
            raw = features.hole_count / (features.surface_area / 100.0)
            hole_density_norm = min(max(raw / 5.0, 0.0), 1.0)
        hull_concavity = max(0.0, min(1.0, 1.0 - features.convex_hull_volume_ratio))
        return cls(
            top1_face_pct=top1,
            unfoldable=bool(unfoldable),
            has_bends=bool(has_bends),
            seamed_tube=bool(seamed_tube),
            aspect_ratio=features.aspect_ratio,
            cross_section_constant=features.cross_section_constant,
            name_profile_hit=0.0,
            name_din_hit=0.0,
            vendor_code_present=0.0,
            bspline_pct=bspline_pct,
            profile_match_designation=profile_designation_hit,
            hole_density=hole_density_norm,
            hull_concavity=hull_concavity,
            pocket_complexity=features.pocket_complexity,
            cylindrical_pct=cylindrical_pct,
        )


__all__ = ["FeatureVector"]
