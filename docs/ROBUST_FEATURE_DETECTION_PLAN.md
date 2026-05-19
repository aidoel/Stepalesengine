# Robust Feature Detection & Classification — Master Plan

**Status:** Draft v1.0  
**Date:** 2026-05-08  
**Scope:** Re-architect the manufacturing classification pipeline so that feature extraction, sheet-metal unfold detection, profile recognition, and `plaat`/`profiel`/`anders` classification are **robust on any STEP file**, regardless of CAD origin, naming quality, or geometric peculiarities.

---

## 0. Executive Summary

The current pipeline (see `docs/archive/classification/CLASSIFICATION_ARCHITECTURE.md`) is **name-first and first-match**. When the STEP parser fails (e.g. `31686-080.stp`), classification falls back to a brittle geometry rule chain that produces silent misclassifications (10 plaat vs expected 8). The single root cause is architectural: name detection vetoes geometry, geometry rules veto each other, and there is no way to reason about ambiguity, confidence, or competing evidence.

This document specifies a complete rebuild around five principles:

1. **Parse defensively.** A STEP parser must never return `None`. It cascades through six naming strategies (NAUO → PD → PRODUCT → BREP → header → comments) plus an OCAF fallback, and always emits at least one `StepPart` record with a `source` provenance tag.
2. **Extract uniformly.** A single `FeatureExtractor.extract(solid)` call yields a rich `ManufacturingFeatures` vector — geometry, topology, cross-section signature, hollow detection, hole pattern — used by every downstream consumer.
3. **Probe, don't decide.** Sheet-metal unfolding, sweep detection, hole pattern, and profile cross-section matching are *probes* that emit features, not classifiers that emit verdicts.
4. **Score, don't match.** Per-class additive scorers consume the full feature vector. Margin-based ambiguity detection escalates close calls through a tiebreaker pipeline. Low-confidence outputs are surfaced as `uncertain` for human review, never silently bucketed into `anders`.
5. **Trace everything.** Every part carries a structured `DecisionTrace` with per-feature contributions, scores, probabilities, margin, tiebreakers run, and model version. The BOM row links to the trace.

The plan is organised into ten phases, with the first three (parser hardening, feature extractor, score classifier) sufficient to fix the `31686-080` regression and to upgrade *every* file's classification quality.

---

## 1. Goals, Non-Goals, Success Metrics

### 1.1 Goals

- **G1** — `parse_step()` returns a non-empty list of `StepPart` records for every well-formed STEP file in the regression corpus, regardless of AP version (203 / 214 / 242), CAD source, or naming conventions.
- **G2** — `FeatureExtractor.extract()` yields the same feature schema for every solid, with all fields populated (defaults documented), no exceptions for "weird" topology.
- **G3** — `ScoreClassifier.classify()` returns a `ClassificationResult` with `label ∈ {plaat, profiel, anders, uncertain}`, full trace, and calibrated `confidence ∈ [0, 1]`.
- **G4** — Standard profiles (DIN 1025/1026, EN 10210-2, EN 10056, EN 10055) are recognised by *cross-section geometry alone*, without depending on names.
- **G5** — Sheet-metal parts are recognised by an **unfold probe** that succeeds, partially succeeds, or fails — feeding the score classifier as a feature, not a verdict.
- **G6** — Every misclassification has a debuggable trace: why each class scored what it scored, which feature dominated, what the margin was.

### 1.2 Non-Goals

- We will not attempt automatic FEA, machining strategy selection, or quoting in this iteration.
- Threaded-hole detection is heuristic only (STEP rarely models threads).
- We will not require a labelled training corpus to ship Phase 1–3; ML lift (Phase 7) is optional and additive.

### 1.3 Success Metrics

| Metric | Current | Target |
|---|---|---|
| `31686-080.stp` plaat count | 10 (wrong) | 8 (expected) |
| `parse_step()` returns `None` | sometimes | never |
| Classification confusion matrix off-diagonal | unknown (no measurement) | <5% of held-out |
| Median plate F1 on regression corpus | unknown | >0.95 |
| Median profile F1 on regression corpus | unknown | >0.90 |
| `uncertain` rate on production | n/a | <10% |
| Full trace available per part | partial | 100% |

---

## 2. Architectural Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│ STEP file                                                            │
└────────┬───────────────────────────────────────┬─────────────────────┘
         │                                       │
         ▼                                       ▼
┌──────────────────────┐              ┌────────────────────────┐
│ RobustStepParser     │              │ Geometry loader        │
│ (Section 3)          │              │ (OCP STEPControl_      │
│  • 6-strategy        │              │  Reader → Solids)      │
│    cascade           │              │                        │
│  • OCAF fallback     │              │ • Heal (ShapeFix_      │
│  • Provenance per    │              │   Shape)               │
│    record            │              │ • Explode compounds    │
│  → List[StepPart]    │              │ → List[TopoDS_Solid]   │
└──────────┬───────────┘              └─────────────┬──────────┘
           │                                        │
           │  joined on assembly graph              │
           └────────────────┬───────────────────────┘
                            ▼
              ┌──────────────────────────────┐
              │ FeatureExtractor             │
              │ (Section 4)                  │
              │  → ManufacturingFeatures     │
              └──────────────┬───────────────┘
                             │
        ┌────────────────────┼─────────────────────┐
        ▼                    ▼                     ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│ UnfoldProbe      │ │ ProfileMatcher   │ │ HoleAnalyzer     │
