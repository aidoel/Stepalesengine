"""``version`` subcommand: print the installed/declared project version."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from ._common import EXIT_OK


def register(subparsers) -> argparse.ArgumentParser:
    """Register the ``version`` parser. Returns the parser."""
    version = subparsers.add_parser("version", help="print project version")
    version.set_defaults(func=run)
    return version


def run(_args: argparse.Namespace) -> int:
    """Execute the version subcommand. Returns exit code."""
    print(_get_version())
    return EXIT_OK


def _get_version() -> str:
    """Return the installed package version, or read it out of pyproject.toml."""
    try:
        from importlib.metadata import version

        return version("stepalesengine")
    except Exception:
        pass

    # Walk up from this file: <repo>/manufacturing_pipeline/cli/_version.py
    pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    if not pyproject.is_file():
        return "0.0.0"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "0.0.0"
