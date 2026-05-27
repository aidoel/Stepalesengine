"""Tests for the PMI extraction probe and its XML round-trip."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from manufacturing_pipeline.classification.types import (
    ClassificationResult,
    DecisionTrace,
)
from manufacturing_pipeline.io.xml_writer import read_xml, write_xml
from manufacturing_pipeline.manifest import AssemblyManifest, PartManifestEntry
from manufacturing_pipeline.parsing.types import StepPart
from manufacturing_pipeline.pipeline.analyze_assembly import (
    AnalyzeOptions,
    analyze,
)
from manufacturing_pipeline.pmi import extract_pmi
from manufacturing_pipeline.pmi.extractor import (
    _emit_datum,
    _emit_surface_finish,
    _resolve_dimension_nominal,
    _scan_nominal_in_refs,
)
from manufacturing_pipeline.pmi.types import (
    Datum,
    DimensionalTolerance,
    GeometricTolerance,
    PMIRecord,
    SurfaceFinish,
)

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

NIST_DIR = Path("/Users/ds/Downloads/NIST-PMI-STEP-Files")
NIST_AP242_E1 = NIST_DIR / "nist_ctc_01_asme1_ap242-e1.stp"
NIST_AP242_E1_TG = NIST_DIR / "nist_ftc_08_asme1_ap242-e1-tg.stp"
NIST_AP203_GEOM = NIST_DIR / "AP203 geometry only" / "nist_ctc_01_asme1_rd.stp"


def _need(path: Path) -> Path:
    if not path.is_file():
        pytest.skip(f"missing NIST fixture: {path}")
    return path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _copy_to_tmp(src: Path, tmp_path: Path) -> Path:
    dst = tmp_path / src.name
    shutil.copy2(src, dst)
    return dst


def _minimal_classification() -> ClassificationResult:
    return ClassificationResult(
        label="uncertain",
        confidence=0.0,
        trace=DecisionTrace(model_version="test"),
    )


# ---------------------------------------------------------------------------
# Tests on real AP242 files
# ---------------------------------------------------------------------------


def test_extract_pmi_ap242_e1_has_semantic(tmp_path: Path) -> None:
    src = _need(NIST_AP242_E1)
    step = _copy_to_tmp(src, tmp_path)

    rec = extract_pmi(step)

    assert rec.has_semantic is True
    assert rec.n_annotations > 0
    assert len(rec.tolerances) > 0


def test_extract_pmi_ap242_e1_tolerances_well_formed(tmp_path: Path) -> None:
    src = _need(NIST_AP242_E1)
    step = _copy_to_tmp(src, tmp_path)

    rec = extract_pmi(step)

    # The CTC-01 file carries position + flatness + perpendicularity +
    # surface-profile tolerances. We assert on the types/values that are
    # stable across STEP-tools authoring vintages.
    types = {t.type for t in rec.tolerances}
    assert "position" in types
    assert "flatness" in types

    # Every tolerance should carry a strictly positive magnitude.
    for t in rec.tolerances:
        assert t.magnitude_mm > 0.0
        assert t.unit == "mm"

    # Position tolerances reference datums A/B/C in this file.
    position = next(t for t in rec.tolerances if t.type == "position")
    assert position.datums == ["A", "B", "C"]


def test_extract_pmi_ap242_e1_dimensions_have_nominal(tmp_path: Path) -> None:
    src = _need(NIST_AP242_E1)
    step = _copy_to_tmp(src, tmp_path)

    rec = extract_pmi(step)

    assert len(rec.dimensions) > 0
    # The CTC-01 file has 35.0mm diameter dimensions; the extractor should
    # have resolved their nominal value through the
    # DIMENSIONAL_CHARACTERISTIC_REPRESENTATION chain.
    assert any(abs(d.nominal - 35.0) < 1e-6 for d in rec.dimensions)


def test_extract_pmi_ap242_e1_datums_abc(tmp_path: Path) -> None:
    src = _need(NIST_AP242_E1)
    step = _copy_to_tmp(src, tmp_path)

    rec = extract_pmi(step)

    # The DATUM entities define identifiers A, B, C.
    identifiers = {d.identifier for d in rec.datums}
    assert {"A", "B", "C"}.issubset(identifiers)


def test_extract_pmi_ap203_geom_only_no_semantic(tmp_path: Path) -> None:
    src = _need(NIST_AP203_GEOM)
    step = _copy_to_tmp(src, tmp_path)

    rec = extract_pmi(step)

    assert rec.has_semantic is False
    assert rec.n_annotations == 0
    assert rec.tolerances == []
    assert rec.dimensions == []
    assert rec.datums == []


def test_extract_pmi_ap242_tg_graphical_only(tmp_path: Path) -> None:
    src = _need(NIST_AP242_E1_TG)
    step = _copy_to_tmp(src, tmp_path)

    rec = extract_pmi(step)

    # ``-tg`` files carry tessellated/graphical annotations only; no
    # semantic GD&T entities.
    assert rec.has_semantic is False
    assert rec.tolerances == []
    assert rec.dimensions == []


def test_extract_pmi_missing_file_returns_empty(tmp_path: Path) -> None:
    rec = extract_pmi(tmp_path / "does_not_exist.stp")

    assert isinstance(rec, PMIRecord)
    assert rec.has_semantic is False
    assert rec.n_annotations == 0
    assert rec.tolerances == []


def test_extract_pmi_malformed_file_does_not_raise(tmp_path: Path) -> None:
    junk = tmp_path / "junk.stp"
    junk.write_text("this is not a step file at all\n#1=GARBAGE();\n")

    rec = extract_pmi(junk)

    assert isinstance(rec, PMIRecord)
    assert rec.has_semantic is False


# ---------------------------------------------------------------------------
# XML round-trip
# ---------------------------------------------------------------------------


def test_pmi_xml_round_trip(tmp_path: Path) -> None:
    pmi = PMIRecord(
        tolerances=[
            GeometricTolerance(
                type="position",
                magnitude_mm=0.1,
                unit="mm",
                datums=["A", "B", "C"],
                applied_to="#235",
                modifier="MMC",
            ),
            GeometricTolerance(
                type="flatness",
                magnitude_mm=0.05,
                unit="mm",
                datums=[],
                applied_to="#297",
            ),
        ],
        dimensions=[
            DimensionalTolerance(
                nominal=10.0,
                upper=0.05,
                lower=-0.05,
                unit="mm",
                applied_to="#120",
            ),
        ],
        datums=[Datum(identifier="A"), Datum(identifier="B")],
        finishes=[SurfaceFinish(value=1.6, unit="um", applied_to="#1500")],
        n_annotations=42,
        has_semantic=True,
    )

    part = StepPart(
        product_id="P1",
        name="Bracket",
        description="test bracket",
        source="test",
    )
    entry = PartManifestEntry(
        part=part,
        classification=_minimal_classification(),
        pmi=pmi,
    )
    manifest = AssemblyManifest(
        source_path="x.stp",
        source_mtime=0.0,
        source_size=0,
        model_version="test",
        generated_at="2026-01-01T00:00:00+00:00",
        parts=[entry],
    )

    out = tmp_path / "manifest.xml"
    write_xml(manifest, out)
    loaded = read_xml(out)

    rt = loaded.parts[0].pmi
    assert rt is not None
    assert rt.has_semantic is True
    assert rt.n_annotations == 42
    assert len(rt.tolerances) == 2
    assert rt.tolerances[0].type == "position"
    assert rt.tolerances[0].magnitude_mm == 0.1
    assert rt.tolerances[0].datums == ["A", "B", "C"]
    assert rt.tolerances[0].modifier == "MMC"
    assert rt.tolerances[1].type == "flatness"
    assert rt.tolerances[1].datums == []
    assert len(rt.dimensions) == 1
    assert rt.dimensions[0].nominal == 10.0
    assert rt.dimensions[0].upper == 0.05
    assert rt.dimensions[0].lower == -0.05
    assert [d.identifier for d in rt.datums] == ["A", "B"]
    assert rt.finishes[0].value == 1.6
    assert rt.finishes[0].unit == "um"


def test_pmi_xml_omitted_when_none(tmp_path: Path) -> None:
    """A manifest entry with ``pmi=None`` round-trips with ``pmi is None``."""
    part = StepPart(product_id="P", name="N", description="", source="t")
    entry = PartManifestEntry(part=part, classification=_minimal_classification())
    manifest = AssemblyManifest(
        source_path="x.stp",
        source_mtime=0.0,
        source_size=0,
        model_version="test",
        generated_at="2026-01-01T00:00:00+00:00",
        parts=[entry],
    )

    out = tmp_path / "manifest.xml"
    write_xml(manifest, out)
    loaded = read_xml(out)

    assert loaded.parts[0].pmi is None


# ---------------------------------------------------------------------------
# End-to-end orchestrator
# ---------------------------------------------------------------------------


def test_analyze_attaches_pmi_to_entries(tmp_path: Path) -> None:
    src = _need(NIST_AP242_E1)
    step = _copy_to_tmp(src, tmp_path)

    out_dir = tmp_path / "out"
    opts = AnalyzeOptions(
        out_dir=out_dir,
        write_dxf=False,
        write_xml=True,
        use_cache=False,
    )
    result = analyze(step, opts)

    # The NIST file should produce at least one part entry with PMI attached.
    assert result.manifest.parts, "analyze() returned no parts"
    pmis = [p.pmi for p in result.manifest.parts if p.pmi is not None]
    assert pmis, "no part received a PMIRecord"
    pmi0 = pmis[0]
    assert pmi0.has_semantic is True
    assert pmi0.n_annotations > 0
    assert len(pmi0.tolerances) > 0


def test_analyze_pmi_survives_xml_round_trip(tmp_path: Path) -> None:
    src = _need(NIST_AP242_E1)
    step = _copy_to_tmp(src, tmp_path)

    out_dir = tmp_path / "out"
    opts = AnalyzeOptions(
        out_dir=out_dir,
        write_dxf=False,
        write_xml=True,
        use_cache=False,
    )
    result = analyze(step, opts)
    assert result.manifest_path is not None

    reloaded = read_xml(result.manifest_path)
    pmi_entries = [p.pmi for p in reloaded.parts if p.pmi is not None]
    assert pmi_entries, "manifest XML lost PMI on round-trip"
    assert pmi_entries[0].has_semantic is True
    assert len(pmi_entries[0].tolerances) > 0


# ---------------------------------------------------------------------------
# Direct helper tests covering silent-skip branches
# ---------------------------------------------------------------------------


def test_emit_surface_finish_walks_referenced_entity_for_ra() -> None:
    """Body has no direct LENGTH_MEASURE -> follow ref to nested entity."""
    entities: dict[int, tuple[str, str]] = {
        # Nested wrapper carrying the actual Ra LENGTH_MEASURE.
        42: ("MEASURE_WITH_UNIT", "LENGTH_MEASURE(1.6),#99"),
    }
    body = "'roughness',#42"  # ref to #42, no LENGTH_MEASURE in body itself
    rec = PMIRecord()

    _emit_surface_finish(body, entities, rec)

    assert len(rec.finishes) == 1
    finish = rec.finishes[0]
    assert isinstance(finish, SurfaceFinish)
    assert finish.value == 1.6
    assert finish.unit == "um"


def test_emit_surface_finish_no_value_anywhere_skips() -> None:
    """Body and refs both lack LENGTH_MEASURE -> no finish appended."""
    entities: dict[int, tuple[str, str]] = {
        7: ("REPRESENTATION_CONTEXT", "'ctx','3D'"),
    }
    body = "'finish',#7"
    rec = PMIRecord()

    _emit_surface_finish(body, entities, rec)

    assert rec.finishes == []


def test_emit_surface_finish_ref_to_missing_entity_skips() -> None:
    """Ref to an unknown ID is silently dropped."""
    body = "'finish',#999"
    rec = PMIRecord()

    _emit_surface_finish(body, {}, rec)

    assert rec.finishes == []


def test_emit_datum_basic_reference_appends_identifier() -> None:
    """A DATUM body ending in a quoted identifier -> one Datum entry."""
    # DATUM args: (name, description, of_shape, products_definitional, identification)
    body = "'datum-A','description',#10,.TRUE.,'A'"
    rec = PMIRecord()

    _emit_datum(body, rec, is_feature=False)

    assert len(rec.datums) == 1
    assert rec.datums[0] == Datum(identifier="A", applied_to="")


def test_emit_datum_feature_uses_first_arg() -> None:
    """DATUM_FEATURE bodies carry the identifier in the first arg."""
    body = "'Simple Datum.1','desc',#5,.TRUE."
    rec = PMIRecord()

    _emit_datum(body, rec, is_feature=True)

    assert len(rec.datums) == 1
    assert rec.datums[0].identifier == "Simple Datum.1"


def test_emit_datum_empty_body_skips() -> None:
    """An empty body has no args -> no Datum appended (no exception)."""
    rec = PMIRecord()

    _emit_datum("", rec, is_feature=False)

    assert rec.datums == []


def test_emit_datum_unquoted_last_arg_skips() -> None:
    """If the identifier slot has no quoted value, _emit_datum bails."""
    body = "$,$,$,$,$"
    rec = PMIRecord()

    _emit_datum(body, rec, is_feature=False)

    assert rec.datums == []


def test_scan_nominal_in_refs_falls_back_to_first_length_measure() -> None:
    """No 'nominal value' tag anywhere -> first LENGTH_MEASURE wins."""
    entities: dict[int, tuple[str, str]] = {
        # Neither entity has the 'nominal value' description tag.
        21: ("REPRESENTATION_ITEM", "'item','no length here'"),
        22: ("MEASURE_REPRESENTATION_ITEM", "'thing',LENGTH_MEASURE(12.5),#30"),
    }
    body = "#21,#22"

    val = _scan_nominal_in_refs(body, entities)

    assert val == 12.5


def test_scan_nominal_in_refs_prefers_nominal_value_tag() -> None:
    """When one ref is tagged 'nominal value', it overrides the fallback."""
    entities: dict[int, tuple[str, str]] = {
        11: ("MEASURE_REPRESENTATION_ITEM", "'first',LENGTH_MEASURE(7.0),#0"),
        12: ("MEASURE_REPRESENTATION_ITEM", "'nominal value',LENGTH_MEASURE(35.0),#0"),
    }
    body = "#11,#12"

    val = _scan_nominal_in_refs(body, entities)

    assert val == 35.0


def test_scan_nominal_in_refs_no_refs_returns_none() -> None:
    """Body with no #NNN tokens -> no fallback to find."""
    assert _scan_nominal_in_refs("just,plain,args", {}) is None


