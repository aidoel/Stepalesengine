"""Structured telemetry for the pipeline.

Single emit point for JSON-lines events so external dashboards can ingest the
pipeline's runtime behaviour. Telemetry is **off by default**: callers must
invoke :func:`configure` to direct output anywhere. Modules that import this
file pay no cost when no sink is configured (the logger has no handlers).

Typical usage::

    from manufacturing_pipeline import telemetry
    telemetry.configure(sink="run.jsonl")
    telemetry.emit("analyze_start", path="foo.step")
    with telemetry.timed("parse"):
        ...

Each event is one JSON object on its own line with at least the keys
``ts`` (unix seconds, float), ``pid`` (int), and ``event`` (str). Caller
fields are merged in on top.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

_LOGGER_NAME = "stepalesengine.telemetry"
_telemetry_log = logging.getLogger(_LOGGER_NAME)
_telemetry_log.propagate = False
# Default level: emit everything the configured handler accepts.
_telemetry_log.setLevel(logging.INFO)


class _JsonlFormatter(logging.Formatter):
    """Pass-through formatter — the message is already a JSON line."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        return record.getMessage()


class _ConsoleFormatter(logging.Formatter):
    """Human-readable formatter that flattens fields into ``key=value`` pairs."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        msg = record.getMessage()
        try:
            data = json.loads(msg)
        except (TypeError, ValueError):
            return msg
        event = data.pop("event", "event")
        bits = " ".join(f"{k}={v}" for k, v in data.items())
        return f"{event} {bits}".strip()


def _clear_handlers() -> None:
    for h in list(_telemetry_log.handlers):
        _telemetry_log.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass


def configure(*, sink: str | Path | None = None, format: str = "jsonl") -> None:
    """Configure where telemetry goes.

    Parameters
    ----------
    sink:
        A file path (``str`` or :class:`Path`) to append JSON-lines to,
        ``"-"`` for stdout, or ``None`` to suppress all output. Calling
        ``configure(sink=None)`` removes any previously installed handler.
    format:
        ``"jsonl"`` (default) writes one JSON object per line.
        ``"console"`` writes a human-readable ``event key=value`` form,
        useful for ad-hoc CLI runs.
    """
    _clear_handlers()
    if sink is None:
        return

    if format == "jsonl":
        formatter: logging.Formatter = _JsonlFormatter()
    elif format == "console":
        formatter = _ConsoleFormatter()
    else:
        raise ValueError(f"unknown telemetry format: {format!r}")

    if sink == "-" or sink is sys.stdout:
        handler: logging.Handler = logging.StreamHandler(sys.stdout)
    else:
        path = Path(sink)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(str(path), mode="a", encoding="utf-8")

    handler.setFormatter(formatter)
    _telemetry_log.addHandler(handler)


def _coerce(value):
    """Convert a value to something JSON-serialisable."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    if isinstance(value, Path):
        return str(value)
    return repr(value)


def emit(event: str, **fields) -> None:
    """Emit a structured event as one JSON line on the telemetry logger.

    Always adds ``ts`` (float unix seconds), ``pid``, and ``event``. Caller
    fields override nothing built-in: if a caller passes ``ts``/``pid``/``event``
    those are dropped silently so the schema stays predictable.
    """
    if not _telemetry_log.handlers:
        return  # no sink configured -> zero-cost path
    payload = {
        "ts": time.time(),
        "pid": os.getpid(),
        "event": str(event),
    }
    for k, v in fields.items():
        if k in ("ts", "pid", "event"):
            continue
        payload[k] = _coerce(v)
    try:
        line = json.dumps(payload, sort_keys=False, default=str)
    except (TypeError, ValueError):
        line = json.dumps(
            {
                "ts": payload["ts"],
                "pid": payload["pid"],
                "event": payload["event"],
                "_unserialisable": True,
            }
        )
    _telemetry_log.info(line)


@contextmanager
def timed(event: str, **fields):
    """Context manager that emits ``{event, duration_ms, ...fields}`` on exit.

    Emits the event even on exceptions, with ``ok=False`` when the body
    raised. Otherwise ``ok=True``. The exception is re-raised unchanged.
    """
    t0 = time.perf_counter()
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        emit(event, duration_ms=round(dt_ms, 3), ok=ok, **fields)


__all__ = ["emit", "timed", "configure"]
