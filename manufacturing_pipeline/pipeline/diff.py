"""Structured diff between two STEP assembly analyses.

Given two :class:`AssemblyManifest` instances (or two :class:`AnalyzeResult`s,
or directly two STEP files), :func:`diff_assemblies` produces an
:class:`AssemblyDiff` summarising which parts were added, removed, geometrically
changed, reclassified, requantified, or left unchanged.

Pure-Python; depends only on the manifest dataclasses already in
``manufacturing_pipeline.io.xml_writer`` plus optional integration with
:func:`manufacturing_pipeline.pipeline.analyze_assembly.analyze` via
:func:`diff_step_files`.

Matching strategies (selectable via ``match_by``):

* ``"product_id"`` - exact ID equality. Useful when IDs are stable.
* ``"name"`` - normalised name equality (lowercase + non-word -> underscore).
* ``"fingerprint"`` - greedy minimum L1 distance over
  ``(round(volume, -2), round(surface_area, -2))``. Catches renames.

For each matched pair we emit one of:
``geometry_changed``, ``classification_changed``, ``quantity_changed``,
``unchanged``. ``details`` contains at most the 5 fields with the largest
relative delta.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..io.xml_writer import AssemblyManifest, PartManifestEntry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .analyze_assembly import AnalyzeOptions, AnalyzeResult


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PartChange:
    """A single per-part diff entry.

    ``kind`` is one of:
      ``"added"``, ``"removed"``, ``"geometry_changed"``,
      ``"quantity_changed"``, ``"classification_changed"``, ``"unchanged"``.

    ``details`` carries field-level deltas, e.g.
    ``{"volume": (old, new, delta_pct), ...}``. For added/removed parts it
    may also contain ``{"volume": (None, new, None)}`` style placeholders.
    """

    product_id: str
    name: str
    kind: str
    details: dict = field(default_factory=dict)


@dataclass
class AssemblyDiff:
    """Result of :func:`diff_assemblies`.

    ``unchanged`` is populated even when ``kind == "unchanged"`` so that
    callers can render the full picture; ``summary`` always reflects the
    counts of every bucket.
    """

    added: list[PartChange] = field(default_factory=list)
    removed: list[PartChange] = field(default_factory=list)
    changed: list[PartChange] = field(default_factory=list)
    unchanged: list[PartChange] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_name(name: str) -> str:
    """Lowercase + non-word -> underscore + strip leading/trailing underscores."""
    return re.sub(r"\W+", "_", (name or "").lower()).strip("_")


def _as_manifest(obj: object) -> AssemblyManifest:
    """Accept either an :class:`AssemblyManifest` or an ``AnalyzeResult``."""
    if isinstance(obj, AssemblyManifest):
        return obj
    manifest = getattr(obj, "manifest", None)
    if isinstance(manifest, AssemblyManifest):
        return manifest
    raise TypeError(f"expected AssemblyManifest or AnalyzeResult, got {type(obj).__name__}")


def _delta_pct(old: float, new: float) -> float:
    """Symmetric relative delta as a percentage.

    Returns 0.0 when both sides are zero; uses ``max(|old|, |new|)`` as the
    denominator so a 0 -> X transition does not blow up.
    """
    denom = max(abs(float(old)), abs(float(new)))
    if denom == 0.0:
        return 0.0
    return abs(float(new) - float(old)) / denom * 100.0


def _geometric_fields(entry: PartManifestEntry) -> dict:
    """Pull the comparable geometric fields out of a manifest entry."""
    f = entry.features
    if f is None:
        return {
            "volume": 0.0,
            "surface_area": 0.0,
            "bbox_l": 0.0,
            "bbox_w": 0.0,
            "bbox_t": 0.0,
        }
    bb = tuple(f.bbox_dims_sorted) + (0.0, 0.0, 0.0)
    return {
        "volume": float(f.volume),
        "surface_area": float(f.surface_area),
        "bbox_l": float(bb[0]),
        "bbox_w": float(bb[1]),
        "bbox_t": float(bb[2]),
    }


def _fingerprint(entry: PartManifestEntry) -> tuple:
    """Coarse-grained signature used by ``match_by='fingerprint'``."""
    g = _geometric_fields(entry)
    return (round(g["volume"], -2), round(g["surface_area"], -2))


def _l1(a: tuple, b: tuple) -> float:
    return sum(abs(float(x) - float(y)) for x, y in zip(a, b))


def _top_n_deltas(diffs: dict, n: int = 5) -> dict:
    """Keep at most the ``n`` field-level deltas with the largest |delta_pct|.

    ``diffs`` maps field-name -> ``(old, new, delta_pct)``.
    """
    items = sorted(
        diffs.items(),
        key=lambda kv: abs(float(kv[1][2] or 0.0)),
        reverse=True,
    )
    return dict(items[:n])


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _key_for(entry: PartManifestEntry, match_by: str) -> str:
    if match_by == "product_id":
        return entry.part.product_id
    if match_by == "name":
        return _normalise_name(entry.part.name) or entry.part.product_id
    if match_by == "fingerprint":
        # Fingerprint pairing is handled separately - this branch is unused but
        # we still return a deterministic string so callers can debug.
        return repr(_fingerprint(entry))
    raise ValueError(f"unknown match_by: {match_by!r}")


def _pair_by_key(
    old_entries: list[PartManifestEntry],
    new_entries: list[PartManifestEntry],
    match_by: str,
) -> tuple[list[tuple], list, list]:
    """Pair entries by string key. Returns (pairs, unmatched_old, unmatched_new)."""
    old_by_key: dict[str, list[PartManifestEntry]] = {}
    for e in old_entries:
        old_by_key.setdefault(_key_for(e, match_by), []).append(e)

    pairs: list[tuple[PartManifestEntry, PartManifestEntry]] = []
    unmatched_new: list[PartManifestEntry] = []
    for new in new_entries:
        key = _key_for(new, match_by)
        bucket = old_by_key.get(key)
        if bucket:
            pairs.append((bucket.pop(0), new))
        else:
            unmatched_new.append(new)

    unmatched_old: list[PartManifestEntry] = []
    for bucket in old_by_key.values():
        unmatched_old.extend(bucket)

    return pairs, unmatched_old, unmatched_new


def _pair_by_fingerprint(
    old_entries: list[PartManifestEntry],
    new_entries: list[PartManifestEntry],
    tolerance_pct: float,
) -> tuple[list[tuple], list, list]:
    """Greedy minimum-L1 fingerprint pairing.

    For every candidate (old, new) pair within ``tolerance_pct`` on volume,
    compute the L1 distance over the fingerprint tuple and greedily consume
    the smallest distances first. Leftovers become unmatched.
    """
    old_fps = [(e, _fingerprint(e)) for e in old_entries]
    new_fps = [(e, _fingerprint(e)) for e in new_entries]

    candidates: list[tuple[float, int, int]] = []
    for i, (_old, old_fp) in enumerate(old_fps):
        for j, (_new, new_fp) in enumerate(new_fps):
            # Reject pairs whose volume relative-delta exceeds tolerance.
            if _delta_pct(old_fp[0], new_fp[0]) > max(tolerance_pct * 5.0, 25.0):
                # Allow a generous-but-finite ceiling so renames-with-small-tweak
                # still match, but a 10x volume swing does not.
                continue
            candidates.append((_l1(old_fp, new_fp), i, j))

    candidates.sort()

    used_old: set[int] = set()
    used_new: set[int] = set()
    pairs: list[tuple[PartManifestEntry, PartManifestEntry]] = []
    for _dist, i, j in candidates:
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        pairs.append((old_fps[i][0], new_fps[j][0]))

    unmatched_old = [e for i, (e, _) in enumerate(old_fps) if i not in used_old]
    unmatched_new = [e for j, (e, _) in enumerate(new_fps) if j not in used_new]
    return pairs, unmatched_old, unmatched_new


# ---------------------------------------------------------------------------
# Per-pair classification
# ---------------------------------------------------------------------------


def _classify_pair(
    old: PartManifestEntry,
    new: PartManifestEntry,
    tolerance_pct: float,
) -> PartChange:
    """Compare a matched pair and return a single :class:`PartChange`."""
    g_old = _geometric_fields(old)
    g_new = _geometric_fields(new)

    field_deltas: dict[str, tuple] = {}
    for name in g_old:
        dpct = _delta_pct(g_old[name], g_new[name])
        field_deltas[name] = (g_old[name], g_new[name], dpct)

    geom_changed = any(d[2] > tolerance_pct for d in field_deltas.values())

    old_lbl = old.classification.label if old.classification else ""
    new_lbl = new.classification.label if new.classification else ""
    old_conf = float(old.classification.confidence) if old.classification else 0.0
    new_conf = float(new.classification.confidence) if new.classification else 0.0
    class_changed = (old_lbl != new_lbl) or (abs(new_conf - old_conf) > 0.1)

    qty_changed = int(old.quantity) != int(new.quantity)

    # Pick the "primary" kind in a defined priority order. Geometry beats
    # classification beats quantity beats unchanged.
    if geom_changed:
        kind = "geometry_changed"
    elif class_changed:
        kind = "classification_changed"
    elif qty_changed:
        kind = "quantity_changed"
    else:
        kind = "unchanged"

    details: dict = {}
    if geom_changed:
        details.update(_top_n_deltas(field_deltas))
    if class_changed:
        details["label"] = (old_lbl, new_lbl, None)
        details["confidence"] = (old_conf, new_conf, abs(new_conf - old_conf) * 100.0)
    if qty_changed:
        details["quantity"] = (int(old.quantity), int(new.quantity), None)

    return PartChange(
        product_id=new.part.product_id or old.part.product_id,
        name=new.part.name or old.part.name,
        kind=kind,
        details=details,
    )


def _change_for_added(entry: PartManifestEntry) -> PartChange:
    g = _geometric_fields(entry)
    details = {k: (None, v, None) for k, v in g.items()}
    return PartChange(
        product_id=entry.part.product_id,
        name=entry.part.name,
        kind="added",
        details=details,
    )


def _change_for_removed(entry: PartManifestEntry) -> PartChange:
    g = _geometric_fields(entry)
    details = {k: (v, None, None) for k, v in g.items()}
    return PartChange(
        product_id=entry.part.product_id,
        name=entry.part.name,
        kind="removed",
        details=details,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def diff_assemblies(
    old: AssemblyManifest | AnalyzeResult,
    new: AssemblyManifest | AnalyzeResult,
    *,
    tolerance_pct: float = 1.0,
    match_by: str = "name",
) -> AssemblyDiff:
    """Compare two assembly analyses and produce a structured diff."""
    if match_by not in ("name", "product_id", "fingerprint"):
        raise ValueError(
            f"match_by must be one of 'name', 'product_id', 'fingerprint'; got {match_by!r}"
        )

    old_m = _as_manifest(old)
    new_m = _as_manifest(new)

    old_entries = list(old_m.parts)
    new_entries = list(new_m.parts)

    if match_by == "fingerprint":
        pairs, unmatched_old, unmatched_new = _pair_by_fingerprint(
            old_entries, new_entries, tolerance_pct=tolerance_pct
        )
    else:
        pairs, unmatched_old, unmatched_new = _pair_by_key(old_entries, new_entries, match_by)

    changed: list[PartChange] = []
    unchanged: list[PartChange] = []
    for old_e, new_e in pairs:
        change = _classify_pair(old_e, new_e, tolerance_pct=tolerance_pct)
        if change.kind == "unchanged":
            unchanged.append(change)
        else:
            changed.append(change)

    added = [_change_for_added(e) for e in unmatched_new]
    removed = [_change_for_removed(e) for e in unmatched_old]

    summary = {
        "n_added": len(added),
        "n_removed": len(removed),
        "n_changed": len(changed),
        "n_unchanged": len(unchanged),
        "match_by": match_by,
        "tolerance_pct": float(tolerance_pct),
    }

    return AssemblyDiff(
        added=added,
        removed=removed,
        changed=changed,
        unchanged=unchanged,
        summary=summary,
    )


def diff_step_files(
    old_path: str | Path,
    new_path: str | Path,
    *,
    options: AnalyzeOptions | None = None,
    **diff_kwargs,
) -> AssemblyDiff:
    """Analyze both STEP files and diff the resulting manifests."""
    from .analyze_assembly import AnalyzeOptions, analyze

    opts = (
        options
        if options is not None
        else AnalyzeOptions(out_dir=None, write_dxf=False, write_xml=False)
    )
    old_result = analyze(old_path, opts)
    new_result = analyze(new_path, opts)
    return diff_assemblies(old_result, new_result, **diff_kwargs)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_value(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3g}"
    return str(v)


def _render_section(title: str, rows: list[PartChange], max_rows: int) -> list[str]:
    """Render a single section (ADDED / REMOVED / CHANGED / UNCHANGED)."""
    lines = [f"{title} ({len(rows)})"]
    if not rows:
        lines.append("  (none)")
        return lines
    shown = rows[:max_rows]
    for row in shown:
        head = f"  {row.kind:<22}  {row.name or row.product_id}"
        if row.product_id and row.product_id != row.name:
            head += f"  [{row.product_id}]"
        lines.append(head)
        for key, val in row.details.items():
            if not isinstance(val, tuple) or len(val) != 3:
                continue
            old_v, new_v, dpct = val
            dpct_str = f" ({dpct:.1f}%)" if isinstance(dpct, (int, float)) else ""
            lines.append(
                f"      {key:<14} {_format_value(old_v):>10} -> "
                f"{_format_value(new_v):>10}{dpct_str}"
            )
    if len(rows) > max_rows:
        lines.append(f"  ... {len(rows) - max_rows} more")
    return lines


def render_diff_text(d: AssemblyDiff, *, max_rows: int = 50) -> str:
    """ASCII summary table for terminal output.

    Always emits the four canonical section headers in upper-case so callers
    (and tests) can grep for them: ``ADDED``, ``REMOVED``, ``CHANGED``,
    ``UNCHANGED``.
    """
    out: list[str] = []
    s = d.summary
    out.append(
        f"diff summary: added={s.get('n_added', 0)} "
        f"removed={s.get('n_removed', 0)} "
        f"changed={s.get('n_changed', 0)} "
        f"unchanged={s.get('n_unchanged', 0)} "
        f"(match_by={s.get('match_by', 'name')}, "
        f"tol={s.get('tolerance_pct', 1.0):.2f}%)"
    )
    out.append("")
    out.extend(_render_section("ADDED", d.added, max_rows))
    out.append("")
    out.extend(_render_section("REMOVED", d.removed, max_rows))
    out.append("")
    out.extend(_render_section("CHANGED", d.changed, max_rows))
    out.append("")
    out.extend(_render_section("UNCHANGED", d.unchanged, max_rows))
    return "\n".join(out)


__all__ = [
    "AssemblyDiff",
    "PartChange",
    "diff_assemblies",
    "diff_step_files",
    "render_diff_text",
]
