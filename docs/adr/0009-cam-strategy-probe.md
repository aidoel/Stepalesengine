# ADR 0009 - CAM strategy probe

**Status:** Accepted (2026-05-16)

## Context

The pipeline already turns a STEP file into a structured manifest: per-part classification (`plaat` / `profiel` / `anders` / `uncertain`), manufacturing features, hole patterns, optional unfold result, optional profile match, and PMI (geometric tolerances, dimensions, datums, surface finishes). Downstream consumers (estimator, ERP, scheduling) still have to translate that bag of measurements back into shop-floor language: "drill ten 6 mm holes, ream the two with tight position, mill the pocket, finish-pass the top face". That last step lived in spreadsheets and tacit knowledge. New parts could not be quoted without a human walking each PMI table to imagine the operation list.

The signal needed to mechanise that translation is already on the manifest. Classification picks the family (sheet vs machined vs purchased). The hole pattern enumerates drills. The unfold result counts bends. PMI tells us which holes are reamed (tight position) and which faces are finish-passed (Ra below the working tolerance). A probe that consumes those existing fields lets the manifest answer "what does this part need on a CNC?" in a single hop.

A black-box model is the wrong tool today: the labelled corpus is the same 33-part NIST set that already constrains the classifier, and the contributions table that powers the web debugger demands per-rule explainability. A rules-and-thresholds probe with explicit `Operation.reason` strings preserves that auditability and fits the same explanation surface as the rest of the pipeline.

## Decision

Add a `manufacturing_pipeline/cam/` module with a single public entry point:

```python
recommend(features, pmi, classification, holes, unfold=None, profile_match=None) -> MachiningStrategy
```

Routing is keyed on the classifier label:

- `plaat` -> `primary_process="sheet_metal"`. Emit `laser_cut` for the outer contour plus all holes, one `press_brake_bend` per bend in the unfold result, and a single closing `deburr`.
- `profiel` -> `primary_process="purchased"`. Emit a single `inspect` op; the part is bought in a standard profile (HEA, RHS, CHS) and needs no shop time beyond receipt inspection.
- `anders` -> `primary_process="machining"`. Emit `mill_contour` (outer), `drill` per hole, `mill_pocket` if `1 - convex_hull_volume_ratio > 0.4` (same threshold as the classifier already uses for "anders" detection), and `inspect` at the end.
- anything else -> `primary_process="unknown"` with a single `inspect` operation.

PMI refines the machining route:

- `GeometricTolerance(type="position", magnitude_mm < 0.05)` on a hole adds a `ream` (priority 140) and an inspect for that hole (priority 210). A part-global position tolerance (`applied_to == ""` or `"part"`) applies to every hole.
- `SurfaceFinish(value < 1.6 um)` anywhere on the part adds one `finish_pass` operation at priority 200, so it groups at the end of milling. `Ra < 0.8 um` flips the rationale string to "mirror finish".
- `Datum` entries produce one inspect op per datum at priority 220 plus a notes string `"setup against datums A,B,C"`.
- A thread fragment (`M6`, `UNC`, etc) found in `applied_to` matching a hole_ref emits a `tap` operation; the strategist uses substring matching only, so callers attaching annotated CAD models drive thread-tapping.

Time estimation is a deliberate heuristic, not a quote:

| Operation | Cost |
|---|---|
| `laser_cut` | 0.5 min per metre of cut path (bbox perimeter + hole circumferences) |
| `drill` | 0.3 min per hole |
| `ream` | 0.5 min per hole |
| `mill_pocket` | 5.0 min per detected pocket (flat fee) |
| `press_brake_bend` | 0.5 min per bend |
| `finish_pass` | 5.0 min flat fee |
| `inspect` | 2.0 min per datum (or one flat fee when no datums declared) |

The `MachiningStrategy.setup_count` defaults to 1; it bumps to 2 when holes span more than one rounded axis direction (suggesting a re-fixturing for back-side drilling).

