"""Cross-section profile match probe.

Slices the solid, checks the section is constant along the principal axis,
then matches the middle slice against the standard-profile tables.
"""

from __future__ import annotations

from ...geometry.cross_section import is_constant, slice_solid
from ...geometry.profile_matcher import ProfileShapeMatcher
from ...geometry.types import ProfileMatch
from . import ProbeContext


class ProfileProbe:
    """Adapter that slices and matches the middle cross-section.

    Returns ``None`` when the part has no constant cross-section."""

    name = "profile"

    def __init__(self, matcher: ProfileShapeMatcher | None = None) -> None:
        self._matcher = matcher if matcher is not None else ProfileShapeMatcher()

    def run(self, inp: ProbeContext) -> ProfileMatch | None:
        sections = slice_solid(inp.solid)
        if not sections or not is_constant(sections):
            return None
        mid = sections[len(sections) // 2]
        return self._matcher.match(mid)
