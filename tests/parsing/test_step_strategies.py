"""Tests for the six-strategy STEP parsing cascade.

Each strategy is a pure function over a synthetic entity dict
``{entity_id: (entity_name, raw_args_string)}`` modelled on the output of
``step_tokenizer.tokenize_entities``.
"""

from __future__ import annotations

import pytest

from manufacturing_pipeline.parsing.step_strategies import (
    JUNK_NAMES,
    is_meaningful,
    looks_like_feature_name,
    strategy_brep_names,
    strategy_comments,
    strategy_header,
    strategy_nauo,
    strategy_product,
    strategy_product_definition,
)
from manufacturing_pipeline.parsing.types import StepPart

# ---------------------------------------------------------------------------
# is_meaningful
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Plaat-12mm",
        "DIN 1026 UNP 160",
        "Bracket-Rev-A",
        "Voorflens",
        "L_Profile_50x50x5",
    ],
)
def test_is_meaningful_accepts_real_names(name):
    assert is_meaningful(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "Part",
        "part",
        "Part1",
        "PART1",
        "Solid_42",
        "Body0042",
        "Component_0001",
        "Item-7",
        "Object_3",
        "component",
        "Default",
        "Unnamed",
        "Untitled",
    ],
)
def test_is_meaningful_rejects_junk_and_auto_names(name):
    assert is_meaningful(name) is False


def test_is_meaningful_non_string_returns_false():
    assert is_meaningful(None) is False  # type: ignore[arg-type]


def test_junk_names_contains_expected_baseline():
    # Sanity-check that the original junk set wasn't dropped.
    for canonical in ["part", "part1", "component", "body", "solid", "unnamed", "untitled"]:
        assert canonical in JUNK_NAMES


# ---------------------------------------------------------------------------
# strategy_product
# ---------------------------------------------------------------------------


def test_strategy_product_emits_one_part_per_product():
    entities = {
        10: ("PRODUCT", "'P-001','Plaat 8mm','top plate',(#1)"),
        11: ("PRODUCT", "'P-002','UNP 160','channel',(#1)"),
    }
    parts = strategy_product(entities)
    assert len(parts) == 2
    names = sorted(p.name for p in parts)
    assert names == ["Plaat 8mm", "UNP 160"]
    assert all(p.source == "product" for p in parts)
    by_id = {p.product_id: p for p in parts}
    assert by_id["P-001"].description == "top plate"


def test_strategy_product_skips_junk_names():
    entities = {
        20: ("PRODUCT", "'X','Part1','',(#1)"),
        21: ("PRODUCT", "'Y','Solid_42','',(#1)"),
        22: ("PRODUCT", "'Z','RealName','',(#1)"),
    }
    parts = strategy_product(entities)
    assert [p.name for p in parts] == ["RealName"]


def test_strategy_product_dedupes():
    entities = {
        30: ("PRODUCT", "'A','Bracket','',(#1)"),
        31: ("PRODUCT", "'A','Bracket','',(#1)"),
    }
    parts = strategy_product(entities)
    assert len(parts) == 1


def test_strategy_product_empty_input():
    assert strategy_product({}) == []


# ---------------------------------------------------------------------------
# strategy_product_definition
# ---------------------------------------------------------------------------


def test_strategy_product_definition_walks_to_product_name():
    entities = {
        100: ("PRODUCT", "'P-100','HEA 200','I-beam',(#1)"),
        101: ("PRODUCT_DEFINITION_FORMATION", "'','first version',#100"),
        102: ("PRODUCT_DEFINITION", "'design','PD description',#101,#999"),
    }
    parts = strategy_product_definition(entities)
    assert len(parts) == 1
    p = parts[0]
    assert p.name == "HEA 200"
    assert p.product_id == "P-100"
    assert p.description == "PD description"
    assert p.source == "product_definition"


def test_strategy_product_definition_skips_when_walk_yields_junk():
    entities = {
        110: ("PRODUCT", "'P','Part1','',(#1)"),
        111: ("PRODUCT_DEFINITION_FORMATION", "'','',#110"),
        112: ("PRODUCT_DEFINITION", "'','desc',#111,$"),
    }
    assert strategy_product_definition(entities) == []


def test_strategy_product_definition_handles_missing_pdf():
    """When the PDF is missing we should still produce no spurious entries."""
    entities = {
        120: ("PRODUCT_DEFINITION", "'design','',#999,$"),
    }
    parts = strategy_product_definition(entities)
    # No PDF and no PRODUCT to walk to → empty (id 'design' is meaningful but
    # there's no usable name; the strategy must not crash).
    assert parts == []


