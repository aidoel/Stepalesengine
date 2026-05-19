# CONTEXT

The shared vocabulary, bounded contexts, and cross-context invariants that govern stepalesengine. This document is the lookup table for new contributors and for AI agents: every term here has a single agreed meaning across the codebase.

## Glossary

### Domain labels (classification outputs)

- **plaat** - Dutch for "plate". A sheet-metal part: flat or bent, uniform thickness, manufacturable on a laser-cutter + press brake. Counts as in-house production.
- **profiel** - extruded structural profile cut to length in-house. Constant cross-section along an axis. Counts as production.
- **anders** - Dutch for "other". Purchased standard items (bolts, brackets, standard rolled profiles bought as-is) or machined / cast complex parts. No production from stock.
- **uncertain** - the classifier's score margin or top probability fell below threshold (`MARGIN_THRESHOLD = 0.15`, `CONFIDENCE_THRESHOLD = 0.65` in `manufacturing_pipeline/config/classification_variables.py`). Surfaced for human review; never silently bucketed into `anders`.

### STEP / OCCT vocabulary

- **NAUO** - `NEXT_ASSEMBLY_USAGE_OCCURRENCE`. The STEP entity that defines parent / child relationships in an assembly tree. Strategy 1 of the parser cascade.
- **PD** - `PRODUCT_DEFINITION`. Strategy 2.
- **BREP** - boundary representation. The geometric description in `MANIFOLD_SOLID_BREP` and `SHELL_BASED_SURFACE_MODEL`. Source of strategy 4 names.
- **AP203 / AP214 / AP242** - STEP application protocols. The parser handles all three; each has its own quirks (e.g. AP242 sometimes carries names only in `SHAPE_ASPECT`).
- **OCAF** - Open CASCADE Application Framework. XCAF labels read via `STEPCAFControl_Reader`. Final fallback in the parser, used by the OCAF helper in `manufacturing_pipeline/parsing/occt_fallback.py`.
- **OBB** - oriented bounding box. Computed by `Bnd_OBB`. Used by the cross-section slicer to find the principal axis.
- **AABB** - axis-aligned bounding box. Fallback when the OBB principal-axis ratio drops below `OBB_PRINCIPAL_AXIS_MIN_RATIO = 1.2` (near-cubic part, OBB orientation unstable).
- **BREP names** - the `name` attribute on `MANIFOLD_SOLID_BREP` / `SHELL_BASED_SURFACE_MODEL`. Strategy 4 of the cascade.

### Sheet-metal vocabulary

- **K-factor** - the neutral-axis position in a bent sheet, as a fraction of the thickness. Default `UNFOLD_K_FACTOR = 0.44` (mild steel, generous bend radius). Neutral radius `R_n = R + K * t`.
- **BA** - bend allowance: arc length of the neutral axis through a bend. `BA = theta * (R + K * t)`.
- **BD** - bend deduction: shortening of the developed length introduced by a sharp-corner bend. `BD = 2 * (R + t) * tan(theta/2) - BA`.
- **Hem** - a bend with `theta > 170 deg` and `R ~ t/2`. The unfold probe handles it but flags it (`flags["has_hem"] = True`).
- **Synthetic bend** - a bend where the source geometry has no cylindrical fillet between the two flats. The probe inserts a zero-radius bend and flags it (`flags["synthetic_bends"] = N`).

### Profile vocabulary

- **IPE / HEA / HEB / HEM** - European I-beam standards (DIN 1025-1..-4). Cross-section signature: `n_inner=0`, `n_seg=12`, `sym_x ∧ sym_y`. Sub-class by fitting `(h, b, t_w, t_f, r)` against the catalogue.
- **UNP / UPE** - European U-channels (DIN 1026). Open profile; `n_inner=0`, `n_seg=8`, `sym_x` only.
- **L** - angle iron (EN 10056). `n_seg=6`, no symmetry, two perpendicular legs.
- **T** - T-bar (EN 10055). `n_seg=8`, `sym_y`, flange + web orthogonal.
- **Z** - Z-section. `n_seg=8`, point-symmetric, no reflective symmetry.
- **RHS / SHS / CHS** - rectangular / square / circular hollow sections (EN 10210-2). `n_inner >= 1`.
- **Designation** - the catalogue identifier, e.g. `HEA 200`, `UNP 160`, `RHS 100x50x4`. Returned by `ProfileShapeMatcher.match` as `ProfileMatch.designation`.

### Classification mechanics

