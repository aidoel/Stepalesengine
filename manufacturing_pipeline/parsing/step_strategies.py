"""Six-strategy parsing cascade.

Each strategy returns a list of StepPart records (possibly empty). The orchestrator
in step_parser.py walks them in order until one yields a non-empty, non-junk result.

All strategies are pure: they take input data and return a list. They never raise on
bad input and never return None.
"""

from __future__ import annotations

import os
import re
from collections import Counter

from .standard_label import normalize_standard
from .step_tokenizer import decode_step_string, split_args
from .types import StepPart

JUNK_NAMES = {
    "part",
    "part1",
    "component",
    "assembly",
    "default",
    "unnamed",
    "noname",
    "body",
    "solid",
    "untitled",
}

# Bare auto-generated labels like Part1, Solid_42, Body0042, Component_0001.
_AUTO_NAME_RE = re.compile(
    r"^(part|solid|body|component|item|object)[_\-]?\d*$",
    re.IGNORECASE,
)

# CAD modelling-feature names that authoring tools (notably SolidWorks) leak
# into PRODUCT / MANIFOLD_SOLID_BREP arg-0. They name a feature in the build
# tree, never the part, so they must not win the name cascade over a real
# part number recovered from the header / filename.
_FEATURE_NAME_RE = re.compile(
    r"^(cut|boss|base)?[\-_ ]?"
    r"(extrude|extrusion|revolve|revolution|sweep|loft|fillet|chamfer|mirror"
    r"|pattern|shell|draft|rib|dome|wrap|hole|hem|flange|bend|cutlist"
    r"|surface[\-_ ]?cut|knit|thicken|move[\-_ ]?face)"
    r"[\-_ ]?\d*$",
    re.IGNORECASE,
)


def looks_like_feature_name(name: str) -> bool:
    """True when ``name`` is a CAD modelling-feature label, not a part name.

    Recognises SolidWorks-style tokens like ``Cut-Extrude9``, ``Boss-Extrude1``,
    ``Fillet3``, ``Mirror2``. Such labels name a step in the feature tree and
    must lose the name cascade to a genuine part number.
    """
    if not isinstance(name, str):
        return False
    return bool(_FEATURE_NAME_RE.match(name.strip()))

# Comment blocks /* ... */ in raw STEP text.
_COMMENT_RE = re.compile(r"/\*(.*?)\*/", re.DOTALL)

