"""Tests for the PMI extraction probe and its XML round-trip."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from manufacturing_pipeline.classification.types import (
    ClassificationResult,
    DecisionTrace,
)
from manufacturing_pipeline.io.xml_writer import (
    AssemblyManifest,
    PartManifestEntry,
    read_xml,
    write_xml,
)
from manufacturing_pipeline.parsing.types import StepPart
from manufacturing_pipeline.pipeline.analyze_assembly import (
    AnalyzeOptions,
    analyze,
)
from manufacturing_pipeline.pmi import extract_pmi
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
