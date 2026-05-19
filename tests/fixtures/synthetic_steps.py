"""Helpers that build small STEP files on disk for integration tests.

Each builder uses real OCP primitives and writes a valid STEP file (Part 21)
that the full pipeline can ingest. Builders return the written path, so tests
can chain them with ``tmp_path``::

    step = write_flat_plate_step(tmp_path / "plate.step", l=200, w=100, t=3)
    rc = manufacturing_pipeline.cli.main(["analyze", str(step), "--out-dir", str(out_dir)])

All geometry is in millimetres, defined relative to the world origin so the
solids do not overlap. The assembly writer uses ``STEPCAFControl_Writer`` so
named parts round-trip via the XCAF document.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

# ---------------------------------------------------------------------------
# Single-solid builders
# ---------------------------------------------------------------------------


def _make_flat_plate_solid(
    l: float,  # noqa: E741 - plate length, domain convention
    w: float,
    t: float,
    holes: Sequence[tuple[float, float, float]] | None,
):
    """Return a TopoDS_Solid for a flat rectangular plate, optionally drilled.

    Coordinates: plate spans X=[0..l], Y=[0..w], Z=[0..t]; holes are through-holes
    in the +Z direction at (cx, cy) with the given diameter.
    """
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    plate = BRepPrimAPI_MakeBox(float(l), float(w), float(t)).Solid()
    if not holes:
        return plate

    current = plate
    for cx, cy, dia in holes:
        radius = float(dia) / 2.0
        # Start the cylinder a hair below z=0 and extend past z=t, so the cut
        # is unambiguous through the full thickness.
        axis = gp_Ax2(gp_Pnt(float(cx), float(cy), -1.0), gp_Dir(0.0, 0.0, 1.0))
        cyl = BRepPrimAPI_MakeCylinder(axis, radius, float(t) + 2.0).Solid()
        cut = BRepAlgoAPI_Cut(current, cyl)
        cut.Build()
        if not cut.IsDone():
            raise RuntimeError(f"hole cut failed at ({cx}, {cy}, {dia})")
        current = cut.Shape()
    return current


def _make_lbracket_solid(
    leg_a: float, leg_b: float, t: float, bend_radius: float, width: float = 30.0
):
    """Build an L-bracket as a real sheet-metal section.

    The cross-section is constructed in the XZ-plane with constant thickness
    ``t`` and an inner fillet of radius ``bend_radius`` joining the two flanges.
    The face is then prismed along +Y over ``width`` to produce the solid.

    NOTE: this geometry yields ``UnfoldStatus.SUCCESS`` from the unfold probe,
    but - because of how the probe selects its BFS base versus where the bend
    is in the outer-skin chain - ``n_bends`` can come back as 0 even though the
    physical part has one bend. See the test module for the corresponding
    ``xfail`` marker.
    """
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeEdge,
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakeWire,
    )
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt, gp_Vec

    la = float(leg_a)
    lb = float(leg_b)
    th = float(t)
    br = float(bend_radius)

    # Geometry in the XZ-plane, Y = 0. The inner & outer bend centres coincide
    # at (t + br, 0, t + br); inner radius = br, outer radius = t + br.
    inner_ax = gp_Ax2(gp_Pnt(th + br, 0.0, th + br), gp_Dir(0.0, 1.0, 0.0))
    inner_circle = gp_Circ(inner_ax, br)
    arc_inner = GC_MakeArcOfCircle(
        inner_circle, gp_Pnt(th, 0.0, th + br), gp_Pnt(th + br, 0.0, th), True
    ).Value()

    outer_ax = gp_Ax2(gp_Pnt(th + br, 0.0, th + br), gp_Dir(0.0, 1.0, 0.0))
    outer_circle = gp_Circ(outer_ax, th + br)
    arc_outer = GC_MakeArcOfCircle(
        outer_circle, gp_Pnt(th + br, 0.0, 0.0), gp_Pnt(0.0, 0.0, th + br), True
    ).Value()

    def _line(a, b):
        return BRepBuilderAPI_MakeEdge(a, b).Edge()

    edges = [
        _line(gp_Pnt(0.0, 0.0, lb), gp_Pnt(th, 0.0, lb)),  # top of vertical flange
        _line(gp_Pnt(th, 0.0, lb), gp_Pnt(th, 0.0, th + br)),  # inner right of vertical
        BRepBuilderAPI_MakeEdge(arc_inner).Edge(),  # inner bend arc
        _line(gp_Pnt(th + br, 0.0, th), gp_Pnt(la, 0.0, th)),  # inner top of horizontal
        _line(gp_Pnt(la, 0.0, th), gp_Pnt(la, 0.0, 0.0)),  # right end of horizontal
        _line(gp_Pnt(la, 0.0, 0.0), gp_Pnt(th + br, 0.0, 0.0)),  # outer bottom of horizontal
        BRepBuilderAPI_MakeEdge(arc_outer).Edge(),  # outer bend arc
        _line(gp_Pnt(0.0, 0.0, th + br), gp_Pnt(0.0, 0.0, lb)),  # outer left of vertical
    ]

    wm = BRepBuilderAPI_MakeWire()
    for e in edges:
        wm.Add(e)
    if not wm.IsDone():
        raise RuntimeError("L-bracket wire build failed")

    face = BRepBuilderAPI_MakeFace(wm.Wire(), True).Face()
    prism = BRepPrimAPI_MakePrism(face, gp_Vec(0.0, float(width), 0.0)).Shape()

    # Extract the single solid from the resulting shape.
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    exp = TopExp_Explorer(prism, TopAbs_SOLID)
    if not exp.More():
        raise RuntimeError("L-bracket prism produced no solids")
    return TopoDS.Solid_s(exp.Current())


def _make_rhs_solid(h: float, b: float, t: float, length: float):
    """Build a rectangular hollow section (RHS) prism along +Z.

    Cross-section spans X=[0..b], Y=[0..h], Z=0, extruded to Z=length.
    A constant wall thickness ``t`` is achieved by sinking an inner rectangle.
    """
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakePolygon,
    )
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.TopoDS import TopoDS

    def _polygon(pts):
        mp = BRepBuilderAPI_MakePolygon()
        for x, y in pts:
            mp.Add(gp_Pnt(float(x), float(y), 0.0))
        mp.Close()
        return mp.Wire()

    outer = _polygon([(0, 0), (b, 0), (b, h), (0, h)])
    inner = _polygon([(t, t), (b - t, t), (b - t, h - t), (t, h - t)])

    fm = BRepBuilderAPI_MakeFace(outer, True)
    fm.Add(TopoDS.Wire_s(inner.Reversed()))
    face = fm.Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(0.0, 0.0, float(length))).Shape()


def _make_chs_solid(d: float, t: float, length: float):
    """Build a circular hollow section (CHS) prism along +Z."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    ax = gp_Ax2(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0))
    outer = BRepPrimAPI_MakeCylinder(ax, float(d) / 2.0, float(length)).Solid()
    inner = BRepPrimAPI_MakeCylinder(ax, float(d) / 2.0 - float(t), float(length)).Solid()
    cut = BRepAlgoAPI_Cut(outer, inner)
    cut.Build()
    if not cut.IsDone():
        raise RuntimeError("CHS cut failed")
    return cut.Shape()


