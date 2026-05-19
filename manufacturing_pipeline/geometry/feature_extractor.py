"""Single-pass manufacturing-feature extractor.

See plan §4.1. The :class:`FeatureExtractor` walks each :class:`TopoDS_Solid`
exactly once and emits a fully-populated :class:`ManufacturingFeatures`.

Hole counts and hole diameters are **not** computed here -- they are produced
by :class:`manufacturing_pipeline.geometry.hole_analyzer.HoleAnalyzer` and
merged into the feature record by the assembly orchestrator.

The extractor is defensive: each subtask is wrapped in try/except. If anything
fails we fall back to that field's dataclass default and add a warning log.
``extract`` must never raise.
"""

from __future__ import annotations

import logging
import math

import numpy as np
from OCP.Bnd import Bnd_Box, Bnd_OBB
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepGProp import BRepGProp
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.GeomAbs import (
    GeomAbs_BezierCurve,
    GeomAbs_BezierSurface,
    GeomAbs_BSplineCurve,
    GeomAbs_BSplineSurface,
    GeomAbs_Circle,
    GeomAbs_Cone,
    GeomAbs_Cylinder,
    GeomAbs_Ellipse,
    GeomAbs_Line,
    GeomAbs_OtherSurface,
    GeomAbs_Plane,
    GeomAbs_Sphere,
    GeomAbs_Torus,
)
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS

from ..config.classification_variables import (
    CROSS_SECTION_N_SLICES,
    HOLLOW_BBOX_FILL_MAX,
    HOLLOW_BBOX_FILL_MIN,
    MESH_DEFLECTION_DEFAULT_MM,
    MESH_DEFLECTION_DIAG_FRACTION,
    MESH_DEFLECTION_MAX_MM,
    OBB_PRINCIPAL_AXIS_MIN_RATIO,
)
from .cross_section import is_constant, principal_axis_obb, slice_solid
from .shape_health import heal_shape
from .types import CrossSection, ManufacturingFeatures

logger = logging.getLogger(__name__)


_SURFACE_KEYS = (
    "planar",
    "cylindrical",
    "conical",
    "toroidal",
    "spherical",
    "bspline",
    "other",
)


def _surface_key(stype) -> str:
    if stype == GeomAbs_Plane:
        return "planar"
    if stype == GeomAbs_Cylinder:
        return "cylindrical"
    if stype == GeomAbs_Cone:
        return "conical"
    if stype == GeomAbs_Torus:
        return "toroidal"
    if stype == GeomAbs_Sphere:
        return "spherical"
    if stype in (GeomAbs_BSplineSurface, GeomAbs_BezierSurface):
        return "bspline"
    return "other"


def _curve_key(ctype) -> str | None:
    if ctype == GeomAbs_Line:
        return "line"
    if ctype == GeomAbs_Circle:
        return "circle"
    if ctype == GeomAbs_Ellipse:
        return "ellipse"
    if ctype in (GeomAbs_BSplineCurve, GeomAbs_BezierCurve):
        return "bspline"
    return None


def _empty_surface_pct() -> dict[str, float]:
    return {k: 0.0 for k in _SURFACE_KEYS}


def _empty_edge_counts() -> dict[str, int]:
    return {"line": 0, "circle": 0, "ellipse": 0, "bspline": 0}


# ---------------------------------------------------------------------------
# Subtask helpers
# ---------------------------------------------------------------------------


def _is_shape_usable(shape) -> bool:
    if shape is None:
        return False
    try:
        if hasattr(shape, "IsNull") and shape.IsNull():
            return False
    except Exception:
        return False
    return True


def _has_any_face(shape) -> bool:
    try:
        exp = TopExp_Explorer(shape, TopAbs_FACE)
        return bool(exp.More())
    except Exception:
        return False


