# ADR 0008 - Cross-term rules in the score classifier

**Status:** Accepted (2026-05-16)

## Context

The score classifier (`manufacturing_pipeline/classification/score_classifier.py`) is per-class additive: each class gets a list of `(feature, transform, weight)` rules and the class score is the sum of `weight * transform(features[feature])`. The softmax over the three class scores gives the final probability, and parts whose winner sits below `CONFIDENCE_THRESHOLD = 0.65` degrade to `uncertain`.

Calibration against the 33-file NIST PMI corpus exposed a structural failure of the additive form. Twenty machined CTC/FTC parts (all `unfoldable = False`, none real sheet metal) ended up as `uncertain` because:

- The plaat scorer rewarded `top1_face_pct` unconditionally at weight `1.5`.
- Machined parts routinely have one face at 0.20-0.47 of the total surface area (a flattish top or boss).
- That single feature pushed the plaat score high enough that the anders winner never cleared 0.65 confidence after softmax.

Threshold sweeps could not fix this: lowering the confidence threshold leaked real ambiguous shapes into `anders`, and raising the plaat weight pushed too many machined parts into a false `plaat` label. The signal "top1 is high" only carries plate evidence in conjunction with "the part is unfoldable". Those two features have to combine multiplicatively, not additively.

## Decision

Extend the rule schema so a rule can reference either a single feature or a tuple of features:

```python
ScorerRule = Tuple[Union[str, Tuple[str, ...]], Callable[..., float], float]
```

- A `str` `feature_ref` keeps the existing single-feature semantics: `fn(features[name])`.
- A `tuple[str, ...]` `feature_ref` is a cross-term: `fn(tuple(features.get(n, 0.0) for n in feature_ref))`. Missing features default to `0.0`.

The `ScoreClassifier.classify` loop dispatches on `isinstance(feat, tuple)`. The `Contribution` dataclass keeps its `feature, cls, value, delta` schema; for cross-terms `feature` becomes the comma-joined list of names and `value` becomes the stringified tuple of values. The XML writer was extended to round-trip both float and string values in the `value` slot.

The default scorers gain two cross-terms:

- `plaat`: `(("top1_face_pct", "unfoldable"), lambda v: v[0] * (1.0 if v[1] else 0.0), 0.8)` paired with **reducing** the unconditional `top1_face_pct` weight from `1.5` to `0.7`. The total plaat reward for `top1_face_pct` is now split: 0.7 unconditional plus 0.8 gated on `unfoldable`. Real plates (both signals true) still clear the threshold; machined parts (top1 high, unfoldable false) lose the gated half.
- `anders`: `(("top1_face_pct", "unfoldable"), lambda v: v[0] * (0.0 if v[1] else 1.0), 1.5)`. "Plate-looking but not unfoldable" is treated as strong evidence of a machined part. The weight was raised from the initial 1.0 to 1.5 to push the confidence of borderline NIST cases past 0.65 without breaking the calibrated ambiguous-case test.

## Alternative rejected: full ML lift

A trained model (gradient-boosted trees or a small MLP) over the same feature dict would have learned the interaction term automatically. We rejected this for the current phase because:

- The labelled corpus is too small (33 NIST parts plus a handful of synthetic fixtures) to train any model worth deploying.
- The downstream consumers (DXF writer, decision trace, web debug viewer) all rely on per-feature contributions for explainability. A black-box classifier would force a parallel explanation path.
- Calibration variables (`SOFTMAX_TEMPERATURE`, `CONFIDENCE_THRESHOLD`) and tiebreakers are tuned by humans against fixtures. Replacing the scorer wholesale requires rebuilding that loop.

Adding explicit cross-terms keeps the scorer linear in the contributions table (each cross-term is still one row in `DecisionTrace.contributions`), preserves the `weight_jitter` sweep machinery in `manufacturing_pipeline/calibration/sweep.py` (`_scale_weights` only touches `w`, not the shape of `feat`), and stays auditable.

## Consequences

- **NIST corpus, before / after** (clean cache, 33 parts): `{uncertain: 20, anders: 13}` becomes `{anders: 30, uncertain: 3}` with zero false plates in either configuration. The three remaining `uncertain` parts (`ftc_10` and its AP203 / AP242 twins) carry `top1_face_pct = 0.144`, too low for the cross-term to push their anders confidence past 0.65; moving them would need either a richer machined-part feature (e.g. pocket count or surface complexity) or a confidence-floor adjustment.
- **Backward compatibility** is preserved: every existing single-feature rule keeps working unchanged because the `isinstance(feat, tuple)` dispatch is additive. The calibration sweep's `_scale_weights` helper still multiplies weights through both forms.
- **Schema stability**: `Contribution`'s four slots (`feature`, `cls`, `value`, `delta`) are unchanged. The `value` type annotation widens to `Union[float, str]` to admit the stringified tuple, but JSON serialisation (`asdict` plus `json.dumps`) and the XML writer's `_fv` / `_pfv` helpers handle both forms.
- **Tests**: a new `tests/classification/test_cross_terms.py` pins both rule shapes, the missing-feature fallback, the joined-feature contribution name, and the failure mode the cross-terms exist to fix. `tests/regression/test_nist_corpus.py` baselines flip five entries from `{uncertain: 1}` to `{anders: 1}`; the change is documented in the file header.
- **Open question**: the unconditional `top1_face_pct` rule on plaat carries weight 0.7 not 0. The 0.7 contribution still helps small-but-clearly-plate fixtures keep their margin against `profiel`. If a future part has `top1_face_pct = 0.45, unfoldable = False, hull_concavity = 0` (a flat machined slab with no pockets), the plaat score will pick up 0.315 from the unconditional term and the anders score will pick up 0.675 from the cross-term; the calibration test `test_ideal_plaat_requires_both_top1_and_unfoldable` pins this routing.
