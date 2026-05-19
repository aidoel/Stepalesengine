"""Dutch-language part-type keyword map.

Kept separate from the standard-label regex so each can be tuned independently.
"""

from __future__ import annotations

import re

DUTCH_PART_TYPES = {
    r"\bplaatdeel\b": "plate_part",
    r"\bprofiel\b": "profile",
    r"\bkoker\b": "hollow_section",
    r"\bbuis\b": "tube",
    r"\bhoeklijn\b": "angle_iron",
    r"\bplaat\b": "plate",
    r"\bstrip\b": "strip",
    r"\bgording\b": "purlin",
    r"\bligger\b": "beam",
    r"\bkolom\b": "column",
}


def detect_part_type(text: str) -> str | None:
    for pattern, label in DUTCH_PART_TYPES.items():
        if re.search(pattern, text, re.IGNORECASE):
            return label
    return None