def _make_profile_solid(family: str, h: float, b: float, t: float, length: float):
    """Dispatcher for hollow-profile families."""
    fam = (family or "").upper()
    if fam == "CHS":
        return _make_chs_solid(d=max(h, b), t=t, length=length)
    if fam == "SHS":
        side = max(h, b)
        return _make_rhs_solid(h=side, b=side, t=t, length=length)
    if fam == "RHS":
        return _make_rhs_solid(h=h, b=b, t=t, length=length)
    raise ValueError(f"unsupported profile family: {family!r}")


# ---------------------------------------------------------------------------
# STEP writers
# ---------------------------------------------------------------------------


def _write_step_single(solid, path: Path) -> Path:
    """Write a single solid to a STEP file via STEPControl_Writer."""
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    writer = STEPControl_Writer()
    rc = writer.Transfer(solid, STEPControl_StepModelType.STEPControl_AsIs)
    if rc != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise RuntimeError(f"STEPControl_Writer.Transfer failed: {rc}")
    wrc = writer.Write(str(p))
    if wrc != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise RuntimeError(f"STEPControl_Writer.Write failed: {wrc}")
    return p


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------


def write_flat_plate_step(
    path: Path,
    *,
    l: float = 100.0,  # noqa: E741 - plate length, domain convention
    w: float = 60.0,
    t: float = 2.0,
    with_holes: Iterable[tuple[float, float, float]] | None = None,
) -> Path:
    """Write a STEP file containing a single flat rectangular plate.

    Optionally adds through-holes at given ``(cx, cy, diameter)`` positions on
    the top face. Returns the resolved path.
    """
    holes = list(with_holes) if with_holes else None
    solid = _make_flat_plate_solid(l=l, w=w, t=t, holes=holes)
    return _write_step_single(solid, Path(path))