# ---------------------------------------------------------------------------
# strategy_nauo
# ---------------------------------------------------------------------------


def _build_simple_assembly():
    """An assembly: ROOT contains CHILD (one NAUO)."""
    return {
        1: ("PRODUCT", "'ROOT','Root assembly','',(#9)"),
        2: ("PRODUCT_DEFINITION_FORMATION", "'','',#1"),
        3: ("PRODUCT_DEFINITION", "'design','',#2,$"),
        4: ("PRODUCT", "'CHILD','Plaat 12mm','',(#9)"),
        5: ("PRODUCT_DEFINITION_FORMATION", "'','',#4"),
        6: ("PRODUCT_DEFINITION", "'design','',#5,$"),
        # NAUO: (id, name, desc, relating_pd, related_pd, ref_designator)
        # ISO order: relating (parent) first, related (child) second.
        7: (
            "NEXT_ASSEMBLY_USAGE_OCCURRENCE",
            "'nauo-1','link','',#3,#6,'1'",
        ),
    }


def test_strategy_nauo_builds_parent_child_link():
    entities = _build_simple_assembly()
    parts = strategy_nauo(entities)
    by_id = {p.product_id: p for p in parts}
    assert "ROOT" in by_id
    assert "CHILD" in by_id
    assert by_id["ROOT"].children == ["CHILD"]
    assert by_id["CHILD"].children == []
    assert all(p.source == "nauo" for p in parts)


def test_strategy_nauo_cycle_guard():
    """A references B references A — must terminate, each part appears once."""
    entities = {
        1: ("PRODUCT", "'A','PartA','',(#9)"),
        2: ("PRODUCT_DEFINITION_FORMATION", "'','',#1"),
        3: ("PRODUCT_DEFINITION", "'design','',#2,$"),
        4: ("PRODUCT", "'B','PartB','',(#9)"),
        5: ("PRODUCT_DEFINITION_FORMATION", "'','',#4"),
        6: ("PRODUCT_DEFINITION", "'design','',#5,$"),
        # A contains B (relating=#3 parent, related=#6 child)
        7: ("NEXT_ASSEMBLY_USAGE_OCCURRENCE", "'n1','','',#3,#6,'1'"),
        # B contains A — cyclic
        8: ("NEXT_ASSEMBLY_USAGE_OCCURRENCE", "'n2','','',#6,#3,'1'"),
    }
    parts = strategy_nauo(entities)
    pids = sorted(p.product_id for p in parts)
    assert pids == ["A", "B"]
    # Each part should appear exactly once.
    assert len(parts) == 2


def test_strategy_nauo_empty_when_no_nauo_entries():
    entities = {
        1: ("PRODUCT", "'A','Plate','',(#9)"),
    }
    assert strategy_nauo(entities) == []


def test_strategy_nauo_tolerates_malformed_args():
    entities = {
        1: ("NEXT_ASSEMBLY_USAGE_OCCURRENCE", "garbage"),
        2: ("NEXT_ASSEMBLY_USAGE_OCCURRENCE", ""),
    }
    # Must not raise.
    assert strategy_nauo(entities) == []


# ---------------------------------------------------------------------------
# strategy_brep_names
# ---------------------------------------------------------------------------


def test_strategy_brep_names_picks_up_named_manifold():
    entities = {
        500: ("MANIFOLD_SOLID_BREP", "'TopPlate',#501"),
        501: ("CLOSED_SHELL", "'',#502"),
        502: ("SHELL_BASED_SURFACE_MODEL", "'SideShell',(#999)"),
    }
    parts = strategy_brep_names(entities)
    names = sorted(p.name for p in parts)
    assert names == ["SideShell", "TopPlate"]
    assert all(p.source == "brep" for p in parts)


def test_strategy_brep_names_skips_junk():
    entities = {
        600: ("MANIFOLD_SOLID_BREP", "'',#1"),
        601: ("MANIFOLD_SOLID_BREP", "'Body0042',#1"),
        602: ("MANIFOLD_SOLID_BREP", "'Real Body',#1"),
    }
    parts = strategy_brep_names(entities)
    assert [p.name for p in parts] == ["Real Body"]


# ---------------------------------------------------------------------------
# strategy_header
# ---------------------------------------------------------------------------


