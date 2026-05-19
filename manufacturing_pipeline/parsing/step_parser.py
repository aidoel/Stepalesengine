"""RobustStepParser orchestrator.

Walks the six-strategy cascade (NAUO -> PRODUCT_DEFINITION -> PRODUCT ->
BREP names -> HEADER -> comments), then falls back to OCAF when every
strategy returns empty. As a last resort a synthetic record is built from
the filename basename. Never returns None and never returns an empty list.

The parser is the single entry point for the parsing layer; downstream
classification, feature extraction, and BOM building all consume the
StepPart records emitted here. Each record carries a ``source`` field so
provenance can be traced end-to-end.

Caching is keyed on ``(absolute_path, mtime_ns, size)`` from ``os.stat``.
A ``cache=False`` call bypasses both lookup and store and is the primary
hook used by tests that need a fresh parse.
"""

from __future__ import annotations

import logging
import os
from collections import Counter

from .. import telemetry
from .dutch_vocabulary import detect_part_type
from .occt_fallback import occt_fallback
from .standard_label import normalize_standard
from .step_strategies import (
    is_meaningful,
    strategy_brep_names,
    strategy_comments,
    strategy_header,
    strategy_nauo,
    strategy_product,
    strategy_product_definition,
)
from .step_tokenizer import parse_header, read_step_text, tokenize_entities
from .types import StepParseError, StepPart

logger = logging.getLogger(__name__)

# Module-level cache keyed on (str(path), mtime_ns, size).
_CACHE: dict[tuple[str, int, int], list[StepPart]] = {}


def _cache_key(path: str) -> tuple[str, int, int]:
    st = os.stat(path)
    return (path, st.st_mtime_ns, st.st_size)


def _has_meaningful(parts: list[StepPart]) -> bool:
    """True if at least one part in the list carries a usable name."""
    return any(is_meaningful(p.name) for p in parts)


def _decorate(parts: list[StepPart]) -> list[StepPart]:
    """Append Dutch part-type tag and standard-label hits to ``description``.

    The tags are joined with ``;`` so the downstream classifier can split
    them back out without ambiguity. We mutate the StepPart in place
    because the records have just been created and no caller holds them
    yet.
    """
    for part in parts:
        haystack = " ".join(filter(None, (part.name, part.description)))
        tags: list[str] = []

        dutch_tag = detect_part_type(haystack)
        if dutch_tag:
            tags.append(dutch_tag)

        try:
            labels = normalize_standard(haystack)
        except Exception:
            labels = []
        for label in labels:
            if label not in tags:
                tags.append(label)

        if not tags:
            continue

        suffix = ";".join(tags)
        if part.description:
            part.description = f"{part.description};{suffix}"
        else:
            part.description = suffix
    return parts


def _filename_part(path: str) -> StepPart:
    base = os.path.splitext(os.path.basename(path))[0] or "unknown"
    return StepPart(
        product_id=base,
        name=base,
        description=base,
        children=[],
        source="filename",
    )


def parse_step(
    path,
    *,
    use_occt_fallback: bool = True,
    cache: bool = True,
) -> list[StepPart]:
    """Cascade tokenize -> strategies in order -> OCAF -> synthetic filename part.

    Parameters
    ----------
    path:
        File system path to a STEP file (Part 21). Accepts ``str`` or
        ``pathlib.Path``.
    use_occt_fallback:
        When True, attempt the OCAF XCAF fallback if every text strategy
        yields an empty (or junk-only) result. When False, skip directly
        to the synthetic filename record.
    cache:
        When True (default), look up an existing parse keyed on
        ``(path, mtime_ns, size)`` and store the freshly parsed result.
        Setting ``cache=False`` forces a re-parse and a re-read of the
        file from disk.

    Returns
    -------
    list[StepPart]
        Always non-empty. The last fallback synthesises a single record
        from the filename basename with ``source='filename'``.

    Raises
    ------
    StepParseError
        Only when the file cannot be read at all (missing path, OS
        error, decode failure that even the latin-1 fallback rejects).
    """
    path_str = str(path)

    if cache:
        try:
            key = _cache_key(path_str)
        except OSError as exc:
            raise StepParseError(f"cannot stat {path_str}: {exc}") from exc
        hit = _CACHE.get(key)
        if hit is not None:
            return hit
    else:
        # Ensure the file exists so we fail fast with StepParseError rather
        # than letting read_step_text raise FileNotFoundError out of band.
        try:
            os.stat(path_str)
        except OSError as exc:
            raise StepParseError(f"cannot stat {path_str}: {exc}") from exc

    try:
        text = read_step_text(path_str)
    except (FileNotFoundError, PermissionError, IsADirectoryError) as exc:
        raise StepParseError(f"cannot read {path_str}: {exc}") from exc
    except OSError as exc:
        raise StepParseError(f"cannot read {path_str}: {exc}") from exc

    header = parse_header(text)
    entities = tokenize_entities(text)

    parts: list[StepPart] = []
    chosen = "filename"

    cascade = (
        ("nauo", lambda: strategy_nauo(entities)),
        ("product_definition", lambda: strategy_product_definition(entities)),
        ("product", lambda: strategy_product(entities)),
        ("brep", lambda: strategy_brep_names(entities)),
        ("header", lambda: strategy_header(header, path_str)),
        ("comments", lambda: strategy_comments(text, path_str)),
    )

    for name, fn in cascade:
        try:
            candidate = fn()
        except Exception as exc:  # strategies promise not to raise, but be defensive
            logger.warning("strategy %s raised %s", name, exc)
            continue
        if candidate and _has_meaningful(candidate):
            parts = candidate
            chosen = name
            break

    if not parts and use_occt_fallback:
        try:
            occt_parts = occt_fallback(path_str)
        except Exception as exc:  # occt_fallback promises [] on error, be paranoid
            logger.warning("occt_fallback raised %s", exc)
            occt_parts = []
        if occt_parts:
            parts = occt_parts
            chosen = "occt_xcaf"

    if not parts:
        parts = [_filename_part(path_str)]
        chosen = "filename"

    _decorate(parts)

    source_counts = Counter(p.source for p in parts)
    logger.info(
        "parsed %d parts via strategy=%s sources=%s",
        len(parts),
        chosen,
        dict(source_counts),
    )
    telemetry.emit(
        "parse_strategy_used",
        path=path_str,
        strategy=chosen,
        n_parts=len(parts),
        sources=dict(source_counts),
    )

    if cache:
        _CACHE[key] = parts

    return parts


__all__ = ["parse_step"]
