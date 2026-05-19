"""Tests for manufacturing_pipeline.io.dxf_writer.

The strategy throughout is round-trip: build a synthetic FlatPattern, write it
to a temporary DXF, reopen with ``ezdxf.readfile``, and assert structural
properties on the resulting document.
"""

from __future__ import annotations

import math
from pathlib import Path

import ezdxf
import pytest

from manufacturing_pipeline.io.dxf_writer import (
    FlatPattern,
    write_assembly_dxf,
    write_dxf,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _rect(x0: float, y0: float, w: float, h: float) -> list[tuple[float, float]]:
    return [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]


def _circle_poly(cx: float, cy: float, r: float, n: int = 32) -> list[tuple[float, float]]:
    return [
        (cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


def _sample_pattern(part_name: str = "TEST-001") -> FlatPattern:
    return FlatPattern(
        outer_contour=[_rect(0.0, 0.0, 100.0, 60.0)],
        holes=[_circle_poly(50.0, 30.0, 8.0, n=32)],
        bend_lines=[
            ((25.0, 0.0), (25.0, 60.0), {"angle": 90.0, "radius": 1.5, "direction": "up"}),
            ((75.0, 0.0), (75.0, 60.0), {"angle": 45.0, "radius": 2.0, "direction": "down"}),
        ],
        thickness=2.0,
        bbox=(0.0, 0.0, 100.0, 60.0),
        part_name=part_name,
    )


def _entity_layers(msp) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in msp:
        out[e.dxf.layer] = out.get(e.dxf.layer, 0) + 1
    return out


def _bbox_of_lwpoly(poly) -> tuple[float, float, float, float]:
    pts = list(poly.get_points("xy"))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------------------
# 1. Round-trip a full pattern
# ---------------------------------------------------------------------------


def test_round_trip_full_pattern(tmp_path: Path):
    pattern = _sample_pattern()
    out = write_dxf(pattern, tmp_path / "rt.dxf")
    assert out.exists()

    doc = ezdxf.readfile(out)
    msp = doc.modelspace()
    layer_names = {ly.dxf.name for ly in doc.layers}

    # All required layers exist
    for layer in ("OUTER", "INNER", "BEND_UP", "BEND_DOWN", "ANNOTATION", "INFO"):
        assert layer in layer_names, f"missing layer {layer}"

    counts = _entity_layers(msp)
    assert counts.get("OUTER", 0) == 1
    assert counts.get("INNER", 0) == 1
    assert counts.get("BEND_UP", 0) == 1
    assert counts.get("BEND_DOWN", 0) == 1
    # INFO header text
    assert counts.get("INFO", 0) == 1

    # Units = millimetres (DXF code 4)
    assert doc.header["$INSUNITS"] == 4

    # OUTER bbox matches the input rectangle
    outer = list(msp.query('LWPOLYLINE[layer=="OUTER"]'))[0]
    bb = _bbox_of_lwpoly(outer)
    assert bb == pytest.approx((0.0, 0.0, 100.0, 60.0), abs=1e-6)


# ---------------------------------------------------------------------------
# 2. Empty hole list still produces a valid DXF
# ---------------------------------------------------------------------------


def test_no_holes(tmp_path: Path):
    pattern = FlatPattern(
        outer_contour=[_rect(0, 0, 50, 50)],
        holes=[],
        bend_lines=[],
        thickness=1.0,
        bbox=(0, 0, 50, 50),
        part_name="SOLID",
    )
    out = write_dxf(pattern, tmp_path / "no_holes.dxf")
    doc = ezdxf.readfile(out)
    msp = doc.modelspace()
    counts = _entity_layers(msp)
    assert counts.get("OUTER", 0) == 1
    assert counts.get("INNER", 0) == 0


# ---------------------------------------------------------------------------
# 3. Multiple disjoint outer contours
# ---------------------------------------------------------------------------


def test_multiple_outer_contours(tmp_path: Path):
    pattern = FlatPattern(
        outer_contour=[_rect(0, 0, 30, 30), _rect(50, 0, 40, 20)],
        holes=[],
        bend_lines=[],
        thickness=1.5,
        bbox=(0, 0, 90, 30),
        part_name="MULTI",
    )
    out = write_dxf(pattern, tmp_path / "multi.dxf")
    doc = ezdxf.readfile(out)
    msp = doc.modelspace()
    outers = list(msp.query('LWPOLYLINE[layer=="OUTER"]'))
    assert len(outers) == 2
    for p in outers:
        assert p.dxf.layer == "OUTER"


# ---------------------------------------------------------------------------
# 4. Assembly with nesting="none" preserves coordinates
# ---------------------------------------------------------------------------


def test_assembly_none_preserves_coords(tmp_path: Path):
    a = FlatPattern(
        outer_contour=[_rect(0, 0, 20, 10)],
        holes=[],
        bend_lines=[],
        thickness=1.0,
        bbox=(0, 0, 20, 10),
        part_name="A",
    )
    b = FlatPattern(
        outer_contour=[_rect(100, 200, 30, 15)],
        holes=[],
        bend_lines=[],
        thickness=1.0,
        bbox=(100, 200, 130, 215),
        part_name="B",
    )
    c = FlatPattern(
        outer_contour=[_rect(-50, -50, 10, 10)],
        holes=[],
        bend_lines=[],
        thickness=1.0,
        bbox=(-50, -50, -40, -40),
        part_name="C",
    )
    out = write_assembly_dxf([a, b, c], tmp_path / "asm_none.dxf", nesting="none")
    doc = ezdxf.readfile(out)
    msp = doc.modelspace()
    outers = list(msp.query('LWPOLYLINE[layer=="OUTER"]'))
    assert len(outers) == 3
    bboxes = sorted(_bbox_of_lwpoly(p) for p in outers)
    expected = sorted([(0, 0, 20, 10), (100, 200, 130, 215), (-50, -50, -40, -40)])
    for got, want in zip(bboxes, expected):
        assert got == pytest.approx(want, abs=1e-6)


# ---------------------------------------------------------------------------
# 5. Assembly with nesting="strip" places bboxes side-by-side without overlap
# ---------------------------------------------------------------------------


def test_assembly_strip_lays_out_in_row(tmp_path: Path):
    a = FlatPattern(
        outer_contour=[_rect(0, 0, 20, 10)],
        holes=[],
        bend_lines=[],
        thickness=1.0,
        bbox=(0, 0, 20, 10),
        part_name="A",
    )
    b = FlatPattern(
        outer_contour=[_rect(0, 0, 30, 15)],
        holes=[],
        bend_lines=[],
        thickness=1.0,
        bbox=(0, 0, 30, 15),
        part_name="B",
    )
    c = FlatPattern(
        outer_contour=[_rect(0, 0, 10, 25)],
        holes=[],
        bend_lines=[],
        thickness=1.0,
        bbox=(0, 0, 10, 25),
        part_name="C",
    )
    out = write_assembly_dxf([a, b, c], tmp_path / "asm_strip.dxf", nesting="strip")
    doc = ezdxf.readfile(out)
    msp = doc.modelspace()
    outers = list(msp.query('LWPOLYLINE[layer=="OUTER"]'))
    assert len(outers) == 3

    bboxes = sorted((_bbox_of_lwpoly(p) for p in outers), key=lambda b: b[0])

    # Each pattern translated such that its left edge sits at the running cursor.
    # First should start at x=0.
    assert bboxes[0][0] == pytest.approx(0.0, abs=1e-6)
    # Widths preserved: 20, 30, 10.
    widths = [b[2] - b[0] for b in bboxes]
    assert sorted(widths) == [10.0, 20.0, 30.0]
    # Bboxes do not overlap in x.
    for i in range(len(bboxes) - 1):
        assert bboxes[i][2] <= bboxes[i + 1][0] + 1e-6


# ---------------------------------------------------------------------------
# 6. Input validation errors
# ---------------------------------------------------------------------------


def test_validation_thickness_zero(tmp_path: Path):
    pattern = FlatPattern(
        outer_contour=[_rect(0, 0, 10, 10)],
        thickness=0.0,
    )
    with pytest.raises(ValueError, match="thickness"):
        write_dxf(pattern, tmp_path / "bad.dxf")


def test_validation_polyline_two_points(tmp_path: Path):
    pattern = FlatPattern(
        outer_contour=[[(0.0, 0.0), (10.0, 0.0)]],
        thickness=1.0,
    )
    with pytest.raises(ValueError, match="distinct points"):
        write_dxf(pattern, tmp_path / "bad.dxf")


def test_validation_coincident_bend_endpoints(tmp_path: Path):
    pattern = FlatPattern(
        outer_contour=[_rect(0, 0, 10, 10)],
        bend_lines=[((5.0, 5.0), (5.0, 5.0), {"angle": 90, "radius": 1, "direction": "up"})],
        thickness=1.0,
    )
    with pytest.raises(ValueError, match="coincident"):
        write_dxf(pattern, tmp_path / "bad.dxf")


# ---------------------------------------------------------------------------
# 7. Bend-table annotation includes every bend record
# ---------------------------------------------------------------------------


def test_bend_table_contents(tmp_path: Path):
    pattern = _sample_pattern("BENDS")
    out = write_dxf(pattern, tmp_path / "bends.dxf")
    doc = ezdxf.readfile(out)
    msp = doc.modelspace()
    mtexts = list(msp.query('MTEXT[layer=="ANNOTATION"]'))
    assert len(mtexts) >= 1
    body = "\n".join(m.text for m in mtexts)
    assert "BEND TABLE" in body
    # Two bends with their distinguishing direction tokens.
    assert "#0" in body
    assert "#1" in body
    assert "up" in body
    assert "down" in body
    # Bend lengths (60.0) should appear.
    assert "60.00" in body


# ---------------------------------------------------------------------------
# 8. Title-block text contains part name and thickness
# ---------------------------------------------------------------------------


def test_title_block_text(tmp_path: Path):
    pattern = _sample_pattern("AWESOME-PART")
    pattern.thickness = 3.25
    out = write_dxf(pattern, tmp_path / "title.dxf")
    doc = ezdxf.readfile(out)
    msp = doc.modelspace()
    infos = list(msp.query('TEXT[layer=="INFO"]'))
    assert len(infos) == 1
    txt = infos[0].dxf.text
    assert "AWESOME-PART" in txt
    assert "3.25" in txt
    assert "mm" in txt


# ---------------------------------------------------------------------------
# 9. Layer colors come back correctly when re-read
# ---------------------------------------------------------------------------


def test_layer_colors(tmp_path: Path):
    pattern = _sample_pattern()
    out = write_dxf(pattern, tmp_path / "colors.dxf")
    doc = ezdxf.readfile(out)
    expected = {
        "OUTER": 1,
        "INNER": 3,
        "BEND_UP": 5,
        "BEND_DOWN": 4,
        "ANNOTATION": 7,
        "INFO": 7,
    }
    for name, color in expected.items():
        layer = doc.layers.get(name)
        assert layer.dxf.color == color, (
            f"layer {name} expected color {color}, got {layer.dxf.color}"
        )


# ---------------------------------------------------------------------------
# 10. DASHED2 linetype is registered when bend lines are present
# ---------------------------------------------------------------------------


def test_dashed2_linetype_registered(tmp_path: Path):
    pattern = _sample_pattern()
    out = write_dxf(pattern, tmp_path / "lt.dxf")
    doc = ezdxf.readfile(out)
    linetype_names = {lt.dxf.name for lt in doc.linetypes}
    assert "DASHED2" in linetype_names
    # And the BEND layers should use DASHED2.
    assert doc.layers.get("BEND_UP").dxf.linetype == "DASHED2"
    assert doc.layers.get("BEND_DOWN").dxf.linetype == "DASHED2"


# ---------------------------------------------------------------------------
# Extra coverage: empty assembly raises; unknown nesting raises.
# ---------------------------------------------------------------------------


def test_assembly_empty_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        write_assembly_dxf([], tmp_path / "empty.dxf")


def test_assembly_unknown_nesting_raises(tmp_path: Path):
    pattern = _sample_pattern()
    with pytest.raises(ValueError, match="nesting"):
        write_assembly_dxf([pattern], tmp_path / "bad.dxf", nesting="quantum")


# ---------------------------------------------------------------------------
# Bin-pack nesting
# ---------------------------------------------------------------------------


def _square_pattern(side: float, name: str) -> FlatPattern:
    return FlatPattern(
        outer_contour=[_rect(0.0, 0.0, side, side)],
        holes=[],
        bend_lines=[],
        thickness=1.0,
        bbox=(0.0, 0.0, side, side),
        part_name=name,
    )


def _bboxes_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    tol: float = 1e-6,
) -> bool:
    return not (
        a[2] <= b[0] + tol or b[2] <= a[0] + tol or a[3] <= b[1] + tol or b[3] <= a[1] + tol
    )


def test_assembly_binpack_single_sheet(tmp_path: Path):
    """Five 50x50 patterns fit on a single 300x300 sheet, non-overlapping."""
    patterns = [_square_pattern(50.0, f"P{i}") for i in range(5)]
    out = write_assembly_dxf(
        patterns,
        tmp_path / "binpack_one.dxf",
        nesting="binpack",
        sheet_size=(300.0, 300.0),
        margin=10.0,
    )
    doc = ezdxf.readfile(out)
    msp = doc.modelspace()
    outers = list(msp.query('LWPOLYLINE[layer=="OUTER"]'))
    assert len(outers) == 5

    bboxes = [_bbox_of_lwpoly(p) for p in outers]
    # All bboxes within the single sheet (x in [0, 300], no second sheet offset).
    for bb in bboxes:
        assert 0.0 <= bb[0] and bb[2] <= 300.0 + 1e-6
        assert 0.0 <= bb[1] and bb[3] <= 300.0 + 1e-6
    # No two bboxes overlap.
    for i in range(len(bboxes)):
        for j in range(i + 1, len(bboxes)):
            assert not _bboxes_overlap(bboxes[i], bboxes[j]), (
                f"bboxes {i} and {j} overlap: {bboxes[i]} vs {bboxes[j]}"
            )


def test_assembly_binpack_multi_sheet(tmp_path: Path):
    """10 patterns of 100x100 cannot fit one 300x300 sheet -> multiple sheets."""
    patterns = [_square_pattern(100.0, f"P{i}") for i in range(10)]
    out = write_assembly_dxf(
        patterns,
        tmp_path / "binpack_multi.dxf",
        nesting="binpack",
        sheet_size=(300.0, 300.0),
        margin=10.0,
    )
    doc = ezdxf.readfile(out)
    msp = doc.modelspace()
    outers = list(msp.query('LWPOLYLINE[layer=="OUTER"]'))
    assert len(outers) == 10

    bboxes = [_bbox_of_lwpoly(p) for p in outers]
    sheet_w = 300.0
    sheet_gap = 50.0
    stride = sheet_w + sheet_gap  # = 350.0

    # Cluster bboxes by sheet index = floor(xmin / stride).
    clusters: dict[int, list] = {}
    for bb in bboxes:
        idx = int((bb[0] + 1e-6) // stride)
        clusters.setdefault(idx, []).append(bb)
    assert len(clusters) >= 2, f"expected >=2 sheets, got {sorted(clusters)}"

    # Each cluster's bboxes are within that sheet's x-range, no overlaps within
    # cluster.
    for idx, group in clusters.items():
        x0 = idx * stride
        x1 = x0 + sheet_w
        for bb in group:
            assert x0 - 1e-6 <= bb[0] and bb[2] <= x1 + 1e-6
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                assert not _bboxes_overlap(group[i], group[j])


def test_assembly_binpack_inferred_sheet_size(tmp_path: Path):
    """sheet_size=None infers a square sheet sized to the biggest part."""
    # One 30x30 part: inferred sheet side = 1.5 * 30 = 45. With margin=2 on a
    # single small part both axes fit easily on one sheet.
    patterns = [_square_pattern(30.0, "A"), _square_pattern(20.0, "B")]
    out = write_assembly_dxf(
        patterns,
        tmp_path / "binpack_inferred.dxf",
        nesting="binpack",
        sheet_size=None,
        margin=2.0,
    )
    doc = ezdxf.readfile(out)
    msp = doc.modelspace()
    outers = list(msp.query('LWPOLYLINE[layer=="OUTER"]'))
    assert len(outers) == 2

    # Inferred side = 1.5 * 30 = 45 mm. Cluster by sheet stride.
    sheet_side = 1.5 * 30.0
    stride = sheet_side + 50.0
    bboxes = [_bbox_of_lwpoly(p) for p in outers]
    for bb in bboxes:
        sheet_idx = int((bb[0] + 1e-6) // stride)
        x0 = sheet_idx * stride
        assert x0 - 1e-6 <= bb[0] and bb[2] <= x0 + sheet_side + 1e-6
        assert -1e-6 <= bb[1] and bb[3] <= sheet_side + 1e-6
    # No overlap.
    assert not _bboxes_overlap(bboxes[0], bboxes[1])


def test_assembly_binpack_invalid_sheet_size(tmp_path: Path):
    pattern = _sample_pattern()
    with pytest.raises(ValueError, match="sheet_size"):
        write_assembly_dxf(
            [pattern],
            tmp_path / "bad.dxf",
            nesting="binpack",
            sheet_size=(0.0, 100.0),
        )
    with pytest.raises(ValueError, match="sheet_size"):
        write_assembly_dxf(
            [pattern],
            tmp_path / "bad2.dxf",
            nesting="binpack",
            sheet_size=(100.0, -5.0),
        )
