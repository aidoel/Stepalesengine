"""Tests for :mod:`manufacturing_pipeline.watch`.

The watcher is driven on a background thread so each test sleeps briefly to
give watchdog a chance to deliver filesystem events. The synthetic STEP
fixture is only used as a convenient "real file with the right extension";
the analyze function under test is a counting lambda, not the real pipeline.

Every test stops its watcher via a :class:`threading.Event` and joins the
worker thread, so the suite never leaks observer threads between tests.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from manufacturing_pipeline.watch import WatchOptions, watch_directory

pytest.importorskip("OCP")
from tests.fixtures.synthetic_steps import write_flat_plate_step  # noqa: E402


class _FakeManifest:
    parts: list = []


class _FakeResult:
    """Stand-in for :class:`AnalyzeResult` - only ``manifest.parts`` is read."""

    manifest = _FakeManifest()


def _spawn_watcher(
    root: Path,
    analyze_fn,
    options: WatchOptions | None = None,
) -> tuple[threading.Thread, threading.Event]:
    """Start :func:`watch_directory` on a thread and return it + a stop Event."""
    stop = threading.Event()
    thread = threading.Thread(
        target=watch_directory,
        args=(root, analyze_fn, options),
        kwargs={"stop_event": stop},
        daemon=True,
    )
    thread.start()
    # Brief settle so the Observer is scheduled before tests poke the FS.
    time.sleep(0.3)
    return thread, stop


def _wait_until(predicate, timeout: float = 4.0, step: float = 0.05) -> bool:
    """Poll ``predicate`` until truthy or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def _stop_observer(thread: threading.Thread, stop: threading.Event) -> None:
    """Tear-down: signal the watcher and join the worker thread."""
    stop.set()
    thread.join(timeout=3.0)


def test_watch_fires_on_new_step_file(tmp_path: Path) -> None:
    """Writing a STEP file inside a watched dir should invoke analyze_fn."""
    seen: list[Path] = []

    def fake_analyze(path: Path) -> Any:
        seen.append(path)
        return _FakeResult()

    thread, stop = _spawn_watcher(tmp_path, fake_analyze)
    try:
        write_flat_plate_step(tmp_path / "plate.step", l=40.0, w=20.0, t=2.0)
        assert _wait_until(lambda: len(seen) >= 1, timeout=3.0), "analyze_fn never fired"
        assert seen[0].suffix.lower() == ".step"
    finally:
        _stop_observer(thread, stop)


def test_watch_debounces_rapid_writes(tmp_path: Path) -> None:
    """Two writes inside the debounce window should only trigger one analyze."""
    seen: list[Path] = []

    def fake_analyze(path: Path) -> Any:
        seen.append(path)
        return _FakeResult()

    step_path = write_flat_plate_step(tmp_path / "plate.step", l=40.0, w=20.0, t=2.0)

    options = WatchOptions(debounce_s=2.0)
    thread, stop = _spawn_watcher(tmp_path, fake_analyze, options)
    try:
        # Two quick modifications back-to-back: ``touch`` updates mtime.
        step_path.touch()
        time.sleep(0.1)
        step_path.touch()
        # Wait long enough for both events to be delivered but well inside
        # the debounce window.
        time.sleep(1.0)
        assert len(seen) == 1, f"expected 1 debounced call, got {len(seen)}"
    finally:
        _stop_observer(thread, stop)


def test_watch_ignores_non_matching_extension(tmp_path: Path) -> None:
    """Files outside ``WatchOptions.extensions`` are not dispatched."""
    seen: list[Path] = []

    def fake_analyze(path: Path) -> Any:
        seen.append(path)
        return _FakeResult()

    thread, stop = _spawn_watcher(tmp_path, fake_analyze)
    try:
        (tmp_path / "readme.txt").write_text("nothing to see")
        (tmp_path / "data.json").write_text("{}")
        time.sleep(1.0)
        assert seen == []
    finally:
        _stop_observer(thread, stop)