│ (Section 5)      │ │ (Section 6)      │ │ (Section 4.5)    │
│  → UnfoldResult  │ │  → ProfileMatch  │ │  → HolePattern   │
└────────┬─────────┘ └────────┬─────────┘ └────────┬─────────┘
         │                    │                    │
         └────────────────────┼────────────────────┘
                              ▼
                ┌────────────────────────────┐
                │ ScoreClassifier            │
                │ (Section 7)                │
                │  • per-class scorers       │
                │  • margin & tiebreakers    │
                │  • calibrated confidence   │
                │  → ClassificationResult    │
                │    + DecisionTrace         │
                └─────────────┬──────────────┘
                              ▼
                ┌────────────────────────────┐
                │ BOMItem                    │
                │  • part_class              │
                │  • confidence              │
                │  • trace_link              │
                └────────────────────────────┘
```

The pipeline is **DAG-shaped**, not sequential. Probes run in parallel on the same feature vector and converge into the score classifier. There is no first-match short-circuit.

---

## 3. Robust STEP Parser

### 3.1 Problem

`parse_step_assembly_structure()` in `manufacturing_pipeline/analysis/assembly_analysis.py:635` returns `None` when:

- No `NEXT_ASSEMBLY_USAGE_OCCURRENCE` entries exist (single compound).
- Names live only in `PRODUCT_DEFINITION` body or `MANIFOLD_SOLID_BREP.name`.
- AP203 vs AP214 vs AP242 quirks (e.g. `SHAPE_ASPECT.name` only).
- File encoded in cp1252 / latin-1 (common from German CAD).
- Standard labels written as `DIN1026`, `DIN-1026`, or `DIN EN ISO 4014`.

### 3.2 Six-Strategy Cascade

Treat naming as an ordered fallback chain. Return on first non-empty, non-junk result:

1. **NAUO** — `NEXT_ASSEMBLY_USAGE_OCCURRENCE` entries; gives the assembly tree.
2. **PRODUCT_DEFINITION** — `id` + `description` of each PD.
3. **PRODUCT** — `id` + `name` of each `PRODUCT`.
4. **BREP names** — `MANIFOLD_SOLID_BREP.name`, `SHELL_BASED_SURFACE_MODEL.name`.
5. **HEADER + filename** — `FILE_NAME`, `FILE_DESCRIPTION`, plus the file's basename.
6. **Comments** — last-resort scan of `/* ... */` blocks for standard regexes.

If all six fail, fall back to **OCAF** via `STEPCAFControl_Reader` + `XCAFDoc_DocumentTool::ShapeTool` and read `TDataStd_Name` labels — this often recovers names that pure-text scans miss.

### 3.3 Pre-Tokenisation Quirks

Handle, in order:

- BOM and encoding cascade (`utf-8-sig` → `utf-8` → `cp1252` → `latin-1`).
- Part 21 line continuation (`\\\r?\n`) — concatenate before tokenising.
- ISO 10303-21 X-encoded strings: `\X\41` → `A`, `\X2\00C4\X0\` → `Ä`, `\X4\0001F600\X0\` → astral.
- Doubled single-quotes (`''`) → `'`.
- Nested parens and quoted commas (custom `_split_args`, **not** `str.split(",")`).

### 3.4 Standard Label Regex

One tolerant pattern handles all separator variants and Dutch/European bodies:

```regex
\b
(?P<body>
    DIN(?:\s*EN(?:\s*ISO)?)?
  | EN(?:\s*ISO)?
  | ISO | NEN(?:\s*EN(?:\s*ISO)?)?
  | ASTM | ASME | JIS | GOST | BS
)
[\s\-_/]*
(?P<num>\d{2,6})
(?:[\s\-_/]*(?P<part>\d{1,4}))?
(?:[\s\-_/]*(?P<suffix>[A-Z]\d?))?
\b
```

Outputs are canonicalised: `DIN1026-2` → `DIN 1026-2`, `din-en-iso 4014` → `DIN EN ISO 4014`.

### 3.5 Dutch Vocabulary Map

Separate keyword map (do **not** stuff into the standard regex):

```python
DUTCH_PART_TYPES = {
    r"\bplaatdeel\b": "plate_part",
    r"\bprofiel\b":   "profile",
    r"\bkoker\b":     "hollow_section",
    r"\bbuis\b":      "tube",
    r"\bhoeklijn\b":  "angle_iron",
    r"\bplaat\b":     "plate",
    r"\bstrip\b":     "strip",
    r"\bgording\b":   "purlin",
    r"\bligger\b":    "beam",
    r"\bkolom\b":     "column",
}
```

### 3.6 Failure-Mode Taxonomy

| # | Mode | Detection | Mitigation |
|---|---|---|---|
| F1 | No NAUO entries | `len(nauo)==0` | Strategies 2–6 |
| F2 | Empty `PRODUCT.name` | `_is_meaningful` filter | Walk to `PD_FORMATION.description` |
| F3 | Junk names (`Part1`, `Body`) | `JUNK_NAMES` set | Header + filename fallback |
| F4 | X-encoded chars | regex match | `_decode_step_string` |
| F5 | Non-UTF-8 file | `UnicodeDecodeError` | Encoding cascade |
| F6 | Line continuations | regex preprocess | `_LINE_CONT.sub` |
| F7 | Names in `/* ... */` only | last-resort scan | `_strategy_comments` |
| F8 | AP242 SHAPE_ASPECT names | entity whitelist | Add SHAPE_ASPECT strategy 4b |
| F9 | Cyclic NAUO | tree walk infinite loop | Visited-set guard |
| F10 | Truncated mid-entity | tokenizer raises | Try/except per entity |