def test_resolve_dimension_nominal_fallback_when_no_char_rep_index() -> None:
    """Hits L375-394: walk DIMENSIONAL_SIZE children directly for LENGTH_MEASURE.

    No DIMENSIONAL_CHARACTERISTIC_REPRESENTATION exists, so the resolver
    must scan the dim entity's direct refs.
    """
    entities: dict[int, tuple[str, str]] = {
        50: ("DIMENSIONAL_SIZE", "#51,'diameter'"),
        51: ("MEASURE_REPRESENTATION_ITEM", "'nominal value',LENGTH_MEASURE(42.0),#0"),
    }

    val = _resolve_dimension_nominal(50, entities, char_rep_index=None)

    assert val == 42.0


def test_resolve_dimension_nominal_fallback_uses_first_length_when_no_tag() -> None:
    """No 'nominal value' tag in any child -> first LENGTH_MEASURE encountered."""
    entities: dict[int, tuple[str, str]] = {
        60: ("DIMENSIONAL_LOCATION", "#61"),
        61: ("MEASURE_REPRESENTATION_ITEM", "'something',LENGTH_MEASURE(8.25),#0"),
    }

    val = _resolve_dimension_nominal(60, entities, char_rep_index={})

    assert val == 8.25


def test_resolve_dimension_nominal_missing_ref_returns_zero() -> None:
    """A ref not in the entity map -> 0.0 (no exception)."""
    assert _resolve_dimension_nominal(999, {}, char_rep_index=None) == 0.0
    assert _resolve_dimension_nominal(None, {}, char_rep_index=None) == 0.0


