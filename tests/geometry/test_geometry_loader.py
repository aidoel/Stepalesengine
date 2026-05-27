"""Tests for the STEP geometry loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from manufacturing_pipeline.geometry.geometry_loader import (
    _canonicalise_unit,
    _explode_to_solids,
    _scale_shape,
    detect_step_units,
    load_solids,
    write_step,
)

# ---------------------------------------------------------------------------
# Helpers for synthesising minimal STEP text.
#
# These are deliberately hand-crafted: they need only enough envelope for the
# text-fallback parser in ``detect_step_units`` to find the SI_UNIT /
# CONVERSION_BASED_UNIT line. OCP's STEPControl_Reader will typically refuse
# to fully parse them (Unresolved Reference etc.), which is exactly the case
# the text fallback exists to handle.
# ---------------------------------------------------------------------------

_STEP_HEADER = (
    "ISO-10303-21;\n"
    "HEADER;\n"
    "FILE_DESCRIPTION((''),'2;1');\n"
    "FILE_NAME('t.stp','2020',('a'),('b'),'pre','sys','auth');\n"
    "FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\n"
    "ENDSEC;\n"
    "DATA;\n"
)
_STEP_FOOTER = "ENDSEC;\nEND-ISO-10303-21;\n"


def _write_step_text(path: Path, body: str) -> Path:
    path.write_text(_STEP_HEADER + body + _STEP_FOOTER, encoding="utf-8")
    return path


def test_load_solids_raises_filenotfound_on_missing_path(tmp_path: Path):
    missing = tmp_path / "definitely_does_not_exist.step"
    with pytest.raises(FileNotFoundError):
        load_solids(missing)


def test_round_trip_box_through_step(tmp_path: Path):
    """Round-trip a unit box through a STEP file and back."""

    box = BRepPrimAPI_MakeBox(20.0, 15.0, 5.0).Solid()
    out_path = tmp_path / "box.step"
    write_step(box, out_path)
    assert out_path.exists() and out_path.stat().st_size > 0

    solids = load_solids(out_path)
    assert len(solids) >= 1
    # Each returned shape should be a SOLID by topological type.
    from OCP.TopAbs import TopAbs_SOLID

    for s in solids:
        assert s.ShapeType() == TopAbs_SOLID


def test_detect_step_units_mm_for_default_export(tmp_path: Path):
    """Default OCP export writes SI_UNIT(.MILLI.,.METRE.) - should detect 'mm'."""

    box = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Solid()
    out_path = tmp_path / "mm.step"
    write_step(box, out_path)
    assert detect_step_units(out_path) == "mm"


def test_detect_step_units_returns_unknown_on_missing_file(tmp_path: Path):
    p = tmp_path / "nope.step"
    assert detect_step_units(p) == "unknown"


def test_load_solids_no_heal(tmp_path: Path):
    """heal=False path still returns the same number of solids."""

    box = BRepPrimAPI_MakeBox(8.0, 8.0, 8.0).Solid()
    out_path = tmp_path / "noheal.step"
    write_step(box, out_path)
    solids = load_solids(out_path, heal=False)
    assert len(solids) == 1


# ---------------------------------------------------------------------------
# detect_step_units: text-fallback parser
#
# These tests target the ``SI_UNIT(...)`` / ``CONVERSION_BASED_UNIT(...)``
# regex branch in ``detect_step_units``. They use minimal hand-crafted STEP
# envelopes - just enough HEADER/DATA scaffolding for the read step to fall
# through to the text scan. This is the path that protects every downstream
# flat-pattern from silent unit rescaling, so it deserves direct coverage.
# ---------------------------------------------------------------------------


def test_detect_step_units_milli_metre_from_text(tmp_path: Path):
    """SI_UNIT(.MILLI.,.METRE.) in body -> 'mm'."""

    p = _write_step_text(tmp_path / "milli.stp", "#1 = SI_UNIT(.MILLI.,.METRE.);\n")
    assert detect_step_units(p) == "mm"


def test_detect_step_units_centi_metre_from_text(tmp_path: Path):
    """SI_UNIT(.CENTI.,.METRE.) -> 'cm'."""

    p = _write_step_text(tmp_path / "centi.stp", "#1 = SI_UNIT(.CENTI.,.METRE.);\n")
    assert detect_step_units(p) == "cm"


def test_detect_step_units_plain_metre_from_text(tmp_path: Path):
    """SI_UNIT(,.METRE.) with empty prefix slot -> 'm'.

    Matches the function's documented contract: when the prefix capture is
    empty but the METRE token is present, it returns the bare metre unit.
    A truly prefix-less ``SI_UNIT(.METRE.)`` (no comma) is NOT recognised
    by the regex - this test pins that distinction.
    """

    p = _write_step_text(tmp_path / "metre.stp", "#1 = SI_UNIT(,.METRE.);\n")
    assert detect_step_units(p) == "m"


def test_detect_step_units_conversion_inch_from_text(tmp_path: Path):
    """CONVERSION_BASED_UNIT('INCH', ...) -> 'inch'."""

    body = "#1 = CONVERSION_BASED_UNIT('INCH',#2);\n"
    p = _write_step_text(tmp_path / "inch.stp", body)
    assert detect_step_units(p) == "inch"


def test_detect_step_units_conversion_foot_from_text(tmp_path: Path):
    """CONVERSION_BASED_UNIT('FOOT', ...) -> 'foot'."""

    body = "#1 = CONVERSION_BASED_UNIT('FOOT',#2);\n"
    p = _write_step_text(tmp_path / "foot.stp", body)
    assert detect_step_units(p) == "foot"


def test_detect_step_units_returns_unknown_when_no_unit_declared(tmp_path: Path):
    """No SI_UNIT or CONVERSION_BASED_UNIT anywhere -> 'unknown' (no raise)."""

    body = "#1 = CARTESIAN_POINT('',(0.,0.,0.));\n"
    p = _write_step_text(tmp_path / "no_units.stp", body)
    # Function never raises; its documented default for unresolved units is
    # the literal string 'unknown'.
    assert detect_step_units(p) == "unknown"


def test_detect_step_units_returns_unknown_on_empty_file(tmp_path: Path):
    """Empty file: both OCP and text fallback should give up gracefully."""

    p = tmp_path / "empty.stp"
    p.write_text("", encoding="utf-8")
    assert detect_step_units(p) == "unknown"


# ---------------------------------------------------------------------------
# _canonicalise_unit: exhaustive coverage of the lookup table branches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("mm", "mm"),
        ("MILLIMETRE", "mm"),
        ("millimeter", "mm"),
        ("milli metre", "mm"),
        ("m", "m"),
        ("metre", "m"),
        ("Meter", "m"),
        ("cm", "cm"),
        ("CENTIMETRE", "cm"),
        ("centimeter", "cm"),
        ("inch", "inch"),
        ("INCHES", "inch"),
        ("in", "inch"),
        ("foot", "foot"),
        ("FEET", "foot"),
        ("ft", "foot"),
        ("furlong", "unknown"),
        ("", "unknown"),
    ],
)
def test_canonicalise_unit_branches(raw: str, expected: str):
    assert _canonicalise_unit(raw) == expected


# ---------------------------------------------------------------------------
# _explode_to_solids: walking compounds + degenerate inputs
# ---------------------------------------------------------------------------


def test_explode_to_solids_handles_none_and_null():
    """``None`` and a null TopoDS_Shape both yield an empty list."""

    from OCP.TopoDS import TopoDS_Shape

    assert _explode_to_solids(None) == []
    assert _explode_to_solids(TopoDS_Shape()) == []


def test_explode_to_solids_returns_single_solid_input_unchanged():
    """A bare TopoDS_Solid input is returned as one entry."""

    box = BRepPrimAPI_MakeBox(5.0, 5.0, 5.0).Solid()
    out = _explode_to_solids(box)
    assert len(out) == 1
    from OCP.TopAbs import TopAbs_SOLID

    assert out[0].ShapeType() == TopAbs_SOLID


def test_explode_to_solids_walks_compound_of_two_solids():
    """A compound of two distinct solids is exploded into two entries."""

    from OCP.BRep import BRep_Builder
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopoDS import TopoDS_Compound

    a = BRepPrimAPI_MakeBox(5.0, 5.0, 5.0).Solid()
    b = BRepPrimAPI_MakeBox(3.0, 3.0, 3.0).Solid()

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, a)
    builder.Add(compound, b)

    out = _explode_to_solids(compound)
    assert len(out) == 2
    assert all(s.ShapeType() == TopAbs_SOLID for s in out)


# ---------------------------------------------------------------------------
# _scale_shape and load_solids with non-default target_units
# ---------------------------------------------------------------------------


def test_scale_shape_identity_short_circuits_when_factor_is_one():
    """factor==1.0 returns the same object without invoking BRep transform."""

    box = BRepPrimAPI_MakeBox(4.0, 4.0, 4.0).Solid()
    assert _scale_shape(box, 1.0) is box


def test_scale_shape_scales_bbox_by_factor():
    """A 2x scale doubles every axis-aligned bounding-box dimension."""

    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Solid()
    scaled = _scale_shape(box, 2.0)

    bb = Bnd_Box()
    BRepBndLib.Add_s(scaled, bb)
    xmin, ymin, zmin, xmax, ymax, zmax = bb.Get()
    assert (xmax - xmin) == pytest.approx(20.0, rel=1e-6)
    assert (ymax - ymin) == pytest.approx(40.0, rel=1e-6)
    assert (zmax - zmin) == pytest.approx(60.0, rel=1e-6)


def test_load_solids_with_inch_target_scales_down(tmp_path: Path):
    """target_units='inch' divides mm-default geometry by 25.4."""

    box = BRepPrimAPI_MakeBox(25.4, 25.4, 25.4).Solid()
    out_path = tmp_path / "to_inch.step"
    write_step(box, out_path)

    solids = load_solids(out_path, heal=False, target_units="inch")
    assert len(solids) == 1

    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    bb = Bnd_Box()
    BRepBndLib.Add_s(solids[0], bb)
    xmin, ymin, zmin, xmax, ymax, zmax = bb.Get()
    # 25.4 mm input -> 1 inch when target_units='inch'.
    assert (xmax - xmin) == pytest.approx(1.0, rel=1e-4)
    assert (ymax - ymin) == pytest.approx(1.0, rel=1e-4)
    assert (zmax - zmin) == pytest.approx(1.0, rel=1e-4)


def test_load_solids_returns_empty_on_unreadable_step(tmp_path: Path):
    """A file that exists but is not a valid STEP -> [] (no raise)."""

    p = tmp_path / "garbage.step"
    p.write_text("this is not a STEP file at all\n", encoding="utf-8")
    assert load_solids(p, heal=False) == []