def _volume_and_area(shape) -> tuple[float, float]:
    volume = 0.0
    sa = 0.0
    try:
        vprops = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, vprops)
        volume = abs(float(vprops.Mass()))  # reject negative volumes
    except Exception as exc:
        logger.warning("volume failed: %s", exc)
    try:
        sprops = GProp_GProps()
        BRepGProp.SurfaceProperties_s(shape, sprops)
        sa = float(sprops.Mass())
    except Exception as exc:
        logger.warning("surface area failed: %s", exc)
    return volume, sa


def _bbox_dims_sorted(shape) -> tuple[float, float, float]:
    """Sorted descending OBB extents, with AABB fallback when OBB is unstable."""

    try:
        obb = Bnd_OBB()
        BRepBndLib.AddOBB_s(shape, obb)
        halves = sorted((obb.XHSize(), obb.YHSize(), obb.ZHSize()), reverse=True)
        ratio = (halves[0] / halves[1]) if halves[1] > 1e-12 else float("inf")
        dims = tuple(h * 2.0 for h in halves)
        if ratio >= OBB_PRINCIPAL_AXIS_MIN_RATIO:
            return dims
        logger.debug("bbox: OBB ratio %.3f < %.3f, using AABB", ratio, OBB_PRINCIPAL_AXIS_MIN_RATIO)
    except Exception as exc:  # pragma: no cover
        logger.warning("OBB failed: %s; using AABB", exc)

    try:
        box = Bnd_Box()
        BRepBndLib.Add_s(shape, box)
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
        sorted_dims = sorted((xmax - xmin, ymax - ymin, zmax - zmin), reverse=True)
        return (float(sorted_dims[0]), float(sorted_dims[1]), float(sorted_dims[2]))
    except Exception as exc:  # pragma: no cover
        logger.warning("AABB failed: %s", exc)
        return (0.0, 0.0, 0.0)


def _iter_faces(shape):
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        yield TopoDS.Face_s(exp.Current())
        exp.Next()


def _iter_edges(shape):
    exp = TopExp_Explorer(shape, TopAbs_EDGE)
    while exp.More():
        yield TopoDS.Edge_s(exp.Current())
        exp.Next()


def _face_area(face) -> float:
    try:
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        return float(props.Mass())
    except Exception:
        return 0.0


def _surface_pct_and_face_area_top(
    shape, total_surface: float
) -> tuple[dict[str, float], list[float], bool, list[float]]:
    """Walk faces once; compute surface-type pct, top-3 planar fractions,
    whether we saw any face with an attached Geom_Surface (brep source), and
    the descending-sorted list of absolute planar face areas."""

    pct = _empty_surface_pct()
    planar_areas: list[float] = []
    saw_brep = False

    if total_surface <= 1e-12:
        return pct, [0.0, 0.0, 0.0], saw_brep, []

    for face in _iter_faces(shape):
        area = _face_area(face)
        if area <= 0.0:
            continue
        # A face without an attached Geom_Surface (triangulation-only, as
        # sometimes seen in tessellated STEP exports) cannot be classified by
        # type; count it under "other" and leave saw_brep alone so the caller
        # can fall through to the mesh-based metrics.
        has_surface = False
        try:
            has_surface = BRep_Tool.Surface_s(face) is not None
        except Exception:  # pragma: no cover - defensive
            has_surface = False
        if not has_surface:
            pct["other"] += area
            continue
        try:
            adaptor = BRepAdaptor_Surface(face)
            stype = adaptor.GetType()
        except Exception:
            pct["other"] += area
            continue
        if stype == GeomAbs_OtherSurface:
            pct["other"] += area
            continue
        saw_brep = True
        key = _surface_key(stype)
        pct[key] += area
        if key == "planar":
            planar_areas.append(area)

    # Normalise to fractions of total surface area.
    for k in pct:
        pct[k] = pct[k] / total_surface

    planar_areas.sort(reverse=True)
    top3 = [planar_areas[i] / total_surface if i < len(planar_areas) else 0.0 for i in range(3)]
    return pct, top3, saw_brep, planar_areas


