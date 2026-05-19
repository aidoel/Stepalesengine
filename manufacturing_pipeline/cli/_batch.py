"""``batch`` subcommand: analyse every STEP file under a directory in parallel."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from ._common import EXIT_BAD_PATH, EXIT_NO_PARTS, EXIT_OK


def register(subparsers) -> argparse.ArgumentParser:
    """Register the ``batch`` parser. Returns the parser."""
    batch = subparsers.add_parser(
        "batch", help="analyse every STEP file under a directory in parallel"
    )
    batch.add_argument("input_dir", help="directory to walk for *.stp/*.step")
    batch.add_argument(
        "--out-dir",
        default=str(Path.cwd() / "out"),
        help="root output directory; each STEP gets its own subfolder",
    )
    batch.add_argument(
        "--workers",
        type=int,
        default=4,
        help="number of worker processes (capped at cpu_count - 1)",
    )
    batch.add_argument("--no-dxf", action="store_true", help="skip DXF writing")
    batch.add_argument("--no-xml", action="store_true", help="skip XML writing")
    batch.add_argument(
        "--no-cache",
        action="store_true",
        help="bypass the disk pipeline cache (read and write)",
    )
    batch.add_argument(
        "--scorers",
        default=None,
        help="path to a custom scorer-weights YAML; defaults to the bundled config",
    )
    batch.set_defaults(func=run)
    return batch


def _collect_step_files(root: Path) -> list[Path]:
    """Recursively gather .stp/.step files under ``root`` (case-insensitive)."""
    suffixes = {".stp", ".step"}
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in suffixes:
            files.append(path)
    return files


def run(args: argparse.Namespace) -> int:
    """Execute the batch subcommand. Returns exit code."""
    from manufacturing_pipeline.batch import BatchResult, batch_analyze

    input_dir = Path(args.input_dir).expanduser().resolve()
    if not input_dir.exists() or not input_dir.is_dir():
        print(f"error: input directory not found: {input_dir}", file=sys.stderr)
        return EXIT_BAD_PATH

    out_root = Path(args.out_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    scorers_path = (
        Path(args.scorers).expanduser().resolve() if args.scorers else None
    )

    files = _collect_step_files(input_dir)
    if not files:
        print(f"error: no .stp/.step files found under {input_dir}", file=sys.stderr)
        return EXIT_NO_PARTS

    total = len(files)
    counter = {"i": 0}

    def _on_complete(result: BatchResult) -> None:
        counter["i"] += 1
        i = counter["i"]
        labels = " ".join(f"{k}={v}" for k, v in sorted(result.label_counts.items()))
        labels = labels or "-"
        status = "ok" if result.ok else f"FAIL ({result.error})"
        print(
            f"[{i}/{total}] {result.file.name} -> {status} "
            f"labels={labels} {result.duration_s:.1f}s warns={result.warnings}"
        )

    t0 = time.monotonic()
    results = batch_analyze(
        files,
        out_root,
        workers=int(args.workers),
        write_dxf=not args.no_dxf,
        write_xml=not args.no_xml,
        use_cache=not args.no_cache,
        scorers_path=scorers_path,
        progress=_on_complete,
    )
    total_dur = time.monotonic() - t0

    ok = sum(1 for r in results if r.ok)
    failed = total - ok
    avg = (total_dur / total) if total else 0.0

    print()
    print("Batch summary")
    print(f"  files:    {total}")
    print(f"  ok:       {ok}")
    print(f"  failed:   {failed}")
    print(f"  total:    {total_dur:.1f}s")
    print(f"  avg/file: {avg:.1f}s")

    return EXIT_OK if failed == 0 else EXIT_NO_PARTS