The probe is total: any internal exception falls through to the `unknown` plan with a logged warning. Callers (`pipeline/analyze_assembly.analyze`) never have to wrap the call in their own try/except. The orchestrator runs `recommend()` after PMI is attached so tolerances and finishes are visible to the probe.

The XML schema (`https://stepalesengine.dev/manifest/1`) gains an additive `<machining-strategy>` block on each `<part>` with attributes `primary-process`, `setup-count`, `material-hint`, `estimated-time-min`. Each operation lives in an `<operation op="..." feature-ref="..." priority="..." reason="...">` element with child `<param name="..." value="..."/>` rows; a `<notes>` text node closes the block. The block is omitted when no strategy is attached, so older manifests parse unchanged. The web detail template renders the new card with a per-op pill colour (cutting reds, drilling blues, forming purples, finishing greens) and a notes row at the bottom.

## Rationale

Classification-keyed routing is the smallest decision tree that fits the four primary processes seen in the corpus. The hull-concavity threshold for pocket detection reuses the same cutoff the classifier already exposes (`hull_concavity > 0.4` is what pushes a candidate from `plaat` to `anders`); using a different number would mean a part labelled "machined because it has pockets" might not get a `mill_pocket` op, which would be silently inconsistent.

PMI refinements only run on the machining route because they only make sense there: a tight position tolerance on a hole-cut into a sheet-metal blank still gets laser-cut, not reamed. Sheet-metal hole tolerances are quoted differently and would be a separate probe.

Time estimates intentionally err conservative. Real laser-cut speeds depend on material thickness, beam power, and assist gas; 0.5 min/m is roughly the midpoint of mild-steel cut times at 2-3 mm. Real drill cycles depend on diameter and depth; 0.3 min/hole is roughly correct for a 6-12 mm through-hole in aluminium. The numbers are tunable module-level constants (`TIME_LASER_CUT_PER_M`, `TIME_DRILL_PER_HOLE`, ...) so calibration against an internal corpus is a one-line change.

The pill colour palette in the web detail card uses the same hue families as the existing `tol-pill` PMI palette so the visual language stays consistent: blues for drilling (linked to the existing `tol-position` blue), reds for cutting (mirrors the DXF outer-contour layer red), purples for forming (parallel to the existing `tol-parallelism` purple), greens for finishing (parallel to the `tol-flatness` green).

## Consequences

- A new optional attribute `PartManifestEntry.strategy: MachiningStrategy | None` propagates through `write_xml` / `read_xml` round-trip and through the disk cache (the cache pickles whole `AnalyzeResult`s, so the new field rides along for free). Test `test_xml_round_trip_preserves_strategy_fields` pins the schema.
- The probe runs unconditionally for every entry that has a classification. For uncertain / ghost rows it produces the cheap `unknown` plan, costing one `Operation` allocation each.
- Adding a new operation kind (e.g. `wire_edm` for hard-steel slots) is an enum extension on `Operation.op` plus a CSS pill colour plus a routing branch in `strategist`. No schema migration is required.
- An NIST CTC sample with N drilled holes and a tight-position datum table produces, for example, `mill_contour + N x drill + 1-2 ream + 1 mill_pocket + per-datum inspect`, totalling roughly `2.0 + 0.3N + 0.5R + 5.0P + 2.0D` min. For `ctc_01` with 8 holes, 2 reamed, 1 pocket, 3 datums (A/B/C): about `2.0 + 2.4 + 1.0 + 5.0 + 6.0 = 16.4 min` of recommended wall-clock. The number is a planning hint - real shops will re-quote against their own machine times.
- **Open question**: the thread-callout matcher is currently substring-only on `applied_to`. Real AP242 PMI carries thread spec in a separate annotation we have not yet extracted; once `pmi/extractor.py` surfaces a `ThreadCallout` type, the strategist's `_thread_callout_holes` helper should switch from string heuristics to that typed entry. The XML schema does not need to change.
- **Open question**: the time estimates have not been calibrated against real shop floor data. A `calibration/cam_sweep.py` analogous to the classifier's sweep machinery could fit the seven constants against a labelled corpus of "actual minutes / per part", but we lack the corpus today.
