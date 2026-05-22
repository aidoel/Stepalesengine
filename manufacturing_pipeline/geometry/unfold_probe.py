"""Sheet-metal unfold probe.

Treats unfoldability as a feature, not a verdict. Never raises. Failures
are reported in :class:`UnfoldResult` (status, reason).

Algorithm
---------
1. Classify every face as planar / cylindrical / other; reject if "other"
   surfaces exceed 5% of the total area.
2. Build the sheet model (:func:`_identify_sheet`): pair each planar face
   with its *nearest* antiparallel partner, cluster the gaps, and take the
   area-heaviest cluster as the thickness ``t``. The faces in that cluster
   are the *flanges* (sheet faces); everything else is rim.
3. Pick the largest flange as the unfold base.
4. Build a bend graph whose nodes are flanges: a cylindrical patch is a bend
   only when it joins two flanges and its axis lies in their planes (a
   rounded corner, whose axis runs along the flange normal, is not a bend).
5. BFS from the base. On each bend, rotate the descendant face about the
   bend axis by ``-theta`` (flattening it). Compute the bend allowance
   ``BA = theta * (R + K * t)`` and add it to the cumulative flat area.
6. Detect failure modes:
   - cyclic graph (closed box / tube),
   - branching factor > 2 (star-shaped flange tree),
   - thickness CV above threshold,
   - material thicker than sheet-metal range (plate / machining work),
   - non-developable "other" faces,
   - disconnected shells.

The probe never raises - every failure becomes a :class:`UnfoldResult` with
``status=FAILURE`` and a specific ``reason`` token.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import Any

from ..config.classification_variables import (
    UNFOLD_ANTIPARALLEL_DOT_MAX,
    UNFOLD_BEND_AXIS_PERP_DOT_MAX,
    UNFOLD_CLOSED_BODY_CYL_AREA_RATIO,
    UNFOLD_COPLANAR_COS_LIMIT,
    UNFOLD_FLANGE_GAP_REL_TOL,
    UNFOLD_HEM_ANGLE_MIN_DEG,
    UNFOLD_K_FACTOR,
    UNFOLD_MAX_OTHER_FACES_RATIO,
    UNFOLD_MAX_SHEET_THICKNESS_MM,
    UNFOLD_MIN_BEND_ANGLE_DEG,
    UNFOLD_ROLLED_TUBE_MIN_WRAP_DEG,
    UNFOLD_THICKNESS_CV_LIMIT,
)
from .types import UnfoldResult, UnfoldStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bend / face records
# ---------------------------------------------------------------------------


@dataclass
class _PlanarFace:
    face: Any
    index: int
    normal: tuple[float, float, float]
    centroid: tuple[float, float, float]
    area: float


@dataclass
class _CylPatch:
    face: Any
    index: int
    radius: float
    axis_dir: tuple[float, float, float]
    axis_loc: tuple[float, float, float]
    area: float
    length_along_axis: float  # the bend "length" L


@dataclass
class Bend:
    """A detected bend joining two planar faces through a cylindrical patch.

    Attributes
    ----------
    p_in_index, p_out_index
        Indices into the planar-face list of the two flats joined by this bend.
    cyl_index
        Index into the cylindrical-patch list, or ``None`` for a synthetic
        (sharp / missing-fillet) bend.
    axis_dir, axis_loc
        Unit direction and a point on the bend axis line.
    angle_rad, angle_deg
        Bend angle (between the two flat-face normals, in [0, pi]).
    radius
        Inner bend radius. 0 for synthetic bends.
    length
        Bend length along the axis (mm).
    is_hem
        True if angle is close to 180 degrees AND radius is close to t/2.
    is_synthetic
        True if no cylindrical patch was found between the two flats
        (sharp corner / missing fillet).
    """

    p_in_index: int
    p_out_index: int
    cyl_index: int | None
    axis_dir: tuple[float, float, float]
    axis_loc: tuple[float, float, float]
    angle_rad: float
    radius: float
    length: float
    is_hem: bool = False
    is_synthetic: bool = False

    @property
    def angle_deg(self) -> float:
        return math.degrees(self.angle_rad)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _unit(v: tuple[float, float, float]) -> tuple[float, float, float]:
    n = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
    if n < 1e-12:
        return (0.0, 0.0, 1.0)
    return (v[0] / n, v[1] / n, v[2] / n)


def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _cv(values: list[float]) -> float:
    """Coefficient of variation. Returns 0 if not enough data."""
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    if abs(m) < 1e-9:
        return float("inf")
    var = sum((x - m) ** 2 for x in values) / len(values)
    return math.sqrt(var) / abs(m)


# ---------------------------------------------------------------------------
# Face classification
# ---------------------------------------------------------------------------


def _classify_faces(solid):
    """Return ``(planars, cylinders, others, total_area, planar_area, other_area)``.

    Each item is a list of indexed records carrying the face plus its key
    geometric quantities.
    """

    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp
    from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    planars: list[_PlanarFace] = []
    cylinders: list[_CylPatch] = []
    others: list[tuple[object, float]] = []
    total_area = 0.0
    other_area = 0.0
    planar_area = 0.0

    exp = TopExp_Explorer(solid, TopAbs_FACE)
    idx_p = 0
    idx_c = 0
    while exp.More():
        f = TopoDS.Face_s(exp.Current())
        try:
            adapt = BRepAdaptor_Surface(f)
            t = adapt.GetType()
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(f, props)
            area = float(props.Mass())
            total_area += area
            if t == GeomAbs_Plane:
                pln = adapt.Plane()
                d = pln.Axis().Direction()
                sign = -1.0 if f.Orientation() == TopAbs_REVERSED else 1.0
                normal = _unit((d.X() * sign, d.Y() * sign, d.Z() * sign))
                cm = props.CentreOfMass()
                centroid = (cm.X(), cm.Y(), cm.Z())
                planars.append(
                    _PlanarFace(face=f, index=idx_p, normal=normal, centroid=centroid, area=area)
                )
                planar_area += area
                idx_p += 1
            elif t == GeomAbs_Cylinder:
                cyl = adapt.Cylinder()
                ax = cyl.Axis()
                ax_dir = _unit((ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()))
                ax_loc_raw = (ax.Location().X(), ax.Location().Y(), ax.Location().Z())
                # length-along-axis: derive from area / (radius * angular_extent)
                u_min = adapt.FirstUParameter()
                u_max = adapt.LastUParameter()
                v_min = adapt.FirstVParameter()
                v_max = adapt.LastVParameter()
                ang = max(u_max - u_min, 0.0)
                rad = float(cyl.Radius())
                if rad > 1e-9 and ang > 1e-9:
                    length = area / (rad * ang)
                else:
                    length = float(v_max - v_min)
                # Normalise ax_loc to the MIDPOINT along the axis (project the
                # face centroid onto the axis line). Cylinder constructors are
                # free to place ax.Location() anywhere on the axis; we want a
                # canonical centered point so the bend strip's midline maps to
                # the centroid of the bend extent.
                cm = props.CentreOfMass()
                cmv = (cm.X() - ax_loc_raw[0], cm.Y() - ax_loc_raw[1], cm.Z() - ax_loc_raw[2])
                t_along = cmv[0] * ax_dir[0] + cmv[1] * ax_dir[1] + cmv[2] * ax_dir[2]
                ax_loc = (
                    ax_loc_raw[0] + t_along * ax_dir[0],
                    ax_loc_raw[1] + t_along * ax_dir[1],
                    ax_loc_raw[2] + t_along * ax_dir[2],
                )
                cylinders.append(
                    _CylPatch(
                        face=f,
                        index=idx_c,
                        radius=rad,
                        axis_dir=ax_dir,
                        axis_loc=ax_loc,
                        area=area,
                        length_along_axis=length,
                    )
                )
                idx_c += 1
            else:
                others.append((f, area))
                other_area += area
        except Exception as exc:
            logger.debug("_classify_faces face skipped: %s", exc)
        exp.Next()

    return planars, cylinders, others, total_area, planar_area, other_area


# ---------------------------------------------------------------------------
# Thickness probing
# ---------------------------------------------------------------------------


@dataclass
class _SheetModel:
    """The sheet-metal interpretation of a solid's planar faces.

    A *flange* (sheet face) is a planar face that sits directly opposite an
    antiparallel partner face one material thickness away. Thin rim faces that
    bound the sheet's edges are deliberately excluded: bends join flanges,
    never rim faces, and a rounded corner between two rim faces must not be
    mistaken for a bend.

    Attributes
    ----------
    thickness, thickness_cv
        Material thickness and its coefficient of variation (taper signal).
    flange_indices
        Indices (into the planar-face list) of the faces identified as flanges.
    n_pairs
        Number of antiparallel flange pairs found.
    """

    thickness: float
    thickness_cv: float
    flange_indices: set[int]
    n_pairs: int


def _pair_gap(a: _PlanarFace, b: _PlanarFace) -> tuple[float, float]:
    """Return ``(gap, lateral)`` for an antiparallel planar pair.

    ``gap`` is the perpendicular distance between the two planes; ``lateral``
    is the in-plane offset between the two centroids. A genuine flange pair
    sits face-to-face, so ``lateral`` is small relative to the face size; two
    unrelated antiparallel faces are laterally far apart.
    """

    off = (
        b.centroid[0] - a.centroid[0],
        b.centroid[1] - a.centroid[1],
        b.centroid[2] - a.centroid[2],
    )
    proj = _dot(off, a.normal)
    perp = (
        off[0] - proj * a.normal[0],
        off[1] - proj * a.normal[1],
        off[2] - proj * a.normal[2],
    )
    lateral = math.sqrt(perp[0] ** 2 + perp[1] ** 2 + perp[2] ** 2)
    return abs(proj), lateral


def _pair_opposite_faces(
    planars: list[_PlanarFace], thickness: float
) -> dict[int, int]:
    """Map each planar face to the opposite surface of the same flat segment.

    The two surfaces of one sheet segment are antiparallel, overlapping, and
    exactly the material thickness apart. This pairing lets the unfold bridge
    a Z-section: its opposite-direction folds attach to opposite faces of the
    middle flange, so the bend graph splits into components a single BFS
    cannot span.
    """

    twin: dict[int, int] = {}
    if thickness <= 1e-9:
        return twin
    tol = max(UNFOLD_FLANGE_GAP_REL_TOL * thickness, 0.15)
    for i in range(len(planars)):
        a = planars[i]
        best_j = -1
        best_gap = 0.0
        for j in range(len(planars)):
            if i == j:
                continue
            b = planars[j]
            if _dot(a.normal, b.normal) > UNFOLD_ANTIPARALLEL_DOT_MAX:
                continue
            gap, lateral = _pair_gap(a, b)
            if abs(gap - thickness) > tol:
                continue
            # Overlap test (mirrors _identify_sheet): the back-side face sits
            # directly behind its front, so the lateral offset is small.
            if lateral > 0.5 * math.sqrt(min(a.area, b.area)) + gap:
                continue
            if best_j < 0 or gap < best_gap:
                best_j = j
                best_gap = gap
        if best_j >= 0:
            twin[i] = best_j
    return twin


def _identify_sheet(planars: list[_PlanarFace]) -> _SheetModel:
    """Identify the flange faces and material thickness of a sheet-metal solid.

    A flange's back side is the antiparallel face *closest* to it: material
    fills the gap, so nothing parallel can sit nearer. Picking each face's
    nearest antiparallel partner is what keeps two parallel flanges straddling
    an air gap (a Z-bend, a U-channel) from being read as one thick pair.

    The per-face gaps are then clustered and the cluster carrying the most
    face area wins as the thickness. Area weighting is what stops a tiny
    chamfer sliver - whose own gap is tiny - from setting the thickness the
    way an unweighted minimum did.
    """

    if len(planars) < 2:
        return _SheetModel(0.0, 0.0, set(), 0)

    # Each face's nearest antiparallel, overlapping partner.
    partner_gap: dict[int, float] = {}
    partner_of: dict[int, int] = {}
    for i in range(len(planars)):
        a = planars[i]
        for j in range(len(planars)):
            if i == j:
                continue
            b = planars[j]
            if _dot(a.normal, b.normal) > UNFOLD_ANTIPARALLEL_DOT_MAX:
                continue
            gap, lateral = _pair_gap(a, b)
            if gap <= 1e-6:
                continue
            # Overlap test: a back-side face sits directly behind its front
            # (lateral offset ~ 0); reject pairs that are laterally far apart.
            if lateral > 0.5 * math.sqrt(min(a.area, b.area)) + gap:
                continue
            if i not in partner_gap or gap < partner_gap[i]:
                partner_gap[i] = gap
                partner_of[i] = j

    if not partner_gap:
        return _SheetModel(0.0, 0.0, set(), 0)

    # Cluster the per-face gaps; the area-heaviest cluster is the thickness.
    ordered = sorted(partner_gap.items(), key=lambda kv: kv[1])
    clusters: list[list[int]] = []
    cluster_min: list[float] = []
    for face, gap in ordered:
        if clusters and gap - cluster_min[-1] <= max(
            UNFOLD_FLANGE_GAP_REL_TOL * cluster_min[-1], 0.15
        ):
            clusters[-1].append(face)
        else:
            clusters.append([face])
            cluster_min.append(gap)

    def _cluster_area(faces: list[int]) -> float:
        return sum(planars[f].area for f in faces)

    best = max(clusters, key=_cluster_area)
    flange_indices: set[int] = set(best)
    for f in best:
        flange_indices.add(partner_of[f])

    weight = sum(planars[f].area for f in best)
    thickness = (
        sum(planars[f].area * partner_gap[f] for f in best) / weight
        if weight > 0
        else partner_gap[best[0]]
    )

    all_probes: list[float] = []
    counted: set[tuple[int, int]] = set()
    for f in best:
        j = partner_of[f]
        key = (min(f, j), max(f, j))
        if key in counted:
            continue
        counted.add(key)
        all_probes.extend(_probe_thickness(planars[f], planars[j]))

    return _SheetModel(
        thickness=thickness,
        thickness_cv=_cv(all_probes),
        flange_indices=flange_indices,
        n_pairs=len(counted),
    )


def _face_vertices(face) -> list[tuple[float, float, float]]:
    """Return the 3D positions of a face's boundary vertices.

    Boundary vertices lie exactly on the face, so projecting them onto an
    opposite face's plane gives true material-gap samples - unlike an
    axis-aligned bounding box, which balloons for a face that is not
    axis-aligned and yields spurious thickness variation.
    """

    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    pts: list[tuple[float, float, float]] = []
    seen: set[tuple[float, float, float]] = set()
    exp = TopExp_Explorer(face, TopAbs_VERTEX)
    while exp.More():
        try:
            p = BRep_Tool.Pnt_s(TopoDS.Vertex_s(exp.Current()))
            key = (round(p.X(), 6), round(p.Y(), 6), round(p.Z(), 6))
            if key not in seen:
                seen.add(key)
                pts.append((p.X(), p.Y(), p.Z()))
        except Exception as exc:
            logger.debug("_face_vertices vertex skipped: %s", exc)
        exp.Next()
    return pts


def _probe_thickness(a: _PlanarFace, b: _PlanarFace) -> list[float]:
    """Sample distances between face A and face B's plane and vice-versa.

    Probes from each face's boundary vertices projected onto the other face's
    plane. Returns a list of distances; for a parallel pair they are all
    equal, for a tapered pair they vary.
    """

    try:
        cs_a = _face_vertices(a.face) or [a.centroid]
        cs_b = _face_vertices(b.face) or [b.centroid]
    except Exception:
        cs_a = [a.centroid]
        cs_b = [b.centroid]

    probes: list[float] = []
    # A's vertices projected onto B's plane (distance = dot(P - B.cm, B.normal)).
    for c in cs_a + [a.centroid]:
        dx = c[0] - b.centroid[0]
        dy = c[1] - b.centroid[1]
        dz = c[2] - b.centroid[2]
        probes.append(abs(dx * b.normal[0] + dy * b.normal[1] + dz * b.normal[2]))
    for c in cs_b + [b.centroid]:
        dx = c[0] - a.centroid[0]
        dy = c[1] - a.centroid[1]
        dz = c[2] - a.centroid[2]
        probes.append(abs(dx * a.normal[0] + dy * a.normal[1] + dz * a.normal[2]))
    return probes


# ---------------------------------------------------------------------------
# Bend graph construction
# ---------------------------------------------------------------------------


def _build_edge_face_map(solid):
    """Map each TopoDS_Edge to the list of TopoDS_Faces using it."""

    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    m = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(solid, TopAbs_EDGE, TopAbs_FACE, m)
    return m


def _axes_collinear(
    a_dir: tuple[float, float, float],
    a_loc: tuple[float, float, float],
    b_dir: tuple[float, float, float],
    b_loc: tuple[float, float, float],
    angle_tol_deg: float = 2.0,
    dist_tol_mm: float = 0.5,
) -> bool:
    """Two axis lines (point + direction) are the same line."""

    cos_lim = math.cos(math.radians(angle_tol_deg))
    d = abs(_dot(a_dir, b_dir))
    if d < cos_lim:
        return False
    dx = b_loc[0] - a_loc[0]
    dy = b_loc[1] - a_loc[1]
    dz = b_loc[2] - a_loc[2]
    t = dx * a_dir[0] + dy * a_dir[1] + dz * a_dir[2]
    proj_x = a_loc[0] + t * a_dir[0]
    proj_y = a_loc[1] + t * a_dir[1]
    proj_z = a_loc[2] + t * a_dir[2]
    perp_sq = (b_loc[0] - proj_x) ** 2 + (b_loc[1] - proj_y) ** 2 + (b_loc[2] - proj_z) ** 2
    return perp_sq <= dist_tol_mm * dist_tol_mm


def _group_coaxial_cylinders(cylinders: list[_CylPatch]) -> list[list[_CylPatch]]:
    """Group concentric cylinders (inner + outer of a sheet-metal bend)."""

    groups: list[list[_CylPatch]] = []
    for c in cylinders:
        placed = False
        for g in groups:
            if _axes_collinear(g[0].axis_dir, g[0].axis_loc, c.axis_dir, c.axis_loc):
                g.append(c)
                placed = True
                break
        if not placed:
            groups.append([c])
    return groups


def _count_physical_bends(bends: list[Bend], thickness: float) -> int:
    """Count distinct physical bends (hinge lines) in a bend list.

    One physical corner is represented in ``bends`` more than once: its inner
    and its outer surface each yield a record — a cylindrical fillet and/or a
    sharp synthetic edge — so a constant-thickness corner is two records and a
    box-minus-box tube wall is a fillet plus a sharp edge. Every record of one
    corner shares that corner's hinge line up to the sheet thickness. Grouping
    records by hinge line (parallel axis, within a thickness-scaled
    perpendicular distance) and counting the groups gives the true physical
    bend count — robust to how the unfold BFS walked, so it does not
    undercount a Z-section whose opposite-folding flange sits off the base
    face's component.
    """
    # A corner's inner and outer surface records sit within ~thickness of each
    # other for a clean constant-thickness bend, and up to ~3*thickness apart
    # for a non-concentric or sharp-cornered one. Distinct corners are a flange
    # length apart, far beyond this. 3*thickness separates the two cleanly.
    merge_dist = max(3.0 * float(thickness), 1.5)
    representatives: list[Bend] = []
    for b in bends:
        if any(
            _axes_collinear(
                rep.axis_dir,
                rep.axis_loc,
                b.axis_dir,
                b.axis_loc,
                dist_tol_mm=merge_dist,
            )
            for rep in representatives
        ):
            continue
        representatives.append(b)
    return len(representatives)


@dataclass
class _RolledTube:
    """A smoothly rolled sheet-metal section (a cylinder or pipe).

    Attributes
    ----------
    thickness
        The wall thickness (outer radius minus inner radius).
    mean_radius
        The arc midline radius, ``(inner + outer) / 2``.
    length
        The cylinder length along the axis (mm).
    """

    thickness: float
    mean_radius: float
    length: float


def _detect_rolled_tube(
    cylinders: list[_CylPatch],
    total_area: float,
) -> _RolledTube | None:
    """Detect a sheet rolled smoothly into a cylinder or pipe section.

    A part rolled into a closed tubular section has cylindrical area dwarfing
    its planar area, so the closed-body heuristic flags it as a closed body.
    But a rolled *sheet* is still developable: slit the manufacturing seam and
    it flattens to a rectangle - exactly what a folded faceted tube does, only
    here the bend is one continuous roll rather than discrete creases.

    A rolled tube shows a single coaxial cylinder group holding exactly two
    wall radii: an inner bore and an outer skin, one sheet thickness apart,
    each wrapping a near-complete circle. A genuinely non-developable closed
    body looks different: a solid bar or boss has a single radius (no inner
    wall); a turned / machined revolve stacks many different radii; a thick
    pipe blank has a wall outside the sheet-metal range. None of those yield a
    rolled-tube model here.

    Returns the :class:`_RolledTube` model, or ``None`` when the cylinders are
    not a single thin-wall full-wrap inner/outer pair.
    """

    if not cylinders or total_area <= 1e-9:
        return None

    groups = _group_coaxial_cylinders(cylinders)
    # The rolled wall is the area-dominant coaxial group.
    groups.sort(key=lambda g: -sum(c.area for c in g))
    group = groups[0]
    if sum(c.area for c in group) / total_area < UNFOLD_CLOSED_BODY_CYL_AREA_RATIO:
        return None

    # Bucket the group's patches by radius: a rolled tube has exactly an inner
    # and an outer wall.
    by_radius: dict[float, list[_CylPatch]] = {}
    for c in group:
        key = round(c.radius, 2)
        by_radius.setdefault(key, []).append(c)
    radii = sorted(by_radius)
    if len(radii) != 2:
        return None

    inner_r, outer_r = radii[0], radii[1]
    thickness = outer_r - inner_r
    if thickness <= 1e-3 or thickness > UNFOLD_MAX_SHEET_THICKNESS_MM:
        return None

    # Each wall must wrap a near-complete circle: a partial arc is an open bend
    # already handled by the bend graph, not a rolled closed section.
    min_wrap = math.radians(UNFOLD_ROLLED_TUBE_MIN_WRAP_DEG)
    for patches in by_radius.values():
        wrap = 0.0
        for c in patches:
            if c.radius > 1e-9 and c.length_along_axis > 1e-9:
                wrap += c.area / (c.radius * c.length_along_axis)
        if wrap < min_wrap:
            return None

    length = max(c.length_along_axis for c in group)
    return _RolledTube(
        thickness=thickness,
        mean_radius=0.5 * (inner_r + outer_r),
        length=length,
    )


def _build_bends(
    planars: list[_PlanarFace],
    cylinders: list[_CylPatch],
    solid,
    thickness: float,
    flange_indices: set[int] | None = None,
) -> tuple[list[Bend], list[list[int]], list[list[int]]]:
    """Construct the bend graph.

    A bend joins two *flange* faces (see :func:`_identify_sheet`); when
    ``flange_indices`` is given, only those faces are eligible as the flats a
    bend connects. This is what stops a rounded outer corner - whose cylinder
    is adjacent only to thin rim faces - from being mistaken for a bend.

    Returns
    -------
    bends:
        List of detected :class:`Bend` records (one per physical bend, merging
        concentric inner/outer cylindrical patches).
    planar_bend_idx:
        For each planar face, the list of bend indices incident to it.
    planar_neighbors:
        For each planar face, the list of *other* planar-face indices it is
        connected to through a bend.
    """

    bends: list[Bend] = []

    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer

    def _planar_index_of(face) -> int:
        for p in planars:
            if p.face.IsSame(face):
                return p.index
        return -1

    # Edge -> faces adjacency
    edge_face_map = _build_edge_face_map(solid)

    def _cylinder_planar_neighbors(cyl_face) -> list[int]:
        seen: set = set()
        out: list[int] = []
        exp_e = TopExp_Explorer(cyl_face, TopAbs_EDGE)
        while exp_e.More():
            e = exp_e.Current()
            for k in range(1, edge_face_map.Size() + 1):
                key = edge_face_map.FindKey(k)
                if key.IsSame(e):
                    flist = edge_face_map.FindFromIndex(k)
                    for f in flist:
                        if not f.IsSame(cyl_face):
                            ip = _planar_index_of(f)
                            if ip >= 0 and ip not in seen:
                                seen.add(ip)
                                out.append(ip)
                    break
            exp_e.Next()
        return out

    # Group concentric cylinders into single physical bends.
    cyl_groups = _group_coaxial_cylinders(cylinders)
    for group in cyl_groups:
        # Use the OUTER cylinder for the bend's geometric properties AND for
        # picking which two planar faces the bend joins. The outer cylinder's
        # planar neighbours are the two OUTER faces of the bent sheet, which
        # forms a connected "outer skin" chain across the whole part.
        outer = max(group, key=lambda c: c.radius)
        inner = min(group, key=lambda c: c.radius)
        bend_radius = inner.radius if len(group) > 1 else outer.radius

        outer_neighbors: list[int] = _cylinder_planar_neighbors(outer.face)
        if len(outer_neighbors) < 2:
            # Fall back to union of all cylinders in the group.
            outer_neighbors = []
            for c in group:
                for ip in _cylinder_planar_neighbors(c.face):
                    if ip not in outer_neighbors:
                        outer_neighbors.append(ip)
            if len(outer_neighbors) < 2:
                continue

        # Choose the pair: the two planars with the LARGEST areas whose
        # normals are *neither coplanar nor antiparallel* with each other.
        # Two faces with antiparallel normals are the OUTER and INNER faces
        # of the same flat segment - they don't define a bend angle.
        #
        # Only flange faces are eligible: a bend connects two sheet faces, so
        # restricting the candidates here is what rejects rounded corners
        # (whose cylinder touches only rim faces).
        eligible = outer_neighbors
        if flange_indices is not None:
            eligible = [i for i in outer_neighbors if i in flange_indices]
        if len(eligible) < 2:
            continue
        sorted_n = sorted(eligible, key=lambda i: -planars[i].area)
        a = None
        b = None
        for i in range(len(sorted_n)):
            cand_a = sorted_n[i]
            n_a = planars[cand_a].normal
            for j in range(i + 1, len(sorted_n)):
                cand_b = sorted_n[j]
                n_b = planars[cand_b].normal
                dot = _dot(n_a, n_b)
                # exclude coplanar (dot ~ +1) AND antiparallel (dot ~ -1)
                if abs(dot) >= UNFOLD_COPLANAR_COS_LIMIT:
                    continue
                # A real bend's hinge axis lies in the plane of both flanges,
                # so it is perpendicular to both normals. Reject candidates
                # whose cylinder axis runs along a flange normal (a corner
                # round between two flanges, not a bend).
                if (
                    abs(_dot(outer.axis_dir, n_a)) > UNFOLD_BEND_AXIS_PERP_DOT_MAX
                    or abs(_dot(outer.axis_dir, n_b)) > UNFOLD_BEND_AXIS_PERP_DOT_MAX
                ):
                    continue
                a = cand_a
                b = cand_b
                break
            if a is not None:
                break
        if a is None or b is None:
            continue
        n_a = planars[a].normal
        n_b = planars[b].normal
        cos = max(-1.0, min(1.0, _dot(n_a, n_b)))
        angle = math.acos(cos)
        if angle < math.radians(UNFOLD_MIN_BEND_ANGLE_DEG):
            continue
        is_hem = angle > math.radians(UNFOLD_HEM_ANGLE_MIN_DEG) and (
            thickness > 1e-6 and abs(bend_radius - thickness * 0.5) < thickness * 0.5
        )
        bends.append(
            Bend(
                p_in_index=a,
                p_out_index=b,
                cyl_index=outer.index,
                axis_dir=outer.axis_dir,
                axis_loc=outer.axis_loc,
                angle_rad=angle,
                radius=bend_radius,
                length=outer.length_along_axis,
                is_hem=is_hem,
                is_synthetic=False,
            )
        )

    # Per-planar adjacency lists
    planar_bend_idx: list[list[int]] = [[] for _ in planars]
    planar_neighbors: list[list[int]] = [[] for _ in planars]
    for bi, bend in enumerate(bends):
        planar_bend_idx[bend.p_in_index].append(bi)
        planar_bend_idx[bend.p_out_index].append(bi)
        planar_neighbors[bend.p_in_index].append(bend.p_out_index)
        planar_neighbors[bend.p_out_index].append(bend.p_in_index)

    return bends, planar_bend_idx, planar_neighbors


# ---------------------------------------------------------------------------
# Bend-graph traversal: cycle and branching detection
# ---------------------------------------------------------------------------


def _detect_cycles(n_nodes: int, neighbors: list[list[int]], start: int) -> bool:
    """Return True if there is a cycle reachable from *start*."""

    parent: dict[int, int] = {start: -1}
    queue = deque([start])
    while queue:
        u = queue.popleft()
        for v in neighbors[u]:
            if v not in parent:
                parent[v] = u
                queue.append(v)
            elif parent[u] != v:
                # found edge back to a non-parent => cycle
                return True
    return False


def _max_branching(neighbors: list[list[int]]) -> int:
    if not neighbors:
        return 0
    return max(len(set(n)) for n in neighbors)


def _find_seam_bend(n_nodes: int, bends: list[Bend], start: int) -> int | None:
    """Return the index of one bend to slit so a cyclic graph becomes a tree.

    A sheet-metal part rolled or folded into a closed tubular section (a
    rectangular tube, a rolled cylinder approximated by facets) is still
    *developable*: the flat blank exists, but its two ends meet, closing the
    bend graph into a loop. AutoPOL unfolds such a part by slitting one bend
    (the manufacturing seam) and flattening the rest.

    This is only valid when the bend graph is a *single simple cycle*: every
    flange reachable from ``start`` has exactly two incident bends and the
    component holds exactly as many bends as flanges. A genuinely closed body
    (a six-walled box, a machined block) produces a denser graph - flanges of
    degree three or more, or multiple independent cycles - and must keep
    failing. Such a graph yields no seam here.

    Returns the bend index to slit, or ``None`` when the graph is not a single
    simple cycle.
    """

    incident: list[list[int]] = [[] for _ in range(n_nodes)]
    for bi, b in enumerate(bends):
        incident[b.p_in_index].append(bi)
        incident[b.p_out_index].append(bi)

    component: set[int] = {start}
    queue = deque([start])
    component_bends: set[int] = set()
    while queue:
        u = queue.popleft()
        for bi in incident[u]:
            component_bends.add(bi)
            b = bends[bi]
            v = b.p_out_index if b.p_in_index == u else b.p_in_index
            if v not in component:
                component.add(v)
                queue.append(v)

    if not component_bends:
        return None
    # Single simple cycle: |bends| == |flanges| and every flange has degree 2.
    if len(component_bends) != len(component):
        return None
    for node in component:
        if len(incident[node]) != 2:
            return None
    # Slit the largest-radius bend in the loop: a wider bend is the most
    # plausible manufacturing seam and keeps the flattened tree well-formed.
    return max(component_bends, key=lambda bi: bends[bi].radius)


def _signed_bend_angle(
    parent_normal: tuple[float, float, float],
    child_normal: tuple[float, float, float],
    axis_dir: tuple[float, float, float],
) -> float:
    """Signed bend angle (rad) between two adjacent flat faces about ``axis_dir``.

    Magnitude is the unsigned angle in ``[0, pi]``; sign is the sign of
    ``(parent_normal x child_normal) . axis_dir``. Positive means a CCW
    rotation about ``axis_dir`` takes the parent normal toward the child
    normal; negative means CW. The reference frame is the bend axis as
    detected (its direction is arbitrary but consistent per bend).
    """

    cos = max(-1.0, min(1.0, _dot(parent_normal, child_normal)))
    mag = math.acos(cos)
    s = _dot(_cross(parent_normal, child_normal), axis_dir)
    sign = 1.0 if s >= 0.0 else -1.0
    return sign * mag


def _detect_joggles(
    planars: list[_PlanarFace],
    bends: list[Bend],
    traversal: list[tuple[int, int, int]],
    *,
    angle_match_tol_deg: float = 3.0,
) -> int:
    """Count joggles (Z-bends) along the BFS traversal.

    A joggle is a pair of bends ``(b1, b2)`` whose signed rotation angles are
    of opposite sign and roughly equal magnitude (within
    ``angle_match_tol_deg`` degrees), and whose hinge axes are roughly
    parallel. Two topologies count:

    * *Serial* (Z / step): one bend's child is the next bend's parent. The
      shared flat is the middle of the unfold chain.
    * *Sibling* (joggle off a single base): both bends share a parent, AND
      the two child flats lie on *opposite sides* of the parent's plane.
      Two siblings on the *same* side (U-channel: both legs UP) are NOT a
      joggle even though their cross-product signs flip due to symmetric
      placement of the two bend axes.

    The cylindrical adaptor returns an arbitrary axis sign; we canonicalise
    each bend axis so the sign of the cross-product test is reproducible
    across parallel axes.
    """

    if len(traversal) < 2:
        return 0

    def _canon_axis(d: tuple[float, float, float]) -> tuple[float, float, float]:
        # Flip so the largest-magnitude component is positive. Gives a
        # stable, traversal-order-independent reference frame for sign.
        ax, ay, az = d
        absx, absy, absz = abs(ax), abs(ay), abs(az)
        if absz >= absx and absz >= absy:
            return d if az >= 0 else (-ax, -ay, -az)
        if absx >= absy:
            return d if ax >= 0 else (-ax, -ay, -az)
        return d if ay >= 0 else (-ax, -ay, -az)

    signed: dict[int, float] = {}
    axes: dict[int, tuple[float, float, float]] = {}
    incoming: dict[int, int] = {}  # face -> bend where face is child
    outgoing: dict[int, list[int]] = {}  # face -> bends where face is parent
    parent_of: dict[int, int] = {}  # bend -> parent face
    child_of: dict[int, int] = {}  # bend -> child face

    for parent, child, bi in traversal:
        b = bends[bi]
        ax = _canon_axis(b.axis_dir)
        signed[bi] = _signed_bend_angle(planars[parent].normal, planars[child].normal, ax)
        axes[bi] = ax
        incoming[child] = bi
        outgoing.setdefault(parent, []).append(bi)
        parent_of[bi] = parent
        child_of[bi] = child

    tol_rad = math.radians(angle_match_tol_deg)
    parallel_cos = math.cos(math.radians(15.0))
    counted: set = set()
    n_joggles = 0

    def _pair_is_joggle(bi1: int, bi2: int) -> bool:
        if abs(_dot(axes[bi1], axes[bi2])) < parallel_cos:
            return False
        if signed[bi1] * signed[bi2] >= 0.0:
            return False
        if abs(abs(signed[bi1]) - abs(signed[bi2])) > tol_rad:
            return False
        return True

    # Serial joggles: a face is both child(b_in) and parent(b_out).
    for face_idx, b_in in incoming.items():
        for b_out in outgoing.get(face_idx, []):
            key = (min(b_in, b_out), max(b_in, b_out))
            if key in counted:
                continue
            if not _pair_is_joggle(b_in, b_out):
                continue
            counted.add(key)
            n_joggles += 1

    # Sibling joggles: two bends share a parent, but the two children lie on
    # OPPOSITE sides of the parent's plane (signed projection of
    # (child_centroid - parent_centroid) onto parent_normal flips sign).
    for parent_idx, bend_list in outgoing.items():
        if len(bend_list) < 2:
            continue
        p = planars[parent_idx]
        side_of: dict[int, float] = {}
        for bi in bend_list:
            cc = planars[child_of[bi]].centroid
            d = (cc[0] - p.centroid[0], cc[1] - p.centroid[1], cc[2] - p.centroid[2])
            side_of[bi] = _dot(d, p.normal)
        for i in range(len(bend_list)):
            for j in range(i + 1, len(bend_list)):
                bi1, bi2 = bend_list[i], bend_list[j]
                key = (min(bi1, bi2), max(bi1, bi2))
                if key in counted:
                    continue
                # Children must straddle the parent's plane.
                if side_of[bi1] * side_of[bi2] >= -1e-6:
                    continue
                if not _pair_is_joggle(bi1, bi2):
                    continue
                counted.add(key)
                n_joggles += 1

    return n_joggles


def _select_base_face(
    planars: list[_PlanarFace],
    bends: list[Bend],
    *,
    flange_indices: set[int] | None = None,
    area_tie_ratio: float = 0.005,
) -> int:
    """Pick the planar face to start the BFS unfold from.

    Only flange faces are eligible (the BFS walks the flange graph). Primary
    key: largest area. Among faces tied within ``area_tie_ratio`` of the
    maximum (default 0.5%), prefer the face that participates in the greatest
    number of detected bends (as either P_in or P_out). Final deterministic
    tie-breaker: smallest centroid tuple (rounded), then index.
    """

    if not planars:
        return -1

    bend_count = [0] * len(planars)
    for b in bends:
        if 0 <= b.p_in_index < len(planars):
            bend_count[b.p_in_index] += 1
        if 0 <= b.p_out_index < len(planars):
            bend_count[b.p_out_index] += 1

    eligible = [i for i in range(len(planars)) if flange_indices is None or i in flange_indices]
    if not eligible:
        eligible = list(range(len(planars)))

    max_area = max(planars[i].area for i in eligible)
    if max_area <= 0.0:
        return eligible[0]
    threshold = max_area * (1.0 - area_tie_ratio)
    tied = [i for i in eligible if planars[i].area >= threshold]

    if len(tied) == 1:
        return tied[0]

    best_count = max(bend_count[i] for i in tied)
    if best_count == 0 and bends:
        logger.warning(
            "UnfoldProbe: %d bends detected but no tied-largest planar face "
            "participates in any bend; falling back to largest area.",
            len(bends),
        )
    candidates = [i for i in tied if bend_count[i] == best_count]
    if len(candidates) == 1:
        return candidates[0]

    # Deterministic final tie-break: smallest rounded centroid then index.
    def _key(i: int):
        c = planars[i].centroid
        return (round(c[0], 6), round(c[1], 6), round(c[2], 6), i)

    return min(candidates, key=_key)


# ---------------------------------------------------------------------------
# UnfoldProbe
# ---------------------------------------------------------------------------


class BendDetector:
    """Detects bends from a sheet-metal solid.

    Pairs planar faces by adjacency through cylindrical faces; computes
    ``(axis, radius, angle)`` per bend. Tolerates missing fillets by inserting
    synthetic bends with ``R=0`` between non-coplanar adjacent planar faces
    that share a straight edge.
    """

    def __init__(self, *, sharp_bend_angle_min_deg: float = 5.0):
        self.sharp_bend_angle_min_deg = float(sharp_bend_angle_min_deg)

    def detect(self, solid) -> list[Bend]:
        try:
            planars, cylinders, _others, _ta, _pa, _oa = _classify_faces(solid)
        except Exception as exc:
            logger.debug("BendDetector.detect failed at classify: %s", exc)
            return []
        model = _identify_sheet(planars)
        try:
            bends, _, _ = _build_bends(
                planars, cylinders, solid, model.thickness, model.flange_indices
            )
        except Exception as exc:
            logger.debug("BendDetector.detect failed at build: %s", exc)
            return []
        # Synthetic bends: two flange faces sharing a straight edge at an angle
        # other than 0 / 180 deg, but with no cylindrical patch between them.
        try:
            bends += self._find_synthetic_bends(
                planars, bends, solid, model.flange_indices
            )
        except Exception as exc:
            logger.debug("BendDetector synthetic step failed: %s", exc)
        return bends

    def _find_synthetic_bends(
        self,
        planars: list[_PlanarFace],
        existing: list[Bend],
        solid,
        flange_indices: set[int] | None = None,
    ) -> list[Bend]:
        """Detect sharp flange-flange bends with no intermediate cylinder.

        Restricted to flange faces: two thin rim faces meeting at 90 deg (the
        sharp edge of a plate) are not a bend.
        """

        if len(planars) < 2:
            return []

        from OCP.BRepAdaptor import BRepAdaptor_Curve
        from OCP.GeomAbs import GeomAbs_Line
        from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
        from OCP.TopExp import TopExp
        from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

        # Edge -> faces map
        m = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(solid, TopAbs_EDGE, TopAbs_FACE, m)

        existing_pairs = {
            (min(b.p_in_index, b.p_out_index), max(b.p_in_index, b.p_out_index)) for b in existing
        }

        synthetic: list[Bend] = []
        seen_pairs = set(existing_pairs)

        for k in range(1, m.Size() + 1):
            edge = m.FindKey(k)
            try:
                curve = BRepAdaptor_Curve(edge)
            except Exception:
                continue
            try:
                if curve.GetType() != GeomAbs_Line:
                    continue
            except Exception:
                continue
            # Walk face ancestors
            flist = m.FindFromIndex(k)
            planar_ids: list[int] = []
            for f in flist:
                for p in planars:
                    if p.face.IsSame(f):
                        planar_ids.append(p.index)
                        break
            if len(planar_ids) != 2:
                continue
            a, b = planar_ids[0], planar_ids[1]
            if a == b:
                continue
            if flange_indices is not None and (
                a not in flange_indices or b not in flange_indices
            ):
                continue
            pair_key = (min(a, b), max(a, b))
            if pair_key in seen_pairs:
                continue
            n_a = planars[a].normal
            n_b = planars[b].normal
            cos = max(-1.0, min(1.0, _dot(n_a, n_b)))
            angle = math.acos(cos)
            if angle < math.radians(self.sharp_bend_angle_min_deg):
                continue
            if angle > math.radians(180.0 - self.sharp_bend_angle_min_deg) and angle < math.radians(
                180.0
            ):
                # could be a hem-like coplanar; still treat as synthetic bend
                pass
            seen_pairs.add(pair_key)

            # Edge geometry
            p1 = curve.Value(curve.FirstParameter())
            p2 = curve.Value(curve.LastParameter())
            axis_loc = ((p1.X() + p2.X()) / 2, (p1.Y() + p2.Y()) / 2, (p1.Z() + p2.Z()) / 2)
            axis_dir = _unit((p2.X() - p1.X(), p2.Y() - p1.Y(), p2.Z() - p1.Z()))
            length = math.sqrt(
                (p2.X() - p1.X()) ** 2 + (p2.Y() - p1.Y()) ** 2 + (p2.Z() - p1.Z()) ** 2
            )

            synthetic.append(
                Bend(
                    p_in_index=a,
                    p_out_index=b,
                    cyl_index=None,
                    axis_dir=axis_dir,
                    axis_loc=axis_loc,
                    angle_rad=angle,
                    radius=0.0,
                    length=length,
                    is_hem=False,
                    is_synthetic=True,
                )
            )
        return synthetic


class UnfoldProbe:
    """Sheet-metal unfold probe.

    See module docstring for the algorithm. The probe NEVER raises; all
    failures are reported via :class:`UnfoldResult`.
    """

    def __init__(
        self,
        *,
        k_factor: float = UNFOLD_K_FACTOR,
        thickness_cv_limit: float = UNFOLD_THICKNESS_CV_LIMIT,
        max_other_faces_ratio: float = UNFOLD_MAX_OTHER_FACES_RATIO,
    ):
        self.k = float(k_factor)
        self.thickness_cv_limit = float(thickness_cv_limit)
        self.max_other_faces_ratio = float(max_other_faces_ratio)
        # Debug-only: index (within the classify_faces planar list) of the
        # face chosen as the BFS base on the last ``run``. ``-1`` if no run
        # has produced a base yet. Reset on each invocation.
        self._last_base_face_id: int = -1

    # ------------------------------------------------------------------
    # Public entry: run
    # ------------------------------------------------------------------

    def run(self, solid) -> UnfoldResult:
        self._last_base_face_id = -1
        try:
            return self._run_inner(solid)
        except Exception as exc:
            logger.warning("UnfoldProbe.run unexpected exception: %s", exc)
            return UnfoldResult(status=UnfoldStatus.FAILURE, reason=f"exception:{exc}")

    def _run_inner(self, solid) -> UnfoldResult:
        # Shell check
        n_shells = self._count_shells(solid)
        if n_shells > 1:
            return UnfoldResult(
                status=UnfoldStatus.FAILURE,
                reason="disconnected_shells",
                flags={"n_shells": n_shells},
            )

        planars, cylinders, others, total_area, planar_area, other_area = _classify_faces(solid)
        if total_area <= 1e-9:
            return UnfoldResult(status=UnfoldStatus.FAILURE, reason="empty_solid")

        if not planars:
            return UnfoldResult(
                status=UnfoldStatus.FAILURE,
                reason="non_developable",
                flags={"other_area_ratio": other_area / total_area},
            )

        # Non-developable check
        other_ratio = other_area / total_area
        if other_ratio > self.max_other_faces_ratio:
            return UnfoldResult(
                status=UnfoldStatus.FAILURE,
                reason="non_developable",
                flags={"other_area_ratio": other_ratio},
            )

        # Closed-body heuristic: a closed tube has cylindrical area dwarfing
        # planar area. Sheet-metal parts always have planar surfaces >> cyl.
        # A sheet rolled smoothly into a cylinder or pipe is the exception:
        # it trips this ratio yet is still developable. Route those through
        # the rolled-tube detector before failing the part outright.
        cyl_area = sum(c.area for c in cylinders)
        if total_area > 1e-9 and cyl_area / total_area >= UNFOLD_CLOSED_BODY_CYL_AREA_RATIO:
            rolled = _detect_rolled_tube(cylinders, total_area)
            if rolled is not None:
                # The flat blank is a rectangle: the unrolled circumference
                # (2*pi*mean_radius) by the cylinder length. The seam is the
                # slit that opens the closed section, so the part is PARTIAL
                # with a seamed_section flag, mirroring a folded faceted tube.
                flat_area = 2.0 * math.pi * rolled.mean_radius * max(rolled.length, 0.0)
                return UnfoldResult(
                    status=UnfoldStatus.PARTIAL,
                    reason="seamed_section",
                    n_bends=1,
                    flat_area=flat_area,
                    thickness_mean=rolled.thickness,
                    flags={
                        "seamed_section": True,
                        "rolled_tube": True,
                        "cyl_area_ratio": cyl_area / total_area,
                    },
                )
            return UnfoldResult(
                status=UnfoldStatus.FAILURE,
                reason="cyclic_graph",
                flags={"cyl_area_ratio": cyl_area / total_area, "closed_body": True},
            )

        # Sheet model: thickness + the flange faces a bend may connect.
        sheet = _identify_sheet(planars)
        t_mean, t_cv, n_pairs = sheet.thickness, sheet.thickness_cv, sheet.n_pairs
        if t_mean <= 1e-6 or n_pairs == 0:
            return UnfoldResult(
                status=UnfoldStatus.FAILURE,
                reason="no_thickness",
                flags={"n_pairs": n_pairs},
            )

        # Too thick to be sheet metal: a chunky machined block has two large
        # parallel faces and would otherwise sail through the no-bends path.
        if t_mean > UNFOLD_MAX_SHEET_THICKNESS_MM:
            return UnfoldResult(
                status=UnfoldStatus.FAILURE,
                reason="too_thick",
                thickness_mean=t_mean,
                flags={"thickness_mean": t_mean},
            )

        thickness_partial = t_cv > self.thickness_cv_limit

        # Build bends (cylindrical + synthetic)
        detector = BendDetector()
        bends = detector.detect(solid)

        # Adjacency
        n_p = len(planars)
        planar_neighbors: list[list[int]] = [[] for _ in range(n_p)]
        planar_bend_idx: list[list[int]] = [[] for _ in range(n_p)]
        for bi, b in enumerate(bends):
            planar_bend_idx[b.p_in_index].append(bi)
            planar_bend_idx[b.p_out_index].append(bi)
            planar_neighbors[b.p_in_index].append(b.p_out_index)
            planar_neighbors[b.p_out_index].append(b.p_in_index)

        # Pick the base: largest planar face, with bend-participation as the
        # tie-breaker among faces equal in area. This avoids the symmetric
        # L-bracket pitfall where the inner-top and outer-bottom flange faces
        # tie on area and the inner face's neighbours never reach the outer-
        # cylinder bend.
        base_idx = _select_base_face(planars, bends, flange_indices=sheet.flange_indices)
        self._last_base_face_id = base_idx

        # No bends: flat plate
        if not bends:
            return UnfoldResult(
                status=UnfoldStatus.SUCCESS if not thickness_partial else UnfoldStatus.PARTIAL,
                n_bends=0,
                flat_area=planars[base_idx].area,
                thickness_mean=t_mean,
                thickness_cv=t_cv,
                blank_bbox=None,
                flags={"thickness_variation": thickness_partial},
                reason="thickness_variation" if thickness_partial else "",
            )

        # Cycle detection. A cycle in the bend graph closes the part into a
        # loop. Two cases must be told apart:
        #   * a single simple cycle - a sheet folded/rolled into a tubular
        #     section (rectangular tube, rolled cylinder). It is still
        #     developable: slit one bend (the seam) and flatten the rest.
        #   * anything denser - a genuinely closed body (six-walled box,
        #     machined block). Not developable; keep failing.
        seam_bend: int | None = None
        if _detect_cycles(n_p, planar_neighbors, base_idx):
            seam_bend = _find_seam_bend(n_p, bends, base_idx)
            if seam_bend is None:
                return UnfoldResult(
                    status=UnfoldStatus.FAILURE,
                    reason="cyclic_graph",
                    n_bends=len(bends),
                    thickness_mean=t_mean,
                    thickness_cv=t_cv,
                    flags={"closed_body": True},
                )
            # Slit the seam bend: drop it from the adjacency so the BFS walks
            # the resulting tree. The bend itself is still reported in
            # ``n_bends`` - the part has that many physical bends - but the
            # flat pattern needs the seam open.
            sb = bends[seam_bend]
            planar_bend_idx[sb.p_in_index] = [
                bi for bi in planar_bend_idx[sb.p_in_index] if bi != seam_bend
            ]
            planar_bend_idx[sb.p_out_index] = [
                bi for bi in planar_bend_idx[sb.p_out_index] if bi != seam_bend
            ]
            planar_neighbors[sb.p_in_index] = [
                p for p in planar_neighbors[sb.p_in_index] if p != sb.p_out_index
            ]
            planar_neighbors[sb.p_out_index] = [
                p for p in planar_neighbors[sb.p_out_index] if p != sb.p_in_index
            ]

        # Branching detection: planar face connected to > 2 bends. Previously
        # this was a hard FAILURE; we now downgrade to PARTIAL so downstream
        # tooling still receives n_bends and a partial flat_area for the
        # subtree the BFS reaches.
        max_b = _max_branching(planar_neighbors)
        branching = max_b > 2

        # BFS unfold + bend-allowance accumulation. Returns the sequence of
        # (parent_planar, child_planar, bend_idx) traversal records so we can
        # compute signed rotation angles for joggle detection.
        flat_area, visited_planars, visited_bends, traversal = self._bfs_unfold(
            base_idx, planars, bends, planar_bend_idx, t_mean
        )

        # Joggle detection: two adjacent bends (sharing a flat face) whose
        # signed rotation angles have opposite signs within a ~3 deg
        # magnitude tolerance. Joggles are informational and do NOT downgrade
        # status from SUCCESS.
        n_joggles = _detect_joggles(planars, bends, traversal)

        # Compose status / reason. Branching downgrades to PARTIAL even if
        # thickness is OK; thickness variation also downgrades to PARTIAL. A
        # slit seam (folded/rolled tubular section) likewise downgrades to
        # PARTIAL: the flat pattern is valid only with the seam cut open.
        seamed = seam_bend is not None
        partial = thickness_partial or branching or seamed
        status = UnfoldStatus.PARTIAL if partial else UnfoldStatus.SUCCESS
        reasons: list[str] = []
        if thickness_partial:
            reasons.append("thickness_variation")
        if branching:
            reasons.append("branching")
        if seamed:
            reasons.append("seamed_section")
        reason = "; ".join(reasons)

        flags: dict = {
            "thickness_variation": thickness_partial,
            "synthetic_bends": sum(1 for bi in visited_bends if bends[bi].is_synthetic),
            "has_hem": any(bends[bi].is_hem for bi in visited_bends),
            "n_planars_visited": len(visited_planars),
        }
        if branching:
            flags["branching"] = True
            flags["branches_found"] = max_b
        if seamed:
            flags["seamed_section"] = True
        if n_joggles > 0:
            flags["has_joggle"] = True
            flags["n_joggles"] = n_joggles

        # n_bends is the count of physical bends. Each physical corner appears
        # in `bends` more than once (inner + outer surface; fillet and/or sharp
        # edge), and _bfs_unfold only crosses bends in the base face's graph
        # component — missing a Z-section's opposite-folding flange. Counting
        # distinct hinge lines over the whole bend list is robust to both, and
        # the slit seam bend, still in `bends`, is counted with the rest.
        n_bends_total = _count_physical_bends(bends, t_mean)

        return UnfoldResult(
            status=status,
            n_bends=n_bends_total,
            flat_area=flat_area,
            blank_bbox=None,
            thickness_mean=t_mean,
            thickness_cv=t_cv,
            flags=flags,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _count_shells(self, solid) -> int:
        try:
            from OCP.TopAbs import TopAbs_SHELL
            from OCP.TopExp import TopExp_Explorer

            n = 0
            exp = TopExp_Explorer(solid, TopAbs_SHELL)
            while exp.More():
                n += 1
                exp.Next()
            return n
        except Exception:
            return 0

    def _bfs_unfold(
        self,
        base: int,
        planars: list[_PlanarFace],
        bends: list[Bend],
        planar_bend_idx: list[list[int]],
        thickness: float,
    ) -> tuple[float, set, set, list[tuple[int, int, int]]]:
        """Walk the bend graph from base, summing flat areas + bend allowances.

        Also returns a traversal log: list of ``(parent_planar, child_planar,
        bend_idx)`` tuples in BFS order. The traversal log lets callers
        compute signed rotation angles per bend (parent -> child) for
        downstream analyses like joggle detection.

        A Z-section's bends attach to opposite faces of its middle flange, so
        the bend graph splits into components a single BFS cannot span. After
        the walk from ``base``, each unreached component is bridged through
        the twin (opposite surface of the same flat segment) of a visited
        face — provided that bridge reaches an as-yet-uncovered segment — and
        unfolded too, so the flat area covers the whole part.
        """

        twin = _pair_opposite_faces(planars, thickness)

        def _seg(f: int) -> int:
            tw = twin.get(f, f)
            return f if f <= tw else tw

        visited_planars = {base}
        visited_bends: set = set()
        traversal: list[tuple[int, int, int]] = []
        covered: set = {_seg(base)}
        flat_area = planars[base].area

        def _walk(start: int) -> None:
            nonlocal flat_area
            queue: deque = deque([start])
            while queue:
                cur = queue.popleft()
                for bi in planar_bend_idx[cur]:
                    if bi in visited_bends:
                        continue
                    b = bends[bi]
                    nxt = b.p_out_index if b.p_in_index == cur else b.p_in_index
                    if nxt in visited_planars or _seg(nxt) in covered:
                        continue
                    visited_bends.add(bi)
                    visited_planars.add(nxt)
                    covered.add(_seg(nxt))
                    traversal.append((cur, nxt, bi))
                    flat_area += planars[nxt].area
                    ba = self._bend_allowance(b, thickness)
                    flat_area += ba * max(b.length, 0.0)
                    queue.append(nxt)

        _walk(base)

        def _reaches_uncovered(f: int) -> bool:
            for bi in planar_bend_idx[f]:
                b = bends[bi]
                nx = b.p_out_index if b.p_in_index == f else b.p_in_index
                if _seg(nx) not in covered:
                    return True
            return False

        bridged = True
        while bridged:
            bridged = False
            for f in range(len(planars)):
                if f in visited_planars or not planar_bend_idx[f]:
                    continue
                tw = twin.get(f)
                if tw is None or tw not in visited_planars:
                    continue
                if not _reaches_uncovered(f):
                    continue
                visited_planars.add(f)  # same flat segment as its twin
                _walk(f)
                bridged = True
                break

        return flat_area, visited_planars, visited_bends, traversal

    def _bend_allowance(self, bend: Bend, thickness: float) -> float:
        """Per-unit-length bend allowance: BA / L = theta * (R + K * t).

        We return the *width* of the unrolled bend strip in the unfold plane,
        per unit length along the bend axis. Multiply by ``bend.length`` to
        get the actual area added by the bend.
        """

        return bend.angle_rad * (bend.radius + self.k * thickness)

    # ------------------------------------------------------------------
    # Flat pattern (true unfold)
    # ------------------------------------------------------------------

    def compute_flat_pattern(self, solid) -> dict:
        """Return a dict suitable for the DXF writer's ``FlatPattern``.

        Format::

            {
                'outer_contour': [[(u, v), ...], ...],   # closed polygons
                'holes':         [[(u, v), ...], ...],   # closed polygons
                'bend_lines':    [((u1,v1),(u2,v2), {"angle","radius","direction"}), ...],
                'thickness':     float,
                'bbox':          (umin, vmin, umax, vmax),
            }

        Algorithm
        ---------
        1. Identify the base face (largest planar face) and build a 2D (u,v)
           frame on its plane.
        2. Run :class:`BendDetector` to get the bend list and adjacency.
        3. BFS the bend graph from the base. For each visited planar face,
           compute the cumulative 4x4 transform that rotates it into the base
           plane (rotation about the hinge axis by -theta + the parent's
           transform).
        4. Sample each face's outer + inner wires at ~0.5 mm chord tolerance,
           apply the transform, project to (u,v), and accumulate.
        5. Insert each bend allowance strip (rectangle along the hinge of
           width BA = theta * (R + K * t)) between the two flats it joins.
        6. Combine all face polygons + bend strips with shapely's
           ``unary_union`` to get a clean single outer contour. If shapely
           is unavailable, emit per-face contours as separate polygons (the
           DXF writer accepts multiple disjoint outer contours).

        Returns ``{}`` if the underlying unfold fails.
        Never raises.
        """

        result = self.run(solid)
        if result.status == UnfoldStatus.FAILURE:
            return {}

        try:
            return self._compute_flat_pattern_inner(solid, result)
        except Exception as exc:
            logger.warning("compute_flat_pattern unexpected exception: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # Inner: gather wires, transforms, bend strips
    # ------------------------------------------------------------------

    def _compute_flat_pattern_inner(self, solid, result) -> dict:
        import numpy as np  # local import: keep module light

        try:
            planars, cylinders, _others, _ta, _pa, _oa = _classify_faces(solid)
        except Exception as exc:
            logger.debug("_compute_flat_pattern: classify failed: %s", exc)
            return {}
        if not planars:
            return {}

        # Build bends first so the base selector can use bend participation
        # as the tie-breaker among equally-large planar faces.
        try:
            bends = BendDetector().detect(solid)
        except Exception as exc:
            logger.debug("_compute_flat_pattern: bend detect failed: %s", exc)
            bends = []

        # Pick the base using the same selector as ``run`` (largest flange,
        # tie-broken by bend participation).
        sheet = _identify_sheet(planars)
        base_idx = _select_base_face(planars, bends, flange_indices=sheet.flange_indices)
        base = planars[base_idx]

        # 2D frame on base plane.
        frame = _build_plane_frame(base.normal, base.centroid)
        n_p = len(planars)
        planar_bend_idx: list[list[int]] = [[] for _ in range(n_p)]
        for bi, b in enumerate(bends):
            if 0 <= b.p_in_index < n_p and 0 <= b.p_out_index < n_p:
                planar_bend_idx[b.p_in_index].append(bi)
                planar_bend_idx[b.p_out_index].append(bi)

        # BFS to assign each visited planar face a cumulative 4x4 transform.
        n_base_arr = np.array(base.normal, dtype=float)
        transforms: dict[int, np.ndarray] = {base_idx: np.eye(4)}
        bend_records: list[dict] = []  # one per visited bend
        queue: deque = deque([base_idx])
        visited_bends: set = set()

        # Face-pairing lets the BFS bridge a Z-section's split bend graph
        # (see _bfs_unfold). ``covered`` tracks which flat segments already
        # have a face placed, keyed by the lower of a face index and its twin.
        twin = _pair_opposite_faces(planars, result.thickness_mean or 0.0)

        def _seg(f: int) -> int:
            tw = twin.get(f, f)
            return f if f <= tw else tw

        covered: set = {_seg(base_idx)}

        while queue:
            cur = queue.popleft()
            T_cur = transforms[cur]
            n_cur = T_cur[:3, :3] @ np.array(planars[cur].normal, dtype=float)
            n_cur = n_cur / (np.linalg.norm(n_cur) + 1e-18)

            for bi in planar_bend_idx[cur]:
                if bi in visited_bends:
                    continue
                b = bends[bi]
                nxt = b.p_out_index if b.p_in_index == cur else b.p_in_index
                if nxt in transforms or _seg(nxt) in covered:
                    continue
                visited_bends.add(bi)

                # Compute the rotation: bring the child's normal (after T_cur
                # applied) onto +n_base. The rotation axis is the bend axis
                # transformed by T_cur.
                n_child_world = np.array(planars[nxt].normal, dtype=float)
                n_child = T_cur[:3, :3] @ n_child_world
                n_child = n_child / (np.linalg.norm(n_child) + 1e-18)

                axis_dir_world = np.array(b.axis_dir, dtype=float)
                axis_loc_world = np.array(b.axis_loc, dtype=float)
                # Hinge AFTER parent's transform.
                hinge_dir = T_cur[:3, :3] @ axis_dir_world
                hinge_dir = hinge_dir / (np.linalg.norm(hinge_dir) + 1e-18)
                hinge_loc = (T_cur[:3, :3] @ axis_loc_world) + T_cur[:3, 3]

                # Determine the signed rotation that maps n_child onto the
                # parent's own flattened normal n_cur. Targeting n_cur rather
                # than the global +n_base is what lets a bridged Z-section
                # component — entered through a face whose flattened normal is
                # -n_base — keep unfolding consistently; for a single-skin part
                # every n_cur equals n_base, so behaviour is unchanged.
                cos_a = float(np.clip(np.dot(n_child, n_cur), -1.0, 1.0))
                angle_mag = math.acos(cos_a)
                # Sign: cross(n_child, n_cur) should align with hinge_dir for a
                # positive rotation.
                cross_cb = np.cross(n_child, n_cur)
                sign = 1.0 if float(np.dot(cross_cb, hinge_dir)) >= 0 else -1.0
                alpha = sign * angle_mag

                R_unfold = _rotation_about_line(hinge_dir, hinge_loc, alpha)
                T_child_rot = R_unfold @ T_cur

                # After the rotation the child lies in the base plane with its
                # bend-side edge touching the hinge. We now translate the child
                # by BA in the in-plane direction perpendicular to the hinge,
                # AWAY from the parent. This inserts the bend-allowance strip
                # between the parent and the child.
                t_mean_for_BA = result.thickness_mean or 0.0
                BA_disp = float(b.angle_rad) * (float(b.radius) + self.k * t_mean_for_BA)
                # In-plane perpendicular = n_cur x hinge_dir (right-hand rule).
                perp_in_plane = np.cross(n_cur, hinge_dir)
                perp_in_plane = perp_in_plane / (np.linalg.norm(perp_in_plane) + 1e-18)
                # Decide which sign of perp points AWAY from the parent. Compute
                # the parent's centroid after T_cur and project it onto perp;
                # the AWAY direction is the opposite sign of that projection
                # relative to the hinge.
                parent_centroid_world = T_cur @ np.array([*planars[cur].centroid, 1.0])
                parent_offset = parent_centroid_world[:3] - hinge_loc
                sign_perp = -1.0 if float(np.dot(parent_offset, perp_in_plane)) >= 0 else 1.0
                translation = sign_perp * BA_disp * perp_in_plane
                T_translate = np.eye(4)
                T_translate[:3, 3] = translation
                T_child = T_translate @ T_child_rot
                transforms[nxt] = T_child
                covered.add(_seg(nxt))

                # Determine bend "direction" tag (up / down) based on the
                # 3D bend geometry (before any unfold transform). "up" means
                # the original child sat above the base plane (+n_base side).
                # We use: dot(centroid_child_world - base.centroid, n_base) > 0.
                cb = np.array(planars[nxt].centroid, dtype=float)
                ob = np.array(base.centroid, dtype=float)
                up_or_down = "up" if float(np.dot(cb - ob, n_base_arr)) >= 0 else "down"

                # Hinge segment in the unfolded plane: midline of the bend
                # strip. Project hinge_loc, hinge_dir, and perp_in_plane into
                # (u, v) on the base frame.
                p_mid_uv = _project_to_uv(hinge_loc, frame)
                d_uv = _project_dir_to_uv(hinge_dir, frame)
                d_uv = _normalize_2d(d_uv)
                perp_uv = _project_dir_to_uv(tuple(perp_in_plane), frame)
                perp_uv = _normalize_2d(perp_uv)
                half_L = 0.5 * max(float(b.length), 0.0)
                hinge_a_uv = (p_mid_uv[0] - half_L * d_uv[0], p_mid_uv[1] - half_L * d_uv[1])
                hinge_b_uv = (p_mid_uv[0] + half_L * d_uv[0], p_mid_uv[1] + half_L * d_uv[1])

                # Bend allowance (width of the unrolled bend strip in (u, v)).
                t_mean = result.thickness_mean or 0.0
                BA = float(b.angle_rad) * (float(b.radius) + self.k * t_mean)

                bend_records.append(
                    {
                        "bend": b,
                        "hinge_uv_a": hinge_a_uv,
                        "hinge_uv_b": hinge_b_uv,
                        "hinge_mid_uv": p_mid_uv,
                        "hinge_dir_uv": d_uv,
                        "perp_uv": perp_uv,
                        "sign_perp": sign_perp,
                        "BA": BA,
                        "direction": up_or_down,
                        "parent_idx": cur,
                        "child_idx": nxt,
                        "alpha": alpha,
                    }
                )
                queue.append(nxt)

            if not queue:
                # Bridge an opposite-fold component: a face whose twin is
                # already placed shares that twin's flat segment (same
                # transform, no rotation) and can carry the bend graph into
                # an as-yet-uncovered segment — a Z-section's far flange.
                for f in range(n_p):
                    if f in transforms or not planar_bend_idx[f]:
                        continue
                    tw = twin.get(f)
                    if tw is None or tw not in transforms:
                        continue
                    reaches_new = False
                    for bi in planar_bend_idx[f]:
                        bb = bends[bi]
                        nx = bb.p_out_index if bb.p_in_index == f else bb.p_in_index
                        if _seg(nx) not in covered:
                            reaches_new = True
                            break
                    if not reaches_new:
                        continue
                    transforms[f] = transforms[tw].copy()
                    queue.append(f)
                    break

        # ------------------------------------------------------------------
        # Wire sampling: for each visited planar face, sample its wires and
        # transform into (u, v).
        # ------------------------------------------------------------------
        face_outers: list[list[tuple[float, float]]] = []
        face_holes: list[list[tuple[float, float]]] = []

        for face_idx, T in transforms.items():
            try:
                outer_2d, holes_2d = _unfold_face(
                    planars[face_idx].face, T, frame, chord_tol_mm=0.1
                )
            except Exception as exc:
                logger.debug("_unfold_face failed (idx=%d): %s", face_idx, exc)
                continue
            if outer_2d:
                face_outers.append(outer_2d)
            face_holes.extend(holes_2d)

        # ------------------------------------------------------------------
        # Build bend-strip rectangles. The strip spans the hinge length and
        # has total width = BA, centered on the hinge midline. We then push
        # half of the strip toward the parent side and half toward the child
        # side, so that after the boolean union the parent's outer wire and
        # the strip and the child's outer wire all join cleanly. In practice,
        # since each face's projected wire already extends to the hinge line
        # (the bend's planar-face boundary edge), a centered strip works.
        # ------------------------------------------------------------------
        bend_strips: list[list[tuple[float, float]]] = []
        bend_lines_out: list[tuple[tuple[float, float], tuple[float, float], dict]] = []
        for rec in bend_records:
            a = rec["hinge_uv_a"]
            b_pt = rec["hinge_uv_b"]
            # Perpendicular direction (in (u, v)) pointing AWAY from parent.
            perp = rec["perp_uv"]
            sign = rec["sign_perp"]
            BA = rec["BA"]
            # The strip spans from the hinge (where parent ends) to the line
            # offset by BA in the AWAY direction (where the child starts).
            ofs_u = sign * BA * perp[0]
            ofs_v = sign * BA * perp[1]
            # Midline of the strip (between parent's outer wire and child's
            # outer wire, after translation).
            mid_a = (a[0] + 0.5 * ofs_u, a[1] + 0.5 * ofs_v)
            mid_b = (b_pt[0] + 0.5 * ofs_u, b_pt[1] + 0.5 * ofs_v)

            if BA <= 1e-9:
                bend_lines_out.append(
                    (
                        a,
                        b_pt,
                        {
                            "angle": rec["bend"].angle_deg,
                            "radius": rec["bend"].radius,
                            "direction": rec["direction"],
                        },
                    )
                )
                continue

            # Strip corners: hinge endpoints + offset endpoints.
            p1 = a
            p2 = b_pt
            p3 = (b_pt[0] + ofs_u, b_pt[1] + ofs_v)
            p4 = (a[0] + ofs_u, a[1] + ofs_v)
            bend_strips.append([p1, p2, p3, p4])

            bend_lines_out.append(
                (
                    mid_a,
                    mid_b,
                    {
                        "angle": rec["bend"].angle_deg,
                        "radius": rec["bend"].radius,
                        "direction": rec["direction"],
                    },
                )
            )

        # ------------------------------------------------------------------
        # Combine outer contours via shapely if available; otherwise emit
        # per-face polygons (which the DXF writer accepts).
        # ------------------------------------------------------------------
        outer_contour, holes_final = _combine_polygons(face_outers + bend_strips, face_holes)

        # Bounding box from outer contours.
        all_pts: list[tuple[float, float]] = []
        for poly in outer_contour:
            all_pts.extend(poly)
        if not all_pts:
            return {}
        xs = [p[0] for p in all_pts]
        ys = [p[1] for p in all_pts]
        bbox = (min(xs), min(ys), max(xs), max(ys))

        return {
            "outer_contour": outer_contour,
            "holes": holes_final,
            "bend_lines": bend_lines_out,
            "thickness": result.thickness_mean,
            "bbox": bbox,
        }


# ---------------------------------------------------------------------------
# Standalone helpers (module-level so they're easy to test and reuse)
# ---------------------------------------------------------------------------


@dataclass
class _PlaneFrame:
    """A 2D coordinate frame on a 3D plane."""

    origin: tuple[float, float, float]
    u_axis: tuple[float, float, float]
    v_axis: tuple[float, float, float]
    normal: tuple[float, float, float]


def _build_plane_frame(
    normal: tuple[float, float, float],
    origin: tuple[float, float, float],
) -> _PlaneFrame:
    """Build a stable (u, v, n) frame on a plane defined by a normal + point."""

    n = _unit(normal)
    ux = (1.0, 0.0, 0.0)
    if abs(_dot(ux, n)) > 0.9:
        ux = (0.0, 1.0, 0.0)
    v_axis = _unit(_cross(n, ux))
    u_axis = _unit(_cross(v_axis, n))
    return _PlaneFrame(origin=origin, u_axis=u_axis, v_axis=v_axis, normal=n)


def _project_to_uv(p3: tuple[float, float, float], frame: _PlaneFrame) -> tuple[float, float]:
    dx = p3[0] - frame.origin[0]
    dy = p3[1] - frame.origin[1]
    dz = p3[2] - frame.origin[2]
    u = dx * frame.u_axis[0] + dy * frame.u_axis[1] + dz * frame.u_axis[2]
    v = dx * frame.v_axis[0] + dy * frame.v_axis[1] + dz * frame.v_axis[2]
    return (u, v)


def _project_dir_to_uv(d3: tuple[float, float, float], frame: _PlaneFrame) -> tuple[float, float]:
    u = d3[0] * frame.u_axis[0] + d3[1] * frame.u_axis[1] + d3[2] * frame.u_axis[2]
    v = d3[0] * frame.v_axis[0] + d3[1] * frame.v_axis[1] + d3[2] * frame.v_axis[2]
    return (u, v)


def _normalize_2d(d: tuple[float, float]) -> tuple[float, float]:
    n = math.sqrt(d[0] ** 2 + d[1] ** 2)
    if n < 1e-12:
        return (1.0, 0.0)
    return (d[0] / n, d[1] / n)


def _rotation_about_line(axis_dir, axis_loc, angle_rad):
    """Return a 4x4 numpy matrix rotating about the line (axis_loc, axis_dir).

    ``axis_dir`` must be a unit vector. Uses Rodrigues' formula.
    """

    import numpy as np

    k = np.asarray(axis_dir, dtype=float)
    k = k / (np.linalg.norm(k) + 1e-18)
    K = np.array(
        [
            [0.0, -k[2], k[1]],
            [k[2], 0.0, -k[0]],
            [-k[1], k[0], 0.0],
        ]
    )
    R3 = np.eye(3) + math.sin(angle_rad) * K + (1.0 - math.cos(angle_rad)) * (K @ K)

    # Translate so axis passes through axis_loc:
    # T = Translate(loc) * R3_homog * Translate(-loc)
    p = np.asarray(axis_loc, dtype=float)
    T = np.eye(4)
    T[:3, :3] = R3
    T[:3, 3] = p - R3 @ p
    return T


def _unfold_face(
    face, transform_4x4, base_frame: _PlaneFrame, *, chord_tol_mm: float = 0.5
) -> tuple[list[tuple[float, float]], list[list[tuple[float, float]]]]:
    """Sample ``face``'s wires, apply ``transform_4x4`` (3D), and project to (u,v).

    Returns ``(outer_polyline_2d, list_of_hole_polylines_2d)``.

    The transform is applied in 3D first; then each transformed point is
    projected onto the base plane using ``base_frame``. For a flat face whose
    plane lies exactly in the base plane after the transform, this is the
    canonical 2D unfold.
    """

    import numpy as np
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepTools import BRepTools_WireExplorer
    from OCP.GCPnts import GCPnts_QuasiUniformDeflection
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    T = np.asarray(transform_4x4, dtype=float)

    def apply_T(p: tuple[float, float, float]) -> tuple[float, float, float]:
        v = T @ np.array([p[0], p[1], p[2], 1.0])
        return (float(v[0]), float(v[1]), float(v[2]))

    def sample_edge(edge) -> list[tuple[float, float, float]]:
        """Sample an edge into 3D points at ~chord_tol mm chord deflection."""
        try:
            bac = BRepAdaptor_Curve(edge)
        except Exception:
            return []
        out: list[tuple[float, float, float]] = []
        try:
            sampler = GCPnts_QuasiUniformDeflection(bac, chord_tol_mm)
            if sampler.IsDone():
                n = sampler.NbPoints()
                for i in range(1, n + 1):
                    p = sampler.Value(i)
                    out.append((p.X(), p.Y(), p.Z()))
                return out
        except Exception:
            pass
        # Fallback: parameter-step sampling.
        try:
            fp = bac.FirstParameter()
            lp = bac.LastParameter()
            n_steps = 16
            for i in range(n_steps + 1):
                u = fp + (lp - fp) * i / n_steps
                p = bac.Value(u)
                out.append((p.X(), p.Y(), p.Z()))
        except Exception:
            pass
        return out

    def wire_to_polyline(wire) -> list[tuple[float, float]]:
        pts: list[tuple[float, float, float]] = []
        try:
            we = BRepTools_WireExplorer(wire)
            while we.More():
                edge = we.Current()
                # The orientation of the edge inside the wire matters for
                # ordering; BRepTools_WireExplorer respects this.
                edge_pts = sample_edge(edge)
                # WireExplorer gives us the start vertex of each edge; we want
                # an ordered sequence. Sample includes both endpoints; we
                # append all but skip duplicates against the last accumulated
                # point. We also need to honor edge orientation.
                try:
                    if str(edge.Orientation()).endswith("REVERSED"):
                        edge_pts = list(reversed(edge_pts))
                except Exception:
                    pass
                for p in edge_pts:
                    if pts and (
                        abs(pts[-1][0] - p[0]) < 1e-9
                        and abs(pts[-1][1] - p[1]) < 1e-9
                        and abs(pts[-1][2] - p[2]) < 1e-9
                    ):
                        continue
                    pts.append(p)
                we.Next()
        except Exception as exc:
            logger.debug("wire_to_polyline failed: %s", exc)
            return []

        # Apply 3D transform and project to (u, v).
        out_uv: list[tuple[float, float]] = []
        for p in pts:
            q = apply_T(p)
            out_uv.append(_project_to_uv(q, base_frame))
        # Dedup consecutive 2D coincidences and drop the closing duplicate so
        # the polyline is a non-self-intersecting ring.
        deduped: list[tuple[float, float]] = []
        tol = 1e-6
        for uv in out_uv:
            if deduped and abs(deduped[-1][0] - uv[0]) < tol and abs(deduped[-1][1] - uv[1]) < tol:
                continue
            deduped.append(uv)
        # Drop closing point (first == last) to avoid shapely confusion.
        if (
            len(deduped) >= 2
            and abs(deduped[0][0] - deduped[-1][0]) < tol
            and abs(deduped[0][1] - deduped[-1][1]) < tol
        ):
            deduped.pop()
        return deduped

    # Iterate all wires; the largest-area one is the outer, the rest are holes.
    wire_records: list[tuple[float, list[tuple[float, float]]]] = []
    exp_w = TopExp_Explorer(face, TopAbs_WIRE)
    while exp_w.More():
        w = TopoDS.Wire_s(exp_w.Current())
        poly = wire_to_polyline(w)
        if len(poly) >= 3:
            # Signed area to size the wire.
            area = 0.0
            for i in range(len(poly)):
                x1, y1 = poly[i]
                x2, y2 = poly[(i + 1) % len(poly)]
                area += x1 * y2 - x2 * y1
            wire_records.append((abs(area) / 2.0, poly))
        exp_w.Next()

    if not wire_records:
        return [], []
    wire_records.sort(key=lambda t: -t[0])
    outer = wire_records[0][1]
    # Drop degenerate wires (area < 1e-3 mm²) - these arise from cylinder seams
    # or other topological artefacts during wire sampling.
    holes = [w[1] for w in wire_records[1:] if w[0] > 1e-3]
    return outer, holes


def _combine_polygons(
    outers: list[list[tuple[float, float]]],
    holes: list[list[tuple[float, float]]],
) -> tuple[list[list[tuple[float, float]]], list[list[tuple[float, float]]]]:
    """Combine outer polygons via boolean union; subtract holes.

    If shapely is unavailable, returns ``(outers, holes)`` unchanged - the DXF
    writer accepts multiple disjoint outer contours.
    """

    if not outers:
        return [], []
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
        from shapely.validation import make_valid
    except Exception:
        logger.info("shapely unavailable; emitting per-face contours")
        return outers, holes

    polys = []
    for poly in outers:
        if len(poly) < 3:
            continue
        try:
            p = Polygon(poly)
            if not p.is_valid:
                p = make_valid(p)
            if p.is_empty:
                continue
            polys.append(p)
        except Exception:
            continue
    if not polys:
        return outers, holes

    try:
        merged = unary_union(polys)
    except Exception as exc:
        logger.debug("unary_union failed: %s", exc)
        return outers, holes

    # Subtract holes if any.
    hole_polys = []
    for h in holes:
        if len(h) < 3:
            continue
        try:
            hp = Polygon(h)
            if not hp.is_valid:
                hp = make_valid(hp)
            if hp.is_empty:
                continue
            hole_polys.append(hp)
        except Exception:
            continue
    if hole_polys:
        try:
            merged = merged.difference(unary_union(hole_polys))
        except Exception as exc:
            logger.debug("hole difference failed: %s", exc)

    out_outers: list[list[tuple[float, float]]] = []
    out_holes: list[list[tuple[float, float]]] = []

    def _extract_polygon(geom) -> None:
        # geom is a shapely Polygon
        if geom.is_empty:
            return
        ext = list(geom.exterior.coords)
        # shapely closes the ring; drop the duplicate last point.
        if len(ext) >= 2 and ext[0] == ext[-1]:
            ext = ext[:-1]
        if len(ext) >= 3:
            out_outers.append([(float(x), float(y)) for (x, y) in ext])
        for interior in geom.interiors:
            inner = list(interior.coords)
            if len(inner) >= 2 and inner[0] == inner[-1]:
                inner = inner[:-1]
            if len(inner) >= 3:
                out_holes.append([(float(x), float(y)) for (x, y) in inner])

    geom_type = merged.geom_type
    if geom_type == "Polygon":
        _extract_polygon(merged)
    elif geom_type == "MultiPolygon":
        for g in merged.geoms:
            _extract_polygon(g)
    elif geom_type == "GeometryCollection":
        for g in merged.geoms:
            if g.geom_type == "Polygon":
                _extract_polygon(g)
    else:
        # Unknown / degenerate geometry: fall back to per-face contours.
        return outers, holes

    if not out_outers:
        return outers, holes
    return out_outers, out_holes
