# Coverage Report

**Total coverage: 78.6%** (statement: 81.5%, branch: 70.1%)

- Statements covered: 6658 / 8167 (missing: 1509)
- Branches covered: 1944 / 2772 (partial: 488)
- Tests: 474 passing
- Generated: 2026-05-16

## Per-module coverage

Sorted alphabetically. `Combined %` is the line+branch weighted figure that coverage.py reports as `Cover`.

| Module | Stmts | Missing | Line % | Branch % | Combined % |
|---|---:|---:|---:|---:|---:|
| `manufacturing_pipeline/__init__.py` | 0 | 0 | n/a | n/a | n/a |
| `manufacturing_pipeline/assembly/__init__.py` | 3 | 0 | 100.0 | n/a | 100.0 |
| `manufacturing_pipeline/assembly/graph.py` | 92 | 0 | 100.0 | 100.0 | 100.0 |
| `manufacturing_pipeline/assembly/matcher.py` | 73 | 21 | 71.2 | 55.6 | 66.1 |
| `manufacturing_pipeline/batch.py` | 70 | 6 | 91.4 | 83.3 | 90.2 |
| `manufacturing_pipeline/cache/__init__.py` | 2 | 0 | 100.0 | n/a | 100.0 |
| `manufacturing_pipeline/cache/disk.py` | 118 | 16 | 86.4 | 75.0 | 84.5 |
| `manufacturing_pipeline/calibration/__init__.py` | 3 | 0 | 100.0 | n/a | 100.0 |
| `manufacturing_pipeline/calibration/sweep.py` | 122 | 13 | 89.3 | 80.0 | 87.0 |
| `manufacturing_pipeline/cam/__init__.py` | 4 | 0 | 100.0 | n/a | 100.0 |
| `manufacturing_pipeline/cam/strategist.py` | 167 | 10 | 94.0 | 84.5 | 90.8 |
| `manufacturing_pipeline/cam/types.py` | 17 | 0 | 100.0 | n/a | 100.0 |
| `manufacturing_pipeline/classification/__init__.py` | 0 | 0 | n/a | n/a | n/a |
| `manufacturing_pipeline/classification/calibration.py` | 7 | 0 | 100.0 | n/a | 100.0 |
| `manufacturing_pipeline/classification/decision_trace.py` | 6 | 6 | 0.0 | n/a | 0.0 |
| `manufacturing_pipeline/classification/feature_vector.py` | 36 | 1 | 97.2 | 75.0 | 95.0 |
| `manufacturing_pipeline/classification/score_classifier.py` | 49 | 0 | 100.0 | 100.0 | 100.0 |
| `manufacturing_pipeline/classification/scorers.py` | 70 | 9 | 87.1 | 73.1 | 83.3 |
| `manufacturing_pipeline/classification/tiebreakers.py` | 15 | 3 | 80.0 | 66.7 | 76.2 |
| `manufacturing_pipeline/classification/types.py` | 23 | 0 | 100.0 | n/a | 100.0 |
| `manufacturing_pipeline/cli/main.py` | 18 | 0 | 100.0 | 100.0 | 100.0 |
| `manufacturing_pipeline/config/__init__.py` | 0 | 0 | n/a | n/a | n/a |
| `manufacturing_pipeline/config/classification_variables.py` | 39 | 0 | 100.0 | n/a | 100.0 |
| `manufacturing_pipeline/geometry/__init__.py` | 0 | 0 | n/a | n/a | n/a |
| `manufacturing_pipeline/geometry/cross_section.py` | 463 | 42 | 90.9 | 75.3 | 87.0 |
| `manufacturing_pipeline/geometry/feature_extractor.py` | 390 | 71 | 81.8 | 74.6 | 80.1 |
| `manufacturing_pipeline/geometry/feature_merger.py` | 14 | 0 | 100.0 | 100.0 | 100.0 |
| `manufacturing_pipeline/geometry/geometry_loader.py` | 157 | 66 | 58.0 | 39.1 | 52.5 |
| `manufacturing_pipeline/geometry/hole_analyzer.py` | 215 | 27 | 87.4 | 72.9 | 84.8 |
| `manufacturing_pipeline/geometry/profile_matcher.py` | 334 | 68 | 79.6 | 69.4 | 76.6 |
| `manufacturing_pipeline/geometry/shape_health.py` | 67 | 20 | 70.1 | 58.3 | 67.0 |
| `manufacturing_pipeline/geometry/types.py` | 64 | 0 | 100.0 | n/a | 100.0 |
| `manufacturing_pipeline/geometry/unfold_probe.py` | 940 | 150 | 84.0 | 72.0 | 80.9 |
| `manufacturing_pipeline/io/__init__.py` | 0 | 0 | n/a | n/a | n/a |
| `manufacturing_pipeline/io/_xml_dataclass.py` | 235 | 53 | 77.4 | 69.2 | 74.5 |
| `manufacturing_pipeline/io/dxf_writer.py` | 226 | 18 | 92.0 | 86.9 | 90.6 |
| `manufacturing_pipeline/io/pdf_corpus_writer.py` | 526 | 49 | 90.7 | 70.3 | 86.2 |
| `manufacturing_pipeline/io/pdf_writer.py` | 309 | 10 | 96.8 | 89.0 | 95.1 |
| `manufacturing_pipeline/io/xml_writer.py` | 397 | 1 | 99.7 | 87.5 | 95.6 |
| `manufacturing_pipeline/parsing/__init__.py` | 0 | 0 | n/a | n/a | n/a |
| `manufacturing_pipeline/parsing/dutch_vocabulary.py` | 8 | 0 | 100.0 | 100.0 | 100.0 |
| `manufacturing_pipeline/parsing/occt_fallback.py` | 89 | 75 | 15.7 | 0.0 | 13.9 |
| `manufacturing_pipeline/parsing/standard_label.py` | 13 | 0 | 100.0 | 100.0 | 100.0 |
| `manufacturing_pipeline/parsing/step_parser.py` | 97 | 15 | 84.5 | 92.9 | 86.4 |
| `manufacturing_pipeline/parsing/step_strategies.py` | 295 | 68 | 76.9 | 72.6 | 75.4 |
| `manufacturing_pipeline/parsing/step_tokenizer.py` | 202 | 14 | 93.1 | 89.7 | 92.2 |
| `manufacturing_pipeline/parsing/types.py` | 10 | 0 | 100.0 | n/a | 100.0 |
| `manufacturing_pipeline/pipeline/__init__.py` | 0 | 0 | n/a | n/a | n/a |
| `manufacturing_pipeline/pipeline/analyze_assembly.py` | 281 | 156 | 44.5 | 11.5 | 37.3 |
| `manufacturing_pipeline/pipeline/diff.py` | 198 | 16 | 91.9 | 78.6 | 88.4 |
| `manufacturing_pipeline/pipeline/probes/__init__.py` | 56 | 0 | 100.0 | 87.5 | 98.4 |
| `manufacturing_pipeline/pipeline/probes/cam_probe.py` | 26 | 2 | 92.3 | 66.7 | 87.5 |
| `manufacturing_pipeline/pipeline/probes/hole_probe.py` | 10 | 0 | 100.0 | n/a | 100.0 |
| `manufacturing_pipeline/pipeline/probes/pmi_probe.py` | 8 | 0 | 100.0 | n/a | 100.0 |
| `manufacturing_pipeline/pipeline/probes/profile_probe.py` | 15 | 0 | 100.0 | 100.0 | 100.0 |
| `manufacturing_pipeline/pipeline/probes/unfold_probe.py` | 10 | 0 | 100.0 | n/a | 100.0 |
| `manufacturing_pipeline/pmi/__init__.py` | 4 | 0 | 100.0 | n/a | 100.0 |
| `manufacturing_pipeline/pmi/extractor.py` | 413 | 116 | 71.9 | 65.1 | 69.6 |
| `manufacturing_pipeline/pmi/types.py` | 34 | 0 | 100.0 | n/a | 100.0 |
| `manufacturing_pipeline/serve_corpus.py` | 142 | 29 | 79.6 | 61.1 | 75.8 |
| `manufacturing_pipeline/telemetry.py` | 83 | 18 | 78.3 | 75.0 | 77.6 |
| `manufacturing_pipeline/validate.py` | 246 | 27 | 89.0 | 68.4 | 84.2 |
| `manufacturing_pipeline/watch.py` | 93 | 7 | 92.5 | 79.2 | 89.7 |
| `manufacturing_pipeline/web/__init__.py` | 3 | 0 | 100.0 | n/a | 100.0 |
| `manufacturing_pipeline/web/dxf_to_svg.py` | 81 | 6 | 92.6 | 76.9 | 88.8 |
| `manufacturing_pipeline/web/server.py` | 229 | 93 | 59.4 | 39.1 | 54.9 |
| `manufacturing_pipeline/web/step_to_glb.py` | 89 | 78 | 12.4 | 0.0 | 9.6 |
| `manufacturing_pipeline/web/step_to_svg.py` | 153 | 129 | 15.7 | 0.0 | 13.1 |
| `manufacturing_pipeline/web/templates.py` | 8 | 0 | 100.0 | 100.0 | 100.0 |

