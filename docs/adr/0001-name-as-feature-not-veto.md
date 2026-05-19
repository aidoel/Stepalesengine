# ADR 0001 — Name is a feature, not a veto

**Status:** Accepted (2026-05-08)

## Context

The legacy classifier short-circuits on `"DIN"`/`"EN"`/`"ISO"` substring matches in part names, returning `anders` before geometry is consulted. This causes silent misclassification when:

- The STEP parser fails (no names → veto unreachable, but plate rule then fires on a profile).
- A laser-cut plate happens to contain `"DIN"` in its name.
- Names are inconsistent across CAD exporters.

## Decision

Treat name signals (`name_din_hit`, `name_profile_hit`, `vendor_code_present`, etc.) as **additive features** in the score classifier. They contribute to scores but cannot veto geometric evidence.

## Consequences

- Robust to missing or inconsistent metadata.
- Requires removing first-match short-circuit from `assembly_analysis.py`.
- Backwards compatibility: classifier may now classify a `DIN-bracket` plate as `plaat` when geometry strongly indicates so. This is correct, but reviewers must be informed.
