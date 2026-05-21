"""Match parsed leaf parts to ``TopoDS_Solid`` bodies.

The parser yields :class:`StepPart` records and ``geometry_loader.load_solids``
yields a list of solids; this module joins them so every leaf in the assembly
tree has a (possibly empty) geometry attached.

A series of strategies are tried in descending confidence:

1. ``1to1``     - exactly one leaf and one solid.
2. ``ordered``  - equal counts > 1, paired by index.
3. ``quantity`` - fewer leaves than solids: expand leaves by NAUO quantity
                  and pair leaf groups to volume-clustered solids.
4. ``ocaf``     - optional helper from ``parsing.occt_fallback`` that maps
                  XCAF shape labels onto solids by name.
5. ``by_name`` / ``unmatched`` - greedy fall-back when nothing else applies.

Every leaf produces exactly one :class:`MatchResult`. Solids without a leaf
counterpart yield extra results carrying a synthetic ``AssemblyNode`` so the
caller can still see the orphaned geometry.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..config.classification_variables import (
    MATCH_CONFIDENCE_BY_NAME,
    MATCH_CONFIDENCE_OCAF,
    MATCH_CONFIDENCE_ONE_TO_ONE,
    MATCH_CONFIDENCE_ORDERED,
    MATCH_CONFIDENCE_QUANTITY,
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


def _solid_volume(solid: object) -> float | None:
    """Best-effort volume of a ``TopoDS_Solid``. ``None`` when OCP is absent.

    Used only to cluster geometrically-identical solids; precision beyond a
    coarse bucket is irrelevant, so any failure simply opts the solid out of
    the geometry-aware strategy.
    """
    try:
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
    except Exception:  # pragma: no cover - OCP optional at import time
        return None
    try:
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(solid, props)
        return abs(float(props.Mass()))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("volume probe failed: %s", exc)
        return None


def _expand_by_quantity(leaves: list[AssemblyNode]) -> list[AssemblyNode]:
    """Repeat each leaf ``quantity`` times so one instance exists per solid.

    NAUO multiplicity collapses repeated component instances onto a single
    leaf carrying ``quantity``; geometry loaders explode every instance into
    its own solid. Expanding here re-aligns the two cardinalities.
    """
    out: list[AssemblyNode] = []
    for leaf in leaves:
        for _ in range(max(int(leaf.quantity), 1)):
            out.append(leaf)
    return out


def _match_by_quantity_clusters(
    leaves: list[AssemblyNode], solids: list
) -> list[MatchResult] | None:
    """Pair quantity-expanded leaves to volume-clustered solids.

    Returns ``None`` (so the caller falls through to the next strategy) when
    the data does not support an unambiguous assignment: the expanded leaf
    count must equal the solid count, every solid volume must be probeable,
    and the leaf groups (one per distinct ``product_id``) must map onto
    solid volume-clusters by a bijection of cluster sizes.
    """
    instances = _expand_by_quantity(leaves)
    if len(instances) != len(solids) or not solids:
        return None

    volumes: list[float] = []
    for solid in solids:
        vol = _solid_volume(solid)
        if vol is None:
            return None
        volumes.append(vol)

    # Cluster solids by a coarse volume bucket (0.1 mm^3) so floating-point
    # noise between nominally identical bodies does not split a cluster.
    clusters: dict[float, list[int]] = defaultdict(list)
    for idx, vol in enumerate(volumes):
        clusters[round(vol, 1)].append(idx)

    # Group leaves by product_id, preserving first-seen order.
    leaf_groups: dict[str, list[AssemblyNode]] = {}
    for leaf in instances:
        leaf_groups.setdefault(leaf.product_id, []).append(leaf)

    cluster_sizes = sorted(len(idxs) for idxs in clusters.values())
    group_sizes = sorted(len(g) for g in leaf_groups.values())
    if cluster_sizes != group_sizes:
        return None
    # An unambiguous size-bijection requires every group size to be unique.
    if len(set(group_sizes)) != len(group_sizes):
        return None

    clusters_by_size = {len(idxs): idxs for idxs in clusters.values()}
    results: list[MatchResult] = []
    for group in leaf_groups.values():
        solid_idxs = clusters_by_size[len(group)]
        for leaf, solid_idx in zip(group, solid_idxs):
            results.append(
                MatchResult(
                    node=leaf,
                    solid=solids[solid_idx],
                    confidence=MATCH_CONFIDENCE_QUANTITY,
                    method="quantity",
                )
            )
    return results


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

    # Strategy 2b - quantity-aware geometry clustering. When NAUO multiplicity
    # has collapsed repeated instances onto fewer leaves than there are solids,
    # expand by quantity and pair the leaf groups to volume-clustered solids.
    if 0 < n_leaves < n_solids:
        quantity_results = _match_by_quantity_clusters(leaves, solids)
        if quantity_results is not None:
            return quantity_results

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
