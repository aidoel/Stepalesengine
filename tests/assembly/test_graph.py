"""Tests for the assembly graph builder."""

from __future__ import annotations

import logging

import pytest

from manufacturing_pipeline.assembly.graph import (
    ROOT_ID,
    aggregate_quantities,
    build_assembly_graph,
    flatten,
    iter_leaves,
)
from manufacturing_pipeline.parsing.types import StepPart


def _part(
    pid: str, name: str = "", children: list[str] | None = None, source: str = "nauo"
) -> StepPart:
    return StepPart(
        product_id=pid,
        name=name or pid,
        description="",
        children=list(children or []),
        source=source,
    )


def test_empty_parts_returns_root_with_no_children() -> None:
    root = build_assembly_graph([])
    assert root.product_id == ROOT_ID
    assert root.children == []
    assert root.depth == 0
    assert root.parent_id is None
    assert root.is_leaf is True


def test_single_part_becomes_a_depth_one_leaf() -> None:
    root = build_assembly_graph([_part("A", "alpha")])
    assert len(root.children) == 1
    leaf = root.children[0]
    assert leaf.name == "alpha"
    assert leaf.depth == 1
    assert leaf.parent_id == ROOT_ID
    assert leaf.is_leaf is True


def test_three_part_nauo_tree_structure_and_depths() -> None:
    parts = [
        _part("A", children=["B", "C"]),
        _part("B"),
        _part("C"),
    ]
    root = build_assembly_graph(parts)
    assert len(root.children) == 1
    a = root.children[0]
    assert a.product_id == "A"
    assert a.depth == 1
    assert a.is_leaf is False
    assert {c.product_id for c in a.children} == {"B", "C"}
    for child in a.children:
        assert child.depth == 2
        assert child.parent_id == "A"
        assert child.is_leaf is True


def test_duplicate_product_ids_are_deduped_keep_first() -> None:
    parts = [
        _part("A", name="first"),
        _part("A", name="second"),
        _part("B"),
    ]
    root = build_assembly_graph(parts)
    by_id = {c.product_id: c for c in root.children}
    assert by_id["A"].name == "first"
    assert "B" in by_id


def test_cyclic_parent_child_does_not_loop(caplog: pytest.LogCaptureFixture) -> None:
    parts = [
        _part("A", children=["B"]),
        _part("B", children=["A"]),
    ]
    with caplog.at_level(logging.WARNING):
        root = build_assembly_graph(parts)
    nodes = flatten(root)
    # Finite traversal (no recursion explosion) and warning was emitted.
    assert len(nodes) <= 10
    assert any("cyclic" in rec.message.lower() for rec in caplog.records)


def test_dangling_child_ref_produces_synthetic_node() -> None:
    parts = [_part("A", children=["GHOST"])]
    root = build_assembly_graph(parts)
    a = root.children[0]
    assert len(a.children) == 1
    ghost = a.children[0]
    assert ghost.product_id == "GHOST"
    assert ghost.source == "dangling"
    assert ghost.is_leaf is True


def test_iter_leaves_returns_only_leaves_in_stable_order() -> None:
    parts = [
        _part("A", children=["Z", "B"]),
        _part("B"),
        _part("Z"),
    ]
    root = build_assembly_graph(parts)
    leaves = list(iter_leaves(root))
    names = [leaf.name for leaf in leaves]
    assert names == sorted(names)
    assert all(leaf.is_leaf for leaf in leaves)


def test_aggregate_quantities_counts_multiplicity() -> None:
    parts = [
        _part("A", children=["B", "B", "C"]),  # B appears twice under A
        _part("B"),
        _part("C"),
    ]
    root = build_assembly_graph(parts)
    counts = aggregate_quantities(root)
    assert counts["A"] == 1
    assert counts["B"] == 2
    assert counts["C"] == 1


def test_flatten_returns_preorder_with_root_first() -> None:
    parts = [
        _part("A", children=["B"]),
        _part("B", children=["C"]),
        _part("C"),
    ]
    root = build_assembly_graph(parts)
    nodes = flatten(root)
    ids = [n.product_id for n in nodes]
    assert ids[0] == ROOT_ID
    assert ids == [ROOT_ID, "A", "B", "C"]


def test_no_nauo_hangs_parts_directly_off_root() -> None:
    parts = [_part("A"), _part("B"), _part("C")]
    root = build_assembly_graph(parts)
    assert {c.product_id for c in root.children} == {"A", "B", "C"}
    for child in root.children:
        assert child.is_leaf is True
        assert child.depth == 1
