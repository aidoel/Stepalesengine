# ADR 0005 - DXF + XML + PDF output writers from a single FlatPattern

**Status:** Accepted (2026-05-16)

## Context

The pipeline has three downstream consumers with incompatible needs. The laser-cutter shop floor (Lantek, Trumpf, Bystronic post-processors) ingests DXF directly off a USB stick. The ERP / BOM integration needs a structured, round-trippable record of every part, its classification, its decision trace, and its geometric features. The machinist on the press brake needs a printable shop drawing with title block, bend table, hole table, and a scaled flat-pattern view. Producing these from three separate code paths invites drift: a thickness fix in one writer that does not land in the others, layer naming that disagrees between formats, bend-table units that drift from millimetres to inches.

## Decision

Drive all three writers from the same two dataclasses: `FlatPattern` (`manufacturing_pipeline/io/dxf_writer.py:50`) for per-part geometry, and `AssemblyManifest` (`manufacturing_pipeline/io/xml_writer.py:59`) for the whole-file payload. The dataclasses are the single source of truth: any change to a field name, unit, or default propagates to all three writers simultaneously through type checking and a shared test corpus.

- `write_dxf(pattern, out_path)` and `write_assembly_dxf(patterns, out_path, nesting=...)` consume `FlatPattern`.
- `write_pdf(pattern, meta, out_path)` and `write_assembly_pdf(patterns, manifest, out_path)` consume `FlatPattern` + optional `PartDrawingMeta`.
- `write_xml(manifest, out_path)` and `read_xml(path)` consume / produce `AssemblyManifest` and round-trip exactly.

The DXF layer convention follows the laser-cutter shop-floor standard used by the major post-processors:

| Layer | Color (ACI) | Linetype | Purpose |
|---|---|---|---|
| `OUTER` | 1 (red) | Continuous | Outer cut contour |
| `INNER` | 3 (green) | Continuous | Inner cuts (holes, slots) |
| `BEND_UP` | 5 (blue) | DASHED2 | Bends folding up |
| `BEND_DOWN` | 4 (cyan) | DASHED2 | Bends folding down |
| `ANNOTATION` | 7 (white) | Continuous | Bend tables, dimensions |
| `INFO` | 7 (white) | Continuous | Title header (part name, thickness) |

Defined as a module-level dict (`LAYER_SPEC` at `manufacturing_pipeline/io/dxf_writer.py:32`) so callers can introspect or override. Bend layer is chosen per-line by reading `meta["direction"]` from the `bend_lines` tuple; `"down"` -> `BEND_DOWN`, anything else -> `BEND_UP`.

## Rationale

DXF is the de-facto shop-floor exchange format for 2D cut geometry; the three named post-processors above consume it natively with no conversion step. XML is the lingua franca for ERP / BOM integration; the schema lives under the namespace `https://stepalesengine.dev/manifest/1` so consumers can pin a version. PDF is the only format a press-brake operator can read on a printout: title block, scaled view, bend / hole tables. All three are independent runtime concerns but share one geometric truth (`FlatPattern`) and one BOM truth (`AssemblyManifest`).

The layer convention is not invented here. It follows the laser-cutter integration standard shared by Lantek Expert, Trumpf TruTops, and Bystronic ByVision: outer cut on layer 1 (red, continuous), inner cuts on layer 3 (green, continuous), bend marks on layers 4-5 (dashed), text on layer 7. Shops that already have a post-processor configured for one of these systems consume our DXF with zero setup.

## Consequences

- Any change to `FlatPattern` (new field, renamed field, changed default) MUST update all three writers and their tests. Failing to update one of the three is the single failure mode this design optimises against.
- The DXF writer rejects `thickness <= 0` at validation time; the orchestrator (`pipeline/analyze_assembly._build_flat_pattern`) substitutes `1.0` when the unfold probe returns `thickness = 0` so a thin plate without a measured thickness still exports.
- Adding a fourth format (STEP-AP242 tessellated mesh, Gerber, G-code) is a matter of writing a new writer module against the same dataclasses. No refactor of the core pipeline is required.
- The shared `_pattern_bbox` helper in `dxf_writer.py` is duplicated in `pdf_writer.py` to keep `io/` modules independent of one another; a future refactor should consolidate into `io/_geometry_utils.py` if a third consumer needs it.
- The XML schema is the canonical wire format. Breaking changes increment the namespace minor (`/manifest/1` -> `/manifest/2`); additive changes (new optional element, new attribute) are not breaking.