### 3.7 Design Rules

- **Never return `None`.** Return an empty list, a synthetic part from filename, or raise `StepParseError`. `None` propagates ambiguously.
- **Always record provenance.** Every `StepPart` has `source ∈ {nauo, product_definition, product, brep, header, comments, occt_xcaf, filename}`.
- **Separate parsing from classification.** The parser produces records; classification consumes them. Re-classify without re-parsing.
- **Cache parsed entities** keyed on `(path, mtime, size)`.

### 3.8 Library Choice

| Library | Use for | Caveats |
|---|---|---|
| `steputils` (pure-Python) | Fast text tokenisation | You build the entity graph |
| `OCP` / `pythonocc-core` | OCAF fallback (XCAF labels) | Heavy native dep |
| `cadquery` / `build123d` | Geometric features | Same OCCT cost |

Recommended split: pure-Python first; OCCT only when the cascade returns empty.

### 3.9 Testing

- **Golden fixtures**: ~30 small `.stp` files in `tests/fixtures/step/` covering AP203/214/242, six CAD sources (SolidWorks, Inventor, NX, CATIA, Fusion 360, FreeCAD), and failure modes F1–F10.
- **Property tests** with `hypothesis`: fuzz the tokeniser with synthesised Part 21 strings.
- **Regression files**: `31686-080.stp` and any other production failures become permanent test cases.
- **Source telemetry**: log `StepPart.source` distribution; alert on regressions.
- **Cross-validation**: 5% sample run through both pure-Python and OCCT fallback; alert if names disagree (`token_set_ratio < 80`).

---

## 4. Uniform Feature Extraction

### 4.1 Master Feature Vector

Single-pass extractor walks each `TopoDS_Solid` once and emits a flat dict:

| Feature | Type | Source |
|---|---|---|
| `volume` | float (mm³) | `BRepGProp.VolumeProperties_s` |
| `surface_area` | float (mm²) | `BRepGProp.SurfaceProperties_s` |
| `bbox_dims_sorted` | (L, W, T) descending | `Bnd_OBB` (oriented) preferred |
| `aspect_ratio` | L / W | derived |
| `thickness_ratio` | T / L | key plate/profile discriminator |
| `top1/top2/top3_face_pct` | 3 floats | sorted planar face areas |
| `planar_pct`, `cylindrical_pct`, `conical_pct`, `toroidal_pct`, `bspline_pct` | floats summing ~1.0 | `BRepAdaptor_Surface.GetType()` |
| `edge_count_{line,circle,ellipse,bspline}` | ints | `BRepAdaptor_Curve.GetType()` |
| `max_edge_radius`, `min_edge_radius` | floats | circular edges only |
| `hole_count`, `hole_diameters` | int, list | §4.5 |
| `sa_v_ratio` | float | `surface_area / volume^(2/3)` (scale-free) |
| `bounding_cylinder_fit_pct` | float | volume / volume of min enclosing cylinder |
| `convex_hull_volume_ratio` | float | `volume / hull_volume` |
| `cross_section_constant` | bool | §4.2 |
| `cross_section_signature` | dict | §4.3 |
| `is_hollow` | bool | §4.4 |
| `inner_shell_count` | int | §4.4 |
| `source` | str | `"brep"` or `"mesh"` |

`thickness_ratio` together with `top1_face_pct` is by far the strongest single signal for `plaat`. `cross_section_constant` plus `aspect_ratio > ~5` is the strongest signal for `profiel`.

### 4.2 Cross-Section Extraction

Slice perpendicular to the OBB's principal axis at 7 normalised positions (skip endpoints):

```python
positions = [0.10, 0.25, 0.40, 0.50, 0.60, 0.75, 0.90]
```

Each slice via `BRepAlgoAPI_Section` → wires via `ShapeAnalysis_FreeBounds.ConnectEdgesToWires_s` → 2D polyline.

Per-slice record: `area`, `perimeter`, `n_outer_segments`, `n_inner_wires`, `bbox_2d`, `shape_hash` (quantised normalised segment lengths + turning angles).

**Constant cross-section test:** coefficient of variation `cv < 0.02` for area, perimeter, and bbox_2d across slices, AND all `shape_hash` equal modulo rotation/reflection. Gates §4.3.

### 4.3 Standard Profile Recognition (Cross-Section Signature)

Run only when `cross_section_constant`. Canonicalise the middle slice's outer wire (collapse fillets with `R < 0.15·min(h, b)` into corner nodes). Compute signature: `n_seg`, `orth_ratio`, `sym_x`, `sym_y`, `hollow_ratio`, `n_inner`.

| Profile | Standard | Signature |
|---|---|---|
| **CHS** | EN 10210-2 | `n_inner=1`, both wires circular, `sym_x ∧ sym_y`, `hollow_ratio ∈ (0.4, 0.95)` |
| **RHS / SHS** | EN 10210-2 | `n_inner=1`, `n_seg_outer=4`, `orth_ratio=1`, `sym_x ∧ sym_y`; SHS if `|h−b|/h < 0.02` |
| **I / HEA / HEB / IPE** | DIN 1025 | `n_inner=0`, `n_seg=12`, `sym_x ∧ sym_y`, two flanges + central web |
| **U / UNP / UPE** | DIN 1026 | `n_inner=0`, `n_seg=8`, `sym_x` only, open side detected |
| **L / angle** | EN 10056 | `n_inner=0`, `n_seg=6`, no symmetry, two perpendicular legs |
| **T-section** | EN 10055 | `n_inner=0`, `n_seg=8`, `sym_y`, flange + web orthogonal |
| **Z-section** | — | `n_inner=0`, `n_seg=8`, point-symmetric, no reflective symmetry |

