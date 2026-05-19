"""Hole-detection probe: thin adapter around :class:`HoleAnalyzer`."""

from __future__ import annotations

from ...geometry.hole_analyzer import HoleAnalyzer
from ...geometry.types import HolePattern
from . import ProbeContext


class HoleProbe:
    """Adapter so :class:`HoleAnalyzer` slots into the registry."""

    name = "holes"

    def __init__(self, analyzer: HoleAnalyzer | None = None) -> None:
        self._analyzer = analyzer if analyzer is not None else HoleAnalyzer()

    def run(self, inp: ProbeContext) -> HolePattern:
        return self._analyzer.analyze(inp.solid)
