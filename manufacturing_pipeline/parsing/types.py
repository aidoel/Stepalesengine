"""Public types for the parsing layer."""

from __future__ import annotations

from dataclasses import dataclass, field


class StepParseError(Exception):
    """Raised when every parsing strategy fails. Never raised for empty assemblies."""


@dataclass
class StepPart:
    product_id: str
    name: str
    description: str = ""
    children: list[str] = field(default_factory=list)
    source: str = "unknown"
