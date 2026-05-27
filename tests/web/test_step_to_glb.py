"""Tests for the GLB builders in :mod:`manufacturing_pipeline.web.step_to_glb`.

We exercise both entry points directly with real geometry:

* :func:`folded_glb` is driven through ``find_solid_for_part`` against a
  one-solid STEP file built from an OCP box primitive.
* :func:`unfolded_glb` is driven from a hand-built :class:`FlatPattern` with
  a square outer contour and one circular hole.

In both cases we assert that the returned bytes are a structurally valid GLB:
the file starts with the ``b"glTF"`` magic, the 12-byte header parses, and the
embedded JSON chunk decodes to an object that lists at least one mesh.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import pytest

pytest.importorskip("trimesh")
pytest.importorskip("shapely")
pytest.importorskip("OCP")

from manufacturing_pipeline.io.dxf_writer import FlatPattern  # noqa: E402
from manufacturing_pipeline.web import step_to_glb  # noqa: E402
from manufacturing_pipeline.web.step_to_glb import folded_glb, unfolded_glb  # noqa: E402

# ---------------------------------------------------------------------------
# GLB structural validation helper
# ---------------------------------------------------------------------------


def _assert_valid_glb(data: bytes) -> dict:
    """Validate the GLB container format and return the parsed JSON chunk.

    GLB layout: 12-byte header (magic, version, total length) followed by one
    or more chunks of (length, type, payload). The first chunk must be JSON.
    """
    assert isinstance(data, bytes) and len(data) > 20, "GLB output too short"
    assert data[:4] == b"glTF", f"missing glTF magic, got {data[:4]!r}"
    version, total_length = struct.unpack("<II", data[4:12])
    assert version == 2, f"unexpected glTF version: {version}"
    assert total_length == len(data), (
        f"header length {total_length} != actual length {len(data)}"
    )
    chunk_length, chunk_type = struct.unpack("<II", data[12:20])
    # 0x4E4F534A == 'JSON' little-endian
    assert chunk_type == 0x4E4F534A, f"first chunk is not JSON ({chunk_type:#x})"
    json_bytes = data[20 : 20 + chunk_length]
    payload = json.loads(json_bytes.decode("utf-8").rstrip("\x00 "))
    assert isinstance(payload, dict)
    assert "meshes" in payload and payload["meshes"], "GLB has no meshes"
    return payload


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_box_step(tmp_path: Path, name: str = "box.step") -> Path:
    """Write a single-solid box STEP and return the path."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    from manufacturing_pipeline.geometry.geometry_loader import write_step

    solid = BRepPrimAPI_MakeBox(40.0, 30.0, 2.0).Solid()
    out = tmp_path / name
    write_step(solid, out)
    return out


def _box_product_id(step_path: Path) -> str:
    """Resolve the product_id that ``find_solid_for_part`` will recognise."""
    from manufacturing_pipeline.web.step_to_svg import _load_assembly

    loaded = _load_assembly(step_path)
    assert loaded.part_index, "expected at least one indexed part in box STEP"
    return next(iter(loaded.part_index))


