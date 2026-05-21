# Next steps + architecture state

State as of 2026-05-21. Test suite **519 passing**, **0 ruff violations**, **0 mypy errors** (81 files), ~22.5 k LOC.

## Module status

| Module | Status |
|---|---|
| `parsing/` | stable; six-strategy cascade + OCAF fallback |
| `geometry/` | stable; feature extractor, unfold probe (branching + joggle), hole analyzer, profile matcher |
| `classification/` | stable; score classifier + cross-terms + `FeatureVector` dataclass |
| `assembly/` | stable; NAUO graph + matcher |
| `pmi/` | stable; 16 GD&T tolerance types, AP242 e1/e2/e3 |
| `cam/` | stable; machining strategy probe |
| `io/` | stable; DXF + XML (declarative walker) + PDF |
| `pipeline/` | stable; orchestrator + diff + prefilter + probe registry |
| `web/` | stable; trace browser + 3D viewer + corpus drill-down |
| `cli/` | stable; 13 per-subcommand modules under `cli/` package |
| `cache/`, `batch.py`, `validate.py`, `watch.py`, `calibration/`, `telemetry.py` | stable |

## Previously-outstanding work — now landed

All items from the prior NEXT_STEPS round are implemented and tested:

- **Perf prefilter + identity cache.** `_is_likely_unfoldable` / `_is_likely_profile` / `_solid_signature` / `_SOLID_RESULT_CACHE` in `analyze_assembly.py`, wired into `_process_pair` behind `AnalyzeOptions.prefilter` (default on). 13 tests in `tests/pipeline/test_prefilter.py`. See `PERF_FINDING_803139.md`.
- **mypy.** Configured in `pyproject.toml` (`[tool.mypy]`, Python 3.10 target, strict on `parsing`/`classification`). Currently 0 errors over 81 files. See `MYPY_DEBT.md`.
- **Coverage.** `pytest-cov` in dev extras; current total ~80%. See `COVERAGE.md`.
- **PDF deliverable.** `io/pdf_writer.py` — per-part and assembly PDF.
- **Watch mode.** `manufacturing_pipeline/watch.py` + `stepalesengine watch` subcommand.
- **Scorer weights in YAML.** `load_scorers_from_yaml` + `tests/classification/test_scorers_yaml.py`.
- **`web/server.py` slimmed** to 444 LOC; inline templates extracted to `web/templates.py`.

## Recently landed

### UnfoldProbe rewritten around a sheet model

The probe failed every real sheet-metal part as `cyclic_graph`: rounded corners (vertical cylinders, axis along the sheet normal) were counted as bends, and thickness was measured by an unweighted minimum that latched onto sub-millimetre slivers. `_identify_sheet` now pairs each planar face with its nearest antiparallel partner, clusters the gaps, and takes the area-heaviest cluster as the thickness and its faces as the *flanges*. Bends are flange-to-flange only, with an axis-perpendicular-to-flange-normal test rejecting corner rounds; thickness probing samples real face vertices, not the AABB. A `too_thick` gate (`UNFOLD_MAX_SHEET_THICKNESS_MM`) keeps machined blocks out of the no-bends "flat plate" path. Verified against the AutoPOL `Sheet_*` reference: bend counts, thickness and flat-pattern extent now match. Covered by new rounded-corner / sheet-model tests in `tests/geometry/test_unfold_probe.py` and the real-part corpus `tests/regression/test_sheet_metal_corpus.py`.

### Orchestrator now runs through the probe registry

`_process_pair` no longer instantiates probes inline. It builds one `ProbeContext` and drives `_REGISTRY.run_all` in three stages: `STAGE_PRE` (holes, before the cache lookup), `STAGE_CLASSIFY` (profile + unfold, cached, prefilterable via a `skip` dict), `STAGE_POST` (pmi + cam, after classification). `run_all` gained `stage` / `skip` / `prior` parameters; probes register with a stage. The registry is built once at import, so `ProfileShapeMatcher` loads its YAML tables once instead of per part. Adding a feature probe is now a single `reg.register(...)` line. Covered by `tests/pipeline/test_probes.py` (stage/skip/prior) and the existing prefilter suite.

