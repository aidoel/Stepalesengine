"""Tests for the CAM strategy probe.

Pure-Python checks: no OCP / cadquery dependency. Each test builds
classification + features + holes from scratch and verifies the routing
plus the PMI-driven refinements.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from manufacturing_pipeline.cam.strategist import recommend
from manufacturing_pipeline.cam.types import MachiningStrategy, Operation
from manufacturing_pipeline.classification.types import (
    ClassificationResult,
    DecisionTrace,
)
from manufacturing_pipeline.geometry.types import (
    HolePattern,
    ManufacturingFeatures,
    UnfoldResult,
    UnfoldStatus,
)
from manufacturing_pipeline.pmi.types import (
    Datum,
    GeometricTolerance,
    PMIRecord,
    SurfaceFinish,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _classification(label: str, conf: float = 0.9) -> ClassificationResult:
    return ClassificationResult(
        label=label,
        confidence=conf,
        trace=DecisionTrace(model_version="rules-test"),
    )


def _features(
    *,
    bbox: tuple[float, float, float] = (200.0, 100.0, 2.0),
    convex_hull_ratio: float = 0.95,
) -> ManufacturingFeatures:
    return ManufacturingFeatures(
        volume=200.0 * 100.0 * 2.0,
        surface_area=2.0 * (200 * 100 + 200 * 2 + 100 * 2),
        bbox_dims_sorted=bbox,
        aspect_ratio=2.0,
        thickness_ratio=bbox[2] / max(bbox[0], 1e-6),
        face_area_top=[0.78, 0.15, 0.05],
        hole_count=0,
        sa_v_ratio=0.37,
        bounding_cylinder_fit_pct=0.42,
        convex_hull_volume_ratio=convex_hull_ratio,
        cross_section_constant=False,
        source="brep",
    )


def _hole_dicts(diameters: list[float], *, depth: float = 5.0, through: bool = False) -> list[dict]:
    return [
        {
            "diameter": d,
            "depth": depth,
            "axis_dir": (0.0, 0.0, 1.0),
            "position": (float(i), 0.0, 0.0),
            "through": through,
            "n_cylinders": 1,
        }
        for i, d in enumerate(diameters)
    ]


def _ops_by_kind(strat: MachiningStrategy) -> dict[str, list[Operation]]:
    grouped: dict[str, list[Operation]] = {}
    for op in strat.operations:
        grouped.setdefault(op.op, []).append(op)
    return grouped


# ---------------------------------------------------------------------------
# Routing tests
# ---------------------------------------------------------------------------


def test_plaat_with_bends_and_holes_emits_laser_bends_and_deburr():
    features = _features()
    holes = HolePattern(
        hole_count=4,
        diameters=[8.0, 8.0, 10.0, 10.0],
        holes=_hole_dicts([8.0, 8.0, 10.0, 10.0]),
    )
    unfold = UnfoldResult(
        status=UnfoldStatus.SUCCESS,
        n_bends=2,
        flat_area=20000.0,
        blank_bbox=(220.0, 120.0),
        thickness_mean=2.0,
    )
    strat = recommend(
        features=features,
        pmi=None,
        classification=_classification("plaat"),
        holes=holes,
        unfold=unfold,
    )
    assert strat.primary_process == "sheet_metal"
    groups = _ops_by_kind(strat)
    assert len(groups.get("laser_cut", [])) == 1
    assert len(groups.get("press_brake_bend", [])) == 2
    assert len(groups.get("deburr", [])) == 1
    # Priority ordering: laser_cut < bend < deburr.
    by_pri = sorted(strat.operations, key=lambda o: o.priority)
    assert by_pri[0].op == "laser_cut"
    assert by_pri[-1].op == "deburr"
    # Estimated time must be non-trivial (positive).
    assert strat.estimated_time_min > 0.0


def test_profiel_yields_purchased_inspect_only():
    strat = recommend(
        features=_features(),
        pmi=None,
        classification=_classification("profiel"),
        holes=HolePattern(),
    )
    assert strat.primary_process == "purchased"
    assert [op.op for op in strat.operations] == ["inspect"]
    assert strat.setup_count == 0


def test_anders_with_ten_holes_pocketed_emits_mill_drills_pocket_inspect():
    holes = HolePattern(
        hole_count=10,
        diameters=[6.0] * 10,
        holes=_hole_dicts([6.0] * 10),
    )
    features = _features(convex_hull_ratio=0.50)  # hull_concavity = 0.50 > 0.40
    strat = recommend(
        features=features,
        pmi=None,
        classification=_classification("anders"),
        holes=holes,
    )
    assert strat.primary_process == "machining"
    groups = _ops_by_kind(strat)
    assert len(groups.get("mill_contour", [])) == 1
    assert len(groups.get("drill", [])) == 10
    assert len(groups.get("mill_pocket", [])) == 1
    # Final inspect when no datums are declared.
    assert len(groups.get("inspect", [])) >= 1


def test_tight_position_tolerance_adds_ream_and_inspect():
    holes = HolePattern(hole_count=1, diameters=[10.0], holes=_hole_dicts([10.0]))
    pmi = PMIRecord(
        tolerances=[
            GeometricTolerance(
                type="position",
                magnitude_mm=0.02,
                datums=["A", "B"],
                applied_to="hole_0",
            ),
        ],
        n_annotations=1,
        has_semantic=True,
    )
    strat = recommend(
        features=_features(),
        pmi=pmi,
        classification=_classification("anders"),
        holes=holes,
    )
    groups = _ops_by_kind(strat)
    assert len(groups.get("ream", [])) == 1
    assert groups["ream"][0].feature_ref == "hole_0"
    # An inspect tied to that hole exists.
    inspects = groups.get("inspect", [])
    assert any(op.feature_ref == "hole_0" for op in inspects)


def test_fine_surface_finish_adds_finish_pass_at_priority_200():
    pmi = PMIRecord(
        finishes=[SurfaceFinish(value=0.4, unit="um", applied_to="face_top")],
        n_annotations=1,
        has_semantic=True,
    )
    strat = recommend(
        features=_features(),
        pmi=pmi,
        classification=_classification("anders"),
        holes=HolePattern(),
    )
    groups = _ops_by_kind(strat)
    assert len(groups.get("finish_pass", [])) == 1
    assert groups["finish_pass"][0].priority == 200


def test_uncertain_classification_yields_inspect_only_unknown():
    strat = recommend(
        features=_features(),
        pmi=None,
        classification=_classification("uncertain", conf=0.1),
        holes=HolePattern(),
    )
    assert strat.primary_process == "unknown"
    assert [op.op for op in strat.operations] == ["inspect"]


def test_datums_appear_in_notes_for_machining_route():
    pmi = PMIRecord(
        datums=[Datum(identifier="A"), Datum(identifier="B"), Datum(identifier="C")],
        n_annotations=3,
        has_semantic=True,
    )
    strat = recommend(
        features=_features(),
        pmi=pmi,
        classification=_classification("anders"),
        holes=HolePattern(),
    )
    assert "A" in strat.notes and "B" in strat.notes and "C" in strat.notes
    # Each datum produces one inspect op.
    inspects = [op for op in strat.operations if op.op == "inspect"]
    refs = {op.feature_ref for op in inspects}
    assert {"datum_A", "datum_B", "datum_C"}.issubset(refs)


def test_xml_round_trip_preserves_strategy_fields(tmp_path: Path):
    from manufacturing_pipeline.classification.types import (
        ClassificationResult as CR,
    )
    from manufacturing_pipeline.classification.types import (
        DecisionTrace as DT,
    )
    from manufacturing_pipeline.io.xml_writer import read_xml, write_xml
    from manufacturing_pipeline.manifest import (
        AssemblyManifest,
        PartManifestEntry,
    )
    from manufacturing_pipeline.parsing.types import StepPart

    strat = MachiningStrategy(
        primary_process="machining",
        operations=[
            Operation(
                op="mill_contour",
                feature_ref="outer_contour",
                params={"length_mm": 100.0, "width_mm": 50.0},
                priority=100,
                reason="rough outer contour",
            ),
            Operation(
                op="drill",
                feature_ref="hole_0",
                params={"diameter_mm": 8.0, "through": True},
                priority=110,
                reason="cylindrical hole",
            ),
        ],
        setup_count=2,
        material_hint="6061-T6",
        estimated_time_min=12.4,
        notes="setup against datums A,B,C",
    )
    manifest = AssemblyManifest(
        source_path=str(tmp_path / "x.step"),
        source_mtime=0.0,
        source_size=0,
        model_version="rules-test",
        generated_at="2026-05-16T00:00:00Z",
        parts=[
            PartManifestEntry(
                part=StepPart(
                    product_id="P-1",
                    name="Bracket",
                    description="",
                    children=[],
                    source="nauo",
                ),
                classification=CR(label="anders", confidence=0.9, trace=DT()),
                strategy=strat,
            )
        ],
    )
    out = write_xml(manifest, tmp_path / "manifest.xml")
    parsed = read_xml(out)
    parsed_strat = parsed.parts[0].strategy
    assert parsed_strat is not None
    assert parsed_strat.primary_process == "machining"
    assert parsed_strat.setup_count == 2
    assert parsed_strat.material_hint == "6061-T6"
    assert parsed_strat.estimated_time_min == pytest.approx(12.4)
    assert parsed_strat.notes == "setup against datums A,B,C"
    assert len(parsed_strat.operations) == 2
    op0 = parsed_strat.operations[0]
    assert op0.op == "mill_contour"
    assert op0.feature_ref == "outer_contour"
    assert op0.priority == 100
    assert op0.reason == "rough outer contour"
    # Params come back as strings (XML attributes), still queryable.
    assert "length_mm" in op0.params
    op1 = parsed_strat.operations[1]
    assert op1.op == "drill"
    assert op1.params["diameter_mm"] == "8.0"
    assert op1.params["through"] == "True"


def test_recommend_never_raises_on_garbage_input():
    # Empty / minimal types should still resolve to "unknown".
    bogus = ClassificationResult(label="", confidence=0.0, trace=DecisionTrace())
    strat = recommend(
        features=ManufacturingFeatures(),
        pmi=None,
        classification=bogus,
        holes=HolePattern(),
    )
    assert strat.primary_process == "unknown"
    assert len(strat.operations) == 1


def test_global_position_tolerance_applies_to_every_hole():
    holes = HolePattern(hole_count=3, diameters=[6.0, 6.0, 6.0], holes=_hole_dicts([6.0, 6.0, 6.0]))
    # No applied_to -> the strategist treats this as a part-global rule.
    pmi = PMIRecord(
        tolerances=[
            GeometricTolerance(type="position", magnitude_mm=0.03, applied_to=""),
        ],
        n_annotations=1,
        has_semantic=True,
    )
    strat = recommend(
        features=_features(),
        pmi=pmi,
        classification=_classification("anders"),
        holes=holes,
    )
    groups = _ops_by_kind(strat)
    assert len(groups.get("ream", [])) == 3
    refs = {op.feature_ref for op in groups["ream"]}
    assert refs == {"hole_0", "hole_1", "hole_2"}


def test_end_to_end_through_analyze_attaches_strategy(tmp_path: Path):
    """Synthetic plate -> analyze() -> manifest entry has a strategy."""
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    from manufacturing_pipeline.geometry.geometry_loader import write_step
    from manufacturing_pipeline.pipeline.analyze_assembly import (
        AnalyzeOptions,
        analyze,
    )

    solid = BRepPrimAPI_MakeBox(80.0, 50.0, 2.0).Solid()
    step_path = tmp_path / "plate.step"
    write_step(solid, step_path)

    result = analyze(
        step_path,
        AnalyzeOptions(out_dir=tmp_path / "out", write_xml=True, write_dxf=False, use_cache=False),
    )
    assert result.manifest.parts, "expected at least one manifest entry"
    entry = result.manifest.parts[0]
    assert entry.strategy is not None
    # primary_process must be a known label.
    assert entry.strategy.primary_process in {
        "sheet_metal",
        "machining",
        "purchased",
        "unknown",
    }
    # At least one operation always.
    assert len(entry.strategy.operations) >= 1


def test_setup_count_bumps_when_holes_span_multiple_axes():
    # Two holes on Z-axis, two on Y-axis: needs at least two fixturings.
    holes_payload = [
        {
            "diameter": 6.0,
            "depth": 5.0,
            "axis_dir": (0.0, 0.0, 1.0),
            "position": (0, 0, 0),
            "through": False,
            "n_cylinders": 1,
        },
        {
            "diameter": 6.0,
            "depth": 5.0,
            "axis_dir": (0.0, 0.0, 1.0),
            "position": (1, 0, 0),
            "through": False,
            "n_cylinders": 1,
        },
        {
            "diameter": 5.0,
            "depth": 3.0,
            "axis_dir": (0.0, 1.0, 0.0),
            "position": (0, 0, 0),
            "through": False,
            "n_cylinders": 1,
        },
        {
            "diameter": 5.0,
            "depth": 3.0,
            "axis_dir": (0.0, 1.0, 0.0),
            "position": (0, 0, 1),
            "through": False,
            "n_cylinders": 1,
        },
    ]
    holes = HolePattern(hole_count=4, diameters=[6, 6, 5, 5], holes=holes_payload)
    strat = recommend(
        features=_features(),
        pmi=None,
        classification=_classification("anders"),
        holes=holes,
    )
    assert strat.setup_count == 2
