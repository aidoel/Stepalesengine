# stepalesengine

A robust pipeline for turning a STEP (Part 21) assembly into a classified bill of materials and a set of shop-floor deliverables. Given a `.step` / `.stp` file, the engine parses every part record, walks each solid through a single-pass feature extractor, runs sheet-metal unfold and standard-profile probes, scores each part as `plaat` (plate), `profiel` (profile), `anders` (other), or `uncertain`, and emits a manifest XML plus per-part flat-pattern DXF / PDF drawings. Built for fabrication shops that need a reliable answer on every file, regardless of CAD origin, naming quality, or geometric peculiarities.

## Install

The pure-Python pipeline runs without any native dependency. Geometry-aware steps (loading the STEP, extracting features, running the unfold probe, slicing cross-sections) need OpenCascade via `cadquery-ocp` and are gated behind the `[occt]` extra.

```
pip install -e .
pip install -e ".[occt]"   # adds cadquery-ocp for geometry
pip install -e ".[dev]"    # pytest + hypothesis for the test suite
```

`cadquery-ocp` ships pre-built wheels for Linux and macOS on Python 3.10 - 3.12. Without it the parser, classifier, diff, and writers still function (they operate on cached / synthesised dataclasses); only the geometry-aware probes are skipped.

## Quick start

Analyse a STEP file from the command line:

```
python3 -m manufacturing_pipeline.cli analyze path/to/file.step --out-dir ./out
```

This produces:

```
out/
  manifest.xml          # the full assembly manifest (see "Output schema")
  parts/
    bracket_a.dxf       # one flat-pattern DXF per part classified as plaat
    bracket_b.dxf
    ...
```

If `cadquery-ocp` is not installed the call still works: parts list, classification labels degrade to `uncertain`, and no DXF files are written.

## Library use

```python
from pathlib import Path
from manufacturing_pipeline.pipeline.analyze_assembly import AnalyzeOptions, analyze

result = analyze(
    Path("path/to/file.step"),
    AnalyzeOptions(out_dir=Path("./out"), write_dxf=True, write_xml=True),
)
print(f"parsed {len(result.manifest.parts)} parts")
for entry in result.manifest.parts:
    label = entry.classification.label
    conf = entry.classification.confidence
    print(f"  {entry.part.product_id:24s} -> {label} (conf={conf:.2f})")
```

`analyze` never raises on geometry errors; per-part failures degrade into `classification.label == "uncertain"` and a warning string on `result.warnings`. The only exceptions are `FileNotFoundError` (missing input) and `manufacturing_pipeline.parsing.types.StepParseError` (unreadable STEP).

## Architecture

The pipeline is DAG-shaped, not sequential. Probes run on the same feature vector and feed into a score-based classifier; no first-match short-circuit decides labels behind the user's back.

```
              STEP file
                 |
        +--------+--------+
        |                 |
        v                 v
   parsing/         geometry/
   step_parser    geometry_loader
   (6-strategy    (OCP loader,
    cascade +      ShapeFix,
    OCAF +         explode
    filename)      compounds)
        |                 |
   StepPart[]      TopoDS_Solid[]
        |                 |
        +--------+--------+
                 |
                 v
          assembly/
        build_assembly_graph
        match_parts_to_solids
                 |
                 v
         geometry/feature_extractor
         (single-pass ManufacturingFeatures)
                 |
        +--------+--------+--------+
        |        |        |        |
        v        v        v        v
     unfold  hole_   profile  cross_
     probe   analyzer matcher  section
        |        |        |        |
        +--------+--------+--------+
                 |
                 v
        classification/
        ScoreClassifier
        + tiebreakers
        + softmax + margin
                 |
                 v
         io/manifest writers
         (XML + DXF + PDF)
```

The orchestrator lives in `manufacturing_pipeline/pipeline/analyze_assembly.py`. Each probe is wrapped in `try / except` so a single solid that confuses OCCT cannot abort the whole assembly.

## Modules

| Package | Responsibility |
|---|---|
| `parsing/` | Defensive STEP text parser. Six-strategy cascade (`NAUO` -> `PRODUCT_DEFINITION` -> `PRODUCT` -> BREP names -> header -> comments) plus an OCAF fallback. Never returns `None`; always emits at least one `StepPart` with a `source` provenance tag. |
| `geometry/` | OCCT-backed feature extraction: STEP load + `ShapeFix`, single-pass `FeatureExtractor`, cross-section slicing + signature, hole analyzer, sheet-metal unfold probe, standard-profile matcher. |
| `assembly/` | Builds an `AssemblyNode` tree from the flat `StepPart` list (handles cyclic refs, dangling children, duplicate IDs). Pairs leaves to solids through a 1-to-1 -> ordered -> OCAF -> name cascade (`assembly/matcher.py`). |
| `classification/` | Score-based classifier: per-class additive scorers, softmax calibration, margin-based ambiguity detection, tiebreaker pipeline, decision trace. |
| `io/` | DXF, XML, and PDF writers driven from `FlatPattern` and `AssemblyManifest` dataclasses. Used for shop-floor deliverables and ERP / BOM integration. |
| `pipeline/` | Orchestration: `analyze_assembly` (end-to-end pipeline), `diff` (compare two STEP files). |
| `config/` | Single source of truth for thresholds and weights (`classification_variables.py`). |
| `data/` | Bundled YAML tables for standard profiles (DIN 1025/1026, EN 10210-2, EN 10056, EN 10055). |