# Entity reference like #42.
_REF_RE = re.compile(r"^#(\d+)$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_meaningful(name: str) -> bool:
    """True if the name carries real information.

    False for blanks, JUNK_NAMES (case-insensitive after stripping), and
    bare auto-generated labels like ``Part1``, ``Solid_42``, ``Body0042``,
    ``Component_0001``.
    """
    if not isinstance(name, str):
        return False
    stripped = name.strip()
    if not stripped:
        return False
    if stripped.lower() in JUNK_NAMES:
        return False
    if _AUTO_NAME_RE.match(stripped):
        return False
    return True


def _clean_string_arg(raw: str) -> str:
    """Strip outer single-quotes, undo doubled-quote escape, X-decode."""
    if raw is None:
        return ""
    s = raw.strip()
    if len(s) >= 2 and s.startswith("'") and s.endswith("'"):
        s = s[1:-1]
    try:
        s = decode_step_string(s)
    except NotImplementedError:
        # Tokenizer not implemented yet — fall back to a minimal decode.
        s = s.replace("''", "'")
    except Exception:
        # Never let a malformed encoding take down a strategy.
        s = s.replace("''", "'")
    return s


def _split(args: str) -> list[str]:
    """Safely call split_args; on failure, return an empty list."""
    if not args:
        return []
    try:
        return list(split_args(args))
    except NotImplementedError:
        # Minimal best-effort splitter so that obviously-simple test cases work
        # before the real tokenizer lands. Does NOT honour nested parens; the
        # production tokenizer must.
        out: list[str] = []
        depth = 0
        buf: list[str] = []
        in_str = False
        i = 0
        while i < len(args):
            ch = args[i]
            if ch == "'" and (i == 0 or args[i - 1] != "\\"):
                # toggle on un-escaped quote; doubled-quote stays inside the string
                if in_str and i + 1 < len(args) and args[i + 1] == "'":
                    buf.append("''")
                    i += 2
                    continue
                in_str = not in_str
                buf.append(ch)
            elif not in_str and ch == "(":
                depth += 1
                buf.append(ch)
            elif not in_str and ch == ")":
                depth -= 1
                buf.append(ch)
            elif not in_str and ch == "," and depth == 0:
                out.append("".join(buf).strip())
                buf = []
            else:
                buf.append(ch)
            i += 1
        tail = "".join(buf).strip()
        if tail or out:
            out.append(tail)
        return out
    except Exception:
        return []


def _ref_id(arg: str) -> int | None:
    """Parse ``#42`` → 42. Returns None for non-refs and ``$``."""
    if not arg:
        return None
    m = _REF_RE.match(arg.strip())
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _entity_args(entities: dict[int, tuple[str, str]], ref: int) -> list[str]:
    """Resolve an entity ref to its split args. Empty list on miss."""
    rec = entities.get(ref)
    if not rec:
        return []
    return _split(rec[1])


def _entity_name(entities: dict[int, tuple[str, str]], ref: int) -> str:
    """Entity type name for a ref (e.g. ``PRODUCT``). Empty string on miss."""
    rec = entities.get(ref)
    if not rec:
        return ""
    return rec[0].upper()


def _is_formation(entities: dict[int, tuple[str, str]], ref: int) -> bool:
    """True if ``ref`` is a PRODUCT_DEFINITION_FORMATION or a known subtype.

    AP214/AP242 files commonly use ``PRODUCT_DEFINITION_FORMATION_WITH_
    SPECIFIED_SOURCE`` in place of the bare entity, so a plain equality check
    misses the formation and the PD -> PRODUCT walk dead-ends.
    """
    return _entity_name(entities, ref).startswith("PRODUCT_DEFINITION_FORMATION")


def _resolve_product_name(
    entities: dict[int, tuple[str, str]],
    pd_ref: int,
    visited: set | None = None,
) -> tuple[str, str, str]:
    """Follow PD → PDF → PRODUCT and return (product_id, name, description).

    Tolerates missing intermediate entities and odd arg orders by scanning
    candidate args for references that resolve to the expected entity types.
    """
    if visited is None:
        visited = set()
    if pd_ref in visited:
        return ("", "", "")
    visited.add(pd_ref)

    if _entity_name(entities, pd_ref) != "PRODUCT_DEFINITION":
        # Maybe the caller already handed us a PRODUCT directly.
        if _entity_name(entities, pd_ref) == "PRODUCT":
            return _product_id_name(_entity_args(entities, pd_ref))
        return ("", "", "")

    pd_args = _entity_args(entities, pd_ref)
    # PRODUCT_DEFINITION args: (id, description, formation_ref, frame_ref)
    pd_desc = _clean_string_arg(pd_args[1]) if len(pd_args) > 1 else ""

    # Walk to formation (typically arg 2). Be tolerant — scan all refs.
    pdf_ref: int | None = None
    for idx in (2, 3, 1, 0):
        if idx < len(pd_args):
            cand = _ref_id(pd_args[idx])
            if cand is not None and _is_formation(entities, cand):
                pdf_ref = cand
                break
    if pdf_ref is None:
        for a in pd_args:
            cand = _ref_id(a)
            if cand is not None and _is_formation(entities, cand):
                pdf_ref = cand
                break

    product_ref: int | None = None
    if pdf_ref is not None and pdf_ref not in visited:
        visited.add(pdf_ref)
        pdf_args = _entity_args(entities, pdf_ref)
        # PDF args: (id, description, of_product_ref)
        for idx in (2, 1, 0):
            if idx < len(pdf_args):
                cand = _ref_id(pdf_args[idx])
                if cand is not None and _entity_name(entities, cand) == "PRODUCT":
                    product_ref = cand
                    break
        if product_ref is None:
            for a in pdf_args:
                cand = _ref_id(a)
                if cand is not None and _entity_name(entities, cand) == "PRODUCT":
                    product_ref = cand
                    break

    # Sometimes PD points straight at PRODUCT without a PDF intermediary.
    if product_ref is None:
        for a in pd_args:
            cand = _ref_id(a)
            if cand is not None and _entity_name(entities, cand) == "PRODUCT":
                product_ref = cand
                break

    if product_ref is None:
        return ("", "", pd_desc)

    pid, pname, _pdesc = _product_id_name(_entity_args(entities, product_ref))
    return (pid, pname, pd_desc)


def _product_id_name(prod_args: list[str]) -> tuple[str, str, str]:
    """Extract (id, name, description) from a PRODUCT entity's split args.

    PRODUCT args: (id, name, description, frame_of_reference)
    """
    pid = _clean_string_arg(prod_args[0]) if len(prod_args) > 0 else ""
    pname = _clean_string_arg(prod_args[1]) if len(prod_args) > 1 else ""
    pdesc = _clean_string_arg(prod_args[2]) if len(prod_args) > 2 else ""
    return (pid, pname, pdesc)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def _nauo_pd_pair(
    entities: dict[int, tuple[str, str]], raw_args: str
) -> tuple[int, int] | None:
    """Extract the ordered ``(first_pd, second_pd)`` ref pair from a NAUO.

    Returns the two PRODUCT_DEFINITION refs in their lexical order, or ``None``
    when the entity does not carry two of them. Orientation (which is the
    assembly) is decided later by :func:`strategy_nauo`.
    """
    try:
        args = _split(raw_args)
    except Exception:
        return None
    if len(args) < 5:
        return None

    pd_refs: list[int] = [
        c
        for c in (_ref_id(a) for a in args[3:])
        if c is not None and _entity_name(entities, c) == "PRODUCT_DEFINITION"
    ]
    if len(pd_refs) < 2:
        pd_refs = [
            c
            for c in (_ref_id(a) for a in args)
            if c is not None and _entity_name(entities, c) == "PRODUCT_DEFINITION"
        ]
    if len(pd_refs) < 2:
        return None
    return (pd_refs[0], pd_refs[1])


def strategy_nauo(entities: dict[int, tuple[str, str]]) -> list[StepPart]:
    """Walk NEXT_ASSEMBLY_USAGE_OCCURRENCE entries and build a parent→children list."""
    if not entities:
        return []

    # First pass: collect every NAUO's ordered PD pair.
    pairs: list[tuple[int, int]] = []
    for _ref, (etype, raw_args) in entities.items():
        if etype.upper() != "NEXT_ASSEMBLY_USAGE_OCCURRENCE":
            continue
        pair = _nauo_pd_pair(entities, raw_args)
        if pair is not None:
            pairs.append(pair)

    if not pairs:
        return []

    # Decide orientation. The ISO 10303 schema orders the attributes
    # (relating_product_definition, related_product_definition): the relating
    # PD is the assembly (parent), the related PD is the component (child).
    # Some authoring tools emit the pair swapped, so we cross-check against the
    # observed structure: the assembly PD recurs across many NAUOs in one slot
    # while components each appear once. The parent slot is the one whose
    # most-repeated PD - counting only PDs exclusive to that slot - recurs the
    # most. Ties fall back to the canonical schema order (first = parent).
    first_slot = Counter(a for a, _b in pairs)
    second_slot = Counter(b for _a, b in pairs)
    first_only = {pd for pd in first_slot if pd not in second_slot}
    second_only = {pd for pd in second_slot if pd not in first_slot}
    first_parent_score = max((first_slot[pd] for pd in first_only), default=0)
    second_parent_score = max((second_slot[pd] for pd in second_only), default=0)
    parent_is_first = second_parent_score <= first_parent_score

    parts: dict[str, StepPart] = {}
    for relating_ref, related_ref in pairs:
        if not parent_is_first:
            relating_ref, related_ref = related_ref, relating_ref

        parent_pid, parent_name, parent_desc = _resolve_product_name(entities, relating_ref)
        child_pid, child_name, child_desc = _resolve_product_name(entities, related_ref)

        if not parent_pid and not parent_name:
            continue
        if not child_pid and not child_name:
            continue

        for pid, name, desc in (
            (parent_pid, parent_name, parent_desc),
            (child_pid, child_name, child_desc),
        ):
            if pid not in parts:
                parts[pid] = StepPart(
                    product_id=pid,
                    name=name,
                    description=desc,
                    children=[],
                    source="nauo",
                )

        # Append on every NAUO occurrence, including repeats: each NAUO is a
        # distinct component instance. build_assembly_graph collapses repeated
        # child ids into a quantity, so multiplicity must survive to here.
        parts[parent_pid].children.append(child_pid)

    # Filter out parts whose names are not meaningful AND who have no children.
    # Keep a part if it has children — its name might be junk but the structure matters.
    out: list[StepPart] = []
    for _pid, part in parts.items():
        if not is_meaningful(part.name) and not part.children:
            continue
        out.append(part)
    return out


def strategy_product_definition(entities: dict[int, tuple[str, str]]) -> list[StepPart]:
    """One StepPart per PRODUCT_DEFINITION, walking PDF→PRODUCT for the name."""
    if not entities:
        return []

    out: list[StepPart] = []
    seen: set = set()
    for ref, (etype, raw_args) in entities.items():
        if etype.upper() != "PRODUCT_DEFINITION":
            continue
        args = _split(raw_args)
        pd_id = _clean_string_arg(args[0]) if len(args) > 0 else ""
        pd_desc = _clean_string_arg(args[1]) if len(args) > 1 else ""

        pid, pname, _walked_desc = _resolve_product_name(entities, ref)
        # Name must come from the walk to PRODUCT; if it failed, the PD has no
        # recoverable name and we skip it (callers fall back to later strategies).
        if not is_meaningful(pname):
            continue
        product_id = pid or pd_id
        name = pname
        description = pd_desc or _walked_desc
        key = (product_id, name)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            StepPart(
                product_id=product_id,
                name=name,
                description=description,
                children=[],
                source="product_definition",
            )
        )
    return out


