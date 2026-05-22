"""Static-site generator for the stepalesengine corpus viewer.

The Flask app in :mod:`manufacturing_pipeline.web.server` renders GLB meshes and
SVG drawings on demand, which needs OpenCascade running at request time. Netlify
(and any other static host) cannot run OpenCascade, so this module runs the
geometry pipeline once -- ahead of deployment -- and freezes every artifact to
disk:

* ``models/<slug>-folded.glb``     tessellated 3D solid, as modelled in CAD
* ``models/<slug>-unfolded.glb``   the flat sheet-metal blank, extruded
* ``drawings/<slug>-{iso,top,front}.svg``  hidden-line technical projections
* ``drawings/<slug>-flat.svg``     the flat-pattern DXF rendered to SVG
* ``dxf/<slug>.dxf``               the laser-ready flat pattern
* ``steps/<file>``                 the original STEP input, for download
* ``parts/<slug>.html`` + ``index.html``  a standalone, browsable site

The output directory is self-contained: open ``index.html`` over HTTP and the
3D viewers, drawings and downloads all resolve through relative paths. Publish
the directory as-is on Netlify (see ``netlify.toml``).

CLI::

    python -m manufacturing_pipeline.web.static_site corpus/ --out site/
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("stepalesengine.static_site")

_TPL_DIR = Path(__file__).parent / "static_templates"
_VIEWS = ("iso", "top", "front")

# Index grouping: every classification label the engine can emit, in the order
# the corpus page presents them.
_LABEL_ORDER = ("plaat", "profiel", "anders", "uncertain")
_LABEL_TITLES = {
    "plaat": "Plaat — plate & sheet metal",
    "profiel": "Profiel — structural profile",
    "anders": "Anders — other / machined",
    "uncertain": "Uncertain",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def slugify(text: str) -> str:
    """Lowercase, hyphenated, filename/URL-safe slug. Never empty."""
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return s or "part"


def _jsonable(value: Any) -> Any:
    """Coerce a probe flag value into something ``json.dumps`` accepts."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _flat_pattern_from_raw(raw: dict, part_name: str):
    """Build a :class:`FlatPattern` from ``UnfoldProbe.compute_flat_pattern``.

    Mirrors ``analyze_assembly._build_flat_pattern`` / the unfolded-GLB route in
    :mod:`server` -- the probe returns either a single polyline or a list of
    polylines for ``outer_contour``.
    """
    from manufacturing_pipeline.io.dxf_writer import FlatPattern

    if not raw:
        return None
    outer = raw.get("outer_contour") or []
    if not outer:
        return None
    if isinstance(outer[0], tuple) and len(outer[0]) == 2:
        outer_contours = [list(outer)]
    else:
        outer_contours = [list(p) for p in outer]

    return FlatPattern(
        outer_contour=outer_contours,
        holes=[list(h) for h in (raw.get("holes") or [])],
        bend_lines=list(raw.get("bend_lines") or []),
        thickness=float(raw.get("thickness") or 1.0) or 1.0,
        bbox=tuple(raw.get("bbox") or (0.0, 0.0, 0.0, 0.0)),
        units="mm",
        part_name=part_name,
    )


