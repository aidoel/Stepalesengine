# ADR 0003 — Sheet-metal unfold is a probe, not a classifier

**Status:** Accepted (2026-05-08)

## Context

Sheet-metal unfolding is geometrically powerful but fragile (cyclic graphs, missing fillets, mitred corners, non-uniform thickness). Using it as a primary classifier has historically produced more false negatives than it solves.

## Decision

Implement `UnfoldProbe.run(solid)` that returns a structured `UnfoldResult` with `status ∈ {SUCCESS, PARTIAL, FAILURE}`, bend count, flat area, thickness statistics, and flag dictionary. The probe **never raises**. Its result feeds the score classifier as features, where it competes alongside cross-section, hole pattern, and surface composition signals.

## Consequences

- Unfold failures no longer block classification of machined or cast parts.
- Synthetic test corpus (CadQuery-generated bent plates, U/Z/hat sections, closed tubes) anchors regression testing.
- K-factor and thickness CV are tunable via `classification_variables.py`.
