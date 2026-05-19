"""Tests for the declarative dataclass <-> XML walker."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum

import pytest

from manufacturing_pipeline.io._xml_dataclass import (
    build_element,
    csv_reader,
    csv_writer,
    from_kebab,
    kv_reader,
    kv_writer,
    parse_element,
    to_kebab,
)


# ---------------------------------------------------------------------------
# Test 1: round-trip a flat dataclass with bool / int / float / str fields.
# ---------------------------------------------------------------------------


@dataclass
class _Flat:
    name: str = ""
    count: int = 0
    weight: float = 0.0
    enabled: bool = False


def test_round_trip_flat_scalar_fields():
    obj = _Flat(name="alpha", count=7, weight=3.5, enabled=True)
    el = build_element(None, "flat", obj)

    # All four fields land as attributes (kebab-cased).
    assert el.tag == "flat"
    assert el.get("name") == "alpha"
    assert el.get("count") == "7"
    assert el.get("weight") == repr(3.5)
    assert el.get("enabled") == "true"

    back = parse_element(el, _Flat)
    assert back == obj


# ---------------------------------------------------------------------------
# Test 2: list-of-dataclass round-trip.
# ---------------------------------------------------------------------------


@dataclass
class _Leaf:
    label: str
    score: float


@dataclass
class _Bag:
    name: str = ""
    leaves: list[_Leaf] = field(default_factory=list)


def test_list_of_dataclass_round_trip():
    bag = _Bag(name="bag1", leaves=[_Leaf("a", 0.1), _Leaf("b", 0.2)])
    el = build_element(None, "bag", bag)

    # The list field becomes a wrapping element + singular children.
    leaves = el.find("leaves")
    assert leaves is not None
    items = list(leaves)
    assert len(items) == 2
    assert items[0].get("label") == "a"
    assert items[1].get("score") == repr(0.2)

    back = parse_element(el, _Bag)
    assert back == bag


# ---------------------------------------------------------------------------
# Test 3: dict field round-trip via kv-element transformer.
# ---------------------------------------------------------------------------


@dataclass
class _WithParams:
    op: str
    params: dict = field(default_factory=dict)


def test_dict_field_round_trip_via_kv_transformer():
    obj = _WithParams(op="drill", params={"diameter_mm": 8.0, "through": True})

    el = build_element(
        None,
        "operation",
        obj,
        transformers={"params": kv_writer("param", key_attr="name")},
    )

    # params become <param name="..." value="..."/> children directly under
    # <operation> (no wrapper).
    params = el.findall("param")
    assert len(params) == 2
    names = {p.get("name") for p in params}
    assert names == {"diameter_mm", "through"}

    back = parse_element(
        el,
        _WithParams,
        transformers={"params": kv_reader("param", key_attr="name")},
    )
    assert back.op == "drill"
    assert back.params == {"diameter_mm": repr(8.0), "through": "true"}


# ---------------------------------------------------------------------------
# Test 4: custom transformer overrides default serialisation.
# ---------------------------------------------------------------------------


@dataclass
class _WithDatums:
    name: str
    datums: list[str] = field(default_factory=list)


def test_custom_transformer_overrides_default():
    obj = _WithDatums(name="tol1", datums=["A", "B", "C"])
    el = build_element(
        None,
        "tol",
        obj,
        transformers={"datums": csv_writer()},
    )
    # Should be a comma-joined attribute, not a child element.
    assert el.get("datums") == "A,B,C"
    assert el.find("datums") is None

    back = parse_element(
        el,
        _WithDatums,
        transformers={"datums": csv_reader("datums")},
    )
    assert back.name == "tol1"
    assert back.datums == ["A", "B", "C"]


def test_custom_transformer_omits_empty_list():
    obj = _WithDatums(name="tol_empty", datums=[])
    el = build_element(None, "tol", obj, transformers={"datums": csv_writer()})
    assert "datums" not in el.attrib


# ---------------------------------------------------------------------------
# Test 5: None/optional field omitted from output, parses back as None.
# ---------------------------------------------------------------------------


@dataclass
class _WithOptional:
    name: str
    note: str | None = None
    weight: float | None = None


def test_optional_field_omitted_when_none():
    obj = _WithOptional(name="x", note=None, weight=None)
    el = build_element(None, "thing", obj)

    assert el.get("name") == "x"
    assert "note" not in el.attrib
    assert "weight" not in el.attrib

    back = parse_element(el, _WithOptional)
    assert back.name == "x"
    assert back.note is None
    assert back.weight is None


def test_optional_field_present_when_set():
    obj = _WithOptional(name="x", note="hello", weight=4.5)
    el = build_element(None, "thing", obj)
    assert el.get("note") == "hello"
    assert el.get("weight") == repr(4.5)

    back = parse_element(el, _WithOptional)
    assert back == obj


# ---------------------------------------------------------------------------
# Bonus: name helpers + enum support.
# ---------------------------------------------------------------------------


class _Status(Enum):
    OK = "ok"
    BAD = "bad"


@dataclass
class _WithEnum:
    status: _Status = _Status.OK


def test_enum_field_round_trips():
    obj = _WithEnum(status=_Status.BAD)
    el = build_element(None, "x", obj)
    assert el.get("status") == "bad"
    back = parse_element(el, _WithEnum)
    assert back.status is _Status.BAD


def test_kebab_helpers_are_inverses():
    assert to_kebab("magnitude_mm") == "magnitude-mm"
    assert from_kebab("magnitude-mm") == "magnitude_mm"
    assert to_kebab("simple") == "simple"
    assert from_kebab("simple") == "simple"


def test_name_overrides_let_field_use_alternative_attr_name():
    @dataclass
    class _Custom:
        cls: str = ""
        magnitude_mm: float = 0.0

    obj = _Custom(cls="plaat", magnitude_mm=0.5)
    el = build_element(
        None, "c", obj, name_overrides={"cls": "class", "magnitude_mm": "magnitude"}
    )
    assert el.get("class") == "plaat"
    assert el.get("magnitude") == repr(0.5)
    back = parse_element(
        el, _Custom, name_overrides={"cls": "class", "magnitude_mm": "magnitude"}
    )
    assert back == obj


def test_empty_string_attr_is_omitted_by_default():
    obj = _Flat(name="", count=0, weight=0.0, enabled=False)
    el = build_element(None, "f", obj)
    # name was "" -> omitted
    assert "name" not in el.attrib
    # but count/weight/enabled remain (defaults of their type)
    assert el.get("count") == "0"
    assert el.get("weight") == repr(0.0)
    assert el.get("enabled") == "false"
