"""``validate-corpus`` subcommand: run the analyze pipeline across a directory of STEP files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ._common import EXIT_BAD_PATH, EXIT_NO_PARTS, EXIT_OK


def register(subparsers) -> argparse.ArgumentParser:
    """Register the ``validate-corpus`` parser. Returns the parser."""
    validate = subparsers.add_parser(
        "validate-corpus",
        help="run the analyze pipeline across a directory of STEP files and emit HTML+MD reports",
    )
    validate.add_argument("dir", help="directory to walk for *.stp/*.step files")
    validate.add_argument(
        "--out-dir",
        default=None,
        help="where to write per-file manifests + DXFs (requires --write-outputs)",
    )
    validate.add_argument(
        "--workers",
        type=int,
        default=4,
        help="worker process count (capped at cpu_count - 1)",
    )
    validate.add_argument(
        "--html",
        default=str(Path.cwd() / "report.html"),
        help="output path for the HTML report (default: ./report.html)",
    )
    validate.add_argument(
        "--md",
        default=str(Path.cwd() / "report.md"),
        help="output path for the Markdown report (default: ./report.md)",
    )
    validate.add_argument(
        "--write-outputs",
        action="store_true",
        help="persist per-file manifests + DXFs to --out-dir (slower, fills disk)",
    )
    validate.add_argument(
        "--pdf",
        default=None,
        help=(
            "optional path for a multi-page corpus PDF (cover + one page per part). "
            "Requires --write-outputs so the per-file manifests are present on disk."
        ),
    )
    validate.set_defaults(func=run)
    return validate


def run(args: argparse.Namespace) -> int:
    """Execute the validate-corpus subcommand. Returns exit code."""
    from manufacturing_pipeline.validate import (
        render_html_report,
        render_markdown_report,
        validate_corpus,
    )

    root = Path(args.dir).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        print(f"error: directory not found: {root}", file=sys.stderr)
        return EXIT_BAD_PATH

    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else None
    html_path = Path(args.html).expanduser().resolve()
    md_path = Path(args.md).expanduser().resolve()

    report = validate_corpus(
        root,
        workers=int(args.workers),
        out_dir=out_dir,
        write_outputs=bool(args.write_outputs),
    )

    render_html_report(report, html_path)
    render_markdown_report(report, md_path)

    pdf_path: Path | None = None
    pdf_status = ""
    if args.pdf:
        pdf_path = Path(args.pdf).expanduser().resolve()
        if not args.write_outputs or out_dir is None:
            pdf_status = "skipped (requires --write-outputs and --out-dir)"
            print(
                "warning: --pdf requested but --write-outputs / --out-dir not set; "
                "per-file manifests are not on disk, skipping PDF.",
                file=sys.stderr,
            )
        else:
            pdf_status = _emit_corpus_pdf(report, out_dir, pdf_path)

    print("Corpus validation summary")
    print(f"  root:      {report.root}")
    print(f"  files:     {report.total_files}")
    print(f"  ok:        {report.ok_count}")
    print(f"  failed:    {report.failed_count}")
    print(f"  parts:     {report.parts_total}")
    print(f"  size:      {report.total_size_mb:.1f} MB")
    print(f"  wall:      {report.total_duration_s:.1f}s")
    print(f"  anomalies: {len(report.anomalies)}")
    print(f"  html:      {html_path}")
    print(f"  md:        {md_path}")
    if pdf_path is not None:
        print(f"  pdf:       {pdf_path}  {pdf_status}".rstrip())

    return EXIT_OK if report.failed_count == 0 else EXIT_NO_PARTS


def _emit_corpus_pdf(report, out_dir: Path, pdf_path: Path) -> str:
    """Load per-file manifest.xml records from disk and render the corpus PDF.

    Returns a short status string for the CLI summary line.
    """
    from manufacturing_pipeline.io.pdf_corpus_writer import (
        CorpusPDFOptions,
        write_corpus_pdf,
    )
    from manufacturing_pipeline.io.xml_writer import read_xml
    from manufacturing_pipeline.manifest import ManifestParseError

    manifests = []
    missing = 0
    parse_failed = 0
    for f in report.files:
        if not f.safe_name:
            missing += 1
            continue
        manifest_path = out_dir / f.safe_name / "manifest.xml"
        if not manifest_path.is_file():
            missing += 1
            continue
        try:
            manifests.append(read_xml(manifest_path))
        except (FileNotFoundError, ManifestParseError):
            parse_failed += 1
            continue

    if not manifests:
        return "skipped (no manifests on disk)"

    avg = report.total_duration_s / report.total_files if report.total_files else 0.0
    extra_summary = {
        "wall_s": float(report.total_duration_s),
        "mean_per_file_s": float(avg),
        "n_files": int(report.total_files),
    }

    write_corpus_pdf(
        manifests,
        pdf_path,
        options=CorpusPDFOptions(),
        extra_summary=extra_summary,
        anomalies=list(report.anomalies),
    )
    tail = []
    if missing:
        tail.append(f"missing={missing}")
    if parse_failed:
        tail.append(f"parse_failed={parse_failed}")
    return ("(" + ", ".join(tail) + ")") if tail else ""