## Output schema

The pipeline emits one **manifest XML** per analysed file plus one **flat-pattern DXF** per classified plate. PDF is opt-in (used by `write_pdf` / `write_assembly_pdf`).

### Manifest XML

Single file at `<out_dir>/manifest.xml`. Lives under the namespace `https://stepalesengine.dev/manifest/1`. Round-trips through `write_xml` / `read_xml`. Top-level structure:

```
<assembly source="..." source-mtime=... source-size=... model-version=... generated-at=...>
  <notes>...</notes>
  <parts>
    <part product-id=... name=... description=... source=... quantity=...>
      <classification label="plaat|profiel|anders|uncertain" confidence=...>
        <scores>...</scores>
        <probabilities>...</probabilities>
        <margin value=... ambiguous=.../>
        <contributions>...</contributions>
        <tiebreakers>...</tiebreakers>
        <probe-results>...</probe-results>
        <model-version value=.../>
      </classification>
      <features>...</features>
      <profile-match family=... standard=... designation=... residual-mm=... confidence=.../>
      <unfold status=... n-bends=... flat-area=... thickness-mean=... thickness-cv=.../>
      <holes count=...><hole diameter=... units="mm"/></holes>
      <flat-dxf path="parts/bracket_a.dxf"/>
    </part>
  </parts>
</assembly>
```

Every part carries the full decision trace (`scores`, `probabilities`, margin, per-feature `contributions`, tiebreakers run). The trace is what makes misclassifications debuggable: any part labelled `uncertain` shows exactly why.

### DXF layer convention

Layer names follow the laser-cutter shop-floor convention shared by Lantek, Trumpf, and Bystronic post-processors. Defined in `manufacturing_pipeline/io/dxf_writer.py:LAYER_SPEC`:

| Layer | Purpose | Color (ACI) | Linetype |
|---|---|---|---|
| `OUTER` | Outer cut contour | 1 (red) | Continuous |
| `INNER` | Inner cuts (holes, slots) | 3 (green) | Continuous |
| `BEND_UP` | Bends folding up | 5 (blue) | DASHED2 |
| `BEND_DOWN` | Bends folding down | 4 (cyan) | DASHED2 |
| `ANNOTATION` | Bend tables, text | 7 (white) | Continuous |
| `INFO` | Title header (part name, thickness) | 7 (white) | Continuous |

A DXF is emitted for every part whose classification label is in `AnalyzeOptions.dxf_only_for` (default `("plaat",)`) AND for which the unfold probe returns `status=SUCCESS`. Multi-part nesting modes (`none` / `strip` / `binpack`) live in `write_assembly_dxf`.

## CLI

```
python3 -m manufacturing_pipeline.cli <subcommand> [options]
```

The console-script entry point installed by `pyproject.toml` (`stepalesengine = "manufacturing_pipeline.cli:main"`) gives you a shorter invocation once the package is installed:

```
stepalesengine analyze path/to/file.step --out-dir ./out
```

| Subcommand | Purpose |
|---|---|
| `analyze PATH` | Full pipeline: parse + load + classify + write DXF / XML. Writes to `--out-dir` (default `./out`). Flags: `--no-dxf`, `--no-xml`, `--quiet`, `--verbose`. |
| `parts PATH` | Parser-only listing. Prints `<product_id>\t<name>\t<source>` per part. Useful for sanity-checking the parse before running geometry. |
| `diff OLD NEW` | Compare two STEP files. Pairs parts by name, product_id, or fingerprint (`--match-by`); tolerance configurable with `--tolerance` (relative percent, default 1.0). Returns exit code 1 when differences exist, 0 when identical. |
| `version` | Print the installed package version. |

Exit codes: `0` success, `1` file parsed but yielded zero parts (or diff found differences), `2` input path missing or unreadable.

### Example: full pipeline output

```
$ stepalesengine analyze 31686-080.stp --out-dir ./out
source: /path/to/31686-080.stp
output: /path/to/out
parts: 12 (plaat=8 profiel=2 anders=1 uncertain=1)
  P-001 bracket-top      -> plaat    (conf=0.94) [parts/bracket_top.dxf]
  P-002 bracket-bottom   -> plaat    (conf=0.93) [parts/bracket_bottom.dxf]
  P-003 cross-beam       -> profiel  (conf=0.88)
  P-004 din-933-m12      -> anders   (conf=0.82)
  ...
```

The `(conf=...)` is the calibrated softmax probability that the winning label is correct; values below `CONFIDENCE_THRESHOLD = 0.65` would be tagged `uncertain` instead.

