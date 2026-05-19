"""Six-strategy parsing cascade.

Each strategy returns a list of StepPart records (possibly empty). The orchestrator
in step_parser.py walks them in order until one yields a non-empty, non-junk result.

All strategies are pure: they take input data and return a list. They never raise on
bad input and never return None.
"""

from __future__ import annotations

import os
import re

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
            if cand is not None and _entity_name(entities, cand) == "PRODUCT_DEFINITION_FORMATION":
                pdf_ref = cand
                break
    if pdf_ref is None:
        for a in pd_args:
            cand = _ref_id(a)
            if cand is not None and _entity_name(entities, cand) == "PRODUCT_DEFINITION_FORMATION":
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


def strategy_nauo(entities: dict[int, tuple[str, str]]) -> list[StepPart]:
    """Walk NEXT_ASSEMBLY_USAGE_OCCURRENCE entries and build a parent→children list."""
    if not entities:
        return []

    parts: dict[str, StepPart] = {}
    edges: list[tuple[str, str]] = []

    # First pass: discover relating/related product_definitions across all NAUOs.
    visited_pd: set = set()
    for _ref, (etype, raw_args) in entities.items():
        if etype.upper() != "NEXT_ASSEMBLY_USAGE_OCCURRENCE":
            continue
        try:
            args = _split(raw_args)
        except Exception:
            continue
        if len(args) < 5:
            continue

        # Canonical NAUO arg order:
        # 0: id, 1: name, 2: description, 3: related_pd, 4: relating_pd, 5: ref_designator
        # Many AP versions swap these — be tolerant and pick the first two PD refs.
        pd_refs: list[int] = []
        for a in args[3:]:
            cand = _ref_id(a)
            if cand is None:
                continue
            if _entity_name(entities, cand) == "PRODUCT_DEFINITION":
                pd_refs.append(cand)
        if len(pd_refs) < 2:
            # Try all args as a last resort.
            pd_refs = [
                c
                for c in (_ref_id(a) for a in args)
                if c is not None and _entity_name(entities, c) == "PRODUCT_DEFINITION"
            ]
        if len(pd_refs) < 2:
            continue

        # Spec: relating is parent, related is child. The canonical order in the
        # entity is (related_pd, relating_pd) but AP variants differ. We default
        # to "first ref = related (child), second ref = relating (parent)" which
        # matches the documented layout in §3 of the plan.
        related_pd, relating_pd = pd_refs[0], pd_refs[1]

        parent_pid, parent_name, parent_desc = _resolve_product_name(
            entities, relating_pd, visited=set(visited_pd)
        )
        child_pid, child_name, child_desc = _resolve_product_name(
            entities, related_pd, visited=set(visited_pd)
        )

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

        if child_pid not in parts[parent_pid].children:
            parts[parent_pid].children.append(child_pid)
        edges.append((parent_pid, child_pid))

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


def strategy_header(header: dict, path: str) -> list[StepPart]:
    """Fallback synthetic part from FILE_NAME description + filename basename."""
    basename = ""
    if path:
        basename = os.path.splitext(os.path.basename(path))[0]

    description = ""
    if isinstance(header, dict):
        file_name = header.get("FILE_NAME") or header.get("file_name") or {}
        if isinstance(file_name, dict):
            description = file_name.get("description") or file_name.get("name") or ""
        elif isinstance(file_name, str):
            description = file_name
        if not description:
            desc_block = header.get("FILE_DESCRIPTION") or header.get("file_description") or {}
            if isinstance(desc_block, dict):
                description = desc_block.get("description", "") or ""
            elif isinstance(desc_block, str):
                description = desc_block

    name = basename or description or "unknown"
    pid = basename or name
    return [
        StepPart(
            product_id=pid,
            name=name,
            description=description or basename,
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
    "strategy_nauo",
    "strategy_product_definition",
    "strategy_product",
    "strategy_brep_names",
    "strategy_header",
    "strategy_comments",
]
