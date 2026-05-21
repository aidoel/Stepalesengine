"""Generate the demo corpus consumed by the static-site builder.

Writes four synthetic STEP files (a flat plate, a sheet-metal L-bracket and two
hollow profiles) into ``corpus/`` and copies three real CAD fixtures, so the
corpus exercises every classification element the engine emits:

* ``plaat``   -- flat plate + bent sheet metal (synthetic + real)
* ``profiel`` -- rectangular and circular hollow section (synthetic)
* ``anders``  -- a machined NIST PMI sample model (real)

Run once and commit ``corpus/``; the static-site build only reads it::

    python tools/make_demo_corpus.py

Requires the ``[occt]`` extra (OpenCascade) for the synthetic builders.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORPUS = REPO / "corpus"
FIXTURES = REPO / "tests" / "fixtures" / "step"


def _load_synthetic_steps():
    """Load ``tests/fixtures/synthetic_steps.py`` as a standalone module.

    Loading by file path (rather than importing ``tests.fixtures...``) keeps
    this build tool decoupled from the test package layout.
    """
    path = REPO / "tests" / "fixtures" / "synthetic_steps.py"
    spec = importlib.util.spec_from_file_location("synthetic_steps", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# (filename, provenance) for the real CAD fixtures copied verbatim.
_REAL = [
    (
        "sheet_10001073530_rev00.stp",
        "Real production sheet-metal part. Bundled UnfoldProbe regression "
        "fixture (tests/fixtures/step/); a multi-bend part used to validate "
        "the unfold probe against AutoPOL reference values.",
    ),
    (
        "sheet_md_17_04194_2.stp",
        "Real production sheet-metal part. Bundled UnfoldProbe regression "
        "fixture (tests/fixtures/step/).",
    ),
    (
        "nist_ctc_01_asme1_ap242-e1.stp",
        "NIST PMI sample model CTC-01 (AP242 edition 1) -- a public-domain "
        "machined part from the NIST MBE PMI Validation & Conformance Testing "
        "program. Exercises the 'anders' (other) class.",
    ),
]


def _write_provenance(notes: list[tuple[str, str]]) -> None:
    lines = [
        "# Demo corpus",
        "",
        "STEP files consumed by the static-site builder "
        "(`manufacturing_pipeline.web.static_site`). Regenerate with "
        "`python tools/make_demo_corpus.py`.",
        "",
        "Each file is processed into a folded 3D mesh, an unfolded flat-pattern "
        "mesh (for sheet metal), technical drawings and a DXF -- see `site/`.",
        "",
        "| File | Provenance |",
        "| --- | --- |",
    ]
    for name, prov in notes:
        lines.append(f"| `{name}` | {prov} |")
    lines.append("")
    (CORPUS / "PROVENANCE.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    CORPUS.mkdir(exist_ok=True)
    syn = _load_synthetic_steps()
    notes: list[tuple[str, str]] = []

    syn.write_flat_plate_step(
        CORPUS / "synthetic_plate.step",
        l=220.0,
        w=140.0,
        t=3.0,
        with_holes=[(55, 45, 14), (165, 45, 14), (55, 95, 14), (165, 95, 14)],
        name="Synthetic flat plate",
    )
    notes.append(
        (
            "synthetic_plate.step",
            "Flat rectangular plate 220 x 140 x 3 mm with four through-holes of "
            "diameter 14 mm. Generated from OCP box + cylinder primitives. "
            "Expect: plaat, unfold SUCCESS, 0 bends.",
        )
    )
    print("wrote synthetic_plate.step")

    syn.write_lbracket_step(
        CORPUS / "synthetic_bracket.step",
        leg_a=90.0,
        leg_b=70.0,
        t=3.0,
        bend_radius=4.0,
        width=60.0,
        name="Synthetic L-bracket",
    )
    notes.append(
        (
            "synthetic_bracket.step",
            "Sheet-metal L-bracket: two flanges (90 / 70 mm) of 3 mm thickness "
            "joined by a single 90 degree bend, inner radius 4 mm, 60 mm wide. "
            "Generated from an OCP wire-to-face prism. Expect: plaat, unfold "
            "SUCCESS.",
        )
    )
    print("wrote synthetic_bracket.step")

    syn.write_profile_step(
        CORPUS / "synthetic_profile_rhs.step",
        family="RHS",
        h=120.0,
        b=80.0,
        t=5.0,
        length=900.0,
        name="Synthetic RHS profile",
    )
    notes.append(
        (
            "synthetic_profile_rhs.step",
            "Rectangular hollow section (RHS) 120 x 80 mm, 5 mm wall, 900 mm "
            "long. Constant cross-section. Expect: profiel.",
        )
    )
    print("wrote synthetic_profile_rhs.step")

    syn.write_profile_step(
        CORPUS / "synthetic_profile_tube.step",
        family="CHS",
        h=100.0,
        b=100.0,
        t=6.0,
        length=900.0,
        name="Synthetic CHS profile",
    )
    notes.append(
        (
            "synthetic_profile_tube.step",
            "Circular hollow section (CHS) outer diameter 100 mm, 6 mm wall, "
            "900 mm long. Non-developable curved skin. Expect: profiel.",
        )
    )
    print("wrote synthetic_profile_tube.step")

    for name, prov in _REAL:
        src = FIXTURES / name
        if src.is_file():
            shutil.copy2(src, CORPUS / name)
            notes.append((name, prov))
            print(f"copied {name}  ({src.stat().st_size // 1024} KB)")
        else:
            print(f"WARNING: real fixture missing, skipped: {src}")

    _write_provenance(notes)
    print(f"\ncorpus ready: {CORPUS}  ({len(notes)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
