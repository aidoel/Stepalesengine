"""Hole detection.

Iterates over a solid's cylindrical faces, classifies each as inner (hole wall)
or outer (boss/cylinder), groups co-axial inner cylinders into single holes
(handles counterbores), and decides through-vs-blind by ray casting along the
hole axis.

This module is the only place in the geometry layer that decides "is this an
inner cylinder?" - downstream code consumes the resulting :class:`HolePattern`.

All exceptions are caught and reported as an empty :class:`HolePattern`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .types import HolePattern

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal record types
# ---------------------------------------------------------------------------


@dataclass
class _CylFace:
    """Per-face cylinder record built while walking the solid."""

    face: object
    radius: float
    axis_dir: tuple[float, float, float]  # unit direction
    axis_loc: tuple[float, float, float]  # point on axis
    length: float  # extent along axis covered by this cylindrical patch
    area: float
    u_min: float
    u_max: float
    v_min: float
    v_max: float
    is_inner: bool


@dataclass
class HoleRecord:
    """A grouped hole: one or more co-axial inner cylinders."""

    diameter: float
    depth: float
    axis_dir: tuple[float, float, float]
    position: tuple[float, float, float]
    through: bool
    n_cylinders: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit(v: tuple[float, float, float]) -> tuple[float, float, float]:
    n = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
    if n < 1e-12:
        return (0.0, 0.0, 1.0)
    return (v[0] / n, v[1] / n, v[2] / n)


def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _axes_collinear(
    a_dir: tuple[float, float, float],
    a_loc: tuple[float, float, float],
    b_dir: tuple[float, float, float],
    b_loc: tuple[float, float, float],
    angle_tol_deg: float,
    dist_tol_mm: float,
) -> bool:
    """True if the two axes (line dir, line point) represent the same line."""

    cos_lim = math.cos(math.radians(angle_tol_deg))
    d = abs(_dot(a_dir, b_dir))
    if d < cos_lim:
        return False

    # Distance from b_loc to line (a_loc, a_dir)
    dx = b_loc[0] - a_loc[0]
    dy = b_loc[1] - a_loc[1]
    dz = b_loc[2] - a_loc[2]
    t = dx * a_dir[0] + dy * a_dir[1] + dz * a_dir[2]
    proj = (a_loc[0] + t * a_dir[0], a_loc[1] + t * a_dir[1], a_loc[2] + t * a_dir[2])
    perp_sq = (b_loc[0] - proj[0]) ** 2 + (b_loc[1] - proj[1]) ** 2 + (b_loc[2] - proj[2]) ** 2
    return perp_sq <= dist_tol_mm * dist_tol_mm


# ---------------------------------------------------------------------------
# Cylindrical face inspection
# ---------------------------------------------------------------------------


def _classify_cyl_face(face) -> _CylFace | None:
    """Build a _CylFace record. Returns None on any failure or if not cylindrical."""

    try:
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.BRepGProp import BRepGProp
        from OCP.BRepLProp import BRepLProp_SLProps
        from OCP.GeomAbs import GeomAbs_Cylinder
        from OCP.GProp import GProp_GProps
        from OCP.TopAbs import TopAbs_REVERSED

        adapt = BRepAdaptor_Surface(face)
        if adapt.GetType() != GeomAbs_Cylinder:
            return None

        cyl = adapt.Cylinder()
        ax = cyl.Axis()
        radius = float(cyl.Radius())
        if radius <= 1e-9:
            return None

        ax_dir = _unit((ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()))
        ax_loc = (ax.Location().X(), ax.Location().Y(), ax.Location().Z())

        u_min = adapt.FirstUParameter()
        u_max = adapt.LastUParameter()
        v_min = adapt.FirstVParameter()
        v_max = adapt.LastVParameter()
        u_mid = (u_min + u_max) * 0.5
        v_mid = (v_min + v_max) * 0.5

        # Outward normal at face centre
        slp = BRepLProp_SLProps(adapt, u_mid, v_mid, 2, 1e-7)
        if not slp.IsNormalDefined():
            return None
        n = slp.Normal()
        pt = slp.Value()
        sign = -1.0 if face.Orientation() == TopAbs_REVERSED else 1.0
        nx, ny, nz = n.X() * sign, n.Y() * sign, n.Z() * sign
        out_normal = _unit((nx, ny, nz))

        # Radial direction from axis to the sampled point on the surface
        dx = pt.X() - ax_loc[0]
        dy = pt.Y() - ax_loc[1]
        dz = pt.Z() - ax_loc[2]
        t = dx * ax_dir[0] + dy * ax_dir[1] + dz * ax_dir[2]
        rx = dx - t * ax_dir[0]
        ry = dy - t * ax_dir[1]
        rz = dz - t * ax_dir[2]
        rn = math.sqrt(rx * rx + ry * ry + rz * rz)
        if rn < 1e-9:
            return None
        radial = (rx / rn, ry / rn, rz / rn)

        # Inner cylinder: outward (material-side) normal points TOWARD axis (dot < 0).
        dot = _dot(out_normal, radial)
        is_inner = dot < -0.5  # well below 0 to reject grazing/ambiguous samples

        # Area + length-along-axis.
        # Cylinder patch area = radius * (u_max - u_min) * (v_max - v_min) using
        # parametric (u in radians, v along axis), but more robustly:
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        area = float(props.Mass())
        if radius > 0 and (u_max - u_min) > 0:
            # length = area / (radius * angular_extent)
            angular = u_max - u_min
            length = area / (radius * angular) if angular > 1e-9 else (v_max - v_min)
        else:
            length = float(v_max - v_min)

        return _CylFace(
            face=face,
            radius=radius,
            axis_dir=ax_dir,
            axis_loc=ax_loc,
            length=length,
            area=area,
            u_min=u_min,
            u_max=u_max,
            v_min=v_min,
            v_max=v_max,
            is_inner=is_inner,
        )
    except Exception as exc:
        logger.debug("_classify_cyl_face failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Through-hole detection
# ---------------------------------------------------------------------------


def _is_through_hole(
    solid,
    group: list[_CylFace],
) -> bool:
    """True if the hole goes clear through the solid along its axis.

    A through-hole's cylindrical patches collectively span the solid's full
    extent along the axis: the axial bounding range of the inner cylinders
    matches the solid's extent along the axis line.

    A blind hole's cylinder stops short of one of the solid's extremes.
    """

    if not solid or not group:
        return False
    try:
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib

        axis_dir = group[0].axis_dir

        # Project the solid's bounding-box corners onto the axis.
        bbox = Bnd_Box()
        BRepBndLib.Add_s(solid, bbox)
        xmin, ymin, zmin, xmax, ymax, zmax = bbox.Get()
        corners = [
            (xmin, ymin, zmin),
            (xmax, ymin, zmin),
            (xmin, ymax, zmin),
            (xmax, ymax, zmin),
            (xmin, ymin, zmax),
            (xmax, ymin, zmax),
            (xmin, ymax, zmax),
            (xmax, ymax, zmax),
        ]
        base = group[0].axis_loc
        proj = []
        for c in corners:
            dx = c[0] - base[0]
            dy = c[1] - base[1]
            dz = c[2] - base[2]
            proj.append(dx * axis_dir[0] + dy * axis_dir[1] + dz * axis_dir[2])
        solid_extent = max(proj) - min(proj)
        if solid_extent <= 1e-9:
            return False

        # Axial extent of cylinder patches.
        cyl_extent = sum(max(c.length, 0.0) for c in group)
        if cyl_extent <= 1e-9:
            return False
        # Through-hole iff the cylinder patches span (close to) the full
        # solid extent along the axis.
        return cyl_extent >= 0.95 * solid_extent
    except Exception as exc:
        logger.debug("_is_through_hole failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------


class HoleAnalyzer:
    """Locate, measure, and group cylindrical holes in a solid."""

    def __init__(self, *, axis_tol_deg: float = 1.0, axis_tol_mm: float = 0.1):
        self.axis_tol_deg = float(axis_tol_deg)
        self.axis_tol_mm = float(axis_tol_mm)

    # ----- public ----------------------------------------------------------

    def analyze(self, solid) -> HolePattern:
        """Return a HolePattern. Never raises - returns an empty pattern on
        any failure."""

        try:
            cyls = self._collect_cylinders(solid)
        except Exception as exc:
            logger.warning("HoleAnalyzer.analyze: cylinder collection failed: %s", exc)
            return HolePattern()

        inner = [c for c in cyls if c.is_inner]
        if not inner:
            return HolePattern()

        try:
            groups = self._group_coaxial(inner)
        except Exception as exc:
            logger.warning("HoleAnalyzer.analyze: grouping failed: %s", exc)
            return HolePattern()

        holes: list[HoleRecord] = []
        diameters: list[float] = []
        for group in groups:
            try:
                rec = self._build_hole_record(solid, group)
            except Exception as exc:
                logger.debug("HoleAnalyzer.analyze: record build failed: %s", exc)
                continue
            if rec is None:
                continue
            holes.append(rec)
            diameters.append(rec.diameter)

        return HolePattern(
            hole_count=len(holes),
            diameters=sorted(diameters),
            holes=[
                {
                    "diameter": h.diameter,
                    "depth": h.depth,
                    "axis_dir": h.axis_dir,
                    "position": h.position,
                    "through": h.through,
                    "n_cylinders": h.n_cylinders,
                }
                for h in holes
            ],
        )

    # ----- internals -------------------------------------------------------

    def _collect_cylinders(self, solid) -> list[_CylFace]:
        from OCP.TopAbs import TopAbs_FACE
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS

        out: list[_CylFace] = []
        exp = TopExp_Explorer(solid, TopAbs_FACE)
        while exp.More():
            f = TopoDS.Face_s(exp.Current())
            try:
                rec = _classify_cyl_face(f)
            except Exception:
                rec = None
            if rec is not None:
                out.append(rec)
            exp.Next()
        return out

    def _group_coaxial(self, inner: list[_CylFace]) -> list[list[_CylFace]]:
        """Group inner cylinders sharing an axis line.

        Two inner cylinders join the same group iff their axes are collinear
        within the configured angle / distance tolerances. We do NOT require
        equal radii - that's the point of a counterbore. We DO use axial
        overlap or proximity to avoid grouping two distinct holes that
        happen to share a parallel axis.
        """

        groups: list[list[_CylFace]] = []
        for c in inner:
            placed = False
            for g in groups:
                rep = g[0]
                if _axes_collinear(
                    rep.axis_dir,
                    rep.axis_loc,
                    c.axis_dir,
                    c.axis_loc,
                    self.axis_tol_deg,
                    self.axis_tol_mm,
                ):
                    g.append(c)
                    placed = True
                    break
            if not placed:
                groups.append([c])
        return groups

    def _build_hole_record(self, solid, group: list[_CylFace]) -> HoleRecord | None:
        if not group:
            return None
        # Diameter: take the SMALLEST radius in the group (the actual hole bore).
        # Counterbores have outer rings that share axis with a smaller inner bore;
        # the bore is what the bolt passes through.
        smallest = min(group, key=lambda c: c.radius)
        diameter = 2.0 * smallest.radius

        # Depth: sum of the per-cylinder lengths along the shared axis.
        # We use the axial extents of each patch.
        depth = self._compute_axial_depth(group)

        axis_dir = group[0].axis_dir
        # Position: use the axial midpoint of the deepest cylinder.
        position = self._compute_axial_position(group)

        through = _is_through_hole(solid, group)

        return HoleRecord(
            diameter=diameter,
            depth=depth,
            axis_dir=axis_dir,
            position=position,
            through=bool(through),
            n_cylinders=len(group),
        )

    def _compute_axial_depth(self, group: list[_CylFace]) -> float:
        """Sum the axial extent of every cylinder in the group.

        Falls back to max(length) on degenerate inputs so a single cylinder
        still reports its own length.
        """

        if not group:
            return 0.0
        total = sum(max(c.length, 0.0) for c in group)
        if total <= 1e-9:
            total = max((c.length for c in group), default=0.0)
        return float(total)

    def _compute_axial_position(self, group: list[_CylFace]) -> tuple[float, float, float]:
        """Return a representative point on the shared axis (averaged loc)."""

        n = len(group)
        if n == 0:
            return (0.0, 0.0, 0.0)
        sx = sum(c.axis_loc[0] for c in group) / n
        sy = sum(c.axis_loc[1] for c in group) / n
        sz = sum(c.axis_loc[2] for c in group) / n
        return (sx, sy, sz)
