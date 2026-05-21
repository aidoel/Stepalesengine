"""End-to-end orchestrator: parse -> load -> match -> probe -> classify.

Pure Python entry point that exposes the full analyze pipeline as a library
API. The CLI is a thin wrapper around :func:`analyze`. Callers may also use
this module programmatically.

The orchestrator never raises on geometry errors - each probe is wrapped in
try/except and degrades to a sensible default. The only exceptions it raises
are :class:`FileNotFoundError` (missing input path) and
:class:`manufacturing_pipeline.parsing.types.StepParseError` (unreadable
STEP file). All other failures end up as warnings on the
:class:`AnalyzeResult` and/or ``classification.label == "uncertain"`` entries.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .. import telemetry
from ..assembly.graph import build_assembly_graph, iter_leaves
from ..assembly.matcher import MatchResult, match_parts_to_solids
from ..cam.types import MachiningStrategy
from ..classification.feature_vector import FeatureVector
from ..classification.score_classifier import ScoreClassifier
from ..classification.scorers import ScorerSpec, load_scorers_from_yaml
from ..classification.tiebreakers import (
    cross_section_tiebreaker,
    profile_match_tiebreaker,
    unfold_tiebreaker,
)
from ..classification.types import ClassificationResult, DecisionTrace
from ..geometry.feature_extractor import FeatureExtractor
from ..geometry.feature_merger import enrich
from ..geometry.geometry_loader import load_solids
from ..geometry.types import (
    HolePattern,
    ManufacturingFeatures,
    ProfileMatch,
    UnfoldResult,
    UnfoldStatus,
)
from ..geometry.unfold_probe import UnfoldProbe
from ..io.dxf_writer import FlatPattern, write_dxf
from ..io.pdf_writer import PartDrawingMeta, write_assembly_pdf, write_pdf
from ..io.xml_writer import AssemblyManifest, PartManifestEntry, write_xml
from ..parsing.step_parser import parse_step
from ..parsing.types import StepPart
from ..pmi.extractor import extract_pmi
from ..pmi.types import PMIRecord
from .probes import (
    STAGE_CLASSIFY,
    STAGE_POST,
    STAGE_PRE,
    ProbeContext,
    ProbeRegistry,
    default_registry,
    probe_result,
)

logger = logging.getLogger(__name__)

MODEL_VERSION = "rules-1.0.0"


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class AnalyzeOptions:
    """Knobs for :func:`analyze`.

    Attributes
    ----------
    out_dir:
        Where to write outputs. If ``None`` no files are written.
    write_dxf:
        When True (and the label is in ``dxf_only_for``), emit a per-part
        flat-pattern DXF beneath ``out_dir/parts``.
    write_xml:
        When True, write the assembly manifest XML to ``out_dir/manifest.xml``.
    dxf_only_for:
        Tuple of classification labels that should produce a DXF.
    cache:
        Forwarded to :func:`parse_step` - reuse a previously-parsed result
        keyed on (path, mtime_ns, size).
    notes:
        Free-form text propagated into the manifest's ``notes`` field.
    cache_dir:
        Override the disk-cache root. ``None`` falls back to
        ``$XDG_CACHE_HOME/stepalesengine`` (or ``~/.cache/stepalesengine``).
    use_cache:
        When True (default), the on-disk pipeline cache is consulted before
        running the full pipeline and the result is written back on a miss.
    """

    out_dir: Path | None = None
    write_dxf: bool = True
    write_xml: bool = True
    write_pdf: bool = False
    write_assembly_pdf: bool = False
    dxf_only_for: tuple[str, ...] = ("plaat",)
    cache: bool = True
    notes: str = ""
    cache_dir: Path | None = None
    use_cache: bool = True
    # Path to a custom scorer-weights YAML. When ``None`` (default) the bundled
    # weights at ``manufacturing_pipeline/data/scorer_weights.yaml`` are used.
    scorers_path: Path | None = None
    # When True (default) drop manifest entries that have no matching solid
    # AND zero volume - these are assembly hierarchy nodes (sub-assembly
    # references, body containers) that produce dead rows in the BOM.
    drop_unmatched_leaves: bool = True
    # When True (default) gate the expensive UnfoldProbe + slice/ProfileMatcher
    # probes behind cheap feature-based prefilters, AND reuse classifications
    # for repeated solids within a single analyze() call. Set to False for the
    # full deep analysis on every solid (useful for regression testing).
    prefilter: bool = True


@dataclass
class AnalyzeResult:
    """Return value of :func:`analyze`.

    ``manifest`` is always present even when the file produced zero parts.
    ``manifest_path`` and ``dxf_paths`` are only populated when the
    corresponding ``write_*`` option is True.
    """

    manifest: AssemblyManifest
    manifest_path: Path | None = None
    dxf_paths: dict[str, Path] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    pdf_paths: dict[str, Path] = field(default_factory=dict)
    assembly_pdf_path: Path | None = None


# ---------------------------------------------------------------------------
# Per-solid prefilter + duplicate cache
# ---------------------------------------------------------------------------
#
# Large assemblies (e.g. ~3018 solids in 803139-0010.step) contain many small
# fasteners and washers that are obviously not unfoldable sheet metal or
# constant-section profiles. Running ``UnfoldProbe`` (167 ms/part) and the
# slice + ``ProfileShapeMatcher`` pipeline (72 ms/part) on every solid burns
# minutes per file. The prefilter below rejects clearly-unsuitable solids
# from those expensive probes; the duplicate cache reuses the per-solid
# result for repeated parts (same M6 bolt 50x).


def _bbox_fill_ratio(features: ManufacturingFeatures) -> float:
    """Volume divided by AABB volume. 0.0 when the bbox is degenerate."""
    L, W, T = features.bbox_dims_sorted or (0.0, 0.0, 0.0)
    if L * W * T <= 0.0:
        return 0.0
    return float(features.volume) / float(L * W * T)


def _is_likely_unfoldable(features: ManufacturingFeatures) -> bool:
    """Cheap pre-check: is it WORTH running UnfoldProbe?

    Returns False when the part is obviously not sheet metal. A False here
    saves the full ~167 ms UnfoldProbe walk; the orchestrator records a
    synthetic ``UnfoldResult(FAILURE, reason="prefilter:not_likely_unfoldable")``
    instead. False positives are fine - the probe will then run and report
    its own failure reason. False negatives are NOT fine: we never want to
    skip a real sheet-metal part. Hence the conservative thresholds.

    Sheet-metal parts always have one of two profiles in our feature space:

    * Flat plate: ``thickness_ratio`` is tiny (~0.02) and the bbox is fully
      filled (fill_ratio ~1.0). Top/bottom faces dominate the surface area.
    * Bent part (L-bracket, channel): ``thickness_ratio`` can be moderate
      because the bbox spans both legs, but the fill_ratio is LOW (the bbox
      hosts a lot of empty air) and the part is still planar-dominated.

    The prefilter rejects only obviously-not-sheet parts: high b-spline
    fraction (curved organic shapes), low planar fraction (CHS pipe, blob),
    OR chunky solid bar (high thickness ratio AND high fill ratio at once).
    """
    # Lots of curved (b-spline) surface area means it's almost certainly not
    # a developable sheet-metal part.
    if features.surface_pct.get("bspline", 0.0) > 0.05:
        return False
    # Sheet metal is dominated by planar surfaces. <50% planar implies a
    # cylinder/cone/blob that the unfold probe will reject anyway.
    if features.surface_pct.get("planar", 0.0) < 0.5:
        return False
    # A solid bar / fastener has high thickness_ratio AND high fill_ratio at
    # once: there is no thin direction AND the bbox is nearly full. A bent
    # sheet has high thickness_ratio but a low fill_ratio (lots of air in
    # the bbox). A flat plate has high fill_ratio but a tiny thickness_ratio.
    if features.thickness_ratio > 0.15 and _bbox_fill_ratio(features) > 0.7:
        return False
    return True


def _is_likely_profile(features: ManufacturingFeatures) -> bool:
    """Cheap pre-check: is it WORTH running ``slice_solid`` + matcher?

    Profiles are elongated (``aspect_ratio >= 3``) and have a constant
    cross-section, which means dominant planar / cylindrical surfaces. A
    high b-spline fraction implies a curved organic shape that cannot be a
    standard structural profile.
    """
    if features.aspect_ratio < 3.0:
        return False
    if features.surface_pct.get("bspline", 0.0) > 0.1:
        return False
    return True


# Module-level cache used to de-dup repeated solids within a single
# ``analyze()`` call. The cache is CLEARED at the start of every ``analyze()``
# so different files cannot poison each other; this keeps the cache
# correctness-preserving and trivially memory-bounded (largest assembly so
# far ~3018 solids, expected unique designs ~20-200).
_SOLID_RESULT_CACHE: dict[
    tuple,
    tuple[ProfileMatch | None, UnfoldResult | None, HolePattern, ClassificationResult],
] = {}


def _solid_signature(features: ManufacturingFeatures) -> tuple:
    """Cheap signature for de-duping near-identical solids.

    Buckets to 0.1 mm / 0.1 mm^2 so nominally identical bodies collide even
    when their floating-point measurements diverge slightly. Includes
    ``hole_count`` because two same-volume blocks with different hole counts
    must NOT share a classification.
    """
    return (
        round(float(features.volume), 1),
        round(float(features.surface_area), 1),
        tuple(round(float(d), 1) for d in features.bbox_dims_sorted),
        int(features.hole_count),
    )


_PREFILTER_UNFOLD_RESULT = UnfoldResult(
    status=UnfoldStatus.FAILURE,
    reason="prefilter:not_likely_unfoldable",
)


# The probe registry is the single wiring point for per-part probes. Built
# once at import: each probe holds its own (sometimes table-loading) state, so
# instantiating it per part would reload those tables on every solid.
_REGISTRY: ProbeRegistry = default_registry()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_name(name: str) -> str:
    """Sanitise a part name for use as a filename.

    Lowercase, non-alnum -> underscore, truncated to 64 chars. Never empty.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    cleaned = cleaned[:64]
    return cleaned or "part"


