"""``serve-corpus`` subcommand: drill from the HTML report into per-file viewers."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ._common import EXIT_BAD_PATH, EXIT_OK


def register(subparsers) -> argparse.ArgumentParser:
    """Register the ``serve-corpus`` parser. Returns the parser."""
    serve = subparsers.add_parser(
        "serve-corpus",
        help=("serve the corpus HTML report with drill-down into the per-file trace browser"),
    )
    serve.add_argument(
        "report_dir",
        help="directory produced by validate-corpus (contains <safe_name>/manifest.xml)",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5050)
    serve.set_defaults(func=run)
    return serve


def run(args: argparse.Namespace) -> int:
    """Execute the serve-corpus subcommand. Returns exit code."""
    from manufacturing_pipeline.serve_corpus import create_corpus_app

    report_dir = Path(args.report_dir).expanduser().resolve()
    if not report_dir.exists() or not report_dir.is_dir():
        print(f"error: report directory not found: {report_dir}", file=sys.stderr)
        return EXIT_BAD_PATH

    wsgi_app = create_corpus_app(report_dir)

    from werkzeug.serving import run_simple

    run_simple(
        args.host,
        int(args.port),
        wsgi_app,
        use_reloader=False,
        use_debugger=False,
    )
    return EXIT_OK