def test_watch_initial_scan_processes_existing_files(tmp_path: Path) -> None:
    """``initial_scan=True`` runs analyze_fn for every pre-existing STEP."""
    write_flat_plate_step(tmp_path / "a.step", l=30.0, w=20.0, t=2.0)
    write_flat_plate_step(tmp_path / "b.step", l=40.0, w=20.0, t=2.0)
    (tmp_path / "skip.txt").write_text("ignore me")

    seen: list[Path] = []

    def fake_analyze(path: Path) -> Any:
        seen.append(path)
        return _FakeResult()

    options = WatchOptions(initial_scan=True)
    thread, stop = _spawn_watcher(tmp_path, fake_analyze, options)
    try:
        # Both pre-existing files should fire synchronously before the
        # observer thread fully takes over; the wait is just defensive.
        assert _wait_until(lambda: len(seen) >= 2, timeout=3.0)
        names = {p.name for p in seen}
        assert names == {"a.step", "b.step"}
    finally:
        _stop_observer(thread, stop)


def test_watch_keyboard_interrupt_returns_cleanly(tmp_path: Path) -> None:
    """A SIGINT-equivalent KeyboardInterrupt must not propagate."""
    # Drive watch_directory inline by patching Observer.join to raise on the
    # first poll - that's exactly what Ctrl+C does to a blocking join().
    from manufacturing_pipeline import watch as watch_mod

    real_observer_cls = watch_mod.Observer

    class _InterruptingObserver(real_observer_cls):  # type: ignore[misc, valid-type]
        _called = False

        def join(self, timeout: float | None = None) -> None:  # type: ignore[override]
            if not _InterruptingObserver._called:
                _InterruptingObserver._called = True
                raise KeyboardInterrupt
            return super().join(timeout)

    watch_mod.Observer = _InterruptingObserver  # type: ignore[assignment]
    try:
        # Should return without raising.
        watch_directory(tmp_path, lambda p: None, WatchOptions(debounce_s=0.1))
    finally:
        watch_mod.Observer = real_observer_cls  # type: ignore[assignment]


def test_watch_on_done_callback_receives_path_and_result(tmp_path: Path) -> None:
    """The ``on_done`` callback fires with the file path and analyze result."""
    received: list[tuple[Path, Any]] = []

    def fake_analyze(path: Path) -> Any:
        return _FakeResult()

    def _on_done(path: Path, result: Any) -> None:
        received.append((path, result))

    options = WatchOptions(on_done=_on_done, debounce_s=0.2)
    thread, stop = _spawn_watcher(tmp_path, fake_analyze, options)
    try:
        write_flat_plate_step(tmp_path / "plate.step", l=40.0, w=20.0, t=2.0)
        assert _wait_until(lambda: len(received) >= 1, timeout=3.0)
        path, result = received[0]
        assert path.suffix.lower() == ".step"
        assert isinstance(result, _FakeResult)
    finally:
        _stop_observer(thread, stop)


def test_watch_analyze_exception_does_not_kill_watcher(tmp_path: Path) -> None:
    """A raising analyze_fn must not stop subsequent dispatches."""
    calls = {"n": 0}

    def raising_analyze(path: Path) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return _FakeResult()

    options = WatchOptions(debounce_s=0.1)
    thread, stop = _spawn_watcher(tmp_path, raising_analyze, options)
    try:
        write_flat_plate_step(tmp_path / "first.step", l=40.0, w=20.0, t=2.0)
        time.sleep(0.7)
        write_flat_plate_step(tmp_path / "second.step", l=40.0, w=20.0, t=2.0)
        assert _wait_until(lambda: calls["n"] >= 2, timeout=3.0)
    finally:
        _stop_observer(thread, stop)


def test_watch_missing_root_raises(tmp_path: Path) -> None:
    """An invalid root directory raises FileNotFoundError immediately."""
    missing = tmp_path / "nope"
    with pytest.raises(FileNotFoundError):
        watch_directory(missing, lambda p: None, WatchOptions())