def _now_iso_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _classifier_features(
    features: ManufacturingFeatures,
    profile_match,
    unfold,
) -> dict:
    """Translate analyzer outputs into the flat dict the ScoreClassifier wants.

    Thin wrapper around :meth:`FeatureVector.from_features`: the dataclass is
    the authoritative producer; the classifier still consumes a dict.
    """
    return FeatureVector.from_features(features, profile_match, unfold).as_dict()


def _build_flat_pattern(raw: dict, part_name: str) -> FlatPattern | None:
    """Convert :meth:`UnfoldProbe.compute_flat_pattern` output into a FlatPattern."""
    if not raw:
        return None
    outer = raw.get("outer_contour") or []
    if not outer:
        return None

    # The probe returns either a single polyline or a list of polylines.
    if outer and isinstance(outer[0], tuple) and len(outer[0]) == 2:
        outer_contours = [list(outer)]
    else:
        outer_contours = [list(p) for p in outer]

    holes = [list(h) for h in (raw.get("holes") or [])]
    bend_lines = list(raw.get("bend_lines") or [])
    thickness = float(raw.get("thickness") or 0.0)
    bbox_raw = raw.get("bbox") or (0.0, 0.0, 0.0, 0.0)

    if thickness <= 0.0:
        # write_dxf rejects thickness <= 0; keep a small positive value so a
        # thin plate without a measured thickness still exports.
        thickness = 1.0

    return FlatPattern(
        outer_contour=outer_contours,
        holes=holes,
        bend_lines=bend_lines,
        thickness=thickness,
        bbox=tuple(bbox_raw),
        units="mm",
        part_name=part_name,
    )


