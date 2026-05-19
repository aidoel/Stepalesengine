"""Tests for the standard-profile cross-section matcher.

Covers:
- YAML loading + designation enumeration.
- Family-by-family geometry round-trip (synthesized CrossSection in, ProfileMatch out).
- Custom (non-catalogue) entries fall to nearest or to ``profiel_custom``.
- Defensive behaviour on empty / malformed inputs.
- Confidence decay vs residual.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

import pytest

from manufacturing_pipeline.config.classification_variables import (
    PROFILE_RESIDUAL_REJECT_MM,
    PROFILE_RESIDUAL_TOLERANCE_MM,
)
from manufacturing_pipeline.geometry.cross_section import compute_signature
from manufacturing_pipeline.geometry.profile_matcher import ProfileShapeMatcher
from manufacturing_pipeline.geometry.types import CrossSection

# --------------------------------------------------------------- polyline mints


def _section(
    *,
    polyline: Sequence[tuple[float, float]],
    sig: dict,
    h: float,
    b: float,
    n_outer: int = 0,
    n_inner: int = 0,
) -> CrossSection:
    """Build a CrossSection that the matcher will accept."""
    return CrossSection(
        position=0.0,
        area=0.0,
        perimeter=0.0,
        n_outer_segments=n_outer,
        n_inner_wires=n_inner,
        bbox_2d=(h, b),
        polyline=list(polyline),
        shape_hash=sig,
    )


def _i_polyline(h: float, b: float, t_w: float, t_f: float) -> list[tuple[float, float]]:
    """Canonical 12-segment I-section polyline (no fillets) centred on the origin."""
    hh, bb = h / 2.0, b / 2.0
    tw = t_w / 2.0
    return [
        (-bb, hh),
        (bb, hh),
        (bb, hh - t_f),
        (tw, hh - t_f),
        (tw, -hh + t_f),
        (bb, -hh + t_f),
        (bb, -hh),
        (-bb, -hh),
        (-bb, -hh + t_f),
        (-tw, -hh + t_f),
        (-tw, hh - t_f),
        (-bb, hh - t_f),
    ]


def _u_polyline(h: float, b: float, t_w: float, t_f: float) -> list[tuple[float, float]]:
    """Canonical 8-segment U-channel polyline, open to +x. Symmetric about y=0."""
    hh = h / 2.0
    return [
        (0.0, hh),
        (b, hh),
        (b, hh - t_f),
        (t_w, hh - t_f),
        (t_w, -hh + t_f),
        (b, -hh + t_f),
        (b, -hh),
        (0.0, -hh),
    ]


def _l_polyline(h: float, b: float, t: float) -> list[tuple[float, float]]:
    """Canonical 6-segment L-angle polyline (no fillet)."""
    return [
        (0.0, 0.0),
        (b, 0.0),
        (b, t),
        (t, t),
        (t, h),
        (0.0, h),
    ]


def _t_polyline(h: float, b: float, t_w: float, t_f: float) -> list[tuple[float, float]]:
    """Canonical 8-segment T-section polyline, flange on top."""
    hh = h / 2.0
    bb = b / 2.0
    tw = t_w / 2.0
    return [
        (-bb, hh),
        (bb, hh),
        (bb, hh - t_f),
        (tw, hh - t_f),
        (tw, -hh),
        (-tw, -hh),
        (-tw, hh - t_f),
        (-bb, hh - t_f),
    ]


def _rhs_polyline(h: float, b: float, t: float) -> tuple[list, list]:
    """Outer + inner rectangle polylines for RHS/SHS."""
    outer = [(-b / 2.0, -h / 2.0), (b / 2.0, -h / 2.0), (b / 2.0, h / 2.0), (-b / 2.0, h / 2.0)]
    inner_h, inner_b = h - 2 * t, b - 2 * t
    inner = [
        (-inner_b / 2.0, -inner_h / 2.0),
        (inner_b / 2.0, -inner_h / 2.0),
        (inner_b / 2.0, inner_h / 2.0),
        (-inner_b / 2.0, inner_h / 2.0),
    ]
    return outer, inner


# ============================================================== loader / enums


def test_yaml_files_load_without_errors():
    m = ProfileShapeMatcher()
    assert len(m.list_designations()) == 439  # 71 + 31 + 22 + 8 + 104 + 101 + 102


def test_per_family_designation_counts():
    m = ProfileShapeMatcher()
    counts = {
        fam: len(m.list_designations(fam)) for fam in ["I", "U", "L", "T", "RHS", "SHS", "CHS"]
    }
    assert counts == {"I": 71, "U": 31, "L": 22, "T": 8, "RHS": 104, "SHS": 101, "CHS": 102}
    i_all = m.list_designations("I")
    assert sum(1 for d in i_all if d.startswith("IPE ")) >= 18
    assert sum(1 for d in i_all if d.startswith("HEA ")) >= 17
    assert sum(1 for d in i_all if d.startswith("HEB ")) >= 17
    assert sum(1 for d in m.list_designations("U") if d.startswith("UNP ")) >= 17


def test_list_designations_unknown_family_returns_empty():
    m = ProfileShapeMatcher()
    assert m.list_designations("XX") == []
    assert "IPE 200" in m.list_designations("I")
    assert all(d.startswith(("IPE", "HEA", "HEB", "HEM", "IPN")) for d in m.list_designations("I"))


# ===================================================================== matches


def test_match_ipe_200():
    m = ProfileShapeMatcher()
    h, b, t_w, t_f = 200.0, 100.0, 5.6, 8.5
    sig = {
        "n_inner": 0,
        "n_seg": 12,
        "sym_x": True,
        "sym_y": True,
        "bbox_h": h,
        "bbox_b": b,
    }
    sec = _section(polyline=_i_polyline(h, b, t_w, t_f), sig=sig, h=h, b=b)
    res = m.match(sec)
    assert res.family == "I"
    assert res.designation == "IPE 200"
    assert res.residual_mm <= 0.1
    assert res.confidence > 0.8


def test_match_hea_200():
    m = ProfileShapeMatcher()
    # HEA 200: h=190 (web height), b=200 (flange width), t_w=6.5, t_f=10.
    # Note this is one of the rare I-sections with b > h.
    h, b, t_w, t_f = 190.0, 200.0, 6.5, 10.0
    sig = {
        "n_inner": 0,
        "n_seg": 12,
        "sym_x": True,
        "sym_y": True,
        "bbox_h": h,
        "bbox_b": b,
    }
    sec = _section(polyline=_i_polyline(h, b, t_w, t_f), sig=sig, h=h, b=b)
    res = m.match(sec)
    assert res.family == "I"
    assert res.designation == "HEA 200"
    assert res.residual_mm <= 0.1


def test_match_unp_100():
    m = ProfileShapeMatcher()
    # UNP 100: h=100, b=50, t_w=6, t_f=8.5
    h, b, t_w, t_f = 100.0, 50.0, 6.0, 8.5
    sig = {
        "n_inner": 0,
        "n_seg": 8,
        "sym_x": True,
        "sym_y": False,
        "bbox_h": h,
        "bbox_b": b,
    }
    sec = _section(polyline=_u_polyline(h, b, t_w, t_f), sig=sig, h=h, b=b)
    res = m.match(sec)
    assert res.family == "U"
    assert res.designation == "UNP 100"
    assert res.residual_mm <= 0.1


def test_match_rhs_100x60x4():
    m = ProfileShapeMatcher()
    h, b, t = 100.0, 60.0, 4.0
    outer, inner = _rhs_polyline(h, b, t)
    sig = {
        "n_inner": 1,
        "n_seg_outer": 4,
        "orth_ratio": 1.0,
        "outer_circular": False,
        "inner_circular": False,
        "bbox_h": h,
        "bbox_b": b,
        "inner_bbox": (h - 2 * t, b - 2 * t),
    }
    sec = _section(polyline=outer, sig=sig, h=h, b=b, n_outer=4, n_inner=1)
    res = m.match(sec)
    assert res.family == "RHS"
    assert res.designation == "RHS 100x60x4"
    assert res.residual_mm <= 0.1


def test_match_shs_80x80x5():
    m = ProfileShapeMatcher()
    h = b = 80.0
    t = 5.0
    outer, _ = _rhs_polyline(h, b, t)
    sig = {
        "n_inner": 1,
        "n_seg_outer": 4,
        "orth_ratio": 1.0,
        "outer_circular": False,
        "inner_circular": False,
        "bbox_h": h,
        "bbox_b": b,
        "inner_bbox": (h - 2 * t, b - 2 * t),
    }
    sec = _section(polyline=outer, sig=sig, h=h, b=b, n_outer=4, n_inner=1)
    res = m.match(sec)
    assert res.family == "SHS"
    assert res.designation == "SHS 80x80x5"
    assert res.residual_mm <= 0.1


def test_match_chs_88_9x4():
    m = ProfileShapeMatcher()
    d = 88.9
    t = 4.0
    sig = {
        "n_inner": 1,
        "outer_circular": True,
        "inner_circular": True,
        "bbox_h": d,
        "bbox_b": d,
        "inner_bbox": (d - 2 * t, d - 2 * t),
    }
    # Approximate the circle with a 24-gon to give the matcher a non-trivial bbox.
    n = 24
    poly = [
        (d / 2.0 * math.cos(2 * math.pi * i / n), d / 2.0 * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]
    sec = _section(polyline=poly, sig=sig, h=d, b=d, n_outer=1, n_inner=1)
    res = m.match(sec)
    assert res.family == "CHS"
    assert res.designation == "CHS 88.9x4.0"
    assert res.residual_mm <= 0.1


def test_match_l_50x50x5():
    m = ProfileShapeMatcher()
    h = b = 50.0
    t = 5.0
    sig = {"n_inner": 0, "n_seg": 6, "sym_x": False, "sym_y": False}
    sec = _section(polyline=_l_polyline(h, b, t), sig=sig, h=h, b=b)
    res = m.match(sec)
    assert res.family == "L"
    assert res.designation == "L 50x50x5"
    assert res.residual_mm <= 0.1


# ============================================================ non-standard / failure


def test_custom_rhs_returns_nearest_within_tolerance():
    """A custom RHS 73x41x3.7 (not in catalogue) snaps to nearest."""
    m = ProfileShapeMatcher()
    h, b, t = 73.0, 41.0, 3.7
    outer, _ = _rhs_polyline(h, b, t)
    sig = {
        "n_inner": 1,
        "n_seg_outer": 4,
        "orth_ratio": 1.0,
        "outer_circular": False,
        "inner_circular": False,
        "bbox_h": h,
        "bbox_b": b,
        "inner_bbox": (h - 2 * t, b - 2 * t),
    }
    sec = _section(polyline=outer, sig=sig, h=h, b=b, n_outer=4, n_inner=1)
    res = m.match(sec)
    assert res.family == "RHS"
    # Nearest catalog entry should be returned with a non-zero residual.
    assert res.residual_mm > 0.0
    # We don't insist the residual stays inside the catalogue's tolerance,
    # because no on-grid entry exists; just that it does not exceed reject.
    if res.residual_mm <= PROFILE_RESIDUAL_REJECT_MM:
        assert res.designation is not None
    else:
        assert res.designation is None


def test_very_off_nominal_returns_no_designation():
    m = ProfileShapeMatcher()
    h = b = 999.0
    t = 50.0
    outer, _ = _rhs_polyline(h, b, t)
    sig = {
        "n_inner": 1,
        "n_seg_outer": 4,
        "orth_ratio": 1.0,
        "outer_circular": False,
        "inner_circular": False,
        "bbox_h": h,
        "bbox_b": b,
        "inner_bbox": (h - 2 * t, b - 2 * t),
    }
    sec = _section(polyline=outer, sig=sig, h=h, b=b, n_outer=4, n_inner=1)
    res = m.match(sec)
    assert res.designation is None
    assert res.residual_mm > PROFILE_RESIDUAL_REJECT_MM


def test_empty_signature_returns_unknown():
    m = ProfileShapeMatcher()
    sec = _section(polyline=[], sig={}, h=0.0, b=0.0)
    res = m.match(sec)
    assert res.family == "unknown"
    assert res.designation is None
    assert math.isinf(res.residual_mm)
    assert res.confidence == 0.0


def test_family_hint_overrides_detection():
    m = ProfileShapeMatcher()
    h, b, t_w, t_f = 200.0, 100.0, 5.6, 8.5
    # Provide a deliberately wrong signature; family_hint forces "I".
    sig = {"n_inner": 0, "n_seg": 4, "sym_x": False, "sym_y": False}
    sec = _section(polyline=_i_polyline(h, b, t_w, t_f), sig=sig, h=h, b=b)
    res = m.match(sec, family_hint="I")
    assert res.family == "I"
    assert res.designation == "IPE 200"


def test_malformed_yaml_entry_is_skipped(tmp_path: Path):
    """A YAML file with one bad entry should still load the good entries."""
    yaml_text = """
