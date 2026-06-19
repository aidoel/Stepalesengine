"""Tests for the grouped-BOM rollup.

Covers:
    - manifest-level :func:`group_bom` (pure transform on AssemblyManifest)
    - PDF-level ``grouped=True`` parameter on ``write_assembly_pdf``
"""

from __future__ import annotations

import re
from pathlib import Path

from manufacturing_pipeline.bom import GroupedPartEntry, group_bom
from manufacturing_pipeline.classification.types import ClassificationResult, DecisionTrace
from manufacturing_pipeline.geometry.types import ManufacturingFeatures
from manufacturing_pipeline.io.dxf_writer import FlatPattern
from manufacturing_pipeline.io.pdf_writer import (
    PartDrawingMeta,
    write_assembly_pdf,
)
from manufacturing_pipeline.manifest import AssemblyManifest, PartManifestEntry
from manufacturing_pipeline.parsing.types import StepPart

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _features(
    *,
    volume: float = 1000.0,
    bbox: tuple[float, float, float] = (100.0, 50.0, 2.0),
) -> ManufacturingFeatures:
    return ManufacturingFeatures(
        volume=volume,
        surface_area=2000.0,
        bbox_dims_sorted=bbox,
    )


def _classification(label: str = "plaat") -> ClassificationResult:
    return ClassificationResult(label=label, confidence=0.9, trace=DecisionTrace())


def _entry(
    product_id: str,
    *,
    label: str = "plaat",
    quantity: int = 1,
    features: ManufacturingFeatures | None = None,
    flat_dxf_path: str | None = None,
) -> PartManifestEntry:
    return PartManifestEntry(
        part=StepPart(product_id=product_id, name=f"part-{product_id}"),
        classification=_classification(label),
        features=features if features is not None else _features(),
        quantity=quantity,
        flat_dxf_path=flat_dxf_path,
    )


def _manifest(parts: list[PartManifestEntry]) -> AssemblyManifest:
    return AssemblyManifest(
        source_path="/x.step",
        source_mtime=0.0,
        source_size=0,
        model_version="t",
        generated_at="2026-05-27",
        parts=parts,
    )


# ---------------------------------------------------------------------------
# 1. Two identical parts collapse with summed quantity
# ---------------------------------------------------------------------------


def test_two_identical_parts_collapse_qty_summed():
    m = _manifest([_entry("p1"), _entry("p2")])
    groups = group_bom(m)
    assert len(groups) == 1
    g = groups[0]
    assert isinstance(g, GroupedPartEntry)
    assert g.quantity == 2
    assert g.instance_ids == ["p1", "p2"]
    assert g.representative.part.product_id == "p1"


def test_quantity_sums_nauo_multiplicity():
    # Same geometry but different NAUO multiplicities on the source entries:
    # 3 instances + 2 instances = 5.
    m = _manifest([_entry("p1", quantity=3), _entry("p2", quantity=2)])
    groups = group_bom(m)
    assert len(groups) == 1
    assert groups[0].quantity == 5


# ---------------------------------------------------------------------------
# 2. Differing classification stays separate
# ---------------------------------------------------------------------------


def test_differing_classification_stays_separate():
    m = _manifest(
        [
            _entry("p1", label="plaat"),
            _entry("p2", label="profiel"),
        ]
    )
    groups = group_bom(m)
    assert len(groups) == 2
    labels = sorted(g.representative.classification.label for g in groups)
    assert labels == ["plaat", "profiel"]
    assert all(g.quantity == 1 for g in groups)


# ---------------------------------------------------------------------------
# 3. Differing flat pattern stays separate
# ---------------------------------------------------------------------------


def test_differing_flat_pattern_stays_separate(tmp_path: Path):
    dxf_a = tmp_path / "a.dxf"
    dxf_b = tmp_path / "b.dxf"
    dxf_a.write_bytes(b"contents-A")
    dxf_b.write_bytes(b"contents-B")
    m = _manifest(
        [
            _entry("p1", flat_dxf_path=str(dxf_a)),
            _entry("p2", flat_dxf_path=str(dxf_b)),
        ]
    )
    groups = group_bom(m)
    assert len(groups) == 2


