"""Tests for ``manufacturing_pipeline.web.step_to_svg``.

The HLR projector + solid matcher are exercised end-to-end against a real
synthetic STEP file (a 10x10x2 box) so the SVG output, the cache, and the
``find_solid_for_part`` lookup all run on actual OCCT geometry.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ``OCP`` is the OCCT Python binding the renderer needs. Skip the whole
# module when it is unavailable so the rest of the suite keeps working.
pytest.importorskip("OCP")

from manufacturing_pipeline.web.step_to_svg import (  # noqa: E402
    find_solid_for_part,
    render_part_views,
)
from tests.fixtures.synthetic_steps import (  # noqa: E402
    build_plate_solid,
    write_assembly_step,
    write_flat_plate_step,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def box_step(tmp_path: Path) -> Path:
    """A single-solid STEP file containing a 10x10x2 box."""
    return write_flat_plate_step(tmp_path / "box.step", l=10.0, w=10.0, t=2.0)


@pytest.fixture
def named_assembly_step(tmp_path: Path) -> tuple[Path, str]:
    """An assembly STEP with one named plate; returns (path, known_product_id)."""
    product_id = "test_plate"
    solid = build_plate_solid(l=10.0, w=10.0, t=2.0)
    path = write_assembly_step(tmp_path / "named.step", parts=[(product_id, solid)])
    return path, product_id


# ---------------------------------------------------------------------------
# render_part_views
# ---------------------------------------------------------------------------


def test_render_part_views_on_box_returns_svg_with_polylines(named_assembly_step):
    """A 10x10x2 box must render to a non-empty SVG containing a viewBox + polylines."""
    path, product_id = named_assembly_step
    out = render_part_views(path, product_id, views=("top",))

    assert isinstance(out, dict)
    assert "top" in out, f"top view missing from result: {list(out)}"
    svg = out["top"]

    # Structural assertions on the SVG payload.
    assert "<svg" in svg, "SVG root element missing"
    assert re.search(r'viewBox="[^"]+"', svg), "viewBox attribute missing"
    assert "<polyline" in svg, "no polyline geometry emitted"
    # Visible-edge stroke color used by the renderer.
    assert "#1a1a1a" in svg


def test_render_part_views_unknown_product_id_returns_empty_dict(box_step: Path):
    """When the part cannot be matched, render_part_views returns ``{}``."""
    out = render_part_views(box_step, "no-such-product", views=("top",))
    assert out == {}


def test_render_part_views_multiple_views(named_assembly_step):
    """Requesting multiple views returns one SVG per requested view."""
    path, product_id = named_assembly_step
    out = render_part_views(path, product_id, views=("top", "front", "side"))

    assert set(out.keys()) == {"top", "front", "side"}
    for view_name, svg in out.items():
        assert "<svg" in svg, f"view {view_name} missing <svg"
        assert view_name.upper() in svg, f"view label {view_name.upper()} missing in svg"


# ---------------------------------------------------------------------------
# find_solid_for_part
# ---------------------------------------------------------------------------


def test_find_solid_for_part_returns_solid_for_known_id(named_assembly_step):
    """A known product_id resolves to a non-None TopoDS_Solid."""
    from OCP.TopoDS import TopoDS_Shape

    path, product_id = named_assembly_step
    solid = find_solid_for_part(path, product_id)
    assert solid is not None
    # The matcher hands back a TopoDS shape (Solid or compound thereof).
    assert isinstance(solid, TopoDS_Shape)


def test_find_solid_for_part_returns_none_for_unknown_id(box_step: Path):
    """Unknown product_id yields ``None`` without raising."""
    result = find_solid_for_part(box_step, "no-such-id")
    assert result is None


def test_find_solid_for_part_missing_file_returns_none(tmp_path: Path):
    """A non-existent STEP path is handled gracefully (logged + None returned)."""
    missing = tmp_path / "does_not_exist.step"
    assert not missing.exists()
    result = find_solid_for_part(missing, "whatever")
    assert result is None


def test_render_part_views_missing_file_returns_empty_dict(tmp_path: Path):
    """A non-existent STEP path yields an empty dict (no exception)."""
    missing = tmp_path / "missing.step"
    out = render_part_views(missing, "x", views=("top",))
    assert out == {}


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


def test_load_assembly_caches_on_repeat_calls(named_assembly_step):
    """The internal cache makes the second _load_assembly call O(1)."""
    from manufacturing_pipeline.web import step_to_svg as mod

    path, product_id = named_assembly_step
    # Prime the cache.
    first = mod._load_assembly(path)
    second = mod._load_assembly(path)
    assert first is second, "expected the cache to return the same object"
    # Sanity: the product_id we put in shows up in the part_index.
    assert product_id in first.part_index
