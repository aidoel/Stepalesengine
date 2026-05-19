"""Command-line entry point for stepalesengine.

Subcommands
-----------
* ``analyze PATH`` - full pipeline: parse + load + classify + DXF/XML export.
* ``parts PATH``   - quick parser-only listing.
* ``diff OLD NEW`` - diff two STEP files.
* ``version``      - print the project version (from installed metadata).
* ``batch DIR``    - analyse every STEP file under a directory in parallel.
* ``validate-corpus DIR`` - run analyze across a directory and emit reports.
* ``serve-corpus DIR`` - serve the report with drill-down into per-file viewers.
* ``calibrate CSV`` - sweep classifier weights/thresholds on a labelled corpus.
* ``watch DIR``    - long-running re-run of analyze on STEP changes.

Exit codes
----------
* ``0`` - success.
* ``1`` - the file parsed/loaded but yielded zero parts.
* ``2`` - the input path does not exist or cannot be read.
"""

from __future__ import annotations

import argparse

from . import (
    _analyze,
    _batch,
    _calibrate,
    _diff,
    _parts,
    _serve_corpus,
    _validate,
    _version,
    _watch,
)
from ._common import _setup_logging

_SUBCOMMANDS = (
    _analyze,
    _parts,
    _diff,
    _version,
    _batch,
    _validate,
    _serve_corpus,
    _calibrate,
    _watch,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stepalesengine",
        description="STEP analysis pipeline: parse, classify, export DXF/XML.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for module in _SUBCOMMANDS:
        module.register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point dispatched to subcommands. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    quiet = bool(getattr(args, "quiet", False))
    verbose = bool(getattr(args, "verbose", False))
    _setup_logging(verbose=verbose, quiet=quiet)

    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
