"""CAM strategy probe.

Wraps :func:`cam.strategist.recommend`. Reads holes/unfold/profile/pmi from
``ctx.prior`` (populated by earlier probes) plus the classification result
the orchestrator injects after classifying.
"""

from __future__ import annotations

from ...cam.strategist import recommend as cam_recommend
from ...cam.types import MachiningStrategy
from ...classification.types import ClassificationResult, DecisionTrace
from ...geometry.types import HolePattern, ManufacturingFeatures, ProfileMatch, UnfoldResult
from ...pmi.types import PMIRecord
from . import ProbeContext


class CamProbe:
    """Adapter so :func:`cam_recommend` slots into the registry.

    Consumes prior probe results from ``ctx.prior`` (``holes``, ``unfold``,
    ``profile``, ``pmi``) plus the optional ``classification`` slot. When
    classification is missing falls back to ``label="uncertain"``."""

    name = "cam"

    def run(self, inp: ProbeContext) -> MachiningStrategy:
        prior = inp.prior or {}
        holes = prior.get("holes")
        if not isinstance(holes, HolePattern):
            holes = HolePattern()
        _unfold = prior.get("unfold")
        unfold = _unfold if isinstance(_unfold, UnfoldResult) else None
        _profile = prior.get("profile")
        profile = _profile if isinstance(_profile, ProfileMatch) else None
        pmi = prior.get("pmi")
        if pmi is not None and not isinstance(pmi, PMIRecord):
            pmi = None
        classification = prior.get("classification")
        if not isinstance(classification, ClassificationResult):
            classification = ClassificationResult(
                label="uncertain",
                confidence=0.0,
                trace=DecisionTrace(),
            )
        features = inp.features if inp.features is not None else ManufacturingFeatures()
        return cam_recommend(
            features=features,
            pmi=pmi,
            classification=classification,
            holes=holes,
            unfold=unfold,
            profile_match=profile,
        )