def test_resolve_dimension_nominal_char_rep_index_path_with_nominal() -> None:
    """char_rep_index hit -> read nominal from the shape-dim-rep body."""
    entities: dict[int, tuple[str, str]] = {
        70: ("DIMENSIONAL_SIZE", "$"),  # body is irrelevant; we go via index
        71: ("SHAPE_DIMENSION_REPRESENTATION", "'name',(#72),#999"),
        72: ("MEASURE_REPRESENTATION_ITEM", "'nominal value',LENGTH_MEASURE(99.5),#0"),
    }

    val = _resolve_dimension_nominal(70, entities, char_rep_index={70: 71})

    assert val == 99.5


# ---------------------------------------------------------------------------
# End-to-end extract_pmi with hand-crafted STEP text
# ---------------------------------------------------------------------------


def _write_step(tmp_path: Path, body: str) -> Path:
    """Wrap a DATA body in a minimal STEP file envelope."""
    step = tmp_path / "synthetic.stp"
    step.write_text(
        "ISO-10303-21;\n"
        "HEADER;\n"
        "FILE_DESCRIPTION(('PMI test'),'2;1');\n"
        "FILE_NAME('synthetic.stp','2026-01-01',('test'),('test'),'','','');\n"
        "FILE_SCHEMA(('AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF'));\n"
        "ENDSEC;\n"
        "DATA;\n"
        f"{body}\n"
        "ENDSEC;\n"
        "END-ISO-10303-21;\n"
    )
    return step


