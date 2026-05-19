"""Probe protocol + registry: a single wiring point for per-part probes.

A probe takes a :class:`ProbeContext` (solid + features + paths) and returns
a structured result. Probes never raise; the registry catches exceptions and
records ``None`` for that probe's slot.

Adding a probe means appending it to the default registry (see
:func:`default_registry`) - no orchestrator edits required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generic, Protocol, TypeVar, runtime_checkable

from ...geometry.types import ManufacturingFeatures

logger = logging.getLogger(__name__)

I_contra = TypeVar("I_contra", contravariant=True)
R_co = TypeVar("R_co", covariant=True)


@runtime_checkable
class Probe(Protocol, Generic[I_contra, R_co]):
    """A probe takes an input shape (e.g., a solid + features) and returns
    a structured result. Probes never raise; failures are encoded in the
    result type."""

    name: str

    def run(self, inp: I_contra) -> R_co: ...


@dataclass
class ProbeContext:
    """Bundle passed to probes inside the orchestrator. Carries everything
    a probe might need without making each one re-extract from raw inputs."""

    solid: object  # TopoDS_Solid
    features: ManufacturingFeatures
    source_path: Path
    part_name: str = ""
    part_id: str = ""
    # Populated by the registry as probes run; later probes can read prior
    # results (e.g. cam_probe consumes holes/unfold/profile_match).
    prior: dict[str, object] = field(default_factory=dict)


class ProbeRegistry:
    """Ordered list of probes the orchestrator runs. Acts as the single
    wiring point - adding a probe = adding it here."""

    def __init__(self) -> None:
        self._probes: list[tuple[str, Probe]] = []

    def register(self, name: str, probe: Probe) -> None:
        """Append a probe under ``name``. Re-registering the same name
        replaces the previous entry (order preserved)."""
        for i, (existing, _) in enumerate(self._probes):
            if existing == name:
                self._probes[i] = (name, probe)
                return
        self._probes.append((name, probe))

    def names(self) -> list[str]:
        """Return the names in registration order."""
        return [n for n, _ in self._probes]

    def run_all(self, ctx: ProbeContext) -> dict[str, object]:
        """Run each probe in order, catching exceptions per probe, return
        ``{name: result}``. Failures land as ``{name: None}`` with a
        logger warning. Each probe sees the running result dict via
        ``ctx.prior`` so later probes can consume earlier results."""
        out: dict[str, object] = {}
        for name, probe in self._probes:
            try:
                ctx.prior = out
                out[name] = probe.run(ctx)
            except Exception as exc:
                logger.warning("probe %r failed for %s: %s", name, ctx.part_id, exc)
                out[name] = None
        return out


def default_registry() -> ProbeRegistry:
    """Return the registry wired with the five built-in probes in their
    standard order: holes -> profile -> unfold -> pmi -> cam.

    The order matters because :class:`CamProbe` consumes the prior probe
    results to assemble its recommendation."""
    from .cam_probe import CamProbe
    from .hole_probe import HoleProbe
    from .pmi_probe import PmiProbe
    from .profile_probe import ProfileProbe
    from .unfold_probe import UnfoldProbeAdapter

    reg = ProbeRegistry()
    reg.register("holes", HoleProbe())
    reg.register("profile", ProfileProbe())
    reg.register("unfold", UnfoldProbeAdapter())
    reg.register("pmi", PmiProbe())
    reg.register("cam", CamProbe())
    return reg


__all__ = [
    "Probe",
    "ProbeContext",
    "ProbeRegistry",
    "default_registry",
]
