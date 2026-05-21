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
from . import ProbeContext, probe_result


class CamProbe:
    """Adapter so :func:`cam_recommend` slots into the registry.

    Consumes prior probe results from ``ctx.prior`` (``holes``, ``unfold``,
    ``profile``, ``pmi``) plus the optional ``classification`` slot. When
    classification is missing falls back to ``label="uncertain"``."""

    name = "cam"

    def run(self, inp: ProbeContext) -> MachiningStrategy:
        prior = inp.prior or {}
        holes = probe_result(prior, "holes", HolePattern, HolePattern())
        unfold = probe_result(prior, "unfold", UnfoldResult)
        profile = probe_result(prior, "profile", ProfileMatch)
        pmi = probe_result(prior, "pmi", PMIRecord)
        classification = probe_result(
            prior,
            "classification",
            ClassificationResult,
            ClassificationResult(label="uncertain", confidence=0.0, trace=DecisionTrace()),
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