def test_strategy_header_synthesises_part_from_filename():
    header = {
        "FILE_NAME": {"description": "Bracket assembly export"},
    }
    parts = strategy_header(header, "/tmp/some/path/31686-080.stp")
    assert len(parts) == 1
    p = parts[0]
    assert p.source == "header"
    assert p.name == "31686-080"
    assert "Bracket assembly export" in p.description


def test_strategy_header_works_without_header_metadata():
    parts = strategy_header({}, "/files/widget.STEP")
    assert len(parts) == 1
    assert parts[0].name == "widget"
    assert parts[0].source == "header"


def test_strategy_header_empty_inputs_still_yield_one_part():
    parts = strategy_header({}, "")
    assert len(parts) == 1
    assert parts[0].source == "header"


# ---------------------------------------------------------------------------
# strategy_comments
# ---------------------------------------------------------------------------


def test_strategy_comments_finds_din_label():
    raw = "ISO-10303-21; /* DIN 1026-2 / UNP 160 */ DATA; #1 = FOO();"
    parts = strategy_comments(raw, "/tmp/profile.stp")
    names = [p.name for p in parts]
    assert "DIN 1026-2" in names
    assert all(p.source == "comments" for p in parts)


def test_strategy_comments_returns_empty_when_no_labels():
    raw = "/* nothing useful here */"
    assert strategy_comments(raw, "/tmp/x.stp") == []


def test_strategy_comments_dedupes_labels():
    raw = "/* DIN 1025 */ /* DIN 1025 */ /* EN 10210-2 */"
    parts = strategy_comments(raw, "/tmp/x.stp")
    names = sorted(p.name for p in parts)
    assert names == ["DIN 1025", "EN 10210-2"]


def test_strategy_comments_handles_empty_raw():
    assert strategy_comments("", "/tmp/x.stp") == []


# ---------------------------------------------------------------------------
# Cross-cutting properties: never None, never raise
# ---------------------------------------------------------------------------


def test_all_strategies_return_lists_not_none_on_empty_input():
    assert isinstance(strategy_nauo({}), list)
    assert isinstance(strategy_product({}), list)
    assert isinstance(strategy_product_definition({}), list)
    assert isinstance(strategy_brep_names({}), list)
    assert isinstance(strategy_header({}, ""), list)
    assert isinstance(strategy_comments("", ""), list)


def test_strategies_return_steppart_instances():
    entities = {1: ("PRODUCT", "'P','Plate-8','',(#1)")}
    parts = strategy_product(entities)
    assert parts
    assert all(isinstance(p, StepPart) for p in parts)


# ---------------------------------------------------------------------------
# CAD feature-name detection and brep-strategy rejection
# ---------------------------------------------------------------------------


def test_looks_like_feature_name_recognises_solidworks_features():
    for name in ("Cut-Extrude9", "Boss-Extrude1", "Fillet3", "Mirror2", "Revolve1"):
        assert looks_like_feature_name(name), name


def test_looks_like_feature_name_rejects_real_part_numbers():
    for name in ("MD-17-04193_2", "10001073530_Rev_00", "31686-080", "UNP 160"):
        assert not looks_like_feature_name(name), name


def test_strategy_brep_skips_cad_feature_names():
    """A MANIFOLD_SOLID_BREP labelled with a SolidWorks feature name carries no
    part identity, so the brep strategy must not emit a part for it."""
    entities = {1: ("MANIFOLD_SOLID_BREP", "'Cut-Extrude9',#9")}
    assert strategy_brep_names(entities) == []


def test_strategy_brep_keeps_real_part_names():
    entities = {1: ("MANIFOLD_SOLID_BREP", "'MD-17-04193_2',#9")}
    parts = strategy_brep_names(entities)
    assert [p.name for p in parts] == ["MD-17-04193_2"]


# ---------------------------------------------------------------------------
# PRODUCT_DEFINITION_FORMATION subtype + NAUO orientation
# ---------------------------------------------------------------------------


def _assembly_with_formation_subtype():
    """ROOT contains CHILD, using PRODUCT_DEFINITION_FORMATION_WITH_SPECIFIED_
    SOURCE (the AP214 subtype) in place of the bare formation entity."""
    return {
        1: ("PRODUCT", "'ROOT','ROOT','',(#9)"),
        2: ("PRODUCT_DEFINITION_FORMATION_WITH_SPECIFIED_SOURCE", "'','',#1,.BOUGHT."),
        3: ("PRODUCT_DEFINITION", "'',' ',#2,$"),
        4: ("PRODUCT", "'CHILD','CHILD','',(#9)"),
        5: ("PRODUCT_DEFINITION_FORMATION_WITH_SPECIFIED_SOURCE", "'','',#4,.BOUGHT."),
        6: ("PRODUCT_DEFINITION", "'',' ',#5,$"),
        7: ("NEXT_ASSEMBLY_USAGE_OCCURRENCE", "'n1','','',#3,#6,''"),
    }


