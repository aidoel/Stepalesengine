"""Declarative dataclass <-> XML element walker.

Used by :mod:`manufacturing_pipeline.io.xml_writer` to render simple
dataclasses (``Contribution``, ``GeometricTolerance``, ``Datum``,
``SurfaceFinish``, ``DimensionalTolerance``, ``Operation``) into XML elements
and parse them back without writing paired ``_build_X`` / ``_parse_X``
functions for every leaf type.

The walker is intentionally narrow: it covers the simple "flat dataclass"
case where every field maps to a single attribute or a uniform list of
sub-elements. Anything more bespoke (tuples-as-attributes, fields that get
renamed, namespace-bearing roots) stays in ``xml_writer.py`` as manual
code.

Defaults
--------
- ``bool`` -> ``"true"``/``"false"`` (lowercase)
- ``int`` -> ``str(int)``
- ``float`` -> ``repr(float)`` (round-trip exact)
- ``str``, ``Path`` -> ``str(value)``
- ``Enum`` -> ``value.value``
- ``list`` of dataclass -> one child element per item
- ``list`` of primitives -> joined with ``,``
- ``dict`` -> ``<kv key=".." value=".."/>`` children (use the
  ``dict_to_kv_elements`` transformer pair to override key/value tags)
- nested dataclass -> nested child element
- ``None`` values are omitted from output; missing attributes parse as
  ``None`` (or the field's default, if one exists).

Field-name to attribute-name uses :func:`to_kebab` by default. Override per
field with ``name_overrides`` (e.g. ``{"cls": "class"}``).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import MISSING, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints


def _resolved_field_type(cls: type, field_name: str) -> Any:
    """Resolve a dataclass field's type, accounting for PEP 563
    ``from __future__ import annotations``."""
    try:
        hints = get_type_hints(cls)
    except Exception:
        return None
    return hints.get(field_name)


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------


def to_kebab(name: str) -> str:
    """``field_name`` -> ``field-name``."""
    return name.replace("_", "-")


def from_kebab(name: str) -> str:
    """``field-name`` -> ``field_name``."""
    return name.replace("-", "_")


# ---------------------------------------------------------------------------
# Serialisation primitives — match xml_writer's existing semantics exactly.
# ---------------------------------------------------------------------------


def _format_value(value: Any) -> str:
    """Render a scalar value to its XML-attribute string form."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(float(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _parse_scalar(raw: str, target_type: Any) -> Any:
    """Inverse of :func:`_format_value` for known scalar target types."""
    if target_type is bool:
        return raw.strip().lower() == "true"
    if target_type is int:
        return int(raw)
    if target_type is float:
        return float(raw)
    if isinstance(target_type, type) and issubclass(target_type, Enum):
        return target_type(raw)
    if target_type is Path:
        return Path(raw)
    # Handle Union (e.g. ``float | str``): try each member in order, fall back
    # to the raw string if every conversion fails.
    if get_origin(target_type) is not None:
        for member in get_args(target_type):
            if member is type(None):
                continue
            try:
                return _parse_scalar(raw, member)
            except (TypeError, ValueError):
                continue
        return raw
    return raw


# ---------------------------------------------------------------------------
# Type inspection
# ---------------------------------------------------------------------------


def _unwrap_optional(tp: Any) -> Any:
    """Return the inner type of ``X | None`` / ``Optional[X]``; else ``tp``."""
    origin = get_origin(tp)
    if origin is None:
        return tp
    args = [a for a in get_args(tp) if a is not type(None)]
    if origin in (list, dict, tuple):
        return tp
    # typing.Union / X | None
    if len(args) == 1:
        return args[0]
    return tp


def _is_optional(tp: Any) -> bool:
    """Return True if ``tp`` is ``X | None`` / ``Optional[X]``."""
    if get_origin(tp) is None:
        return False
    return type(None) in get_args(tp)


def _is_scalar_type(tp: Any) -> bool:
    tp = _unwrap_optional(tp)
    if tp in (str, int, float, bool, Path):
        return True
    if isinstance(tp, type) and issubclass(tp, Enum):
        return True
    return False


# ---------------------------------------------------------------------------
# build_element
# ---------------------------------------------------------------------------


