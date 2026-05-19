"""Tests for the leaf-to-solid matcher."""

from __future__ import annotations

import pytest

from manufacturing_pipeline.assembly.graph import AssemblyNode
from manufacturing_pipeline.assembly.matcher import MatchResult, match_parts_to_solids


def _make_solid(size: float = 10.0):
    """Build a small OCP solid box. Skips the test if OCP isn't present."""
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return BRepPrimAPI_MakeBox(size, size, size).Solid()


def _leaf(pid: str, name: str | None = None) -> AssemblyNode:
    return AssemblyNode(
        product_id=pid,
        name=name or pid,
        description="",
        source="nauo",
        is_leaf=True,
        depth=1,
        parent_id="__ROOT__",
    )


def test_one_leaf_one_solid_uses_1to1_with_full_confidence() -> None:
    leaf = _leaf("A")
    solid = _make_solid(5.0)
    results = match_parts_to_solids([leaf], [solid])
    assert len(results) == 1
    r = results[0]
    assert r.method == "1to1"
    assert r.confidence == pytest.approx(1.0)
    assert r.solid is solid
    assert r.node is leaf


def test_equal_counts_above_one_uses_ordered() -> None:
    leaves = [_leaf("A"), _leaf("B"), _leaf("C")]
    solids = [_make_solid(s) for s in (3.0, 4.0, 5.0)]
    results = match_parts_to_solids(leaves, solids)
    assert len(results) == 3
    for r, leaf, solid in zip(results, leaves, solids):
        assert r.method == "ordered"
        assert r.confidence == pytest.approx(0.8)
        assert r.node is leaf
        assert r.solid is solid


def test_more_leaves_than_solids_leaves_some_unmatched() -> None:
    leaves = [_leaf("A"), _leaf("B"), _leaf("C")]
    solids = [_make_solid(3.0)]
    results = match_parts_to_solids(leaves, solids)
    # Each leaf shows up exactly once in the results.
    leaf_ids = [r.node.product_id for r in results if r.node.product_id in {"A", "B", "C"}]
    assert sorted(leaf_ids) == ["A", "B", "C"]
    unmatched = [r for r in results if r.method == "unmatched"]
    assert len(unmatched) == 2
    for r in unmatched:
        assert r.solid is None
        assert r.confidence == pytest.approx(0.0)


def test_more_solids_than_leaves_yields_unmatched_solid_extras() -> None:
    leaves = [_leaf("A")]
    solids = [_make_solid(3.0), _make_solid(4.0), _make_solid(5.0)]
    results = match_parts_to_solids(leaves, solids)
    extras = [r for r in results if r.method == "unmatched_solid"]
    assert len(extras) == 2
    for r in extras:
        assert r.node.source == "unmatched_solid"
        assert r.solid is not None


def test_empty_inputs_yield_empty_results() -> None:
    assert match_parts_to_solids([], []) == []


def test_every_result_has_non_negative_confidence() -> None:
    leaves = [_leaf("A"), _leaf("B")]
    solids = [_make_solid(3.0), _make_solid(4.0), _make_solid(5.0)]
    results = match_parts_to_solids(leaves, solids)
    assert results, "expected at least one match result"
    for r in results:
        assert isinstance(r, MatchResult)
        assert 0.0 <= r.confidence <= 1.0
