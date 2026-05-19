"""Tests for the defensive Part 21 tokeniser.

Covers the encoding cascade, BOM handling, line continuations, X-encoded
strings, doubled-quote escapes, nested-paren argument splitting, multi-line
entities, malformed-entity resilience, and HEADER parsing.
"""

from __future__ import annotations

import os
import tempfile

from manufacturing_pipeline.parsing.step_tokenizer import (
    decode_step_string,
    parse_header,
    read_step_text,
    split_args,
    tokenize_entities,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_bytes(b: bytes) -> str:
    """Write bytes to a temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".stp")
    with os.fdopen(fd, "wb") as fh:
        fh.write(b)
    return path


# ---------------------------------------------------------------------------
# read_step_text -- encoding cascade & BOM
# ---------------------------------------------------------------------------


def test_read_utf8_with_bom_strips_bom():
    payload = b"ISO-10303-21;\nHEADER;\nENDSEC;\n"
    bom = b"\xef\xbb\xbf"
    path = _write_bytes(bom + payload)
    try:
        text = read_step_text(path)
        assert not text.startswith("﻿")
        assert text.startswith("ISO-10303-21;")
    finally:
        os.unlink(path)


def test_read_utf8_umlaut():
    raw = "DATA;\n#1=PRODUCT('plaatdeel-Ä','name','',(#2));\nENDSEC;\n".encode()
    path = _write_bytes(raw)
    try:
        text = read_step_text(path)
        assert "Ä" in text
    finally:
        os.unlink(path)


def test_read_cp1252_umlaut():
    # 'ö' in cp1252 is byte 0xF6, which is also valid latin-1 -> cascade reaches
    # this codec after utf-8 fails. Force a sequence that is invalid UTF-8.
    raw = b"DATA;\n#1=PRODUCT('K\xf6rper','x','',(#2));\nENDSEC;\n"
    # Insert an invalid utf-8 continuation byte to force the cascade past utf-8.
    raw_with_bad = b"\xc3\x28" + raw  # \xc3\x28 is not valid utf-8
    path = _write_bytes(raw_with_bad)
    try:
        text = read_step_text(path)
        assert "Körper" in text or "K\xf6rper" in text
    finally:
        os.unlink(path)


def test_read_latin1_fallback():
    # Bytes that are valid in latin-1 but invalid as utf-8 *start* sequences.
    # 0x80 is a continuation byte; standalone it cannot decode as utf-8.
    raw = b"DATA;\n#1=PRODUCT('weird-\x80-byte','x','',(#2));\nENDSEC;\n"
    path = _write_bytes(raw)
    try:
        text = read_step_text(path)
        # We don't care which exact decode wins as long as we get a string.
        assert "weird-" in text
        assert "-byte" in text
    finally:
        os.unlink(path)


def test_read_collapses_line_continuations():
    raw = b"#1=PRODUCT('foo',\\\n'bar',\\\r\n'baz');\n"
    path = _write_bytes(raw)
    try:
        text = read_step_text(path)
        # The continuation backslash + newline pairs must be gone, so the
        # entity body is a single logical line.
        assert "\\\n" not in text
        assert "\\\r\n" not in text
        assert "PRODUCT('foo','bar','baz')" in text
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# decode_step_string -- X-encoded strings & doubled quotes
# ---------------------------------------------------------------------------


def test_decode_x1():
    assert decode_step_string("\\X\\41") == "A"
    assert decode_step_string("hello \\X\\41 world") == "hello A world"


def test_decode_x2_umlaut():
    # \X2\00C4\X0\ is 'Ä' as UTF-16BE 0x00C4.
    assert decode_step_string("\\X2\\00C4\\X0\\") == "Ä"


def test_decode_x2_multi_codepoint():
    # 'Ä' (00C4) + 'Ö' (00D6) packed in one X2 run.
    assert decode_step_string("\\X2\\00C400D6\\X0\\") == "ÄÖ"


def test_decode_x4_astral():
    # U+1F600 (grinning face emoji) encoded as 8 hex chars.
    assert decode_step_string("\\X4\\0001F600\\X0\\") == "\U0001f600"


def test_decode_doubled_quotes():
    assert decode_step_string("Cap'n's hat".replace("'", "''")) == "Cap'n's hat"
    assert decode_step_string("hello''world") == "hello'world"


def test_decode_malformed_passes_through():
    # An unterminated \X2\ sequence (no \X0\) should be left as-is rather than
    # raising.
    bad = "\\X2\\00C4 not terminated"
    assert decode_step_string(bad) == bad
    # An \X4\ run that is the wrong length (not multiple of 8) also passes
    # through unchanged via the regex never matching.
    bad2 = "\\X4\\00C4\\X0\\"
    assert decode_step_string(bad2) == bad2


def test_decode_combined():
    s = "Pre \\X\\41 mid \\X2\\00C4\\X0\\ tail ''quote''"
    assert decode_step_string(s) == "Pre A mid Ä tail 'quote'"


# ---------------------------------------------------------------------------
# split_args -- nested parens, brackets, quoted strings
# ---------------------------------------------------------------------------


def test_split_args_simple():
    assert split_args("'a','b','c'") == ["'a'", "'b'", "'c'"]


def test_split_args_quoted_comma():
    # A comma inside a quoted string must NOT be treated as a separator.
    assert split_args("'foo, bar','baz'") == ["'foo, bar'", "'baz'"]


def test_split_args_doubled_quote_inside_string():
    # The doubled '' is an escaped quote, not a string boundary.
    out = split_args("'Cap''n','x'")
    assert out == ["'Cap''n'", "'x'"]


def test_split_args_nested_parens():
    out = split_args("(1.0,2.0,3.0),#42,(#1,#2,(#3,#4)),$")
    assert out == ["(1.0,2.0,3.0)", "#42", "(#1,#2,(#3,#4))", "$"]


def test_split_args_brackets():
    out = split_args("[1,2,3],#9,[#1,(#2,#3)]")
    assert out == ["[1,2,3]", "#9", "[#1,(#2,#3)]"]


def test_split_args_mixed():
    out = split_args("'plate, 10mm',#1,(.T.,.F.),*,$,(1.5,2.5)")
    assert out == [
        "'plate, 10mm'",
        "#1",
        "(.T.,.F.)",
        "*",
        "$",
        "(1.5,2.5)",
    ]


def test_split_args_empty():
    assert split_args("") == []


# ---------------------------------------------------------------------------
# tokenize_entities
# ---------------------------------------------------------------------------


_SAMPLE_STEP = """
ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('demo file'),'2;1');
FILE_NAME('demo.stp','2026-05-15T00:00:00',('Author'),('Org'),'IDA','x','');
FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));
ENDSEC;
DATA;
/* a comment that mentions #99=FAKE() should not be parsed */
#1=PRODUCT('id-1','demo plate','description, with comma',(#2));
#2=PRODUCT_CONTEXT('',$,'mechanical');
#3=MANIFOLD_SOLID_BREP('shell',#4);
#4=CLOSED_SHELL('',(#5,#6));
#10=NEXT_ASSEMBLY_USAGE_OCCURRENCE('id-10','nauo','',#1,#11,$);
ENDSEC;
END-ISO-10303-21;
"""


def test_tokenize_basic_entities():
    ents = tokenize_entities(_SAMPLE_STEP)
    assert 1 in ents and ents[1][0] == "PRODUCT"
    assert 3 in ents and ents[3][0] == "MANIFOLD_SOLID_BREP"
    assert 10 in ents and ents[10][0] == "NEXT_ASSEMBLY_USAGE_OCCURRENCE"


def test_tokenize_skips_comments():
    ents = tokenize_entities(_SAMPLE_STEP)
    # The fake entity inside /* ... */ must not appear.
    assert 99 not in ents


def test_tokenize_skips_header_entities():
    ents = tokenize_entities(_SAMPLE_STEP)
    # FILE_NAME and friends live in HEADER, not DATA; they must not appear in
    # the tokenised entity dict.
    names = {n for n, _ in ents.values()}
    assert "FILE_NAME" not in names
    assert "FILE_DESCRIPTION" not in names
    assert "FILE_SCHEMA" not in names


def test_tokenize_multiline_entity():
    text = """DATA;
