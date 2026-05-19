"""Tiebreaker probes for ambiguous score-based decisions.

``material_spec_tiebreaker`` is intentionally kept as a reserved hook for
the Phase 6 calibration work documented in docs/ROBUST_FEATURE_DETECTION_PLAN.md;
it is not yet wired into the orchestrator and remains a placeholder so that
the public symbol stays stable for downstream consumers and future
calibration experiments. See docs/CONTEXT.md for the planned signature.
"""

from __future__ import annotations


def material_spec_tiebreaker(features: dict) -> str | None:
    """Reserved hook for material-spec based tiebreaking (Phase 6).

    Currently a no-op: returns None so the tiebreaker chain falls through
    to the next probe. The full implementation lands with Phase 6 once a
    standard material-spec vocabulary is in place.
    """
    return None


def unfold_tiebreaker(features: dict) -> str | None:
    if features.get("unfoldable"):
        return "plaat"
    return None


def cross_section_tiebreaker(features: dict) -> str | None:
    if features.get("cross_section_constant"):
        return "profiel"
    return None


def profile_match_tiebreaker(features: dict) -> str | None:
    if features.get("profile_match_designation"):
        return "anders"
    return None
