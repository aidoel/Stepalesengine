"""Property-based fuzz tests for the Part 21 tokeniser.

Phase 1 plan section 3.9 specified Hypothesis-driven fuzz tests for the
defensive tokeniser. The goal is *not* to assert "correct" output on
arbitrary input -- it is to assert that the tokeniser never raises and
that it preserves the small set of invariants we depend on downstream
(quoted commas, balanced parens, ASCII passthrough, garbage tolerance,
roundtrip of simple entities).
"""

from __future__ import annotations

import string

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from manufacturing_pipeline.parsing.step_tokenizer import (
    decode_step_string,
    parse_header,
    split_args,
    tokenize_entities,
)

# ---------------------------------------------------------------------------
# Reusable strategies
# ---------------------------------------------------------------------------

# Plain ASCII characters that contain neither backslash nor single quote, so
# they cannot accidentally form an X-escape or a string literal.
_SAFE_PRINTABLE = "".join(c for c in string.printable if c not in ("\\", "'"))

# Text built only from safe printable ASCII -- used for the idempotence test.
plain_ascii_text = st.text(alphabet=_SAFE_PRINTABLE, min_size=0, max_size=200)


# Generators for ISO 10303-21 X-encoded blobs of each width.
_HEX = "0123456789ABCDEFabcdef"


def _x1_blob() -> st.SearchStrategy[str]:
    return st.text(alphabet=_HEX, min_size=2, max_size=2).map(lambda h: "\\X\\" + h)


def _x2_blob() -> st.SearchStrategy[str]:
    # 1 to 6 UTF-16 code units (4 hex chars each).
    return st.integers(min_value=1, max_value=6).flatmap(
        lambda n: st.text(alphabet=_HEX, min_size=4 * n, max_size=4 * n).map(
            lambda h: "\\X2\\" + h + "\\X0\\"
        )
    )


def _x4_blob() -> st.SearchStrategy[str]:
    # 1 to 3 UTF-32 code points (8 hex chars each). Hypothesis can otherwise
    # generate code points >= 0x110000 which `chr` rejects; the regex handler
    # already catches that, so we don't need to bias the alphabet further.
    return st.integers(min_value=1, max_value=3).flatmap(
        lambda n: st.text(alphabet=_HEX, min_size=8 * n, max_size=8 * n).map(
            lambda h: "\\X4\\" + h + "\\X0\\"
        )
    )


_double_quote = st.just("''")

# A single "chunk" of text that may include normal characters, X-escapes,
# or doubled single quotes.
_chunk = st.one_of(
    st.text(alphabet=string.printable, min_size=0, max_size=20),
    _x1_blob(),
    _x2_blob(),
    _x4_blob(),
    _double_quote,
)

# A composite string: 0-6 chunks concatenated. Mixes safe ASCII with up to a
# handful of X-encoded blobs and doubled quotes.
encoded_step_string = st.lists(_chunk, min_size=0, max_size=6).map("".join)


# Strategy for arbitrary "argument-list-shaped" text. Each chunk is either
# random text, a quoted-string-shaped chunk, or a parenthesised sub-list.
def _arg_chunk() -> st.SearchStrategy[str]:
    raw = st.text(
        alphabet=string.ascii_letters + string.digits + ".,_#$* \t",
        min_size=0,
        max_size=10,
    )
    quoted = st.text(alphabet=_SAFE_PRINTABLE, min_size=0, max_size=10).map(
        lambda inner: f"'{inner}'"
    )
    nested = st.text(
        alphabet=string.ascii_letters + string.digits + ".,_#$* ",
        min_size=0,
        max_size=10,
    ).map(lambda inner: f"({inner})")
    return st.one_of(raw, quoted, nested)


arg_list_text = st.lists(_arg_chunk(), min_size=0, max_size=8).map(lambda chunks: ",".join(chunks))


# Arbitrary text up to 5 KB -- used to confirm tokenize_entities / parse_header
# never explode on random junk.
random_text_5kb = st.text(min_size=0, max_size=5000)


# ---------------------------------------------------------------------------
# 1. decode_step_string never raises
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(s=encoded_step_string)
def test_decode_step_string_never_raises(s: str) -> None:
    out = decode_step_string(s)
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# 2. decode_step_string is the identity on plain ASCII with no escapes
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(s=plain_ascii_text)
def test_decode_step_string_idempotent_on_plain_ascii(s: str) -> None:
    # By construction the alphabet excludes ``\`` and ``'`` so no X-escape or
    # doubled quote can appear; decode must be the identity.
    assert decode_step_string(s) == s


# ---------------------------------------------------------------------------
# 3. split_args never raises on arbitrary balanced-paren text
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(s=arg_list_text)
def test_split_args_never_raises(s: str) -> None:
    out = split_args(s)
    assert isinstance(out, list)
    # Every element is a string.
    assert all(isinstance(p, str) for p in out)


# ---------------------------------------------------------------------------
# 4. split_args preserves commas inside quoted strings
# ---------------------------------------------------------------------------


