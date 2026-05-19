"""Shared helpers and constants for the stepalesengine CLI subcommands."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_logger = logging.getLogger("stepalesengine")

EXIT_OK = 0
EXIT_NO_PARTS = 1
EXIT_BAD_PATH = 2


def _safe_name(name: str) -> str:
    """Sanitise a part name for use as a filename.

    Lowercase, non-alnum -> underscore, truncated to 64 chars. Never empty.
    Kept as a public-ish CLI helper for backwards compatibility with the
    test suite; delegates to the orchestrator's private helper.
    """
    from manufacturing_pipeline.pipeline.analyze_assembly import _safe_name as _impl

    return _impl(name)


def _resolve_path(p: str) -> Path:
    """Expand and resolve a path string to an absolute Path."""
    return Path(p).expanduser().resolve()


def _setup_logging(verbose: bool, quiet: bool) -> None:
    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _print_err(msg: str) -> None:
    """Write a single line to stderr."""
    print(msg, file=sys.stderr)
