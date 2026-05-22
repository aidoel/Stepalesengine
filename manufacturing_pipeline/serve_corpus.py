"""Combined corpus + per-file web viewer.

Mounts the per-file Flask app created by
:func:`manufacturing_pipeline.web.server.create_app` underneath
``/file/<safe_name>/`` for every subdirectory of ``report_dir`` that contains
a ``manifest.xml``. The top-level route renders the corpus HTML report so
users can drill from a row in the report straight into the trace browser
for the underlying STEP file.

Layout on disk (produced by ``stepalesengine validate-corpus --write-outputs``)::

    REPORT_DIR/
      <safe_name>/manifest.xml
      <safe_name>/parts/*.dxf
      ...
      report.html             (optional, regenerated on demand)

The serve-corpus app works even when ``--write-outputs`` was not used: in
that case there are no per-file manifests, and the index renders without
drill-down links.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import Flask, abort
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from manufacturing_pipeline.io.xml_writer import (
    AssemblyManifest,
    ManifestParseError,
    read_xml,
)
from manufacturing_pipeline.validate import (
    CorpusFile,
    CorpusReport,
    _build_anomalies,
    render_html_report,
)
from manufacturing_pipeline.web.server import create_app as create_file_app

_logger = logging.getLogger("stepalesengine.serve_corpus")

EXIT_OK = 0
EXIT_BAD_PATH = 2


# ---------------------------------------------------------------------------
# Corpus report regeneration from on-disk manifests
# ---------------------------------------------------------------------------


@dataclass
class _DiscoveredFile:
    safe_name: str
    manifest_path: Path
    manifest: AssemblyManifest


def _discover_per_file_manifests(report_dir: Path) -> list[_DiscoveredFile]:
    """Walk ``report_dir`` one level deep, return the per-file manifests we find.

    A directory is treated as a per-file output bundle when it contains a
    readable ``manifest.xml``. Directories without one (or whose manifest is
    corrupt) are silently skipped so a partially-broken corpus still serves.
    """
    found: list[_DiscoveredFile] = []
    if not report_dir.is_dir():
        return found

    for entry in sorted(report_dir.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.xml"
        if not manifest_path.is_file():
            continue
        try:
            manifest = read_xml(manifest_path)
        except (FileNotFoundError, ManifestParseError) as exc:
            _logger.debug("skipping unreadable manifest %s: %s", manifest_path, exc)
            continue
        found.append(
            _DiscoveredFile(
                safe_name=entry.name,
                manifest_path=manifest_path,
                manifest=manifest,
            )
        )
    return found


def _record_from_manifest(
    safe_name: str,
    manifest_path: Path,
    manifest: AssemblyManifest,
) -> CorpusFile:
    """Translate a stored per-file manifest into a :class:`CorpusFile`."""
    label_counts: dict[str, int] = {}
    n_holes = 0
    n_bends = 0
    pmi_has_semantic = False
    pmi_n_annotations = 0
    bbox_max_mm = 0.0
    for entry in manifest.parts:
        label = entry.classification.label
        label_counts[label] = label_counts.get(label, 0) + 1
        if entry.holes is not None:
            n_holes += int(entry.holes.hole_count)
        if entry.unfold is not None:
            n_bends += int(entry.unfold.n_bends)
        if entry.pmi is not None:
            pmi_n_annotations += int(entry.pmi.n_annotations)
            if entry.pmi.has_semantic:
                pmi_has_semantic = True
        if entry.features is not None and entry.features.bbox_dims_sorted:
            local = max(float(d) for d in entry.features.bbox_dims_sorted)
            if local > bbox_max_mm:
                bbox_max_mm = local

    source_path = Path(manifest.source_path)
    rel = source_path.name or safe_name
    size = 0
    try:
        if source_path.is_file():
            size = source_path.stat().st_size
    except OSError:
        size = 0

    record = CorpusFile(
        relative_path=rel,
        size_bytes=size,
        parts=len(manifest.parts),
        labels=label_counts,
        n_holes=n_holes,
        n_bends=n_bends,
        pmi_has_semantic=pmi_has_semantic,
        pmi_n_annotations=pmi_n_annotations,
        duration_s=0.0,
        ok=True,
        error="",
        bbox_max_mm=bbox_max_mm,
        is_ap242=False,
        safe_name=safe_name,
    )
    record.warnings = _build_anomalies(record)
    return record


def _build_report_from_disk(report_dir: Path, files: list[_DiscoveredFile]) -> CorpusReport:
    """Aggregate discovered per-file manifests into a :class:`CorpusReport`."""
    records: list[CorpusFile] = []
    total_size = 0
    ok_count = 0
    failed_count = 0
    parts_total = 0
    label_distribution: dict[str, int] = {}
    anomaly_lines: list[str] = []
    for f in files:
        record = _record_from_manifest(f.safe_name, f.manifest_path, f.manifest)
        records.append(record)
        total_size += record.size_bytes
        if record.ok:
            ok_count += 1
        else:
            failed_count += 1
        parts_total += record.parts
        for label, count in record.labels.items():
            label_distribution[label] = label_distribution.get(label, 0) + count
        if record.warnings:
            anomaly_lines.append(f"{record.relative_path}: " + "; ".join(record.warnings))

    return CorpusReport(
        root=str(report_dir),
        total_files=len(records),
        total_size_mb=round(total_size / (1024 * 1024), 2),
        total_duration_s=0.0,
        ok_count=ok_count,
        failed_count=failed_count,
        label_distribution=label_distribution,
        parts_total=parts_total,
        files=records,
        anomalies=anomaly_lines,
    )


# ---------------------------------------------------------------------------
# Outer Flask app + WSGI dispatcher
# ---------------------------------------------------------------------------


def _build_outer_app(
    report_dir: Path,
    discovered: list[_DiscoveredFile],
) -> Flask:
    """Build the top-level Flask app that serves the corpus index."""
    app = Flask("stepalesengine.serve_corpus")
    app.config["REPORT_DIR"] = report_dir
    app.config["DISCOVERED"] = discovered

    @app.get("/")
    def index() -> tuple[str, int, dict[str, str]]:
        report = _build_report_from_disk(report_dir, discovered)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as tmp:
            tmp_path = Path(tmp.name)
        try:
            render_html_report(report, tmp_path, link_mode=bool(discovered))
            html = tmp_path.read_text(encoding="utf-8")
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return (html, 200, {"Content-Type": "text/html; charset=utf-8"})

    @app.get("/file/<safe_name>")
    @app.get("/file/<safe_name>/")
    def _file_not_mounted(safe_name: str):
        # If the URL reaches this handler it means the dispatcher didn't have
        # a mount for <safe_name>. Return a 404 with a clear message.
        abort(404, description=f"no per-file viewer mounted at {safe_name!r}")

    return app


def create_corpus_app(report_dir: Path | str) -> Any:
    """Build a WSGI application that serves the corpus + per-file viewers.

    Returns a :class:`werkzeug.middleware.dispatcher.DispatcherMiddleware` so
    each discovered ``<safe_name>/manifest.xml`` is mounted at
    ``/file/<safe_name>``. Tests can call this directly and wrap it with
    :class:`werkzeug.test.Client`.
    """
    report_dir = Path(report_dir).expanduser().resolve()
    discovered = _discover_per_file_manifests(report_dir)

    outer = _build_outer_app(report_dir, discovered)

    mounts: dict[str, Any] = {}
    for f in discovered:
        try:
            sub_app = create_file_app(
                f.manifest_path,
                out_dir=f.manifest_path.parent,
            )
        except Exception as exc:  # noqa: BLE001 - one bad file mustn't kill the corpus
            _logger.warning("could not mount per-file viewer for %s: %s", f.safe_name, exc)
            continue
        mounts[f"/file/{f.safe_name}"] = sub_app

    return DispatcherMiddleware(outer.wsgi_app, mounts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """``stepalesengine-serve-corpus REPORT_DIR [--port N]``."""
    parser = argparse.ArgumentParser(
        prog="stepalesengine-serve-corpus",
        description=(
            "Serve a corpus validator REPORT_DIR (containing <safe_name>/manifest.xml "
            "subdirs) with drill-down into the per-file trace browser."
        ),
    )
    parser.add_argument("report_dir", type=Path, help="directory produced by validate-corpus")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args(argv)

    report_dir = args.report_dir.expanduser().resolve()
    if not report_dir.is_dir():
        print(f"error: report directory not found: {report_dir}", file=sys.stderr)
        return EXIT_BAD_PATH

    wsgi_app = create_corpus_app(report_dir)

    # werkzeug's serving.run_simple drives the WSGI app directly so we can host
    # the DispatcherMiddleware without wrapping it in a Flask app.
    from werkzeug.serving import run_simple

    run_simple(args.host, args.port, wsgi_app, use_reloader=False, use_debugger=False)
    return EXIT_OK


__all__ = ["create_corpus_app", "main"]
