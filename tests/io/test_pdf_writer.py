"""Tests for manufacturing_pipeline.io.pdf_writer.

We verify by writing real PDFs to ``tmp_path`` and inspecting them:

    1. via the leading byte signature (``%PDF-``),
    2. via the trailer page count (``/Type /Page`` occurrences),
    3. via byte-grepping for embedded metadata strings -- the strings end up
       in the content stream / xref so this is a reliable way to assert
       without pulling in pypdf.

No new runtime deps are introduced; tests use only ``reportlab`` (already
required by the writer) and the standard library.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from manufacturing_pipeline.io.dxf_writer import FlatPattern
from manufacturing_pipeline.io.pdf_writer import (
    PartDrawingMeta,
    write_assembly_pdf,
    write_pdf,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _rect(x0: float, y0: float, w: float, h: float) -> list[tuple[float, float]]:
    return [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]


def _circle_poly(cx: float, cy: float, r: float, n: int = 32) -> list[tuple[float, float]]:
    return [
        (cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


def _sample_pattern(part_name: str = "TEST-001") -> FlatPattern:
    return FlatPattern(
        outer_contour=[_rect(0.0, 0.0, 100.0, 60.0)],
        holes=[
            _circle_poly(20.0, 30.0, 4.0),
            _circle_poly(50.0, 30.0, 4.0),
            _circle_poly(80.0, 30.0, 8.0),
        ],
        bend_lines=[
            ((25.0, 0.0), (25.0, 60.0), {"angle": 90.0, "radius": 1.5, "direction": "up"}),
            ((75.0, 0.0), (75.0, 60.0), {"angle": 45.0, "radius": 2.0, "direction": "down"}),
        ],
        thickness=2.0,
        bbox=(0.0, 0.0, 100.0, 60.0),
        part_name=part_name,
    )


# ---------------------------------------------------------------------------
# PDF inspection helpers
# ---------------------------------------------------------------------------


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _count_pages(pdf_bytes: bytes) -> int:
    """Count page objects by matching ``/Type /Page`` (not ``/Pages``)."""
    return len(re.findall(rb"/Type\s*/Page\b(?!s)", pdf_bytes))


def _byte_grep(pdf_bytes: bytes, needle: str) -> bool:
    """True if ``needle`` (utf-8) appears anywhere in the raw PDF bytes."""
    return needle.encode("utf-8") in pdf_bytes


# ---------------------------------------------------------------------------
# 1. Basic write
# ---------------------------------------------------------------------------


def test_write_pdf_simple_rectangle(tmp_path: Path):
    pattern = FlatPattern(
        outer_contour=[_rect(0, 0, 80, 40)],
        thickness=1.5,
        bbox=(0, 0, 80, 40),
        part_name="SIMPLE",
    )
    out = write_pdf(pattern, tmp_path / "simple.pdf")
    assert out.exists()
    assert out.stat().st_size > 0


# ---------------------------------------------------------------------------
# 2. Valid PDF signature
# ---------------------------------------------------------------------------


def test_pdf_starts_with_magic(tmp_path: Path):
    out = write_pdf(_sample_pattern(), tmp_path / "magic.pdf")
    data = _read_bytes(out)
    assert data.startswith(b"%PDF-"), "missing PDF magic bytes"
    # PDFs also include the EOF marker.
    assert b"%%EOF" in data


# ---------------------------------------------------------------------------
# 3. Sanity: PDF is at least 1 KB
# ---------------------------------------------------------------------------


def test_pdf_size_above_1kb(tmp_path: Path):
    out = write_pdf(_sample_pattern(), tmp_path / "size.pdf")
    assert out.stat().st_size > 1024, f"PDF unexpectedly small: {out.stat().st_size} bytes"


# ---------------------------------------------------------------------------
# 4. Metadata strings present in raw bytes
# ---------------------------------------------------------------------------


def test_pdf_contains_metadata(tmp_path: Path):
    pattern = _sample_pattern("BRACKET-1")
    meta = PartDrawingMeta(
        part_name="BRACKET-1",
        part_number="ABC-123",
        material="S235",
        quantity=4,
        revision="B",
        drawn_by="DS",
        date_iso="2026-05-15",
    )
    out = write_pdf(pattern, tmp_path / "meta.pdf", meta=meta)
    data = _read_bytes(out)

    # Each tagged metadata string the writer puts on the title block.
    for needle in ["ABC-123", "S235", "BRACKET-1", "2026-05-15", "DS"]:
        assert _byte_grep(data, needle), (
            f"expected metadata string {needle!r} not found in PDF bytes"
        )

    # Quantity '4' is short and ambiguous, so look for an exact title-block label.
    assert _byte_grep(data, "Qty"), "missing Qty label in title block"


# ---------------------------------------------------------------------------
# 5. Assembly PDF: 3 parts -> >=4 pages (cover + parts)
# ---------------------------------------------------------------------------


def test_assembly_pdf_multipage(tmp_path: Path):
    parts: list[tuple[FlatPattern, PartDrawingMeta]] = [
        (_sample_pattern("P-1"), PartDrawingMeta(part_name="P-1", part_number="N1")),
        (_sample_pattern("P-2"), PartDrawingMeta(part_name="P-2", part_number="N2")),
        (_sample_pattern("P-3"), PartDrawingMeta(part_name="P-3", part_number="N3")),
    ]
    out = write_assembly_pdf(parts, tmp_path / "asm.pdf")
    data = _read_bytes(out)
    assert data.startswith(b"%PDF-")
    assert _count_pages(data) >= 4, f"expected >=4 pages (cover + 3), got {_count_pages(data)}"
    # BOM cover should list every part number.
    for n in ("N1", "N2", "N3"):
        assert _byte_grep(data, n)


# ---------------------------------------------------------------------------
# 6. Validation: thickness=0 raises
# ---------------------------------------------------------------------------


def test_thickness_zero_raises(tmp_path: Path):
    bad = FlatPattern(
        outer_contour=[_rect(0, 0, 10, 10)],
        thickness=0.0,
        bbox=(0, 0, 10, 10),
        part_name="BAD",
    )
    with pytest.raises(ValueError, match="thickness"):
        write_pdf(bad, tmp_path / "bad.pdf")


# ---------------------------------------------------------------------------
# 7. Empty holes + empty bends still renders
# ---------------------------------------------------------------------------


def test_no_holes_no_bends_still_renders(tmp_path: Path):
    pattern = FlatPattern(
        outer_contour=[_rect(0, 0, 50, 50)],
        holes=[],
        bend_lines=[],
        thickness=1.0,
        bbox=(0, 0, 50, 50),
        part_name="PLAIN",
    )
    out = write_pdf(pattern, tmp_path / "plain.pdf")
    data = _read_bytes(out)
    assert data.startswith(b"%PDF-")
    assert out.stat().st_size > 1024
    # Tables still emit their headers even when empty.
    assert _byte_grep(data, "Bend table")
    assert _byte_grep(data, "Hole table")


# ---------------------------------------------------------------------------
# Extra coverage
# ---------------------------------------------------------------------------


def test_unknown_page_size_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="page_size"):
        write_pdf(
            _sample_pattern(),
            tmp_path / "x.pdf",
            page_size="Tabloid",
        )


def test_unknown_orientation_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="orientation"):
        write_pdf(
            _sample_pattern(),
            tmp_path / "y.pdf",
            sheet_orientation="diagonal",
        )


def test_assembly_empty_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        write_assembly_pdf([], tmp_path / "empty.pdf")


def test_assembly_invalid_part_raises(tmp_path: Path):
    bad = FlatPattern(
        outer_contour=[_rect(0, 0, 10, 10)],
        thickness=0.0,
        part_name="BAD",
    )
    with pytest.raises(ValueError, match="thickness"):
        write_assembly_pdf(
            [(bad, PartDrawingMeta(part_name="BAD"))],
            tmp_path / "asm-bad.pdf",
        )
