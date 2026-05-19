# ADR 0006 - Explicit assembly graph, not a flat part list

**Status:** Accepted (2026-05-16)

## Context

Multi-component STEP files have hierarchical structure. A weldment assembly contains sub-assemblies (a frame, a control box) which in turn contain parts (brackets, plates, profiles). The flat `list[StepPart]` produced by `parse_step` records every part once but encodes parent / child relationships only as a list of `product_id` strings on each `StepPart.children`. Walking this flat list does not tell us quantity rollups, sub-assembly groupings, or which solids belong to which sub-tree. The manifest XML needs to expose that hierarchy. Downstream BOM consumers need quantity-per-line ("3 of HEA 200, 1500 mm long" rather than three separate lines). The diff subcommand needs to compare structurally similar trees, not flat reorderings.

## Decision

Build an explicit tree of `AssemblyNode` records (`manufacturing_pipeline/assembly/graph.py:36`) from the flat `list[StepPart]` returned by the parser. The tree is rooted in a synthetic node (`ROOT_ID = "__ROOT__"`) so callers always start traversal from a single entry point, even when the file contains multiple top-level products. Each `AssemblyNode` carries `product_id`, `name`, `description`, `source`, `parent_id`, `depth`, `is_leaf`, `quantity`, and `children`.

`build_assembly_graph(parts)` is the only entry point. The helpers `iter_leaves(root)`, `flatten(root)`, and `aggregate_quantities(root)` expose three common traversals. Quantity rollups are computed by `aggregate_quantities` as `quantity * parent_multiplier`, so a sub-assembly used twice with three brackets inside it correctly reports `quantity = 6` for the bracket.

### Solid-to-leaf matching cascade

The geometry loader returns a `list[TopoDS_Solid]`. The leaves of the assembly tree must be paired to those solids. `match_parts_to_solids(leaves, solids, *, step_path=...)` (`manufacturing_pipeline/assembly/matcher.py`) runs a strategy cascade in descending confidence:

1. **`1to1`** (confidence `1.0`) - exactly one leaf and one solid in the file. Trivial case.
2. **`ordered`** (confidence `0.8`) - equal counts greater than one, paired by index. Works whenever the geometry loader walks solids in the same order the parser walks parts, which is the case for OCCT's STEPControl_Reader on every CAD source we have tested.
3. **`ocaf`** (confidence `0.7`) - optional name-based lookup via OCAF / XCAF shape labels. Falls through silently when the OCAF helper is not available.
4. **`by_name`** (confidence `0.4`) - greedy by index when nothing more specific applies. Marks remaining slots as `unmatched`.
5. **`unmatched`** (confidence `0.0`) - leaf has no solid, or solid has no leaf. Synthetic `AssemblyNode` placeholders surface as `MatchResult(..., method="unmatched_solid")` so orphaned geometry remains visible downstream.

Every input leaf yields exactly one `MatchResult`. Solids that no leaf claimed yield extra results so they remain visible to the orchestrator and end up in the manifest with `source = "unmatched_solid"`.

## Rationale

Without the tree, quantity rollups cannot be computed. Without the matching cascade, an assembly with two identical brackets ends up with both classified, both written to DXF, but no way to mark them as "qty 2 of the same part" in the BOM. Without the synthetic root, callers have to special-case "what if there are multiple top-level products in this file" - which happens routinely with skeleton models from SolidWorks.

The cascade is ordered cheap-to-expensive and most-confident-to-least, mirroring the parser strategy ordering. The same code shape (cascade with explicit method tags + confidence numbers) appears in `parse_step`, in `match_parts_to_solids`, and in `ScoreClassifier.classify`'s tiebreaker pipeline. This is intentional: it is the shape of "try the best strategy first, fall through to the next, never raise, always emit a typed result with provenance."

## Edge cases

- **Cyclic references** - `_dedupe` keeps the first occurrence of each `product_id`. The `visit` recursion in `build_assembly_graph` carries an `on_path: Set[str]` and prunes the recursion at the first repeated id, logging a warning. The repeated node still appears as a leaf so the tree stays traversable.
- **Dangling children** - a part references a child `product_id` that does not exist in the part list. A placeholder `AssemblyNode` is synthesised with `source = "dangling"` and `is_leaf = True`. The orchestrator logs a warning but the pipeline does not abort.
- **Duplicate IDs** - the first record wins (`_dedupe`). This mirrors how the parsing strategies treat repeated definitions: the earliest record is the most likely to carry the canonical name.
- **No NAUO at all** - `_top_level_ids` falls back to "every part is top-level"; every part becomes a leaf under the synthetic root. This is the common case for files coming through the comments / header / filename fallback strategies.
- **Repeated child id** - the same `product_id` listed twice in a parent's `children` increments `quantity` (`Counter(part.children)`), preserving NAUO multiplicity.

## Consequences

- Every classification result has a deterministic place in the manifest tree. The XML writer can emit nested `<children>` blocks.
- Quantity rollups are correct end-to-end. The BOM line for a bracket inside a twice-used sub-assembly reports `quantity = 2 * n_brackets`.
- Orphan solids (no parent in the parse) are visible, not silently dropped. The shop floor sees them as `unmatched_solid` entries instead of `where did that part go?`.
- The matcher's confidence scores propagate into `MatchResult.confidence` so the manifest can surface low-confidence pairings for review.
- `aggregate_quantities` is O(N); the graph walks are all small for the assemblies we see in practice (tens of parts, occasional file with thousands).
- Future work: when the OCAF helper becomes reliable for our regression corpus, promote it ahead of the index-based `ordered` strategy. The cascade already accommodates this.
