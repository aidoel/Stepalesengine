"""Degraded-mode (no-OCP) behaviour of the ghost-leaf filter.

These tests deliberately do NOT import OCP or the synthetic_steps fixtures, so
they run with or without ``cadquery-ocp`` installed. They pin the contract that
``drop_unmatched_leaves`` is gated on geometry availability, not merely on a
single file's solids:

- OCP unavailable  -> every file loads zero solids, so unmatched leaves must
  *survive* as ``uncertain`` (dropping them would erase the whole BOM and break
  the documented degraded-mode promise).
- OCP available    -> a file that still yields no solids is genuinely
  empty/unreadable, so its zero-geometry leaf is dropped (the "zero parts"
  anomaly that validate_corpus relies on).

The gate is exercised by monkeypatching ``occt_available`` and feeding a
non-STEP blob, which ``load_solids`` resolves to an empty solid list on either
interpreter (ImportError without OCP; parse failure with it).
"""

from __future__ import annotations

from pathlib import Path

from manufacturing_pipeline.pipeline import analyze_assembly as aa
from manufacturing_pipeline.pipeline.analyze_assembly import AnalyzeOptions, analyze


def _write_blob(tmp_path: Path) -> Path:
    # A non-STEP file: the parser cascade still emits a fallback leaf, but no
    # geometry loads, so the leaf is unmatched with zero volume / confidence.
    path = tmp_path / "blob.step"
    path.write_text("not a STEP file at all", encoding="utf-8")
    return path


def test_unmatched_leaf_survives_as_uncertain_without_occt(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(aa, "occt_available", lambda: False)

    result = analyze(
        _write_blob(tmp_path),
        AnalyzeOptions(out_dir=tmp_path / "out", write_dxf=False, write_xml=False),
    )

    assert len(result.manifest.parts) >= 1
    assert all(e.classification.label == "uncertain" for e in result.manifest.parts)


def test_unmatched_zero_geometry_leaf_is_dropped_when_occt_available(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(aa, "occt_available", lambda: True)

    result = analyze(
        _write_blob(tmp_path),
        AnalyzeOptions(out_dir=tmp_path / "out", write_dxf=False, write_xml=False),
    )

    assert len(result.manifest.parts) == 0
