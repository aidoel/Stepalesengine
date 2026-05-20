"""STEPCAFControl_Reader / OCAF fallback.

Used when all six text strategies return empty. Reads XCAF labels and
recovers ``TDataStd_Name`` attributes that pure-text scans miss.

This module is the only one in the parsing layer allowed to touch OCP
and it does so lazily inside the function so that ``import manufacturing_pipeline.parsing``
remains free of any native dependency.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .types import StepPart

logger = logging.getLogger(__name__)


def _label_entry(label: Any) -> str:
    """Best-effort TDF_Label entry string. Returns '' on failure."""
    try:
        from OCP.TCollection import TCollection_AsciiString
        from OCP.TDF import TDF_Tool

        entry = TCollection_AsciiString()
        TDF_Tool.Entry_s(label, entry)
        return entry.ToCString()
    except Exception:
        return ""


def _label_name(label: Any) -> str:
    """Read TDataStd_Name attribute as a Python string. Returns '' on miss."""
    try:
        from OCP.TDataStd import TDataStd_Name
    except Exception:
        return ""

    try:
        attr_id = TDataStd_Name.GetID_s()
    except Exception:
        try:
            attr_id = TDataStd_Name.GetID()
        except Exception:
            return ""

    try:
        attr = TDataStd_Name()
        found = label.FindAttribute(attr_id, attr)
        if not found:
            return ""
        raw = attr.Get()
    except Exception:
        return ""

    # raw is a TCollection_ExtendedString; convert via PrintToString / utf8.
    try:
        return raw.ToExtString()
    except Exception:
        pass
    try:
        return str(raw)
    except Exception:
        return ""


def occt_fallback(path: str | Path) -> list[StepPart]:
    """Use OCP STEPCAFControl_Reader + XCAFDoc_DocumentTool to recover names.

    Returns an empty list if OCP is unavailable, the reader fails, or no
    labels are recovered. Never raises -- every exception path is caught
    and logged as a warning.
    """
    try:
        from OCP.STEPCAFControl import STEPCAFControl_Reader
        from OCP.TCollection import TCollection_ExtendedString
        from OCP.TDF import TDF_LabelSequence
        from OCP.TDocStd import TDocStd_Document
        from OCP.XCAFApp import XCAFApp_Application
        from OCP.XCAFDoc import XCAFDoc_DocumentTool
    except Exception as exc:
        logger.debug("occt_fallback: OCP unavailable (%s)", exc)
        return []

    path_str = os.fspath(path)

    try:
        app = XCAFApp_Application.GetApplication_s()
        doc = TDocStd_Document(TCollection_ExtendedString("STEP"))
        app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc)

        reader = STEPCAFControl_Reader()
        reader.SetColorMode(True)
        reader.SetNameMode(True)
        reader.SetLayerMode(True)

        if not reader.ReadFile(path_str):
            logger.debug("occt_fallback: ReadFile failed for %s", path_str)
            return []
        if not reader.Transfer(doc):
            logger.debug("occt_fallback: Transfer returned false for %s", path_str)
            return []

        shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
        labels = TDF_LabelSequence()
        shape_tool.GetFreeShapes(labels)

        out: list[StepPart] = []
        seen: set = set()
        for i in range(1, labels.Length() + 1):
            label = labels.Value(i)
            name = _label_name(label).strip()
            entry = _label_entry(label).strip()
            if not name:
                continue
            key = (entry, name)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                StepPart(
                    product_id=entry or name,
                    name=name,
                    description="",
                    children=[],
                    source="occt_xcaf",
                )
            )
        return out
    except Exception as exc:
        logger.warning("occt_fallback: unexpected error: %s", exc)
        return []


__all__ = ["occt_fallback"]