@given(
    quoted_body=st.text(alphabet=_SAFE_PRINTABLE + ",", min_size=1, max_size=40),
    middle_int=st.integers(min_value=-1000, max_value=1000),
    a=st.floats(allow_nan=False, allow_infinity=False, width=32),
    b=st.floats(allow_nan=False, allow_infinity=False, width=32),
)
@settings(max_examples=100, deadline=None)
def test_split_args_preserves_quoted_commas(
    quoted_body: str, middle_int: int, a: float, b: float
) -> None:
    """A quoted string containing commas must remain a single lexical arg."""
    arg_list = f"'{quoted_body}', {middle_int}, ({a}, {b})"
    parts = split_args(arg_list)
    assert len(parts) == 3, f"Expected 3 parts, got {len(parts)}: {parts!r}"
    # The first part should still be the entire quoted string with its commas.
    assert parts[0].startswith("'") and parts[0].endswith("'")
    # Strip the outer quotes and confirm the body survives intact.
    assert parts[0][1:-1] == quoted_body
    # Second part is the integer.
    assert parts[1] == str(middle_int)
    # Third part is the parenthesised tuple, unsplit.
    assert parts[2].startswith("(") and parts[2].endswith(")")


# Deterministic spot check that the example in the task description holds.
def test_split_args_preserves_quoted_commas_literal_example() -> None:
    parts = split_args("'a,b,c', 42, (1.0, 2.0)")
    assert len(parts) == 3
    assert ",b,c" in parts[0]
    assert parts[1] == "42"
    assert parts[2] == "(1.0, 2.0)"


# ---------------------------------------------------------------------------
# 5. tokenize_entities recovers a valid entity surrounded by garbage
# ---------------------------------------------------------------------------


_VALID_ENTITY = "#42=PRODUCT('id','name','desc',(#7));"


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)
@given(
    prefix=st.text(min_size=0, max_size=200),
    suffix=st.text(min_size=0, max_size=200),
)
def test_tokenize_entities_resilient_to_garbage(prefix: str, suffix: str) -> None:
    """Random prefix/suffix garbage must not hide a syntactically valid entity.

    The recovery is best-effort: a comment-open ``/*`` inside the prefix or a
    stray ``#42=...`` head in the garbage could legitimately swallow or shadow
    our planted entity. Filter those pathological cases out so the property
    captures the actual invariant.
    """
    # If the garbage itself contains a comment that swallows the planted
    # entity, or another #42= head, the property doesn't apply -- skip.
    if "/*" in prefix and "*/" not in prefix:
        return
    if "#42" in prefix or "#42" in suffix:
        return

    text = prefix + "\n" + _VALID_ENTITY + "\n" + suffix
    ents = tokenize_entities(text)
    assert 42 in ents, f"Lost #42 with prefix={prefix!r}, suffix={suffix!r}"
    name, args = ents[42]
    assert name == "PRODUCT"
    assert "'id'" in args


# ---------------------------------------------------------------------------
# 6. tokenize_entities never raises on random input
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(s=random_text_5kb)
def test_tokenize_entities_never_raises_on_random_input(s: str) -> None:
    out = tokenize_entities(s)
    assert isinstance(out, dict)
    # All keys are ints and all values are 2-tuples of (str, str).
    for k, v in out.items():
        assert isinstance(k, int)
        assert isinstance(v, tuple) and len(v) == 2
        assert isinstance(v[0], str) and isinstance(v[1], str)


# ---------------------------------------------------------------------------
# 7. parse_header never raises on random input
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(s=random_text_5kb)
def test_parse_header_never_raises(s: str) -> None:
    out = parse_header(s)
    assert isinstance(out, dict)
    # Recognised keys (if present) carry list values.
    for k, v in out.items():
        assert isinstance(k, str)
        assert isinstance(v, list)


# ---------------------------------------------------------------------------
# 8. Roundtrip: build a synthetic entity, tokenize, retrieve, split
# ---------------------------------------------------------------------------


_ENTITY_NAME = st.text(alphabet=string.ascii_uppercase + "_", min_size=1, max_size=20).filter(
    lambda s: s[0].isalpha()
)


@settings(max_examples=100, deadline=None)
@given(
    ent_id=st.integers(min_value=1, max_value=10_000_000),
    name=_ENTITY_NAME,
    label=st.text(alphabet=_SAFE_PRINTABLE, min_size=0, max_size=20),
    value=st.floats(
        allow_nan=False,
        allow_infinity=False,
        min_value=-1e6,
        max_value=1e6,
        width=32,
    ),
)
def test_roundtrip_simple_entity(ent_id: int, name: str, label: str, value: float) -> None:
    """Build a Part 21 entity from random pieces and roundtrip through the
    tokeniser. We must recover the entity id, name, and three args (label,
    numeric value, ``$``)."""
    entity = f"#{ent_id} = {name}('{label}', {value}, $);"
    text = f"DATA;\n{entity}\nENDSEC;\n"

    ents = tokenize_entities(text)
    assert ent_id in ents, f"Lost entity id {ent_id}"
    got_name, raw_args = ents[ent_id]
    assert got_name == name

    args = split_args(raw_args)
    assert len(args) == 3, f"Expected 3 args, got {len(args)}: {args!r}"
    assert args[0] == f"'{label}'"
    # The numeric arg gets whitespace-stripped but is otherwise verbatim; we
    # don't assert exact equality because Python's float repr may differ from
    # the format-spec we used.
    assert args[1] == str(value)
    assert args[2] == "$"