def strategy_product(entities: dict[int, tuple[str, str]]) -> list[StepPart]:
    """One StepPart per PRODUCT entity, skipping duplicates and junk names."""
    if not entities:
        return []

    out: list[StepPart] = []
    seen: set = set()
    for _ref, (etype, raw_args) in entities.items():
        if etype.upper() != "PRODUCT":
            continue
        args = _split(raw_args)
        pid, name, desc = _product_id_name(args)
        if not is_meaningful(name):
            continue
        key = (pid, name)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            StepPart(
                product_id=pid,
                name=name,
                description=desc,
                children=[],
                source="product",
            )
        )
    return out


def strategy_brep_names(entities: dict[int, tuple[str, str]]) -> list[StepPart]:
    """Scan MANIFOLD_SOLID_BREP and SHELL_BASED_SURFACE_MODEL for arg-0 names."""
    if not entities:
        return []

    targets = {"MANIFOLD_SOLID_BREP", "SHELL_BASED_SURFACE_MODEL"}
    out: list[StepPart] = []
    seen: set = set()
    for ref, (etype, raw_args) in entities.items():
        if etype.upper() not in targets:
            continue
        args = _split(raw_args)
        if not args:
            continue
        name = _clean_string_arg(args[0])
        if not is_meaningful(name):
            continue
        # A brep solid labelled with a CAD modelling-feature name (Cut-Extrude9,
        # Fillet3, ...) carries no part identity; skip it so the cascade can
        # reach the header strategy and recover the real part number.
        if looks_like_feature_name(name):
            continue
        pid = f"#{ref}"
        key = (pid, name)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            StepPart(
                product_id=pid,
                name=name,
                description="",
                children=[],
                source="brep",
            )
        )
    return out