**Open-side detection:** convex hull of outer wire in 2D; the largest hull edge that is *not* part of the wire is the opening. Length / perimeter ratio further discriminates U / T / L.

**Sub-classification (HEA vs HEB vs IPE):** fit `(h, b, t_w, t_f, r)`; nearest standard table entry by Euclidean distance with tolerance ~1 mm. Reject if residual > 1.5 mm — flag `profiel_custom`.

### 4.4 Hollow vs Solid Detection

Two independent signals; require agreement:

1. **Volumetric** — `bbox_fill = volume / (L·W·T)`. Hollow profiles in `(0.05, 0.45)`, solid bars > `0.6`.
2. **Topological** — count shells via `TopExp_Explorer(solid, TopAbs_SHELL)`. Hollow B-rep has `inner_shell_count ≥ 1`. Validate with `BRepClass3d_SolidClassifier` at the centroid of an inner shell — `TopAbs_OUT` confirms void.

Combined predicate:
```python
is_hollow = (inner_shell_count >= 1) or (cross_section.n_inner >= 1)
```

### 4.5 Hole Detection

Iterate cylindrical faces. For each face with `GetType() == GeomAbs_Cylinder`:

1. Sample at `(u_mid, v_mid)`, get outward normal via `BRepLProp_SLProps`.
2. Compare with radial direction from cylinder axis. If outward normal points *toward* axis (dot < 0) → inner cylindrical surface = hole wall.
3. Group by collinear axes (1° and 0.1 mm tolerance) — counterbores yield 2–3 cylinders sharing axis.
4. Record `(diameter, depth, axis_dir, position)` per hole.

**Through vs blind:** ray-cast both axis rays against the solid; two distinct surface hits = through.

**Threaded heuristic (advisory):** `0.5·diameter < depth < 3·diameter` + entry chamfer → `likely_threaded`. Do **not** rely on this in classification.

### 4.6 Surface Composition Fingerprint

7-vector `{planar, cylindrical, conical, toroidal, spherical, bspline, other}` weighted by face area, robust across CAD exporters:

- Plates: `planar_pct > 0.95`, `top1_face_pct > 0.4`
- Open profiles: `planar_pct > 0.85`
- CHS: `cylindrical_pct > 0.9`
- RHS/SHS: `planar_pct > 0.8`, residual cylindrical from corner radii
- Cast/freeform `anders`: noticeable `bspline_pct` or `toroidal_pct > 0.1`

Cosine similarity against reference centroids = cheap fallback when other heuristics conflict.

### 4.7 Robustness Notes

- **Tessellated vs B-rep:** if no underlying `Geom_Surface`, fall back to mesh pipeline (PCA bbox, mesh volume via divergence theorem, RANSAC plane/cylinder fits via `pyransac3d`). Mark `source = "mesh"`.
- **Units:** read `SI_UNIT` / `CONVERSION_BASED_UNIT` via `STEPControl_Reader.WS().Model()`. Convert to mm at load. Never trust raw values.
- **Scale invariance:** all classification features are ratios. Absolute dims kept only for standard-profile lookup.
- **Broken topology:** run `ShapeFix_Shape` (with `ShapeFix_Wire`, `ShapeFix_Face`) before extraction; retry after `ShapeUpgrade_UnifySameDomain` on `BRepCheck_Analyzer.IsValid() == False`.
- **Multi-body compounds:** `TopExp_Explorer(c, TopAbs_SOLID)` to explode. Per-solid extraction. Assembly verdict = multiset of part verdicts (do **not** average features).
- **Tolerance:** use `BRep_Tool.Tolerance` as floor; never hardcode `1e-6`.

---

## 5. Sheet-Metal Unfold Probe

### 5.1 Geometry of Unfolding

Unfolding inverts bending. A flat blank of uniform thickness `t` is bent around straight axes; each bend converts a strip into a cylindrical patch. Unfolding reverses this.

Key parameters:

- **Thickness `t`** — pairs of antiparallel offset planar faces.
- **Bend radius `R`** — inner radius of a cylindrical face joining two planar faces.
- **Bend angle `θ`** — angle between adjacent flat-face normals.
- **K-factor `K ∈ [0, 1]`** — neutral-axis position. Defaults: 0.33 tight, 0.44–0.50 generous (mild steel).
- **Neutral radius:** `R_n = R + K·t`.
- **Bend allowance:** `BA = θ · (R + K·t)`.
- **Bend deduction:** `BD = 2·(R + t)·tan(θ/2) − BA`.

Flat area = `Σ A_flat_i + Σ (BA_j · L_j)`.

### 5.2 Algorithm

```
INPUT: solid S, K-factor K
1. detect_thickness(S) → t                         # antiparallel offset faces
2. classify_faces(S) → {planar, cylindrical, other}
3. if other faces > 5% by area and not fillet/hem: FAIL("non-developable")
4. base = argmax_area(planar)                      # largest planar face
5. G = adjacency graph (planar↔cylindrical via shared straight edge tangent)
6. if G has cycles or branching > 2: FAIL or PARTIAL
7. unfolded = {base}, queue = bends adjacent to base
8. while queue:
     bend = pop(queue)
     (P_in, P_out) = endpoints(bend)
     θ = angle(normal(P_in), normal(P_out))
     R = inner_radius(bend)
     BA = θ · (R + K·t)
     rotate P_out and descendants about bend axis by −θ
     unfolded += {bend_strip, P_out}
     enqueue bends adjacent to P_out (unvisited)
9. return UnfoldResult(success=True, n_bends, flat_area, blank_bbox)
```

