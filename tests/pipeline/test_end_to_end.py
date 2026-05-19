"""End-to-end integration tests for the stepalesengine pipeline.

These tests build a small STEP file from primitives, run the FULL pipeline
(``manufacturing_pipeline.cli.main(['analyze', ...])``), and inspect the
generated manifest XML + DXF artifacts.

The goal is to prove that an arbitrary STEP file can be ingested and produce
a manifest plus (where applicable) a flat-pattern DXF. After the classifier
calibration pass (softmax temperature lowered, dominant signals strengthened)
the obvious plate/profile cases now clear the confidence threshold and so the
DXF-emission assertions run unconditionally; remaining ``xfail`` markers
cover unrelated probe/matcher work (e.g. unfold base-face selection, the
shape-hash tuple/dict mismatch in :mod:`profile_matcher`).

Tag the whole module with ``@pytest.mark.integration`` and skip cleanly when
OCP is unavailable.
"""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import pytest

# Skip the entire module when OCP / STEPCAFControl is missing.
pytest.importorskip("OCP.STEPCAFControl")

import ezdxf

from manufacturing_pipeline.cli import main as cli_main
from manufacturing_pipeline.io.xml_writer import read_xml
from tests.fixtures.synthetic_steps import (
    build_lbracket_solid,
    build_plate_solid,
    build_profile_solid,
    translate_solid,
    write_assembly_step,
    write_flat_plate_step,
    write_lbracket_step,
    write_profile_step,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_pipeline(step_path: Path, out_dir: Path) -> int:
    """Run the CLI ``analyze`` subcommand. Returns the exit code."""
    out_dir.mkdir(parents=True, exist_ok=True)
    return cli_main(["analyze", str(step_path), "--out-dir", str(out_dir), "--quiet"])


def _entity_layer_counts(msp) -> dict:
    out: dict = {}
    for e in msp:
        out[e.dxf.layer] = out.get(e.dxf.layer, 0) + 1
    return out


def _lwpoly_bbox(poly) -> tuple:
    pts = list(poly.get_points("xy"))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _polygon_area(pts) -> float:
    """Signed polygon area via the shoelace formula; always returns absolute value."""
    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def _winner_class(part_entry) -> str:
    """Return the highest-probability class name from the decision trace."""
    probs = part_entry.classification.trace.probabilities
    if not probs:
        return part_entry.classification.label
    return max(probs.items(), key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# 1. Flat plate
# ---------------------------------------------------------------------------


def test_flat_plate_classifies_as_plaat(tmp_path: Path):
    step = write_flat_plate_step(tmp_path / "plate.step", l=100.0, w=60.0, t=2.0)
    out = tmp_path / "out"

    rc = _run_pipeline(step, out)
    assert rc == 0, "CLI analyze should exit 0 on a valid STEP"

    manifest = read_xml(out / "manifest.xml")
    assert len(manifest.parts) == 1, "single-solid file should yield one part"

    entry = manifest.parts[0]
    # Highest-probability class should be plaat (label may be 'uncertain' if
    # the confidence threshold isn't met - that is a separate calibration
    # concern; this assertion captures the underlying scoring intent).
    assert _winner_class(entry) == "plaat", (
        f"flat plate's winning class should be 'plaat', got "
        f"{_winner_class(entry)!r} (label={entry.classification.label!r})"
    )

    # Unfold probe should succeed with no bends for a flat plate.
    assert entry.unfold is not None
    assert entry.unfold.status.value == "success"
    assert entry.unfold.n_bends == 0

    # Geometric sanity: bounding box dims match input within discretisation tol.
    bbox = entry.features.bbox_dims_sorted
    assert math.isclose(bbox[0], 100.0, rel_tol=0.05)
    assert math.isclose(bbox[1], 60.0, rel_tol=0.05)
    assert math.isclose(bbox[2], 2.0, rel_tol=0.05)


def test_flat_plate_emits_a_dxf(tmp_path: Path):
    step = write_flat_plate_step(tmp_path / "plate.step", l=100.0, w=60.0, t=2.0)
    out = tmp_path / "out"

    rc = _run_pipeline(step, out)
    assert rc == 0

    manifest = read_xml(out / "manifest.xml")
    entry = manifest.parts[0]
    assert entry.classification.label == "plaat"
    assert entry.flat_dxf_path is not None

    dxf_path = out / entry.flat_dxf_path
    assert dxf_path.exists()
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    counts = _entity_layer_counts(msp)
    assert counts.get("OUTER", 0) == 1
    bbox = _lwpoly_bbox(next(iter(msp.query('LWPOLYLINE[layer=="OUTER"]'))))
    assert math.isclose(bbox[2] - bbox[0], 100.0, rel_tol=0.05)
    assert math.isclose(bbox[3] - bbox[1], 60.0, rel_tol=0.05)


# ---------------------------------------------------------------------------
# 2. Plate with 4 holes
# ---------------------------------------------------------------------------


def test_plate_with_four_holes_records_hole_count(tmp_path: Path):
    step = write_flat_plate_step(
        tmp_path / "plate4.step",
        l=200.0,
        w=100.0,
        t=3.0,
        with_holes=[
            (20.0, 20.0, 10.0),
            (180.0, 20.0, 10.0),
            (20.0, 80.0, 10.0),
            (180.0, 80.0, 10.0),
        ],
    )
    out = tmp_path / "out"
    assert _run_pipeline(step, out) == 0

    manifest = read_xml(out / "manifest.xml")
    assert len(manifest.parts) == 1
    entry = manifest.parts[0]

    # The HoleAnalyzer should report exactly four holes; their diameters
    # should all be near 10 mm.
    assert entry.holes is not None
    assert entry.holes.hole_count == 4, f"expected 4 holes, got {entry.holes.hole_count}"
    for d in entry.holes.diameters:
        assert math.isclose(d, 10.0, rel_tol=0.05)

    # Winning class is still 'plaat' for a holey plate.
    assert _winner_class(entry) == "plaat"


def test_plate_with_four_holes_dxf_has_four_inner_polylines(tmp_path: Path):
    step = write_flat_plate_step(
        tmp_path / "plate4.step",
        l=200.0,
        w=100.0,
        t=3.0,
        with_holes=[
            (20.0, 20.0, 10.0),
            (180.0, 20.0, 10.0),
            (20.0, 80.0, 10.0),
            (180.0, 80.0, 10.0),
        ],
    )
    out = tmp_path / "out"
    assert _run_pipeline(step, out) == 0

    manifest = read_xml(out / "manifest.xml")
    entry = manifest.parts[0]
    assert entry.flat_dxf_path is not None
    doc = ezdxf.readfile(out / entry.flat_dxf_path)
    counts = _entity_layer_counts(doc.modelspace())
    assert counts.get("INNER", 0) == 4


# ---------------------------------------------------------------------------
# 3. L-bracket
# ---------------------------------------------------------------------------


def test_lbracket_unfold_succeeds(tmp_path: Path):
    step = write_lbracket_step(tmp_path / "l.step", leg_a=50.0, leg_b=40.0, t=2.0, bend_radius=3.0)
    out = tmp_path / "out"
    assert _run_pipeline(step, out) == 0

    manifest = read_xml(out / "manifest.xml")
    assert len(manifest.parts) == 1
    entry = manifest.parts[0]

    # Plate-like classification: winning class should be 'plaat'.
    assert _winner_class(entry) == "plaat"

    # Unfold completes (status SUCCESS) even when n_bends is reported as 0
    # because of the outer-skin / base-face quirk in unfold_probe.
    assert entry.unfold is not None
    assert entry.unfold.status.value in {"success", "partial"}
    assert math.isclose(entry.unfold.thickness_mean, 2.0, rel_tol=0.05)


def test_lbracket_reports_one_bend_and_writes_bend_line_dxf(tmp_path: Path):
    step = write_lbracket_step(tmp_path / "l.step", leg_a=50.0, leg_b=40.0, t=2.0, bend_radius=3.0)
    out = tmp_path / "out"
    assert _run_pipeline(step, out) == 0

    manifest = read_xml(out / "manifest.xml")
    entry = manifest.parts[0]
    assert entry.unfold is not None
    assert entry.unfold.n_bends == 1
    assert entry.classification.label == "plaat"
    assert entry.flat_dxf_path is not None

    doc = ezdxf.readfile(out / entry.flat_dxf_path)
    counts = _entity_layer_counts(doc.modelspace())
    n_bend_lines = counts.get("BEND_UP", 0) + counts.get("BEND_DOWN", 0)
    assert n_bend_lines == 1

    # Total outer-contour area = A_outer_horiz + A_outer_vert + BA*width.
    # The OUTER skin's flat portion on each leg has length ``leg - (t + R)``
    # because the outer bend arc consumes the corner region. K = 0.44 matches
    # ``UNFOLD_K_FACTOR`` in classification_variables.
    leg_a, leg_b, t, br = 50.0, 40.0, 2.0, 3.0
    width = 30.0  # the fixture's default extrusion length
    a1 = (leg_a - t - br) * width
    a2 = (leg_b - t - br) * width
    ba = (math.pi / 2.0) * (br + 0.44 * t) * width
    expected = a1 + a2 + ba

    outer_total = 0.0
    for poly in doc.modelspace().query('LWPOLYLINE[layer=="OUTER"]'):
        outer_total += _polygon_area(list(poly.get_points("xy")))
    assert math.isclose(outer_total, expected, rel_tol=0.01), (
        f"outer area {outer_total:.2f} != expected {expected:.2f}"
    )


# ---------------------------------------------------------------------------
# 4. RHS profile
# ---------------------------------------------------------------------------


def test_rhs_profile_is_recognised_as_profiel(tmp_path: Path):
    step = write_profile_step(
        tmp_path / "rhs.step", family="RHS", h=100.0, b=60.0, t=4.0, length=1000.0
    )
    out = tmp_path / "out"
    assert _run_pipeline(step, out) == 0

    manifest = read_xml(out / "manifest.xml")
    assert len(manifest.parts) == 1
    entry = manifest.parts[0]

    # Winning class must be 'profiel' (label may still be 'uncertain' due to
    # the conf threshold). cross_section_constant must be True.
    assert _winner_class(entry) == "profiel", (
        f"RHS profile should win as 'profiel', got {_winner_class(entry)!r}"
    )
    assert entry.features is not None
    assert entry.features.cross_section_constant is True

    # Profiles must NOT have a flat-DXF emitted.
    assert entry.flat_dxf_path is None
    parts_dir = out / "parts"
    if parts_dir.exists():
        assert not any(parts_dir.glob("*.dxf")), "profiles should not produce a DXF"


def test_rhs_profile_match_designation(tmp_path: Path):
    step = write_profile_step(
        tmp_path / "rhs.step", family="RHS", h=100.0, b=60.0, t=4.0, length=1000.0
    )
    out = tmp_path / "out"
    assert _run_pipeline(step, out) == 0

    manifest = read_xml(out / "manifest.xml")
    entry = manifest.parts[0]
    assert entry.profile_match is not None
    assert (entry.profile_match.family == "RHS") or (
        entry.profile_match.designation and "RHS" in entry.profile_match.designation
    )


# ---------------------------------------------------------------------------
# 5. CHS profile
# ---------------------------------------------------------------------------


def test_chs_profile_is_recognised_as_profiel(tmp_path: Path):
    step = write_profile_step(
        tmp_path / "chs.step", family="CHS", h=88.9, b=88.9, t=4.0, length=1000.0
    )
    out = tmp_path / "out"
    assert _run_pipeline(step, out) == 0

    manifest = read_xml(out / "manifest.xml")
    assert len(manifest.parts) == 1
    entry = manifest.parts[0]

    assert _winner_class(entry) == "profiel", (
        f"CHS profile should win as 'profiel', got {_winner_class(entry)!r}"
    )
    assert entry.features is not None
    assert entry.features.cross_section_constant is True
    # Diameter survives in the bbox dims.
    bbox = entry.features.bbox_dims_sorted
    assert math.isclose(bbox[1], 88.9, rel_tol=0.05)
    assert math.isclose(bbox[2], 88.9, rel_tol=0.05)


def test_chs_profile_match_family(tmp_path: Path):
    step = write_profile_step(
        tmp_path / "chs.step", family="CHS", h=88.9, b=88.9, t=4.0, length=1000.0
    )
    out = tmp_path / "out"
    assert _run_pipeline(step, out) == 0

    manifest = read_xml(out / "manifest.xml")
    entry = manifest.parts[0]
    assert entry.profile_match is not None
    assert entry.profile_match.family == "CHS"


# ---------------------------------------------------------------------------
# 6. Multi-part assembly
# ---------------------------------------------------------------------------


def _build_assembly_step(target: Path) -> Path:
    """Build a 3-part named assembly: plate, L-bracket, RHS profile."""
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


def test_multi_part_assembly_lists_three_named_parts(tmp_path: Path):
    step = _build_assembly_step(tmp_path / "asm.step")
    out = tmp_path / "out"
    assert _run_pipeline(step, out) == 0

    manifest = read_xml(out / "manifest.xml")
    names = sorted(p.part.name for p in manifest.parts)
    assert names == ["lbracket_001", "plate_001", "rhs_001"], (
        f"part names did not round-trip: {names}"
    )
    assert len(manifest.parts) == 3

    by_name = {p.part.name: p for p in manifest.parts}

    # The profile entry must not get a DXF.
    assert by_name["rhs_001"].flat_dxf_path is None
    # The plate and the L-bracket get a DXF iff they reach label=='plaat';
    # under the current calibration they both fall back to 'uncertain' so we
    # do NOT assert DXF presence here. The decision-trace probabilities are
    # still meaningful.
    assert _winner_class(by_name["plate_001"]) == "plaat"
    assert _winner_class(by_name["lbracket_001"]) == "plaat"
    assert _winner_class(by_name["rhs_001"]) == "profiel"


def test_multi_part_assembly_emits_dxfs_for_plate_and_lbracket(tmp_path: Path):
    step = _build_assembly_step(tmp_path / "asm.step")
    out = tmp_path / "out"
    assert _run_pipeline(step, out) == 0

    manifest = read_xml(out / "manifest.xml")
    by_name = {p.part.name: p for p in manifest.parts}

    assert by_name["plate_001"].flat_dxf_path is not None
    assert by_name["lbracket_001"].flat_dxf_path is not None
    assert by_name["rhs_001"].flat_dxf_path is None

    # Both produced DXFs should be readable by ezdxf and have an OUTER layer.
    for name in ("plate_001", "lbracket_001"):
        dxf_path = out / by_name[name].flat_dxf_path
        assert dxf_path.exists()
        doc = ezdxf.readfile(dxf_path)
        counts = _entity_layer_counts(doc.modelspace())
        assert counts.get("OUTER", 0) >= 1, f"{name}: no OUTER polyline"


# ---------------------------------------------------------------------------
# 7. Manifest XML round-trip
# ---------------------------------------------------------------------------


def test_manifest_xml_round_trips_via_read_xml(tmp_path: Path):
    step = _build_assembly_step(tmp_path / "asm.step")
    out = tmp_path / "out"
    assert _run_pipeline(step, out) == 0

    manifest_path = out / "manifest.xml"
    assert manifest_path.exists()

    parsed = read_xml(manifest_path)
    assert len(parsed.parts) == 3

    # Labels are the same set as the strings we hand-tabulated.
    labels = sorted(p.classification.label for p in parsed.parts)
    assert all(lab in {"plaat", "profiel", "anders", "uncertain"} for lab in labels)

    # Every part must carry a non-empty decision-trace contributions list -
    # the score classifier always emits one Contribution per (class, feature).
    for entry in parsed.parts:
        assert entry.classification.trace.contributions, (
            f"empty contributions for part {entry.part.name!r}"
        )

    # Source path / model version / generated-at survive the round-trip.
    assert parsed.source_path.endswith("asm.step")
    assert parsed.model_version != ""
    assert parsed.generated_at != ""


# ---------------------------------------------------------------------------
# 8. CLI invocation as a subprocess
# ---------------------------------------------------------------------------


def test_cli_subprocess_analyzes_a_tiny_plate(tmp_path: Path):
    step = write_flat_plate_step(tmp_path / "tiny.step", l=40.0, w=20.0, t=1.5)
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "manufacturing_pipeline.cli",
            "analyze",
            str(step),
            "--out-dir",
            str(out),
            "--quiet",
        ],
        cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"CLI subprocess exited {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert (out / "manifest.xml").exists()

    parsed = read_xml(out / "manifest.xml")
    assert len(parsed.parts) >= 1