def _header_file_name(header: dict) -> str:
    """Recover the FILE_NAME name field (sans extension) from a parsed header.

    ``parse_header`` returns each header entity as the raw lexical arg list, so
    FILE_NAME's first arg is the authored file name. CAD exporters write the
    real part number there even when the on-disk file has been renamed.
    """
    if not isinstance(header, dict):
        return ""
    file_name = header.get("file_name") or header.get("FILE_NAME") or []
    raw = ""
    if isinstance(file_name, (list, tuple)) and file_name:
        raw = _clean_string_arg(str(file_name[0]))
    elif isinstance(file_name, str):
        raw = file_name
    elif isinstance(file_name, dict):
        raw = file_name.get("name") or ""
    raw = (raw or "").strip()
    if not raw:
        return ""
    return os.path.splitext(os.path.basename(raw))[0]


def _header_description(header: dict) -> str:
    """Recover a free-text description from the header, best effort.

    Prefers FILE_DESCRIPTION; falls back to a ``description`` carried on a
    dict-shaped FILE_NAME entry.
    """
    if not isinstance(header, dict):
        return ""
    block = header.get("file_description") or header.get("FILE_DESCRIPTION") or []
    if isinstance(block, (list, tuple)) and block:
        text = _clean_string_arg(str(block[0])).strip()
        if text:
            return text
    elif isinstance(block, str) and block.strip():
        return block.strip()
    elif isinstance(block, dict):
        text = (block.get("description") or "").strip()
        if text:
            return text

    file_name = header.get("file_name") or header.get("FILE_NAME") or {}
    if isinstance(file_name, dict):
        return (file_name.get("description") or "").strip()
    return ""


