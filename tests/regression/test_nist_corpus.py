"""Regression test for the NIST PMI STEP corpus.

Replaces the earlier ``test_31686_080.py`` placeholder, which referenced a
fixture that was never committed. This test locks in today's classification
behaviour for six representative NIST PMI STEP files (committed under
``tests/fixtures/step/``) so that future tuning of feature detection or
classification thresholds cannot quietly regress the labels without anyone
noticing.

Baselines are pinned to the behaviour observed on 2026-05-16 against
``manufacturing_pipeline.pipeline.analyze_assembly.analyze``. When the
classifier is intentionally changed, update the ``BASELINES`` map in the
same commit that changes the rules so the diff is explicit.

PMI baselines
-------------
Each entry that carries semantic PMI also asserts lower bounds on the
number of annotations, geometric tolerances, dimensional tolerances and
datums extracted by :func:`manufacturing_pipeline.pmi.extractor.extract_pmi`.
Bounds are set to roughly 90% of the live count observed on 2026-05-16 so
that the test does not flap on minor extractor improvements that surface
*more* PMI, but does fire if the extractor starts losing entities. The
tessellated NIST FTC 08 and the AP203 fixture carry no semantic PMI and
are pinned to ``has_semantic=False``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from manufacturing_pipeline.pipeline.analyze_assembly import (
    AnalyzeOptions,
    analyze,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "step"

# Pinned to today's (2026-05-16) live analyze() output. Each entry asserts the
# total number of parts, the label distribution, and (where applicable) the
# PMI floor. Confidence scores are intentionally NOT asserted so small
# numerical drifts in feature extraction don't flap the test; what matters is
# that the final categorical label is stable and that the PMI extractor keeps
# finding at least the entities it finds today.
#
# 2026-05-16: every machined NIST case now resolves to ``anders``. The previous
# ``uncertain`` baseline was an artefact of the plaat scorer rewarding
# ``top1_face_pct`` unconditionally; the cross-term added in ADR 0008 gates
# that reward on ``unfoldable``, so machined parts with a flattish top face
# land on ``anders`` instead of falling below the confidence threshold.
BASELINES: dict[str, dict] = {
    "nist_ctc_01_asme1_ap242-e1.stp": {
        "parts": 1,
        "labels": {"anders": 1},
        "pmi": {
            "has_semantic": True,
            "min_annotations": 57,
            "min_tolerances": 5,
            "min_dimensions": 4,
            "min_datums": 5,
        },
    },
    "nist_ftc_06_asme1_ap242-e2.stp": {
        "parts": 1,
        "labels": {"anders": 1},
        "pmi": {
            "has_semantic": True,
            "min_annotations": 177,
            "min_tolerances": 25,
            "min_dimensions": 17,
            "min_datums": 14,
        },
    },
    "nist_ftc_08_asme1_ap242-e1-tg.stp": {
        "parts": 1,
        "labels": {"anders": 1},
        # Tessellated AP242: graphical PMI only, no semantic GD&T.
        "pmi": {
            "has_semantic": False,
            "min_annotations": 0,
        },
    },
    "nist_ftc_11_asme1_ap242-e2.stp": {
        "parts": 1,
        "labels": {"anders": 1},
        "pmi": {
            "has_semantic": True,
            "min_annotations": 36,
            "min_tolerances": 3,
            "min_dimensions": 3,
            "min_datums": 3,
        },
    },
    "nist_stc_06_asme1_ap242-e3.stp": {
        "parts": 1,
        "labels": {"anders": 1},
        "pmi": {
            "has_semantic": True,
            "min_annotations": 193,
            "min_tolerances": 22,
            "min_dimensions": 14,
            "min_datums": 9,
        },
    },
    "nist_ftc_11_asme1_rb_ap203.stp": {
        "parts": 1,
        "labels": {"anders": 1},
        # AP203 has no semantic PMI carriers.
        "pmi": {
            "has_semantic": False,
            "min_annotations": 0,
        },
    },
}


@pytest.mark.parametrize(
    "filename, expected",
    list(BASELINES.items()),
    ids=list(BASELINES.keys()),
)
def test_nist_baseline(filename: str, expected: dict, tmp_path: Path) -> None:
    path = FIXTURES / filename
    if not path.exists():
        pytest.skip(f"fixture missing: {filename}")

    result = analyze(
        path,
        AnalyzeOptions(
            out_dir=tmp_path,
            write_dxf=False,
            write_xml=False,
            use_cache=False,
        ),
    )

    assert len(result.manifest.parts) == expected["parts"], (
        f"part count mismatch for {filename}: "
        f"got {len(result.manifest.parts)}, expected {expected['parts']}"
    )

    labels: dict[str, int] = {}
    for part in result.manifest.parts:
        labels[part.classification.label] = labels.get(part.classification.label, 0) + 1
    assert (
        labels == expected["labels"]
    ), f"label distribution mismatch for {filename}: got {labels}, expected {expected['labels']}"

    if "pmi" in expected:
        pmi = result.manifest.parts[0].pmi
        assert pmi is not None, f"PMIRecord should always be attached for {filename}"
        assert pmi.has_semantic == expected["pmi"]["has_semantic"], (
            f"PMI has_semantic mismatch for {filename}: "
            f"got {pmi.has_semantic}, expected {expected['pmi']['has_semantic']}"
        )
        assert pmi.n_annotations >= expected["pmi"]["min_annotations"], (
            f"PMI n_annotations regressed for {filename}: "
            f"got {pmi.n_annotations}, expected >= "
            f"{expected['pmi']['min_annotations']}"
        )
        if "min_tolerances" in expected["pmi"]:
            assert len(pmi.tolerances) >= expected["pmi"]["min_tolerances"], (
                f"PMI tolerances regressed for {filename}: "
                f"got {len(pmi.tolerances)}, expected >= "
                f"{expected['pmi']['min_tolerances']}"
            )
        if "min_dimensions" in expected["pmi"]:
            assert len(pmi.dimensions) >= expected["pmi"]["min_dimensions"], (
                f"PMI dimensions regressed for {filename}: "
                f"got {len(pmi.dimensions)}, expected >= "
                f"{expected['pmi']['min_dimensions']}"
            )
        if "min_datums" in expected["pmi"]:
            assert len(pmi.datums) >= expected["pmi"]["min_datums"], (
                f"PMI datums regressed for {filename}: "
                f"got {len(pmi.datums)}, expected >= "
                f"{expected['pmi']['min_datums']}"
            )
