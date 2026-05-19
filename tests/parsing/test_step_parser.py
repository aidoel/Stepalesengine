"""Tests for the RobustStepParser orchestrator.

Covers the six-strategy cascade ordering, the OCAF fallback hook, the
filename-of-last-resort path, caching, decoration with Dutch vocabulary
and standard-label tags, and the StepParseError contract.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from manufacturing_pipeline.parsing import step_parser
from manufacturing_pipeline.parsing.step_parser import parse_step
from manufacturing_pipeline.parsing.types import StepParseError, StepPart

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_step_text(
    parts: list[dict[str, str]],
    *,
    header_filename: str = "synthetic.stp",
    schema: str = "AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }",
    extra_entities: Iterable[str] = (),
    nauo: Iterable[tuple] = (),
) -> str:
    """Build a minimal valid Part 21 body from a list of part specs.

    Each ``parts`` dict supports keys ``id``, ``name``, ``description``,
    and optional ``brep_name`` (emits a MANIFOLD_SOLID_BREP for the part).

    ``nauo`` is an iterable of ``(parent_index, child_index)`` tuples
    that emits one NEXT_ASSEMBLY_USAGE_OCCURRENCE per tuple linking the
    PRODUCT_DEFINITIONs of the indexed parts.
    """
    lines: list[str] = []
    lines.append("ISO-10303-21;")
    lines.append("HEADER;")
    lines.append("FILE_DESCRIPTION(('synthetic STEP'),'2;1');")
    lines.append(
        f"FILE_NAME('{header_filename}','2026-05-15T00:00:00',('Author'),('Org'),'tester','x','');"
    )
    lines.append(f"FILE_SCHEMA(('{schema}'));")
    lines.append("ENDSEC;")
    lines.append("DATA;")

    # Global product context shared by all PRODUCTs.
    lines.append("#1=APPLICATION_CONTEXT('mechanical design');")
    lines.append("#2=PRODUCT_CONTEXT('',#1,'mechanical');")

    pd_refs: list[int] = []
    next_id = 10
    for spec in parts:
        pid = spec["id"]
        name = spec["name"].replace("'", "''")
        desc = spec.get("description", "").replace("'", "''")

        prod_id = next_id
        pdf_id = next_id + 1
        pd_id = next_id + 2
        next_id += 3

        lines.append(f"#{prod_id}=PRODUCT('{pid}','{name}','{desc}',(#2));")
        lines.append(f"#{pdf_id}=PRODUCT_DEFINITION_FORMATION('','rev-a',#{prod_id});")
        lines.append(f"#{pd_id}=PRODUCT_DEFINITION('design','{desc}',#{pdf_id},$);")
        pd_refs.append(pd_id)

        brep_name = spec.get("brep_name")
        if brep_name:
            brep_name_escaped = brep_name.replace("'", "''")
            shell_id = next_id
            brep_id = next_id + 1
            next_id += 2
            lines.append(f"#{shell_id}=CLOSED_SHELL('',(#999));")
            lines.append(f"#{brep_id}=MANIFOLD_SOLID_BREP('{brep_name_escaped}',#{shell_id});")

    for parent_idx, child_idx in nauo:
        parent_pd = pd_refs[parent_idx]
        child_pd = pd_refs[child_idx]
        nauo_id = next_id
        next_id += 1
        lines.append(
            f"#{nauo_id}=NEXT_ASSEMBLY_USAGE_OCCURRENCE("
            f"'nauo-{nauo_id}','link','',#{child_pd},#{parent_pd},'1');"
        )

    for ent in extra_entities:
        lines.append(ent)

    lines.append("ENDSEC;")
    lines.append("END-ISO-10303-21;")
    return "\n".join(lines) + "\n"


def _write(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Every test starts with a fresh module-level cache."""
    step_parser._CACHE.clear()
    yield
    step_parser._CACHE.clear()


# ---------------------------------------------------------------------------
# End-to-end smoke tests
# ---------------------------------------------------------------------------