## Testing

```
pytest -q
```

The full suite is 283 tests across `tests/parsing/`, `tests/geometry/`, `tests/classification/`, `tests/assembly/`, `tests/io/`, `tests/pipeline/`, and `tests/regression/`. It includes a Hypothesis-driven fuzz test for the STEP tokenizer, synthetic CadQuery fixtures for the unfold probe, and a canonical regression test for `31686-080.stp`. The geometry-heavy tests are skipped when `cadquery-ocp` is not importable. Wall-clock runtime is roughly 12 seconds on a modern laptop.

## Project status

**Stable** (covered by regression tests, documented, used in production):

- Six-strategy parser cascade plus OCAF fallback. Never returns `None`.
- Single-pass `FeatureExtractor` with consistent ratios across plate / profile / anders.
- Score-based classifier with margin + softmax + tiebreakers and a full `DecisionTrace` per part.
- Cross-section signature with fillet-collapse canonicalisation (see ADR 0007).
- Standard-profile matcher for DIN 1025 (I / IPE / HEA / HEB), DIN 1026 (UNP / UPE), EN 10210-2 (RHS / SHS / CHS), EN 10056 (L), EN 10055 (T).
- DXF, XML, and PDF writers (see ADR 0005).
- Assembly walker with quantity rollups (see ADR 0006).

**Beta** (works on the regression corpus, may regress on adversarial inputs):

- Multi-bend unfold for branching parts (star-shaped flange trees). Falls back to `PARTIAL` when the bend graph branches more than 2.
- Mesh-only STEP files: a tessellated fallback produces a `source = "mesh"` feature vector with a subset of fields populated.
- `diff` over `match_by="fingerprint"`: greedy minimum L1 distance over a coarse signature. Works well for parts that differ by quantity but identical geometry, less so for renamed-and-resized parts.

**Out of scope** for this iteration:

- Automatic FEA, machining strategy selection, quoting.
- Threaded-hole detection beyond an advisory heuristic.
- ML-based scorer lift (see ROBUST_FEATURE_DETECTION_PLAN.md section 7.8 - the plumbing is in place, the trained model is not).

## Configuration

Every tunable threshold in the pipeline lives in `manufacturing_pipeline/config/classification_variables.py`:

| Constant | Default | Used by |
|---|---|---|
| `MARGIN_THRESHOLD` | `0.15` | Score classifier: minimum `top1 - top2` gap before tiebreakers fire. |
| `CONFIDENCE_THRESHOLD` | `0.65` | Score classifier: winner must clear this softmax probability or label becomes `uncertain`. |
| `SOFTMAX_TEMPERATURE` | `0.5` | Score classifier: lower = sharper distribution. |
| `CROSS_SECTION_CV_LIMIT` | `0.02` | Cross-section: max coefficient of variation across slices for `is_constant` to be true. |
| `CROSS_SECTION_N_SLICES` | `7` | Cross-section: number of normalised positions along the principal axis. |
| `PROFILE_FILLET_COLLAPSE_RATIO` | `0.15` | Cross-section: short-edge fraction of `min(bbox_h, bbox_b)` to collapse as fillet. |
| `PROFILE_RESIDUAL_TOLERANCE_MM` | `1.0` | Profile matcher: residual below this means confident match. |
| `PROFILE_RESIDUAL_REJECT_MM` | `1.5` | Profile matcher: residual above this means `profiel_custom`. |
| `UNFOLD_K_FACTOR` | `0.44` | Unfold probe: neutral-axis position. |
| `UNFOLD_THICKNESS_CV_LIMIT` | `0.05` | Unfold probe: thickness CV above this -> `PARTIAL`. |
| `OBB_PRINCIPAL_AXIS_MIN_RATIO` | `1.2` | OBB: ratio below which we fall back to AABB. |

Phase 6 of the master plan replaces these hand-tuned values with sweep-optimised values driven by a held-out cost-weighted confusion matrix; the constants are imported by name everywhere so a single edit re-tunes the whole pipeline.

## Documentation

- `docs/ROBUST_FEATURE_DETECTION_PLAN.md` - the 959-line master plan that the implementation tracks.
- `docs/CONTEXT.md` - domain glossary, bounded contexts, cross-context invariants.
- `docs/adr/` - architecture decision records (0001 through 0007).
  - `0001-name-as-feature-not-veto.md` - name signals are features, not vetoes.
  - `0002-score-based-classification.md` - score-based classifier with margin + softmax + tiebreakers.
  - `0003-unfold-probe-as-feature.md` - unfold is a probe-as-feature, not a classifier.
  - `0004-feature-extractor-single-pass.md` - single-pass uniform feature extractor.
  - `0005-output-writers.md` - DXF + XML + PDF writers from one FlatPattern.
  - `0006-assembly-graph.md` - explicit `AssemblyNode` tree, not a flat list.
  - `0007-fillet-collapse-canonicalisation.md` - fillet collapse before profile signature.

## License

MIT. See `LICENSE`.