- **Decision trace** - the `DecisionTrace` dataclass (`manufacturing_pipeline/classification/types.py`) that ships with every `ClassificationResult`. Records `scores`, `probabilities`, `margin`, `ambiguous`, per-feature `contributions`, `tiebreakers_run`, `probe_results`, and `model_version`. Round-trips through the manifest XML.
- **Score margin** - `top1_score - top2_score`. When the margin is below `MARGIN_THRESHOLD = 0.15` the result is marked `ambiguous` and the tiebreaker pipeline fires.
- **Tiebreaker** - one of `unfold_tiebreaker`, `cross_section_tiebreaker`, `profile_match_tiebreaker`, `material_spec_tiebreaker` (`manufacturing_pipeline/classification/tiebreakers.py`). Runs in cheap-to-expensive order until one returns a non-`None` label. Stored in `DecisionTrace.tiebreakers_run` for auditability.
- **Softmax temperature** - `SOFTMAX_TEMPERATURE = 0.5`. Sharpens the score distribution so dominant features lift confidence above `CONFIDENCE_THRESHOLD`. Lower temperature = sharper decisions.

### Geometry mechanics

- **Unfold probe** - the `UnfoldProbe.run(solid)` call (`manufacturing_pipeline/geometry/unfold_probe.py`). Returns an `UnfoldResult` with `status ∈ {SUCCESS, PARTIAL, FAILURE}` plus bend count, flat area, thickness statistics, and flag dictionary. Never raises (see ADR 0003).
- **Fillet collapse** - cross-section canonicalisation that replaces short fillet runs with their corner intersection so the segment-count signature matches catalogue expectations. Defined in `cross_section.collapse_fillets`; threshold is `PROFILE_FILLET_COLLAPSE_RATIO = 0.15` of `min(bbox_h, bbox_b)`. See ADR 0007.
- **Signature** - the dict returned by `cross_section.compute_signature(polyline, inner_polylines)`. Keys: `n_inner`, `n_seg`, `n_seg_outer`, `sym_x`, `sym_y`, `point_sym`, `orth_ratio`, `outer_circular`, `inner_circular`, `inner_bbox`, `wall_thickness`, `bbox_h`, `bbox_b`. Consumed by the profile matcher to identify the family before fitting dimensions.

### Diff vocabulary

- **Fingerprint (diff matching)** - the coarse-grained signature used by `cli diff --match-by fingerprint` (`manufacturing_pipeline/pipeline/diff.py:_fingerprint`). Tuple of geometric features used in a greedy minimum-L1 pairing when names differ between revisions.
- **ECN** - engineering change notice. The `diff` subcommand is the foundation for ECN reports: it produces `added`, `removed`, `changed` lists with per-field deltas at `--tolerance` percent.

### Output vocabulary

- **Shelf nesting** - the bottom-left shelf-fill bin-packer in `dxf_writer._shelf_binpack`. Sorts patterns by bbox height descending, fills shelves greedily, opens a new sheet on overflow. No rotation. Used by `write_assembly_dxf(..., nesting="binpack")`. Logs per-sheet utilisation so the shop can compare to manual nesting.
- **FlatPattern** - the 2D output of the unfold probe: outer contour, holes, bend lines, thickness, bbox, units. Defined in `manufacturing_pipeline/io/dxf_writer.py`. Drives all three writers (DXF, PDF, XML).
- **AssemblyManifest** - the top-level XML payload (`manufacturing_pipeline/io/xml_writer.py`). Carries source provenance (`source_path`, `source_mtime`, `source_size`), `model_version`, `generated_at`, and a list of `PartManifestEntry` records.
- **Manifest namespace** - `https://stepalesengine.dev/manifest/1`. Bumps to `/manifest/2` on a breaking schema change. The minor in the URL is the only versioning consumers must pin.

### Process vocabulary

- **ECN** - engineering change notice. The `cli diff OLD NEW` subcommand is the foundation for an ECN report: it produces `added`, `removed`, `changed` lists with per-field deltas at `--tolerance` percent. Match strategies are `name` (default), `product_id`, and `fingerprint` (greedy minimum L1 distance over the coarse geometric tuple).
- **Cascade** - the architectural pattern shared by `parse_step` (six strategies + OCAF + filename), `match_parts_to_solids` (1to1 -> ordered -> ocaf -> by_name -> unmatched), and `ScoreClassifier.classify` (argmax -> margin -> tiebreaker chain). Each cascade returns the first non-empty, typed, provenance-tagged result; never raises.
- **Provenance tag** - the `source` field on every `StepPart` and `AssemblyNode`. Tells you which cascade rung emitted the record so a downstream surprise is traceable to its origin.

## Bounded contexts

Each context owns its terminology, its dataclasses, and its failure mode. Crossing a context boundary requires passing a typed dataclass; no context reaches into another's private internals.

### Parsing context

