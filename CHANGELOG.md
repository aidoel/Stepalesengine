# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - 2026-05-22

### Added

- **Web** (`web/`, `serve_corpus.py`): GLB meshes and SVG projections are
  cached per app instead of recomputed from the STEP file on every request;
  `serve_corpus` renders the corpus index via a new `render_html_string()`
  and caches it, dropping a per-request temp-file round trip. The corpus
  report now shares the per-file viewer's `base.css` theme, carries viewport
  meta on every page, and gains corpus-overview / part-list navigation links
  and a landing header.
- **Geometry** (`geometry/unfold_probe.py`): the flat-pattern unfold bridges a
  Z-section's split bend graph - `_pair_opposite_faces` pairs the two surfaces
  of each flat segment, both unfold BFS loops bridge an unreached component
  through a paired face (only when it reaches a not-yet-covered segment, so
  single-skin parts are unchanged), and the unfold rotation targets the parent
  face's flattened normal instead of the global `+n_base`. A Z-section now
  develops to a complete flat blank instead of dropping its far flange.
- **Parser** (`manufacturing_pipeline/parsing/`): defensive Part 21 tokenizer
  (encoding cascade, X-encoded strings, doubled-quote escapes, balanced-paren
  argument splitting); six-strategy cascade (NAUO, PRODUCT_DEFINITION, PRODUCT,
  B-rep names, header, comments); Dutch standard-label vocabulary; optional
  OCAF/XCAF fallback for shape labels.
- **Geometry** (`manufacturing_pipeline/geometry/`): single-pass
  `FeatureExtractor` producing `ManufacturingFeatures`; `HoleAnalyzer` with
  co-axial grouping and through/blind detection; cross-section slicing with
  rotation/reflection-invariant shape hash, fillet collapse, family signature
  derivation; `ProfileShapeMatcher` consuming YAML catalogues for I/U/T/Z/L/
  RHS/SHS/CHS; `UnfoldProbe` with bend-graph BFS, BA accumulation, and
  flat-pattern projection.
- **Classification** (`manufacturing_pipeline/classification/`): additive
  per-class `ScoreClassifier` with margin gate, softmax calibration, decision
  trace, and four tiebreakers (`unfold`, `cross_section`, `profile_match`,
  reserved `material_spec`).
- **Assembly** (`manufacturing_pipeline/assembly/`): tree construction with
  cycle/dangling-reference tolerance; leaf-to-solid matcher with five
  strategies (1to1, ordered, OCAF, by_name, unmatched_solid).
- **IO** (`manufacturing_pipeline/io/`): DXF flat-pattern writer (ezdxf,
  LWPOLYLINE/CIRCLE/LINE on layer schema); XML assembly manifest writer
  with round-trippable reader; multi-page PDF shop-drawing writer (reportlab)
  with title block, bend table, and hole table.
- **Pipeline** (`manufacturing_pipeline/pipeline/`): `analyze` orchestrator
  parse -> load -> match -> probe -> classify -> emit; `diff_step_files` for
  before/after BOM comparison with configurable matching key and tolerance.
- **Cache** (`manufacturing_pipeline/cache/`): atomic disk-backed cache keyed
  on (path, mtime, size, model_version) storing manifest XML plus generated
  DXFs for byte-identical replay.
- **Web** (`manufacturing_pipeline/web/`): FastAPI server with DXF-to-SVG
  preview endpoint and static result browser.
- **Telemetry** (`manufacturing_pipeline/telemetry.py`): emit hook for
  pipeline events (start/end/per-part) routed through stdlib logging with
  optional JSON sink.
- **Calibration** (`manufacturing_pipeline/calibration/`): grid-sweep over
  scorer weights, margin/confidence thresholds, and softmax temperature
  against a held-out corpus with cost-weighted confusion matrices.
- **Config**: every tunable threshold consolidated in
  `manufacturing_pipeline/config/classification_variables.py` as the single
  source of truth (Phase 10).

### Changed

- **Geometry** (`geometry/unfold_probe.py`): `n_bends` now counts distinct
  bend hinge lines (`_count_physical_bends`) rather than bends crossed by the
  unfold BFS, which undercounted a Z-section whose opposite-folding flange
  sits off the base face's bend-graph component.
- Cleanup pass (Phase 10): pulled fillet/circularity/symmetry/sheet-pair
  thresholds out of `geometry/cross_section.py` and `geometry/unfold_probe.py`
  into `config/classification_variables.py`; promoted assembly-matcher
  confidence levels and mesh-deflection knobs to the same module.

### Removed

- Phase 10 cleanup: dead `NotImplementedError("Phase 6")` stub in
  `tiebreakers.material_spec_tiebreaker` (now a documented no-op);
  several unused imports (`tempfile`, `defaultdict`, duplicated `BRep_Tool`
  imports, `Iterable`, `os`, `PackageNotFoundError`, `gp_Ax3`, `Path`,
  `TDF_Attribute`).

### Fixed

- **Web** (`web/`, `serve_corpus.py`): per-file viewer templates use Flask
  `url_for()` instead of emitting absolute URLs (`/glb/...`, `/dxf/...`),
  which 404'd under the `serve-corpus` `/file/<name>/` mount - the 3D viewer,
  technical drawings and downloads were all broken on the deployed corpus
  viewer.
- **Web** (`web/templates/detail.html.jinja`): the `'%.3f'` number format is
  guarded against string-valued `Contribution.value` (cross-term features
  render as tuples/booleans); the part-detail page used to 500 on those parts.
- **Web** (`validate.py`): `render_html_report` no longer leaks the server
  filesystem path into the report title/body; the always-zero "Duration"
  column and wall-time KPIs are dropped when the report is rebuilt from
  stored manifests (no timing data there).
- **Web** (`validate.py`): `/diff?other=` is confined to the corpus report
  root (`create_app` gained a `diff_root` argument), so a public deployment
  cannot be coaxed into reading arbitrary server files.
