"""Hole detection.

Iterates over a solid's cylindrical faces, classifies each as inner (hole wall)
or outer (boss/cylinder), groups co-axial inner cylinders into single holes
(handles counterbores), and decides through-vs-blind by ray casting along the
hole axis.

A "hole" here means a genuine round bore: its cylindrical wall must wrap a
near-full circle (360 degrees, usually split into two 180-degree patches by the
surface seam). Partial-arc inner cylinders - the rounded corners of rectangular
cutouts, bend-relief notches, or the bend radii of folded flanges - are NOT
holes and are excluded from ``hole_count``. Co-axial bores separated by an axial
gap (two holes drilled on the same axis line through parallel flanges) are split
into one record each rather than merged. A countersunk/counterbored hole, whose
co-axial cylinders touch axially, still counts once.

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


# A genuine round hole's cylindrical wall wraps a full circle (2*pi). The seam
# of a closed cylindrical surface usually splits that wall into two 180-degree
# patches, so we sum the angular extent of the bore-tier patches and require it
# to reach (close to) a full turn. Partial-arc patches - rounded cutout corners,
# bend reliefs, flange bend radii - fall well short of this and are rejected.
_FULL_CIRCLE = 2.0 * math.pi
_MIN_HOLE_ANGULAR_COVERAGE = _FULL_CIRCLE - math.radians(20.0)

# Tolerance for treating two cylinder radii as the same bore tier.
_RADIUS_TIER_TOL_MM = 0.05


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


def _axial_span(cyl: _CylFace, base: tuple[float, float, float]) -> tuple[float, float]:
    """Return the cylinder patch's [lo, hi] extent along the shared axis.

    The patch covers ``axis_loc`` projected onto the axis, plus its v-parameter
    range (v runs along the cylinder axis). ``base`` is a point on the shared
    axis line used as the projection origin.
    """

    dx = cyl.axis_loc[0] - base[0]
    dy = cyl.axis_loc[1] - base[1]
    dz = cyl.axis_loc[2] - base[2]
    off = dx * cyl.axis_dir[0] + dy * cyl.axis_dir[1] + dz * cyl.axis_dir[2]
    lo = off + cyl.v_min
    hi = off + cyl.v_max
    return (min(lo, hi), max(lo, hi))


def _bore_angular_coverage(group: list[_CylFace]) -> float:
    """Total angular extent (radians) of the group's smallest-radius patches.

    The smallest radius is the bore - what a fastener passes through. A genuine
    hole's bore wall wraps a full circle; a rounded corner or bend relief only
    covers a partial arc.
    """

    if not group:
        return 0.0
    r_min = min(c.radius for c in group)
    bore = [c for c in group if c.radius - r_min <= _RADIUS_TIER_TOL_MM]
    return sum(max(c.u_max - c.u_min, 0.0) for c in bore)


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
        """Group inner cylinders into one record per genuine hole.

        First gather cylinders sharing an axis *line* (collinear within the
        configured tolerances). We do NOT require equal radii - that's the
        point of a counterbore. Then split each collinear cluster by axial
        gap: two bores drilled on the same axis line but through parallel
        flanges share a line yet are distinct holes, while the co-axial
        cylinders of a single counterbored hole touch or overlap axially.
        """

        lines: list[list[_CylFace]] = []
        for c in inner:
            placed = False
            for g in lines:
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
                lines.append([c])

        groups: list[list[_CylFace]] = []
        for line in lines:
            groups.extend(self._split_axial(line))
        return groups

    def _split_axial(self, line: list[_CylFace]) -> list[list[_CylFace]]:
        """Split collinear cylinders into axially-contiguous sub-groups.

        Cylinders join the same hole when their axial spans overlap or are
        separated by a gap no larger than the longest patch in the pair (so
        the touching tiers of a counterbore stay merged) - distinct holes on
        the same axis line, separated by a wider gap, become separate records.
        """

        if len(line) <= 1:
            return [line]
        base = line[0].axis_loc
        spans = sorted(
            ((c, _axial_span(c, base)) for c in line),
            key=lambda item: item[1][0],
        )
        sub: list[list[_CylFace]] = []
        current: list[_CylFace] = [spans[0][0]]
        cur_hi = spans[0][1][1]
        cur_max_len = max(spans[0][0].length, 0.0)
        for cyl, (lo, hi) in spans[1:]:
            gap = lo - cur_hi
            tol = max(cur_max_len, cyl.length, 0.0)
            if gap <= tol:
                current.append(cyl)
                cur_hi = max(cur_hi, hi)
                cur_max_len = max(cur_max_len, cyl.length, 0.0)
            else:
                sub.append(current)
                current = [cyl]
                cur_hi = hi
                cur_max_len = max(cyl.length, 0.0)
        sub.append(current)
        return sub

    def _build_hole_record(self, solid, group: list[_CylFace]) -> HoleRecord | None:
        if not group:
            return None

        # Reject partial-arc groups: a genuine round hole's bore wall wraps a
        # full circle. Rounded cutout corners, bend reliefs and flange bend
        # radii are inner cylindrical patches too, but only span a partial arc.
        if _bore_angular_coverage(group) < _MIN_HOLE_ANGULAR_COVERAGE:
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
