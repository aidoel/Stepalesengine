# Next steps + architecture state

State as of 2026-05-21. Test suite **535 passing**, **0 ruff violations**, **0 mypy errors** (81 files), ~22.5 k LOC.

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

### Corpus-validation follow-ups landed

A broad real-corpus run surfaced four issues, now addressed:

- **PARTIAL unfold no longer collapses to `uncertain`.** `feature_vector` counts `status != FAILURE` as `unfoldable`, consistent with `has_bends`; PARTIAL plates keep the plaat boost.
- **Rolled/folded tubular sections unfold.** A single simple cycle in the bend graph is now slit at its largest-radius bend and reported `PARTIAL` (`seamed_section`) instead of `FAILURE`. On `Silo 2` this turned 20 false failures into PARTIAL; only genuine pipes/blocks/freeform still fail.
- **Hole over-count fixed.** `HoleAnalyzer` now rejects partial-arc cylinders (corner rounds, bend reliefs) and splits co-axial bores by axial gap. Counts match the AutoPOL round-hole inventory exactly (530: 49->30, 529: 47->20).
- **Calibration sweep honours per-part labels.** `sweep.py` matches the CSV `product_id` column so multi-part assemblies can be labelled per-part.

### Rolled tubes unfold; seamed tubes classify as profiel

A post-fix corpus run measured holes/bends/thickness exact (4/4 vs AutoPOL), unfold ~90%, but classification only 60% - profiles mislabelled `plaat`. Two fixes: (1) `_detect_rolled_tube` - a thin-wall full-wrap (>=300 deg) cylinder pair is a developable rolled sheet, now `PARTIAL`/`seamed_section` instead of `FAILURE`/`cyclic_graph`; solid bars and thick pipes still fail. (2) A `seamed_tube` `FeatureVector` feature gates the plaat `unfoldable`/`has_bends` rewards off via an `and_not` cross-term and rewards `profiel` instead, so seamed hollow sections classify `profiel`. Classification on the labelled slice moved 12/20 -> 16/20.

## Outstanding work (in priority order)

### Grow the labelled corpus, then calibrate

Only ~20 folder-labelled STEP files exist (`Zetwerk/`=plaat, `profiel/`=profiel, `samenstelling/`=anders under `Downloads/stepfile/`) - too few for a meaningful weight sweep. `stepalesengine calibrate` + `calibration/sweep.py` are ready (per-part labels now work). Needs more labelled single-part files before a real sweep.

### Short / blocky profiles still misclassify

The seamed-tube fix handled hollow-section profiles. Four corpus files remain wrong: short non-uniform profiles (`10000182371`, `803041-7028`, `333380_rev[B]`) that fail unfold, do not match a standard profile, and read `cross_section_constant=false`, plus one borderline pocketed plate (`10000362951`). Fixing them needs a profile-matcher / cross-section-detection improvement or the calibration corpus - not safely done by weight-tuning.

### Assembly part listing is per-instance (by design)

The manifest emits one `<part>` per solid instance; AutoPOL groups identical parts with a quantity. The `quantity` field already carries NAUO multiplicity, so a grouped BOM would be a presentation-layer rollup. Not a bug - listed so any per-assembly count comparison against AutoPOL accounts for it.

## Quick reference

- Tests: `python3 -m pytest -q`
- Lint: `ruff check manufacturing_pipeline/ tests/`
- Types: `mypy manufacturing_pipeline/`
- Run pipeline on a directory: `stepalesengine batch DIR --out-dir OUT --workers 8`
- Validate + report: `stepalesengine validate-corpus DIR --html report.html`
- Serve a corpus: `stepalesengine serve-corpus DIR --port 5050`
- Watch a directory: `stepalesengine watch DIR`
- Single file UI: `stepalesengine-web /path/to/manifest.xml`
