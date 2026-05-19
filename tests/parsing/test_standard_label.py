"""Standard-label canonicaliser tests."""

from __future__ import annotations

from manufacturing_pipeline.parsing.standard_label import normalize_standard


def test_din_variants_normalised():
    out = normalize_standard("Profiel DIN1026-2 / EN 10210-2 vs din-en-iso 4014")
    assert "DIN 1026-2" in out
    assert "EN 10210-2" in out
    assert "DIN EN ISO 4014" in out


def test_no_match_returns_empty():
    assert normalize_standard("Plaatdeel 014") == []


def test_iso_alone():
    assert "ISO 4762" in normalize_standard("ISO 4762 - M8 - 30")


def test_separator_variants():
    text = "DIN 1025 / DIN-1025 / DIN1025"
    out = normalize_standard(text)
    assert out.count("DIN 1025") == 3
