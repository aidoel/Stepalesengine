"""Defensive Part 21 tokeniser.

Phase 1 implementation. Handles:

  * Encoding cascade (utf-8-sig -> utf-8 -> cp1252 -> latin-1) with BOM strip.
  * Part 21 line continuation (``\\`` followed by newline) collapsed pre-tokenise.
  * ISO 10303-21 X-encoded strings (``\\X\\HH``, ``\\X2\\HHHH...\\X0\\``,
    ``\\X4\\HHHHHHHH...\\X0\\``) and doubled-quote escapes (``''`` -> ``'``).
  * Comments ``/* ... */`` and the HEADER section.
  * Nested parens, bracketed lists, and quoted commas in argument splitting.

The module is pure-Python; nothing in here imports OCP. Failures during
tokenisation degrade gracefully -- a corrupt entity must not abort the file.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

_LINE_CONT = re.compile(r"\\\r?\n")
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

# X-encoded sequences. ``\X\HH`` is a single ISO-8859-1 byte. ``\X2\...\X0\`` is
# a run of UTF-16 code units (4 hex chars each). ``\X4\...\X0\`` is a run of
# UTF-32 code points (8 hex chars each). The greedy run for X2/X4 stops at
# the explicit ``\X0\`` terminator.
_X1 = re.compile(r"\\X\\([0-9A-Fa-f]{2})")
_X2 = re.compile(r"\\X2\\((?:[0-9A-Fa-f]{4})+)\\X0\\")
_X4 = re.compile(r"\\X4\\((?:[0-9A-Fa-f]{8})+)\\X0\\")

# Header section delimiters.
_HEADER_RE = re.compile(r"HEADER\s*;(.*?)ENDSEC\s*;", re.DOTALL | re.IGNORECASE)
_DATA_RE = re.compile(r"DATA\s*;(.*?)ENDSEC\s*;", re.DOTALL | re.IGNORECASE)

# Match a single entity declaration ``#ID = NAME(ARGS);`` allowing nested
# parens inside ARGS. We do not use re for the *body* because nested parens
# and quoted strings would defeat a flat regex; instead we use re only to
# locate the entity head and then walk character-by-character for the body.
_ENT_HEAD = re.compile(r"#(\d+)\s*=\s*([A-Za-z_][A-Za-z_0-9]*)\s*\(", re.MULTILINE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_step_text(path: str) -> str:
    """Read a STEP file with encoding cascade and line-continuation handling.

    Cascades through ``utf-8-sig`` -> ``utf-8`` -> ``cp1252`` -> ``latin-1``.
    The BOM (if any) is stripped. Part 21 line continuations (``\\`` immediately
    followed by ``\\r?\\n``) are collapsed so that entity definitions are on a
    single logical line by the time downstream tokenisation runs.
    """
    with open(path, "rb") as fh:
        raw = fh.read()

    text = _decode_bytes(raw)

    # Strip leading BOM if it survived the decode (utf-8-sig handles the
    # canonical BOM, but other BOMs or stray U+FEFF can appear).
    if text and text[0] == "﻿":
        text = text.lstrip("﻿")

    # Collapse Part 21 line continuations BEFORE any further processing so
    # that entities can be parsed as single logical lines.
    text = _LINE_CONT.sub("", text)
    return text


def decode_step_string(s: str) -> str:
    """Decode ISO 10303-21 X-encoded strings and doubled-quote escapes.

    Examples
    --------
    >>> decode_step_string(r"\\X\\41")
    'A'
    >>> decode_step_string(r"\\X2\\00C4\\X0\\")
    'Ä'
    >>> decode_step_string(r"hello''world")
    "hello'world"

    Malformed sequences are returned verbatim rather than raising.
    """
    if not s:
        return s

    out = s

    # UTF-32 runs first (longest hex group). Tolerate malformed code points by
    # falling back to the original substring.
    def _repl_x4(m: re.Match) -> str:
        try:
            hex_run = m.group(1)
            chars = []
            for i in range(0, len(hex_run), 8):
                cp = int(hex_run[i : i + 8], 16)
                chars.append(chr(cp))
            return "".join(chars)
        except (ValueError, OverflowError):
            return m.group(0)

    out = _X4.sub(_repl_x4, out)

    def _repl_x2(m: re.Match) -> str:
        try:
            hex_run = m.group(1)
            # UTF-16 with surrogate pairs.
            code_units = [int(hex_run[i : i + 4], 16) for i in range(0, len(hex_run), 4)]
            # Reassemble as bytes and decode via UTF-16-BE to honour surrogates.
            buf = bytearray()
            for cu in code_units:
                buf.append((cu >> 8) & 0xFF)
                buf.append(cu & 0xFF)
            return buf.decode("utf-16-be", errors="replace")
        except (ValueError, UnicodeDecodeError):
            return m.group(0)

    out = _X2.sub(_repl_x2, out)

    def _repl_x1(m: re.Match) -> str:
        try:
            return bytes([int(m.group(1), 16)]).decode("latin-1")
        except (ValueError, UnicodeDecodeError):
            return m.group(0)

    out = _X1.sub(_repl_x1, out)

    # Collapse doubled single-quotes that appear inside string literals to a
    # single literal quote. We do this after X-decoding so the runtime cost is
    # paid only once.
    out = out.replace("''", "'")
    return out


def parse_header(text: str) -> dict:
    """Parse the HEADER section into a best-effort dict.

    Returns an empty dict when no HEADER section is present. Recognised keys:
    ``file_description``, ``file_name``, ``file_schema``. Each value is the
    raw arg list (already passed through :func:`split_args`).
    """
    m = _HEADER_RE.search(text)
    if not m:
        return {}

    body = m.group(1)
    # Strip comments inside the header so they don't disturb arg splitting.
    body = _COMMENT.sub("", body)

    result: dict[str, list[str]] = {}
    for name_key in ("FILE_DESCRIPTION", "FILE_NAME", "FILE_SCHEMA"):
        head_re = re.compile(rf"\b{name_key}\s*\(", re.IGNORECASE)
        hm = head_re.search(body)
        if not hm:
            continue
        args_str, end = _read_balanced_args(body, hm.end())
        if args_str is None:
            continue
        try:
            args = split_args(args_str)
        except Exception:
            args = []
        result[name_key.lower()] = args
    return result


def tokenize_entities(text: str) -> dict[int, tuple[str, str]]:
    """Return ``{entity_id: (entity_name, raw_args_string)}`` from the DATA section.

    Comments are stripped first. The HEADER section is skipped (callers can use
    :func:`parse_header` to read it separately). One malformed entity does not
    abort tokenisation of the rest of the file -- each entity is wrapped in a
    try/except.
    """
    # Strip comments first; they can appear anywhere in the file.
    cleaned = _COMMENT.sub("", text)

    # Locate the DATA section. If we cannot find one, fall back to scanning
    # the entire cleaned text (some authoring tools omit the wrapper).
    dm = _DATA_RE.search(cleaned)
    if dm:
        body = dm.group(1)
    else:
        body = cleaned

    out: dict[int, tuple[str, str]] = {}
    for m in _ENT_HEAD.finditer(body):
        try:
            ent_id = int(m.group(1))
            name = m.group(2).upper()
            args_str, end = _read_balanced_args(body, m.end())
            if args_str is None:
                continue
            out[ent_id] = (name, args_str)
        except Exception:
            # Skip individual malformed entities; do not abort the whole file.
            continue
    return out


def split_args(args: str) -> list:
    """Split a STEP entity argument string into top-level lexical args.

    Respects nested parens ``(...)``, bracketed lists ``[...]``, and
    single-quoted strings with ``''`` escapes. Each element of the returned
    list is the original lexical form (``'Foo'``, ``#42``, ``(1.0,2.0)``, ``$``,
    ``*``, ``.T.``, etc.) with surrounding whitespace stripped. Empty input
    yields an empty list.
    """
    if args is None:
        return []
    s = args
    parts: list[str] = []
    buf: list[str] = []
    depth_paren = 0
    depth_bracket = 0
    in_quote = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if in_quote:
            buf.append(ch)
            if ch == "'":
                # Doubled single-quote is an escape, not a terminator.
                if i + 1 < n and s[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
            buf.append(ch)
            i += 1
            continue
        if ch == "(":
            depth_paren += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth_paren -= 1
            buf.append(ch)
            i += 1
            continue
        if ch == "[":
            depth_bracket += 1
            buf.append(ch)
            i += 1
            continue
        if ch == "]":
            depth_bracket -= 1
            buf.append(ch)
            i += 1
            continue
        if ch == "," and depth_paren == 0 and depth_bracket == 0:
            parts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail or parts:
        parts.append(tail)
    return parts


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _decode_bytes(raw: bytes) -> str:
    """Decode bytes using the standard STEP encoding cascade."""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    # latin-1 cannot fail on a byte stream, but be defensive.
    return raw.decode("latin-1", errors="replace")


def _read_balanced_args(text: str, start: int) -> tuple[str | None, int]:
    """Read characters from ``start`` until the matching closing paren.

    ``start`` must point to the first character *after* the opening ``(``.
    Returns ``(args_string, index_after_close)``. ``args_string`` is everything
    between the opening and closing paren, exclusive. Quoted strings and
    nested parens are honoured. Returns ``(None, start)`` if no balanced close
    is found before EOF.
    """
    depth = 1
    in_quote = False
    i = start
    n = len(text)
    out: list[str] = []
    while i < n:
        ch = text[i]
        if in_quote:
            out.append(ch)
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
            out.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
            out.append(ch)
            i += 1
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                # Consume the trailing ``;`` if present.
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                if j < n and text[j] == ";":
                    j += 1
                return "".join(out), j
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return None, start


__all__ = [
    "read_step_text",
    "decode_step_string",
    "tokenize_entities",
    "split_args",
    "parse_header",
]