def strategy_header(header: dict, path: str) -> list[StepPart]:
    """Fallback synthetic part from the FILE_NAME field or filename basename.

    The authored FILE_NAME field is preferred over the on-disk basename: it
    carries the real part number even when the file has been renamed (and the
    basename gathered an unrelated ``sheet_`` prefix or similar).
    """
    basename = ""
    if path:
        basename = os.path.splitext(os.path.basename(path))[0]

    header_name = _header_file_name(header)
    description = _header_description(header)

    name = header_name or basename or description or "unknown"
    pid = name
    return [
        StepPart(
            product_id=pid,
            name=name,
            description=description or name,
            children=[],
            source="header",
        )
    ]


def strategy_comments(raw: str, path: str) -> list[StepPart]:
    """Last-resort: scan /* ... */ blocks for canonical standard labels."""
    if not raw:
        return []

    out: list[StepPart] = []
    seen: set = set()
    basename = os.path.splitext(os.path.basename(path))[0] if path else ""

    for m in _COMMENT_RE.finditer(raw):
        block = m.group(1)
        try:
            labels = normalize_standard(block)
        except Exception:
            labels = []
        for label in labels:
            if label in seen:
                continue
            seen.add(label)
            pid = f"{basename}:{label}" if basename else label
            out.append(
                StepPart(
                    product_id=pid,
                    name=label,
                    description=block.strip(),
                    children=[],
                    source="comments",
                )
            )
    return out


# Re-export for the orchestrator's convenience.
__all__ = [
    "JUNK_NAMES",
    "is_meaningful",
    "looks_like_feature_name",
    "strategy_nauo",
    "strategy_product_definition",
    "strategy_product",
    "strategy_brep_names",
    "strategy_header",
    "strategy_comments",
]