def build_element(
    parent: ET.Element | None,
    tag: str,
    obj: Any,
    *,
    attr_fields: set[str] | None = None,
    element_fields: set[str] | None = None,
    transformers: dict[str, Callable[..., Any]] | None = None,
    name_overrides: dict[str, str] | None = None,
    skip_fields: set[str] | None = None,
    omit_empty_strings: bool = True,
) -> ET.Element:
    """Build an XML element from a dataclass instance.

    Parameters
    ----------
    parent:
        Parent element to attach to. Pass ``None`` to return a standalone
        element.
    tag:
        Element tag name.
    obj:
        Dataclass instance.
    attr_fields:
        Field names that MUST be rendered as attributes (overrides the
        default classification).
    element_fields:
        Field names that MUST be rendered as nested elements (overrides the
        default classification).
    transformers:
        ``{field_name: callable(value) -> str | None | (elem_builder)}``.
        Callables that return ``None`` omit the field. Callables that
        return a string set the attribute. To build child elements, the
        transformer receives ``(parent_elem, field_value)`` (two args) and
        returns ``None``; the walker detects the two-arg signature.
    name_overrides:
        ``{field_name: attribute_or_tag_name}`` to override
        :func:`to_kebab`. Use for ``cls`` -> ``class`` or
        ``magnitude_mm`` -> ``magnitude``.
    skip_fields:
        Field names to skip entirely.
    omit_empty_strings:
        If True (default), empty string values are omitted from the output
        (matches the existing schema convention for optional string
        attributes).
    """
    if not is_dataclass(obj):
        raise TypeError(f"build_element expected a dataclass, got {type(obj).__name__}")

    attr_fields = attr_fields or set()
    element_fields = element_fields or set()
    transformers = transformers or {}
    name_overrides = name_overrides or {}
    skip_fields = skip_fields or set()

    el = ET.SubElement(parent, tag) if parent is not None else ET.Element(tag)

    for f in fields(obj):
        if f.name in skip_fields:
            continue
        value = getattr(obj, f.name)
        target_name = name_overrides.get(f.name, to_kebab(f.name))

        # Custom transformer takes precedence.
        if f.name in transformers:
            tx = transformers[f.name]
            # Two-arg form: builds child elements directly.
            if getattr(tx, "_xml_element_builder", False):
                tx(el, value)
                continue
            rendered = tx(value)
            if rendered is None:
                continue
            el.set(target_name, rendered)
            continue

        if value is None:
            continue

        # Forced attribute / element classification.
        if f.name in attr_fields:
            el.set(target_name, _format_value(value))
            continue
        if f.name in element_fields:
            _build_child(el, target_name, value)
            continue

        # Default classification by type.
        if _is_scalar_type(type(value)) or isinstance(value, (str, int, float, bool, Path, Enum)):
            # Omit only empty strings on fields that have a default - required
            # fields must always be emitted so parse_element can reconstruct.
            field_has_default = f.default is not MISSING or f.default_factory is not MISSING
            if isinstance(value, str) and omit_empty_strings and value == "" and field_has_default:
                continue
            el.set(target_name, _format_value(value))
        elif isinstance(value, list):
            if not value:
                continue
            # list of dataclasses -> child elements
            if is_dataclass(value[0]):
                container = ET.SubElement(el, target_name)
                # Singularise: "items" -> "item" if the field ends in 's'.
                child_tag = target_name[:-1] if target_name.endswith("s") else target_name
                for item in value:
                    build_element(container, child_tag, item)
            else:
                # list of primitives -> comma-joined attribute
                el.set(target_name, ",".join(_format_value(v) for v in value))
        elif isinstance(value, dict):
            if not value:
                continue
            container = ET.SubElement(el, target_name)
            for k, v in value.items():
                kv = ET.SubElement(container, "kv")
                kv.set("key", str(k))
                kv.set("value", _format_value(v))
        elif is_dataclass(value):
            build_element(el, target_name, value)
        else:
            el.set(target_name, _format_value(value))

    return el


def _build_child(parent: ET.Element, tag: str, value: Any) -> None:
    """Build a single child element for the forced ``element_fields`` case."""
    if is_dataclass(value):
        build_element(parent, tag, value)
    else:
        child = ET.SubElement(parent, tag)
        child.text = _format_value(value)


# ---------------------------------------------------------------------------
# parse_element
# ---------------------------------------------------------------------------


