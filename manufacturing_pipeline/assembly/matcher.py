"""Match parsed leaf parts to ``TopoDS_Solid`` bodies.

The parser yields :class:`StepPart` records and ``geometry_loader.load_solids``
yields a list of solids; this module joins them so every leaf in the assembly
tree has a (possibly empty) geometry attached.

A series of strategies are tried in descending confidence:

1. ``1to1``     - exactly one leaf and one solid.
2. ``ordered``  - equal counts > 1, paired by index.
3. ``ocaf``     - optional helper from ``parsing.occt_fallback`` that maps
                  XCAF shape labels onto solids by name.
4. ``by_name`` / ``unmatched`` - greedy fall-back when nothing else applies.

Every leaf produces exactly one :class:`MatchResult`. Solids without a leaf
counterpart yield extra results carrying a synthetic ``AssemblyNode`` so the
caller can still see the orphaned geometry.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..config.classification_variables import (
    MATCH_CONFIDENCE_BY_NAME,
    MATCH_CONFIDENCE_OCAF,
    MATCH_CONFIDENCE_ONE_TO_ONE,
    MATCH_CONFIDENCE_ORDERED,
    MATCH_CONFIDENCE_UNMATCHED_SOLID,
)
from .graph import AssemblyNode

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    """A single leaf-to-solid pairing with provenance for downstream BOMs."""

    node: AssemblyNode
    solid: object | None
    confidence: float
    method: str


def _normalise(name: str) -> str:
    """Lower-case, alnum-only canonical form for fuzzy name comparison."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _synthetic_solid_node(index: int) -> AssemblyNode:
    """Build a placeholder node for a solid that has no parsed leaf."""
    return AssemblyNode(
        product_id=f"solid_{index}",
        name=f"solid_{index}",
        description="",
        source="unmatched_solid",
        is_leaf=True,
        depth=1,
    )


def _try_ocaf_labels(step_path: str | Path) -> dict[str, object]:
    """Try to recover an OCAF ``name -> shape`` mapping.

    This is wired as an optional hook: if the helper exists in
    ``parsing.occt_fallback`` (a future refinement), we use it. Otherwise we
    silently return an empty mapping and let the caller fall through to the
    next strategy.
    """
    try:
        from ..parsing import occt_fallback
    except Exception as exc:  # pragma: no cover - import guarded for safety
        logger.debug("OCAF helper import failed: %s", exc)
        return {}

    helper = getattr(occt_fallback, "occt_shape_labels", None)
    if helper is None:
        return {}

    try:
        mapping = helper(str(step_path))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("OCAF helper raised %s", exc)
        return {}

    if not isinstance(mapping, dict):
        return {}
    return mapping


def match_parts_to_solids(
    leaves: list[AssemblyNode],
    solids: list,
    *,
    step_path: str | Path | None = None,
) -> list[MatchResult]:
    """Best-effort matching between leaf nodes and solids.

    See module docstring for the strategy order. Every input leaf produces
    exactly one :class:`MatchResult`. Solids that no leaf claimed surface as
    extra results so they remain visible to downstream consumers.
    """
    results: list[MatchResult] = []

    n_leaves = len(leaves)
    n_solids = len(solids)

    if n_leaves == 0 and n_solids == 0:
        return results

    # Strategy 1 - exact 1-to-1.
    if n_leaves == 1 and n_solids == 1:
        results.append(
            MatchResult(
                node=leaves[0],
                solid=solids[0],
                confidence=MATCH_CONFIDENCE_ONE_TO_ONE,
                method="1to1",
            )
        )
        return results

    # Strategy 2 - equal cardinality, pair by index.
    if n_leaves > 0 and n_leaves == n_solids:
        for leaf, solid in zip(leaves, solids):
            results.append(
                MatchResult(
                    node=leaf,
                    solid=solid,
                    confidence=MATCH_CONFIDENCE_ORDERED,
                    method="ordered",
                )
            )
        return results

    # Strategy 3 - optional OCAF lookup hook.
    ocaf_map: dict[str, object] = {}
    if step_path is not None:
        ocaf_map = _try_ocaf_labels(step_path)

    used_solids: set = set()
    if ocaf_map:
        # Build normalised lookup from OCAF names to solid indices.
        norm_to_solid_idx: dict[str, int] = {}
        for ocaf_name, ocaf_shape in ocaf_map.items():
            for idx, solid in enumerate(solids):
                if idx in used_solids:
                    continue
                if solid is ocaf_shape:
                    norm_to_solid_idx[_normalise(ocaf_name)] = idx
                    break

        for leaf in leaves:
            key = _normalise(leaf.name)
            match_idx: int | None = norm_to_solid_idx.get(key)
            if match_idx is not None and match_idx not in used_solids:
                used_solids.add(match_idx)
                results.append(
                    MatchResult(
                        node=leaf,
                        solid=solids[match_idx],
                        confidence=MATCH_CONFIDENCE_OCAF,
                        method="ocaf",
                    )
                )
            else:
                results.append(
                    MatchResult(node=leaf, solid=None, confidence=0.0, method="unmatched")
                )
    else:
        # Strategy 4 - greedy by index, marked as ``by_name`` when a leaf
        # exists for that slot, otherwise ``unmatched``.
        for i, leaf in enumerate(leaves):
            if i < n_solids:
                results.append(
                    MatchResult(
                        node=leaf,
                        solid=solids[i],
                        confidence=MATCH_CONFIDENCE_BY_NAME,
                        method="by_name",
                    )
                )
                used_solids.add(i)
            else:
                results.append(
                    MatchResult(node=leaf, solid=None, confidence=0.0, method="unmatched")
                )

    # Extra solids without a leaf get a synthetic node so they stay visible.
    for i, solid in enumerate(solids):
        if i in used_solids:
            continue
        results.append(
            MatchResult(
                node=_synthetic_solid_node(i),
                solid=solid,
                confidence=MATCH_CONFIDENCE_UNMATCHED_SOLID,
                method="unmatched_solid",
            )
        )

    return results


__all__ = ["MatchResult", "match_parts_to_solids"]
