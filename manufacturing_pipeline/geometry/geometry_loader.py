"""STEP geometry loader.

Reads a STEP file via ``STEPControl_Reader``, converts units to millimetres,
explodes compounds into individual solids, and (optionally) heals each solid.

All OCP calls used by downstream modules to read STEP files go through this
module - no other module should reach into ``OCP.STEPControl`` directly.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


# Conversion factors from common unit names to millimetres.
_UNIT_TO_MM = {
    "mm": 1.0,
    "millimetre": 1.0,
    "millimeter": 1.0,
    "milli metre": 1.0,
    "m": 1000.0,
    "metre": 1000.0,
    "meter": 1000.0,
    "cm": 10.0,
    "centimetre": 10.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "inches": 25.4,
    "in": 25.4,
    "ft": 304.8,
    "foot": 304.8,
}


def _canonicalise_unit(name: str) -> str:
    n = name.lower().strip()
    if n in ("millimetre", "millimeter", "mm", "milli metre"):
        return "mm"
    if n in ("metre", "meter", "m"):
        return "m"
    if n in ("centimetre", "centimeter", "cm"):
        return "cm"
    if n in ("inch", "inches", "in"):
        return "inch"
    if n in ("foot", "feet", "ft"):
        return "foot"
    return "unknown"


def detect_step_units(path: str | Path) -> str:
    """Inspect a STEP file's header and return the length unit.

    Returns one of ``'mm'``, ``'m'``, ``'cm'``, ``'inch'``, ``'foot'`` or
    ``'unknown'``. Never raises - returns ``'unknown'`` on any failure.
    """

    p = Path(path)
    if not p.exists():
        return "unknown"

    # 1) Try via OCP's reader, which understands AP203/214/242 unit entries.
    try:
        from OCP.STEPControl import STEPControl_Reader
        from OCP.TColStd import TColStd_SequenceOfAsciiString

        reader = STEPControl_Reader()
        status = reader.ReadFile(str(p))
        from OCP.IFSelect import IFSelect_ReturnStatus

        if status == IFSelect_ReturnStatus.IFSelect_RetDone:
            lens = TColStd_SequenceOfAsciiString()
            angs = TColStd_SequenceOfAsciiString()
            sols = TColStd_SequenceOfAsciiString()
            try:
                reader.FileUnits(lens, angs, sols)
            except Exception:
                pass
            if lens.Size() >= 1:
                try:
                    raw = lens.Value(1).ToCString()
                except Exception:
                    raw = ""
                canon = _canonicalise_unit(str(raw))
                if canon != "unknown":
                    return canon
    except Exception as exc:
        logger.debug("detect_step_units: OCP path failed: %s", exc)

    # 2) Fallback: parse SI_UNIT(.MILLI.,.METRE.) from the file text.
    try:
        for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
            try:
                txt = p.read_text(encoding=enc, errors="ignore")
                break
            except Exception:
                continue
        else:
            txt = ""
        m = re.search(r"SI_UNIT\s*\(\s*\.?([A-Z]*)\.?\s*,\s*\.?(METRE|METER)\.?\s*\)", txt, re.I)
        if m:
            prefix = (m.group(1) or "").upper()
            if prefix == "MILLI":
                return "mm"
            if prefix in ("CENTI",):
                return "cm"
            if prefix == "":
                return "m"
        if re.search(r"CONVERSION_BASED_UNIT[^;]*INCH", txt, re.I):
            return "inch"
        if re.search(r"CONVERSION_BASED_UNIT[^;]*FOOT", txt, re.I):
            return "foot"
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("detect_step_units: text fallback failed: %s", exc)

    return "unknown"


def _explode_to_solids(shape) -> list:
    """Walk *shape* and return every TopoDS_Solid found."""

    out: list = []
    if shape is None:
        return out
    try:
        if hasattr(shape, "IsNull") and shape.IsNull():
            return out
        from OCP.TopAbs import TopAbs_SOLID
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS

        # If the shape itself is already a solid, capture it.
        if shape.ShapeType() == TopAbs_SOLID:
            try:
                out.append(TopoDS.Solid_s(shape))
            except Exception:
                out.append(shape)
            return out

        exp = TopExp_Explorer(shape, TopAbs_SOLID)
        while exp.More():
            try:
                solid = TopoDS.Solid_s(exp.Current())
                out.append(solid)
            except Exception:
                out.append(exp.Current())
            exp.Next()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("_explode_to_solids failed: %s", exc)
    return out


def _scale_shape(shape, factor: float):
    """Return a new shape scaled uniformly by *factor* about the origin."""

    if abs(factor - 1.0) < 1e-12:
        return shape
    try:
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
        from OCP.gp import gp_Trsf

        trsf = gp_Trsf()
        trsf.SetScaleFactor(float(factor))
        xform = BRepBuilderAPI_Transform(shape, trsf, True)
        xform.Build()
        if xform.IsDone():
            return xform.Shape()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("_scale_shape failed: %s", exc)
    return shape


def load_solids(
    path: str | Path,
    *,
    heal: bool = True,
    target_units: str = "mm",
) -> list:
    """Read a STEP file and return its solids in *target_units*.

    Parameters
    ----------
    path:
        Path to the STEP file.
    heal:
        If True, run :func:`shape_health.heal_shape` on each returned solid.
    target_units:
        Unit string to scale into - usually ``"mm"``. If the file's units
        are not detectable, no scaling is applied.

    Returns
    -------
    list of TopoDS_Solid
        Possibly empty if the file cannot be parsed.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    """

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"STEP file not found: {p}")

    target = _canonicalise_unit(target_units)
    target_factor = _UNIT_TO_MM.get(target, 1.0)

    try:
        from OCP.IFSelect import IFSelect_ReturnStatus
        from OCP.STEPControl import STEPControl_Reader

        reader = STEPControl_Reader()
        status = reader.ReadFile(str(p))
        if status != IFSelect_ReturnStatus.IFSelect_RetDone:
            logger.warning("load_solids: ReadFile returned %s for %s", status, p)
            return []

        n_roots = reader.TransferRoots()
        if n_roots <= 0:
            logger.warning("load_solids: TransferRoots transferred 0 roots for %s", p)
            return []

        shape = reader.OneShape()
        if shape is None or shape.IsNull():
            return []
    except Exception as exc:
        logger.warning("load_solids: parse failure on %s: %s", p, exc)
        return []

    # Unit normalisation.
    #
    # OCP's STEPControl_Reader normalises geometry to its working unit (mm by
    # default) regardless of the file's declared unit: inch files with a
    # CONVERSION_BASED_UNIT come back in mm, mm-declared files too. So the
    # default ``target_units='mm'`` path needs NO extra scaling.
    #
    # We only apply an explicit scale when the caller asks for something
    # other than OCP's working unit (uncommon: tests requesting raw inches).
    target_is_mm = abs(target_factor - 1.0) < 1e-12
    if not target_is_mm and target_factor > 0:
        scale = 1.0 / target_factor  # mm -> requested unit
        if abs(scale - 1.0) > 1e-9:
            shape = _scale_shape(shape, scale)

    solids = _explode_to_solids(shape)

    if heal:
        from .shape_health import heal_shape

        healed: list = []
        for s in solids:
            try:
                healed.append(heal_shape(s))
            except Exception:  # pragma: no cover - heal_shape is defensive itself
                healed.append(s)
        solids = healed

    return solids


def write_step(solid, path: str | Path) -> Path:
    """Write *solid* to a STEP file (used by tests).

    Returns the resolved Path. Raises on failure so test code fails loudly.
    """

    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    p = Path(path)
    writer = STEPControl_Writer()
    transfer_status = writer.Transfer(solid, STEPControl_StepModelType.STEPControl_AsIs)
    if transfer_status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise RuntimeError(f"STEP transfer failed: {transfer_status}")
    write_status = writer.Write(str(p))
    if write_status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise RuntimeError(f"STEP write failed: {write_status}")
    return p
