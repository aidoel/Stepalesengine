# mypy Type-Check Debt

This document tracks the state of mypy type-checking for `manufacturing_pipeline/`.
Generated when mypy was first added to the project.

## Summary

- **Initial error count:** 52 errors in 16 files (77 files checked)
- **Final error count:** 0 errors (81 files checked)
- **Fixes applied:** 19 changes across 14 files
- **Test result after fixes:** all tests pass

## Initial error categories

| Category | Count | Notes |
|---|---|---|
| `[arg-type]` | 16 | Mostly `object` passed to functions expecting concrete types (probe-registry results). |
| `[union-attr]` | 12 | Variable rebinding inside loops shadowed earlier `int \| None` annotations. |
| `[unused-ignore]` | 11 | `# type: ignore` on OCP imports now redundant under `ignore_missing_imports`. |
| `[attr-defined]` | 4 | `object` typed `face` field; `DXFEntity.closed`/`get_points` resolved at runtime. |
| `[assignment]` | 4 | Variable rebinding to a different shape (tuple-of-3 -> tuple-of-2, etc.). |
| `[misc]` | 3 | Invariant TypeVars used in a Protocol where variance is required. |
| `[return-value]` | 1 | Function returns `(None, ...)` but signature said `tuple[str, int]`. |
| `[call-arg]` | 1 | Two-arity transformer callable widened to `Callable[..., Any]`. |

## Fixes applied

| File | Line(s) | Fix |
|---|---|---|
| `manufacturing_pipeline/parsing/occt_fallback.py` | 24, 25, 37, 77-82 | Removed 9 unused `# type: ignore` comments on OCP imports (now redundant under `ignore_missing_imports`). |
| `manufacturing_pipeline/assembly/matcher.py` | 75 | Removed unused `# type: ignore` on internal import. |
| `manufacturing_pipeline/assembly/matcher.py` | 159 | Renamed loop variable `idx` -> `match_idx` to avoid shadowing earlier `int` binding; added explicit `int \| None` annotation. |
| `manufacturing_pipeline/geometry/feature_extractor.py` | 163 | Removed stale `# type: ignore[return-value]`. |
| `manufacturing_pipeline/geometry/feature_extractor.py` | 172-173 | Renamed local `dims` -> `sorted_dims` to avoid shadowing the outer `tuple[...]` binding. |
| `manufacturing_pipeline/geometry/unfold_probe.py` | 58, 67 | `face: object` -> `face: Any` on `_PlanarFace`/`_CylPatch` so OCP `IsSame`/methods type-check. |
| `manufacturing_pipeline/geometry/unfold_probe.py` | 564-568 | Renamed enumerate variable `b` -> `bend` to avoid `int \| None` shadowing. |
| `manufacturing_pipeline/geometry/unfold_probe.py` | 1653-1656 | Renamed `q` -> `uv` to avoid shadowing a 3-tuple binding. |
| `manufacturing_pipeline/geometry/cross_section.py` | 335 | `tuple(p) for p in polyline` -> `(p[0], p[1]) for p in polyline` so length-2 tuple is preserved. |
| `manufacturing_pipeline/geometry/cross_section.py` | 702 | Same fix; added explicit `list[tuple[float, float]]` annotation. |
| `manufacturing_pipeline/parsing/step_tokenizer.py` | 293 | Return type fixed to `tuple[str \| None, int]` to match docstring + behaviour. |
| `manufacturing_pipeline/cli/_watch.py` | 97 | Changed `# type: ignore[arg-type]` -> `# type: ignore[call-overload]` (correct error code). |
| `manufacturing_pipeline/classification/score_classifier.py` | 50 | `value_repr: object` -> `value_repr: float \| str` matches the `Contribution.value` field type. |
| `manufacturing_pipeline/web/server.py` | 75 | Added `not isinstance(obj, type)` to narrow `is_dataclass()` to instance-only. |
| `manufacturing_pipeline/web/dxf_to_svg.py` | 98 | `entity.closed` -> `getattr(entity, "closed", False)` (ezdxf type stubs don't see LWPolyline subtype). |
| `manufacturing_pipeline/io/pdf_corpus_writer.py` | 353, 358 | Same `getattr(...)` pattern for `get_points` / `closed`. |
| `manufacturing_pipeline/io/_xml_dataclass.py` | 161, 222 | Transformer signature widened to `Callable[..., Any]` to admit both 1-arg (value -> str) and 2-arg (parent, value -> None) callables. |
| `manufacturing_pipeline/io/_xml_dataclass.py` | 358 | Added `isinstance(inner, type)` guard for `parse_element(c, inner)`. |
| `manufacturing_pipeline/validate.py` | 235 | Hoisted `out_dir is not None` guard into the branch to satisfy `Path(...)`. |
| `manufacturing_pipeline/calibration/sweep.py` | 226-230 | Added `if features is None: continue` before passing to `_classifier_features`. |
| `manufacturing_pipeline/pipeline/probes/__init__.py` | 22-34 | Made TypeVars variance-correct: `I_contra` (contravariant input) + `R_co` (covariant return) on `Probe` protocol. |
| `manufacturing_pipeline/pipeline/probes/cam_probe.py` | 32-33 | Added isinstance narrowing on `unfold`/`profile` so they match `cam_recommend` signature. |
| `manufacturing_pipeline/pipeline/analyze_assembly.py` | 39-43, 263-269 | Added `ProfileMatch`/`UnfoldResult` imports and isinstance narrowing for all four probe results going into `enrich(...)` / `PartManifestEntry(...)`. |

## Remaining errors deliberately kept

None. mypy reports `Success: no issues found in 81 source files` with the configured settings.

The note `pyproject.toml: note: unused section(s): module = ['mapbox_earcut.*', 'playwright.*']` is informational - those modules are kept in the override list for future imports and are not currently referenced.

## Suggested cleanup priority for the next pass

mypy is currently configured at the project's friendliest level: `ignore_missing_imports = true`, no `strict_optional` enforcement on third-party calls, no `disallow_untyped_defs`. To tighten gradually:

1. **Enable `disallow_untyped_defs` per-module**, starting with `manufacturing_pipeline/parsing/` and `manufacturing_pipeline/classification/` (smallest surface, fewest OCP touches). Many internal helpers there have full annotations already; adding `-> None` returns to a handful of `def __init__` and CLI helpers would clear most diagnostics.
2. **Replace `face: Any`** on `_PlanarFace` / `_CylPatch` with a `Protocol` describing the OCP methods used (`IsSame`, etc.) once the OCP surface area is small enough to enumerate.
3. **Replace `dict[str, object]` on `ProbeRegistry.run_all`** with a `TypedDict` keyed by probe name; would let `analyze_assembly.py` drop the four isinstance guards added in this pass.
4. **Tighten `Callable[..., Any]` on `build_element.transformers`** by splitting the two arities into separate registries or by exposing a `@dataclass class Transformer` with explicit `as_attr` / `as_element` methods.
5. **Promote the workflow** from `continue-on-error: true` -> required once `--strict` mode passes on at least three sub-packages.

## CI

`.github/workflows/typecheck.yml` runs mypy on every PR. Failures are surfaced but do not fail the build (`continue-on-error: true`) until the codebase is ready to make typecheck a hard gate.