def _pocket_complexity(planar_areas: list[float]) -> float:
    """Score 0..1: high when many small planar facets dominate.

    Triggers on parts with many machined pocket walls/floors. Returns 0.0 when
    fewer than 8 planar faces are present (simple shapes shouldn't trigger).
    Otherwise: ``(1 - top1_share) * min(n_planar / 30, 1.0)`` where ``top1_share``
    is the largest planar face divided by the total planar area.
    """

    n_planar = len(planar_areas)
    if n_planar < 8:
        return 0.0
    total = sum(planar_areas)
    if total <= 0.0:
        return 0.0
    top1 = planar_areas[0] / total
    score = (1.0 - top1) * min(n_planar / 30.0, 1.0)
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return float(score)


def _edge_counts_and_radius(shape) -> tuple[dict[str, int], tuple[float, float]]:
    counts = _empty_edge_counts()
    radii: list[float] = []
    for edge in _iter_edges(shape):
        try:
            adaptor = BRepAdaptor_Curve(edge)
            ctype = adaptor.GetType()
        except Exception:
            continue
        key = _curve_key(ctype)
        if key is not None:
            counts[key] += 1
        if ctype == GeomAbs_Circle:
            try:
                radii.append(float(adaptor.Circle().Radius()))
            except Exception:
                pass
    if not radii:
        return counts, (0.0, 0.0)
    return counts, (max(radii), min(radii))


def _shell_count(shape) -> int:
    exp = TopExp_Explorer(shape, TopAbs_SHELL)
    n = 0
    while exp.More():
        n += 1
        exp.Next()
    return n


def _sa_v_ratio(volume: float, surface_area: float) -> float:
    if volume <= 1e-12:
        return 0.0
    return float(surface_area / (volume ** (2.0 / 3.0)))


def _bounding_cylinder_fit(shape, volume: float, dims: tuple[float, float, float]) -> float:
    """volume / (pi * r_max^2 * L) using OBB principal axis."""

    if volume <= 1e-12:
        return 0.0
    try:
        _, axis, axis_dims = principal_axis_obb(shape)
        L = axis_dims[0]
        if L <= 1e-9:
            return 0.0
        # Radial extent = max distance from principal axis (centroid line) to any
        # OBB corner -> approximated by half-diagonal of the cross-section.
        w, t = axis_dims[1], axis_dims[2]
        r_max = math.hypot(w, t) / 2.0
        cyl_vol = math.pi * r_max * r_max * L
        if cyl_vol <= 1e-12:
            return 0.0
        return float(min(volume / cyl_vol, 1.0))
    except Exception as exc:
        logger.warning("bounding_cylinder_fit failed: %s", exc)
        return 0.0


