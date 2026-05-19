"""Tests for :mod:`manufacturing_pipeline.telemetry`.

The module is OFF by default; tests always restore that state in a finally
block so subsequent tests start from a clean handler list.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from manufacturing_pipeline import telemetry


@pytest.fixture(autouse=True)
def _reset_telemetry():
    """Strip handlers before and after every test."""
    telemetry.configure(sink=None)
    yield
    telemetry.configure(sink=None)


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Unit-level tests
# ---------------------------------------------------------------------------


def test_emit_adds_timestamp_and_pid(tmp_path: Path):
    sink = tmp_path / "t.jsonl"
    telemetry.configure(sink=sink)
    t_before = time.time()
    telemetry.emit("hello", who="world")
    rows = _read_jsonl(sink)
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "hello"
    assert row["who"] == "world"
    assert isinstance(row["pid"], int) and row["pid"] == os.getpid()
    assert isinstance(row["ts"], (int, float))
    # ts should be roughly now (allow generous wall-clock slack)
    assert t_before - 1.0 <= row["ts"] <= time.time() + 1.0


def test_timed_context_emits_one_event_with_duration_ms(tmp_path: Path):
    sink = tmp_path / "t.jsonl"
    telemetry.configure(sink=sink)
    with telemetry.timed("phase_a", phase="a"):
        time.sleep(0.001)
    rows = _read_jsonl(sink)
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "phase_a"
    assert "duration_ms" in row
    assert isinstance(row["duration_ms"], (int, float))
    assert row["duration_ms"] >= 0.0
    assert row["phase"] == "a"
    assert row["ok"] is True


def test_configure_sink_file_each_line_is_valid_json(tmp_path: Path):
    sink = tmp_path / "foo.jsonl"
    telemetry.configure(sink=sink)
    for i in range(5):
        telemetry.emit("step", i=i)
    lines = sink.read_text().splitlines()
    assert len(lines) == 5
    for line in lines:
        obj = json.loads(line)
        assert obj["event"] == "step"
        assert "i" in obj


def test_configure_sink_none_suppresses_output():
    # No handler -> emit returns silently and writes nothing observable.
    telemetry.configure(sink=None)
    # Should not raise even with rich payloads.
    telemetry.emit("ignored", a=1, b="two", c=[1, 2, 3])
    assert telemetry._telemetry_log.handlers == []


def test_timed_emits_ok_false_on_exception(tmp_path: Path):
    sink = tmp_path / "err.jsonl"
    telemetry.configure(sink=sink)
    with pytest.raises(RuntimeError):
        with telemetry.timed("op"):
            raise RuntimeError("boom")
    rows = _read_jsonl(sink)
    assert len(rows) == 1
    assert rows[0]["event"] == "op"
    assert rows[0]["ok"] is False
    assert "duration_ms" in rows[0]


def test_caller_cannot_overwrite_builtin_fields(tmp_path: Path):
    sink = tmp_path / "t.jsonl"
    telemetry.configure(sink=sink)
    telemetry.emit("e", ts="not-a-time", pid="not-a-pid", x=1)
    rows = _read_jsonl(sink)
    assert rows[0]["event"] == "e"
    assert rows[0]["pid"] == os.getpid()
    assert isinstance(rows[0]["ts"], (int, float))
    assert rows[0]["x"] == 1


def test_configure_unknown_format_raises():
    with pytest.raises(ValueError):
        telemetry.configure(sink="-", format="xml")


# ---------------------------------------------------------------------------
# End-to-end via analyze
# ---------------------------------------------------------------------------


def _make_box_step(tmp_path: Path) -> Path:
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    from manufacturing_pipeline.geometry.geometry_loader import write_step

    solid = BRepPrimAPI_MakeBox(40.0, 30.0, 2.0).Solid()
    out = tmp_path / "box.step"
    write_step(solid, out)
    return out


def test_analyze_emits_start_and_end_to_telemetry_file(tmp_path: Path):
    from manufacturing_pipeline.pipeline.analyze_assembly import (
        AnalyzeOptions,
        analyze,
    )

    step_path = _make_box_step(tmp_path)
    sink = tmp_path / "telemetry.jsonl"
    telemetry.configure(sink=sink)

    analyze(
        step_path,
        AnalyzeOptions(out_dir=None, write_dxf=False, write_xml=False, use_cache=False),
    )

    rows = _read_jsonl(sink)
    events = [r["event"] for r in rows]
    assert "analyze_start" in events
    assert "analyze_end" in events
    # parse_strategy_used should fire during parse_step
    assert "parse_strategy_used" in events
    # at least one analyze_part event
    assert any(r["event"] == "analyze_part" for r in rows)
