"""Grouped BOM (Bill of Materials) rollup for assembly manifests.

The on-disk XML manifest emits one ``<part>`` per solid instance (matching the
STEP NAUO multiplicity stored on :class:`PartManifestEntry.quantity`). This
module provides a *presentation-layer* rollup that collapses identical parts
into a single :class:`GroupedPartEntry` with a summed quantity, matching the
way CAM tools such as AutoPOL display a BOM.

The structural format (XML schema, :class:`AssemblyManifest`) is not changed -
this is a pure read-side transform.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from manufacturing_pipeline.manifest import AssemblyManifest, PartManifestEntry

__all__ = ["GroupedPartEntry", "group_bom"]


@dataclass
class GroupedPartEntry:
    """A rollup of one-or-more identical :class:`PartManifestEntry` instances.

    ``representative`` is the first entry encountered for this group (used as
    the source of classification / features / flat-pattern metadata).
    ``quantity`` is the sum of every instance's ``quantity`` field (so for an
    assembly that has the same part listed twice with NAUO multiplicity 3 + 2,
    the group quantity is 5).
    ``instance_ids`` carries the ``product_id`` of every instance that
    contributed, preserving the original order.
    ``instance_names`` is the corresponding list of human-readable names.
    """

    representative: PartManifestEntry
    quantity: int
    instance_ids: list[str] = field(default_factory=list)
    instance_names: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Grouping key
# ---------------------------------------------------------------------------


def _flat_dxf_hash(path_str: str) -> str | None:
    """Hash the *contents* of a flat DXF, not just the path.

    Two parts that point at the same DXF on disk are obviously identical; two
    parts that point at different DXFs with byte-identical contents are also
    identical (e.g. copied during corpus build). Returns ``None`` if the file
    cannot be read - the caller then falls back to the geometric signature.
    """
    p = Path(path_str)
    try:
        data = p.read_bytes()
    except (OSError, FileNotFoundError):
        return None
    return hashlib.sha256(data).hexdigest()


def _geometric_signature(entry: PartManifestEntry) -> tuple[object, ...]:
    """Fallback signature when no DXF is available.

    Uses bbox dimensions and volume rounded to 0.1 mm / 0.1 mm^3 - tight
    enough to separate distinct geometries, loose enough to absorb the kind
    of floating-point jitter that creeps in when the same part is exported
    twice from a CAD system.
    """
    f = entry.features
    if f is None:
        return ("no-features",)
    bb = tuple(round(d, 1) for d in f.bbox_dims_sorted)
    vol = round(f.volume, 1)
    return ("geom", bb, vol)


def _grouping_key(entry: PartManifestEntry) -> tuple[object, ...]:
    """The "identical part" key.

    Two parts collapse into one BOM row iff they share:

    1. classification label (``"plaat"``, ``"profiel"``, ``"buis"`` ...);
    2. profile-match designation if either has one (so an HEA-100 and an
       HEA-120 never merge, and a profile never merges with a plate that
       happens to share dimensions);
    3. flat-pattern geometry signature: the sha256 of the flat DXF file
       contents when available, otherwise a feature-vector tuple of
       ``(bbox_sorted, volume)`` rounded to 0.1 mm.

    The key is intentionally conservative - if any of these differ, the
    parts stay in separate rows.
    """
    cls_label = entry.classification.label if entry.classification else ""

    profile_designation: str | None = None
    if entry.profile_match is not None:
        profile_designation = getattr(entry.profile_match, "designation", None) or None

    geom_key: tuple[object, ...]
    if entry.flat_dxf_path:
        h = _flat_dxf_hash(entry.flat_dxf_path)
        if h is not None:
            geom_key = ("dxf", h)
        else:
            geom_key = _geometric_signature(entry)
    else:
        geom_key = _geometric_signature(entry)

    return (cls_label, profile_designation, geom_key)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def group_bom(manifest: AssemblyManifest) -> list[GroupedPartEntry]:
    """Collapse a per-instance manifest into a grouped BOM view.

    The returned list preserves the order in which each group first appears
    in ``manifest.parts`` - so a stable, deterministic ordering that matches
    the assembly traversal order, not an arbitrary hash order.

    The input ``manifest`` is not modified; the returned :class:`GroupedPartEntry`
    objects hold references (not copies) to the original entries.
    """
    groups: dict[tuple[object, ...], GroupedPartEntry] = {}
    for entry in manifest.parts:
        key = _grouping_key(entry)
        existing = groups.get(key)
        if existing is None:
            groups[key] = GroupedPartEntry(
                representative=entry,
                quantity=int(entry.quantity),
                instance_ids=[entry.part.product_id],
                instance_names=[entry.part.name],
            )
        else:
            existing.quantity += int(entry.quantity)
            existing.instance_ids.append(entry.part.product_id)
            existing.instance_names.append(entry.part.name)
    return list(groups.values())