### 5.3 Failure Modes

| Mode | Detection |
|---|---|
| Cyclic adjacency graph | DFS finds back-edge (closed box / tube) |
| Branching > 2 | Star-shaped flange tree; flag `branching=True` |
| Ambiguous base | Two planar faces of nearly equal area, different normals |
| Thickness variation | `σ(t)/μ(t) > 5%` |
| Non-developable surfaces | Sphere, torus, BSpline with non-zero Gaussian curvature |
| Self-intersection after unfold | 2D AABBs of unfolded faces overlap → PARTIAL |
| Disconnected sheet body | Multiple shells in `TopoDS_Solid` |

### 5.4 Robustness

- **Non-uniform thickness:** sample `t` at multiple points (`BRepExtrema_DistShapeShape`); `cv > 5%` → PARTIAL.
- **Non-orthogonal bends:** OK as long as bend axis is straight.
- **Hems:** `θ > 170°` and `R ≈ t/2`; treat normally; flag `has_hem=True`.
- **Missing fillets (sharp bends):** detect adjacent planar faces meeting at angle ≠ 180° with no intermediate cylindrical face. Insert synthetic bend with `R = 0` or default min radius. Flag `synthetic_bends=N`.
- **Mitred corners:** vertices shared by ≥ 3 cylindrical faces. Continue per branch; flag `mitred_corners=True`.
- **Tabs / louvers / embossings:** tolerate up to N small `other` faces (area < 5% of base) as `features_ignored`; otherwise FAIL.

### 5.5 OCP Building Blocks

```python
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Cylinder
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
```

- Face area: `BRepGProp.SurfaceProperties_s(face, props); props.Mass()`.
- Surface type: `BRepAdaptor_Surface(face).GetType()`.
- Cylinder radius/axis: `adaptor.Cylinder().Radius()`, `.Axis()`.
- Plane normal: `adaptor.Plane().Axis().Direction()`.
- Adjacency: `TopExp.MapShapesAndAncestors_s(shape, EDGE, FACE, map)`.
- Thickness: pair planar faces, sample `BRepExtrema_DistShapeShape` from face A to face B at centroid.

