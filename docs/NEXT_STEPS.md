# Next steps + architecture state

State as of 2026-05-16. Test suite **447 passing**, **0 ruff violations**, ~22.5 k LOC.

## What this session landed

| Module | Status |
|---|---|
| `parsing/` | stable; six-strategy cascade + OCAF fallback |
| `geometry/` | stable; feature extractor (incl. `pocket_complexity`), unfold probe (branching + joggle), hole analyzer, profile matcher |
| `classification/` | stable; score classifier + cross-terms + `FeatureVector` dataclass |
| `assembly/` | stable; NAUO graph + matcher |
| `pmi/` | stable; 16 GD&T tolerance types, AP242 e1/e2/e3 |
| `cam/` | stable; machining strategy probe |
| `io/` | stable; DXF + XML (declarative walker) + PDF |
| `pipeline/` | stable; orchestrator + diff + **probe registry** |
| `web/` | stable; trace browser + 3D viewer + corpus drill-down |
| `cli/` | **refactored**: 11 per-subcommand modules under `cli/` package |
| `cache/`, `batch.py`, `validate.py`, `calibration/`, `telemetry.py` | stable |

## Architectural improvements this round

1. **`cli.py` -> `cli/` package.** Each subcommand in its own < 200 LOC module with shared helpers in `_common.py`. Adding a new subcommand is now a single-file edit.

2. **Probe registry** (`pipeline/probes/`). The five probes (unfold, hole, profile, pmi, cam) implement a shared `Probe` Protocol and live in a `ProbeRegistry`. Adding a probe = registering one wrapper.

3. **`FeatureVector` dataclass** (`classification/feature_vector.py`). Replaces the untyped dict from `_classifier_features` with a typed 12-field dataclass. Scorer rules now reference fields that can be checked by static analysis.

4. **Declarative manifest XML** (`io/_xml_dataclass.py`). A `build_element` / `parse_element` walker handles every leaf dataclass via reflection. Adding a field to a manifest dataclass no longer requires touching XML code.

5. **Corpus -> per-file drill-down.** `stepalesengine serve-corpus DIR` mounts the trace browser per file under `/file/<safe_name>/` so a user can click a row in the report and land in the full per-file viewer.

## Outstanding work (in priority order)

### Performance: skip expensive probes on hopeless inputs

`docs/PERF_FINDING_803139.md` documents the 626 s outlier. The fix is a cheap pre-filter:

- Compute `face_count`, `volume/(L*W*T)`, `aspect_ratio` first.
- Skip `UnfoldProbe.run` if `face_count > 200` or solid-fill > 0.7 (saves 167 ms/part).
- Skip `slice_solid + ProfileShapeMatcher.match` unless `aspect_ratio > 3` (saves 72 ms/part).
- Per-solid identity cache keyed on `(volume_bucket, surface_area_bucket, n_faces)` so identical fasteners aren't re-classified 50 times.

Expected payoff: ~10x on assemblies with many duplicates. Real-corpus median should drop from 7 s to ~1 s per file.

### Type-checking with mypy

`pyproject.toml` already declares the API surface. Adding `mypy` to dev extras + `[tool.mypy]` config would catch a class of bugs (the `_xml_dataclass.py` failure that landed this round was exactly this kind of bug).

### Coverage measurement

`pytest-cov` would tell us which paths are tested. Useful for the next refactor wave; today's confidence comes from 447 tests passing, not from coverage data.

### Calibration on the user's 92-file corpus

We have a labelled-by-folder corpus (`Zetwerk/` = plaat, `profiel/` = profiel, `samenstelling/` = anders, etc). A folder-derived label CSV + `stepalesengine calibrate` would tune thresholds against real production data instead of just the NIST set.

### Real-world PDF / PMI report deliverable

The corpus report is HTML + Markdown today. A printable PDF version (one page per part with the flat DXF, hole table, bend table, classification trace) would be shop-floor-ready.

### Watch mode

`stepalesengine watch DIR` that re-processes files on change. Useful for CAD designers iterating in real time. Trivial with `watchdog` (~50 LOC).

## Architectural debt worth tracking

- **`analyze_assembly.py` is still ~700 LOC.** Now that probes live in a registry, the orchestrator could be slimmer: just `parse -> load -> match -> run_probes -> classify -> emit`. Worth a follow-up pass.
- **`web/server.py` is ~900 LOC** with inline templates. A `web/templates.py` module would help.
- **Scorer weights live in code, not config.** A YAML weights file + `default_scorers_from_yaml()` would let users tune without code changes.

## Quick reference

- Tests: `python3 -m pytest -q`
- Lint: `ruff check manufacturing_pipeline/ tests/` (currently clean)
- Run pipeline on a directory: `stepalesengine batch DIR --out-dir OUT --workers 8`
- Validate + report: `stepalesengine validate-corpus DIR --html report.html`
- Serve a corpus: `stepalesengine serve-corpus DIR --port 5050`
- Single file UI: `stepalesengine-web /path/to/manifest.xml`