def _degenerate_entry(node, warnings: list[str]) -> PartManifestEntry:
    """Build a placeholder entry for a leaf with no matching solid."""
    part = StepPart(
        product_id=node.product_id,
        name=node.name,
        description=node.description,
        children=[],
        source=node.source,
    )
    trace = DecisionTrace(model_version=MODEL_VERSION)
    classification = ClassificationResult(label="uncertain", confidence=0.0, trace=trace)
    warnings.append(f"no solid matched leaf {node.product_id!r}")
    return PartManifestEntry(
        part=part,
        classification=classification,
        features=ManufacturingFeatures(),
        profile_match=None,
        unfold=None,
        holes=HolePattern(),
        quantity=max(node.quantity, 1),
        flat_dxf_path=None,
    )


def _prefilter_skips(features: ManufacturingFeatures) -> dict[str, object]:
    """Build the ``skip`` dict for the CLASSIFY-stage probe run.

    A skipped probe is not executed; its slot is filled with the supplied
    value. The cheap prefilter rejects parts that are obviously not profiles
    or not sheet-metal, so the expensive ``slice_solid``/``UnfoldProbe`` walks
    never run on them. ``profile`` skips to ``None`` (its normal "no match"
    value); ``unfold`` skips to the synthetic prefilter FAILURE result.
    """
    skip: dict[str, object] = {}
    if not _is_likely_profile(features):
        skip["profile"] = None
    if not _is_likely_unfoldable(features):
        skip["unfold"] = _PREFILTER_UNFOLD_RESULT
    return skip