**Reference implementation:** [`shaise/FreeCAD_SheetMetal`](https://github.com/shaise/FreeCAD_SheetMetal) — `SheetMetalUnfolder.py` is a pragmatic OCCT reference for missing fillets and mitred corners.

### 5.6 Probe-as-Feature

Probe returns `UnfoldResult(status, n_bends, flat_area, thickness_mean, thickness_cv, flags, reason)`. The classifier consumes it as features:

- `success=True, n_bends ∈ [1, 8], thickness_uniform=True` → strong evidence for **bent plate**.
- `success=True, n_bends=0` → flat plate; trivially succeeds, defer to other probes.
- `success=True, constant_cross_section_along_axis=True` → **profile**, combine with sweep probe.
- `success=False (non-developable)` → likely **machined** or **cast** (`anders`).
- `status=PARTIAL` → ambiguous; small positive contribution to "bent plate" score.

The probe **must never raise**. Failures are reported in the result.

### 5.7 Synthetic Test Corpus

Generate ground-truth parts in CadQuery:

1. Flat plate 100×200×2 mm → `n_bends=0`, `flat_area=20000`.
2. L-bracket (one 90° bend, R=3, t=2, K=0.44) → `n_bends=1`, BA = `(π/2)·(3+0.44·2) ≈ 6.09 mm`.
3. U-channel, Z-bend, hat section.
4. Box with four flanges (branching).
5. Closed tube (cyclic) → expect FAIL.
6. Hemmed edge, mitred-corner box.
7. Tapered "sheet" (variable t) → expect PARTIAL.
8. Machined pocket part → FAIL with `non_developable_faces > 0`.
9. Extruded I-beam → FAIL or success-as-profile.

Tolerance: 0.5% on area, exact on bend count.

---

## 6. Profile Cross-Section Matcher

See §4.3. The matcher is a separate module because its standard tables (DIN 1025 IPE/HEA/HEB/HEM, DIN 1026 UNP/UPE, EN 10210-2 RHS/SHS/CHS, EN 10056 L, EN 10055 T) need to be versioned and updatable independently.

### 6.1 Standard Tables

Stored as YAML / JSON, one file per standard:

```
data/profiles/
  din_1025_i.yaml       # IPE 80, IPE 100, ..., HEA 100, HEB 100, ...
  din_1026_u.yaml       # UNP 50, ..., UNP 400, UPE 80, ...
  en_10210_2_rhs.yaml   # RHS 40×20×2, ..., RHS 400×200×16
  en_10210_2_shs.yaml
  en_10210_2_chs.yaml
  en_10056_l.yaml
  en_10055_t.yaml
```

Each entry: `{designation, h, b, t_w, t_f, r, weight_per_m, area_section}`.

### 6.2 Match Procedure

1. Receive `CrossSection` (canonicalised polyline, signature).
2. Identify shape family (CHS / RHS / SHS / I / U / L / T / Z) from signature.
3. Fit family-specific dimensions from polyline geometry.
4. Look up nearest entry by Euclidean distance in `(h, b, t_w, t_f)`.
5. If residual ≤ 1 mm → confident match; return `designation`.
6. If residual ∈ (1, 1.5] → low-confidence match; flag for review.
7. If residual > 1.5 mm → `profiel_custom`.

### 6.3 Output

```python
@dataclass
class ProfileMatch:
    family: str                      # "I" | "U" | "L" | "T" | "Z" | "RHS" | "SHS" | "CHS"
    standard: str                    # "DIN 1025" etc.
    designation: Optional[str]       # "HEA 200" or None
    fitted_dims: dict                # {"h": ..., "b": ..., "t_w": ..., ...}
    residual_mm: float
    confidence: float                # exp(-residual / 0.5)
```

---

## 7. Score-Based Classifier

### 7.1 Why Score-Based

First-match rules have:

- No ambiguity handling (0.51 vs 0.99 treated identically).
- Hidden ordering bias (a laser-cut plate named `DIN-bracket-12mm.stp` gets vetoed to `anders`).
- No tiebreakers when signals disagree.
- Brittle thresholds (49.9% silently flips outcome).
- Not explainable.
- Name acts as veto, not prior.

Score-based fixes all of these by computing **independent per-class scores from the full feature vector** and combining via argmax + margin. Every feature contributes to every class.

### 7.2 Per-Class Scoring

```
score_plaat   = +w1·top1_face_pct
              + w2·sigmoid(thickness_ratio < 0.1)
              + w3·unfoldable
              − w4·aspect_ratio_norm
              − w5·name_profile_hit

score_profiel = +w1·cross_section_constant
              + w2·aspect_ratio_norm
              + w3·name_profile_hit
              − w4·top1_face_pct

score_anders  = +w1·name_din_hit
              + w2·vendor_code_present
              + w3·low_volume_to_bbox
              + w4·bspline_pct
              − w5·unfoldable
```

All features feed all classes. **Name is a contributor, never a gate.**

### 7.3 Margin & Ambiguity

```python
ranked = sorted(scores.items(), key=lambda x: -x[1])
margin = ranked[0][1] - ranked[1][1]
ambiguous = margin < MARGIN_THRESHOLD   # default 0.15
```

### 7.4 Tiebreaker Pipeline

When `ambiguous`, run cheap-to-expensive probes:

1. **Material spec hint** — `S235`, `AlMg3` from PLM/metadata.
2. **Unfold probe** — success ⇒ `plaat`.
3. **Cross-section sweep** — constant signature ⇒ `profiel`.
4. **Profile matcher** — confident match ⇒ `anders` (purchased standard profile).
5. **Confidence fallback** — all probes inconclusive → `uncertain`.

### 7.5 Calibration

Raw scores → probabilities via softmax with temperature `T`:

```python
p[c] = exp(score[c] / T) / Σ_c' exp(score[c'] / T)
```

Tune `T` on held-out data so reliability diagram matches. Optional: Platt scaling or isotonic regression once labelled corpus exists.

Decision rule:

```python
if max(probs.values()) < CONF_THRESHOLD:   # default 0.65
    label = "uncertain"
else:
    label = argmax(probs)
```

### 7.6 Decision Trace Schema

```json
{
  "part_id": "ASM-0042-12",
  "label": "plaat",
  "confidence": 0.91,
  "scores": {"plaat": 1.42, "profiel": 0.31, "anders": 0.18},
  "probabilities": {"plaat": 0.71, "profiel": 0.16, "anders": 0.13},
  "margin": 1.11,
  "ambiguous": false,
  "tiebreakers_run": [],
  "contributions": [
    {"feature": "top1_face_pct", "class": "plaat",   "value": 0.78, "delta": +0.62},
    {"feature": "unfoldable",    "class": "plaat",   "value": true, "delta": +0.40},
    {"feature": "name_din_hit",  "class": "anders",  "value": 1.0,  "delta": +0.20},
    {"feature": "aspect_ratio",  "class": "profiel", "value": 3.2,  "delta": +0.15}
  ],
  "probe_results": {
    "unfold":  {"status": "success", "n_bends": 2, "flat_area": 18234.5},
    "profile": {"family": null, "designation": null, "residual_mm": null},
    "holes":   {"hole_count": 4, "diameters": [10.0, 10.0, 10.0, 10.0]}
  },
  "model_version": "rules-1.4.0",
  "needs_review": false
}
```

The BOM row surfaces `label`, `confidence`, and a `?` icon that opens the trace.

### 7.7 Confusion-Matrix-Driven Tuning

Hand-tuning thresholds is folklore. Instead:

1. Sweep `MARGIN_THRESHOLD` and `CONF_THRESHOLD` on a grid.
2. Compute confusion matrix per cell on held-out set.
3. Optimise **cost-weighted** objective:
   ```
   cost = 1·FP(plaat→profiel) + 5·FP(anders→plaat) + 0.1·uncertain_rate
   ```
   (Misclassifying purchased as plate is expensive — triggers phantom production order.)

### 7.8 Optional ML Lift (Phase 7)

When labelled corpus ≥ 200 parts, train LightGBM on the *same* feature vector:

```python
import lightgbm as lgb
model = lgb.LGBMClassifier(
    n_estimators=300, max_depth=4, learning_rate=0.05,
    class_weight="balanced", min_child_samples=5,
)
```

Ensemble: `final_score = 0.5·rule_score + 0.5·ml_score`. Keeps explainability while letting ML correct systematic biases.

### 7.9 Active Learning

```
production parts → classifier → confidence
                                    │
                  high ─────────────┴──────────── low
                   │                               │
                  use                         queue for label
                                                   │
                                            labelled batch (≥20)
                                                   │
                                       retrain weights / GBM
                                                   │
                                       shadow-deploy → A/B → ship
```

Weekly retraining cadence; low-confidence + rules-vs-ML disagreement = highest-information samples.

---

## 8. Data Model

```python
# parser
@dataclass
class StepPart:
    product_id: str
    name: str
    description: str = ""
    children: list[str] = field(default_factory=list)
    source: str = "unknown"         # provenance

# extraction
@dataclass
class ManufacturingFeatures:
    volume: float
    surface_area: float
    bbox_dims_sorted: tuple
    aspect_ratio: float
    thickness_ratio: float
    face_area_top: list             # top1, top2, top3 pct
    surface_pct: dict
    edge_counts: dict
    edge_radius: tuple
    hole_count: int
    hole_diameters: list
    sa_v_ratio: float
    bounding_cylinder_fit_pct: float
    convex_hull_volume_ratio: float
    cross_section_constant: bool
    cross_section_signature: dict
    is_hollow: bool
    inner_shell_count: int
    source: str = "brep"            # "brep" | "mesh"

# probes
@dataclass
class UnfoldResult:
    status: UnfoldStatus            # SUCCESS | PARTIAL | FAILURE
    n_bends: int
    flat_area: float
    blank_bbox: Optional[tuple]
    thickness_mean: float
    thickness_cv: float
    flags: dict
    reason: str

@dataclass
class ProfileMatch:
    family: str
    standard: str
    designation: Optional[str]
    fitted_dims: dict
    residual_mm: float
    confidence: float

@dataclass
class HolePattern:
    hole_count: int
    diameters: list
    holes: list                     # detailed (diameter, depth, axis, position)

# classification
@dataclass
class Contribution:
    feature: str
    cls: str
    value: float
    delta: float

@dataclass
class DecisionTrace:
    scores: dict
    probabilities: dict
    margin: float
    ambiguous: bool
    contributions: list[Contribution]
    tiebreakers_run: list[str]
    probe_results: dict
    model_version: str

@dataclass
class ClassificationResult:
    label: str                      # plaat | profiel | anders | uncertain
    confidence: float
    trace: DecisionTrace
```

---

## 9. Module Layout

```
manufacturing_pipeline/
  parsing/
    __init__.py
    step_parser.py            # RobustStepParser (cascade + OCAF)
    step_tokenizer.py         # _read_step, _tokenize_entities, X-decode
    step_strategies.py        # _strategy_nauo, ..., _strategy_comments
    standard_label.py         # DIN/EN/ISO regex + canonicaliser
    dutch_vocabulary.py       # plaatdeel, profiel, koker, ...
    occt_fallback.py          # STEPCAFControl_Reader path
    types.py                  # StepPart, StepParseError
  geometry/
    __init__.py
    geometry_loader.py        # OCP STEPControl_Reader → solids, ShapeFix
    feature_extractor.py      # FeatureExtractor.extract()
    cross_section.py          # CrossSection, slicing, shape_hash
    profile_matcher.py        # ProfileShapeMatcher + standard tables
    hole_analyzer.py          # HoleAnalyzer
    unfold_probe.py           # UnfoldProbe, BendDetector, Bend
    shape_health.py           # ShapeFix wrappers, validity checks
    types.py                  # ManufacturingFeatures, Bend, etc.
  classification/
    __init__.py
    score_classifier.py       # ScoreClassifier
    scorers.py                # per-class scoring functions + weights
    tiebreakers.py            # tiebreaker pipeline
    calibration.py            # softmax, Platt, isotonic
    decision_trace.py         # DecisionTrace, Contribution, JSON serialise
    types.py                  # ClassificationResult
  data/
    profiles/
      din_1025_i.yaml
      din_1026_u.yaml
      en_10210_2_rhs.yaml
      en_10210_2_shs.yaml
      en_10210_2_chs.yaml
      en_10056_l.yaml
      en_10055_t.yaml
  config/
    classification_variables.py   # thresholds, weights, MARGIN_THRESHOLD
  pipeline/
    analyze_assembly.py           # orchestrator: parser → loader → extractor → probes → classifier → BOM

tests/
  fixtures/
    step/                         # ~30 small files covering AP203/214/242, F1–F10
    synthetic/                    # CadQuery-generated unfold corpus
  parsing/
    test_step_parser.py
    test_strategies.py
    test_standard_label.py
    test_property_fuzz.py         # hypothesis
  geometry/
    test_feature_extractor.py
    test_cross_section.py
    test_profile_matcher.py
    test_unfold_probe.py
    test_hole_analyzer.py
  classification/
    test_score_classifier.py
    test_calibration.py
    test_decision_trace.py
  regression/
    test_31686_080.py             # canonical regression
    test_known_files.py           # full corpus

docs/
  ROBUST_FEATURE_DETECTION_PLAN.md   # this file
  archive/classification/            # legacy v2.1 docs
  adr/
    0001-name-as-feature-not-veto.md
    0002-score-based-classification.md
    0003-unfold-probe-as-feature.md
    0004-feature-extractor-single-pass.md
```

---

## 10. Implementation Phases

### Phase 0 — Scaffolding (this PR)

- Module/test directory layout.
- `types.py` dataclasses (no behaviour).
- ADR stubs.
- `tests/fixtures/` directory.
- This master plan in `docs/`.

### Phase 1 — Robust Parser

- `step_tokenizer.py` (encoding cascade, X-decode, line continuation, `_split_args`).
- `step_strategies.py` (six strategies).
- `standard_label.py` (regex + canonicaliser).
- `dutch_vocabulary.py`.
- `occt_fallback.py`.
- `step_parser.py` orchestrator.
- Goldens for AP203/214/242 + F1–F10.
- **Exit:** `parse_step()` returns ≥ 1 record on every fixture; `31686-080.stp` no longer returns `None`.

### Phase 2 — Feature Extractor

- `geometry_loader.py` (load + heal + explode compounds + units).
- `feature_extractor.py` (master single-pass).
- `cross_section.py` (slice + canonicalise + signature).
- `hole_analyzer.py`.
- Tessellated fallback for mesh-only files.
- **Exit:** schema-stable `ManufacturingFeatures` for every fixture; CV < 1% across re-extractions.

### Phase 3 — Score Classifier (no probes yet)

- `scorers.py` with hand-tuned weights informed by current `classification_variables.py`.
- `score_classifier.py` with margin + softmax.
- `decision_trace.py`.
- **Exit:** `31686-080.stp` plate count = 8 from geometry alone; full trace per part.

### Phase 4 — Unfold Probe

- `unfold_probe.py` with `BendDetector`, `Bend`, `UnfoldResult`.
- Synthetic CadQuery corpus.
- Wire as feature input to scorer; tiebreaker plugin.
- **Exit:** unfold probe success on synthetic L/U/Z, fail on closed tube.

### Phase 5 — Profile Matcher

- `profile_matcher.py` + standard tables YAML.
- Wire as feature + tiebreaker.
- **Exit:** UNP160, HEA200, RHS 100×50×4, CHS 88.9×4 fixtures classified as `anders` with correct designation.

### Phase 6 — Calibration & Threshold Tuning

- Held-out regression corpus.
- Grid sweep + cost-weighted confusion matrix.
- Persist tuned `MARGIN_THRESHOLD`, `CONF_THRESHOLD`, weights.
- **Exit:** off-diagonal < 5% on regression corpus.

### Phase 7 — ML Lift (optional)

- LightGBM hybrid scorer.
- SHAP audit.
- Ensemble weight sweep.
- **Exit:** ensemble F1 ≥ rules-only F1 on held-out.

### Phase 8 — Active Learning

- Low-confidence queue.
- Labelling UI hook (out of scope for this repo).
- Weekly retrain CI.

### Phase 9 — Production Telemetry

- `StepPart.source` distribution alerts.
- `uncertain` rate dashboard.
- Disagreement (rules vs ML) sampling.
- Trace browser linking from BOM rows.

### Phase 10 — Cleanup

- Remove legacy first-match logic from `assembly_analysis.py`.
- Move all thresholds into `classification_variables.py` (single source of truth).
- Archive v2.1 docs.

---

## 11. Risk Register

| # | Risk | Mitigation |
|---|---|---|
| R1 | OCP/OCCT install pain in CI | Pin via conda-forge; pre-built wheels; smoke-test on macOS + Linux |
| R2 | Cross-section slicing slow | Cache per-solid; reduce to 5 slices when N parts > 100 |
| R3 | OBB unstable for near-cubic parts | Fall back to AABB when OBB principal-axis ratio < 1.2 |
| R4 | Standard tables drift | YAML versioning; validate-on-load schema |
| R5 | Calibration overfits on small corpus | Stratified 5-fold CV; reserve 20% never-seen holdout |
| R6 | "Uncertain" rate too high | Tunable; cost-weighted threshold sweep |
| R7 | Mesh-only STEP files | RANSAC fallback path |
| R8 | Threading detection false-positives | Mark advisory only; never feed classifier weights |
| R9 | Backwards compatibility with current BOM consumers | Keep `part_class` field name; add `confidence` and `trace_link` as optional |
| R10 | Hidden coupling from name veto removal | Shadow-deploy new classifier alongside old; diff outputs for 1 sprint |

---

## 12. Glossary

- **plaat** — plate (sheet metal, flat or bent).
- **profiel** — extruded profile, in-house cut to length (counts as production).
- **anders** — purchased item or machined complex part (no production from sheet/profile stock).
- **NAUO** — `NEXT_ASSEMBLY_USAGE_OCCURRENCE`, STEP entity defining the assembly tree.
- **PD** — `PRODUCT_DEFINITION`.
- **OCAF** — Open CASCADE Application Framework (XCAF labels, document storage).
- **OBB** — oriented bounding box.
- **AABB** — axis-aligned bounding box.
- **K-factor** — neutral-axis position in sheet bending, fraction of thickness.
- **BA** — bend allowance.
- **BD** — bend deduction.
- **CHS / RHS / SHS** — circular / rectangular / square hollow section.
- **UNP / UPE / IPE / HEA / HEB** — European standard rolled profiles.
- **DIN 1025 / 1026, EN 10210-2 / 10056 / 10055** — standards for I, U, hollow, angle, T sections.

---

## 13. References

- ISO 10303-21 (STEP Part 21 file format).
- DIN 1025-1..-4 (I, IPE, HEA, HEB, HEM beams).
- DIN 1026-1..-2 (UNP/UPE channels).
- EN 10210-2 (hot-finished hollow sections).
- EN 10056-1..-2 (equal-leg and unequal-leg angles).
- EN 10055 (T-bars).
- FreeCAD SheetMetal Workbench, `shaise/FreeCAD_SheetMetal`.
- OpenCascade Technology documentation.
- `docs/archive/classification/CLASSIFICATION_ARCHITECTURE.md` (v2.1, superseded).
- `docs/archive/classification/CLASSIFICATION_DECISION_TREE.md` (v2.1, superseded).

---

**End of plan.**
