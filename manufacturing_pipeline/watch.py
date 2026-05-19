"""Long-running watch mode that re-runs the analyze pipeline on STEP changes.

Wraps :mod:`watchdog` with debouncing, extension filtering and an optional
initial scan. The :func:`watch_directory` entry point is blocking and is
intended to be driven either from the ``stepalesengine watch`` CLI subcommand
or from library code that needs a synchronous file watcher.

Design notes
------------
* The :class:`Observer` runs file-system events on its own thread. We do the
  per-file work on that thread too, but isolate every callback in a
  ``try/except`` so a single failure never poisons the watcher.
* Debouncing is per-path: editors that emit ``modified`` events in rapid
  succession (or atomic-rename writes that fire ``created`` + ``modified``)
  collapse into a single :func:`analyze_fn` invocation.
* Cancellation is cooperative. The main thread blocks on
  :meth:`Observer.join` via a short polling loop so ``KeyboardInterrupt`` is
  delivered promptly; on interrupt we stop the observer and return.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import TYPE_CHECKING, Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .pipeline.analyze_assembly import AnalyzeResult

logger = logging.getLogger(__name__)


@dataclass
class WatchOptions:
    """Knobs for :func:`watch_directory`.

    Attributes
    ----------
    extensions:
        Suffixes (lowercase, leading dot) that should trigger
        :func:`analyze_fn`. Anything else is ignored.
    debounce_s:
        Minimum interval between two analyses of the same path. Editors and
        OS atomic-saves often emit several events per write; only the first
        event in the window triggers a run.
    initial_scan:
        When True, every existing matching file under ``root`` is processed
        once at startup before the watcher begins listening for events.
    recursive:
        Forwarded to :meth:`Observer.schedule`. When False only events in
        ``root`` itself fire.
    on_done:
        Optional callback invoked after every successful analyze. Receives
        ``(path, AnalyzeResult)``. Exceptions inside the callback are caught
        and logged so the watcher stays up.
    """

    extensions: tuple[str, ...] = (".stp", ".step")
    debounce_s: float = 1.0
    initial_scan: bool = False
    recursive: bool = True
    on_done: Callable[[Path, "AnalyzeResult"], None] | None = None


class _StepFileHandler(FileSystemEventHandler):
    """Translate filesystem events into debounced ``analyze_fn`` calls."""

    def __init__(
        self,
        analyze_fn: Callable[[Path], "AnalyzeResult"],
        options: WatchOptions,
    ) -> None:
        super().__init__()
        self._analyze_fn = analyze_fn
        self._options = options
        # ``_last_run`` is read+written from the observer thread (which is the
        # only thread that runs the dispatcher) but we still guard it so an
        # ``initial_scan`` on the main thread races safely with the observer.
        self._last_run: dict[Path, float] = {}
        self._lock = Lock()
        self.processed = 0

    # -- watchdog hooks -----------------------------------------------------

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_run(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._maybe_run(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        # A rename/move usually means the destination is the file the user
        # cares about; treat it like a fresh write.
        dest = getattr(event, "dest_path", None)
        if dest:
            self._dispatch(Path(str(dest)))

    # -- internals ----------------------------------------------------------

    def _maybe_run(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path_str = getattr(event, "src_path", None)
        if not path_str:
            return
        self._dispatch(Path(str(path_str)))

    def _dispatch(self, path: Path) -> None:
        """Filter, debounce, then run ``analyze_fn`` on ``path``."""
        if path.suffix.lower() not in self._options.extensions:
            return
        if not path.exists():
            # Modify events for deleted/temp files are common from editors.
            return
        now = time.monotonic()
        with self._lock:
            last = self._last_run.get(path)
            if last is not None and (now - last) < self._options.debounce_s:
                logger.debug("debounced %s (%.3fs since last run)", path, now - last)
                return
            self._last_run[path] = now
        self.run_once(path)

    def run_once(self, path: Path) -> None:
        """Synchronously run analyze + on_done; never raise.

        Public-ish so :func:`watch_directory` can reuse it for the
        ``initial_scan`` pass.
        """
        try:
            result = self._analyze_fn(path)
        except Exception as exc:  # noqa: BLE001 - watcher must survive
            logger.warning("analyze failed for %s: %s", path, exc)
            return
        self.processed += 1
        if self._options.on_done is not None:
            try:
                self._options.on_done(path, result)
            except Exception as exc:  # noqa: BLE001 - callback isolated
                logger.warning("on_done callback failed for %s: %s", path, exc)


def _existing_matches(root: Path, exts: tuple[str, ...], recursive: bool) -> list[Path]:
    """Return matching files under ``root`` (sorted, deterministic)."""
    glob = root.rglob if recursive else root.glob
    matches = [p for p in glob("*") if p.is_file() and p.suffix.lower() in exts]
    return sorted(matches)


def watch_directory(
    root: Path,
    analyze_fn: Callable[[Path], "AnalyzeResult"],
    options: WatchOptions | None = None,
    *,
    stop_event: Event | None = None,
) -> None:
    """Block, re-running ``analyze_fn`` on every changed STEP file.

    Parameters
    ----------
    root:
        Directory to watch. Must exist.
    analyze_fn:
        Callable invoked with each changed file path. Should be idempotent;
        exceptions are caught and logged.
    options:
        Optional :class:`WatchOptions`. Defaults: ``.stp``/``.step``, 1.0s
        debounce, recursive, no initial scan, no completion callback.
    stop_event:
        Optional :class:`threading.Event` used as a cooperative cancel from
        another thread. Set the event to make :func:`watch_directory` return
        cleanly. Distinct from ``KeyboardInterrupt`` for callers that drive
        the watcher from non-main threads (notably tests).

    Notes
    -----
    Returns normally on ``KeyboardInterrupt`` or when ``stop_event`` is set.
    The watchdog ``Observer`` is stopped and joined cleanly before returning,
    so no daemon threads remain.
    """
    opts = options if options is not None else WatchOptions()
    root = Path(root)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"watch root not found or not a directory: {root}")

    handler = _StepFileHandler(analyze_fn, opts)

    if opts.initial_scan:
        for existing in _existing_matches(root, opts.extensions, opts.recursive):
            handler.run_once(existing)

    observer: Any = Observer()
    observer.schedule(handler, str(root), recursive=opts.recursive)
    observer.start()
    logger.info(
        "watching %s (recursive=%s, exts=%s, debounce=%.1fs)",
        root,
        opts.recursive,
        ",".join(opts.extensions),
        opts.debounce_s,
    )

    try:
        # Short-polling join so KeyboardInterrupt is delivered while we wait
        # and any external stop_event is honoured promptly.
        while observer.is_alive():
            if stop_event is not None and stop_event.is_set():
                break
            observer.join(0.2)
    except KeyboardInterrupt:
        logger.info("watch interrupted by user")
    finally:
        observer.stop()
        observer.join()


__all__ = ["WatchOptions", "watch_directory"]
