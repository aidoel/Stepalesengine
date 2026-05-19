"""Public types for the CAM strategy probe."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Operation:
    """A single machining operation recommendation.

    Attributes
    ----------
    op:
        Operation kind. One of ``"drill"``, ``"ream"``, ``"tap"``,
        ``"mill_pocket"``, ``"mill_contour"``, ``"deburr"``, ``"punch"``,
        ``"laser_cut"``, ``"press_brake_bend"``, ``"finish_pass"``,
        ``"inspect"``.
    feature_ref:
        Free-form reference such as ``"hole_3"``, ``"bend_0"``,
        ``"outer_contour"``, or ``"part"``.
    params:
        Arbitrary numeric / string parameters (diameter, depth, length,
        radius, datum list, ...). Serialised verbatim into XML.
    priority:
        Lower = earlier. Roughing operations sit around ``100``; finishing
        operations sit around ``200`` so they group at the end of a setup.
    reason:
        Human-readable note explaining why the operation was emitted.
    """

    op: str
    feature_ref: str = ""
    params: dict = field(default_factory=dict)
    priority: int = 100
    reason: str = ""


@dataclass
class MachiningStrategy:
    """Per-part machining plan produced by the CAM probe.

    ``primary_process`` is one of ``"sheet_metal"``, ``"machining"``,
    ``"purchased"``, ``"unknown"``. ``estimated_time_min`` is a rough
    wall-clock estimate aggregated from per-operation heuristics - it is
    not a quote. ``setup_count`` is the operator-visible number of fixturings
    the plan implies (1 for sheet metal, 2 for machining with multiple
    drilling axes, etc).
    """

    primary_process: str
    operations: list[Operation] = field(default_factory=list)
    setup_count: int = 1
    material_hint: str = ""
    estimated_time_min: float = 0.0
    notes: str = ""
