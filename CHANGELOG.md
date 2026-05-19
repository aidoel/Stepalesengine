# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - 2026-05-16

### Added

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