@dataclass
class _PartAnalysis:
    """Outputs of the geometry/probe/classify phase, consumed by the
    output-writing phase. ``_process_pair`` is the only producer/consumer."""

    part: StepPart
    solid: object
    features: ManufacturingFeatures
    holes: HolePattern
    profile_match: ProfileMatch | None
    unfold: UnfoldResult | None
    classification: ClassificationResult
    pmi_record: PMIRecord | None
    strategy: MachiningStrategy | None
    quantity: int


def _analyse_part(
    match: MatchResult,
    options: AnalyzeOptions,
    warnings: list[str],
    source_path: Path | None,
    scorers: ScorerSpec | None,
) -> _PartAnalysis:
    """Geometry + probe + classify phase for one matched (non-degenerate) solid.

    Runs feature extraction, then the PRE / CLASSIFY / POST probe stages
    around the duplicate-solid cache: the PRE stage (hole count) must run
    before the cache lookup because it feeds the signature, the CLASSIFY
    stage is cached, and the POST stage runs after classification.
    """
    node = match.node
    solid = match.solid

    part = StepPart(
        product_id=node.product_id,
        name=node.name,
        description=node.description,
        children=[],
        source=node.source,
    )

    features = ManufacturingFeatures()
    try:
        features = FeatureExtractor().extract(solid)
    except Exception as exc:
        logger.warning("feature extraction failed for %s: %s", part.product_id, exc)
        warnings.append(f"feature extraction failed for {part.product_id}: {exc}")

    prefilter_on = bool(options.prefilter)

    ctx = ProbeContext(
        solid=solid,
        features=features,
        source_path=source_path if source_path is not None else Path(""),
        part_name=part.name or "",
        part_id=part.product_id or "",
    )

    # PRE stage - the hole probe. Cheap (~2 ms/part) and its hole count feeds
    # the cache signature, so it must run on every solid before the lookup.
    probe_results = _REGISTRY.run_all(ctx, stage=STAGE_PRE)
    holes = probe_result(probe_results, "holes", HolePattern, HolePattern())
    probe_results["holes"] = holes
    # The HoleAnalyzer fills hole_count/hole_diameters via enrich() later but
    # we also stash them on features now so the signature is meaningful for
    # the very first occurrence of a given design.
    features.hole_count = holes.hole_count
    features.hole_diameters = list(holes.diameters)

    sig = _solid_signature(features) if prefilter_on else None
    cached = _SOLID_RESULT_CACHE.get(sig) if sig is not None else None
    if cached is not None:
        profile_match, unfold, holes, classification = cached
        # Reuse the cached results by identity so downstream code and the
        # POST-stage probes see the values the first occurrence produced.
        probe_results["holes"] = holes
        probe_results["profile"] = profile_match
        probe_results["unfold"] = unfold
    else:
        # CLASSIFY stage - profile + unfold, gated by the cheap prefilter.
        skip = _prefilter_skips(features) if prefilter_on else {}
        probe_results = _REGISTRY.run_all(
            ctx, stage=STAGE_CLASSIFY, skip=skip, prior=probe_results
        )
        profile_match = probe_result(probe_results, "profile", ProfileMatch)
        unfold = probe_result(probe_results, "unfold", UnfoldResult)
        classification = None  # filled in below

    try:
        features = enrich(features, holes=holes, profile_match=profile_match, unfold=unfold)
    except Exception as exc:
        logger.debug("enrich failed for %s: %s", part.product_id, exc)

    if classification is None:
        try:
            clf_features = _classifier_features(features, profile_match, unfold)
            classification = ScoreClassifier(
                scorers=scorers, model_version=MODEL_VERSION
            ).classify(
                clf_features,
                tiebreakers=[
                    ("unfold", unfold_tiebreaker),
                    ("cross_section", cross_section_tiebreaker),
                    ("profile_match", profile_match_tiebreaker),
                ],
                probe_results={
                    "unfoldable": "true" if clf_features["unfoldable"] else "false",
                },
            )
        except Exception as exc:
            logger.warning("classification failed for %s: %s", part.product_id, exc)
            warnings.append(f"classification failed for {part.product_id}: {exc}")
            classification = ClassificationResult(
                label="uncertain",
                confidence=0.0,
                trace=DecisionTrace(model_version=MODEL_VERSION),
            )
        # Store the freshly-computed result for the next occurrence of this
        # signature. Note that PMI/CAM are deliberately NOT cached: PMI is
        # file-global and CAM depends on the classification which IS cached,
        # so re-running it is cheap.
        if sig is not None:
            _SOLID_RESULT_CACHE[sig] = (profile_match, unfold, holes, classification)

    # POST stage - pmi + cam, run on every part with the final classification
    # and prior probe results visible via ctx.prior. Neither is cached: PMI is
    # file-global (PmiProbe caches by path internally) and CAM is milliseconds.
    # ctx.features is refreshed so the cam probe sees the enriched features.
    ctx.features = features
    probe_results["classification"] = classification
    post_skip: dict[str, object] = {}
    if source_path is None:
        # No STEP text to read - keep the legacy pmi=None rather than the
        # empty record extract_pmi(Path("")) would return.
        post_skip["pmi"] = None
    probe_results = _REGISTRY.run_all(
        ctx, stage=STAGE_POST, skip=post_skip, prior=probe_results
    )
    pmi_record = probe_result(probe_results, "pmi", PMIRecord)
    strategy = probe_result(probe_results, "cam", MachiningStrategy)

    return _PartAnalysis(
        part=part,
        solid=solid,
        features=features,
        holes=holes,
        profile_match=profile_match,
        unfold=unfold,
        classification=classification,
        pmi_record=pmi_record,
        strategy=strategy,
        quantity=max(node.quantity, 1),
    )