def _circle(cx: float, cy: float, r: float, n: int = 24) -> list[tuple[float, float]]:
    return [
        (cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


def _square(side: float = 50.0) -> list[tuple[float, float]]:
    return [(0.0, 0.0), (side, 0.0), (side, side), (0.0, side)]


# ---------------------------------------------------------------------------
# folded_glb
# ---------------------------------------------------------------------------


def test_folded_glb_returns_valid_glb_for_real_box(tmp_path: Path):
    """A box STEP should tessellate and round-trip into a structurally valid GLB."""
    step = _make_box_step(tmp_path)
    pid = _box_product_id(step)

    data = folded_glb(step, pid)

    assert data is not None, "folded_glb returned None for a real box solid"
    payload = _assert_valid_glb(data)
    # Box should produce at least one accessor and buffer view.
    assert payload.get("accessors"), "GLB has no accessors"
    assert payload.get("bufferViews"), "GLB has no bufferViews"


def test_folded_glb_returns_none_for_unknown_product_id(tmp_path: Path):
    """When the product_id has no matching solid, folded_glb returns ``None``."""
    step = _make_box_step(tmp_path)
    result = folded_glb(step, "no-such-product-id")
    assert result is None


def test_folded_glb_returns_none_when_source_path_missing(tmp_path: Path):
    """A non-existent source STEP should yield ``None`` rather than raising."""
    result = folded_glb(tmp_path / "does_not_exist.step", "whatever")
    assert result is None


# ---------------------------------------------------------------------------
# unfolded_glb
# ---------------------------------------------------------------------------


def test_unfolded_glb_returns_valid_glb_for_square_with_hole():
    """A square outer contour with one circular hole produces a valid GLB."""
    pattern = FlatPattern(
        outer_contour=[_square(50.0)],
        holes=[_circle(25.0, 25.0, 5.0)],
        bend_lines=[],
        thickness=2.0,
        bbox=(0.0, 0.0, 50.0, 50.0),
        units="mm",
        part_name="SQUARE_HOLE",
    )
    data = unfolded_glb(pattern)

    assert data is not None
    _assert_valid_glb(data)


def test_unfolded_glb_returns_none_for_empty_outer_contour():
    """No outer contour means there's nothing to extrude: return ``None``."""
    pattern = FlatPattern(
        outer_contour=[],
        holes=[],
        bend_lines=[],
        thickness=2.0,
        bbox=(0.0, 0.0, 0.0, 0.0),
        units="mm",
        part_name="EMPTY",
    )
    assert unfolded_glb(pattern) is None


def test_unfolded_glb_returns_none_when_all_outers_are_degenerate():
    """Outer contours with < 3 distinct points get dropped, leaving nothing."""
    pattern = FlatPattern(
        outer_contour=[[(0.0, 0.0), (1.0, 1.0)]],  # only 2 points
        holes=[],
        bend_lines=[],
        thickness=2.0,
        bbox=(0.0, 0.0, 1.0, 1.0),
        units="mm",
        part_name="DEGENERATE",
    )
    assert unfolded_glb(pattern) is None


def test_unfolded_glb_handles_multipolygon_via_disjoint_outers():
    """Two disjoint square outers union into a MultiPolygon, both get extruded."""
    pattern = FlatPattern(
        outer_contour=[
            [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
            [(20.0, 0.0), (30.0, 0.0), (30.0, 10.0), (20.0, 10.0)],
        ],
        holes=[],
        bend_lines=[],
        thickness=1.5,
        bbox=(0.0, 0.0, 30.0, 10.0),
        units="mm",
        part_name="TWO_SQUARES",
    )
    data = unfolded_glb(pattern)
    assert data is not None
    _assert_valid_glb(data)


def test_unfolded_glb_clamps_zero_thickness_to_minimum():
    """``thickness`` of 0 falls back to the 0.1 mm minimum so the extrude works."""
    pattern = FlatPattern(
        outer_contour=[_square(10.0)],
        holes=[],
        bend_lines=[],
        thickness=0.0,  # the max(..., 0.1) branch should kick in
        bbox=(0.0, 0.0, 10.0, 10.0),
        units="mm",
        part_name="ZERO_T",
    )
    data = unfolded_glb(pattern)
    assert data is not None
    _assert_valid_glb(data)


# ---------------------------------------------------------------------------
# Internal mesh -> GLB helper
# ---------------------------------------------------------------------------


def test_mesh_to_glb_empty_arrays_fall_back_to_unit_box():
    """Empty vertex/face arrays trigger the unit-cube fallback so output is valid."""
    import numpy as np

    data = step_to_glb._mesh_to_glb(
        np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    )
    _assert_valid_glb(data)