## Top 10 under-covered modules

| Module | Stmts | Missing | Combined % |
|---|---:|---:|---:|
| `manufacturing_pipeline/classification/decision_trace.py` | 6 | 6 | 0.0 |
| `manufacturing_pipeline/web/step_to_glb.py` | 89 | 78 | 9.6 |
| `manufacturing_pipeline/web/step_to_svg.py` | 153 | 129 | 13.1 |
| `manufacturing_pipeline/parsing/occt_fallback.py` | 89 | 75 | 13.9 |
| `manufacturing_pipeline/pipeline/analyze_assembly.py` | 281 | 156 | 37.3 |
| `manufacturing_pipeline/geometry/geometry_loader.py` | 157 | 66 | 52.5 |
| `manufacturing_pipeline/web/server.py` | 229 | 93 | 54.9 |
| `manufacturing_pipeline/assembly/matcher.py` | 73 | 21 | 66.1 |
| `manufacturing_pipeline/geometry/shape_health.py` | 67 | 20 | 67.0 |
| `manufacturing_pipeline/pmi/extractor.py` | 413 | 116 | 69.6 |

## Top 5 best-covered modules (10+ stmts)

| Module | Stmts | Combined % |
|---|---:|---:|
| `manufacturing_pipeline/assembly/graph.py` | 92 | 100.0 |
| `manufacturing_pipeline/geometry/types.py` | 64 | 100.0 |
| `manufacturing_pipeline/classification/score_classifier.py` | 49 | 100.0 |
| `manufacturing_pipeline/config/classification_variables.py` | 39 | 100.0 |
| `manufacturing_pipeline/pmi/types.py` | 34 | 100.0 |

## Suggested test additions (3 lowest non-trivial modules)

- **`manufacturing_pipeline/web/step_to_glb.py` (9.6%)** -- Add a smoke test that exercises STEP-to-GLB conversion against a small fixture; the entire module path 26-146 is currently untested.
- **`manufacturing_pipeline/web/step_to_svg.py` (13.1%)** -- Add a STEP-to-SVG conversion test using an existing `tests/fixtures` STEP; functions on lines 46-260 have zero coverage.
- **`manufacturing_pipeline/parsing/occt_fallback.py` (13.9%)** -- Add a test that triggers the OCCT fallback parser path (gate with the `occt` extra) so lines 23-134 in `_parse_with_occt` get exercised.

## Regenerating locally

```bash
pytest --cov=manufacturing_pipeline --cov-report=term-missing -q
```

For an HTML report (writes to `htmlcov/`, which is gitignored):

```bash
pytest --cov=manufacturing_pipeline --cov-report=term --cov-report=html -q
open htmlcov/index.html
```

Coverage configuration lives in `pyproject.toml` under `[tool.coverage.run]` and `[tool.coverage.report]`. The CI workflow (`.github/workflows/test.yml`) runs the same command and uploads `htmlcov/` as the `coverage-html-<python>` artifact.