def test_extract_pmi_synthetic_surface_finish_with_nested_ref(tmp_path: Path) -> None:
    """End-to-end: SURFACE_TEXTURE_REPRESENTATION whose Ra is in a nested ref."""
    body = (
        "#1=MEASURE_REPRESENTATION_ITEM('Ra',LENGTH_MEASURE(3.2),#2);\n"
        "#2=DIMENSIONAL_EXPONENTS(0.,0.,0.,0.,0.,0.,0.);\n"
        "#10=SURFACE_TEXTURE_REPRESENTATION('finish',(#1),#99);\n"
    )
    step = _write_step(tmp_path, body)

    rec = extract_pmi(step)

    assert any(abs(f.value - 3.2) < 1e-9 for f in rec.finishes)
    assert rec.has_semantic is True


def test_extract_pmi_synthetic_datum_emits_identifier(tmp_path: Path) -> None:
    """End-to-end: a DATUM entity yields one Datum in the record."""
    body = (
        "#1=PRODUCT_DEFINITION('p','desc',#2,#3);\n"
        "#20=DATUM('datum-A','description',#1,.TRUE.,'A');\n"
    )
    step = _write_step(tmp_path, body)

    rec = extract_pmi(step)

    assert [d.identifier for d in rec.datums] == ["A"]
    assert rec.has_semantic is True


def test_extract_pmi_synthetic_malformed_body_returns_empty(tmp_path: Path) -> None:
    """A DATA section with only meaningless tokens -> empty PMIRecord, no raise."""
    body = (
        "#1=CARTESIAN_POINT('p',(0.,0.,0.));\n"
        "#2=DIRECTION('d',(1.,0.,0.));\n"
        "#3=AXIS2_PLACEMENT_3D('a',#1,#2,$);\n"
    )
    step = _write_step(tmp_path, body)

    rec = extract_pmi(step)

    assert rec.tolerances == []
    assert rec.dimensions == []
    assert rec.datums == []
    assert rec.finishes == []
    assert rec.has_semantic is False
