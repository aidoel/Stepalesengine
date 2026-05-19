"""Assembly graph construction from parsed StepPart records.

Builds a tree of :class:`AssemblyNode` instances from the flat
``list[StepPart]`` produced by :mod:`manufacturing_pipeline.parsing.step_parser`.
The tree is rooted in a synthetic node so callers can always start traversal
from a single entry point, even when the file contains multiple top-level
products.

Robustness goals (mirroring the cascade in §3 of
``docs/ROBUST_FEATURE_DETECTION_PLAN.md``):

* Tolerate duplicate ``product_id`` values (keep the first occurrence).
* Tolerate cyclic ``children`` references without infinite recursion.
* Tolerate dangling child references (id present in ``children`` but absent
  from the part list) by synthesising a placeholder node.
* When no NAUO data is present, every part hangs directly off the virtual
  root as a leaf.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field

from ..parsing.types import StepPart

logger = logging.getLogger(__name__)

# Reserved product_id used for the synthetic root node. Chosen so it cannot
# collide with a real STEP product identifier.
ROOT_ID = "__ROOT__"


@dataclass
class AssemblyNode:
    """A single node in the assembly tree."""

    product_id: str
    name: str
    description: str = ""
    source: str = "unknown"
    children: list[AssemblyNode] = field(default_factory=list)
    parent_id: str | None = None
    quantity: int = 1
    is_leaf: bool = True
    depth: int = 0


def _dedupe(parts: list[StepPart]) -> list[StepPart]:
    """Return ``parts`` with duplicate ``product_id`` values removed.

    The first occurrence wins. This mirrors how the parsing strategies
    treat repeated definitions, where the earliest record is the most
    likely to carry the canonical name.
    """
    seen: set[str] = set()
    out: list[StepPart] = []
    for part in parts:
        if part.product_id in seen:
            logger.debug("duplicate product_id %s deduped", part.product_id)
            continue
        seen.add(part.product_id)
        out.append(part)
    return out


def _top_level_ids(parts: list[StepPart]) -> list[str]:
    """Identify product_ids that no other part lists as a child."""
    referenced: set[str] = set()
    for part in parts:
        for child in part.children:
            referenced.add(child)
    tops = [p.product_id for p in parts if p.product_id not in referenced]
    if not tops:
        # Cyclic graph or self-references at the top: fall back to all ids.
        tops = [p.product_id for p in parts]
    return tops


def build_assembly_graph(parts: list[StepPart]) -> AssemblyNode:
    """Build a tree rooted at a virtual node containing all top-level parts.

    Parameters
    ----------
    parts:
        Flat list of parsed :class:`StepPart` records. May be empty.

    Returns
    -------
    AssemblyNode
        The synthetic root. Its ``children`` are the top-level products in
        the input. Every reachable node carries its depth (root=0), its
        ``parent_id`` (``None`` only on the root), and ``is_leaf`` set to
        True iff it has no children in the parsed records.
    """
    root = AssemblyNode(
        product_id=ROOT_ID,
        name="root",
        description="",
        source="root",
        depth=0,
        is_leaf=True,
        parent_id=None,
    )

    if not parts:
        return root

    deduped = _dedupe(parts)
    by_id: dict[str, StepPart] = {p.product_id: p for p in deduped}

    tops = _top_level_ids(deduped)

    def visit(pid: str, parent_id: str | None, depth: int, on_path: set[str]) -> AssemblyNode:
        part = by_id.get(pid)
        if part is None:
            # Dangling child reference: synthesise a placeholder so the tree
            # remains traversable but the missing data is visible to callers.
            return AssemblyNode(
                product_id=pid,
                name=pid,
                description="",
                source="dangling",
                parent_id=parent_id,
                depth=depth,
                is_leaf=True,
            )

        if pid in on_path:
            logger.warning("cyclic reference detected at product_id=%s; pruning", pid)
            return AssemblyNode(
                product_id=pid,
                name=part.name or pid,
                description=part.description,
                source=part.source,
                parent_id=parent_id,
                depth=depth,
                is_leaf=True,
            )

        # Count child occurrences to capture NAUO multiplicity (same id listed
        # more than once means more than one instance under this parent).
        child_counts = Counter(part.children)
        unique_children = list(child_counts.keys())

        node = AssemblyNode(
            product_id=pid,
            name=part.name or pid,
            description=part.description,
            source=part.source,
            parent_id=parent_id,
            depth=depth,
            is_leaf=not unique_children,
        )

        next_on_path = on_path | {pid}
        for child_id in unique_children:
            child_node = visit(child_id, pid, depth + 1, next_on_path)
            child_node.quantity = child_counts[child_id]
            node.children.append(child_node)
        return node

    for top in tops:
        child = visit(top, ROOT_ID, 1, set())
        root.children.append(child)

    root.is_leaf = not root.children
    return root


def iter_leaves(root: AssemblyNode) -> Iterator[AssemblyNode]:
    """Yield every leaf in stable order (depth-first, children sorted by name).

    The synthetic root itself is never yielded. If the root has no children
    the iterator is empty.
    """

    def walk(node: AssemblyNode) -> Iterator[AssemblyNode]:
        if node.is_leaf and node.product_id != ROOT_ID:
            yield node
            return
        for child in sorted(node.children, key=lambda n: (n.name or "", n.product_id)):
            yield from walk(child)

    yield from walk(root)


def aggregate_quantities(root: AssemblyNode) -> dict[str, int]:
    """Total instance count per ``product_id`` across the whole assembly.

    A node's contribution is ``quantity * parent_multiplier`` so quantities
    propagate down sub-assemblies.
    """
    counts: dict[str, int] = {}

    def walk(node: AssemblyNode, multiplier: int) -> None:
        if node.product_id != ROOT_ID:
            counts[node.product_id] = counts.get(node.product_id, 0) + multiplier
        for child in node.children:
            walk(child, multiplier * max(child.quantity, 1))

    walk(root, 1)
    return counts


def flatten(root: AssemblyNode) -> list[AssemblyNode]:
    """Pre-order list of every node in the tree, root first."""
    out: list[AssemblyNode] = []

    def walk(node: AssemblyNode) -> None:
        out.append(node)
        for child in node.children:
            walk(child)

    walk(root)
    return out


__all__ = [
    "AssemblyNode",
    "ROOT_ID",
    "aggregate_quantities",
    "build_assembly_graph",
    "flatten",
    "iter_leaves",
]