def write_lbracket_step(
    path: Path,
    *,
    leg_a: float = 50.0,
    leg_b: float = 40.0,
    t: float = 2.0,
    bend_radius: float = 3.0,
    width: float = 30.0,
) -> Path:
    """Write a STEP file containing a single sheet-metal L-bracket.

    The bracket consists of two flat flanges of thickness ``t`` joined by a
    single 90 degree bend with the given inner radius. ``width`` is the
    extrusion length perpendicular to the bend axis.
    """
    solid = _make_lbracket_solid(
        leg_a=leg_a, leg_b=leg_b, t=t, bend_radius=bend_radius, width=width
    )
    return _write_step_single(solid, Path(path))


def write_profile_step(
    path: Path,
    *,
    family: str = "RHS",
    h: float = 100.0,
    b: float = 60.0,
    t: float = 4.0,
    length: float = 1000.0,
) -> Path:
    """Write a STEP file containing a single hollow profile (RHS/SHS/CHS).

    For ``CHS`` use ``h=b=outer_diameter``; the profile is created via
    closed-wire to face to prism.
    """
    solid = _make_profile_solid(family=family, h=h, b=b, t=t, length=length)
    return _write_step_single(solid, Path(path))


def write_assembly_step(
    path: Path,
    *,
    parts: Sequence[tuple[str, object]] | None = None,
) -> Path:
    """Write a STEP file containing multiple named solids as a compound assembly.

    Each entry is ``(part_name, solid)``. The writer uses
    ``STEPCAFControl_Writer`` plus an XCAF document, so the part names ride
    along on ``TDataStd_Name`` attributes and round-trip through the parser.
    """
    if not parts:
        raise ValueError("write_assembly_step needs at least one (name, solid) entry")

    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("XCAF"))
    app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc)
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    for name, solid in parts:
        label = shape_tool.AddShape(solid, False)
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(str(name)))

    writer = STEPCAFControl_Writer()
    if not writer.Transfer(doc):
        raise RuntimeError("STEPCAFControl_Writer.Transfer(doc) returned false")

    from OCP.IFSelect import IFSelect_ReturnStatus

    wrc = writer.Write(str(p))
    if wrc != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise RuntimeError(f"STEPCAFControl_Writer.Write failed: {wrc}")
    return p


# ---------------------------------------------------------------------------
# Convenience builders used by the assembly test
# ---------------------------------------------------------------------------


def build_plate_solid(*, l: float = 100.0, w: float = 60.0, t: float = 2.0):  # noqa: E741
    """Return a TopoDS_Solid for a plate, without writing to disk."""
    return _make_flat_plate_solid(l=l, w=w, t=t, holes=None)


