"""XML BOM/manifest writer.

Renders an `AssemblyManifest` (containing parts, classifications, decision
traces, geometric features, profile matches, unfold results, and hole patterns)
to a single XML file, and parses it back. Stdlib only — `xml.etree.ElementTree`.

The on-disk format lives under the namespace
``https://stepalesengine.dev/manifest/1`` and is designed to round-trip exactly
through `read_xml(write_xml(m))`.

Trivial dataclasses (`Contribution`, `GeometricTolerance`, `DimensionalTolerance`,
`Datum`, `SurfaceFinish`, `Operation`) are serialised by the declarative walker
in :mod:`._xml_dataclass`; the heterogeneous ones (`ManufacturingFeatures`,
`UnfoldResult`, `ClassificationResult`, etc.) keep manual code because they
mix tuples-as-attribute-groups, per-field unit hints, and other one-off
patterns that the walker is intentionally narrow about.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from manufacturing_pipeline.cam.types import MachiningStrategy, Operation
from manufacturing_pipeline.classification.types import (
    ClassificationResult,
    Contribution,
    DecisionTrace,
)
from manufacturing_pipeline.geometry.types import (
    HolePattern,
    ManufacturingFeatures,
    ProfileMatch,
    UnfoldResult,
    UnfoldStatus,
)
from manufacturing_pipeline.io._xml_dataclass import (
    build_element,
    csv_reader,
    csv_writer,
    kv_reader,
    kv_writer,
    parse_element,
)
from manufacturing_pipeline.parsing.types import StepPart
from manufacturing_pipeline.pmi.types import (
    Datum,
    DimensionalTolerance,
    GeometricTolerance,
    PMIRecord,
    SurfaceFinish,
)

NAMESPACE = "https://stepalesengine.dev/manifest/1"
_NS = f"{{{NAMESPACE}}}"


class ManifestParseError(ValueError):
    """Raised when an XML manifest cannot be parsed."""


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PartManifestEntry:
    part: StepPart
    classification: ClassificationResult
    features: ManufacturingFeatures | None = None
    profile_match: ProfileMatch | None = None
    unfold: UnfoldResult | None = None
    holes: HolePattern | None = None
    quantity: int = 1
    flat_dxf_path: str | None = None
    pmi: PMIRecord | None = None
    strategy: MachiningStrategy | None = None


@dataclass
class AssemblyManifest:
    source_path: str
    source_mtime: float
    source_size: int
    model_version: str
    generated_at: str
    parts: list[PartManifestEntry] = field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Float helpers — repr(float) round-trips exactly for finite floats.
# ---------------------------------------------------------------------------


def _f(value: float) -> str:
    return repr(float(value))


def _pf(s: str) -> float:
    return float(s)


def _pi(s: str) -> int:
    return int(s)


def _pb(s: str) -> bool:
    return s.strip().lower() == "true"


def _bs(value: bool) -> str:
    return "true" if value else "false"


# ---------------------------------------------------------------------------
# Element builders
# ---------------------------------------------------------------------------


def _sub(parent: ET.Element, tag: str, **attrs: object) -> ET.Element:
    el = ET.SubElement(parent, tag)
    for k, v in attrs.items():
        if v is None:
            continue
        el.set(k.replace("_", "-"), str(v))
    return el


def _build_classification(parent: ET.Element, c: ClassificationResult) -> None:
    cl = _sub(parent, "classification", label=c.label, confidence=_f(c.confidence))
    t = c.trace

    scores_el = ET.SubElement(cl, "scores")
    for cls_name, val in t.scores.items():
        _sub(scores_el, "score", **{"class": cls_name, "value": _f(val)})

    probs_el = ET.SubElement(cl, "probabilities")
    for cls_name, val in t.probabilities.items():
        _sub(probs_el, "probability", **{"class": cls_name, "value": _f(val)})

    _sub(cl, "margin", value=_f(t.margin), ambiguous=_bs(t.ambiguous))

    contribs_el = ET.SubElement(cl, "contributions")
    for contrib in t.contributions:
        build_element(
            contribs_el,
            "contribution",
            contrib,
            name_overrides={"cls": "class"},
            omit_empty_strings=False,
        )

    tb_el = ET.SubElement(cl, "tiebreakers")
    for name in t.tiebreakers_run:
        _sub(tb_el, "tiebreaker", name=name)

    if t.probe_results:
        pr_el = ET.SubElement(cl, "probe-results")
        for key, val in t.probe_results.items():
            _sub(pr_el, "probe", name=key, value=str(val))

    _sub(cl, "model-version", value=t.model_version)


def _build_features(parent: ET.Element, f: ManufacturingFeatures) -> None:
    fe = ET.SubElement(parent, "features")
    _sub(fe, "volume", value=_f(f.volume), units="mm3")
    _sub(fe, "surface-area", value=_f(f.surface_area), units="mm2")
    bb = f.bbox_dims_sorted
    _sub(
        fe,
        "bbox",
        length=_f(bb[0]),
        width=_f(bb[1]),
        thickness=_f(bb[2]),
        units="mm",
    )
    _sub(fe, "aspect-ratio", value=_f(f.aspect_ratio))
    _sub(fe, "thickness-ratio", value=_f(f.thickness_ratio))

    if f.face_area_top:
        fat = ET.SubElement(fe, "face-area-top")
        for i, pct in enumerate(f.face_area_top, start=1):
            _sub(fat, "face", rank=str(i), pct=_f(pct))

    if f.surface_pct:
        comp = ET.SubElement(fe, "surface-composition")
        for surf_type, pct in f.surface_pct.items():
            _sub(comp, "surface", type=surf_type, pct=_f(pct))

    if f.edge_counts:
        ec = ET.SubElement(fe, "edge-counts")
        for kind, n in f.edge_counts.items():
            _sub(ec, "edge", kind=kind, count=str(int(n)))

    er = f.edge_radius
    _sub(fe, "edge-radius", mean=_f(er[0]), std=_f(er[1]))

    _sub(fe, "hole-count", value=str(f.hole_count))
    if f.hole_diameters:
        hd = ET.SubElement(fe, "hole-diameters")
        for d in f.hole_diameters:
            _sub(hd, "diameter", value=_f(d), units="mm")

    _sub(fe, "sa-v-ratio", value=_f(f.sa_v_ratio))
    _sub(fe, "bounding-cylinder-fit-pct", value=_f(f.bounding_cylinder_fit_pct))
    _sub(fe, "convex-hull-volume-ratio", value=_f(f.convex_hull_volume_ratio))
    _sub(fe, "cross-section-constant", value=_bs(f.cross_section_constant))

    if f.cross_section_signature:
        css = ET.SubElement(fe, "cross-section-signature")
        for k, v in f.cross_section_signature.items():
            _sub(css, "entry", key=k, value=str(v))

    _sub(fe, "hollow", **{"inner-shells": str(f.inner_shell_count), "is-hollow": _bs(f.is_hollow)})
    _sub(fe, "source", value=f.source)


_PROFILE_MATCH_TRANSFORMERS_WRITE = {
    "fitted_dims": kv_writer("fitted-dim", key_attr="name"),
}
_PROFILE_MATCH_TRANSFORMERS_READ = {
    "fitted_dims": kv_reader("fitted-dim", key_attr="name", value_type=float),
}


def _build_profile_match(parent: ET.Element, p: ProfileMatch) -> None:
    build_element(
        parent,
        "profile-match",
        p,
        transformers=_PROFILE_MATCH_TRANSFORMERS_WRITE,
    )


def _build_unfold(parent: ET.Element, u: UnfoldResult) -> None:
    uf = _sub(
        parent,
        "unfold",
        status=u.status.value,
        **{
            "n-bends": str(u.n_bends),
            "flat-area": _f(u.flat_area),
            "thickness-mean": _f(u.thickness_mean),
            "thickness-cv": _f(u.thickness_cv),
        },
    )
    if u.blank_bbox is not None and len(u.blank_bbox) >= 2:
        _sub(
            uf,
            "blank-bbox",
            length=_f(u.blank_bbox[0]),
            width=_f(u.blank_bbox[1]),
            units="mm",
        )
    for name, val in u.flags.items():
        _sub(uf, "flag", name=name, value=_bs(bool(val)) if isinstance(val, bool) else str(val))
    if u.reason:
        _sub(uf, "reason", text=u.reason)


def _build_holes(parent: ET.Element, h: HolePattern) -> None:
    hl = _sub(parent, "holes", count=str(h.hole_count))
    for d in h.diameters:
        _sub(hl, "hole", diameter=_f(d), units="mm")


_TOLERANCE_OVERRIDES = {"magnitude_mm": "magnitude"}
_TOLERANCE_TRANSFORMERS_WRITE = {"datums": csv_writer()}
_TOLERANCE_TRANSFORMERS_READ = {"datums": csv_reader("datums")}


def _build_pmi(parent: ET.Element, p: PMIRecord) -> None:
    pmi_el = _sub(
        parent,
        "pmi",
        **{"n-annotations": str(p.n_annotations), "has-semantic": _bs(p.has_semantic)},
    )

    if p.tolerances:
        tols = ET.SubElement(pmi_el, "tolerances")
        for t in p.tolerances:
            build_element(
                tols,
                "tolerance",
                t,
                name_overrides=_TOLERANCE_OVERRIDES,
                transformers=_TOLERANCE_TRANSFORMERS_WRITE,
            )

    if p.dimensions:
        dims = ET.SubElement(pmi_el, "dimensions")
        for d in p.dimensions:
            build_element(dims, "dimension", d)

    if p.datums:
        ds = ET.SubElement(pmi_el, "datums")
        for dt in p.datums:
            build_element(ds, "datum", dt)

    if p.finishes:
        fs = ET.SubElement(pmi_el, "finishes")
        for f in p.finishes:
            build_element(fs, "finish", f)


def _operation_param_writer():
    """Emit Operation.params as ``<param name=".." value=".."/>`` children using
    ``str(val)`` (NOT ``_format_value``) — matches the legacy schema where
    booleans render as ``"True"``/``"False"`` and floats as ``str(float)``."""
    def _emit(parent: ET.Element, mapping: dict) -> None:
        if not mapping:
            return
        for k, v in mapping.items():
            child = ET.SubElement(parent, "param")
            child.set("name", str(k))
            child.set("value", str(v))
    _emit._xml_element_builder = True  # type: ignore[attr-defined]
    return _emit


_OPERATION_TRANSFORMERS_WRITE = {"params": _operation_param_writer()}
_OPERATION_TRANSFORMERS_READ = {"params": kv_reader("param", key_attr="name")}


def _build_strategy(parent: ET.Element, s: MachiningStrategy) -> None:
    ms = _sub(
        parent,
        "machining-strategy",
        **{
            "primary-process": s.primary_process,
            "setup-count": str(int(s.setup_count)),
            "material-hint": s.material_hint or None,
            "estimated-time-min": _f(s.estimated_time_min),
        },
    )
    for op in s.operations:
        build_element(
            ms,
            "operation",
            op,
            transformers=_OPERATION_TRANSFORMERS_WRITE,
        )
    if s.notes:
        notes_el = ET.SubElement(ms, "notes")
        notes_el.text = s.notes


def _build_part(parent: ET.Element, entry: PartManifestEntry) -> None:
    p = entry.part
    pa = _sub(
        parent,
        "part",
        **{
            "product-id": p.product_id,
            "name": p.name,
            "description": p.description,
            "source": p.source,
            "quantity": str(entry.quantity),
        },
    )
    _build_classification(pa, entry.classification)

    if entry.features is not None:
        _build_features(pa, entry.features)
    if entry.profile_match is not None:
        _build_profile_match(pa, entry.profile_match)
    if entry.unfold is not None:
        _build_unfold(pa, entry.unfold)
    if entry.holes is not None:
        _build_holes(pa, entry.holes)
    if entry.pmi is not None:
        _build_pmi(pa, entry.pmi)
    if entry.strategy is not None:
        _build_strategy(pa, entry.strategy)

    if p.children:
        ch_el = ET.SubElement(pa, "children")
        for ref in p.children:
            _sub(ch_el, "child", ref=ref)

    if entry.flat_dxf_path is not None:
        _sub(pa, "flat-dxf", path=entry.flat_dxf_path)


# ---------------------------------------------------------------------------
# write_xml
# ---------------------------------------------------------------------------


def write_xml(manifest: AssemblyManifest, out_path: str | Path, *, pretty: bool = True) -> Path:
    """Render `manifest` to an XML file at `out_path`. Returns the resolved path."""
    out = Path(out_path)
    root = ET.Element(
        "assembly",
        {
            "xmlns": NAMESPACE,
            "source": manifest.source_path,
            "source-mtime": _f(manifest.source_mtime),
            "source-size": str(manifest.source_size),
            "generated-at": manifest.generated_at,
            "model-version": manifest.model_version,
        },
    )
    if manifest.notes:
        notes_el = ET.SubElement(root, "notes")
        notes_el.text = manifest.notes

    parts_el = ET.SubElement(root, "parts")
    for entry in manifest.parts:
        _build_part(parts_el, entry)

    if pretty:
        ET.indent(root, space="  ")

    tree = ET.ElementTree(root)
    tree.write(out, encoding="utf-8", xml_declaration=True)
    return out


# ---------------------------------------------------------------------------
# read_xml
# ---------------------------------------------------------------------------


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def _find(parent: ET.Element, name: str) -> ET.Element | None:
    for child in parent:
        if _strip_ns(child.tag) == name:
            return child
    return None


def _findall(parent: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in parent if _strip_ns(child.tag) == name]


def _attr(el: ET.Element, name: str, default: str | None = None) -> str | None:
    return el.attrib.get(name, default)


def _parse_classification(el: ET.Element) -> ClassificationResult:
    label = _attr(el, "label") or ""
    confidence = _pf(_attr(el, "confidence") or "0.0")

    trace = DecisionTrace()

    scores_el = _find(el, "scores")
    if scores_el is not None:
        for s in _findall(scores_el, "score"):
            trace.scores[_attr(s, "class") or ""] = _pf(_attr(s, "value") or "0.0")

    probs_el = _find(el, "probabilities")
    if probs_el is not None:
        for p in _findall(probs_el, "probability"):
            trace.probabilities[_attr(p, "class") or ""] = _pf(_attr(p, "value") or "0.0")

    margin_el = _find(el, "margin")
    if margin_el is not None:
        trace.margin = _pf(_attr(margin_el, "value") or "0.0")
        trace.ambiguous = _pb(_attr(margin_el, "ambiguous") or "false")

    contribs_el = _find(el, "contributions")
    if contribs_el is not None:
        for c in _findall(contribs_el, "contribution"):
            trace.contributions.append(
                parse_element(c, Contribution, name_overrides={"cls": "class"})
            )

    tb_el = _find(el, "tiebreakers")
    if tb_el is not None:
        for tb in _findall(tb_el, "tiebreaker"):
            name = _attr(tb, "name")
            if name:
                trace.tiebreakers_run.append(name)

    pr_el = _find(el, "probe-results")
    if pr_el is not None:
        for probe in _findall(pr_el, "probe"):
            trace.probe_results[_attr(probe, "name") or ""] = _attr(probe, "value") or ""

    mv_el = _find(el, "model-version")
    if mv_el is not None:
        trace.model_version = _attr(mv_el, "value") or trace.model_version

    return ClassificationResult(label=label, confidence=confidence, trace=trace)


def _parse_features(el: ET.Element) -> ManufacturingFeatures:
    f = ManufacturingFeatures()

    vol = _find(el, "volume")
    if vol is not None:
        f.volume = _pf(_attr(vol, "value") or "0.0")
    sa = _find(el, "surface-area")
    if sa is not None:
        f.surface_area = _pf(_attr(sa, "value") or "0.0")
    bb = _find(el, "bbox")
    if bb is not None:
        f.bbox_dims_sorted = (
            _pf(_attr(bb, "length") or "0.0"),
            _pf(_attr(bb, "width") or "0.0"),
            _pf(_attr(bb, "thickness") or "0.0"),
        )
    ar = _find(el, "aspect-ratio")
    if ar is not None:
        f.aspect_ratio = _pf(_attr(ar, "value") or "0.0")
    tr = _find(el, "thickness-ratio")
    if tr is not None:
        f.thickness_ratio = _pf(_attr(tr, "value") or "0.0")

    fat = _find(el, "face-area-top")
    if fat is not None:
        ranked = sorted(_findall(fat, "face"), key=lambda x: int(_attr(x, "rank") or "0"))
        f.face_area_top = [_pf(_attr(face, "pct") or "0.0") for face in ranked]

    comp = _find(el, "surface-composition")
    if comp is not None:
        for s in _findall(comp, "surface"):
            f.surface_pct[_attr(s, "type") or ""] = _pf(_attr(s, "pct") or "0.0")

    ec = _find(el, "edge-counts")
    if ec is not None:
        for e in _findall(ec, "edge"):
            f.edge_counts[_attr(e, "kind") or ""] = int(_attr(e, "count") or "0")

    er = _find(el, "edge-radius")
    if er is not None:
        f.edge_radius = (
            _pf(_attr(er, "mean") or "0.0"),
            _pf(_attr(er, "std") or "0.0"),
        )

    hc = _find(el, "hole-count")
    if hc is not None:
        f.hole_count = _pi(_attr(hc, "value") or "0")
    hd = _find(el, "hole-diameters")
    if hd is not None:
        f.hole_diameters = [_pf(_attr(d, "value") or "0.0") for d in _findall(hd, "diameter")]

    sav = _find(el, "sa-v-ratio")
    if sav is not None:
        f.sa_v_ratio = _pf(_attr(sav, "value") or "0.0")
    bc = _find(el, "bounding-cylinder-fit-pct")
    if bc is not None:
        f.bounding_cylinder_fit_pct = _pf(_attr(bc, "value") or "0.0")
    chr_ = _find(el, "convex-hull-volume-ratio")
    if chr_ is not None:
        f.convex_hull_volume_ratio = _pf(_attr(chr_, "value") or "0.0")
    csc = _find(el, "cross-section-constant")
    if csc is not None:
        f.cross_section_constant = _pb(_attr(csc, "value") or "false")

    css = _find(el, "cross-section-signature")
    if css is not None:
        for entry in _findall(css, "entry"):
            f.cross_section_signature[_attr(entry, "key") or ""] = _attr(entry, "value") or ""

    hol = _find(el, "hollow")
    if hol is not None:
        f.inner_shell_count = _pi(_attr(hol, "inner-shells") or "0")
        f.is_hollow = _pb(_attr(hol, "is-hollow") or "false")

    src = _find(el, "source")
    if src is not None:
        f.source = _attr(src, "value") or "brep"

    return f


def _parse_profile_match(el: ET.Element) -> ProfileMatch:
    return parse_element(
        el,
        ProfileMatch,
        transformers=_PROFILE_MATCH_TRANSFORMERS_READ,
    )


def _parse_unfold(el: ET.Element) -> UnfoldResult:
    status = UnfoldStatus(_attr(el, "status") or "failure")
    blank_bbox: tuple | None = None
    bb = _find(el, "blank-bbox")
    if bb is not None:
        blank_bbox = (
            _pf(_attr(bb, "length") or "0.0"),
            _pf(_attr(bb, "width") or "0.0"),
        )
    flags: dict = {}
    for fl in _findall(el, "flag"):
        raw = _attr(fl, "value") or ""
        if raw.lower() in ("true", "false"):
            flags[_attr(fl, "name") or ""] = _pb(raw)
        else:
            flags[_attr(fl, "name") or ""] = raw
    reason_el = _find(el, "reason")
    reason = (_attr(reason_el, "text") or "") if reason_el is not None else ""
    return UnfoldResult(
        status=status,
        n_bends=_pi(_attr(el, "n-bends") or "0"),
        flat_area=_pf(_attr(el, "flat-area") or "0.0"),
        blank_bbox=blank_bbox,
        thickness_mean=_pf(_attr(el, "thickness-mean") or "0.0"),
        thickness_cv=_pf(_attr(el, "thickness-cv") or "0.0"),
        flags=flags,
        reason=reason,
    )


def _parse_holes(el: ET.Element) -> HolePattern:
    diameters = [_pf(_attr(h, "diameter") or "0.0") for h in _findall(el, "hole")]
    return HolePattern(
        hole_count=_pi(_attr(el, "count") or "0"),
        diameters=diameters,
    )


def _parse_pmi(el: ET.Element) -> PMIRecord:
    rec = PMIRecord(
        n_annotations=_pi(_attr(el, "n-annotations") or "0"),
        has_semantic=_pb(_attr(el, "has-semantic") or "false"),
    )

    tols_el = _find(el, "tolerances")
    if tols_el is not None:
        for t in _findall(tols_el, "tolerance"):
            rec.tolerances.append(
                parse_element(
                    t,
                    GeometricTolerance,
                    name_overrides=_TOLERANCE_OVERRIDES,
                    transformers=_TOLERANCE_TRANSFORMERS_READ,
                )
            )

    dims_el = _find(el, "dimensions")
    if dims_el is not None:
        for d in _findall(dims_el, "dimension"):
            rec.dimensions.append(parse_element(d, DimensionalTolerance))

    ds_el = _find(el, "datums")
    if ds_el is not None:
        for dt in _findall(ds_el, "datum"):
            rec.datums.append(parse_element(dt, Datum))

    fs_el = _find(el, "finishes")
    if fs_el is not None:
        for f in _findall(fs_el, "finish"):
            rec.finishes.append(parse_element(f, SurfaceFinish))

    return rec


def _parse_strategy(el: ET.Element) -> MachiningStrategy:
    notes_el = _find(el, "notes")
    notes = (notes_el.text or "") if notes_el is not None else ""
    ops = [
        parse_element(op_el, Operation, transformers=_OPERATION_TRANSFORMERS_READ)
        for op_el in _findall(el, "operation")
    ]
    return MachiningStrategy(
        primary_process=_attr(el, "primary-process") or "unknown",
        operations=ops,
        setup_count=_pi(_attr(el, "setup-count") or "1"),
        material_hint=_attr(el, "material-hint") or "",
        estimated_time_min=_pf(_attr(el, "estimated-time-min") or "0.0"),
        notes=notes,
    )


def _parse_part(el: ET.Element) -> PartManifestEntry:
    children_el = _find(el, "children")
    children: list[str] = []
    if children_el is not None:
        for c in _findall(children_el, "child"):
            ref = _attr(c, "ref")
            if ref:
                children.append(ref)

    part = StepPart(
        product_id=_attr(el, "product-id") or "",
        name=_attr(el, "name") or "",
        description=_attr(el, "description") or "",
        children=children,
        source=_attr(el, "source") or "unknown",
    )

    cl_el = _find(el, "classification")
    if cl_el is None:
        raise ManifestParseError(f"part {part.product_id!r} is missing <classification>")
    classification = _parse_classification(cl_el)

    features_el = _find(el, "features")
    features = _parse_features(features_el) if features_el is not None else None

    pm_el = _find(el, "profile-match")
    profile_match = _parse_profile_match(pm_el) if pm_el is not None else None

    uf_el = _find(el, "unfold")
    unfold = _parse_unfold(uf_el) if uf_el is not None else None

    holes_el = _find(el, "holes")
    holes = _parse_holes(holes_el) if holes_el is not None else None

    pmi_el = _find(el, "pmi")
    pmi = _parse_pmi(pmi_el) if pmi_el is not None else None

    strat_el = _find(el, "machining-strategy")
    strategy = _parse_strategy(strat_el) if strat_el is not None else None

    dxf_el = _find(el, "flat-dxf")
    flat_dxf_path = _attr(dxf_el, "path") if dxf_el is not None else None

    return PartManifestEntry(
        part=part,
        classification=classification,
        features=features,
        profile_match=profile_match,
        unfold=unfold,
        holes=holes,
        quantity=_pi(_attr(el, "quantity") or "1"),
        flat_dxf_path=flat_dxf_path,
        pmi=pmi,
        strategy=strategy,
    )


def read_xml(path: str | Path) -> AssemblyManifest:
    """Parse an XML manifest from `path` into an `AssemblyManifest`."""
    p = Path(path)
    try:
        tree = ET.parse(p)
    except ET.ParseError as exc:
        raise ManifestParseError(f"malformed XML in {p}: {exc}") from exc
    root = tree.getroot()
    if _strip_ns(root.tag) != "assembly":
        raise ManifestParseError(f"expected root <assembly>, found <{_strip_ns(root.tag)}> in {p}")

    notes_el = _find(root, "notes")
    notes = (notes_el.text or "") if notes_el is not None else ""

    parts_el = _find(root, "parts")
    entries: list[PartManifestEntry] = []
    if parts_el is not None:
        for part_el in _findall(parts_el, "part"):
            entries.append(_parse_part(part_el))

    return AssemblyManifest(
        source_path=_attr(root, "source") or "",
        source_mtime=_pf(_attr(root, "source-mtime") or "0.0"),
        source_size=_pi(_attr(root, "source-size") or "0"),
        model_version=_attr(root, "model-version") or "",
        generated_at=_attr(root, "generated-at") or "",
        parts=entries,
        notes=notes,
    )