def test_strategy_nauo_walks_formation_subtype():
    parts = strategy_nauo(_assembly_with_formation_subtype())
    by_id = {p.product_id: p for p in parts}
    assert by_id["ROOT"].children == ["CHILD"]
    assert by_id["CHILD"].children == []


def test_strategy_product_definition_walks_formation_subtype():
    parts = strategy_product_definition(_assembly_with_formation_subtype())
    assert sorted(p.name for p in parts) == ["CHILD", "ROOT"]


def test_strategy_nauo_keeps_repeated_child_instances():
    """Three NAUOs naming the same component must produce three child entries
    so build_assembly_graph can recover a quantity of 3."""
    entities = {
        1: ("PRODUCT", "'ASM','ASM','',(#9)"),
        2: ("PRODUCT_DEFINITION_FORMATION", "'','',#1"),
        3: ("PRODUCT_DEFINITION", "'',' ',#2,$"),
        4: ("PRODUCT", "'PART','PART','',(#9)"),
        5: ("PRODUCT_DEFINITION_FORMATION", "'','',#4"),
        6: ("PRODUCT_DEFINITION", "'',' ',#5,$"),
        7: ("NEXT_ASSEMBLY_USAGE_OCCURRENCE", "'n1','','',#3,#6,''"),
        8: ("NEXT_ASSEMBLY_USAGE_OCCURRENCE", "'n2','','',#3,#6,''"),
        9: ("NEXT_ASSEMBLY_USAGE_OCCURRENCE", "'n3','','',#3,#6,''"),
    }
    parts = strategy_nauo(entities)
    by_id = {p.product_id: p for p in parts}
    assert by_id["ASM"].children == ["PART", "PART", "PART"]


def test_strategy_nauo_flips_orientation_when_schema_inverted():
    """When the authoring tool emits the PD pair swapped (component first), the
    recurring assembly slot is still detected as the parent."""
    entities = {
        1: ("PRODUCT", "'ASM','ASM','',(#9)"),
        2: ("PRODUCT_DEFINITION_FORMATION", "'','',#1"),
        3: ("PRODUCT_DEFINITION", "'',' ',#2,$"),
        4: ("PRODUCT", "'P1','P1','',(#9)"),
        5: ("PRODUCT_DEFINITION_FORMATION", "'','',#4"),
        6: ("PRODUCT_DEFINITION", "'',' ',#5,$"),
        7: ("PRODUCT", "'P2','P2','',(#9)"),
        8: ("PRODUCT_DEFINITION_FORMATION", "'','',#7"),
        9: ("PRODUCT_DEFINITION", "'',' ',#8,$"),
        # Component PD first, assembly PD (#3) second on every NAUO.
        10: ("NEXT_ASSEMBLY_USAGE_OCCURRENCE", "'n1','','',#6,#3,''"),
        11: ("NEXT_ASSEMBLY_USAGE_OCCURRENCE", "'n2','','',#9,#3,''"),
    }
    parts = strategy_nauo(entities)
    by_id = {p.product_id: p for p in parts}
    assert sorted(by_id["ASM"].children) == ["P1", "P2"]
    assert by_id["P1"].children == []
    assert by_id["P2"].children == []


# ---------------------------------------------------------------------------
# strategy_header prefers the authored FILE_NAME over the on-disk basename
# ---------------------------------------------------------------------------


def test_strategy_header_prefers_file_name_field_over_basename():
    """parse_header yields FILE_NAME as a raw arg list; its first arg is the
    authored name and must beat a renamed on-disk basename."""
    header = {"file_name": ["'10001073530_Rev_00.stp'", "'2026-03-24'"]}
    parts = strategy_header(header, "/tmp/exports/sheet_10001073530_rev00.stp")
    assert len(parts) == 1
    assert parts[0].name == "10001073530_Rev_00"


def test_strategy_header_falls_back_to_basename_without_file_name():
    parts = strategy_header({}, "/tmp/exports/31686-080.stp")
    assert parts[0].name == "31686-080"