def build_lbracket_solid(
    *,
    leg_a: float = 50.0,
    leg_b: float = 40.0,
    t: float = 2.0,
    bend_radius: float = 3.0,
    width: float = 30.0,
):
    """Return a TopoDS_Solid for an L-bracket, without writing to disk."""
    return _make_lbracket_solid(leg_a=leg_a, leg_b=leg_b, t=t, bend_radius=bend_radius, width=width)


def build_profile_solid(
    *,
    family: str = "RHS",
    h: float = 100.0,
    b: float = 60.0,
    t: float = 4.0,
    length: float = 1000.0,
):
    """Return a TopoDS shape for a hollow profile, without writing to disk."""
    return _make_profile_solid(family=family, h=h, b=b, t=t, length=length)


def translate_solid(solid, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0):
    """Return ``solid`` translated by (dx, dy, dz)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf, gp_Vec

    trsf = gp_Trsf()
    trsf.SetTranslation(gp_Vec(float(dx), float(dy), float(dz)))
    xform = BRepBuilderAPI_Transform(solid, trsf, True)
    xform.Build()
    return xform.Shape()


# ---------------------------------------------------------------------------
# Tessellated-only builders (no Geom_Surface on any face -> mesh fallback)
# ---------------------------------------------------------------------------


def _build_tessellated_solid(verts, tri_faces):
    """Assemble a TopoDS_Solid from a vertex list and triangle index list.

    Each triangle becomes a single :class:`TopoDS_Face` carrying a
    :class:`Poly_Triangulation` and *no* :class:`Geom_Surface`. The faces are
    sewn into a shell and the shell becomes the solid. This is the only way
    we can synthesise a tessellation-only solid: real CAD-emitted STEP files
    that contain ``TESSELLATED_SOLID`` round-trip through OCCT into the same
    shape.
    """

    from OCP.BRep import BRep_Builder
    from OCP.gp import gp_Pnt
    from OCP.Poly import Poly_Array1OfTriangle, Poly_Triangle, Poly_Triangulation
    from OCP.TColgp import TColgp_Array1OfPnt
    from OCP.TopoDS import TopoDS_Face, TopoDS_Shell, TopoDS_Solid

    bb = BRep_Builder()
    shell = TopoDS_Shell()
    bb.MakeShell(shell)

    for i, j, k in tri_faces:
        nodes = TColgp_Array1OfPnt(1, 3)
        nodes.SetValue(1, gp_Pnt(*verts[i]))
        nodes.SetValue(2, gp_Pnt(*verts[j]))
        nodes.SetValue(3, gp_Pnt(*verts[k]))
        tris = Poly_Array1OfTriangle(1, 1)
        tris.SetValue(1, Poly_Triangle(1, 2, 3))
        triangulation = Poly_Triangulation(nodes, tris)
        face = TopoDS_Face()
        bb.MakeFace(face, triangulation)
        bb.Add(shell, face)

    solid = TopoDS_Solid()
    bb.MakeSolid(solid)
    bb.Add(solid, shell)
    return solid


def _octahedron(size: float):
    """Vertices & triangles of a regular octahedron centred at the origin.

    The vertex-to-centre distance is ``size / 2`` so the AABB matches a cube
    of side ``size``.
    """

    h = size / 2.0
    verts = [
        (h, 0.0, 0.0),
        (-h, 0.0, 0.0),
        (0.0, h, 0.0),
        (0.0, -h, 0.0),
        (0.0, 0.0, h),
        (0.0, 0.0, -h),
    ]
    # 8 outward-pointing triangles, 4 around the +Z apex, 4 around -Z.
    tri_faces = [
        (0, 2, 4),
        (2, 1, 4),
        (1, 3, 4),
        (3, 0, 4),
        (2, 0, 5),
        (1, 2, 5),
        (3, 1, 5),
        (0, 3, 5),
    ]
    return verts, tri_faces


def _tetrahedron(size: float):
    """A regular tetrahedron inscribed in a cube of side ``size``."""

    verts = [
        (0.0, 0.0, 0.0),
        (size, size, 0.0),
        (size, 0.0, size),
        (0.0, size, size),
    ]
    tri_faces = [
        (0, 1, 2),
        (0, 3, 1),
        (0, 2, 3),
        (1, 3, 2),
    ]
    return verts, tri_faces


def _cube(size: float):
    """A cube of side ``size``, decomposed into 12 outward-facing triangles.

    Each quad face is split into two triangles. Winding order is CCW seen
    from outside so divergence-theorem volume comes out positive.
    """

    s = float(size)
    verts = [
        (0.0, 0.0, 0.0),
        (s, 0.0, 0.0),
        (s, s, 0.0),
        (0.0, s, 0.0),
        (0.0, 0.0, s),
        (s, 0.0, s),
        (s, s, s),
        (0.0, s, s),
    ]
    tri_faces = [
        # z=0 bottom (normal -Z)
        (0, 3, 2),
        (0, 2, 1),
        # z=size top (normal +Z)
        (4, 5, 6),
        (4, 6, 7),
        # y=0 front (normal -Y)
        (0, 1, 5),
        (0, 5, 4),
        # y=size back (normal +Y)
        (2, 3, 7),
        (2, 7, 6),
        # x=0 left (normal -X)
        (0, 4, 7),
        (0, 7, 3),
        # x=size right (normal +X)
        (1, 2, 6),
        (1, 6, 5),
    ]
    return verts, tri_faces


def make_tessellated_solid(shape: str = "octahedron", size: float = 50.0):
    """Return a :class:`TopoDS_Solid` whose faces have NO ``Geom_Surface``.

    Each face carries a :class:`Poly_Triangulation` instead. This forces the
    :class:`FeatureExtractor` onto the mesh-only fallback path: real CAD
    systems sometimes export STEP files with ``TESSELLATED_SOLID`` entities
    that round-trip into exactly this configuration.

    Parameters
    ----------
    shape:
        One of ``"octahedron"``, ``"cube"``, ``"tetrahedron"``.
    size:
        Characteristic dimension in millimetres. For ``cube`` it is the side
        length (so volume ~= ``size**3``). For ``octahedron`` it is the
        diameter (the AABB matches a cube of side ``size``). For
        ``tetrahedron`` it is the cube the tetrahedron is inscribed in.
    """

    s = (shape or "").lower()
    if s == "octahedron":
        verts, tri_faces = _octahedron(size)
    elif s == "cube":
        verts, tri_faces = _cube(size)
    elif s == "tetrahedron":
        verts, tri_faces = _tetrahedron(size)
    else:
        raise ValueError(
            f"unsupported tessellated shape: {shape!r}; expected octahedron|cube|tetrahedron"
        )
    return _build_tessellated_solid(verts, tri_faces)


def write_tessellated_step(path: Path, *, shape: str = "cube", size: float = 50.0) -> Path:
    """Write a tessellated solid to a STEP file via ``STEPControl_Writer``.

    OCCT emits ``TESSELLATED_SOLID`` / ``TRIANGULATED_FACE`` entities for these
    surfaceless solids; on read-back every face still has ``BRep_Tool.Surface``
    returning null, so the FeatureExtractor's mesh fallback is exercised.
    """

    solid = make_tessellated_solid(shape=shape, size=size)
    return _write_step_single(solid, Path(path))


__all__ = [
    "write_flat_plate_step",
    "write_lbracket_step",
    "write_profile_step",
    "write_assembly_step",
    "write_tessellated_step",
    "build_plate_solid",
    "build_lbracket_solid",
    "build_profile_solid",
    "make_tessellated_solid",
    "translate_solid",
]