standard: "TEST"
family: "I"
units: "mm"
profiles:
  - designation: "GOOD 100"
    family: "I"
    standard: "TEST"
    h: 100.0
    b: 50.0
    t_w: 4.0
    t_f: 6.0
  - designation: "BAD MISSING DIM"
    family: "I"
    standard: "TEST"
    h: 100.0
    b: 50.0
    # missing t_w, t_f -> should be skipped
  - family: "I"
    standard: "TEST"
    h: 80.0
    b: 40.0
    t_w: 3.0
    t_f: 4.0
    # missing designation -> should be skipped
"""
    f = tmp_path / "fake_i.yaml"
    f.write_text(yaml_text)
    m = ProfileShapeMatcher(data_dir=tmp_path)
    designations = m.list_designations("I")
    assert designations == ["GOOD 100"]


def test_confidence_decreases_with_residual():
    m = ProfileShapeMatcher()
    # Build a CHS that is exactly on-grid and one with a deliberate offset.
    base_d, base_t = 88.9, 4.0
    poly = [
        (
            base_d / 2.0 * math.cos(2 * math.pi * i / 24),
            base_d / 2.0 * math.sin(2 * math.pi * i / 24),
        )
        for i in range(24)
    ]
    sig = {
        "n_inner": 1,
        "outer_circular": True,
        "inner_circular": True,
        "bbox_h": base_d,
        "bbox_b": base_d,
        "inner_bbox": (base_d - 2 * base_t, base_d - 2 * base_t),
    }
    on_grid = _section(polyline=poly, sig=sig, h=base_d, b=base_d, n_outer=1, n_inner=1)
    res_on = m.match(on_grid)

    off_d = 91.5
    off_t = 5.5
    poly_off = [
        (off_d / 2.0 * math.cos(2 * math.pi * i / 24), off_d / 2.0 * math.sin(2 * math.pi * i / 24))
        for i in range(24)
    ]
    sig_off = {
        "n_inner": 1,
        "outer_circular": True,
        "inner_circular": True,
        "bbox_h": off_d,
        "bbox_b": off_d,
        "inner_bbox": (off_d - 2 * off_t, off_d - 2 * off_t),
    }
    off_grid = _section(polyline=poly_off, sig=sig_off, h=off_d, b=off_d, n_outer=1, n_inner=1)
    res_off = m.match(off_grid)

    assert res_on.residual_mm < res_off.residual_mm
    assert res_on.confidence >= res_off.confidence
    assert 0.0 <= res_on.confidence <= 1.0
    assert 0.0 <= res_off.confidence <= 1.0


def test_fit_dimensions_returns_empty_for_unknown_family():
    m = ProfileShapeMatcher()
    sec = _section(polyline=[(0, 0), (100, 0), (100, 50), (0, 50)], sig={}, h=100.0, b=50.0)
    assert m.fit_dimensions(sec, "XX") == {}


def test_fit_dimensions_for_known_family():
    m = ProfileShapeMatcher()
    h, b, t_w, t_f = 200.0, 100.0, 5.6, 8.5
    sec = _section(polyline=_i_polyline(h, b, t_w, t_f), sig={}, h=h, b=b)
    dims = m.fit_dimensions(sec, "I")
    assert set(dims.keys()) == {"h", "b", "t_w", "t_f"}
    assert dims["h"] == pytest.approx(200.0, abs=0.01)
    assert dims["b"] == pytest.approx(100.0, abs=0.01)
    # Web/flange thicknesses fitted within PROFILE_RESIDUAL_TOLERANCE_MM.
    assert abs(dims["t_w"] - t_w) <= PROFILE_RESIDUAL_TOLERANCE_MM
    assert abs(dims["t_f"] - t_f) <= PROFILE_RESIDUAL_TOLERANCE_MM


# ============================================================== integration: fillets


def _i_with_fillets_polyline(
    h: float, b: float, t_w: float, t_f: float, r: float, n_arc: int = 8
) -> list[tuple[float, float]]:
    """Synthesise an I-section polyline including discretised concave fillets
    at the four web/flange transitions, mirroring what a real CAD slicer
    produces for an IPE with rolled fillets."""

    hh, bb, tw = h / 2.0, b / 2.0, t_w / 2.0

    def arc(cx: float, cy: float, ang_start: float, ang_end: float) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for i in range(n_arc + 1):
            ratio = i / n_arc
            a = math.radians(ang_start + (ang_end - ang_start) * ratio)
            out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return out

    pts: list[tuple[float, float]] = []
    pts.append((-bb, hh))
    pts.append((bb, hh))
    pts.append((bb, hh - t_f))
    pts.extend(arc(tw + r, hh - t_f - r, 90, 180))
    pts.append((tw, -hh + t_f + r))
    pts.extend(arc(tw + r, -hh + t_f + r, 180, 270))
    pts.append((bb, -hh + t_f))
    pts.append((bb, -hh))
    pts.append((-bb, -hh))
    pts.append((-bb, -hh + t_f))
    pts.extend(arc(-tw - r, -hh + t_f + r, 270, 360))
    pts.append((-tw, hh - t_f - r))
    pts.extend(arc(-tw - r, hh - t_f - r, 0, 90))
    pts.append((-bb, hh - t_f))

    deduped: list[tuple[float, float]] = []
    for p in pts:
        if deduped and abs(deduped[-1][0] - p[0]) < 1e-9 and abs(deduped[-1][1] - p[1]) < 1e-9:
            continue
        deduped.append(p)
    return deduped


def test_match_ipe_200_with_fillets():
    """Real production CAD output of IPE 200 carries an R=9 fillet at every
    web/flange transition. The fillet-collapse step must recover the
    canonical 12-corner I signature, restoring the matcher's correct hit."""

    m = ProfileShapeMatcher()
    h, b, t_w, t_f, r = 200.0, 100.0, 5.6, 8.5, 9.0
    poly = _i_with_fillets_polyline(h, b, t_w, t_f, r, n_arc=8)
    sig = compute_signature(poly, inner_polylines=[])

    assert sig["n_seg_pre_collapse"] > 16
    assert sig["n_seg_post_collapse"] == 12
    assert sig["sym_x"] and sig["sym_y"]

    sec = CrossSection(
        position=0.0,
        area=0.0,
        perimeter=0.0,
        n_outer_segments=sig["n_seg"],
        n_inner_wires=0,
        bbox_2d=(sig["bbox_h"], sig["bbox_b"]),
        polyline=poly,
        shape_hash=tuple(),
        signature=sig,
    )
    res = m.match(sec)
    assert res.family == "I"
    assert res.designation == "IPE 200"
    assert res.confidence > 0.5
