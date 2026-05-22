"""``watch`` subcommand: long-running analyzer that re-runs on STEP changes."""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
import time
from pathlib import Path

from ._common import EXIT_BAD_PATH, EXIT_OK


def register(subparsers) -> argparse.ArgumentParser:
    """Register the ``watch`` parser. Returns the parser."""
    watch = subparsers.add_parser(
        "watch", help="re-run analyze on every STEP file change under a directory"
    )
    watch.add_argument("dir", help="directory to watch for *.stp/*.step changes")
    watch.add_argument(
        "--out-dir",
        default=str(Path.cwd() / "out"),
        help="output directory (default: ./out)",
    )
    watch.add_argument(
        "--workers",
        type=int,
        default=1,
        help="reserved for future parallel watchers (currently unused, default 1)",
    )
    watch.add_argument(
        "--initial-scan",
        action="store_true",
        help="process every existing matching file at start",
    )
    watch.add_argument(
        "--no-recursive",
        action="store_true",
        help="do not descend into subdirectories",
    )
    watch.add_argument(
        "--debounce",
        type=float,
        default=1.0,
        help="seconds to wait before re-processing the same file (default 1.0)",
    )
    watch.add_argument("--no-dxf", action="store_true", help="skip DXF writing")
    watch.add_argument("--no-xml", action="store_true", help="skip XML writing")
    watch.add_argument(
        "--no-cache",
        action="store_true",
        help="bypass the disk pipeline cache (read and write)",
    )
    watch.set_defaults(func=run)
    return watch


def run(args: argparse.Namespace) -> int:
    """Execute the watch subcommand. Returns exit code."""
    from manufacturing_pipeline.pipeline.analyze_assembly import (
        AnalyzeOptions,
        AnalyzeResult,
        analyze,
    )
    from manufacturing_pipeline.watch import WatchOptions, watch_directory

    root = Path(args.dir).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        print(f"error: watch directory not found: {root}", file=sys.stderr)
        return EXIT_BAD_PATH

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Stats kept on a mutable container so the on_done closure can update them.
    stats: dict[str, object] = {
        "events": 0,
        "files": set(),
        "last_run_at": None,
    }

    _last_durations: dict[str, float] = {}

    def _analyze_fn(path: Path) -> AnalyzeResult:
        options = AnalyzeOptions(
            out_dir=out_dir / path.stem,
            write_dxf=not args.no_dxf,
            write_xml=not args.no_xml,
            use_cache=not args.no_cache,
        )
        t0 = time.monotonic()
        result = analyze(path, options)
        _last_durations[str(path)] = time.monotonic() - t0
        return result

    def _on_done(path: Path, result: AnalyzeResult) -> None:
        stats["events"] = int(stats["events"]) + 1  # type: ignore[call-overload]
        files_set = stats["files"]
        if isinstance(files_set, set):
            files_set.add(str(path))
        now = _dt.datetime.now()
        stats["last_run_at"] = now
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        counts: dict[str, int] = {}
        for entry in result.manifest.parts:
            label = entry.classification.label
            counts[label] = counts.get(label, 0) + 1
        label_str = " ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "-"
        wall = _last_durations.get(str(path), 0.0)
        print(
            f"[{now.strftime('%H:%M:%S')}] {rel} -> {label_str} (wall={wall:.1f}s)",
            flush=True,
        )

    opts = WatchOptions(
        debounce_s=float(args.debounce),
        initial_scan=bool(args.initial_scan),
        recursive=not args.no_recursive,
        on_done=_on_done,
    )

    print(f"watching {root} (out={out_dir}); Ctrl+C to stop", flush=True)
    watch_directory(root, _analyze_fn, opts)

    files_seen = stats["files"]
    n_files = len(files_seen) if isinstance(files_seen, set) else 0
    last_run = stats["last_run_at"]
    last_str = last_run.strftime("%H:%M:%S") if isinstance(last_run, _dt.datetime) else "never"
    print(
        f"watched {n_files} files, processed {stats['events']} events, last run at {last_str}",
        flush=True,
    )
    return EXIT_OK
