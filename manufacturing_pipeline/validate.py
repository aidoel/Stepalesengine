"""Corpus-level STEP validation.

Walks a directory tree for ``*.stp`` / ``*.step`` files, runs the analyze
pipeline on every match in parallel via :func:`manufacturing_pipeline.batch.batch_analyze`,
and aggregates the outcome into a :class:`CorpusReport`. Two renderers turn the
report into a self-contained HTML page or a Markdown document so a single
``stepalesengine validate-corpus DIR`` invocation produces something a human
can read and a CI job can diff.

The pipeline always serialises a manifest XML per file (into ``out_dir`` when
``write_outputs=True``, otherwise into a temporary directory that is cleaned
up at the end). The validation code reads those manifests back via
:func:`manufacturing_pipeline.io.xml_writer.read_xml` to extract richer
per-file fields (hole counts, bend counts, PMI semantics, bbox) than
:class:`manufacturing_pipeline.batch.BatchResult` carries on its own.
"""

from __future__ import annotations

import html
import logging
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from manufacturing_pipeline.batch import BatchResult, batch_analyze
from manufacturing_pipeline.io.xml_writer import ManifestParseError, read_xml

logger = logging.getLogger(__name__)

# Anomaly thresholds (kept module-level so tests can poke them).
DURATION_WARN_S = 30.0
BBOX_WARN_MM = 100_000.0

# Label palette matched against the web viewer (see web/server.py).
_LABEL_COLORS = {
    "plaat": ("#d9e8ff", "#0b3a82"),
    "profiel": ("#d6f5d6", "#1f5e1f"),
    "anders": ("#fff4c5", "#6b5300"),
    "uncertain": ("#e5e5e5", "#555555"),
}
_DEFAULT_LABEL_COLOR = ("#e5e5e5", "#555555")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CorpusFile:
    """One file's validation record."""

    relative_path: str
    size_bytes: int
    parts: int = 0
    labels: dict[str, int] = field(default_factory=dict)
    n_holes: int = 0
    n_bends: int = 0
    pmi_has_semantic: bool = False
    pmi_n_annotations: int = 0
    duration_s: float = 0.0
    ok: bool = False
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    # Internal fields used by anomaly detection. Not part of the spec dataclass
    # but kept so renderers can show why a file was flagged.
    bbox_max_mm: float = 0.0
    is_ap242: bool = False
    # On-disk subdir name (set when write_outputs=True so the HTML report can
    # link rows into the per-file web viewer). Empty when no per-file manifest
    # was written.
    safe_name: str = ""
    # Inline SVG markup for a small iso-projection thumbnail of the file's
    # primary part. Embedded directly into the HTML report so it stays
    # self-contained. Empty when no thumbnail could be rendered.
    thumbnail_svg: str = ""


@dataclass
class CorpusReport:
    """Aggregate report across a STEP corpus."""

    root: str
    total_files: int
    total_size_mb: float
    total_duration_s: float
    ok_count: int
    failed_count: int
    label_distribution: dict[str, int]
    parts_total: int
    files: list[CorpusFile]
    anomalies: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_step_files(root: Path) -> list[Path]:
    """Recursively gather *.stp / *.step files under ``root`` (case-insensitive)."""
    suffixes = {".stp", ".step"}
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in suffixes:
            files.append(path)
    return files


def _safe_dir_name(name: str) -> str:
    """Mirror of :func:`batch._safe_dir_name`. Inlined to avoid importing a private."""
    import re

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip()).strip("_")
    return cleaned[:96] or "step"


