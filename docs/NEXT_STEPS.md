# Next steps + architecture state

State as of 2026-05-20. Test suite **490 passing**, **0 ruff violations**, **0 mypy errors** (81 files), ~22.5 k LOC.

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

## Outstanding work (in priority order)

### Orchestrator does not use the probe registry

`ProbeRegistry` / `default_registry()` exist and are tested (`tests/pipeline/test_probes.py`), but `analyze_assembly.py::_process_pair` bypasses them — it instantiates `HoleAnalyzer`, the profile/unfold probes, `extract_pmi`, and `CamProbe` inline. The registry is currently decorative for the production path. Either route `_process_pair` through `registry.run_all` (so adding a probe really is a one-line edit) or drop the registry. The inline path also carries the prefilter/cache logic, so a migration must preserve that.

### `analyze_assembly.py` is 895 LOC

Grew past the ~700 LOC noted last round. Now that probes are factored out, the orchestrator could be a thin `parse -> load -> match -> run_probes -> classify -> emit`. Tie this to the registry-migration above.

### Calibration on the user's 92-file corpus

`stepalesengine calibrate` + `calibration/sweep.py` exist. Remaining work is operational: produce the folder-derived label CSV (`Zetwerk/` = plaat, `profiel/` = profiel, `samenstelling/` = anders) and run a real sweep against production data instead of the NIST set.

### Mesh-only STEP fallback is untested

`FeatureExtractor` has a mesh-only path that no test exercises (noted in the architecture memory). Add a synthetic mesh-only fixture.

## Quick reference

- Tests: `python3 -m pytest -q`
- Lint: `ruff check manufacturing_pipeline/ tests/`
- Types: `mypy manufacturing_pipeline/`
- Run pipeline on a directory: `stepalesengine batch DIR --out-dir OUT --workers 8`
- Validate + report: `stepalesengine validate-corpus DIR --html report.html`
- Serve a corpus: `stepalesengine serve-corpus DIR --port 5050`
- Watch a directory: `stepalesengine watch DIR`
- Single file UI: `stepalesengine-web /path/to/manifest.xml`