### `has_bends` scorer feature for bent sheet parts

`FeatureVector` gained a `has_bends` feature (unfold did not fail and `n_bends > 0`) - the strongest sheet-metal signal, since machined/freeform parts do not unfold and a flat plate has no bends. New scorer rules reward `plaat` (+1.2), suppress `anders` (-1.2, overriding the hole-density / pocket rewards a bent part trips) and mildly damp `profiel` (-0.3). Long bent parts that previously fell to `anders`/`uncertain` now classify `plaat`. Verified end-to-end in `tests/regression/test_sheet_metal_corpus.py`.

### Part-name resolution + multi-solid matching fixed

Multi-solid files mislabelled parts (copies took sibling names; CAD feature names like `Cut-Extrude9` leaked in). `step_strategies` now accepts `PRODUCT_DEFINITION_FORMATION` subtypes, fixes NAUO parent/child orientation and multiplicity, skips feature-tree names, and reads the authored `FILE_NAME`. The matcher gained a quantity-aware geometry-clustering strategy.

### `_process_pair` split + mesh-only path tested

`_process_pair` is now a thin coordinator over `_analyse_part` and `_write_part_outputs` (linked by `_PartAnalysis`). The `FeatureExtractor` mesh-only fallback gained a regression test.

## Outstanding work (in priority order)

### PARTIAL unfold collapses to `uncertain`

`feature_vector.py` sets `unfoldable = (status == SUCCESS)`, so a `PARTIAL` unfold (real plates that trip thickness-variation or branching) loses the plaat `unfoldable` boost and drops to `uncertain`. A corpus run found this on every `partial` part. Decide whether `PARTIAL` should count as `unfoldable` (or carry a partial boost); re-pin the classifier baselines in the same change.

### Branched-flange parts fail to unfold

The `Silo 2` assembly has ~24 parts the rewritten bend-graph traversal reports as `failure` where AutoPOL unfolds all of them. Star/branched flange topologies are not yet handled. Investigate the bend-graph traversal on these.

### Hole over-count and assembly over-segmentation

Hole counts run high vs the AutoPOL reference (47 vs 29; 49 vs 37) - the probe counts every circular contour including slots. And the pipeline emits one `<part>` per solid instance where AutoPOL groups identical parts with a quantity, inflating per-assembly counts.

### Calibration needs a larger labelled corpus

`stepalesengine calibrate` + `calibration/sweep.py` exist, but only ~20 folder-labelled STEP files are available (`Zetwerk/`=plaat, `profiel/`=profiel, `samenstelling/`=anders under `Downloads/stepfile/`) - too few for a meaningful sweep. `sweep.py` also ignores the CSV `product_id` column, so multi-part assemblies cannot be labelled per-part. Needs more labelled single-part files and a `sweep.py` fix before a real sweep.

### Tapered/cut profiles misclassify

Profiles with `cross_section_constant=false` (tapered, cut, non-uniform) miss the `profiel` scorer's boost and fall to `anders`/`uncertain` (6 of 13 profile files in the corpus run). Pre-existing classifier weakness.

## Quick reference

- Tests: `python3 -m pytest -q`
- Lint: `ruff check manufacturing_pipeline/ tests/`
- Types: `mypy manufacturing_pipeline/`
- Run pipeline on a directory: `stepalesengine batch DIR --out-dir OUT --workers 8`
- Validate + report: `stepalesengine validate-corpus DIR --html report.html`
- Serve a corpus: `stepalesengine serve-corpus DIR --port 5050`
- Watch a directory: `stepalesengine watch DIR`
- Single file UI: `stepalesengine-web /path/to/manifest.xml`
