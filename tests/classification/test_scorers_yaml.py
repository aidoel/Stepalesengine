"""Tests for the YAML-backed scorer-weights loader.

The bundled ``manufacturing_pipeline/data/scorer_weights.yaml`` must produce a
``ScorerSpec`` numerically identical to what was previously hardcoded in
``manufacturing_pipeline/classification/scorers.py``. These tests pin that
equivalence, the error paths of :func:`load_scorers_from_yaml`, and the CLI
``--scorers`` wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from manufacturing_pipeline.classification.score_classifier import ScoreClassifier
from manufacturing_pipeline.classification.scorers import (
    ScorerSpec,
    boolf,
    default_scorers,
    linear,
    load_scorers_from_yaml,
)

# ---------------------------------------------------------------------------
# Reference: the pre-refactor hardcoded spec. Equivalence test pivots on this.
# ---------------------------------------------------------------------------


def _hardcoded_reference_scorers() -> ScorerSpec:
    """The hardcoded spec from before the YAML refactor.

    Replicated here verbatim so we can prove the YAML-backed
    :func:`default_scorers` is numerically identical for every probed input.
    """
    return {
        "plaat": [
            ("top1_face_pct", linear, 0.7),
            (
                ("top1_face_pct", "unfoldable"),
                lambda v: v[0] * (1.0 if v[1] else 0.0),
                0.8,
            ),
            ("unfoldable", boolf, 1.2),
            ("aspect_ratio", lambda x: -min(x, 10) / 10, 0.3),
            ("name_profile_hit", linear, -0.3),
        ],
        "profiel": [
            ("cross_section_constant", boolf, 1.5),
            ("aspect_ratio", lambda x: min(x, 10) / 10, 0.8),
            ("name_profile_hit", linear, 0.3),
            ("profile_match_designation", boolf, 0.8),
            ("top1_face_pct", linear, -0.5),
            ("unfoldable", boolf, -0.8),
        ],
        "anders": [
            ("name_din_hit", linear, 0.5),
            ("vendor_code_present", linear, 0.5),
            ("bspline_pct", linear, 1.5),
            ("hole_density", linear, 1.2),
            ("hull_concavity", linear, 1.0),
            (
                ("top1_face_pct", "unfoldable"),
                lambda v: v[0] * (0.0 if v[1] else 1.0),
                1.5,
            ),
            ("pocket_complexity", linear, 1.4),
            ("unfoldable", boolf, -0.3),
        ],
    }


# A grid of probe features that exercises every rule in the spec, including
# the cross-terms. Each entry is one ``features`` dict we feed both specs.
_PROBE_FEATURES: list[dict] = [
    {  # ideal plate
        "top1_face_pct": 0.92,
        "unfoldable": True,
        "aspect_ratio": 2.0,
        "cross_section_constant": False,
        "name_profile_hit": 0.0,
        "name_din_hit": 0.0,
        "vendor_code_present": 0.0,
        "bspline_pct": 0.0,
        "hole_density": 0.0,
        "hull_concavity": 0.0,
        "profile_match_designation": 0.0,
        "pocket_complexity": 0.0,
    },
    {  # ideal profile
        "top1_face_pct": 0.2,
        "unfoldable": False,
        "aspect_ratio": 12.0,
        "cross_section_constant": True,
        "name_profile_hit": 1.0,
        "name_din_hit": 0.0,
        "vendor_code_present": 0.0,
        "bspline_pct": 0.0,
        "hole_density": 0.0,
        "hull_concavity": 0.0,
        "profile_match_designation": 1.0,
        "pocket_complexity": 0.0,
    },
    {  # plate-looking but not unfoldable - tests the negative cross-term
        "top1_face_pct": 0.45,
        "unfoldable": False,
        "aspect_ratio": 1.5,
        "cross_section_constant": False,
        "name_profile_hit": 0.0,
        "name_din_hit": 0.0,
        "vendor_code_present": 0.0,
        "bspline_pct": 0.0,
        "hole_density": 0.0,
        "hull_concavity": 0.0,
        "profile_match_designation": 0.0,
        "pocket_complexity": 0.0,
    },
    {  # freeform - lots of bspline + vendor name evidence
        "top1_face_pct": 0.15,
        "unfoldable": False,
        "aspect_ratio": 1.2,
        "cross_section_constant": False,
        "name_profile_hit": 0.0,
        "name_din_hit": 1.0,
        "vendor_code_present": 1.0,
        "bspline_pct": 0.8,
        "hole_density": 0.4,
        "hull_concavity": 0.3,
        "profile_match_designation": 0.0,
        "pocket_complexity": 0.6,
    },
    {  # empty dict - every feature defaults to 0/False
        "unfoldable": False,
    },
]


def _scores(spec: ScorerSpec, features: dict) -> dict:
    """Run ``ScoreClassifier`` with ``spec`` and return the raw scores dict."""
    res = ScoreClassifier(scorers=spec, conf_thr=0.0).classify(features)
    return res.trace.scores


# ---------------------------------------------------------------------------
# Test 1: bundled YAML -> identical spec to the hardcoded reference
# ---------------------------------------------------------------------------


def test_bundled_yaml_matches_hardcoded_spec_numerically():
    yaml_spec = default_scorers()
    reference = _hardcoded_reference_scorers()

    # Same class names, same order, same number of rules per class.
    assert list(yaml_spec.keys()) == list(reference.keys())
    for cls in reference:
        assert len(yaml_spec[cls]) == len(reference[cls]), cls

    # Same shape of feature_refs and same weights.
    for cls, rules in reference.items():
        for (yfeat, _yfn, yw), (rfeat, _rfn, rw) in zip(yaml_spec[cls], rules):
            assert yfeat == rfeat
            assert yw == pytest.approx(rw)

    # And, the test that actually matters for "behaviour unchanged": running
    # both specs through ScoreClassifier on a probe grid yields identical
    # per-class scores.
    for features in _PROBE_FEATURES:
        yaml_scores = _scores(yaml_spec, features)
        ref_scores = _scores(reference, features)
        assert set(yaml_scores) == set(ref_scores)
        for cls in ref_scores:
            assert yaml_scores[cls] == pytest.approx(ref_scores[cls]), (
                cls,
                features,
            )


# ---------------------------------------------------------------------------
# Test 2: malformed YAML rejected with a clear error
# ---------------------------------------------------------------------------


def test_malformed_yaml_raises_value_error(tmp_path: Path):
    bad = tmp_path / "broken.yaml"
    bad.write_text("classes: {plaat: [\nunterminated", encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        load_scorers_from_yaml(bad)
    msg = str(exc.value).lower()
    assert "malformed yaml" in msg
    assert str(bad) in str(exc.value)


def test_missing_classes_mapping_raises_value_error(tmp_path: Path):
    bad = tmp_path / "no_classes.yaml"
    bad.write_text("schema_version: 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing or empty 'classes' mapping"):
        load_scorers_from_yaml(bad)


# ---------------------------------------------------------------------------
# Test 3: unknown transform name -> ValueError mentioning the bad name
# ---------------------------------------------------------------------------


def test_unknown_transform_raises_value_error(tmp_path: Path):
    bad = tmp_path / "unknown_transform.yaml"
    bad.write_text(
        "classes:\n"
        "  plaat:\n"
        "    - feature: top1_face_pct\n"
        "      transform: definitely_not_a_real_transform\n"
        "      weight: 1.0\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc:
        load_scorers_from_yaml(bad)
    assert "definitely_not_a_real_transform" in str(exc.value)


# ---------------------------------------------------------------------------
# Test 4: custom YAML at tmp_path overrides bundled defaults
# ---------------------------------------------------------------------------


def test_custom_yaml_overrides_default_weights(tmp_path: Path):
    custom = tmp_path / "custom.yaml"
    custom.write_text(
        "classes:\n"
        "  plaat:\n"
        "    - feature: top1_face_pct\n"
        "      transform: linear\n"
        "      weight: 9.0\n"
        "  profiel:\n"
        "    - feature: cross_section_constant\n"
        "      transform: bool\n"
        "      weight: 0.01\n"
        "  anders:\n"
        "    - feature: bspline_pct\n"
        "      transform: linear\n"
        "      weight: 0.0\n",
        encoding="utf-8",
    )
    spec = load_scorers_from_yaml(custom)
    assert spec["plaat"][0][2] == 9.0
    assert spec["profiel"][0][2] == 0.01
    assert spec["anders"][0][2] == 0.0

    # Behavioural sanity: with the bumped plaat weight a moderately flat part
    # (top1=0.5) should out-score the bundled defaults on the plaat axis.
    bundled = default_scorers()
    features = {"top1_face_pct": 0.5, "unfoldable": False}
    assert _scores(spec, features)["plaat"] > _scores(bundled, features)["plaat"]


# ---------------------------------------------------------------------------
# Test 5: end-to-end CLI with --scorers flips the classification
# ---------------------------------------------------------------------------


pytest.importorskip("OCP.STEPCAFControl")


def test_cli_analyze_with_custom_scorers_changes_label(tmp_path: Path):
    """A bundled-defaults run labels the plate as ``plaat``; a tuned YAML that
    zeroes the plate signals + boosts ``anders`` must produce a different
    label on the same STEP file."""
    from manufacturing_pipeline.cli import main as cli_main
    from manufacturing_pipeline.io.xml_writer import read_xml
    from tests.fixtures.synthetic_steps import write_flat_plate_step

    step = write_flat_plate_step(tmp_path / "plate.step", l=100.0, w=60.0, t=3.0)

    # Run 1: bundled defaults.
    out_default = tmp_path / "default"
    out_default.mkdir()
    rc = cli_main(
        [
            "analyze",
            str(step),
            "--out-dir",
            str(out_default),
            "--no-cache",
            "--quiet",
        ]
    )
    assert rc == 0
    default_manifest = read_xml(out_default / "manifest.xml")
    default_label = default_manifest.parts[0].classification.label

    # Run 2: a custom YAML that murders the plaat scorer and heavily rewards
    # ``anders`` on the same features. The plate cannot still be labeled
    # ``plaat`` once the plate weights collapse.
    custom = tmp_path / "anti_plate.yaml"
    custom.write_text(
        "classes:\n"
        "  plaat:\n"
        "    - feature: top1_face_pct\n"
        "      transform: linear\n"
        "      weight: -10.0\n"
        "    - feature: unfoldable\n"
        "      transform: bool\n"
        "      weight: -10.0\n"
        "  profiel:\n"
        "    - feature: cross_section_constant\n"
        "      transform: bool\n"
        "      weight: 0.1\n"
        "  anders:\n"
        "    - feature: top1_face_pct\n"
        "      transform: linear\n"
        "      weight: 10.0\n"
        "    - feature: unfoldable\n"
        "      transform: bool\n"
        "      weight: 10.0\n",
        encoding="utf-8",
    )
    out_custom = tmp_path / "custom"
    out_custom.mkdir()
    rc = cli_main(
        [
            "analyze",
            str(step),
            "--out-dir",
            str(out_custom),
            "--no-cache",
            "--scorers",
            str(custom),
            "--quiet",
        ]
    )
    assert rc == 0
    custom_manifest = read_xml(out_custom / "manifest.xml")
    custom_label = custom_manifest.parts[0].classification.label

    assert default_label != custom_label, (
        f"--scorers had no effect: default={default_label} custom={custom_label}"
    )
    assert custom_label != "plaat", (
        f"after slashing plate weights the part still classified as plaat: "
        f"{custom_label}"
    )
