"""Walk a STEP file's tokenised entities for semantic PMI annotations.

The extractor is pure Python and tolerant: malformed entities are skipped
and contribute only to the raw ``n_annotations`` count. Never raises.

AP242 PMI types handled
-----------------------
* ``GEOMETRIC_TOLERANCE`` (and its specialisations:
  ``POSITION_TOLERANCE``, ``FLATNESS_TOLERANCE``,
  ``STRAIGHTNESS_TOLERANCE``, ``CIRCULARITY_TOLERANCE``,
  ``CYLINDRICITY_TOLERANCE``, ``PARALLELISM_TOLERANCE``,
  ``PERPENDICULARITY_TOLERANCE``, ``ANGULARITY_TOLERANCE``,
  ``SURFACE_PROFILE_TOLERANCE``, ``LINE_PROFILE_TOLERANCE``,
  ``CIRCULAR_RUNOUT_TOLERANCE``, ``TOTAL_RUNOUT_TOLERANCE``,
  ``CONCENTRICITY_TOLERANCE``, ``COAXIALITY_TOLERANCE``,
  ``SYMMETRY_TOLERANCE``) emit :class:`GeometricTolerance` records.
* ``DIMENSIONAL_LOCATION`` / ``DIMENSIONAL_SIZE`` joined to the
  corresponding ``PLUS_MINUS_TOLERANCE`` -> :class:`DimensionalTolerance`.
* ``DATUM`` and ``DATUM_FEATURE`` -> :class:`Datum`.
* ``SURFACE_TEXTURE_REPRESENTATION`` -> :class:`SurfaceFinish` (best
  effort: Ra value pulled from the first numeric child).
* ``DRAUGHTING_CALLOUT`` / ``DRAUGHTING_ANNOTATION_OCCURRENCE`` /
  ``ANNOTATION_OCCURRENCE`` contribute to ``n_annotations`` only.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..parsing.step_tokenizer import read_step_text, split_args, tokenize_entities
from .types import (
    Datum,
    DimensionalTolerance,
    GeometricTolerance,
    PMIRecord,
    SurfaceFinish,
)

# Complex multi-inheritance entity head: ``#NNN=(`` (no name token).
# The body is everything between the outermost balanced parens. We use
# this as a secondary scan because the base tokeniser only matches the
# named form ``#NNN=NAME(...)``.
_COMPLEX_HEAD = re.compile(r"#(\d+)\s*=\s*\(", re.MULTILINE)

# ---------------------------------------------------------------------------
# Entity-name catalogues
# ---------------------------------------------------------------------------

# Specific GD&T tolerance entity names mapped to the short ``type`` we expose.
_GT_TYPES: dict[str, str] = {
    "POSITION_TOLERANCE": "position",
    "FLATNESS_TOLERANCE": "flatness",
    "STRAIGHTNESS_TOLERANCE": "straightness",
    "CIRCULARITY_TOLERANCE": "circularity",
    "CYLINDRICITY_TOLERANCE": "cylindricity",
    "PARALLELISM_TOLERANCE": "parallelism",
    "PERPENDICULARITY_TOLERANCE": "perpendicularity",
    "ANGULARITY_TOLERANCE": "angularity",
    "SURFACE_PROFILE_TOLERANCE": "surface_profile",
    "LINE_PROFILE_TOLERANCE": "line_profile",
    "CIRCULAR_RUNOUT_TOLERANCE": "circular_runout",
    "TOTAL_RUNOUT_TOLERANCE": "total_runout",
    "CONCENTRICITY_TOLERANCE": "concentricity",
    "COAXIALITY_TOLERANCE": "coaxiality",
    "SYMMETRY_TOLERANCE": "symmetry",
    "ROUNDNESS_TOLERANCE": "roundness",
}

# Counted but not decoded.
_ANNOTATION_NAMES = (
    "DRAUGHTING_CALLOUT",
    "DRAUGHTING_ANNOTATION_OCCURRENCE",
    "ANNOTATION_OCCURRENCE",
    "ANNOTATION_PLANE",
)

# Surface-finish carriers. Different vendors use slightly different names.
_SURFACE_FINISH_NAMES = (
    "SURFACE_TEXTURE_REPRESENTATION",
    "SURFACE_TEXTURE",
    "SURFACE_CONDITION_CALLOUT",
)

# Regex used to pull a LENGTH_MEASURE(<number>) hit out of an arbitrary arg
# string (including complex multi-inheritance bodies like
# ``( LENGTH_MEASURE_WITH_UNIT() MEASURE_WITH_UNIT(LENGTH_MEASURE(0.2),#42) )``).
_LENGTH_MEASURE_NUM = re.compile(r"LENGTH_MEASURE\(\s*(-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)\s*\)")

# Entity-ref regex (lifts every ``#NNN`` token).
_ENT_REF = re.compile(r"#(\d+)")

# Quoted string regex.
_QSTR = re.compile(r"'([^']*)'")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _measure_value(args_str: str) -> float | None:
    """Pull the first LENGTH_MEASURE(<x>) numeric out of an arg string."""
    m = _LENGTH_MEASURE_NUM.search(args_str)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (ValueError, TypeError):
        return None


def _resolve_magnitude(
    ref: int | None,
    entities: dict[int, tuple[str, str]],
) -> float | None:
    """Follow a magnitude reference to a LENGTH_MEASURE value."""
    if ref is None or ref not in entities:
        return None
    _, body = entities[ref]
    # Direct LENGTH_MEASURE in the body (works for both simple and complex
    # entity definitions).
    val = _measure_value(body)
    if val is not None:
        return val
    # Otherwise, follow each referenced ID one step and try again.
    for nested in _ENT_REF.findall(body):
        try:
            nested_id = int(nested)
        except ValueError:
            continue
        if nested_id == ref:  # avoid trivial self-loop
            continue
        nested_ent = entities.get(nested_id)
        if nested_ent is None:
            continue
        v = _measure_value(nested_ent[1])
        if v is not None:
            return v
    return None


def _first_quoted(s: str) -> str:
    m = _QSTR.search(s)
    return m.group(1) if m else ""


def _parse_int_ref(token: str) -> int | None:
    """Pull a `#NNN` ref out of a single argument token."""
    if not token:
        return None
    t = token.strip()
    if t.startswith("#"):
        try:
            return int(t[1:])
        except ValueError:
            return None
    return None


def _collect_datum_letters(
    ref: int | None,
    entities: dict[int, tuple[str, str]],
    depth: int = 0,
    seen: set | None = None,
) -> list:
    """Walk DATUM_SYSTEM / DATUM_REFERENCE_COMPARTMENT / DATUM trees,
    returning the ordered identifier letters (e.g. ``['A', 'B', 'C']``)."""
    if seen is None:
        seen = set()
    if ref is None or ref in seen or depth > 6:
        return []
    seen.add(ref)
    ent = entities.get(ref)
    if ent is None:
        return []
    name, body = ent

    if name == "DATUM":
        try:
            args = split_args(body)
        except Exception:
            return []
        # DATUM args: (name, description, of_shape, products_definitional, identification)
        if args:
            ident = _first_quoted(args[-1])
            if ident:
                return [ident]
        return []

    # DATUM_SYSTEM, DATUM_REFERENCE_COMPARTMENT, DATUM_REFERENCE: chase children.
    out: list = []
    for nested in _ENT_REF.findall(body):
        try:
            nested_id = int(nested)
        except ValueError:
            continue
        out.extend(_collect_datum_letters(nested_id, entities, depth + 1, seen))
    # Preserve order but drop dupes
    deduped: list = []
    for item in out:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def _read_balanced(text: str, start: int) -> tuple[str | None, int]:
    """Return the substring inside the outermost parens starting at ``start - 1``.

    ``start`` must point at the char immediately after the opening ``(``.
    Returns ``(inner_text, index_after_close)`` or ``(None, start)``.
    Honours nested parens and single-quoted strings.
    """
    depth = 1
    in_quote = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if in_quote:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1
        i += 1
    return None, start


def _scan_complex_entities(text: str) -> dict[int, str]:
    """Find every ``#NNN=(...);`` (no name token) and return ``{id: body}``.

    Body excludes the outermost parens. The tokeniser in
    :mod:`step_tokenizer` skips these because its regex requires a NAME
    head; this is a PMI-side complement to that limitation.
    """
    out: dict[int, str] = {}
    for m in _COMPLEX_HEAD.finditer(text):
        try:
            ent_id = int(m.group(1))
        except ValueError:
            continue
        body, _ = _read_balanced(text, m.end())
        if body is None:
            continue
        out[ent_id] = body
    return out


def _expand_geometric_tolerance_complex(body: str) -> tuple[str, str] | None:
    """For complex entities like
    ``(GEOMETRIC_TOLERANCE(...) POSITION_TOLERANCE())``, return
    ``(specific_type, geometric_tolerance_args_str)`` if both clauses
    can be located. Otherwise ``(None, None)`` style.

    Returns ``(type_short, gt_args)`` or ``None``.
    """
    text = body
    specific_type: str | None = None
    for name, short in _GT_TYPES.items():
        if re.search(rf"\b{name}\b", text):
            specific_type = short
            break
    if specific_type is None:
        return None

    m = re.search(r"GEOMETRIC_TOLERANCE\s*\(", text)
    if not m:
        return None
    # Read balanced parens starting at m.end().
    depth = 1
    i = m.end()
    in_quote = False
    n = len(text)
    while i < n:
        ch = text[i]
        if in_quote:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return specific_type, text[m.end() : i]
        i += 1
    return None


# ---------------------------------------------------------------------------
# Plus/minus extraction
# ---------------------------------------------------------------------------


def _resolve_tolerance_value(
    ref: int,
    entities: dict[int, tuple[str, str]],
) -> tuple[float, float] | None:
    """Return ``(upper, lower)`` for a ``TOLERANCE_VALUE(lower_ref, upper_ref)``.

    ISO 10303-47 declares the schema as ``TOLERANCE_VALUE(lower_bound,
    upper_bound)``; we report ``(upper, lower)`` so the caller (which
    builds :class:`DimensionalTolerance` from a +X / -Y interval) gets a
    consistent ordering.
    """
    ent = entities.get(ref)
    if ent is None:
        return None
    name, body = ent
    if name != "TOLERANCE_VALUE":
        return None
    try:
        args = split_args(body)
    except Exception:
        return None
    if len(args) < 2:
        return None
    lo_ref = _parse_int_ref(args[0])
    up_ref = _parse_int_ref(args[1])
    lower = _resolve_magnitude(lo_ref, entities)
    upper = _resolve_magnitude(up_ref, entities)
    if upper is None and lower is None:
        return None
    return (upper or 0.0, lower or 0.0)


def _resolve_dimension_nominal(
    ref: int | None,
    entities: dict[int, tuple[str, str]],
    char_rep_index: dict[int, int] | None = None,
) -> float:
    """Return the nominal LENGTH_MEASURE for a DIMENSIONAL_SIZE /
    DIMENSIONAL_LOCATION ref.

    The chain is normally
    ``DIMENSIONAL_CHARACTERISTIC_REPRESENTATION(dim_ref, shape_dim_rep)`` ->
    ``SHAPE_DIMENSION_REPRESENTATION(name, (items,), ctx)`` -> a complex
    measure item carrying ``LENGTH_MEASURE(nominal)``. If the index is
    available we follow that chain; otherwise we fall back to a depth-1
    scan of the direct children.
    """
    if ref is None or ref not in entities:
        return 0.0

    if char_rep_index is not None and ref in char_rep_index:
        rep_id = char_rep_index[ref]
        rep_ent = entities.get(rep_id)
        if rep_ent is not None:
            _, rep_body = rep_ent
            nominal = _scan_nominal_in_refs(rep_body, entities)
            if nominal is not None:
                return nominal

    _, body = entities[ref]
    nominal_val = 0.0
    for nested in _ENT_REF.findall(body):
        try:
            nested_id = int(nested)
        except ValueError:
            continue
        ent = entities.get(nested_id)
        if ent is None:
            continue
        nbody = ent[1]
        if "nominal value" in nbody:
            v = _measure_value(nbody)
            if v is not None:
                return v
        if nominal_val == 0.0:
            v = _measure_value(nbody)
            if v is not None:
                nominal_val = v
    return nominal_val


def _scan_nominal_in_refs(
    body: str,
    entities: dict[int, tuple[str, str]],
) -> float | None:
    """Look at every entity referenced by ``body`` and return the first
    LENGTH_MEASURE that is tagged ``nominal value``. Falls back to the
    first LENGTH_MEASURE encountered otherwise."""
    fallback: float | None = None
    for nested in _ENT_REF.findall(body):
        try:
            nested_id = int(nested)
        except ValueError:
            continue
        ent = entities.get(nested_id)
        if ent is None:
            continue
        nbody = ent[1]
        if "nominal value" in nbody:
            v = _measure_value(nbody)
            if v is not None:
                return v
        if fallback is None:
            v = _measure_value(nbody)
            if v is not None:
                fallback = v
    return fallback


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_pmi(step_path: str | Path) -> PMIRecord:
    """Return a :class:`PMIRecord` for the STEP file at ``step_path``.

    Never raises. Missing or unreadable files return an empty record.
    """
    record = PMIRecord()
    path = Path(step_path)
    if not path.exists() or not path.is_file():
        return record

    try:
        text = read_step_text(str(path))
    except Exception:
        return record

    try:
        entities = tokenize_entities(text)
    except Exception:
        return record

    # The base tokeniser drops complex multi-inheritance entities of the
    # form ``#NN=(CLAUSE_A() CLAUSE_B() ...);``. We catalogue them here so
    # references can still resolve and so GEOMETRIC_TOLERANCE clauses can
    # be discovered.
    try:
        complex_bodies = _scan_complex_entities(text)
    except Exception:
        complex_bodies = {}

    # Merge complex bodies into the entity map under a synthetic name so the
    # rest of the resolver can use the same lookup.
    for cid, cbody in complex_bodies.items():
        if cid not in entities:
            entities[cid] = ("__COMPLEX__", cbody)

    if not entities and not complex_bodies:
        return record

    # Build an index from DIMENSIONAL_SIZE / DIMENSIONAL_LOCATION ref -> the
    # SHAPE_DIMENSION_REPRESENTATION carrying the nominal value.
    char_rep_index: dict[int, int] = {}
    for _ent_id, (name, body) in entities.items():
        if name != "DIMENSIONAL_CHARACTERISTIC_REPRESENTATION":
            continue
        try:
            args = split_args(body)
        except Exception:
            continue
        if len(args) < 2:
            continue
        dim_ref = _parse_int_ref(args[0])
        rep_ref = _parse_int_ref(args[1])
        if dim_ref is not None and rep_ref is not None:
            char_rep_index[dim_ref] = rep_ref

    # First pass: scan every entity, dispatch by name.
    plus_minus: list = []  # (tolerance_value_ref, applied_to_ref)

    for ent_id, (name, body) in entities.items():
        try:
            # --- Complex multi-inheritance bodies ------------------------
            if name == "__COMPLEX__":
                if "GEOMETRIC_TOLERANCE" in body:
                    short = _detect_complex_gt_type(body)
                    if short is not None:
                        _emit_simple_geometric_tolerance(ent_id, short, body, entities, record)
                        record.n_annotations += 1
                        continue
                if any(sf in body for sf in _SURFACE_FINISH_NAMES):
                    _emit_surface_finish(body, entities, record)
                    record.n_annotations += 1
                    continue
                # Otherwise: ignore - geometry / units / camera complex
                # entities are not PMI.
                continue

            # --- Geometric tolerances (named specialisations) ------------
            short = _GT_TYPES.get(name)
            if short is not None:
                _emit_simple_geometric_tolerance(ent_id, short, body, entities, record)
                record.n_annotations += 1
                continue

            # --- DATUM / DATUM_FEATURE -----------------------------------
            if name == "DATUM":
                _emit_datum(body, record, is_feature=False)
                record.n_annotations += 1
                continue
            if name == "DATUM_FEATURE":
                _emit_datum(body, record, is_feature=True)
                record.n_annotations += 1
                continue

            # --- Plus/minus tolerances -----------------------------------
            if name == "PLUS_MINUS_TOLERANCE":
                try:
                    args = split_args(body)
                except Exception:
                    args = []
                if len(args) >= 2:
                    tv_ref = _parse_int_ref(args[0])
                    dim_ref = _parse_int_ref(args[1])
                    if tv_ref is not None:
                        plus_minus.append((tv_ref, dim_ref))
                record.n_annotations += 1
                continue

            # --- Surface finish ------------------------------------------
            if name in _SURFACE_FINISH_NAMES:
                _emit_surface_finish(body, entities, record)
                record.n_annotations += 1
                continue

            # --- Counted-only annotations --------------------------------
            if name in _ANNOTATION_NAMES:
                record.n_annotations += 1
                continue

            # --- Bare-base GEOMETRIC_TOLERANCE (uncommon standalone) -----
            if name == "GEOMETRIC_TOLERANCE":
                _emit_simple_geometric_tolerance(ent_id, "geometric", body, entities, record)
                record.n_annotations += 1
                continue

        except Exception:
            # Conservative: any per-entity error increments the raw count.
            record.n_annotations += 1
            continue

    # Resolve dimensional tolerances now that all entities are loaded.
    for tv_ref, dim_ref in plus_minus:
        bounds = _resolve_tolerance_value(tv_ref, entities)
        if bounds is None:
            continue
        upper, lower = bounds
        nominal = _resolve_dimension_nominal(dim_ref, entities, char_rep_index)
        record.dimensions.append(
            DimensionalTolerance(
                nominal=nominal,
                upper=upper,
                lower=lower,
                unit="mm",
                applied_to=f"#{dim_ref}" if dim_ref is not None else "",
            )
        )

    record.has_semantic = (
        bool(record.tolerances)
        or bool(record.dimensions)
        or bool(record.datums)
        or bool(record.finishes)
    )
    return record


# ---------------------------------------------------------------------------
# Per-entity emitters
# ---------------------------------------------------------------------------


def _detect_complex_gt_type(body: str) -> str | None:
    """Pull the short type for a complex GEOMETRIC_TOLERANCE entity body."""
    for full, short in _GT_TYPES.items():
        if re.search(rf"\b{full}\b", body):
            return short
    if "GEOMETRIC_TOLERANCE" in body:
        return "geometric"
    return None


def _emit_simple_geometric_tolerance(
    ent_id: int,
    short_type: str,
    body: str,
    entities: dict[int, tuple[str, str]],
    record: PMIRecord,
) -> None:
    """Pull magnitude + datum refs out of a GEOMETRIC_TOLERANCE clause body.

    Handles both simple form ``POSITION_TOLERANCE(name, desc, mag, geom, ds...)``
    and the complex multi-clause body that bundles a
    ``GEOMETRIC_TOLERANCE(...) ... _WITH_DATUM_REFERENCE((#52))``.
    """
    # Locate the GEOMETRIC_TOLERANCE clause inside the body (complex case).
    gt_args: str | None = None
    complex_info = _expand_geometric_tolerance_complex(body)
    if complex_info is not None:
        _, gt_args = complex_info
    else:
        gt_args = body

    try:
        args = split_args(gt_args or "")
    except Exception:
        args = []

    magnitude = 0.0
    if len(args) >= 3:
        mag_ref = _parse_int_ref(args[2])
        m = _resolve_magnitude(mag_ref, entities)
        if m is not None:
            magnitude = m

    applied_to = ""
    if len(args) >= 4:
        applied_ref = _parse_int_ref(args[3])
        if applied_ref is not None:
            applied_to = f"#{applied_ref}"

    # Datum refs: look for ``_WITH_DATUM_REFERENCE((#52))`` first, then any
    # trailing list ``(#51)`` of the specialised tolerance call.
    datums: list = []
    wdm = re.search(r"_WITH_DATUM_REFERENCE\s*\(\s*\(([^)]+)\)\s*\)", body)
    if wdm:
        for ref in _ENT_REF.findall(wdm.group(1)):
            try:
                datums.extend(_collect_datum_letters(int(ref), entities))
            except ValueError:
                continue
    else:
        # Some perpendicularity-style tolerances carry the datum list as
        # the 5th arg ``(name, desc, mag, geom, (#51))``.
        if len(args) >= 5:
            tail = args[4]
            for ref in _ENT_REF.findall(tail):
                try:
                    datums.extend(_collect_datum_letters(int(ref), entities))
                except ValueError:
                    continue

    # Deduplicate, preserve order.
    deduped: list = []
    for d in datums:
        if d and d not in deduped:
            deduped.append(d)

    record.tolerances.append(
        GeometricTolerance(
            type=short_type,
            magnitude_mm=magnitude,
            unit="mm",
            datums=deduped,
            applied_to=applied_to or f"#{ent_id}",
        )
    )


def _emit_datum(body: str, record: PMIRecord, *, is_feature: bool) -> None:
    """Append a Datum from the entity body. DATUM args have identifier last;
    DATUM_FEATURE args carry a 'Simple Datum.N' name first."""
    try:
        args = split_args(body)
    except Exception:
        args = []
    if not args:
        return
    if is_feature:
        ident = _first_quoted(args[0])
    else:
        ident = _first_quoted(args[-1])
    if not ident:
        return
    record.datums.append(Datum(identifier=ident, applied_to=""))


def _emit_surface_finish(
    body: str,
    entities: dict[int, tuple[str, str]],
    record: PMIRecord,
) -> None:
    """Best-effort Ra value extraction from a surface-texture body."""
    val = _measure_value(body)
    if val is None:
        # Walk into referenced ents for a number.
        for nested in _ENT_REF.findall(body):
            try:
                nested_id = int(nested)
            except ValueError:
                continue
            ent = entities.get(nested_id)
            if ent is None:
                continue
            v = _measure_value(ent[1])
            if v is not None:
                val = v
                break
    if val is None:
        return
    record.finishes.append(SurfaceFinish(value=val, unit="um", applied_to=""))


__all__ = ["extract_pmi"]