def _bend_table(raw: dict) -> list[dict]:
    """Turn the probe's ``bend_lines`` into a renderable bend table.

    Each ``bend_lines`` entry is ``(p1, p2, meta)`` with ``meta`` carrying the
    bend ``angle`` (deg), inner ``radius`` (mm) and fold ``direction``.
    """
    bends: list[dict] = []
    for i, item in enumerate(raw.get("bend_lines") or [], start=1):
        try:
            p1, p2, meta = item
        except (ValueError, TypeError):
            continue
        meta = meta or {}
        length = ((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2) ** 0.5
        bends.append(
            {
                "index": i,
                "angle": round(float(meta.get("angle", 0.0) or 0.0), 1),
                "radius": round(float(meta.get("radius", 0.0) or 0.0), 2),
                "direction": str(meta.get("direction", "up") or "up"),
                "length": round(length, 1),
            }
        )
    return bends


def _record_from_entry(entry, slug: str, step_path: Path, origin: str) -> dict:
    """Project a :class:`PartManifestEntry` into a flat, JSON-serialisable dict.

    The same dict drives both the Jinja templates and ``data/corpus.json``.
    """
    clf = entry.classification
    trace = getattr(clf, "trace", None)
    scores: dict = {}
    if trace is not None:
        scores = dict(trace.scores or {}) or dict(trace.probabilities or {})
    scores_sorted = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    unfold = entry.unfold
    feats = entry.features
    bbox = list(feats.bbox_dims_sorted) if feats else [0.0, 0.0, 0.0]

    rec: dict[str, Any] = {
        "slug": slug,
        "product_id": entry.part.product_id,
        "name": entry.part.name or entry.part.product_id,
        "description": entry.part.description or "",
        "source_file": step_path.name,
        "origin": origin,
        "label": clf.label,
        "confidence": round(float(clf.confidence), 4),
        "scores": [[k, round(float(v), 4)] for k, v in scores_sorted],
        "margin": round(float(getattr(trace, "margin", 0.0) or 0.0), 4),
        "quantity": int(entry.quantity),
        "unfold_status": unfold.status.value if unfold else None,
        "n_bends": int(unfold.n_bends) if unfold else 0,
        "flat_area": round(float(unfold.flat_area), 1) if unfold else 0.0,
        "thickness": round(float(unfold.thickness_mean), 2) if unfold else 0.0,
        "thickness_cv": round(float(unfold.thickness_cv), 4) if unfold else 0.0,
        "unfold_reason": unfold.reason if unfold else "",
        "unfold_flags": (
            {k: _jsonable(v) for k, v in (unfold.flags or {}).items()} if unfold else {}
        ),
        "bends": [],
        "features": {
            "volume": round(float(feats.volume), 1) if feats else 0.0,
            "surface_area": round(float(feats.surface_area), 1) if feats else 0.0,
            "bbox": [round(float(x), 1) for x in bbox],
            "aspect_ratio": round(float(feats.aspect_ratio), 2) if feats else 0.0,
            "thickness_ratio": round(float(feats.thickness_ratio), 3) if feats else 0.0,
            "source": feats.source if feats else "",
            "material": feats.material_guess if feats else "",
            "hollow": bool(feats.is_hollow) if feats else False,
        },
        "holes": {
            "count": int(entry.holes.hole_count) if entry.holes else 0,
            "diameters": [
                round(float(d), 2) for d in (entry.holes.diameters if entry.holes else [])
            ],
        },
        "profile_match": None,
        "pmi": None,
        "assets": {
            "folded_glb": None,
            "unfolded_glb": None,
            "iso_svg": None,
            "top_svg": None,
            "front_svg": None,
            "flat_svg": None,
            "dxf": None,
            "step": f"steps/{step_path.name}",
        },
    }

    pm = entry.profile_match
    if pm is not None:
        rec["profile_match"] = {
            "family": pm.family,
            "standard": pm.standard,
            "designation": pm.designation,
            "residual_mm": round(float(pm.residual_mm), 3),
            "confidence": round(float(pm.confidence), 3),
        }

    pmi = entry.pmi
    if pmi is not None and (
        getattr(pmi, "has_semantic", False) or getattr(pmi, "n_annotations", 0)
    ):
        rec["pmi"] = {
            "n_annotations": int(getattr(pmi, "n_annotations", 0) or 0),
            "has_semantic": bool(getattr(pmi, "has_semantic", False)),
            "n_tolerances": len(getattr(pmi, "tolerances", []) or []),
            "n_dimensions": len(getattr(pmi, "dimensions", []) or []),
        }
    return rec


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------


def _process_file(
    step_path: Path,
    origin: str,
    out: Path,
    staging: Path,
    deflection: float,
    seen_slugs: set[str],
) -> list[dict]:
    """Analyse one STEP file and freeze every per-part artifact to ``out``."""
    from manufacturing_pipeline.geometry.unfold_probe import UnfoldProbe
    from manufacturing_pipeline.pipeline.analyze_assembly import AnalyzeOptions, analyze
    from manufacturing_pipeline.web.dxf_to_svg import dxf_to_svg
    from manufacturing_pipeline.web.step_to_glb import folded_glb, unfolded_glb
    from manufacturing_pipeline.web.step_to_svg import find_solid_for_part, render_part_views

    file_stem = slugify(step_path.stem)
    file_staging = staging / file_stem
    records: list[dict] = []

    try:
        result = analyze(
            step_path,
            AnalyzeOptions(out_dir=file_staging, write_dxf=True, write_xml=True),
        )
    except Exception as exc:  # noqa: BLE001 - never abort the whole corpus
        logger.error("  analyze failed for %s: %s", step_path.name, exc)
        return records

    shutil.copy2(step_path, out / "steps" / step_path.name)
    if result.manifest_path and Path(result.manifest_path).is_file():
        shutil.copy2(result.manifest_path, out / "data" / f"{file_stem}.manifest.xml")

    parts = result.manifest.parts
    for idx, entry in enumerate(parts):
        pid = entry.part.product_id
        base = file_stem if len(parts) == 1 else f"{file_stem}-{idx + 1}"
        slug = base
        n = 2
        while slug in seen_slugs:
            slug = f"{base}-{n}"
            n += 1
        seen_slugs.add(slug)

        rec = _record_from_entry(entry, slug, step_path, origin)

        # 1. Folded GLB -- the original CAD solid, tessellated.
        try:
            data = folded_glb(step_path, pid, deflection=deflection)
            if data:
                (out / "models" / f"{slug}-folded.glb").write_bytes(data)
                rec["assets"]["folded_glb"] = f"models/{slug}-folded.glb"
        except Exception as exc:  # noqa: BLE001
            logger.warning("  folded GLB failed for %s: %s", slug, exc)

        # 2. Hidden-line technical projections.
        try:
            svgs = render_part_views(step_path, pid, views=_VIEWS, width=640, height=480)
            for view, svg in svgs.items():
                (out / "drawings" / f"{slug}-{view}.svg").write_text(svg, encoding="utf-8")
                rec["assets"][f"{view}_svg"] = f"drawings/{slug}-{view}.svg"
        except Exception as exc:  # noqa: BLE001
            logger.warning("  SVG views failed for %s: %s", slug, exc)

        # 3. Unfolded GLB + bend table -- only when the unfold genuinely
        #    succeeded; recomputed from the source solid so we get real 2D
        #    polygons in mm (the cached DXF would round-trip them).
        if rec["unfold_status"] in ("success", "partial"):
            try:
                solid = find_solid_for_part(step_path, pid)
                if solid is not None:
                    raw = UnfoldProbe().compute_flat_pattern(solid)
                    if raw and raw.get("outer_contour"):
                        rec["bends"] = _bend_table(raw)
                        pattern = _flat_pattern_from_raw(raw, entry.part.name or pid)
                        if pattern is not None:
                            uglb = unfolded_glb(pattern)
                            if uglb:
                                (out / "models" / f"{slug}-unfolded.glb").write_bytes(uglb)
                                rec["assets"]["unfolded_glb"] = f"models/{slug}-unfolded.glb"
            except Exception as exc:  # noqa: BLE001
                logger.warning("  unfold artifacts failed for %s: %s", slug, exc)

        # 4. Flat-pattern DXF + its SVG render.
        if entry.flat_dxf_path:
            dxf_src = file_staging / entry.flat_dxf_path
            if dxf_src.is_file():
                shutil.copy2(dxf_src, out / "dxf" / f"{slug}.dxf")
                rec["assets"]["dxf"] = f"dxf/{slug}.dxf"
                try:
                    flat_svg = dxf_to_svg(dxf_src, width=640, height=480)
                    (out / "drawings" / f"{slug}-flat.svg").write_text(flat_svg, encoding="utf-8")
                    rec["assets"]["flat_svg"] = f"drawings/{slug}-flat.svg"
                except Exception as exc:  # noqa: BLE001
                    logger.warning("  DXF->SVG failed for %s: %s", slug, exc)

        records.append(rec)
        logger.info(
            "  %-26s %-9s unfold=%-8s bends=%d %s",
            rec["name"][:26],
            rec["label"],
            rec["unfold_status"] or "-",
            rec["n_bends"],
            "3D" if rec["assets"]["folded_glb"] else "(no mesh)",
        )

    return records


# ---------------------------------------------------------------------------
# Site rendering
# ---------------------------------------------------------------------------


def _corpus_stats(records: list[dict]) -> dict:
    by_label: dict[str, int] = {}
    for r in records:
        by_label[r["label"]] = by_label.get(r["label"], 0) + 1
    return {
        "n_parts": len(records),
        "n_files": len({r["source_file"] for r in records}),
        "by_label": by_label,
        "n_unfoldable": sum(1 for r in records if r["unfold_status"] == "success"),
        "total_bends": sum(r["n_bends"] for r in records),
    }


def _grouped(records: list[dict]) -> list[dict]:
    """Group part records by classification label for the index page."""
    groups: list[dict] = []
    for label in _LABEL_ORDER:
        members = [r for r in records if r["label"] == label]
        members.sort(key=lambda r: (r["origin"] != "synthetic", r["source_file"]))
        if members:
            groups.append({"label": label, "title": _LABEL_TITLES[label], "parts": members})
    return groups


def _render(out: Path, records: list[dict], title: str) -> None:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(_TPL_DIR)),
        autoescape=select_autoescape(["html", "jinja"]),
    )
    shutil.copy2(_TPL_DIR / "style.css", out / "assets" / "style.css")

    stats = _corpus_stats(records)
    featured = next(
        (r for r in records if r["assets"]["unfolded_glb"]),
        records[0] if records else None,
    )

    index_tpl = env.get_template("index.html.jinja")
    (out / "index.html").write_text(
        index_tpl.render(
            title=title,
            groups=_grouped(records),
            stats=stats,
            featured=featured,
            root="",
        ),
        encoding="utf-8",
    )

    part_tpl = env.get_template("part.html.jinja")
    for rec in records:
        (out / "parts" / f"{rec['slug']}.html").write_text(
            part_tpl.render(title=title, p=rec, root="../"),
            encoding="utf-8",
        )

    (out / "data" / "corpus.json").write_text(
        json.dumps({"title": title, "stats": stats, "parts": records}, indent=2),
        encoding="utf-8",
    )


