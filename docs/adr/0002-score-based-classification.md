# ADR 0002 — Score-based classification with margin + tiebreakers

**Status:** Accepted (2026-05-08)

## Context

First-match if/elif rules have no notion of confidence, no ambiguity handling, hidden ordering bias, and no tiebreaker mechanism. Misclassifications are silent and not debuggable.

## Decision

Per-class additive scoring functions over the full feature vector. Argmax with margin-based ambiguity detection. When ambiguous, run a tiebreaker pipeline (material spec → unfold probe → cross-section → profile matcher → uncertain). Calibrate scores to probabilities via softmax with temperature; below `CONFIDENCE_THRESHOLD` → `uncertain` (human review), never silently bucketed.

## Consequences

- Every part carries a `DecisionTrace` with per-feature contributions and probe results.
- Threshold tuning becomes a cost-weighted confusion-matrix sweep, not folklore.
- New label `uncertain` requires UI/BOM consumers to handle it.
- Enables future ML lift on the same feature vector without changing the architecture.
