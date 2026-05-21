# Demo corpus

STEP files consumed by the static-site builder (`manufacturing_pipeline.web.static_site`). Regenerate with `python tools/make_demo_corpus.py`.

Each file is processed into a folded 3D mesh, an unfolded flat-pattern mesh (for sheet metal), technical drawings and a DXF -- see `site/`.

| File | Provenance |
| --- | --- |
| `synthetic_plate.step` | Flat rectangular plate 220 x 140 x 3 mm with four through-holes of diameter 14 mm. Generated from OCP box + cylinder primitives. Expect: plaat, unfold SUCCESS, 0 bends. |
| `synthetic_bracket.step` | Sheet-metal L-bracket: two flanges (90 / 70 mm) of 3 mm thickness joined by a single 90 degree bend, inner radius 4 mm, 60 mm wide. Generated from an OCP wire-to-face prism. Expect: plaat, unfold SUCCESS. |
| `synthetic_profile_rhs.step` | Rectangular hollow section (RHS) 120 x 80 mm, 5 mm wall, 900 mm long. Constant cross-section. Expect: profiel. |
| `synthetic_profile_tube.step` | Circular hollow section (CHS) outer diameter 100 mm, 6 mm wall, 900 mm long. Non-developable curved skin. Expect: profiel. |
| `sheet_10001073530_rev00.stp` | Real production sheet-metal part. Bundled UnfoldProbe regression fixture (tests/fixtures/step/); a multi-bend part used to validate the unfold probe against AutoPOL reference values. |
| `sheet_md_17_04194_2.stp` | Real production sheet-metal part. Bundled UnfoldProbe regression fixture (tests/fixtures/step/). |
| `nist_ctc_01_asme1_ap242-e1.stp` | NIST PMI sample model CTC-01 (AP242 edition 1) -- a public-domain machined part from the NIST MBE PMI Validation & Conformance Testing program. Exercises the 'anders' (other) class. |
