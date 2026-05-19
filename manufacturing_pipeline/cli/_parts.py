"""``parts`` subcommand: parser-only listing of a STEP file."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ._common import EXIT_BAD_PATH, EXIT_NO_PARTS, EXIT_OK


def register(subparsers) -> argparse.ArgumentParser:
    """Register the ``parts`` parser. Returns the parser."""
    parts = subparsers.add_parser("parts", help="parser-only listing")
    parts.add_argument("path", help="path to a STEP file")
    parts.set_defaults(func=run)
    return parts


def run(args: argparse.Namespace) -> int:
    """Execute the parts subcommand. Returns exit code."""
    from manufacturing_pipeline.parsing.step_parser import parse_step
    from manufacturing_pipeline.parsing.types import StepParseError

    path = Path(args.path).expanduser().resolve()
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        return EXIT_BAD_PATH

    try:
        parts = parse_step(path)
    except StepParseError as exc:
        print(f"error: cannot parse {path}: {exc}", file=sys.stderr)
        return EXIT_BAD_PATH

    if not parts:
        print("error: no parts parsed", file=sys.stderr)
        return EXIT_NO_PARTS

    for p in parts:
        print(f"{p.product_id}\t{p.name}\t{p.source}")
    return EXIT_OK