Owns: STEP text -> `list[StepPart]` with provenance tags. Lives in `manufacturing_pipeline/parsing/`. Public entry point is `parse_step(path)`. Internal failure modes (truncated entities, encoding quirks, X-encoded chars, cyclic NAUO) all degrade to "use the next strategy" rather than raising. The cascade is `nauo -> product_definition -> product -> brep -> header -> comments -> occt_xcaf -> filename`. The contract is `parse_step` returns a non-empty list; the only way it raises is `StepParseError` when the file is unreadable at all.

### Geometry context

Owns: `TopoDS_Solid` -> `ManufacturingFeatures` + probe results. Lives in `manufacturing_pipeline/geometry/`. The `FeatureExtractor.extract` call walks each solid exactly once and emits a flat `ManufacturingFeatures` dataclass (see ADR 0004). Probes (`UnfoldProbe`, `HoleAnalyzer`, `ProfileShapeMatcher`, `slice_solid`) consume the same solid + features and emit their own typed results (`UnfoldResult`, `HolePattern`, `ProfileMatch`, `list[CrossSection]`). None of them raise on bad geometry; failures become typed values (`UnfoldStatus.FAILURE`, `ProfileMatch.designation = None`, empty hole list).

### Classification context

Owns: feature dict -> `ClassificationResult` with `DecisionTrace`. Lives in `manufacturing_pipeline/classification/`. The `ScoreClassifier.classify(features, tiebreakers=..., probe_results=...)` call is the only public entry. The classifier is intentionally feature-vector-pure: it does not load geometry, does not call probes itself, and does not know about file paths. Probe results are passed in as already-computed values. This keeps the classifier deterministic, fast (no I/O), and trivially testable.

### Assembly context

Owns: `list[StepPart]` -> `AssemblyNode` tree + `list[MatchResult]` joining leaves to solids. Lives in `manufacturing_pipeline/assembly/`. `build_assembly_graph` deduplicates by `product_id`, handles cyclic references through a visited-set guard, synthesises placeholders for dangling children, and roots everything at a single virtual `ROOT_ID` node. `match_parts_to_solids` then pairs leaf nodes to solids through the strategy cascade documented in ADR 0006.

### I/O context

Owns: `FlatPattern` + `AssemblyManifest` -> on-disk DXF / XML / PDF. Lives in `manufacturing_pipeline/io/`. Each writer is independent and depends only on its input dataclass; they share no mutable state. The DXF and PDF writers consume `FlatPattern`, the XML writer consumes `AssemblyManifest` (which in turn references `FlatPattern` indirectly through `entry.flat_dxf_path`). See ADR 0005 for the rationale on shipping all three formats and ADR 0007 for the layer convention.

### Pipeline orchestration context

Owns: file path -> `AnalyzeResult`. Lives in `manufacturing_pipeline/pipeline/`. `analyze_assembly.analyze` is the end-to-end orchestrator: parse -> load -> graph -> match -> per-part probes -> classify -> write. Wraps every per-part probe in `try / except` so a single bad solid cannot abort the whole assembly. Failures degrade into `uncertain` entries with warnings. The companion `pipeline/diff.py` runs the same pipeline on two files and produces a structured diff.

### Configuration context

Owns: thresholds, weights, calibration constants. Lives in `manufacturing_pipeline/config/classification_variables.py`. Every tunable scalar in the pipeline (margin threshold, confidence threshold, softmax temperature, fillet collapse ratio, K-factor, profile residual tolerance, OBB ratio cutoff) is imported from this single module. Tests assert against these constants by name, not by literal, so a sweep-driven re-tuning (Phase 6 of the master plan) only needs to change one file.

## Cross-context invariants

These hold across the whole pipeline. Violating any of them is a bug, not a design tradeoff. Tests in `tests/` enforce them.

### Parser never returns None

`parse_step` always returns `list[StepPart]` with at least one entry. The terminal fallback synthesises a single record from the filename basename with `source = "filename"`. Code downstream may rely on this: there is no need to defend against `None` returns. Enforced by `tests/parsing/test_step_parser.py` and the regression test for `31686-080.stp`.

### Probes never raise

`UnfoldProbe.run`, `HoleAnalyzer.analyze`, `ProfileShapeMatcher.match`, `slice_solid`, and `is_constant` all return a typed result on every input, including malformed solids, BSpline soup, and tessellated meshes. Failures appear as `UnfoldStatus.FAILURE`, `designation = None`, empty lists, or `False`. This is what makes the score classifier robust: any probe that decides to bail still contributes a known feature value rather than blowing up the run.

### Single-pass feature extractor

`FeatureExtractor.extract(solid)` walks each solid exactly once and emits a flat `ManufacturingFeatures` dataclass. Downstream code consumes it unchanged. No part of the pipeline computes `aspect_ratio`, `thickness_ratio`, `top1_face_pct`, or `surface_pct` independently; this is what guarantees that ratios are consistent across probes. See ADR 0004.