def parse_element(
    elem: ET.Element,
    cls: type,
    *,
    transformers: dict[str, Callable[[ET.Element], Any]] | None = None,
    name_overrides: dict[str, str] | None = None,
    skip_fields: set[str] | None = None,
) -> Any:
    """Inverse of :func:`build_element` for a flat dataclass ``cls``.

    Each field is populated from either an attribute (default) or a child
    element (lists / dicts / nested dataclasses).

    Transformers receive the full element and return the parsed field
    value (so they can read child elements, multiple attrs, etc).
    Returning ``None`` from a transformer leaves the dataclass default.
    """
    if not is_dataclass(cls):
        raise TypeError(f"parse_element expected a dataclass type, got {cls!r}")

    transformers = transformers or {}
    name_overrides = name_overrides or {}
    skip_fields = skip_fields or set()

    kwargs: dict[str, Any] = {}

    for f in fields(cls):
        if f.name in skip_fields:
            continue

        target_name = name_overrides.get(f.name, to_kebab(f.name))

        if f.name in transformers:
            parsed = transformers[f.name](elem)
            if parsed is not None:
                kwargs[f.name] = parsed
            continue

        resolved = _resolved_field_type(cls, f.name)
        ftype = _unwrap_optional(resolved) if resolved is not None else None
        origin = get_origin(ftype) if ftype is not None else None

        # Tagged child elements: list / dict / nested dataclass.
        if origin is list:
            child = _find_child(elem, target_name)
            if child is None:
                # Maybe the list is comma-joined on the attribute instead.
                if target_name in elem.attrib:
                    raw = elem.attrib[target_name]
                    inner_args = get_args(ftype)
                    inner = inner_args[0] if inner_args else str
                    inner = _unwrap_optional(inner)
                    kwargs[f.name] = [_parse_scalar(p, inner) for p in raw.split(",") if p]
                continue
            inner_args = get_args(ftype)
            inner = inner_args[0] if inner_args else str
            inner = _unwrap_optional(inner)
            if is_dataclass(inner) and isinstance(inner, type):
                child_tag = target_name[:-1] if target_name.endswith("s") else target_name
                kwargs[f.name] = [parse_element(c, inner) for c in _findall_child(child, child_tag)]
            else:
                # Primitives nested as <items><item value=".."/></items>
                # Not currently emitted by build_element, but handle gracefully.
                continue
        elif origin is dict:
            child = _find_child(elem, target_name)
            if child is None:
                continue
            result: dict[str, str] = {}
            for kv in _findall_child(child, "kv"):
                k = kv.get("key", "")
                v = kv.get("value", "")
                result[k] = v
            kwargs[f.name] = result
        elif ftype is not None and is_dataclass(ftype) and isinstance(ftype, type):
            child = _find_child(elem, target_name)
            if child is not None:
                kwargs[f.name] = parse_element(child, ftype)
        else:
            # Scalar attribute.
            if target_name in elem.attrib:
                raw = elem.attrib[target_name]
                kwargs[f.name] = _parse_scalar(raw, ftype if ftype is not None else str)
            # else: leave at default (or None if Optional + no default)

    # Fill in None for required Optional fields that were absent in the XML.
    for f in fields(cls):
        if f.name in kwargs:
            continue
        if f.default is not MISSING or f.default_factory is not MISSING:
            continue
        resolved = _resolved_field_type(cls, f.name)
        if resolved is not None and _is_optional(resolved):
            kwargs[f.name] = None

    return cls(**kwargs)


def _find_child(parent: ET.Element, name: str) -> ET.Element | None:
    for child in parent:
        if _strip_ns(child.tag) == name:
            return child
    return None


def _findall_child(parent: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in parent if _strip_ns(child.tag) == name]


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


# ---------------------------------------------------------------------------
# Common transformer factories
# ---------------------------------------------------------------------------


def kv_writer(
    tag: str = "kv", *, key_attr: str = "key", value_attr: str = "value"
) -> Callable[[ET.Element, dict], None]:
    """Return a two-arg transformer that emits ``<tag key=".." value=".."/>``
    children directly under the dataclass element (no wrapper)."""

    def _emit(parent: ET.Element, mapping: dict) -> None:
        if not mapping:
            return
        for k, v in mapping.items():
            child = ET.SubElement(parent, tag)
            child.set(key_attr, str(k))
            child.set(value_attr, _format_value(v))

    _emit._xml_element_builder = True  # type: ignore[attr-defined]
    return _emit


def kv_reader(
    tag: str = "kv",
    *,
    key_attr: str = "key",
    value_attr: str = "value",
    value_type: type | None = None,
) -> Callable[[ET.Element], dict]:
    """Return a transformer that reads ``<tag key=".." value=".."/>``
    children placed directly under the dataclass element.

    ``value_type``: optional scalar type to coerce values into (``float``,
    ``int``, ``bool``, ``str``). Defaults to ``str``."""

    def _read(elem: ET.Element) -> dict:
        result: dict[str, Any] = {}
        for child in _findall_child(elem, tag):
            raw = child.get(value_attr, "")
            if value_type is None:
                result[child.get(key_attr, "")] = raw
            else:
                result[child.get(key_attr, "")] = _parse_scalar(raw, value_type)
        return result

    return _read


def csv_writer() -> Callable[[Any], str | None]:
    """Comma-join a list of strings; return ``None`` for empty lists."""

    def _emit(value):
        if not value:
            return None
        return ",".join(value)

    return _emit


def csv_reader(attr: str) -> Callable[[ET.Element], list[str]]:
    """Read a comma-separated attribute back into a list of strings."""

    def _read(elem: ET.Element) -> list[str]:
        raw = elem.get(attr, "") or ""
        return [p for p in raw.split(",") if p]

    return _read