#1=PRODUCT(
    'id-1',
    'plate',
    'multi
     line',
    (#2)
);
#2=PRODUCT_CONTEXT('',$,'mech');
ENDSEC;
"""
    ents = tokenize_entities(text)
    assert 1 in ents
    assert ents[1][0] == "PRODUCT"
    # The raw args should still contain both #2 reference and the multi-line
    # description string.
    assert "#2" in ents[1][1]


def test_tokenize_with_collapsed_continuation():
    raw = b"DATA;\n#7=PRODUCT('foo',\\\n'bar',\\\n'baz',(#8));\nENDSEC;\n"
    path = _write_bytes(raw)
    try:
        text = read_step_text(path)
        ents = tokenize_entities(text)
        assert 7 in ents
        args = split_args(ents[7][1])
        assert args == ["'foo'", "'bar'", "'baz'", "(#8)"]
    finally:
        os.unlink(path)


def test_tokenize_resilient_to_corrupt_entity():
    text = """DATA;
#1=PRODUCT('a','b','',(#2));
#2=CORRUPT_ENTITY('unterminated string ;
#3=MANIFOLD_SOLID_BREP('ok',#4);
#4=CLOSED_SHELL('',());
ENDSEC;
"""
    ents = tokenize_entities(text)
    # The corrupt entity #2 may or may not appear (it depends on how greedy
    # the recovery is) but #1 and either #3 or #4 must survive.
    assert 1 in ents
    assert ents[1][0] == "PRODUCT"
    # At least one healthy entity past the corruption survives.
    assert any(eid in ents for eid in (3, 4))


def test_tokenize_handles_quoted_semicolon():
    text = """DATA;