### Source provenance survives end-to-end

Every `StepPart` carries `source ∈ {nauo, product_definition, product, brep, header, comments, occt_xcaf, filename, dangling, unmatched_solid, root}`. The value propagates through the assembly graph (`AssemblyNode.source`) into the manifest XML (`<part source=...>`). Telemetry can alert on regressions in the source distribution: if a file that historically parsed via `nauo` suddenly comes through `filename`, something upstream changed and the file deserves a manual look.

### Manifest round-trips exactly

`read_xml(write_xml(m))` returns a value structurally equal to `m`. Float fields use `repr(float(value))` so the textual representation is exact. The on-disk schema is versioned via the XML namespace (`https://stepalesengine.dev/manifest/1`); any breaking change increments the namespace minor. Enforced by `tests/io/test_xml_writer.py`.

### Orchestrator never raises on geometry errors

`pipeline.analyze_assembly.analyze` raises only `FileNotFoundError` and `StepParseError`. Every per-part probe is wrapped in `try / except`; failures append to `AnalyzeResult.warnings` and degrade the affected entry to `classification.label == "uncertain"` with `confidence = 0.0`. A single bad solid in a 200-part assembly cannot abort the analysis.

### Thresholds live in one file

Every tunable scalar (margin, confidence, K-factor, fillet collapse ratio, residual tolerances, OBB ratio, slice count) is imported from `manufacturing_pipeline/config/classification_variables.py`. Tests assert against the symbolic constant by name, not the literal value, so a Phase 6 sweep-driven re-tuning is a single-file change.

## Design choices reflected in ADRs

### Why score-based, not rule-based

First-match if / elif rules cannot represent confidence, hide ordering bias, and silently flip outcomes on 49.9% boundaries. The score-based classifier (`ScoreClassifier` in `manufacturing_pipeline/classification/score_classifier.py`) computes additive per-class scores from the full feature vector, picks `argmax`, and falls back to `uncertain` when the softmax probability is below `CONFIDENCE_THRESHOLD`. Every part carries a `DecisionTrace` showing exactly which features contributed how much. See ADR 0002.

### Why probe-as-feature, not classifier

Sheet-metal unfolding is geometrically powerful but fragile (cyclic graphs, missing fillets, mitred corners, non-uniform thickness). Using it as a primary classifier produces more false negatives than it solves. Instead, `UnfoldProbe.run` emits an `UnfoldResult` that feeds the score classifier as features (`unfoldable`, `n_bends`, `thickness_uniform`); it competes alongside cross-section, hole pattern, and surface-composition signals. Failures contribute zero, not a veto. See ADR 0003.

### Why name-as-feature, not veto

A laser-cut plate happens to have `DIN` in its filename. The legacy classifier short-circuited to `anders`; the new one treats `name_din_hit` as one feature among many. Geometry can still win when the evidence is strong enough. See ADR 0001.

### Why explicit assembly graph

Multi-component STEP files have hierarchical structure: an assembly contains sub-assemblies contains parts. A flat `list[StepPart]` cannot represent that, cannot compute quantity rollups, and cannot show parent / child relationships in the manifest. The `AssemblyNode` tree (`manufacturing_pipeline/assembly/graph.py`) is the explicit representation. See ADR 0006.

### Why three output writers

DXF goes to the laser-cutter shop floor (Lantek, Trumpf, Bystronic consume it directly). XML feeds ERP and BOM systems. PDF is the human-readable shop drawing for a machinist. All three are driven from the same `FlatPattern` / `AssemblyManifest` dataclasses so they stay in sync. See ADR 0005.

### Why fillet collapse before profile signature

Real CAD exports include the 2 mm fillets that every rolled profile has. The discretised wire emits roughly 44 vertices for an I-section that has 12 canonical corners. Without canonicalisation, the signature dict's `n_seg` count never matches any catalogue family and every profile classifies as `anders`. `cross_section.collapse_fillets` replaces short fillet runs with their corner intersection before the signature is computed; the original polyline is preserved on `CrossSection.polyline` for the DXF writer. See ADR 0007.

## Reading order for new contributors

1. This file (CONTEXT.md) - vocabulary and contexts.
2. `docs/ROBUST_FEATURE_DETECTION_PLAN.md` sections 0-2 - the executive summary and the architecture overview.
3. `docs/adr/` 0001-0007 in order - the seven decisions that shape the codebase.
4. `manufacturing_pipeline/cli.py` and `manufacturing_pipeline/pipeline/analyze_assembly.py` - the two entry points. Everything else hangs off these.
5. Pick one bounded context and read its module + its tests in `tests/`. Each context is small enough (a few hundred LOC) to absorb in one sitting.
