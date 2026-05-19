"""PMI extraction probe.

Wraps :func:`extract_pmi`. Needs ``ctx.source_path`` because PMI lives in the
raw STEP text, not the loaded geometry.
"""

from __future__ import annotations

from ...pmi.extractor import extract_pmi
from ...pmi.types import PMIRecord
from . import ProbeContext


class PmiProbe:
    """Adapter so :func:`extract_pmi` slots into the registry."""

    name = "pmi"

    def run(self, inp: ProbeContext) -> PMIRecord:
        return extract_pmi(inp.source_path)
