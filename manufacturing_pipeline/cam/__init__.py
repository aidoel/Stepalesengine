"""CAM strategy probe.

Pure-Python heuristic that turns analyzer outputs (manufacturing features,
hole pattern, classification, PMI, optional unfold + profile match) into a
per-part :class:`MachiningStrategy`: an ordered list of machining
:class:`Operation` recommendations with rough time estimates.

The probe never raises - bad input yields a strategy with
``primary_process="unknown"`` and a single ``inspect`` operation.
"""

from __future__ import annotations

from .strategist import recommend
from .types import MachiningStrategy, Operation

__all__ = ["MachiningStrategy", "Operation", "recommend"]
