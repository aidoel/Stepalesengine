# Perf finding: 803139-0010.step (626s outlier)

## File facts

- Path: `~/Downloads/stepfile/803139-0010.step`
- Size: 4.5 MB
- Reported in NIST run: **626.5 s wall time** (vs ~5 s median per file in the corpus).
- The file is an assembly: parser yields **142 product records**; geometry loader yields **3018 individual solids** (every fastener, washer, weld is its own body).

## Phase timing

`parse_step` and `load_solids` are not the bottleneck:

| Phase | Wall | Notes |
|---|---|---|
| parse_step | 0.55 s | Pure-Python cascade, fine |
| load_solids | 20.17 s | Reads + heals 3018 solids, fine for the volume |

Per-solid loop timing (sampled on first 10 solids, projected linearly to 3018):

| Probe | Per part | Projected (3018) |
|---|---|---|
| FeatureExtractor.extract | **143 ms** | **432 s** |
| UnfoldProbe.run | **167 ms** | **505 s** |
| slice_solid + ProfileShapeMatcher.match | 72 ms | 216 s |
| HoleAnalyzer.analyze | 2 ms | 5 s |

The projected sum exceeds the actual wall time because the assembly has many small/identical solids that go through the same cold-path cost. Sample variance also inflates the projection. But the conclusion is unambiguous: **the unfold probe and feature extractor dominate**.

## Diagnosis

The pipeline runs every probe on every solid, regardless of whether the probe can possibly succeed. For this assembly:

- Most of the 3018 solids are fasteners (M6 bolts, washers, nuts) — none are sheet-metal so `UnfoldProbe` will always fail, but only after spending 167 ms walking faces, classifying surfaces, attempting BFS.
- Many are duplicates (the same M6 bolt appears 50+ times) — we re-do all the work each time.
- `slice_solid` runs 7 cross-section slices on every part, even though a hexagonal bolt clearly isn't a profile.

## Proposed fix (not yet implemented)

Two complementary changes:

1. **Cheap pre-filter before expensive probes.** Inside the orchestrator's per-part loop, compute the cheap features first (`volume`, `bbox`, `face_count`, `surface_pct`) and short-circuit:
   - `face_count > 200` → set `unfold_status=FAILURE(reason="too_complex")` without running the probe.
   - `volume / (L*W*T) > 0.7` (almost-solid bar) → set `unfold_status=FAILURE(reason="not_sheet")` without running.
   - `face_count <= 6` (cube-like fastener body) → set `unfold_status=FAILURE` without running.
   - Similar guard before `slice_solid`: only run if `aspect_ratio > 3` (anything not elongated cannot be a profile).

2. **Per-solid identity cache** keyed on a cheap shape signature `(volume, surface_area, n_faces)` rounded to coarse buckets. If we've seen this signature before, reuse the prior classification result. For an assembly with 50 identical bolts, we'd do real work once and reuse 49 times.

Expected speedup on this file: **~10×** (from ~600 s to ~60 s), assuming ~80 % of the solids are duplicates of ~20 unique designs.

## Status

Diagnosis only. The fix is architecturally clean and worth implementing, but it's a real change to `_process_pair` in `analyze_assembly.py` and warrants its own task with regression test coverage.
