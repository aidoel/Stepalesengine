"""``diff`` subcommand: diff two STEP files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ._common import EXIT_BAD_PATH, EXIT_NO_PARTS, EXIT_OK


def register(subparsers) -> argparse.ArgumentParser:
    """Register the ``diff`` parser. Returns the parser."""
    diff = subparsers.add_parser("diff", help="diff two STEP files")
    diff.add_argument("old_path", help="old/baseline STEP file")
    diff.add_argument("new_path", help="new/candidate STEP file")
    diff.add_argument(
        "--match-by",
        choices=("name", "product_id", "fingerprint"),
        default="name",
        help="how to pair parts across the two assemblies (default: name)",
    )
    diff.add_argument(
        "--tolerance",
        type=float,
        default=1.0,
        help="relative tolerance (%%) for geometric fields (default: 1.0)",
    )
    diff.set_defaults(func=run)
    return diff


def run(args: argparse.Namespace) -> int:
    """Execute the diff subcommand. Returns exit code."""
    from manufacturing_pipeline.parsing.types import StepParseError
    from manufacturing_pipeline.pipeline.analyze_assembly import AnalyzeOptions
    from manufacturing_pipeline.pipeline.diff import diff_step_files, render_diff_text

    old_path = Path(args.old_path).expanduser().resolve()
    new_path = Path(args.new_path).expanduser().resolve()

    opts = AnalyzeOptions(out_dir=None, write_dxf=False, write_xml=False)
    try:
        result = diff_step_files(
            old_path,
            new_path,
            options=opts,
            match_by=args.match_by,
            tolerance_pct=float(args.tolerance),
        )
    except FileNotFoundError as exc:
        print(f"error: file not found: {exc}", file=sys.stderr)
        return EXIT_BAD_PATH
    except StepParseError as exc:
        print(f"error: cannot parse STEP: {exc}", file=sys.stderr)
        return EXIT_BAD_PATH

    print(render_diff_text(result))
    has_diff = bool(result.added or result.removed or result.changed)
    return EXIT_NO_PARTS if has_diff else EXIT_OK
