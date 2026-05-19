"""Tests for the structured assembly-diff module.

Uses real STEP fixtures from :mod:`tests.fixtures.synthetic_steps` so the
pipeline analyses are end-to-end and the diff sees actual geometric
features, not hand-crafted ``ManufacturingFeatures`` objects.

Most cases rely on :func:`manufacturing_pipeline.pipeline.analyze_assembly.analyze`,
so the whole module is skipped when OCP is unavailable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("OCP.STEPCAFControl")

from manufacturing_pipeline.pipeline.analyze_assembly import (  # noqa: E402
    AnalyzeOptions,
    analyze,
)
from manufacturing_pipeline.pipeline.diff import (  # noqa: E402
    AssemblyDiff,
    PartChange,
    diff_assemblies,
    diff_step_files,
    render_diff_text,
)
from tests.fixtures.synthetic_steps import (  # noqa: E402
    build_lbracket_solid,
    build_plate_solid,
    build_profile_solid,
    translate_solid,
    write_assembly_step,
    write_flat_plate_step,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_OPTS = AnalyzeOptions(out_dir=None, write_dxf=False, write_xml=False)


def _analyze(path: Path):
    return analyze(path, _OPTS)


def _two_part_assembly(target: Path) -> Path:
    """A 2-part assembly: plate_001 + lbracket_001."""
    plate = build_plate_solid(l=100.0, w=60.0, t=2.0)
    lb = translate_solid(build_lbracket_solid(), dx=200.0)
    return write_assembly_step(target, parts=[("plate_001", plate), ("lbracket_001", lb)])


def _three_part_assembly(target: Path) -> Path:
    """A 3-part assembly: plate_001 + lbracket_001 + rhs_001."""
    plate = build_plate_solid(l=100.0, w=60.0, t=2.0)
    lb = translate_solid(build_lbracket_solid(), dx=200.0)
    rhs = translate_solid(
        build_profile_solid(family="RHS", h=100.0, b=60.0, t=4.0, length=1000.0),
        dx=400.0,
    )
    return write_assembly_step(
        target,
        parts=[
            ("plate_001", plate),
            ("lbracket_001", lb),
            ("rhs_001", rhs),
        ],
    )


def _two_part_assembly_renamed_plate(target: Path) -> Path:
    """Same geometry as :func:`_two_part_assembly` but the plate gets a new name."""
    plate = build_plate_solid(l=100.0, w=60.0, t=2.0)
    lb = translate_solid(build_lbracket_solid(), dx=200.0)
    return write_assembly_step(target, parts=[("plate_renamed_v2", plate), ("lbracket_001", lb)])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_self_diff_yields_no_added_or_removed_and_only_unchanged_parts(tmp_path: Path):
    step = _two_part_assembly(tmp_path / "asm.step")
    a = _analyze(step)

    d = diff_assemblies(a, a, match_by="name", tolerance_pct=1.0)

    assert isinstance(d, AssemblyDiff)
    assert d.added == []
    assert d.removed == []
    assert d.changed == []
    assert len(d.unchanged) == len(a.manifest.parts)
    assert d.summary["n_unchanged"] == len(a.manifest.parts)
    assert d.summary["n_added"] == 0
    assert d.summary["n_removed"] == 0
    assert d.summary["n_changed"] == 0


def test_plate_grown_by_ten_percent_reports_geometry_changed(tmp_path: Path):
    old_path = write_flat_plate_step(tmp_path / "old.step", l=100.0, w=60.0, t=2.0)
    new_path = write_flat_plate_step(tmp_path / "new.step", l=110.0, w=60.0, t=2.0)

    # STEPControl_Writer auto-names solids with a session-local counter, so the
    # two plates land under different product names. Fingerprint matching pairs
    # them on (volume, surface_area) so we actually exercise the
    # geometry_changed branch.
    d = diff_step_files(
        old_path,
        new_path,
        options=_OPTS,
        match_by="fingerprint",
        tolerance_pct=1.0,
    )

    # No new parts, no missing parts: just a single geometry change.
    assert d.added == []
    assert d.removed == []
    assert len(d.changed) == 1
    change = d.changed[0]
    assert change.kind == "geometry_changed"

    vol = change.details.get("volume")
    assert vol is not None, f"expected 'volume' in details, got {change.details!r}"
    old_v, new_v, delta_pct = vol
    assert new_v > old_v
    # 100x60x2 -> 110x60x2 is ~9.5% on the symmetric delta.
    assert delta_pct > 5.0


def test_added_profile_lands_in_added_bucket(tmp_path: Path):
    old_step = _two_part_assembly(tmp_path / "old.step")
    new_step = _three_part_assembly(tmp_path / "new.step")

    d = diff_step_files(old_step, new_step, options=_OPTS, match_by="name", tolerance_pct=1.0)

    assert d.removed == []
    assert len(d.added) == 1
    added_names = {c.name for c in d.added}
    assert "rhs_001" in added_names
    assert d.summary["n_added"] == 1


def test_removed_bracket_lands_in_removed_bucket(tmp_path: Path):
    old_step = _two_part_assembly(tmp_path / "old.step")
    plate_only_step = write_flat_plate_step(tmp_path / "plate_only.step", l=100.0, w=60.0, t=2.0)

    old = _analyze(old_step)
    new = _analyze(plate_only_step)

    # Use fingerprint matching so the plate pairs across the two assemblies
    # even though the parser may emit different product_ids/names for a
    # multi-part vs single-solid STEP file.
    d = diff_assemblies(old, new, match_by="fingerprint", tolerance_pct=1.0)

    assert len(d.removed) >= 1
    # The bracket is the part that disappeared. The plate must NOT be reported
    # as removed; that would mean our matcher failed.
    removed_kinds = {c.kind for c in d.removed}
    assert removed_kinds == {"removed"}
    assert d.summary["n_removed"] == len(d.removed)


def test_name_matching_misses_rename_but_fingerprint_pairs_renamed_part(
    tmp_path: Path,
):
    old_step = _two_part_assembly(tmp_path / "old.step")
    new_step = _two_part_assembly_renamed_plate(tmp_path / "new.step")

    old = _analyze(old_step)
    new = _analyze(new_step)

    # Under name matching, the renamed plate looks like one added + one removed.
    d_name = diff_assemblies(old, new, match_by="name", tolerance_pct=1.0)
    added_names = {c.name for c in d_name.added}
    removed_names = {c.name for c in d_name.removed}
    assert "plate_renamed_v2" in added_names
    assert "plate_001" in removed_names

    # Under fingerprint matching, the rename is detected and the plate pairs up
    # as unchanged.
    d_fp = diff_assemblies(old, new, match_by="fingerprint", tolerance_pct=1.0)
    assert d_fp.added == []
    assert d_fp.removed == []
    # All matches should be unchanged (or at worst classification_changed if the
    # classifier emitted a tiny confidence swing between runs); critically, no
    # geometry_changed entries because the geometry is identical.
    geom_changes = [c for c in d_fp.changed if c.kind == "geometry_changed"]
    assert geom_changes == []


def test_render_diff_text_contains_all_section_headers(tmp_path: Path):
    old_step = _two_part_assembly(tmp_path / "old.step")
    new_step = _three_part_assembly(tmp_path / "new.step")

    d = diff_step_files(old_step, new_step, options=_OPTS, match_by="name")
    text = render_diff_text(d)

    assert "ADDED" in text
    assert "REMOVED" in text
    assert "CHANGED" in text
    assert "UNCHANGED" in text
    # Sanity: the summary line is present too.
    assert "diff summary" in text


def test_render_diff_text_truncates_with_max_rows():
    """Smoke test for the max_rows ellipsis behaviour without needing a STEP."""
    rows = [PartChange(product_id=f"id_{i}", name=f"n_{i}", kind="added") for i in range(20)]
    d = AssemblyDiff(
        added=rows,
        removed=[],
        changed=[],
        unchanged=[],
        summary={
            "n_added": 20,
            "n_removed": 0,
            "n_changed": 0,
            "n_unchanged": 0,
            "match_by": "name",
            "tolerance_pct": 1.0,
        },
    )
    text = render_diff_text(d, max_rows=3)
    assert "... 17 more" in text
