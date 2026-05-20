"""Probe protocol + registry: a single wiring point for per-part probes.

A probe takes a :class:`ProbeContext` (solid + features + paths) and returns
a structured result. Probes never raise; the registry catches exceptions and
records ``None`` for that probe's slot.

Adding a probe means appending it to the default registry (see
:func:`default_registry`) - no orchestrator edits required.

Each probe is registered under a *stage* so the orchestrator can run the
pipeline in three segments around the work it owns itself (the duplicate-solid
cache and the score classifier, neither of which is a probe):

* :data:`STAGE_PRE` - runs before the cache lookup. Its output feeds the cache
  signature, so it must run on every solid (the hole probe).
* :data:`STAGE_CLASSIFY` - feeds the score classifier. Results are cached per
  solid signature and may be skipped by the cheap prefilter (profile, unfold).
* :data:`STAGE_POST` - runs after classification on every part, consuming the
  classification plus prior probe results (pmi, cam).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generic, Protocol, TypeVar, runtime_checkable

from ...geometry.types import ManufacturingFeatures

logger = logging.getLogger(__name__)

STAGE_PRE = "pre"
STAGE_CLASSIFY = "classify"
STAGE_POST = "post"

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
        self._probes: list[tuple[str, Probe, str]] = []

    def register(self, name: str, probe: Probe, stage: str = STAGE_CLASSIFY) -> None:
        """Append a probe under ``name`` in ``stage``. Re-registering the same
        name replaces the previous entry (order and stage are taken from the
        new registration)."""
        for i, (existing, _, _) in enumerate(self._probes):
            if existing == name:
                self._probes[i] = (name, probe, stage)
                return
        self._probes.append((name, probe, stage))

    def names(self) -> list[str]:
        """Return the names in registration order."""
        return [n for n, _, _ in self._probes]

    def run_all(
        self,
        ctx: ProbeContext,
        *,
        stage: str | None = None,
        skip: dict[str, object] | None = None,
        prior: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Run probes in order and return ``{name: result}``.

        ``stage`` restricts the run to probes registered under that stage
        (``None`` runs every probe). ``prior`` seeds the result dict so a
        staged run can see results from an earlier segment. ``skip`` maps a
        probe name to a pre-decided result: a skipped probe is not executed
        and its slot is filled with the supplied value (used by the cheap
        prefilter to short-circuit the expensive profile/unfold probes).

        Failures land as ``{name: None}`` with a logger warning. Each probe
        sees the running result dict via ``ctx.prior`` so later probes can
        consume earlier results.
        """
        out: dict[str, object] = dict(prior or {})
        skip = skip or {}
        for name, probe, pstage in self._probes:
            if stage is not None and pstage != stage:
                continue
            ctx.prior = out
            if name in skip:
                out[name] = skip[name]
                continue
            try:
                out[name] = probe.run(ctx)
            except Exception as exc:
                logger.warning("probe %r failed for %s: %s", name, ctx.part_id, exc)
                out[name] = None
        return out


def default_registry() -> ProbeRegistry:
    """Return the registry wired with the five built-in probes in their
    standard order: holes -> profile -> unfold -> pmi -> cam.

    Stages segment the run around the orchestrator's cache and classifier:
    ``holes`` is PRE (feeds the cache signature), ``profile``/``unfold`` are
    CLASSIFY (feed the classifier, cached, prefilterable), ``pmi``/``cam`` are
    POST (run after classification). The order within POST matters because
    :class:`CamProbe` consumes the prior probe results."""
    from .cam_probe import CamProbe
    from .hole_probe import HoleProbe
    from .pmi_probe import PmiProbe
    from .profile_probe import ProfileProbe
    from .unfold_probe import UnfoldProbeAdapter

    reg = ProbeRegistry()
    reg.register("holes", HoleProbe(), stage=STAGE_PRE)
    reg.register("profile", ProfileProbe(), stage=STAGE_CLASSIFY)
    reg.register("unfold", UnfoldProbeAdapter(), stage=STAGE_CLASSIFY)
    reg.register("pmi", PmiProbe(), stage=STAGE_POST)
    reg.register("cam", CamProbe(), stage=STAGE_POST)
    return reg


__all__ = [
    "Probe",
    "ProbeContext",
    "ProbeRegistry",
    "STAGE_CLASSIFY",
    "STAGE_POST",
    "STAGE_PRE",
    "default_registry",
]