def _convex_hull_volume_ratio(solid, volume: float) -> float | None:
    """Return ``volume / hull_volume`` using scipy's 3D convex hull.

    Steps:
      1. Mesh ``solid`` with :class:`BRepMesh_IncrementalMesh` using a
         deflection of ``min(0.5, 5% of bbox-diagonal)`` capped at 2 mm. A
         smaller deflection on very small parts keeps the triangulation
         meaningful.
      2. Walk every face, pull its triangulation via ``BRep_Tool.Triangulation_s``
         and transform each node into world coordinates via the face location.
      3. Build a (N, 3) numpy array and feed it to :class:`scipy.spatial.ConvexHull`.
      4. Return ``volume / hull.volume`` clamped to ``(0, 1.5]``.

    Returns ``None`` if scipy is missing, the mesh contains fewer than four
    unique points, or any exception is raised — the caller then keeps the
    dataclass default.
    """

    if volume <= 0.0:
        return None

    try:
        from scipy.spatial import ConvexHull  # local import: keep module loadable
    except Exception as exc:  # pragma: no cover - scipy is a hard dep
        logger.debug("convex_hull_volume_ratio: scipy import failed: %s", exc)
        return None

    try:
        # Mesh deflection: 5% of bbox diagonal, capped at 2 mm, never above 0.5
        # for normal-sized parts; tiny parts (< 10 mm diagonal) use a smaller
        # value to keep the triangulation resolved.
        diag = 0.0
        try:
            box = Bnd_Box()
            BRepBndLib.Add_s(solid, box)
            xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
            diag = math.sqrt((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2)
        except Exception:
            pass
        deflection = (
            min(MESH_DEFLECTION_DEFAULT_MM, MESH_DEFLECTION_DIAG_FRACTION * diag)
            if diag > 0.0
            else MESH_DEFLECTION_DEFAULT_MM
        )
        deflection = min(deflection, MESH_DEFLECTION_MAX_MM)
        if deflection <= 0.0:
            deflection = MESH_DEFLECTION_DEFAULT_MM

        BRepMesh_IncrementalMesh(solid, deflection)

        pts: list[tuple[float, float, float]] = []
        for face in _iter_faces(solid):
            try:
                loc = TopLoc_Location()
                tri = BRep_Tool.Triangulation_s(face, loc)
                if tri is None:
                    continue
                trsf = loc.Transformation()
                for i in range(1, tri.NbNodes() + 1):
                    p = tri.Node(i).Transformed(trsf)
                    pts.append((p.X(), p.Y(), p.Z()))
            except Exception as exc:  # pragma: no cover
                logger.debug("convex_hull: face skipped: %s", exc)

        if len(pts) < 4:
            return None

        arr = np.asarray(pts, dtype=float)
        hull = ConvexHull(arr)
        if hull.volume <= 1e-12:
            return None
        ratio = volume / float(hull.volume)
        # Clamp: triangulation under-shoots curved surfaces so the hull volume
        # can be slightly smaller than the true solid volume.
        if ratio <= 0.0:
            return None
        return min(ratio, 1.5)
    except Exception as exc:
        logger.debug("convex_hull_volume_ratio: failed: %s", exc)
        return None


def _cross_section_features(shape, n_slices: int) -> tuple[bool, dict, list[CrossSection]]:
    """Slice and report constancy + the middle slice's signature.

    The signature dict is now produced by ``slice_solid`` itself (see
    :func:`compute_signature`); we extend it with the hollow_ratio metric so
    downstream consumers retain that field.
    """

    sections = slice_solid(shape, n_slices)
    if not sections:
        return False, {}, []

    constant = is_constant(sections)
    mid = sections[len(sections) // 2]
    bbox_area = mid.bbox_2d[0] * mid.bbox_2d[1]
    sig = dict(mid.signature) if mid.signature else {}
    sig.setdefault("n_inner_wires", mid.n_inner_wires)
    sig["hollow_ratio"] = (mid.area / bbox_area) if bbox_area > 1e-9 else 0.0
    return constant, sig, sections


# ---------------------------------------------------------------------------
# Mesh fallback (no Geom_Surface attached to any face)
# ---------------------------------------------------------------------------


def _mesh_features(shape) -> tuple[float, tuple[float, float, float], float]:
    """Return ``(volume, bbox_dims_sorted, surface_area)`` from triangulation."""

    try:
        BRepMesh_IncrementalMesh(shape, MESH_DEFLECTION_DEFAULT_MM)
    except Exception as exc:
        logger.warning("meshing failed: %s", exc)
        return 0.0, (0.0, 0.0, 0.0), 0.0

    pts: list[tuple[float, float, float]] = []
    tris: list[tuple[int, int, int]] = []

    for face in _iter_faces(shape):
        try:
            loc = TopLoc_Location()
            tri = BRep_Tool.Triangulation_s(face, loc)
            if tri is None:
                continue
            base = len(pts)
            trsf = loc.Transformation()
            for i in range(1, tri.NbNodes() + 1):
                p = tri.Node(i).Transformed(trsf)
                pts.append((p.X(), p.Y(), p.Z()))
            for i in range(1, tri.NbTriangles() + 1):
                t = tri.Triangle(i)
                a, b, c = t.Get()
                tris.append((base + a - 1, base + b - 1, base + c - 1))
        except Exception as exc:  # pragma: no cover
            logger.debug("mesh fallback face skipped: %s", exc)

    if not pts:
        return 0.0, (0.0, 0.0, 0.0), 0.0

    arr = np.asarray(pts)
    # Divergence-theorem volume.
    volume = 0.0
    sa = 0.0
    for i, j, k in tris:
        p1, p2, p3 = arr[i], arr[j], arr[k]
        cross = np.cross(p2 - p1, p3 - p1)
        sa += 0.5 * float(np.linalg.norm(cross))
        volume += float(np.dot(p1, cross) / 6.0)
    volume = abs(volume)

    # PCA-derived bbox extents (ranges along principal axes).
    centred = arr - arr.mean(axis=0)
    if centred.size > 0:
        try:
            cov = np.cov(centred.T)
            _, vecs = np.linalg.eigh(cov)
            projected = centred @ vecs
            dims = projected.max(axis=0) - projected.min(axis=0)
            sorted_dims = tuple(float(d) for d in sorted(dims, reverse=True))
        except Exception:
            ranges = arr.max(axis=0) - arr.min(axis=0)
            sorted_dims = tuple(float(d) for d in sorted(ranges, reverse=True))
    else:  # pragma: no cover
        sorted_dims = (0.0, 0.0, 0.0)

    return volume, sorted_dims, sa  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------


class FeatureExtractor:
    """Single-pass feature extractor for a healed :class:`TopoDS_Solid`.

    Parameters
    ----------
    n_slices:
        Number of cross-section slices.
    enable_cross_section:
        If False, skip cross-section slicing entirely (faster path for
        callers that only want bulk-volume / surface metrics).
    """

    def __init__(
        self,
        *,
        n_slices: int = CROSS_SECTION_N_SLICES,
        enable_cross_section: bool = True,
    ) -> None:
        self.n_slices = n_slices
        self.enable_cross_section = enable_cross_section

    def extract(self, solid) -> ManufacturingFeatures:
        """Return a fully-populated :class:`ManufacturingFeatures`.

        Never raises. On failure of any subtask the corresponding field is
        left at its dataclass default and a warning is logged.

        NOTE on holes: ``hole_count`` and ``hole_diameters`` are intentionally
        left at their defaults (0, []). The :class:`HoleAnalyzer` (separate
        module/agent) fills those in, and the assembly orchestrator merges
        them onto the record produced here.
        """

        feats = ManufacturingFeatures(
            surface_pct=_empty_surface_pct(),
            edge_counts=_empty_edge_counts(),
            face_area_top=[0.0, 0.0, 0.0],
        )

        if not _is_shape_usable(solid):
            logger.warning("extract: input shape is None / null; returning defaults")
            return feats

        try:
            shape = heal_shape(solid)
        except Exception as exc:  # pragma: no cover - heal_shape is defensive
            logger.warning("extract: heal_shape raised %s; using original", exc)
            shape = solid

        if not _is_shape_usable(shape):
            return feats

        has_faces = _has_any_face(shape)

        try:
            volume, surface_area = _volume_and_area(shape)
            feats.volume = volume
            feats.surface_area = surface_area
        except Exception as exc:  # pragma: no cover
            logger.warning("volume/area block failed: %s", exc)

        # If no faces are present at all, the part is degenerate — return early
        # with defaults but keep volume/area numbers we could compute.
        if not has_faces:
            return feats

        try:
            feats.bbox_dims_sorted = _bbox_dims_sorted(shape)
        except Exception as exc:  # pragma: no cover
            logger.warning("bbox failed: %s", exc)

        L, W, T = feats.bbox_dims_sorted
        try:
            feats.aspect_ratio = float(L / W) if W > 1e-9 else 0.0
            feats.thickness_ratio = float(T / L) if L > 1e-9 else 0.0
        except Exception as exc:  # pragma: no cover
            logger.warning("ratios failed: %s", exc)

        try:
            pct, top3, saw_brep, planar_areas = _surface_pct_and_face_area_top(
                shape, feats.surface_area
            )
            feats.surface_pct = pct
            feats.face_area_top = top3
            feats.source = "brep" if saw_brep else "mesh"
            feats.pocket_complexity = _pocket_complexity(planar_areas)
        except Exception as exc:
            logger.warning("surface composition failed: %s", exc)

        # Mesh fallback when there is no usable B-rep surface info on faces.
        if feats.source == "mesh":
            try:
                m_vol, m_dims, m_sa = _mesh_features(shape)
                if feats.volume <= 0.0:
                    feats.volume = m_vol
                if feats.surface_area <= 0.0:
                    feats.surface_area = m_sa
                if feats.bbox_dims_sorted == (0.0, 0.0, 0.0):
                    feats.bbox_dims_sorted = m_dims
                    L, W, T = m_dims
                    if W > 1e-9:
                        feats.aspect_ratio = float(L / W)
                    if L > 1e-9:
                        feats.thickness_ratio = float(T / L)
            except Exception as exc:  # pragma: no cover
                logger.warning("mesh fallback failed: %s", exc)

        try:
            counts, edge_radii = _edge_counts_and_radius(shape)
            feats.edge_counts = counts
            feats.edge_radius = edge_radii
        except Exception as exc:
            logger.warning("edge stats failed: %s", exc)

        try:
            feats.sa_v_ratio = _sa_v_ratio(feats.volume, feats.surface_area)
        except Exception as exc:  # pragma: no cover
            logger.warning("sa_v_ratio failed: %s", exc)

        try:
            feats.bounding_cylinder_fit_pct = _bounding_cylinder_fit(
                shape, feats.volume, feats.bbox_dims_sorted
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("bounding cylinder fit failed: %s", exc)

        try:
            ratio = _convex_hull_volume_ratio(shape, feats.volume)
            if ratio is None:
                logger.debug("convex_hull_volume_ratio: helper returned None, falling back to 1.0")
                feats.convex_hull_volume_ratio = 1.0
            else:
                feats.convex_hull_volume_ratio = float(ratio)
        except Exception as exc:  # pragma: no cover
            logger.warning("convex hull ratio failed: %s; using 1.0", exc)
            feats.convex_hull_volume_ratio = 1.0

        try:
            feats.inner_shell_count = max(0, _shell_count(shape) - 1)
        except Exception as exc:  # pragma: no cover
            logger.warning("shell count failed: %s", exc)

        # Hollow predicate (combines topology + volumetric fill).
        try:
            bbox_vol = float(L * W * T)
            bbox_fill = (feats.volume / bbox_vol) if bbox_vol > 1e-9 else 0.0
            volumetric_hollow = HOLLOW_BBOX_FILL_MIN < bbox_fill < HOLLOW_BBOX_FILL_MAX
            feats.is_hollow = (feats.inner_shell_count >= 1) or volumetric_hollow
        except Exception as exc:  # pragma: no cover
            logger.warning("hollow predicate failed: %s", exc)

        # Cross-section signature (mid slice) + constancy flag.
        if self.enable_cross_section:
            try:
                constant, signature, _sections = _cross_section_features(shape, self.n_slices)
                feats.cross_section_constant = constant
                feats.cross_section_signature = signature
            except Exception as exc:
                logger.warning("cross-section block failed: %s", exc)

        # Hole counts/diameters are populated downstream by HoleAnalyzer.
        return feats
