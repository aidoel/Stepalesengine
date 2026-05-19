# ADR 0007 - Fillet-collapse canonicalisation for profile signatures

**Status:** Accepted (2026-05-16)

## Context

The cross-section signature dict consumed by `ProfileShapeMatcher` (`manufacturing_pipeline/geometry/profile_matcher.py`) drives profile-family identification: an `n_seg = 12, sym_x ∧ sym_y, n_inner = 0` slice is an I-section; `n_seg = 8, sym_x` is a U-channel; and so on. The catalogue is built around the canonical corner count for each family.

Real CAD exports break this. A nominal I-section has 12 canonical corners (four outer flange tips, four inner flange-web transitions, four web-flange transitions on the other flange). When the modeller adds a 2 mm fillet at each inner corner (which every real rolled profile has and every CAD library emits faithfully), the discretised wire emits roughly 44 vertices instead of 12: the four fillet arcs each get sampled into eight to ten short edges. The corner-detection signature now sees `n_seg = 44`, no family matches, and the profile is classified `anders` with `designation = None` even though it is a textbook IPE 200.

The same issue hits every family with internal fillets: HEA/HEB (12 -> 44), UNP/UPE (8 -> 32), L-angles (6 -> 18), T-bars (8 -> 32). The catalogue cannot enumerate every possible fillet-discretisation count; the signature has to canonicalise the polyline before counting corners.

## Decision

Before computing the signature dict in `cross_section.compute_signature`, run `collapse_fillets(polyline, scale_ref=min(bbox_h, bbox_b))` to replace short fillet runs with their corner intersection. The collapsed polyline is then fed to `_count_corners`, `_has_reflective_symmetry`, `_has_point_symmetry`, and `_orthogonality_ratio`. The original polyline is preserved on `CrossSection.polyline` so downstream code (DXF export, debugging, hash) sees the un-mutated geometry.

The fillet collapse is also applied inside `ProfileShapeMatcher.fit_dimensions` when fitting `(h, b, t_w, t_f)` against the catalogue: dimension fitting needs the canonical corner positions, not the arc samples.

### Threshold

`PROFILE_FILLET_COLLAPSE_RATIO = 0.15` of `min(bbox_h, bbox_b)` (`manufacturing_pipeline/config/classification_variables.py:34`).

- Edges below `0.15 * min(bbox_h, bbox_b)` are candidate fillet edges.
- Runs of contiguous short edges form candidate fillet groups.
- A run is collapsed only if all three of these hold:
  - The cumulative turning angle of its interior vertices is at least `_FILLET_RUN_DEG_MIN = 60 deg` (so a tight 90 deg fillet clearly qualifies, a near-straight chain of noisy vertices does not).
  - Each individual vertex turn is below `_FILLET_VERTEX_DEG_MAX = 20 deg` (so a sharp 90 deg corner with a single transition vertex is left alone).
  - The flanking long edges intersect (parallel edges produce no corner replacement).
  - The chord length of the run is below `ratio * scale_ref` (large radii on tubular profiles like CHS stay intact).

The threshold was chosen by inspection: an IPE 200 has flange thickness 8.5 mm, web thickness 5.6 mm, inner fillet radius 12 mm, so `min(bbox_h, bbox_b) = 100 mm`. The 12 mm fillet chord is 12% of 100 mm, well within the 15% limit. A CHS 88.9x4 has bbox 88.9 mm and a chord that is the full circle - the chord limit is 13.3 mm so it is preserved. The number is empirical and lives in `classification_variables.py` because Phase 6 of the master plan tunes it on a held-out corpus.

## Rationale

Without fillet collapse, family identification fails for every real-world profile. With it, the signature returns to the canonical corner counts and the catalogue matches first try. The alternative (enumerating every possible fillet-discretisation count in the catalogue) explodes combinatorially with CAD source and curve sampling density; the alternative-alternative (fitting circles to short arc runs and reasoning about radii) is more correct but more code and more failure modes. Collapse is the cheap, deterministic, testable choice.

Keeping the original polyline on `CrossSection.polyline` matters: the DXF writer should faithfully draw the manufactured shape, fillets and all, not the abstracted matcher view. Only the matcher and the signature dict operate on the collapsed view.

## Edge cases

- **No fillets** - `is_short.any()` is `False`; the function returns the input polyline unchanged.
- **All short edges** - `is_short.all()` is `True`; no flanking long edges exist, no collapse possible, returns input unchanged.
- **Parallel flank edges** - `_line_line_intersection` returns `None`; the run is skipped.
- **Large radii** (CHS, hand-drawn curves) - chord exceeds `chord_limit`; the run is left intact. This is what keeps tubular profiles correctly classified as `CHS` rather than as a polygon with N tiny corners.
- **Discretised arc near a sharp corner** - rejected by `max_turn >= _FILLET_VERTEX_DEG_MAX` because the single transition vertex carries a 90 deg angle. The sharp corner stays.
- **Multiple fillets on the same wire** - each run is collapsed independently; the deterministic ordering (start scan from the first long edge) ensures identical input produces identical output across slices.

## Consequences

- `ProfileShapeMatcher.match` reliably identifies family on real CAD exports for IPE/HEA/HEB/UNP/UPE/L/T/Z/RHS/SHS/CHS.
- The signature dict's `n_seg_pre_collapse` field is preserved (`compute_signature` records it) so debugging tools can see how aggressive the collapse was on any given slice.
- Threshold tuning is a one-line change in `classification_variables.py`; tests assert against the symbolic constant rather than the literal `0.15`.
- The fillet collapse is deterministic: identical input always produces identical output. This is tested in `tests/geometry/test_cross_section.py`.
- Future work: when the catalogue gains a fillet-radius column per entry, the collapse can return the implied radius so `ProfileMatch.fitted_dims["r"]` is populated. Today the radius is recovered from the catalogue entry only, not measured from the polyline.