#1=PRODUCT('foo;bar','desc;with;semis','',(#2));
#2=PRODUCT_CONTEXT('',$,'mech');
ENDSEC;
"""
    ents = tokenize_entities(text)
    assert 1 in ents
    args = split_args(ents[1][1])
    # Semicolons inside the quoted string must not terminate the entity.
    assert args[0] == "'foo;bar'"
    assert args[1] == "'desc;with;semis'"


def test_tokenize_split_args_roundtrip():
    ents = tokenize_entities(_SAMPLE_STEP)
    args = split_args(ents[1][1])
    assert args[0] == "'id-1'"
    assert args[1] == "'demo plate'"
    # The third arg has a comma inside the quoted string -- must come back
    # as ONE lexical arg, not two.
    assert args[2] == "'description, with comma'"
    assert args[3] == "(#2)"


# ---------------------------------------------------------------------------
# parse_header
# ---------------------------------------------------------------------------


def test_parse_header_extracts_file_name():
    h = parse_header(_SAMPLE_STEP)
    assert "file_name" in h
    assert h["file_name"][0] == "'demo.stp'"
    assert "file_description" in h
    assert "file_schema" in h


def test_parse_header_missing_returns_empty():
    text = "DATA;\n#1=PRODUCT('x','y','',(#2));\nENDSEC;\n"
    assert parse_header(text) == {}


def test_parse_header_with_continuation_in_filename():
    raw = (
        b"HEADER;\n"
        b"FILE_DESCRIPTION(('d'),'2;1');\n"
        b"FILE_NAME('frag-\\\nname.stp','2026','','','','','');\n"
        b"FILE_SCHEMA(('AP214'));\n"
        b"ENDSEC;\n"
        b"DATA;\nENDSEC;\n"
    )
    path = _write_bytes(raw)
    try:
        text = read_step_text(path)
        h = parse_header(text)
        # After continuation collapse the filename token is a single quoted arg.
        assert "file_name" in h
        assert h["file_name"][0].startswith("'frag-")
    finally:
        os.unlink(path)