def build_static_site(
    step_files: list[tuple[Path, str]],
    out_dir: str | Path,
    *,
    title: str = "stepalesengine — 3D corpus & unfold viewer",
    deflection: float = 0.5,
) -> list[dict]:
    """Render a complete static site for ``step_files`` into ``out_dir``.

    ``step_files`` is a list of ``(path, origin)`` pairs where ``origin`` is
    ``"synthetic"`` or ``"real"``. The output directory is wiped and rebuilt.
    Returns the list of per-part record dicts.
    """
    out = Path(out_dir).resolve()
    if out.exists():
        shutil.rmtree(out)
    for sub in ("parts", "models", "drawings", "dxf", "steps", "data", "assets"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix="stepalesengine-build-"))
    records: list[dict] = []
    seen: set[str] = set()
    try:
        for step_path, origin in step_files:
            logger.info("processing %s (%s)", step_path.name, origin)
            records.extend(_process_file(step_path, origin, out, staging, deflection, seen))
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if not records:
        raise RuntimeError("no parts produced -- corpus yielded zero analysable parts")

    _render(out, records, title)
    return records


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m manufacturing_pipeline.web.static_site corpus/ --out site/``."""
    parser = argparse.ArgumentParser(
        prog="stepalesengine-static-site",
        description="Pre-render a stepalesengine STEP corpus into a static site.",
    )
    parser.add_argument(
        "corpus", type=Path, help="directory of STEP files (or a single .step/.stp file)"
    )
    parser.add_argument(
        "--out", type=Path, default=Path("site"), help="output directory (default: ./site)"
    )
    parser.add_argument("--title", default="stepalesengine — 3D corpus & unfold viewer")
    parser.add_argument(
        "--deflection",
        type=float,
        default=0.5,
        help="mesh tessellation deflection in mm; lower = finer (default: 0.5)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # ezdxf logs every dictionary it creates at INFO; keep the build readable.
    logging.getLogger("ezdxf").setLevel(logging.WARNING)

    corpus: Path = args.corpus
    if corpus.is_dir():
        files = sorted(
            (p for p in corpus.iterdir() if p.suffix.lower() in (".step", ".stp")),
            key=lambda p: (not p.stem.startswith("synthetic"), p.name),
        )
    elif corpus.is_file():
        files = [corpus]
    else:
        print(f"corpus path not found: {corpus}", file=sys.stderr)
        return 2

    if not files:
        print(f"no .step/.stp files found in {corpus}", file=sys.stderr)
        return 2

    step_files = [(p, "synthetic" if p.stem.startswith("synthetic") else "real") for p in files]
    records = build_static_site(step_files, args.out, title=args.title, deflection=args.deflection)
    stats = _corpus_stats(records)
    print(
        f"\nbuilt {stats['n_parts']} part page(s) from {stats['n_files']} file(s) "
        f"-> {args.out}\n  labels: {stats['by_label']}  "
        f"unfoldable: {stats['n_unfoldable']}  bends: {stats['total_bends']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_static_site", "slugify", "main"]
