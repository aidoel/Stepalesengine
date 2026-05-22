"""Regression test for the UnfoldProbe against real-world sheet-metal parts.

Two production STEP files are committed under ``tests/fixtures/step/`` with a
``sheet_`` prefix. They are the parts that exposed the corner-fillet bug: the
probe used to fail every one of them as ``cyclic_graph`` because rounded
corners were mistaken for bends.

Baselines are pinned to the geometry reported by the AutoPOL ``Sheet_*``
reference XML for the same parts (thickness, bend count) and were verified to
match the UnfoldProbe on 2026-05-21. The probe measures the *folded* 3D solid,
so its bend count and material thickness are asserted - not the flat-pattern
extent, which depends on the K-factor.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from manufacturing_pipeline.geometry.geometry_loader import load_solids
from manufacturing_pipeline.geometry.types import UnfoldStatus
from manufacturing_pipeline.geometry.unfold_probe import UnfoldProbe
from manufacturing_pipeline.pipeline.analyze_assembly import AnalyzeOptions, analyze

FIXTURES = Path(__file__).parent.parent / "fixtures" / "step"

# filename -> sorted list of (status name, n_bends, rounded thickness) per solid.
# sheet_md_17_04194_2: three identical 1.5 mm flat brackets + one 1 mm part
# with two bends. sheet_10001073530: one 5 mm part with two bends.
BASELINES: dict[str, list[tuple[str, int, float]]] = {
    "sheet_md_17_04194_2.stp": [
        ("SUCCESS", 0, 1.5),
        ("SUCCESS", 0, 1.5),
        ("SUCCESS", 0, 1.5),
        ("SUCCESS", 2, 1.0),
    ],
    "sheet_10001073530_rev00.stp": [
        ("SUCCESS", 2, 5.0),
    ],
}


@pytest.mark.parametrize(
    "filename, expected",
    list(BASELINES.items()),
    ids=list(BASELINES.keys()),
)
def test_unfold_baseline(filename: str, expected: list[tuple[str, int, float]]) -> None:
    path = FIXTURES / filename
    if not path.exists():
        pytest.skip(f"fixture missing: {filename}")

    solids = load_solids(str(path))
    assert len(solids) == len(
        expected
    ), f"solid count mismatch for {filename}: got {len(solids)}, expected {len(expected)}"

    observed: list[tuple[str, int, float]] = []
    for solid in solids:
        res = UnfoldProbe().run(solid)
        observed.append((res.status.name, res.n_bends, round(res.thickness_mean, 1)))

    # No real sheet-metal part may fail to unfold (the corner-fillet bug).
    assert all(
        s != UnfoldStatus.FAILURE.name for s, _, _ in observed
    ), f"a sheet-metal part failed to unfold in {filename}: {observed}"
    assert Counter(observed) == Counter(expected), (
        f"unfold baseline mismatch for {filename}: got {sorted(observed)}, "
        f"expected {sorted(expected)}"
    )


@pytest.mark.parametrize("filename", list(BASELINES.keys()))
def test_classifies_as_plaat(filename: str, tmp_path: Path) -> None:
    """Every part in these files is sheet metal; with the UnfoldProbe and the
    ``has_bends`` scorer fixed, the classifier must label them all ``plaat``."""

    path = FIXTURES / filename
    if not path.exists():
        pytest.skip(f"fixture missing: {filename}")

    result = analyze(
        path,
        AnalyzeOptions(out_dir=tmp_path, write_dxf=False, write_xml=False, use_cache=False),
    )
    labels = [p.classification.label for p in result.manifest.parts]
    assert all(
        lab == "plaat" for lab in labels
    ), f"sheet-metal parts in {filename} should all classify as 'plaat', got {labels}"