def test_identical_dxf_contents_collapse(tmp_path: Path):
    dxf_a = tmp_path / "a.dxf"
    dxf_b = tmp_path / "b.dxf"
    payload = b"identical-flat-pattern"
    dxf_a.write_bytes(payload)
    dxf_b.write_bytes(payload)
    m = _manifest(
        [
            _entry("p1", flat_dxf_path=str(dxf_a)),
            _entry("p2", flat_dxf_path=str(dxf_b)),
        ]
    )
    groups = group_bom(m)
    # Same DXF content -> same hash -> single group.
    assert len(groups) == 1
    assert groups[0].quantity == 2


def test_differing_geometry_stays_separate_when_no_dxf():
    # Falls back to bbox + volume signature.
    m = _manifest(
        [
            _entry("p1", features=_features(volume=1000.0, bbox=(100.0, 50.0, 2.0))),
            _entry("p2", features=_features(volume=2000.0, bbox=(200.0, 50.0, 2.0))),
        ]
    )
    groups = group_bom(m)
    assert len(groups) == 2


# ---------------------------------------------------------------------------
# 4. Empty manifest
# ---------------------------------------------------------------------------


def test_empty_manifest_yields_empty_list():
    m = _manifest([])
    assert group_bom(m) == []


# ---------------------------------------------------------------------------
# 5. PDF surface: grouped=True collapses pages and BOM rows
# ---------------------------------------------------------------------------


def _rect(x0: float, y0: float, w: float, h: float) -> list[tuple[float, float]]:
    return [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]


def _flat_pattern(name: str = "P") -> FlatPattern:
    return FlatPattern(
        outer_contour=[_rect(0.0, 0.0, 80.0, 40.0)],
        thickness=2.0,
        bbox=(0.0, 0.0, 80.0, 40.0),
        part_name=name,
    )


def _count_pages(pdf_bytes: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page\b(?!s)", pdf_bytes))


def test_pdf_grouped_collapses_identical_parts(tmp_path: Path):
    # Two identical parts (same part_number/material/thickness/bbox), one different.
    parts: list[tuple[FlatPattern, PartDrawingMeta]] = [
        (
            _flat_pattern("A"),
            PartDrawingMeta(part_name="A", part_number="N-A", material="S235"),
        ),
        (
            _flat_pattern("A"),
            PartDrawingMeta(part_name="A", part_number="N-A", material="S235"),
        ),
        (
            _flat_pattern("B"),
            PartDrawingMeta(part_name="B", part_number="N-B", material="S355"),
        ),
    ]
    out = write_assembly_pdf(parts, tmp_path / "grouped.pdf", grouped=True)
    data = out.read_bytes()
    # cover + 2 unique parts == 3 pages (not cover + 3).
    assert _count_pages(data) == 3
    # Both part numbers still appear on cover.
    assert b"N-A" in data
    assert b"N-B" in data


def test_pdf_default_per_instance_still_works(tmp_path: Path):
    # The grouped flag defaults to False; this is the existing behaviour.
    parts: list[tuple[FlatPattern, PartDrawingMeta]] = [
        (
            _flat_pattern("A"),
            PartDrawingMeta(part_name="A", part_number="N-A", material="S235"),
        ),
        (
            _flat_pattern("A"),
            PartDrawingMeta(part_name="A", part_number="N-A", material="S235"),
        ),
    ]
    out = write_assembly_pdf(parts, tmp_path / "per_instance.pdf")
    data = out.read_bytes()
    # cover + 2 instances == 3 pages.
    assert _count_pages(data) == 3


def test_pdf_grouped_does_not_mutate_caller_meta(tmp_path: Path):
    meta = PartDrawingMeta(part_name="A", part_number="N-A", material="S235", quantity=1)
    parts = [(_flat_pattern("A"), meta), (_flat_pattern("A"), meta)]
    write_assembly_pdf(parts, tmp_path / "nm.pdf", grouped=True)
    # Caller's meta object must be untouched.
    assert meta.quantity == 1
