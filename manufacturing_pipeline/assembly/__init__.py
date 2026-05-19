"""Assembly graph traversal and multi-component aggregation."""

from .graph import (
    ROOT_ID,
    AssemblyNode,
    aggregate_quantities,
    build_assembly_graph,
    flatten,
    iter_leaves,
)
from .matcher import MatchResult, match_parts_to_solids

__all__ = [
    "AssemblyNode",
    "MatchResult",
    "ROOT_ID",
    "aggregate_quantities",
    "build_assembly_graph",
    "flatten",
    "iter_leaves",
    "match_parts_to_solids",
]
