"""``analyze`` subcommand: full pipeline (parse + classify + DXF/XML export)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ._common import EXIT_BAD_PATH, EXIT_NO_PARTS, EXIT_OK


def register(subparsers) -> argparse.ArgumentParser:
    """Register the ``analyze`` parser. Returns the parser."""
    analyze = subparsers.add_parser("analyze", help="full pipeline: classify + export")
    analyze.add_argument("path", help="path to a STEP file")
    analyze.add_argument(
        "--out-dir",
        default=str(Path.cwd() / "out"),
        help="output directory (default: ./out)",
    )
    analyze.add_argument("--no-dxf", action="store_true", help="skip DXF writing")
    analyze.add_argument("--no-xml", action="store_true", help="skip XML writing")
    analyze.add_argument(
        "--no-cache",
        action="store_true",
        help="bypass the disk pipeline cache (read and write)",
    )
    analyze.add_argument(
        "--no-prefilter",
        action="store_true",
        help=(
            "disable the cheap per-solid prefilter and the duplicate cache; "
            "every solid runs through the full UnfoldProbe + slicer pipeline. "
            "Slow on large assemblies but useful for regression testing."
        ),
    )
    analyze.add_argument(
        "--scorers",
        default=None,
        help="path to a custom scorer-weights YAML; defaults to the bundled config",
    )
    analyze.add_argument("--quiet", action="store_true", help="suppress info output")
    analyze.add_argument("--verbose", action="store_true", help="enable debug logs")
    analyze.set_defaults(func=run)
    return analyze


def run(args: argparse.Namespace) -> int:
    """Execute the analyze subcommand. Returns exit code."""
    from manufacturing_pipeline.parsing.types import StepParseError
    from manufacturing_pipeline.pipeline.analyze_assembly import (
        AnalyzeOptions,
        analyze,
    )

    path = Path(args.path).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    scorers_path = (
        Path(args.scorers).expanduser().resolve() if args.scorers else None
    )

    options = AnalyzeOptions(
        out_dir=out_dir,
        write_dxf=not args.no_dxf,
        write_xml=not args.no_xml,
        use_cache=not args.no_cache,
        scorers_path=scorers_path,
        prefilter=not args.no_prefilter,
    )

    try:
        result = analyze(path, options)
    except FileNotFoundError as exc:
        print(f"error: file not found: {exc}", file=sys.stderr)
        return EXIT_BAD_PATH
    except StepParseError as exc:
        print(f"error: cannot parse {path}: {exc}", file=sys.stderr)
        return EXIT_BAD_PATH

    entries = result.manifest.parts
    if not entries:
        print("error: no parts parsed", file=sys.stderr)
        return EXIT_NO_PARTS

    _print_summary(entries, quiet=args.quiet, out_dir=out_dir, path=path)
    return EXIT_OK


def _print_summary(entries, *, quiet: bool, out_dir: Path, path: Path) -> None:
    """Write a human-readable summary to stdout (or stderr in quiet mode)."""
    stream = sys.stderr if quiet else sys.stdout
    counts = {"plaat": 0, "profiel": 0, "anders": 0, "uncertain": 0}
    for entry in entries:
        counts[entry.classification.label] = counts.get(entry.classification.label, 0) + 1

    print(f"source: {path}", file=stream)
    print(f"output: {out_dir}", file=stream)
    print(
        f"parts: {len(entries)} "
        f"(plaat={counts['plaat']} profiel={counts['profiel']} "
        f"anders={counts['anders']} uncertain={counts['uncertain']})",
        file=stream,
    )
    for entry in entries:
        flat = f" [{entry.flat_dxf_path}]" if entry.flat_dxf_path else ""
        print(
            f"  {entry.part.product_id} {entry.part.name} -> "
            f"{entry.classification.label} "
            f"(conf={entry.classification.confidence:.2f}){flat}",
            file=stream,
        )
