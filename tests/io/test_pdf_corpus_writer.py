"""Tests for manufacturing_pipeline.io.pdf_corpus_writer.

Strategy: synthesise small AssemblyManifests in memory (no STEP files,
no pipeline), pipe them through ``write_corpus_pdf`` into ``tmp_path``,
and inspect the resulting PDFs by:

    1. checking the magic ``%PDF-`` header + ``%%EOF`` marker,
    2. counting page objects (``/Type /Page`` matches),
    3. byte-grepping for embedded labels we know the writer prints
       (part names, classification labels, PMI fields, CAM process,
       feature row headers).

No new runtime deps. Tests run in well under 5 s.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

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
from manufacturing_pipeline.io.dxf_writer import FlatPattern, write_dxf
from manufacturing_pipeline.io.pdf_corpus_writer import (
    CorpusPDFOptions,
    write_corpus_pdf,
)
from manufacturing_pipeline.io.xml_writer import AssemblyManifest, PartManifestEntry
from manufacturing_pipeline.parsing.types import StepPart
from manufacturing_pipeline.pmi.types import (
    Datum,
    GeometricTolerance,
    PMIRecord,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _rect(x0: float, y0: float, w: float, h: float) -> list[tuple[float, float]]:
    return [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]


def _circle_poly(cx: float, cy: float, r: float, n: int = 24) -> list[tuple[float, float]]:
    return [
        (cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


def _write_sample_dxf(path: Path, part_name: str = "PART-1") -> Path:
    pattern = FlatPattern(
        outer_contour=[_rect(0.0, 0.0, 100.0, 60.0)],
        holes=[_circle_poly(20.0, 30.0, 4.0), _circle_poly(80.0, 30.0, 6.0)],
        bend_lines=[
            ((50.0, 0.0), (50.0, 60.0), {"angle": 90.0, "radius": 1.5, "direction": "up"}),
        ],
        thickness=2.0,
        bbox=(0.0, 0.0, 100.0, 60.0),
        part_name=part_name,
    )
    return write_dxf(pattern, path)


def _features(thickness: float = 2.0, holes: int = 3) -> ManufacturingFeatures:
    return ManufacturingFeatures(
        volume=12000.0,
        surface_area=4800.0,
        bbox_dims_sorted=(100.0, 60.0, thickness),
        aspect_ratio=100.0 / 60.0,
        thickness_ratio=thickness / 100.0,
        face_area_top=[0.78, 0.15, 0.05],
        hole_count=holes,
        hole_diameters=[8.0] * (holes // 2) + [12.0] * (holes - holes // 2),
        convex_hull_volume_ratio=0.92,
        pocket_complexity=0.18,
        is_hollow=False,
        inner_shell_count=0,
        source="brep",
    )


def _classification(label: str = "plaat", confidence: float = 0.91) -> ClassificationResult:
    trace = DecisionTrace(
        scores={"plaat": 1.4, "profiel": 0.3, "anders": 0.2},
        probabilities={"plaat": 0.78, "profiel": 0.14, "anders": 0.08},
        margin=1.1,
    )
    return ClassificationResult(label=label, confidence=confidence, trace=trace)


def _entry(
    *,
    name: str = "BRACKET-1",
    product_id: str = "PID-001",
    label: str = "plaat",
    confidence: float = 0.91,
    dxf_path: str | None = None,
    with_unfold: bool = True,
    with_pmi: bool = False,
    with_strategy: bool = False,
    holes: int = 3,
    thickness: float = 2.0,
) -> PartManifestEntry:
    part = StepPart(product_id=product_id, name=name, description="", source="nauo")
    features = _features(thickness=thickness, holes=holes)
    cls = _classification(label=label, confidence=confidence)
    holes_obj = HolePattern(
        hole_count=holes,
        diameters=[8.0] * (holes // 2) + [12.0] * (holes - holes // 2),
    )
    unfold = (
        UnfoldResult(
            status=UnfoldStatus.SUCCESS,
            n_bends=2,
            flat_area=18234.5,
            blank_bbox=(250.0, 120.0),
            thickness_mean=thickness,
            thickness_cv=0.01,
        )
        if with_unfold
        else None
    )
    pmi = None
    if with_pmi:
        pmi = PMIRecord(
            tolerances=[GeometricTolerance(type="position", magnitude_mm=0.05, datums=["A", "B"])],
            datums=[Datum(identifier="A"), Datum(identifier="B")],
            n_annotations=5,
            has_semantic=True,
        )
    strategy = None
    if with_strategy:
        strategy = MachiningStrategy(
            primary_process="sheet_metal",
            operations=[
                Operation(op="laser_cut", feature_ref="outer_contour", params={}, priority=10),
                Operation(op="press_brake_bend", feature_ref="bend_0", params={}, priority=200),
            ],
            setup_count=1,
            material_hint="S235",
            estimated_time_min=3.2,
            notes="",
        )
    return PartManifestEntry(
        part=part,
        classification=cls,
        features=features,
        unfold=unfold,
        holes=holes_obj,
        quantity=1,
        flat_dxf_path=dxf_path,
        pmi=pmi,
        strategy=strategy,
    )


def _manifest(
    *,
    source: str,
    entries: list[PartManifestEntry],
) -> AssemblyManifest:
    return AssemblyManifest(
        source_path=source,
        source_mtime=1.0,
        source_size=100,
        model_version="rules-1.0.0",
        generated_at="2026-05-15T12:00:00Z",
        parts=entries,
    )


# ---------------------------------------------------------------------------
# PDF inspection helpers
# ---------------------------------------------------------------------------


def _count_pages(pdf_bytes: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page\b(?!s)", pdf_bytes))


def _byte_grep(pdf_bytes: bytes, needle: str) -> bool:
    return needle.encode("utf-8") in pdf_bytes


# ---------------------------------------------------------------------------
# 1. Valid PDF, >5 KB, for a single-part manifest
# ---------------------------------------------------------------------------


def test_single_part_pdf_is_valid_and_above_5kb(tmp_path: Path) -> None:
    manifest = _manifest(
        source=str(tmp_path / "asm.step"),
        entries=[_entry(name="SOLO-1", product_id="PID-SOLO")],
    )
    out = write_corpus_pdf([manifest], tmp_path / "solo.pdf")
    data = out.read_bytes()
    assert out.exists()
    assert data.startswith(b"%PDF-"), "missing PDF magic"
    assert b"%%EOF" in data
    assert out.stat().st_size > 5 * 1024, f"PDF unexpectedly small: {out.stat().st_size} bytes"
    # Cover page wording is always present.
    assert _byte_grep(data, "parts processed by stepalesengine")
    assert _byte_grep(data, "SOLO-1")
    assert _byte_grep(data, "PID-SOLO")


# ---------------------------------------------------------------------------
# 2. 3-part manifest renders 4+ pages (cover + 3 parts)
# ---------------------------------------------------------------------------


def test_three_part_manifest_renders_cover_plus_three_pages(tmp_path: Path) -> None:
    # Mix of one assembly with two parts + one assembly with one part = 3 parts.
    m1 = _manifest(
        source=str(tmp_path / "asm-a.step"),
        entries=[
            _entry(name="A-1", product_id="PID-A1", label="plaat"),
            _entry(name="A-2", product_id="PID-A2", label="profiel"),
        ],
    )
    m2 = _manifest(
        source=str(tmp_path / "asm-b.step"),
        entries=[_entry(name="B-1", product_id="PID-B1", label="anders")],
    )
    out = write_corpus_pdf([m1, m2], tmp_path / "three.pdf")
    data = out.read_bytes()
    assert data.startswith(b"%PDF-")
    page_count = _count_pages(data)
    assert page_count >= 4, f"expected >=4 pages (cover + 3 parts), got {page_count}"
    for needle in ("A-1", "A-2", "B-1"):
        assert _byte_grep(data, needle), f"missing {needle!r} in PDF"
    # Cover header line.
    assert _byte_grep(data, "3 parts processed by stepalesengine")


# ---------------------------------------------------------------------------
# 3. Part without a flat_dxf_path still gets a page (placeholder)
# ---------------------------------------------------------------------------


def test_part_without_dxf_uses_placeholder(tmp_path: Path) -> None:
    manifest = _manifest(
        source=str(tmp_path / "no-dxf.step"),
        entries=[_entry(name="NO-DXF", product_id="PID-X", dxf_path=None)],
    )
    out = write_corpus_pdf([manifest], tmp_path / "nodxf.pdf")
    data = out.read_bytes()
    assert data.startswith(b"%PDF-")
    # Placeholder note text the writer emits when no DXF is available.
    # PDF content streams escape `(` and `)` with a backslash so the literal
    # substring won't match end-to-end; check the two halves separately.
    assert _byte_grep(data, "as-modelled")
    assert _byte_grep(data, "no flat DXF")
    # Per-part page still exists alongside the cover.
    assert _count_pages(data) >= 2


# ---------------------------------------------------------------------------
# 4. PMI fields make it into the per-part footer
# ---------------------------------------------------------------------------


def test_pmi_fields_rendered_in_footer(tmp_path: Path) -> None:
    manifest = _manifest(
        source=str(tmp_path / "pmi.step"),
        entries=[_entry(name="PMI-PART", product_id="PID-PMI", with_pmi=True)],
    )
    out = write_corpus_pdf([manifest], tmp_path / "pmi.pdf")
    data = out.read_bytes()
    # n_annotations=5, 1 tolerance, 2 datums, has_semantic=True --> rendered.
    assert _byte_grep(data, "annotations=5")
    assert _byte_grep(data, "tolerances=1")
    assert _byte_grep(data, "datums=2")
    assert _byte_grep(data, "semantic=yes")


# ---------------------------------------------------------------------------
# 5. CAM strategy summary line appears when entry.strategy is set
# ---------------------------------------------------------------------------


def test_cam_strategy_summary_rendered(tmp_path: Path) -> None:
    manifest = _manifest(
        source=str(tmp_path / "cam.step"),
        entries=[_entry(name="CAM-PART", product_id="PID-CAM", with_strategy=True)],
    )
    out = write_corpus_pdf([manifest], tmp_path / "cam.pdf")
    data = out.read_bytes()
    assert _byte_grep(data, "CAM: process=sheet_metal")
    assert _byte_grep(data, "setups=1")
    assert _byte_grep(data, "ops=2")
    # estimated_time_min printed with one decimal.
    assert _byte_grep(data, "est=3.2 min")


# ---------------------------------------------------------------------------
# 6. Real on-disk DXF gets embedded as vector graphics (no placeholder note)
# ---------------------------------------------------------------------------


def test_real_dxf_is_embedded(tmp_path: Path) -> None:
    dxf_path = _write_sample_dxf(tmp_path / "real.dxf", part_name="WITH-DXF")
    manifest = _manifest(
        source=str(tmp_path / "with-dxf.step"),
        entries=[_entry(name="WITH-DXF", product_id="PID-DXF", dxf_path=str(dxf_path))],
    )
    out = write_corpus_pdf([manifest], tmp_path / "real.pdf")
    data = out.read_bytes()
    assert data.startswith(b"%PDF-")
    # The placeholder text MUST NOT appear when the DXF was actually loaded.
    assert not _byte_grep(data, "as-modelled (no flat DXF)")
    assert not _byte_grep(data, "DXF unavailable")
    # Preview label is still drawn.
    assert _byte_grep(data, "Flat-DXF preview")


# ---------------------------------------------------------------------------
# 7. Compact (4-up) layout produces fewer pages
# ---------------------------------------------------------------------------


def test_compact_layout_fewer_pages(tmp_path: Path) -> None:
    entries = [_entry(name=f"P-{i}", product_id=f"PID-{i}", label="plaat") for i in range(5)]
    manifest = _manifest(source=str(tmp_path / "compact.step"), entries=entries)
    # 4-up: 5 parts -> 2 part pages + 1 cover = 3 pages.
    options = CorpusPDFOptions(parts_per_page=4, include_dxf_preview=False)
    out = write_corpus_pdf([manifest], tmp_path / "compact.pdf", options=options)
    data = out.read_bytes()
    assert data.startswith(b"%PDF-")
    page_count = _count_pages(data)
    assert page_count <= 4, f"compact layout should be <=4 pages, got {page_count}"
    # Cover still includes the total parts count.
    assert _byte_grep(data, "5 parts processed by stepalesengine")


# ---------------------------------------------------------------------------
# 8. Cover anomalies list flags zero-part assemblies
# ---------------------------------------------------------------------------


def test_cover_lists_zero_part_anomalies(tmp_path: Path) -> None:
    empty = _manifest(source=str(tmp_path / "empty.step"), entries=[])
    full = _manifest(
        source=str(tmp_path / "good.step"),
        entries=[_entry(name="OK-1", product_id="PID-OK")],
    )
    out = write_corpus_pdf([empty, full], tmp_path / "anom.pdf")
    data = out.read_bytes()
    assert _byte_grep(data, "empty.step: zero parts")
    # Should be 2 pages (cover + 1 real part).
    assert _count_pages(data) == 2


# ---------------------------------------------------------------------------
# 9. Page size / orientation validation
# ---------------------------------------------------------------------------


def test_invalid_page_size_raises(tmp_path: Path) -> None:
    manifest = _manifest(
        source=str(tmp_path / "x.step"),
        entries=[_entry(name="X", product_id="X")],
    )
    with pytest.raises(ValueError, match="page_size"):
        write_corpus_pdf(
            [manifest],
            tmp_path / "x.pdf",
            options=CorpusPDFOptions(page_size="Tabloid"),
        )


def test_invalid_orientation_raises(tmp_path: Path) -> None:
    manifest = _manifest(
        source=str(tmp_path / "x.step"),
        entries=[_entry(name="X", product_id="X")],
    )
    with pytest.raises(ValueError, match="orientation"):
        write_corpus_pdf(
            [manifest],
            tmp_path / "x.pdf",
            options=CorpusPDFOptions(page_size_orientation="diagonal"),
        )