def _write_part_outputs(
    analysis: _PartAnalysis,
    options: AnalyzeOptions,
    parts_dir: Path | None,
    warnings: list[str],
) -> tuple[
    PartManifestEntry,
    Path | None,
    Path | None,
    FlatPattern | None,
    PartDrawingMeta | None,
]:
    """Flat-pattern + DXF/PDF writing phase; assembles the manifest entry."""
    part = analysis.part
    features = analysis.features
    classification = analysis.classification
    unfold = analysis.unfold

    dxf_path: Path | None = None
    pdf_path = None
    pattern = None
    meta = None

    need_pattern = (
        (options.write_dxf or options.write_pdf or options.write_assembly_pdf)
        and parts_dir is not None
        and classification.label in options.dxf_only_for
        and unfold is not None
        and unfold.status == UnfoldStatus.SUCCESS
    )

    if need_pattern:
        try:
            raw = UnfoldProbe().compute_flat_pattern(analysis.solid)
            pattern = _build_flat_pattern(raw, part_name=part.name or part.product_id)
            if pattern is not None:
                meta = PartDrawingMeta(
                    part_name=part.name or part.product_id,
                    part_number=part.product_id,
                    material=features.material_guess or "S235",
                    quantity=analysis.quantity,
                    drawn_by="stepalesengine",
                )
        except Exception as exc:
            logger.warning("Unfold flat pattern computation failed for %s: %s", part.product_id, exc)
            warnings.append(f"Unfold flat pattern computation failed for {part.product_id}: {exc}")

    if pattern is not None and parts_dir is not None:
        if options.write_dxf:
            try:
                fname = f"{_safe_name(part.name or part.product_id)}.dxf"
                out = parts_dir / fname
                write_dxf(pattern, out)
                dxf_path = out
            except Exception as exc:
                logger.warning("DXF export failed for %s: %s", part.product_id, exc)
                warnings.append(f"DXF export failed for {part.product_id}: {exc}")
                dxf_path = None

        if options.write_pdf:
            try:
                fname = f"{_safe_name(part.name or part.product_id)}.pdf"
                out = parts_dir / fname
                write_pdf(pattern, out, meta=meta)
                pdf_path = out
            except Exception as exc:
                logger.warning("PDF export failed for %s: %s", part.product_id, exc)
                warnings.append(f"PDF export failed for {part.product_id}: {exc}")
                pdf_path = None

    flat_dxf_rel: str | None = None
    if dxf_path is not None and options.out_dir is not None:
        try:
            flat_dxf_rel = str(dxf_path.relative_to(options.out_dir))
        except ValueError:
            flat_dxf_rel = str(dxf_path)

    entry = PartManifestEntry(
        part=part,
        classification=classification,
        features=features,
        profile_match=analysis.profile_match,
        unfold=unfold,
        holes=analysis.holes,
        quantity=analysis.quantity,
        flat_dxf_path=flat_dxf_rel,
        pmi=analysis.pmi_record if isinstance(analysis.pmi_record, PMIRecord) else None,
        strategy=analysis.strategy,
    )
    return entry, dxf_path, pdf_path, pattern, meta


