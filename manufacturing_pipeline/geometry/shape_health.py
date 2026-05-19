"""Shape healing and validity reporting.

Thin wrappers around OCCT's ShapeFix and BRepCheck that:
  - never raise on bad input,
  - always return *something usable* (the original shape on failure),
  - emit logging.warning when healing/validation fails or fixes something material.

See plan §4.7 (Robustness Notes) and §3.7.
"""

from __future__ import annotations

import logging
from typing import Any

from OCP.BRep import BRep_Tool
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.ShapeFix import ShapeFix_Shape
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

logger = logging.getLogger(__name__)


def _has_face_without_surface(shape: Any) -> bool:
    """True if *shape* contains any TopoDS_Face without a Geom_Surface.

    ShapeFix_Shape segfaults inside OCCT when asked to heal a tessellation-only
    face (no surface, no wires). We use this pre-flight check to bail out and
    return the original shape untouched, letting the FeatureExtractor pick the
    mesh fallback path.
    """

    try:
        exp = TopExp_Explorer(shape, TopAbs_FACE)
    except Exception:  # pragma: no cover - defensive
        return False
    while exp.More():
        try:
            face = TopoDS.Face_s(exp.Current())
            if BRep_Tool.Surface_s(face) is None:
                return True
        except Exception:  # pragma: no cover - defensive
            pass
        exp.Next()
    return False


def heal_shape(shape: Any) -> Any:
    """Run ShapeFix_Shape on *shape* and return the healed copy.

    ShapeFix_Shape internally drives ShapeFix_Wire / ShapeFix_Face / ShapeFix_Solid,
    so the explicit per-class fixers in the spec are subsumed by a single Perform()
    pass. We never raise: on any failure we return the input shape and log a warning.

    Tessellation-only shapes (faces with attached Poly_Triangulation but no
    Geom_Surface) are returned untouched, because ShapeFix_Shape can segfault
    on them.
    """

    if shape is None:
        logger.warning("heal_shape: received None, returning as-is")
        return shape
    try:
        if hasattr(shape, "IsNull") and shape.IsNull():
            logger.warning("heal_shape: shape is null, returning as-is")
            return shape
    except Exception:  # pragma: no cover - defensive
        pass

    if _has_face_without_surface(shape):
        logger.debug("heal_shape: shape has tessellation-only faces, skipping ShapeFix")
        return shape

    try:
        fixer = ShapeFix_Shape(shape)
        fixer.Perform()  # bool return; False can mean "nothing to fix"
        healed = fixer.Shape()
        if healed is None or (hasattr(healed, "IsNull") and healed.IsNull()):
            logger.warning("heal_shape: ShapeFix returned null shape, using original")
            return shape
        return healed
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("heal_shape: ShapeFix raised %s, using original", exc)
        return shape


def validity_report(shape: Any) -> dict[str, Any]:
    """Run BRepCheck_Analyzer over *shape*.

    Returns a dict with:
      - ``is_valid``: overall boolean
      - ``n_faults``: number of sub-shapes that report invalid
      - ``details``: list of `{shape_type, status}` strings (best-effort)
    """

    report: dict[str, Any] = {"is_valid": False, "n_faults": 0, "details": []}
    details: list[str] = report["details"]

    if shape is None:
        return report
    try:
        if hasattr(shape, "IsNull") and shape.IsNull():
            return report
    except Exception:  # pragma: no cover
        return report

    try:
        analyzer = BRepCheck_Analyzer(shape)
        report["is_valid"] = bool(analyzer.IsValid())
    except Exception as exc:  # pragma: no cover
        logger.warning("validity_report: analyzer construction failed: %s", exc)
        return report

    if report["is_valid"]:
        return report

    # Best-effort fault counting: walk shells/faces/edges/vertices.
    try:
        from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_VERTEX
        from OCP.TopExp import TopExp_Explorer

        for tag, abs_type in (
            ("shell", TopAbs_SHELL),
            ("face", TopAbs_FACE),
            ("edge", TopAbs_EDGE),
            ("vertex", TopAbs_VERTEX),
        ):
            exp = TopExp_Explorer(shape, abs_type)
            while exp.More():
                sub = exp.Current()
                try:
                    if not analyzer.IsValid(sub):
                        report["n_faults"] += 1
                        details.append(tag)
                except Exception:
                    pass
                exp.Next()
    except Exception as exc:  # pragma: no cover
        logger.warning("validity_report: enumeration failed: %s", exc)

    return report
