"""Render 3D meshes (folded STEP solid + unfolded flat-pattern blank) as GLB.

Provides two entry points used by the web viewer:

* :func:`folded_glb` -- tessellate the original ``TopoDS_Solid`` and return GLB.
* :func:`unfolded_glb` -- extrude a :class:`FlatPattern` to thickness and
  return GLB.

Both return binary GLB bytes ready to be served as ``model/gltf-binary``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from manufacturing_pipeline.web.step_to_svg import find_solid_for_part

logger = logging.getLogger(__name__)


def _tessellate_solid(solid, deflection: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    """Tessellate a TopoDS_Shape into (vertices, triangle_indices)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    BRepMesh_IncrementalMesh(solid, deflection, True, 0.5, True).Perform()

    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    explorer = TopExp_Explorer(solid, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        loc = face.Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            explorer.Next()
            continue
        trsf = loc.Transformation()
        offset = len(verts)
        for i in range(1, tri.NbNodes() + 1):
            p = tri.Node(i).Transformed(trsf)
            verts.append((p.X(), p.Y(), p.Z()))
        reverse = face.Orientation() != 0  # 0 = forward
        for i in range(1, tri.NbTriangles() + 1):
            t = tri.Triangle(i)
            a, b, c = t.Get()
            if reverse:
                faces.append((offset + a - 1, offset + c - 1, offset + b - 1))
            else:
                faces.append((offset + a - 1, offset + b - 1, offset + c - 1))
        explorer.Next()
    if not verts:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    return np.asarray(verts, dtype=np.float64), np.asarray(faces, dtype=np.int64)


def _mesh_to_glb(
    vertices: np.ndarray,
    faces: np.ndarray,
    *,
    color: tuple[float, float, float, float] = (0.55, 0.70, 0.90, 1.0),
) -> bytes:
    """Wrap a mesh in trimesh and export as GLB bytes."""
    import trimesh

    if len(vertices) == 0 or len(faces) == 0:
        # Empty fallback so the route still returns valid GLB.
        mesh = trimesh.creation.box(extents=(1, 1, 1))
    else:
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
        try:
            mesh.merge_vertices()
        except Exception:
            pass
    mesh.visual = trimesh.visual.ColorVisuals(
        mesh,
        face_colors=np.tile((np.asarray(color) * 255).astype(np.uint8), (len(mesh.faces), 1)),
    )
    return mesh.export(file_type="glb")


def folded_glb(source_path: Path, product_id: str, *, deflection: float = 0.5) -> bytes | None:
    """Tessellate the source solid for ``product_id`` and return GLB bytes."""
    solid = find_solid_for_part(source_path, product_id)
    if solid is None:
        return None
    try:
        v, f = _tessellate_solid(solid, deflection=deflection)
        return _mesh_to_glb(v, f, color=(0.62, 0.74, 0.88, 1.0))
    except Exception as exc:  # noqa: BLE001
        logger.warning("folded_glb failed for %s: %s", product_id, exc)
        return None


def unfolded_glb(
    flat_pattern, *, color: tuple[float, float, float, float] = (0.85, 0.55, 0.45, 1.0)
) -> bytes | None:
    """Extrude a :class:`FlatPattern` to its thickness and return GLB bytes.

    The outer contour and holes are combined into a shapely polygon-with-holes,
    triangulated and extruded. Bend lines are not rendered in 3D (they are
    drawn on the 2D SVG view) but the flat solid carries the right footprint.
    """
    try:
        import trimesh
        from shapely.geometry import MultiPolygon, Polygon
        from shapely.ops import unary_union

        if not flat_pattern.outer_contour:
            return None
        outers = [Polygon(p) for p in flat_pattern.outer_contour if len(p) >= 3]
        holes = [Polygon(h) for h in flat_pattern.holes if len(h) >= 3]
        if not outers:
            return None
        # Combine outers via union; subtract holes.
        combined = unary_union(outers)
        for h in holes:
            combined = combined.difference(h)

        thickness = max(float(flat_pattern.thickness or 1.0), 0.1)

        def _extrude(geom) -> trimesh.Trimesh:
            return trimesh.creation.extrude_polygon(geom, height=thickness)

        if combined.is_empty:
            return None
        if isinstance(combined, MultiPolygon):
            meshes = [_extrude(g) for g in combined.geoms if not g.is_empty]
            mesh = trimesh.util.concatenate(meshes)
        else:
            mesh = _extrude(combined)

        mesh.visual = trimesh.visual.ColorVisuals(
            mesh,
            face_colors=np.tile((np.asarray(color) * 255).astype(np.uint8), (len(mesh.faces), 1)),
        )
        return mesh.export(file_type="glb")
    except Exception as exc:  # noqa: BLE001
        logger.warning("unfolded_glb failed: %s", exc)
        return None


__all__ = ["folded_glb", "unfolded_glb"]
