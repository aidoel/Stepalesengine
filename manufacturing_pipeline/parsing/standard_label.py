"""Tolerant DIN/EN/ISO standard-label detection and canonicalisation."""

from __future__ import annotations

import re

STANDARD_RE = re.compile(
    r"""
    \b
    (?P<body>
       DIN(?:[\s\-_]*EN(?:[\s\-_]*ISO)?)?
     | EN(?:[\s\-_]*ISO)?
     | ISO
     | NEN(?:[\s\-_]*EN(?:[\s\-_]*ISO)?)?
     | ASTM | ASME | JIS | GOST | BS
    )
    [\s\-_/]*
    (?P<num>\d{2,6})
    (?:\s*-\s*(?P<part>\d{1,4}))?
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def normalize_standard(text: str) -> list[str]:
    """Return canonical labels like 'DIN EN 10210-2'.

    >>> normalize_standard("Profiel DIN1026-2 / EN 10210-2 vs din-en-iso 4014")
    ['DIN 1026-2', 'EN 10210-2', 'DIN EN ISO 4014']
    """
    out: list[str] = []
    for m in STANDARD_RE.finditer(text):
        body_raw = m.group("body").upper()
        body = re.sub(r"[\s\-_]+", " ", body_raw).strip()
        token = f"{body} {m.group('num')}"
        if m.group("part"):
            token += f"-{m.group('part')}"
        out.append(token)
    return out
