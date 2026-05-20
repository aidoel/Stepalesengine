"""Tests for the Probe protocol + registry and the five built-in adapters.

The geometry-dependent adapters (HoleProbe, ProfileProbe, UnfoldProbeAdapter,
PmiProbe, CamProbe) are exercised against synthetic in-memory STEP fixtures
where possible, with OCP skipped if absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from manufacturing_pipeline.geometry.types import (
    HolePattern,
    ManufacturingFeatures,
    UnfoldResult,
    UnfoldStatus,
)
from manufacturing_pipeline.pipeline.probes import (
    Probe,
    ProbeContext,
    ProbeRegistry,
    default_registry,
)

# ---------------------------------------------------------------------------
# Fakes for protocol-level tests
# ---------------------------------------------------------------------------


class _StubProbe:
    """Minimal probe used to exercise registry behaviour without geometry."""

    def __init__(self, name: str, payload):
        self.name = name
        self._payload = payload
        self.calls = 0

    def run(self, inp: ProbeContext):
        self.calls += 1
        return self._payload


class _BoomProbe:
    name = "boom"

    def run(self, inp: ProbeContext):
        raise RuntimeError("kaboom")


def _ctx() -> ProbeContext:
    return ProbeContext(
        solid=object(),
        features=ManufacturingFeatures(),
        source_path=Path(""),
        part_id="pid",
        part_name="pname",
    )


# ---------------------------------------------------------------------------
# Registry behaviour
# ---------------------------------------------------------------------------


def test_empty_registry_returns_empty_dict():
    reg = ProbeRegistry()
    assert reg.run_all(_ctx()) == {}


def test_registry_runs_probes_in_registration_order_and_isolates_failures():
    reg = ProbeRegistry()
    a = _StubProbe("a", 1)
    b = _BoomProbe()
    c = _StubProbe("c", 3)
    reg.register("a", a)
    reg.register("b", b)
    reg.register("c", c)
    out = reg.run_all(_ctx())
    assert list(out.keys()) == ["a", "b", "c"]
    assert out["a"] == 1
    assert out["b"] is None
    assert out["c"] == 3
    # Failure of "b" must not stop later probes.
    assert a.calls == 1
    assert c.calls == 1


def test_registering_twice_under_the_same_name_replaces_the_probe():
    reg = ProbeRegistry()
    first = _StubProbe("k", "first")
    second = _StubProbe("k", "second")
    reg.register("k", first)
    reg.register("k", second)
    assert reg.names() == ["k"]
    out = reg.run_all(_ctx())
    assert out == {"k": "second"}
    assert first.calls == 0
    assert second.calls == 1


def test_probes_can_read_prior_results_from_the_running_dict():
    reg = ProbeRegistry()

    class _PriorReader:
        name = "reader"

        def run(self, inp: ProbeContext):
            return list((inp.prior or {}).keys())

    reg.register("a", _StubProbe("a", "A"))
    reg.register("b", _StubProbe("b", "B"))
    reg.register("reader", _PriorReader())
    out = reg.run_all(_ctx())
    assert out["reader"] == ["a", "b"]


def test_default_registry_lists_five_probes_in_canonical_order():
    reg = default_registry()
    assert reg.names() == ["holes", "profile", "unfold", "pmi", "cam"]


def test_probe_protocol_runtime_checkable_accepts_minimal_adapter():
    class _Mini:
        name = "mini"

        def run(self, inp):
            return 42

    assert isinstance(_Mini(), Probe)


# ---------------------------------------------------------------------------
# Geometry-backed wrappers vs underlying modules
# ---------------------------------------------------------------------------


def _make_plate_solid():
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return BRepPrimAPI_MakeBox(40.0, 30.0, 2.0).Solid()


def test_hole_probe_returns_same_result_as_calling_hole_analyzer_directly():
    from manufacturing_pipeline.geometry.hole_analyzer import HoleAnalyzer
    from manufacturing_pipeline.pipeline.probes.hole_probe import HoleProbe

    solid = _make_plate_solid()
    direct = HoleAnalyzer().analyze(solid)
    ctx = ProbeContext(
        solid=solid,
        features=ManufacturingFeatures(),
        source_path=Path(""),
    )
    via = HoleProbe().run(ctx)
    assert isinstance(direct, HolePattern)
    assert isinstance(via, HolePattern)
    assert via.hole_count == direct.hole_count
    assert via.diameters == direct.diameters


def test_unfold_probe_adapter_matches_underlying_module():
    from manufacturing_pipeline.geometry.unfold_probe import UnfoldProbe
    from manufacturing_pipeline.pipeline.probes.unfold_probe import (
        UnfoldProbeAdapter,
    )

    solid = _make_plate_solid()
    direct = UnfoldProbe().run(solid)
    ctx = ProbeContext(
        solid=solid,
        features=ManufacturingFeatures(),
        source_path=Path(""),
    )
    via = UnfoldProbeAdapter().run(ctx)
    assert isinstance(direct, UnfoldResult)
    assert isinstance(via, UnfoldResult)
    assert via.status == direct.status
    assert via.n_bends == direct.n_bends


def test_pmi_probe_returns_an_empty_record_for_missing_file(tmp_path: Path):
    from manufacturing_pipeline.pipeline.probes.pmi_probe import PmiProbe
    from manufacturing_pipeline.pmi.types import PMIRecord

    ctx = ProbeContext(
        solid=object(),
        features=ManufacturingFeatures(),
        source_path=tmp_path / "missing.step",
    )
    out = PmiProbe().run(ctx)
    assert isinstance(out, PMIRecord)
    # Empty record: no callouts on a non-existent file.
    assert out.n_annotations == 0


def test_cam_probe_falls_back_to_unknown_without_classification():
    from manufacturing_pipeline.cam.types import MachiningStrategy
    from manufacturing_pipeline.pipeline.probes.cam_probe import CamProbe

    ctx = ProbeContext(
        solid=object(),
        features=ManufacturingFeatures(),
        source_path=Path(""),
    )
    ctx.prior = {"holes": HolePattern(), "unfold": None, "profile": None}
    strategy = CamProbe().run(ctx)
    assert isinstance(strategy, MachiningStrategy)
    assert strategy.primary_process == "unknown"


def test_cam_probe_uses_classification_from_prior_when_present():
    from manufacturing_pipeline.cam.types import MachiningStrategy
    from manufacturing_pipeline.classification.types import (
        ClassificationResult,
        DecisionTrace,
    )
    from manufacturing_pipeline.pipeline.probes.cam_probe import CamProbe

    ctx = ProbeContext(
        solid=object(),
        features=ManufacturingFeatures(),
        source_path=Path(""),
    )
    classification = ClassificationResult(
        label="plaat",
        confidence=0.9,
        trace=DecisionTrace(),
    )
    unfold = UnfoldResult(status=UnfoldStatus.SUCCESS, n_bends=1)
    ctx.prior = {
        "holes": HolePattern(),
        "unfold": unfold,
        "profile": None,
        "pmi": None,
        "classification": classification,
    }
    strategy = CamProbe().run(ctx)
    assert isinstance(strategy, MachiningStrategy)
    assert strategy.primary_process == "sheet_metal"