def _detect_ap242(step_path: Path) -> bool:
    """Best-effort AP242 detection: scan the STEP header for the AP242 schema token.

    Reading only the first 4 KiB keeps this cheap on large files. Robust enough
    for the anomaly rule that flags graphical-only PMI on AP242 files.
    """
    try:
        with open(step_path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    return b"AP242" in head or b"ap242" in head


def _extract_from_manifest(manifest_path: Path) -> dict:
    """Pull per-file aggregates out of a manifest XML."""
    out = {
        "n_holes": 0,
        "n_bends": 0,
        "pmi_has_semantic": False,
        "pmi_n_annotations": 0,
        "bbox_max_mm": 0.0,
    }
    try:
        manifest = read_xml(manifest_path)
    except (FileNotFoundError, ManifestParseError) as exc:
        logger.debug("could not read manifest %s: %s", manifest_path, exc)
        return out

    for entry in manifest.parts:
        if entry.holes is not None:
            out["n_holes"] += int(entry.holes.hole_count)
        if entry.unfold is not None:
            out["n_bends"] += int(entry.unfold.n_bends)
        if entry.pmi is not None:
            out["pmi_n_annotations"] += int(entry.pmi.n_annotations)
            if entry.pmi.has_semantic:
                out["pmi_has_semantic"] = True
        if entry.features is not None:
            bb = entry.features.bbox_dims_sorted
            if bb:
                local = max(float(d) for d in bb)
                if local > out["bbox_max_mm"]:
                    out["bbox_max_mm"] = local
    return out


# Thumbnail size for the per-row geometry preview in the HTML report.
_THUMB_WIDTH = 160
_THUMB_HEIGHT = 120
# Wall-clock budget for one thumbnail's HLR projection before we give up.
_THUMB_TIMEOUT_S = 60.0


def _thumbnail_worker(source_step: Path, manifest_path: Path) -> str:
    """Worker body: render the iso-projection SVG of a file's primary part.

    Top-level (picklable) so it can run inside a ``spawn`` subprocess. The
    "primary part" is the first part in the manifest. Returns the SVG string,
    or ``""`` when there is no part / projection produces nothing.
    """
    from manufacturing_pipeline.web.step_to_svg import render_part_views

    manifest = read_xml(Path(manifest_path))
    if not manifest.parts:
        return ""
    product_id = manifest.parts[0].part.product_id
    if not product_id:
        return ""
    svgs = render_part_views(
        Path(source_step),
        product_id,
        views=("iso",),
        width=_THUMB_WIDTH,
        height=_THUMB_HEIGHT,
    )
    return svgs.get("iso", "")


def _render_thumbnail_svg(source_step: Path, manifest_path: Path) -> str:
    """Render a small iso-projection SVG thumbnail of a file's primary part.

    Best-effort and crash-proof: the OCCT HLR projector can hard-segfault the
    interpreter on some solids, which no ``try/except`` can catch. The render
    therefore runs in a one-shot ``spawn`` subprocess — a crashed (or
    timed-out, or exception-raising) worker just yields an empty string, so a
    corpus run never fails over a missing thumbnail.
    """
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures import TimeoutError as FutTimeout

    ctx = mp.get_context("spawn")
    pool = ProcessPoolExecutor(max_workers=1, mp_context=ctx)
    try:
        future = pool.submit(_thumbnail_worker, Path(source_step), Path(manifest_path))
        return future.result(timeout=_THUMB_TIMEOUT_S) or ""
    except FutTimeout:
        logger.debug("thumbnail render timed out for %s", source_step)
        return ""
    except Exception as exc:  # noqa: BLE001 - thumbnail must never fail the run
        # BrokenProcessPool (segfault in the worker) lands here too.
        logger.debug("thumbnail render skipped for %s: %s", source_step, exc)
        return ""
    finally:
        # Don't block on a stuck/timed-out worker; kill it and move on.
        pool.shutdown(wait=False, cancel_futures=True)


def _build_anomalies(file_record: CorpusFile) -> list[str]:
    """Apply the corpus-validation anomaly rules to one file record."""
    reasons: list[str] = []
    if not file_record.ok:
        # Pipeline exception is itself an anomaly. Carry the error message.
        reasons.append(f"pipeline error: {file_record.error or 'unknown'}")
    else:
        if file_record.parts == 0:
            reasons.append("zero parts after pipeline ran")
    if file_record.duration_s > DURATION_WARN_S:
        reasons.append(f"long duration {file_record.duration_s:.1f}s")
    if file_record.bbox_max_mm > BBOX_WARN_MM:
        reasons.append(
            f"bbox dim {file_record.bbox_max_mm:.0f}mm > {BBOX_WARN_MM:.0f}mm (unit error?)"
        )
    if (
        file_record.is_ap242
        and file_record.pmi_n_annotations > 0
        and not file_record.pmi_has_semantic
    ):
        reasons.append("AP242 file with graphical-only PMI (no semantic annotations)")
    return reasons


# ---------------------------------------------------------------------------
# Public driver
# ---------------------------------------------------------------------------


def validate_corpus(
    root: Path,
    *,
    workers: int = 4,
    out_dir: Path | None = None,
    write_outputs: bool = False,
) -> CorpusReport:
    """Walk ``root`` for STEP files, run the pipeline, return a CorpusReport.

    Parameters
    ----------
    root:
        Directory to walk. Missing or non-directory roots produce an empty
        :class:`CorpusReport` rather than raising.
    workers:
        Forwarded to :func:`batch_analyze`. Clamped to ``cpu_count - 1`` inside.
    out_dir:
        Where manifests + DXFs are written when ``write_outputs=True``. Ignored
        otherwise; the run uses a self-cleaning temporary directory.
    write_outputs:
        When True the pass writes per-file manifest.xml + DXFs into ``out_dir``.
        When False the pipeline still runs end-to-end (we need the manifest to
        extract holes/bends/PMI), but the writes land in a temp dir that is
        cleaned up before this function returns.
    """
    root = Path(root)
    files = _collect_step_files(root) if root.is_dir() else []

    if not files:
        return CorpusReport(
            root=str(root),
            total_files=0,
            total_size_mb=0.0,
            total_duration_s=0.0,
            ok_count=0,
            failed_count=0,
            label_distribution={},
            parts_total=0,
            files=[],
            anomalies=[],
        )

    persistent = bool(write_outputs and out_dir is not None)
    temp_holder: tempfile.TemporaryDirectory | None = None
    if persistent and out_dir is not None:
        run_root = Path(out_dir)
        run_root.mkdir(parents=True, exist_ok=True)
    else:
        temp_holder = tempfile.TemporaryDirectory(prefix="stepalesengine-validate-")
        run_root = Path(temp_holder.name)

    t0 = time.monotonic()
    try:
        results: list[BatchResult] = batch_analyze(
            files,
            run_root,
            workers=int(workers),
            write_dxf=bool(write_outputs),
            write_xml=True,
            use_cache=True,
        )

        # batch_analyze returns results in completion order (parallel) or
        # input order (workers==1). Build a path->index map keyed by name
        # so each STEP's per-file metadata is matched correctly even after
        # the pool has reordered the list.
        by_path = {Path(r.file).resolve(): r for r in results}

        records: list[CorpusFile] = []
        total_size = 0
        ok_count = 0
        failed_count = 0
        parts_total = 0
        label_distribution: dict[str, int] = {}
        anomaly_lines: list[str] = []

        for step_path in files:
            resolved = step_path.resolve()
            result = by_path.get(resolved)
            try:
                rel = str(step_path.relative_to(root))
            except ValueError:
                rel = str(step_path)
            size = step_path.stat().st_size if step_path.is_file() else 0
            total_size += size

            if result is None:
                record = CorpusFile(
                    relative_path=rel,
                    size_bytes=size,
                    ok=False,
                    error="no batch result returned",
                    safe_name=(_safe_dir_name(step_path.stem) if persistent else ""),
                )
                failed_count += 1
                record.warnings = _build_anomalies(record)
                if record.warnings:
                    anomaly_lines.append(f"{rel}: " + "; ".join(record.warnings))
                records.append(record)
                continue

            manifest_path = result.manifest_path
            extra = (
                _extract_from_manifest(manifest_path)
                if manifest_path and manifest_path.exists()
                else {
                    "n_holes": 0,
                    "n_bends": 0,
                    "pmi_has_semantic": False,
                    "pmi_n_annotations": 0,
                    "bbox_max_mm": 0.0,
                }
            )

            record = CorpusFile(
                relative_path=rel,
                size_bytes=size,
                parts=int(result.parts),
                labels=dict(result.label_counts),
                n_holes=int(extra["n_holes"]),
                n_bends=int(extra["n_bends"]),
                pmi_has_semantic=bool(extra["pmi_has_semantic"]),
                pmi_n_annotations=int(extra["pmi_n_annotations"]),
                duration_s=float(result.duration_s),
                ok=bool(result.ok),
                error=result.error or "",
                bbox_max_mm=float(extra["bbox_max_mm"]),
                is_ap242=_detect_ap242(step_path),
                safe_name=(_safe_dir_name(step_path.stem) if persistent else ""),
            )

            # When --write-outputs is set, render a small iso thumbnail of the
            # primary part next to the manifest and embed it in the report.
            if persistent and manifest_path and manifest_path.exists():
                thumb = _render_thumbnail_svg(step_path, manifest_path)
                if thumb:
                    record.thumbnail_svg = thumb
                    try:
                        (manifest_path.parent / "thumb.svg").write_text(thumb, encoding="utf-8")
                    except OSError as exc:
                        logger.debug("could not write thumb.svg for %s: %s", step_path, exc)

            if record.ok:
                ok_count += 1
                parts_total += record.parts
                for label, count in record.labels.items():
                    label_distribution[label] = label_distribution.get(label, 0) + count
            else:
                failed_count += 1

            record.warnings = _build_anomalies(record)
            if record.warnings:
                anomaly_lines.append(f"{rel}: " + "; ".join(record.warnings))

            records.append(record)
    finally:
        if temp_holder is not None:
            temp_holder.cleanup()

    total_duration = time.monotonic() - t0

    return CorpusReport(
        root=str(root),
        total_files=len(records),
        total_size_mb=round(total_size / (1024 * 1024), 2),
        total_duration_s=round(total_duration, 2),
        ok_count=ok_count,
        failed_count=failed_count,
        label_distribution=label_distribution,
        parts_total=parts_total,
        files=records,
        anomalies=anomaly_lines,
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _shared_css() -> str:
    """Return the shared web-UI stylesheet (``web/templates/base.css``).

    The corpus report and the per-file viewer then render from one theme.
    Returns an empty string if the file cannot be read, so report generation
    still succeeds from an unusual install layout.
    """
    css_path = Path(__file__).parent / "web" / "templates" / "base.css"
    try:
        return css_path.read_text(encoding="utf-8")
    except OSError:
        return ""


# Corpus-report-only CSS. The shared visual theme — body, cards, labels,
# links, typography — lives in base.css (loaded by _shared_css); this block
# adds just the dashboard widgets the corpus index needs on top of it.
_CORPUS_EXTRA_CSS = """
body { max-width: 1400px; margin: 0 auto; }
h1 { font-size: 24px; margin: 0 0 6px 0; }
h2 { font-size: 14px; text-transform: uppercase; letter-spacing: 0.06em;
     color: #555; margin: 24px 0 10px 0; }
.corpus-header { margin-bottom: 18px; }
.corpus-header .lede { font-size: 15px; color: #555; max-width: 72ch;
                       margin: 6px 0 10px 0; line-height: 1.5; }
.kpi-row { display: flex; flex-wrap: wrap; gap: 16px; }
.kpi { flex: 1 1 140px; padding: 12px 16px; background: #f5f5f5;
       border-radius: 6px; }
.kpi .k { font-size: 11px; text-transform: uppercase; color: #666;
          letter-spacing: 0.06em; }
.kpi .v { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
.dist-bar { height: 14px; border-radius: 3px; overflow: hidden;
            display: flex; background: #eee; margin: 6px 0; }
.dist-seg { height: 100%; }
.dist-legend { font-size: 12px; color: #555; display: flex; flex-wrap: wrap;
               gap: 12px; }
.anomaly-list { margin: 0; padding-left: 18px; font-size: 13px; }
.anomaly-list li { margin: 2px 0; }
.anomaly-empty { color: #1f5e1f; font-size: 13px; }
.toolbar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
           margin-bottom: 12px; }
.toolbar input[type=search] { flex: 1; min-width: 220px; padding: 8px 12px;
           border: 1px solid #ccc; border-radius: 6px; font-size: 14px;
           background: white; }
.toolbar input[type=search]:focus { outline: none; border-color: #1a5dd9;
           box-shadow: 0 0 0 3px rgba(26,93,217,0.15); }
.toolbar .count { font-size: 13px; color: #555; font-variant-numeric: tabular-nums; }
table { width: 100%; border-collapse: collapse; font-size: 13px; box-shadow: none; }
th { text-align: left; padding: 8px 10px; border-bottom: 2px solid #ddd;
     cursor: pointer; user-select: none; background: #fafafa;
     position: sticky; top: 0; font-weight: 600; text-transform: none;
     letter-spacing: 0; font-size: 13px; color: #1a1a1a; }
th.sort-asc::after { content: " \\25B2"; color: #999; font-size: 10px; }
th.sort-desc::after { content: " \\25BC"; color: #999; font-size: 10px; }
td { padding: 6px 10px; border-bottom: 1px solid #f0f0f0;
     font-variant-numeric: tabular-nums; }
tr.row-bad { background: #fff7f7; }
.pmi-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
           background: #1f5e1f; }
.pmi-dot.off { background: #ddd; }
.path-cell { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
             font-size: 12px; word-break: break-all; }
.thumb-cell { width: 168px; }
.thumb-cell svg { display: block; width: 160px; height: 120px;
                  border: 1px solid #e3e3e3; border-radius: 4px;
                  background: #fff; }
.thumb-cell .thumb-empty { display: flex; align-items: center;
                  justify-content: center; width: 160px; height: 120px;
                  border: 1px dashed #ddd; border-radius: 4px; color: #aaa;
                  font-size: 11px; }
.warn-cell { font-size: 11px; color: #821818; }
.link-note { color: #1a5dd9; font-size: 13px; margin: 4px 0 0 0; }
"""


def _label_pill(label: str, count: int) -> str:
    bg, fg = _LABEL_COLORS.get(label, _DEFAULT_LABEL_COLOR)
    return (
        f'<span class="label" style="background:{bg};color:{fg}">'
        f"{html.escape(label)} {count}</span>"
    )


def _format_size_mb(size_bytes: int) -> str:
    mb = size_bytes / (1024 * 1024)
    if mb >= 10:
        return f"{mb:.0f} MB"
    if mb >= 1:
        return f"{mb:.1f} MB"
    kb = size_bytes / 1024
    return f"{kb:.0f} KB"


def _label_distribution_html(distribution: dict[str, int]) -> str:
    total = sum(distribution.values())
    if total == 0:
        return '<p class="muted">no parts classified</p>'
    bar_segments = []
    legend_items = []
    for label, count in sorted(distribution.items(), key=lambda kv: -kv[1]):
        pct = (count / total) * 100.0
        bg, _fg = _LABEL_COLORS.get(label, _DEFAULT_LABEL_COLOR)
        bar_segments.append(
            f'<span class="dist-seg" style="width:{pct:.2f}%;background:{bg}" '
            f'title="{html.escape(label)}: {count} ({pct:.1f}%)"></span>'
        )
        legend_items.append(_label_pill(label, count))
    return (
        f'<div class="dist-bar">{"".join(bar_segments)}</div>'
        f'<div class="dist-legend">{" ".join(legend_items)}</div>'
    )


def _render_anomalies_html(report: CorpusReport) -> str:
    if not report.anomalies:
        return '<p class="anomaly-empty">no anomalies detected</p>'
    items = "\n".join(f"<li>{html.escape(line)}</li>" for line in report.anomalies)
    return f'<ul class="anomaly-list">{items}</ul>'


def _render_file_rows(
    files: list[CorpusFile],
    *,
    link_mode: bool = False,
    show_duration: bool = True,
    show_thumbnails: bool = False,
) -> str:
    rows = []
    for f in files:
        labels_html = (
            " ".join(_label_pill(label, count) for label, count in sorted(f.labels.items()))
            or '<span class="muted">-</span>'
        )
        pmi_dot = (
            '<span class="pmi-dot" title="semantic PMI"></span>'
            if f.pmi_has_semantic
            else (
                '<span class="pmi-dot off" title="graphical PMI only"></span>'
                if f.pmi_n_annotations > 0
                else '<span class="muted">-</span>'
            )
        )
        warn_text = "; ".join(f.warnings)
        ok_class = "row-bad" if (not f.ok or f.warnings) else ""
        # When the corpus was run with --write-outputs we know the per-file
        # subdir name and can hyperlink the path cell into the trace browser.
        path_html = html.escape(f.relative_path)
        if link_mode and f.safe_name:
            href = "/file/" + html.escape(f.safe_name, quote=True) + "/"
            path_html = f'<a href="{href}">{path_html}</a>'
        warn_html = (
            f'<br><span class="warn-cell">{html.escape(warn_text)}</span>' if warn_text else ""
        )
        duration_td = (
            f'<td data-sort="{f.duration_s:.3f}">{f.duration_s:.2f}s</td>' if show_duration else ""
        )
        # The thumbnail SVG produced by render_part_views is already a complete,
        # self-contained <svg> element; embed it inline so the report needs no
        # external assets.
        thumb_td = ""
        if show_thumbnails:
            thumb_inner = (
                f.thumbnail_svg if f.thumbnail_svg else '<div class="thumb-empty">no preview</div>'
            )
            thumb_td = f'<td class="thumb-cell">{thumb_inner}</td>'
        rows.append(
            f'<tr class="{ok_class}" data-ok="{"1" if f.ok else "0"}">'
            f"{thumb_td}"
            f'<td class="path-cell">{path_html}'
            f"{warn_html}"
            f"</td>"
            f'<td data-sort="{f.size_bytes}">{_format_size_mb(f.size_bytes)}</td>'
            f'<td data-sort="{f.parts}">{f.parts}</td>'
            f"<td>{labels_html}</td>"
            f'<td data-sort="{f.n_holes}">{f.n_holes}</td>'
            f'<td data-sort="{f.n_bends}">{f.n_bends}</td>'
            f"<td>{pmi_dot}</td>"
            f"{duration_td}"
            f"</tr>"
        )
    return "\n".join(rows)


_SORT_JS = """
(function () {
  var input = document.getElementById('corpus-filter');
  var counter = document.getElementById('corpus-count');
  var table = document.getElementById('corpus-table');
  if (!table) return;
  var rows = Array.prototype.slice.call(table.tBodies[0].rows);
  var total = rows.length;

  function applyFilter() {
    if (!input) return;
    var q = input.value.toLowerCase().trim();
    var shown = 0;
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var match = q === '' || r.textContent.toLowerCase().indexOf(q) !== -1;
      r.style.display = match ? '' : 'none';
      if (match) shown++;
    }
    if (counter) counter.textContent = 'showing ' + shown + ' of ' + total;
  }
  if (input) input.addEventListener('input', applyFilter);

  var headers = table.tHead.rows[0].cells;
  var sortState = { col: -1, dir: 1 };
  for (var i = 0; i < headers.length; i++) {
    (function (idx) {
      headers[idx].addEventListener('click', function () {
        var dir = sortState.col === idx ? -sortState.dir : 1;
        sortState = { col: idx, dir: dir };
        var sorted = rows.slice().sort(function (a, b) {
          var av = a.cells[idx].getAttribute('data-sort');
          var bv = b.cells[idx].getAttribute('data-sort');
          if (av === null) av = a.cells[idx].textContent.trim();
          if (bv === null) bv = b.cells[idx].textContent.trim();
          var an = parseFloat(av), bn = parseFloat(bv);
          if (!isNaN(an) && !isNaN(bn)) return (an - bn) * dir;
          return av.localeCompare(bv) * dir;
        });
        var tbody = table.tBodies[0];
        for (var k = 0; k < sorted.length; k++) tbody.appendChild(sorted[k]);
        for (var k = 0; k < headers.length; k++) {
          headers[k].classList.remove('sort-asc', 'sort-desc');
        }
        headers[idx].classList.add(dir === 1 ? 'sort-asc' : 'sort-desc');
      });
    })(i);
  }
})();
"""


def render_html_string(
    report: CorpusReport,
    *,
    link_mode: bool | None = None,
) -> str:
    """Render the corpus report as a self-contained HTML document string.

    The HTML embeds its own CSS + a small JS for column sort and a search
    filter; no external assets are referenced. Returning a string (rather than
    only writing a file) lets :mod:`manufacturing_pipeline.serve_corpus` serve
    the report straight from memory.

    Parameters
    ----------
    link_mode:
        When True, hyperlink each row's path into ``/file/<safe_name>/`` so the
        report can be served through :mod:`manufacturing_pipeline.serve_corpus`.
        When False, the report stays self-contained for emailing.
        When None (default), link mode is auto-detected: turned on iff at
        least one file record has a non-empty ``safe_name``.
    """
    if link_mode is None:
        link_mode = any(bool(f.safe_name) for f in report.files)

    # The geometry-preview column appears only when at least one file carries a
    # rendered thumbnail, so a corpus run without --write-outputs stays compact.
    show_thumbnails = any(bool(f.thumbnail_svg) for f in report.files)

    # Wall-time / mean-time carry information only for a live validate-corpus
    # run. When the report is rebuilt from stored manifests there is no
    # timing, so those KPIs and the Duration column are dropped rather than
    # shown as a misleading 0.
    has_timing = report.total_duration_s > 0.0
    avg_duration = report.total_duration_s / report.total_files if report.total_files else 0.0

    link_note = (
        '<p class="muted link-note">Click any row to drill into the per-file ' "trace browser.</p>"
        if link_mode
        else ""
    )
    timing_kpis = (
        '    <div class="kpi"><div class="k">Wall time</div>'
        f'<div class="v">{report.total_duration_s:.1f}s</div></div>\n'
        '    <div class="kpi"><div class="k">Mean / file</div>'
        f'<div class="v">{avg_duration:.2f}s</div></div>\n'
        if has_timing
        else ""
    )
    duration_th = "        <th>Duration</th>\n" if has_timing else ""
    preview_th = "        <th>Preview</th>\n" if show_thumbnails else ""
    style = _shared_css() + _CORPUS_EXTRA_CSS

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>STEP corpus validation</title>
<style>{style}</style>
</head>
<body>
<header class="corpus-header">
  <h1>STEP corpus validation</h1>
  <p class="lede">Feature detection, sheet-metal unfold and bill-of-materials
  classification run across a corpus of STEP assemblies — every file gets a
  verdict, every part a label, hole count and bend count.</p>
  {link_note}
</header>

<div class="card">
  <div class="kpi-row">
    <div class="kpi"><div class="k">Files</div><div class="v">{report.total_files}</div></div>
    <div class="kpi"><div class="k">OK</div><div class="v" style="color:#1f5e1f">{report.ok_count}</div></div>
    <div class="kpi"><div class="k">Failed</div><div class="v" style="color:#821818">{report.failed_count}</div></div>
    <div class="kpi"><div class="k">Parts</div><div class="v">{report.parts_total}</div></div>
    <div class="kpi"><div class="k">Size</div><div class="v">{report.total_size_mb:.1f} MB</div></div>
{timing_kpis}  </div>
</div>

<h2>Label distribution</h2>
<div class="card">
  {_label_distribution_html(report.label_distribution)}
</div>

<h2>Anomalies ({len(report.anomalies)})</h2>
<div class="card">
  {_render_anomalies_html(report)}
</div>

<h2>Files ({report.total_files})</h2>
<div class="card">
  <div class="toolbar">
    <input type="search" id="corpus-filter" placeholder="filter files..."
           aria-label="filter files" autocomplete="off">
    <span class="count" id="corpus-count">showing {report.total_files} of {report.total_files}</span>
  </div>
  <table id="corpus-table">
    <thead>
      <tr>
{preview_th}        <th>Relative path</th>
        <th>Size</th>
        <th>Parts</th>
        <th>Labels</th>
        <th>Holes</th>
        <th>Bends</th>
        <th>PMI</th>
{duration_th}      </tr>
    </thead>
    <tbody>
{_render_file_rows(report.files, link_mode=link_mode, show_duration=has_timing, show_thumbnails=show_thumbnails)}
    </tbody>
  </table>
</div>

<script>{_SORT_JS}</script>
</body>
</html>
"""


def render_html_report(
    report: CorpusReport,
    out_path: Path,
    *,
    link_mode: bool | None = None,
) -> Path:
    """Render the corpus report as a styled, self-contained HTML page on disk.

    Thin wrapper over :func:`render_html_string`; see it for ``link_mode``.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html_string(report, link_mode=link_mode), encoding="utf-8")
    return out_path


def render_markdown_report(report: CorpusReport, out_path: Path) -> Path:
    """Render the same data as Markdown."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append("# STEP corpus validation\n")
    lines.append(f"- Corpus: `{report.root}`")
    lines.append(f"- Files: **{report.total_files}**")
    lines.append(f"- OK: **{report.ok_count}**, Failed: **{report.failed_count}**")
    lines.append(f"- Parts total: {report.parts_total}")
    lines.append(f"- Size: {report.total_size_mb:.1f} MB")
    lines.append(f"- Wall time: {report.total_duration_s:.1f}s")
    if report.total_files:
        avg = report.total_duration_s / report.total_files
        lines.append(f"- Mean / file: {avg:.2f}s")
    lines.append("")

    lines.append("## Label distribution\n")
    if report.label_distribution:
        for label, count in sorted(report.label_distribution.items(), key=lambda kv: -kv[1]):
            lines.append(f"- `{label}`: {count}")
    else:
        lines.append("_no parts classified_")
    lines.append("")

    lines.append(f"## Anomalies ({len(report.anomalies)})\n")
    if report.anomalies:
        for line in report.anomalies:
            lines.append(f"- {line}")
    else:
        lines.append("_no anomalies detected_")
    lines.append("")

    lines.append(f"## Files ({report.total_files})\n")
    lines.append("| Path | Size | Parts | Labels | Holes | Bends | PMI | Duration | Status |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for f in report.files:
        labels = ", ".join(f"{k}={v}" for k, v in sorted(f.labels.items())) or "-"
        pmi = (
            "semantic"
            if f.pmi_has_semantic
            else (str(f.pmi_n_annotations) if f.pmi_n_annotations else "-")
        )
        status = "ok" if f.ok and not f.warnings else ("warn" if f.ok else "fail")
        lines.append(
            f"| `{f.relative_path}` | {_format_size_mb(f.size_bytes)} | {f.parts} | "
            f"{labels} | {f.n_holes} | {f.n_bends} | {pmi} | {f.duration_s:.2f}s | {status} |"
        )

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


__all__ = [
    "CorpusFile",
    "CorpusReport",
    "validate_corpus",
    "render_html_report",
    "render_html_string",
    "render_markdown_report",
]
