"""Sheet-metal unfold probe adapter."""

from __future__ import annotations

from ...geometry.types import UnfoldResult
from ...geometry.unfold_probe import UnfoldProbe
from . import ProbeContext


class UnfoldProbeAdapter:
    """Adapter so :class:`UnfoldProbe` slots into the registry."""

    name = "unfold"

    def __init__(self, probe: UnfoldProbe | None = None) -> None:
        self._probe = probe if probe is not None else UnfoldProbe()

    def run(self, inp: ProbeContext) -> UnfoldResult:
        return self._probe.run(inp.solid)