def _process_pair(
    match: MatchResult,
    options: AnalyzeOptions,
    parts_dir: Path | None,
    warnings: list[str],
    source_path: Path | None = None,
    scorers: ScorerSpec | None = None,
) -> tuple[
    PartManifestEntry,
    Path | None,
    Path | None,
    FlatPattern | None,
    PartDrawingMeta | None,
]:
    """Run the per-part pipeline for a single MatchResult.

    A degenerate match (no solid) yields a placeholder entry. Otherwise the
    work splits into :func:`_analyse_part` (feature extraction, the staged
    probe run around the duplicate-solid cache, classification) and
    :func:`_write_part_outputs` (flat pattern, DXF/PDF, manifest entry).

    When :attr:`AnalyzeOptions.prefilter` is True (the default) the expensive
    profile/unfold runs are skipped on parts the cheap features rule out, and
    identical solids (per :func:`_solid_signature`) reuse the prior result.
    """
    if match.solid is None:
        return _degenerate_entry(match.node, warnings), None, None, None, None
    analysis = _analyse_part(match, options, warnings, source_path, scorers)
    return _write_part_outputs(analysis, options, parts_dir, warnings)


def _finalise_cache_hit(
    hit: AnalyzeResult,
    out_dir: Path | None,
    parts_dir: Path | None,
    opts: AnalyzeOptions,
) -> AnalyzeResult:
    """Materialise a cache hit on disk so the result is byte-for-byte
    interchangeable with a fresh run.

    The cache stores DXFs and PDFs under its own per-key directory. When the
    caller passed ``out_dir`` we copy them into ``out_dir/parts/`` and rewrite
    the paths dicts to point at the new locations. The manifest XML is
    re-written when requested.
    """
    dxf_paths: dict[str, Path] = {}
    if parts_dir is not None and hit.dxf_paths:
        parts_dir.mkdir(parents=True, exist_ok=True)
        for pid, src in hit.dxf_paths.items():
            src_path = Path(src)
            if not src_path.is_file():
                continue
            dst = parts_dir / src_path.name
            shutil.copy2(src_path, dst)
            dxf_paths[pid] = dst
    else:
        dxf_paths = dict(hit.dxf_paths)

    pdf_paths: dict[str, Path] = {}
    if parts_dir is not None and hit.pdf_paths:
        parts_dir.mkdir(parents=True, exist_ok=True)
        for pid, src in hit.pdf_paths.items():
            src_path = Path(src)
            if not src_path.is_file():
                continue
            dst = parts_dir / src_path.name
            shutil.copy2(src_path, dst)
            pdf_paths[pid] = dst
    else:
        pdf_paths = dict(hit.pdf_paths)

    assembly_pdf_path: Path | None = None
    if out_dir is not None and hit.assembly_pdf_path is not None:
        src_path = Path(hit.assembly_pdf_path)
        if src_path.is_file():
            dst = out_dir / src_path.name
            shutil.copy2(src_path, dst)
            assembly_pdf_path = dst
    else:
        assembly_pdf_path = hit.assembly_pdf_path

    manifest_path: Path | None = None
    if opts.write_xml and out_dir is not None:
        manifest_path = write_xml(hit.manifest, out_dir / "manifest.xml")

    return AnalyzeResult(
        manifest=hit.manifest,
        manifest_path=manifest_path,
        dxf_paths=dxf_paths,
        warnings=list(hit.warnings),
        pdf_paths=pdf_paths,
        assembly_pdf_path=assembly_pdf_path,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def analyze(
    path: str | Path,
    options: AnalyzeOptions | None = None,
) -> AnalyzeResult:
    """End-to-end pipeline. Pure Python entry point - no console output.

    Logs via stdlib logging. Never raises on geometry errors; raises only
    on missing file or unreadable STEP. Returns :class:`AnalyzeResult`.

    Parameters
    ----------
    path:
        Path to a STEP (Part 21) file.
    options:
        Optional :class:`AnalyzeOptions`. Defaults to writing DXF + XML
        when ``out_dir`` is set.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    StepParseError
        If the STEP file cannot be parsed at all.
    """
    opts = options if options is not None else AnalyzeOptions()

    # Reset the per-solid duplicate cache so two consecutive analyze() calls
    # on different files cannot poison each other.
    _SOLID_RESULT_CACHE.clear()

    step_path = Path(path).expanduser().resolve()
    if not step_path.exists():
        raise FileNotFoundError(f"STEP file not found: {step_path}")

    telemetry.emit(
        "analyze_start",
        path=str(step_path),
        write_dxf=bool(opts.write_dxf),
        write_xml=bool(opts.write_xml),
        write_pdf=bool(opts.write_pdf),
        write_assembly_pdf=bool(opts.write_assembly_pdf),
    )
    _analyze_t0 = _dt.datetime.now(_dt.timezone.utc).timestamp()

    out_dir: Path | None = None
    parts_dir: Path | None = None
    if opts.out_dir is not None:
        out_dir = Path(opts.out_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        # We need a parts_dir if we are writing either DXF or PDF single files.
        if opts.write_dxf or opts.write_pdf:
            parts_dir = out_dir / "parts"
            parts_dir.mkdir(parents=True, exist_ok=True)
    # Replace options.out_dir with the resolved absolute path so
    # _process_pair's relative_to() call is correct.
    opts = AnalyzeOptions(
        out_dir=out_dir,
        write_dxf=opts.write_dxf,
        write_xml=opts.write_xml,
        write_pdf=opts.write_pdf,
        write_assembly_pdf=opts.write_assembly_pdf,
        dxf_only_for=tuple(opts.dxf_only_for),
        cache=opts.cache,
        notes=opts.notes,
        cache_dir=opts.cache_dir,
        use_cache=opts.use_cache,
        scorers_path=opts.scorers_path,
        drop_unmatched_leaves=opts.drop_unmatched_leaves,
        prefilter=opts.prefilter,
    )

    # Load the scorer spec once per analyze() call. None means "use the bundled
    # default", which ScoreClassifier resolves itself via default_scorers().
    scorers_spec: ScorerSpec | None = None
    if opts.scorers_path is not None:
        scorers_spec = load_scorers_from_yaml(opts.scorers_path)

    disk_cache = None
    if opts.use_cache:
        from ..cache.disk import PipelineCache

        disk_cache = PipelineCache(opts.cache_dir, version=MODEL_VERSION)
        hit = disk_cache.get(step_path)
        if hit is not None:
            cached = _finalise_cache_hit(hit, out_dir, parts_dir, opts)
            duration_ms = (_dt.datetime.now(_dt.timezone.utc).timestamp() - _analyze_t0) * 1000.0
            telemetry.emit(
                "analyze_end",
                path=str(step_path),
                n_parts=len(cached.manifest.parts),
                n_warnings=len(cached.warnings),
                n_dxf=len(cached.dxf_paths),
                duration_ms=round(duration_ms, 3),
                cache_hit=True,
            )
            return cached

    warnings: list[str] = []

    parts = parse_step(step_path, cache=opts.cache)

    # PMI is global to the file in our current shape - parsed once and
    # attached to every leaf entry below. Failures are non-fatal.
    try:
        pmi_record: PMIRecord | None = extract_pmi(step_path)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("pmi extraction failed: %s", exc)
        warnings.append(f"pmi extraction failed: {exc}")
        pmi_record = None

    try:
        solids = load_solids(step_path)
    except FileNotFoundError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("load_solids failed: %s", exc)
        warnings.append(f"load_solids failed: {exc}")
        solids = []

    root = build_assembly_graph(parts)
    leaves = list(iter_leaves(root))

    matches = match_parts_to_solids(leaves, solids, step_path=step_path)

    entries: list[PartManifestEntry] = []
    dxf_paths: dict[str, Path] = {}
    pdf_paths: dict[str, Path] = {}
    assembly_patterns: list[tuple[FlatPattern, PartDrawingMeta]] = []
    dropped_ghosts = 0
    for match in matches:
        if match.node is None:
            continue
        entry, dxf, pdf, pattern, meta = _process_pair(
            match, opts, parts_dir, warnings, source_path=step_path, scorers=scorers_spec
        )
        # Filter ghost assembly leaves: nodes in the NAUO graph that have no
        # matching solid AND no real geometry. These are sub-assembly handles
        # or container references; they would otherwise clutter the BOM with
        # zero-volume "PartBody"/"NONE" rows.
        if (
            opts.drop_unmatched_leaves
            and match.solid is None
            and (entry.features is None or entry.features.volume <= 0.0)
            and entry.classification.confidence == 0.0
        ):
            dropped_ghosts += 1
            continue
        # Degenerate entries (no solid) bypass the probe registry, so attach
        # the file-global PMI record explicitly to keep behaviour unchanged.
        if entry.pmi is None and pmi_record is not None:
            entry.pmi = pmi_record
        entries.append(entry)
        if dxf is not None:
            dxf_paths[entry.part.product_id] = dxf
        if pdf is not None:
            pdf_paths[entry.part.product_id] = pdf
        if pattern is not None and meta is not None:
            assembly_patterns.append((pattern, meta))
        telemetry.emit(
            "analyze_part",
            product_id=entry.part.product_id,
            name=entry.part.name,
            source=entry.part.source,
            label=entry.classification.label,
            confidence=round(float(entry.classification.confidence), 4),
        )
    if dropped_ghosts:
        logger.info("dropped %d ghost assembly leaves with no geometry", dropped_ghosts)

    assembly_pdf_path: Path | None = None
    if opts.write_assembly_pdf and out_dir is not None and assembly_patterns:
        try:
            pdf_out = out_dir / "assembly.pdf"
            write_assembly_pdf(assembly_patterns, pdf_out)
            assembly_pdf_path = pdf_out
        except Exception as exc:
            logger.warning("Assembly PDF export failed: %s", exc)
            warnings.append(f"Assembly PDF export failed: {exc}")
            assembly_pdf_path = None

    stat = step_path.stat()
    manifest = AssemblyManifest(
        source_path=str(step_path),
        source_mtime=float(stat.st_mtime),
        source_size=int(stat.st_size),
        model_version=MODEL_VERSION,
        generated_at=_now_iso_utc(),
        parts=entries,
        notes=opts.notes,
    )

    manifest_path: Path | None = None
    if opts.write_xml and out_dir is not None:
        manifest_path = write_xml(manifest, out_dir / "manifest.xml")

    result = AnalyzeResult(
        manifest=manifest,
        manifest_path=manifest_path,
        dxf_paths=dxf_paths,
        warnings=warnings,
        pdf_paths=pdf_paths,
        assembly_pdf_path=assembly_pdf_path,
    )

    if disk_cache is not None:
        try:
            disk_cache.put(step_path, result)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("could not write pipeline cache entry: %s", exc)

    duration_ms = (_dt.datetime.now(_dt.timezone.utc).timestamp() - _analyze_t0) * 1000.0
    telemetry.emit(
        "analyze_end",
        path=str(step_path),
        n_parts=len(entries),
        n_warnings=len(warnings),
        n_dxf=len(dxf_paths),
        n_pdf=len(pdf_paths),
        duration_ms=round(duration_ms, 3),
    )

    return result


__all__ = ["AnalyzeOptions", "AnalyzeResult", "analyze"]