def test_parse_step_end_to_end_yields_non_empty(tmp_path):
    text = make_step_text(
        [
            {"id": "P-001", "name": "Plate 8mm", "description": "top plate"},
            {"id": "P-002", "name": "UNP 160", "description": "channel"},
        ]
    )
    path = _write(tmp_path, "demo.stp", text)
    parts = parse_step(path)
    assert isinstance(parts, list)
    assert len(parts) >= 1
    names = sorted(p.name for p in parts)
    assert "Plate 8mm" in names
    assert "UNP 160" in names


def test_parse_step_nauo_fixture_carries_parent_child_link(tmp_path):
    text = make_step_text(
        [
            {"id": "ROOT", "name": "Root assembly"},
            {"id": "CHILD", "name": "Bracket-Rev-A"},
        ],
        nauo=[(0, 1)],
    )
    path = _write(tmp_path, "assy.stp", text)
    parts = parse_step(path)
    by_id = {p.product_id: p for p in parts}
    assert "ROOT" in by_id and "CHILD" in by_id
    assert by_id["ROOT"].children == ["CHILD"]
    assert all(p.source == "nauo" for p in parts)


def test_parse_step_without_any_names_uses_filename(tmp_path):
    """No PRODUCT/PD/BREP entries, no comments, no useful header description."""
    text = (
        "ISO-10303-21;\n"
        "HEADER;\n"
        "FILE_DESCRIPTION((''),'2;1');\n"
        "FILE_NAME('','2026','','','','','');\n"
        "FILE_SCHEMA(('AP214'));\n"
        "ENDSEC;\n"
        "DATA;\n"
        "ENDSEC;\n"
        "END-ISO-10303-21;\n"
    )
    path = _write(tmp_path, "nameless-31686-080.stp", text)
    parts = parse_step(path, use_occt_fallback=False)
    # Filename basename is meaningful and falls through to either the
    # header strategy (basename) or to the final filename synthesise step.
    assert len(parts) >= 1
    assert parts[0].name.startswith("nameless-31686-080")
    assert parts[0].source in {"header", "filename"}


def test_parse_step_only_filename_source_when_strategies_blank(tmp_path, monkeypatch):
    """Force every strategy empty so the final filename synthesise step runs."""
    text = make_step_text([{"id": "X", "name": "Part1"}])  # junk name only
    path = _write(tmp_path, "fallback-99.stp", text)

    monkeypatch.setattr(step_parser, "strategy_header", lambda h, p: [])
    monkeypatch.setattr(step_parser, "strategy_comments", lambda r, p: [])
    monkeypatch.setattr(step_parser, "occt_fallback", lambda _p: [])

    parts = parse_step(path)
    assert len(parts) == 1
    assert parts[0].source == "filename"
    assert parts[0].name == "fallback-99"


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_parse_step_cache_returns_same_object(tmp_path, monkeypatch):
    text = make_step_text([{"id": "P", "name": "Plate 8mm"}])
    path = _write(tmp_path, "cached.stp", text)

    first = parse_step(path)

    # Force any subsequent disk read to blow up so we can prove the cache hit.
    def _bomb(_p: str) -> str:
        raise AssertionError("read_step_text should not be called on cache hit")

    monkeypatch.setattr(step_parser, "read_step_text", _bomb)

    second = parse_step(path)
    assert second is first
    assert [p.name for p in second] == ["Plate 8mm"]


def test_parse_step_cache_false_forces_reparse(tmp_path):
    text = make_step_text([{"id": "P", "name": "Plate 8mm"}])
    path = _write(tmp_path, "uncached.stp", text)

    first = parse_step(path, cache=False)
    second = parse_step(path, cache=False)
    assert first is not second
    assert [p.name for p in first] == [p.name for p in second]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_parse_step_raises_step_parse_error_on_missing(tmp_path):
    missing = str(tmp_path / "does-not-exist.stp")
    with pytest.raises(StepParseError):
        parse_step(missing)


# ---------------------------------------------------------------------------
# Strategy priority
# ---------------------------------------------------------------------------


