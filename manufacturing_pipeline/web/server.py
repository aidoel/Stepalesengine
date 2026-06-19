"""Flask-based local web UI for browsing a stepalesengine manifest.

This module exposes :func:`create_app`, which builds a Flask app rooted at a
specific ``manifest.xml`` (parts directory defaulting to that file's parent),
and :func:`main`, the ``stepalesengine-web`` CLI entry point.

Routes
------
``/``                  Index BOM table with classification labels.
``/part/<product_id>`` Per-part detail page with decision trace + DXF preview.
``/dxf/<filename>``    Raw DXF download (``application/dxf``).
``/dxf-svg/<filename>``Inline SVG render of the DXF flat pattern.
``/dxfs.zip``          Bulk ZIP of every per-part DXF in the manifest.
``/diff``              Compare this manifest against another (?other=<path>).
``/api/manifest``      JSON dump of the parsed manifest.

Templates live as separate files under ``manufacturing_pipeline/web/templates/``
and are read once at import time via
:func:`manufacturing_pipeline.web.templates.load`, then rendered through
``render_template_string``.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import zipfile
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, render_template_string, request, send_file

from manufacturing_pipeline.io.xml_writer import read_xml
from manufacturing_pipeline.manifest import AssemblyManifest, PartManifestEntry
from manufacturing_pipeline.pipeline.diff import (
    diff_assemblies,
    render_diff_text,
)
from manufacturing_pipeline.web.dxf_to_svg import dxf_to_svg
from manufacturing_pipeline.web.step_to_glb import folded_glb, unfolded_glb
from manufacturing_pipeline.web.step_to_svg import render_part_views
from manufacturing_pipeline.web.templates import load as _load_template

_logger = logging.getLogger("stepalesengine.web")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_BASE_CSS = _load_template("base.css")
_INDEX_TPL = _load_template("index.html.jinja")
_DIFF_TPL = _load_template("diff.html.jinja")
_DETAIL_TPL = _load_template("detail.html.jinja")


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    raise TypeError(f"object of type {type(obj).__name__} is not JSON serialisable")


def _manifest_to_dict(manifest: AssemblyManifest) -> dict[str, Any]:
    """Convert the manifest to a plain dict suitable for ``jsonify``."""
    return json.loads(json.dumps(asdict(manifest), default=_json_default))


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    manifest_path: str | Path,
    *,
    out_dir: str | Path | None = None,
    debug: bool = False,
    diff_root: str | Path | None = None,
) -> Flask:
    """Build a Flask app rooted at the given ``manifest.xml``.

    ``out_dir`` defaults to the manifest's parent directory; DXF files are
    served relative to ``out_dir / 'parts'`` matching the layout produced by
    :mod:`manufacturing_pipeline.pipeline.analyze_assembly`.

    ``diff_root`` confines the ``/diff?other=`` manifest path: when set, only
    manifests resolving inside that directory may be compared. The corpus
    viewer passes its report root so a public deployment cannot be coaxed
    into reading arbitrary server files. ``None`` (the standalone local
    default) leaves the path unconstrained.
    """
    manifest_path = Path(manifest_path).resolve()
    out_dir_path = Path(out_dir).resolve() if out_dir is not None else manifest_path.parent
    diff_root_path = Path(diff_root).resolve() if diff_root is not None else None
    manifest = read_xml(manifest_path)

    app = Flask(__name__)
    app.config["MANIFEST"] = manifest
    app.config["MANIFEST_PATH"] = manifest_path
    app.config["OUT_DIR"] = out_dir_path
    app.config["DEBUG"] = debug

    # Index parts by product_id for O(1) lookup.
    by_id: dict[str, PartManifestEntry] = {e.part.product_id: e for e in manifest.parts}

    # Per-app render caches. The manifest and its source STEP file are
    # immutable for the lifetime of the app, so each GLB mesh / SVG projection
    # — an expensive OCP recompute — is memoised on first request.
    _glb_cache: dict[str, bytes] = {}
    _svg_cache: dict[str, str] = {}

    # ----- Jinja filters
    @app.template_filter("basename")
    def _basename(value: str) -> str:
        return Path(str(value)).name if value else ""

    @app.template_filter("items")
    def _items(value: Any) -> list:
        """Expose ``dict.items()`` to templates as a list of ``[key, value]``
        pairs so we can ``map('join', '=')`` them.
        """
        if isinstance(value, dict):
            return [[str(k), str(v)] for k, v in value.items()]
        return []

    # ----- Routes
    @app.get("/")
    def index() -> str:
        return render_template_string(_INDEX_TPL, manifest=manifest, css=_BASE_CSS)

    @app.get("/part/<path:product_id>")
    def part_detail(product_id: str) -> str:
        entry = by_id.get(product_id)
        if entry is None:
            abort(404, description=f"unknown product_id: {product_id!r}")

        scores = dict(entry.classification.trace.scores)
        if not scores:
            # Fall back to probabilities if scores are missing.
            scores = dict(entry.classification.trace.probabilities)
        scores_sorted = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

        top_contributions = sorted(
            entry.classification.trace.contributions,
            key=lambda c: abs(c.delta),
            reverse=True,
        )[:5]

        trace_json = json.dumps(asdict(entry.classification.trace), indent=2, default=_json_default)

        dxf_filename: str | None = Path(entry.flat_dxf_path).name if entry.flat_dxf_path else None

        strategy_ops_sorted = (
            sorted(entry.strategy.operations, key=lambda o: (o.priority, o.op))
            if entry.strategy is not None
            else []
        )

        return render_template_string(
            _DETAIL_TPL,
            entry=entry,
            css=_BASE_CSS,
            scores_sorted=scores_sorted,
            top_contributions=top_contributions,
            trace_json=trace_json,
            dxf_filename=dxf_filename,
            strategy_ops_sorted=strategy_ops_sorted,
        )

    @app.get("/dxf/<path:filename>")
    def dxf_raw(filename: str):
        dxf_path = _resolve_dxf(out_dir_path, filename)
        if dxf_path is None:
            abort(404, description=f"dxf not found: {filename!r}")
        return send_file(
            dxf_path,
            mimetype="application/dxf",
            as_attachment=False,
            download_name=dxf_path.name,
        )

    @app.get("/glb/folded/<path:product_id>")
    def glb_folded(product_id: str):
        if product_id not in by_id:
            abort(404, description=f"unknown product_id: {product_id!r}")
        cache_key = f"folded:{product_id}"
        data = _glb_cache.get(cache_key)
        if data is None:
            source = Path(manifest.source_path)
            if not source.is_file():
                abort(404, description=f"source STEP not available: {source}")
            data = folded_glb(source, product_id)
            if data is None:
                abort(500, description=f"failed to mesh {product_id}")
            _glb_cache[cache_key] = data
        return (data, 200, {"Content-Type": "model/gltf-binary"})

    @app.get("/glb/unfolded/<path:product_id>")
    def glb_unfolded(product_id: str):
        entry = by_id.get(product_id)
        if entry is None:
            abort(404, description=f"unknown product_id: {product_id!r}")
        cache_key = f"unfolded:{product_id}"
        cached = _glb_cache.get(cache_key)
        if cached is not None:
            return (cached, 200, {"Content-Type": "model/gltf-binary"})
        # We need the flat pattern; recompute it from the source solid so we
        # don't depend on the cached DXF (we want real 2D polygons in mm).
        source = Path(manifest.source_path)
        if not source.is_file():
            abort(404, description=f"source STEP not available: {source}")
        solid = None
        try:
            from manufacturing_pipeline.geometry.unfold_probe import UnfoldProbe
            from manufacturing_pipeline.io.dxf_writer import FlatPattern
            from manufacturing_pipeline.web.step_to_svg import find_solid_for_part

            solid = find_solid_for_part(source, product_id)
            if solid is None:
                abort(404, description=f"no solid for {product_id!r}")
            raw = UnfoldProbe().compute_flat_pattern(solid)
            if not raw or not raw.get("outer_contour"):
                abort(404, description=f"no unfold available for {product_id!r}")
            outer = raw["outer_contour"]
            if outer and isinstance(outer[0], tuple) and len(outer[0]) == 2:
                outer = [list(outer)]
            else:
                outer = [list(p) for p in outer]
            pattern = FlatPattern(
                outer_contour=outer,
                holes=[list(h) for h in raw.get("holes") or []],
                bend_lines=list(raw.get("bend_lines") or []),
                thickness=float(raw.get("thickness") or 1.0),
                bbox=tuple(raw.get("bbox") or (0, 0, 0, 0)),
                units="mm",
                part_name=entry.part.name,
            )
        except Exception as exc:
            _logger.exception("unfold for glb failed for %s", product_id)
            abort(500, description=f"unfold failed: {exc}")
        data = unfolded_glb(pattern)
        if data is None:
            abort(500, description=f"failed to mesh unfolded {product_id}")
        _glb_cache[cache_key] = data
        return (data, 200, {"Content-Type": "model/gltf-binary"})

    @app.get("/step-svg/<path:product_id>")
    def step_svg(product_id: str):
        if product_id not in by_id:
            abort(404, description=f"unknown product_id: {product_id!r}")
        view = request.args.get("view", "iso")
        cache_key = f"step:{product_id}:{view}"
        svg = _svg_cache.get(cache_key)
        if svg is None:
            source = Path(manifest.source_path)
            if not source.is_file():
                abort(404, description=f"source STEP not available: {source}")
            try:
                svgs = render_part_views(source, product_id, views=(view,), width=600, height=450)
            except Exception as exc:
                _logger.exception("step_to_svg failed for %s view=%s", product_id, view)
                abort(500, description=f"failed to render {product_id}: {exc}")
            svg = svgs.get(view)
            if not svg:
                abort(404, description=f"no projection for {product_id} view={view}")
            _svg_cache[cache_key] = svg
        return (svg, 200, {"Content-Type": "image/svg+xml; charset=utf-8"})

    @app.get("/dxf-svg/<path:filename>")
    def dxf_svg(filename: str):
        dxf_path = _resolve_dxf(out_dir_path, filename)
        if dxf_path is None:
            abort(404, description=f"dxf not found: {filename!r}")
        cache_key = f"dxf:{dxf_path}"
        svg = _svg_cache.get(cache_key)
        if svg is None:
            try:
                svg = dxf_to_svg(dxf_path)
            except Exception as exc:  # surface readable errors during dev
                _logger.exception("dxf_to_svg failed for %s", dxf_path)
                abort(500, description=f"failed to render {filename}: {exc}")
            _svg_cache[cache_key] = svg
        return (svg, 200, {"Content-Type": "image/svg+xml; charset=utf-8"})

    @app.get("/api/manifest")
    def api_manifest():
        return jsonify(_manifest_to_dict(manifest))

    @app.get("/dxfs.zip")
    def dxfs_zip():
        """Return a ZIP of every per-part DXF referenced by the manifest.

        Files are added under ``parts/<basename>`` to mirror the on-disk layout.
        Missing files (referenced in the manifest but not present on disk) are
        silently skipped — the manifest is the source of truth.
        """
        seen: set[str] = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for entry in manifest.parts:
                if not entry.flat_dxf_path:
                    continue
                basename = Path(entry.flat_dxf_path).name
                if not basename or basename in seen:
                    continue
                dxf_path = _resolve_dxf(out_dir_path, basename)
                if dxf_path is None:
                    continue
                seen.add(basename)
                zf.write(dxf_path, arcname=f"parts/{basename}")
        buf.seek(0)
        source_stem = Path(manifest.source_path).stem or "manifest"
        download_name = f"{source_stem}_dxfs.zip"
        return send_file(
            buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name=download_name,
        )

    @app.get("/diff")
    def diff_view():
        other_raw = request.args.get("other", "").strip()
        if not other_raw:
            return render_template_string(
                _DIFF_TPL,
                manifest=manifest,
                manifest_path=str(manifest_path),
                css=_BASE_CSS,
                diff=None,
                diff_text=None,
                sections=[],
                other_path="",
                error=None,
                fmt=_diff_fmt,
            )

        other_path = Path(other_raw).expanduser()
        try:
            resolved = other_path.resolve()
        except OSError as exc:
            return _diff_error(f"invalid path: {exc}", other_raw), 400
        # Path confinement: a public corpus deployment must not be able to
        # read manifests outside its own report tree.
        if diff_root_path is not None:
            try:
                resolved.relative_to(diff_root_path)
            except ValueError:
                return (
                    _diff_error("path is outside the permitted directory", other_raw),
                    403,
                )
        if not resolved.is_file():
            return _diff_error(f"manifest not found: {other_raw}", other_raw), 404
        try:
            other_manifest = read_xml(resolved)
        except Exception as exc:
            _logger.exception("failed to read other manifest: %s", resolved)
            return _diff_error(f"failed to parse manifest: {exc}", other_raw), 400

        diff = diff_assemblies(manifest, other_manifest)
        sections = [
            {"title": "ADDED", "rows": diff.added},
            {"title": "REMOVED", "rows": diff.removed},
            {"title": "CHANGED", "rows": diff.changed},
            {"title": "UNCHANGED", "rows": diff.unchanged},
        ]
        return render_template_string(
            _DIFF_TPL,
            manifest=manifest,
            manifest_path=str(manifest_path),
            css=_BASE_CSS,
            diff=diff,
            diff_text=render_diff_text(diff),
            sections=sections,
            other_path=other_raw,
            error=None,
            fmt=_diff_fmt,
        )

    def _diff_error(message: str, other_raw: str) -> str:
        return render_template_string(
            _DIFF_TPL,
            manifest=manifest,
            manifest_path=str(manifest_path),
            css=_BASE_CSS,
            diff=None,
            diff_text=None,
            sections=[],
            other_path=other_raw,
            error=message,
            fmt=_diff_fmt,
        )

    return app


def _diff_fmt(value: Any) -> str:
    """Render a diff detail tuple element compactly for HTML."""
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3g}"
    return str(value)


def _resolve_dxf(out_dir: Path, filename: str) -> Path | None:
    """Look up a DXF by basename inside ``out_dir`` and ``out_dir/parts``.

    Returns ``None`` if the file does not exist or escapes the out_dir (path
    traversal protection — we only ever match a leaf basename).
    """
    safe = Path(filename).name
    if not safe or safe.startswith("."):
        return None
    candidates = [
        out_dir / "parts" / safe,
        out_dir / safe,
    ]
    for c in candidates:
        try:
            resolved = c.resolve()
        except OSError:
            continue
        try:
            resolved.relative_to(out_dir.resolve())
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI: ``stepalesengine-web /path/to/manifest.xml [--port 5000]``."""
    parser = argparse.ArgumentParser(
        prog="stepalesengine-web",
        description="Local Flask UI for browsing a stepalesengine manifest.xml.",
    )
    parser.add_argument("manifest", type=Path, help="path to manifest.xml")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="root for DXF lookups (default: manifest's parent dir)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    if not args.manifest.is_file():
        print(f"manifest not found: {args.manifest}", file=sys.stderr)
        return 2

    app = create_app(args.manifest, out_dir=args.out_dir, debug=args.debug)
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
    return 0


__all__ = ["create_app", "main"]
