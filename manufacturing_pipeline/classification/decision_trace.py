"""DecisionTrace JSON serialisation."""

from __future__ import annotations

import json
from dataclasses import asdict

from .types import DecisionTrace


from typing import Any


def trace_to_json(trace: DecisionTrace, **kwargs: Any) -> str:
    return json.dumps(asdict(trace), **kwargs)