def test_parse_step_nauo_wins_over_product(tmp_path):
    """Both NAUO and PRODUCT carry meaningful names. NAUO must win."""
    text = make_step_text(
        [
            {"id": "ROOT", "name": "Root assembly"},
            {"id": "CHILD", "name": "Plate 8mm"},
        ],
        nauo=[(0, 1)],
    )
    path = _write(tmp_path, "priority.stp", text)
    parts = parse_step(path)
    assert {p.source for p in parts} == {"nauo"}


# ---------------------------------------------------------------------------
# Decoration: Dutch vocabulary + standard labels
# ---------------------------------------------------------------------------


def test_parse_step_decorates_dutch_part_type(tmp_path):
    text = make_step_text(
        [
            {"id": "P-100", "name": "Plaatdeel-Frame", "description": "main frame plate"},
        ]
    )
    path = _write(tmp_path, "Plaatdeel-Frame.stp", text)
    parts = parse_step(path)
    assert len(parts) == 1
    assert "plate_part" in parts[0].description


def test_parse_step_decorates_standard_label(tmp_path):
    text = make_step_text(
        [
            {"id": "PROF-1", "name": "Channel", "description": "DIN-1026 channel"},
        ]
    )
    path = _write(tmp_path, "channel.stp", text)
    parts = parse_step(path)
    assert len(parts) == 1
    assert "DIN 1026" in parts[0].description


# ---------------------------------------------------------------------------
# OCAF fallback wiring
# ---------------------------------------------------------------------------


def test_parse_step_invokes_occt_fallback_when_strategies_empty(tmp_path, monkeypatch):
    text = make_step_text([{"id": "X", "name": "Part1"}])  # junk only
    path = _write(tmp_path, "occt-target.stp", text)

    # Force tokenize_entities empty and the header/comments strategies empty
    # so the cascade has nothing usable.
    monkeypatch.setattr(step_parser, "tokenize_entities", lambda _t: {})
    monkeypatch.setattr(step_parser, "strategy_header", lambda h, p: [])
    monkeypatch.setattr(step_parser, "strategy_comments", lambda r, p: [])

    called: dict[str, int] = {"n": 0}

    def fake_occt(_path):
        called["n"] += 1
        return [
            StepPart(
                product_id="0:1:1:1",
                name="From XCAF",
                description="",
                children=[],
                source="occt_xcaf",
            )
        ]

    monkeypatch.setattr(step_parser, "occt_fallback", fake_occt)

    parts = parse_step(path)
    assert called["n"] == 1
    assert len(parts) == 1
    assert parts[0].source == "occt_xcaf"
    assert parts[0].name == "From XCAF"


def test_occt_fallback_returns_empty_when_ocp_unavailable(tmp_path, monkeypatch):
    """The fallback must never propagate import or runtime errors."""
    import builtins

    from manufacturing_pipeline.parsing import occt_fallback as ocf

    real_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name.startswith("OCP"):
            raise ImportError("OCP not available in this test environment")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)

    # Write a file so the path exists, then call the fallback directly.
    text = make_step_text([{"id": "P", "name": "Plate 8mm"}])
    path = _write(tmp_path, "nopcp.stp", text)

    result = ocf.occt_fallback(path)
    assert result == []


def test_parse_step_skips_occt_when_disabled(tmp_path, monkeypatch):
    text = make_step_text([{"id": "X", "name": "Part1"}])  # junk only
    path = _write(tmp_path, "skip-occt.stp", text)

    monkeypatch.setattr(step_parser, "tokenize_entities", lambda _t: {})
    monkeypatch.setattr(step_parser, "strategy_header", lambda h, p: [])
    monkeypatch.setattr(step_parser, "strategy_comments", lambda r, p: [])

    def _no_occt(_path):
        raise AssertionError("occt_fallback should not be called when use_occt_fallback=False")

    monkeypatch.setattr(step_parser, "occt_fallback", _no_occt)

    parts = parse_step(path, use_occt_fallback=False)
    assert len(parts) == 1
    assert parts[0].source == "filename"
