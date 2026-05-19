# ADR 0004 — Single-pass uniform feature extractor

**Status:** Accepted (2026-05-08)

## Context

The legacy pipeline computes geometry in multiple places (plate detection, profile detection, standard-profile fallback) with inconsistent ratios and overlapping work. Adding a new probe required re-walking the solid.

## Decision

Implement `FeatureExtractor.extract(solid) -> ManufacturingFeatures` as the single entry point for all geometry. Walks each solid once, populates a flat dataclass (volume, OBB dims, surface composition, edge counts, cross-section signature, hole pattern, hollow detection, etc.), and is consumed unchanged by every downstream probe and the score classifier.

## Consequences

- All ratios (`thickness_ratio`, `top1_face_pct`, `cross_section_constant`, `surface_pct`) computed consistently from the same source.
- Easy to cache by `(path, mtime, size)`.
- Adding a new probe means adding fields to `ManufacturingFeatures` and a scorer entry — no re-walking.
- Mesh-only files fall back to a `source = "mesh"` path with a subset of features populated.
