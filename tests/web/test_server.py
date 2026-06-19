"""Flask app + DXF-to-SVG renderer tests.

Skipped wholesale when Flask is not importable so the rest of the suite
remains runnable. All HTTP work happens through ``app.test_client()`` — no
real ports are bound.
"""

from __future__ import annotations

import io
import math
import re
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("flask")

from manufacturing_pipeline.classification.types import (  # noqa: E402
    ClassificationResult,
    Contribution,
    DecisionTrace,
)
from manufacturing_pipeline.geometry.types import (  # noqa: E402
    HolePattern,
    ManufacturingFeatures,
    ProfileMatch,
    UnfoldResult,
    UnfoldStatus,
)
from manufacturing_pipeline.io.dxf_writer import FlatPattern, write_dxf  # noqa: E402
from manufacturing_pipeline.io.xml_writer import write_xml  # noqa: E402
from manufacturing_pipeline.manifest import (  # noqa: E402
    AssemblyManifest,
    PartManifestEntry,
)
from manufacturing_pipeline.parsing.types import StepPart  # noqa: E402
from manufacturing_pipeline.pmi.types import (  # noqa: E402
    Datum,
    DimensionalTolerance,
    GeometricTolerance,
    PMIRecord,
    SurfaceFinish,
)
from manufacturing_pipeline.web.dxf_to_svg import dxf_to_svg  # noqa: E402
from manufacturing_pipeline.web.server import create_app  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _rect(x0: float, y0: float, w: float, h: float) -> list[tuple]:
    return [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]


def _circle(cx: float, cy: float, r: float, n: int = 16) -> list[tuple]:
    return [
        (cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


def _classification(label: str, conf: float) -> ClassificationResult:
    trace = DecisionTrace(
        scores={"plaat": 1.42, "profiel": 0.31, "anders": 0.18},
        probabilities={"plaat": 0.71, "profiel": 0.18, "anders": 0.11},
        margin=1.11,
        ambiguous=False,
        contributions=[
            Contribution(feature="top1_face_pct", cls="plaat", value=0.78, delta=0.62),
            Contribution(feature="aspect_ratio", cls="profiel", value=9.0, delta=0.31),
            Contribution(feature="thickness_ratio", cls="plaat", value=0.01, delta=0.12),
        ],
        tiebreakers_run=[],
        probe_results={},
        model_version="rules-1.0.0",
    )
    return ClassificationResult(label=label, confidence=conf, trace=trace)


def _features() -> ManufacturingFeatures:
    return ManufacturingFeatures(
        volume=120_000.0,
        surface_area=45_000.0,
        bbox_dims_sorted=(200.0, 100.0, 2.0),
        aspect_ratio=2.0,
        thickness_ratio=0.01,
        face_area_top=[0.78, 0.15, 0.05],
        edge_radius=(1.5, 0.25),
        hole_count=2,
        hole_diameters=[10.0, 8.0],
        sa_v_ratio=0.37,
        bounding_cylinder_fit_pct=0.42,
        convex_hull_volume_ratio=0.95,
        source="brep",
    )


def _unfold() -> UnfoldResult:
    return UnfoldResult(
        status=UnfoldStatus.SUCCESS,
        n_bends=1,
        flat_area=20_000.0,
        blank_bbox=(220.0, 120.0),
        thickness_mean=2.0,
        thickness_cv=0.01,
        flags={"has_hem": False},
        reason="",
    )


def _build_dxf(out_path: Path, part_name: str = "FRONT_BRACKET") -> Path:
    pattern = FlatPattern(
        outer_contour=[_rect(0.0, 0.0, 100.0, 60.0)],
        holes=[_circle(30.0, 30.0, 6.0), _circle(70.0, 30.0, 6.0)],
        bend_lines=[
            ((50.0, 0.0), (50.0, 60.0), {"angle": 90.0, "radius": 1.5, "direction": "up"}),
        ],
        thickness=2.0,
        part_name=part_name,
    )
    return write_dxf(pattern, out_path)


@pytest.fixture
def manifest_setup(tmp_path: Path):
    """Build an out_dir with a manifest.xml + two DXFs and a real second part."""
    out_dir = tmp_path / "out"
    parts_dir = out_dir / "parts"
    parts_dir.mkdir(parents=True)

    # One plaat with a DXF, one profiel without a DXF, one uncertain with a DXF.
    dxf_a = _build_dxf(parts_dir / "front_bracket.dxf", part_name="FRONT_BRACKET")
    dxf_b = _build_dxf(parts_dir / "back_plate.dxf", part_name="BACK_PLATE")

    entries = [
        PartManifestEntry(
            part=StepPart(
                product_id="ASM-0042-12",
                name="Front Bracket",
                description="Stainless plaat",
                source="nauo",
            ),
            classification=_classification("plaat", 0.91),
            features=_features(),
            unfold=_unfold(),
            holes=HolePattern(hole_count=2, diameters=[10.0, 8.0]),
            quantity=2,
            flat_dxf_path=f"parts/{dxf_a.name}",
        ),
        PartManifestEntry(
            part=StepPart(
                product_id="ASM-0042-13", name="Beam", description="HEA profiel", source="nauo"
            ),
            classification=_classification("profiel", 0.74),
            features=_features(),
            profile_match=ProfileMatch(
                family="HEA",
                standard="DIN 1025-3",
                designation="HEA 200",
                fitted_dims={"h": 190.0, "b": 200.0},
                residual_mm=0.4,
                confidence=0.85,
            ),
            quantity=1,
            flat_dxf_path=None,
        ),
        PartManifestEntry(
            part=StepPart(
                product_id="ASM-0042-14", name="Back Plate", description="", source="nauo"
            ),
            classification=_classification("uncertain", 0.42),
            features=_features(),
            quantity=1,
            flat_dxf_path=f"parts/{dxf_b.name}",
        ),
    ]

    manifest = AssemblyManifest(
        source_path=str(tmp_path / "asm.stp"),
        source_mtime=1734567890.0,
        source_size=123_456,
        model_version="rules-1.0.0",
        generated_at="2026-05-15T12:00:00Z",
        parts=entries,
        notes="test manifest",
    )
    manifest_path = out_dir / "manifest.xml"
    write_xml(manifest, manifest_path)

    return {
        "manifest_path": manifest_path,
        "out_dir": out_dir,
        "dxf_filename": dxf_a.name,
        "entries": entries,
    }


@pytest.fixture
def client(manifest_setup):
    app = create_app(manifest_setup["manifest_path"], out_dir=manifest_setup["out_dir"])
    app.config["TESTING"] = True
    return app.test_client()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_index_returns_200_and_lists_every_product_id(client, manifest_setup):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    for entry in manifest_setup["entries"]:
        assert entry.part.product_id in body, f"missing {entry.part.product_id} on index"
    # Spot-check at least one classification label badge.
    assert "label-plaat" in body
    assert "label-profiel" in body
    assert "label-uncertain" in body


def test_part_detail_returns_200_with_label_and_decision_trace(client, manifest_setup):
    pid = manifest_setup["entries"][0].part.product_id
    resp = client.get(f"/part/{pid}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert pid in body
    assert "plaat" in body  # classification label
    assert "Decision trace" in body
    # Top-5 contributions table renders the feature names.
    assert "top1_face_pct" in body
    # Embedded SVG preview is referenced.
    assert "/dxf-svg/" in body


def test_part_detail_unknown_product_id_returns_404(client):
    resp = client.get("/part/does-not-exist")
    assert resp.status_code == 404


def test_api_manifest_returns_json_with_parts_array(client, manifest_setup):
    resp = client.get("/api/manifest")
    assert resp.status_code == 200
    assert "application/json" in resp.headers["Content-Type"]
    data = resp.get_json()
    assert isinstance(data, dict)
    assert "parts" in data
    assert isinstance(data["parts"], list)
    assert len(data["parts"]) == len(manifest_setup["entries"])
    ids = {p["part"]["product_id"] for p in data["parts"]}
    assert ids == {e.part.product_id for e in manifest_setup["entries"]}


def test_dxf_svg_route_returns_svg_with_geometry(client, manifest_setup):
    fname = manifest_setup["dxf_filename"]
    resp = client.get(f"/dxf-svg/{fname}")
    assert resp.status_code == 200
    ctype = resp.headers["Content-Type"].lower()
    assert "svg" in ctype or "xml" in ctype
    body = resp.get_data(as_text=True)
    assert body.lstrip().startswith("<?xml") or body.lstrip().startswith("<svg")
    assert "<svg" in body
    # At least one polyline or polygon or path representing geometry.
    assert re.search(r"<polyline|<polygon|<path", body), "no geometry element in svg"


def test_dxf_raw_route_serves_dxf_bytes(client, manifest_setup):
    fname = manifest_setup["dxf_filename"]
    resp = client.get(f"/dxf/{fname}")
    assert resp.status_code == 200
    # Content should look like DXF (sectional file starting with "  0\nSECTION").
    body = resp.get_data(as_text=True)
    assert "SECTION" in body
    assert "ENTITIES" in body


def test_dxf_svg_unknown_filename_returns_404(client):
    resp = client.get("/dxf-svg/no_such_file.dxf")
    assert resp.status_code == 404


def test_dxf_to_svg_on_synthetic_pattern_renders_correct_polylines(tmp_path: Path):
    """Render a known DXF and assert geometry counts match the input."""
    dxf_path = tmp_path / "synthetic.dxf"
    pattern = FlatPattern(
        outer_contour=[_rect(0.0, 0.0, 80.0, 40.0)],
        holes=[
            _circle(20.0, 20.0, 5.0),
            _circle(60.0, 20.0, 5.0),
        ],
        bend_lines=[
            ((40.0, 0.0), (40.0, 40.0), {"angle": 90.0, "radius": 1.0, "direction": "up"}),
        ],
        thickness=2.0,
        part_name="SYN",
    )
    write_dxf(pattern, dxf_path)

    svg = dxf_to_svg(dxf_path)
    assert svg.lstrip().startswith("<?xml")
    assert "<svg" in svg
    # Outer + 2 holes -> 3 closed polygons; 1 bend line -> 1 open polyline.
    polygons = re.findall(r"<polygon\b", svg)
    polylines = re.findall(r"<polyline\b", svg)
    assert len(polygons) == 3, f"expected 3 polygons, got {len(polygons)}"
    assert len(polylines) == 1, f"expected 1 polyline, got {len(polylines)}"
    # Layer-class attributes are present.
    assert 'class="layer-OUTER"' in svg
    assert 'class="layer-INNER"' in svg
    assert 'class="layer-BEND_UP"' in svg


def test_create_app_defaults_out_dir_to_manifest_parent(manifest_setup):
    """When out_dir is omitted, the manifest's parent must be used."""
    app = create_app(manifest_setup["manifest_path"])
    assert Path(app.config["OUT_DIR"]) == manifest_setup["manifest_path"].parent


# ---------------------------------------------------------------------------
# New: index search filter, bulk DXF zip, diff page
# ---------------------------------------------------------------------------


def test_index_contains_search_input_and_links(client):
    """Index renders a client-side filter input, counter, and toolbar links."""
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Search input with placeholder.
    assert re.search(r'<input[^>]*type="search"', body), "missing <input type=search>"
    assert "filter parts" in body
    # Counter element.
    assert "showing" in body and "of" in body
    # Header links to the new routes.
    assert "/dxfs.zip" in body
    assert 'href="/diff"' in body


def test_dxfs_zip_returns_zip_with_expected_filenames(client, manifest_setup):
    """/dxfs.zip returns a valid ZIP containing every per-part DXF."""
    resp = client.get("/dxfs.zip")
    assert resp.status_code == 200
    assert "zip" in resp.headers["Content-Type"].lower()
    disp = resp.headers.get("Content-Disposition", "")
    assert "attachment" in disp.lower()
    assert ".zip" in disp.lower()

    expected = {Path(e.flat_dxf_path).name for e in manifest_setup["entries"] if e.flat_dxf_path}
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = {Path(n).name for n in zf.namelist()}
    assert expected.issubset(names), f"expected {expected} in zip, got {names}"


def test_dxfs_zip_with_no_dxfs_returns_empty_zip(tmp_path):
    """When no parts reference a DXF, the ZIP is still served (empty)."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    entries = [
        PartManifestEntry(
            part=StepPart(product_id="X-1", name="No DXF", description="", source="nauo"),
            classification=_classification("plaat", 0.91),
            features=_features(),
            quantity=1,
            flat_dxf_path=None,
        ),
    ]
    manifest = AssemblyManifest(
        source_path=str(tmp_path / "asm.stp"),
        source_mtime=0.0,
        source_size=0,
        model_version="rules-1.0.0",
        generated_at="2026-05-15T12:00:00Z",
        parts=entries,
        notes="",
    )
    manifest_path = out_dir / "manifest.xml"
    write_xml(manifest, manifest_path)

    app = create_app(manifest_path, out_dir=out_dir)
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/dxfs.zip")
    assert resp.status_code == 200
    assert "zip" in resp.headers["Content-Type"].lower()
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        assert zf.namelist() == []


def test_diff_get_without_other_renders_form(client):
    """GET /diff with no ?other= renders a form, no diff body."""
    resp = client.get("/diff")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<form" in body
    assert 'name="other"' in body
    # The summary card should not appear yet.
    assert "n_added" not in body
    assert "Summary" not in body


def test_diff_with_valid_other_renders_diff_sections(client, manifest_setup, tmp_path):
    """/diff?other=<valid> shows ADDED / REMOVED / CHANGED / UNCHANGED."""
    # Build a second manifest dropping one part so we get a REMOVED row.
    entries = manifest_setup["entries"][:-1]  # drop the last entry
    other_manifest = AssemblyManifest(
        source_path=str(tmp_path / "asm2.stp"),
        source_mtime=0.0,
        source_size=0,
        model_version="rules-1.0.0",
        generated_at="2026-05-15T12:00:00Z",
        parts=entries,
        notes="",
    )
    other_path = tmp_path / "other_manifest.xml"
    write_xml(other_manifest, other_path)

    resp = client.get(f"/diff?other={other_path}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # All four canonical headers should be present.
    for header in ("ADDED", "REMOVED", "CHANGED", "UNCHANGED"):
        assert header in body, f"missing {header} header in diff page"
    # Summary card must render.
    assert "Summary" in body
    # The dropped part should show up somewhere (as added or removed).
    dropped = manifest_setup["entries"][-1].part.product_id
    assert dropped in body


def test_diff_with_nonexistent_other_returns_error(client):
    """A path that does not exist should yield a 4xx with a clear message."""
    resp = client.get("/diff?other=/nonexistent/manifest.xml")
    assert resp.status_code in (400, 404)
    body = resp.get_data(as_text=True)
    assert "not found" in body.lower() or "manifest" in body.lower()


# ---------------------------------------------------------------------------
# PMI rendering
# ---------------------------------------------------------------------------


def _pmi_record() -> PMIRecord:
    """A synthetic PMI record covering tolerances, dimensions, datums, finishes."""
    return PMIRecord(
        tolerances=[
            GeometricTolerance(
                type="position",
                magnitude_mm=0.05,
                unit="mm",
                datums=["A", "B"],
                applied_to="face_42",
                modifier="MMC",
            ),
            GeometricTolerance(
                type="flatness",
                magnitude_mm=0.02,
                unit="mm",
                datums=[],
                applied_to="face_17",
            ),
        ],
        dimensions=[
            DimensionalTolerance(
                nominal=10.0,
                upper=0.05,
                lower=0.02,
                unit="mm",
                applied_to="edge_3",
            ),
        ],
        datums=[
            Datum(identifier="A", applied_to="face_1"),
            Datum(identifier="B", applied_to="face_5"),
        ],
        finishes=[SurfaceFinish(value=1.6, unit="um", applied_to="face_9")],
        n_annotations=4,
        has_semantic=True,
    )


@pytest.fixture
def pmi_manifest_setup(tmp_path: Path, manifest_setup):
    """Re-build the manifest with a PMI record on the first part."""
    entries = manifest_setup["entries"]
    entries[0].pmi = _pmi_record()
    manifest = AssemblyManifest(
        source_path=str(tmp_path / "asm_pmi.stp"),
        source_mtime=1734567890.0,
        source_size=123_456,
        model_version="rules-1.0.0",
        generated_at="2026-05-15T12:00:00Z",
        parts=entries,
        notes="pmi test",
    )
    manifest_path = manifest_setup["out_dir"] / "manifest_pmi.xml"
    write_xml(manifest, manifest_path)
    return {
        "manifest_path": manifest_path,
        "out_dir": manifest_setup["out_dir"],
        "entries": entries,
    }


@pytest.fixture
def pmi_client(pmi_manifest_setup):
    app = create_app(pmi_manifest_setup["manifest_path"], out_dir=pmi_manifest_setup["out_dir"])
    app.config["TESTING"] = True
    return app.test_client()


def test_index_contains_pmi_column_header(pmi_client):
    """Index BOM table should expose a PMI column."""
    resp = pmi_client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<th>PMI</th>" in body


def test_index_row_shows_pmi_annotation_count(pmi_client, pmi_manifest_setup):
    """The row for the part with PMI shows its annotation count and a semantic dot."""
    resp = pmi_client.get("/")
    body = resp.get_data(as_text=True)
    # n_annotations=4 must appear, and the green semantic dot CSS class too.
    assert "pmi-sem" in body
    assert ">4<" in body or "4\n" in body or " 4 " in body


def test_part_detail_renders_tolerances_table(pmi_client, pmi_manifest_setup):
    """Part detail page shows the GD&T tolerances table with color-coded pills."""
    pid = pmi_manifest_setup["entries"][0].part.product_id
    resp = pmi_client.get(f"/part/{pid}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Product Manufacturing Information" in body
    assert "tol-position" in body
    assert "tol-flatness" in body
    assert "Geometric tolerances" in body
    # Magnitude value is rendered.
    assert "0.050" in body


def test_part_detail_renders_datum_pills(pmi_client, pmi_manifest_setup):
    """Datum pills render with the datum-pill class and identifiers."""
    pid = pmi_manifest_setup["entries"][0].part.product_id
    resp = pmi_client.get(f"/part/{pid}")
    body = resp.get_data(as_text=True)
    assert 'class="datum-pill"' in body
    assert ">A<" in body
    assert ">B<" in body


def test_part_detail_no_pmi_shows_no_pmi_data(client, manifest_setup):
    """A part without a PMIRecord renders the 'No PMI data.' message."""
    # The third entry in manifest_setup has no pmi assigned -> entry.pmi is None.
    pid = manifest_setup["entries"][2].part.product_id
    resp = client.get(f"/part/{pid}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "No PMI data." in body


# ---------------------------------------------------------------------------
# Template loader
# ---------------------------------------------------------------------------


def test_template_loader_returns_base_css_starting_with_root():
    """``load('base.css')`` returns the CSS file content starting with ``:root``."""
    from manufacturing_pipeline.web.templates import load

    css = load("base.css")
    assert isinstance(css, str) and css
    # The file preserves its original leading newline from the inline string.
    assert css.lstrip().startswith(":root")


def test_template_loader_missing_file_raises_filenotfound():
    """Asking for a template that does not exist raises ``FileNotFoundError``."""
    from manufacturing_pipeline.web.templates import load

    with pytest.raises(FileNotFoundError):
        load("nonexistent.html.jinja")


def test_template_loader_caches_repeated_loads():
    """Two consecutive ``load()`` calls return the same cached string object."""
    from manufacturing_pipeline.web.templates import load

    first = load("index.html.jinja")
    second = load("index.html.jinja")
    assert first is second


# ---------------------------------------------------------------------------
# /step-svg/<product_id> route (HLR projection of the source STEP solid)
# ---------------------------------------------------------------------------


def _build_step_svg_client(tmp_path: Path):
    """Build a Flask client whose manifest references a real on-disk STEP file.

    Returns ``(client, product_id)``. The manifest's ``source_path`` points at
    an assembly STEP that contains one named solid, so ``/step-svg/<pid>`` can
    actually resolve and render it via the HLR projector.
    """
    pytest.importorskip("OCP")
    from tests.fixtures.synthetic_steps import build_plate_solid, write_assembly_step

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    product_id = "svg_plate"
    step_path = tmp_path / "asm.step"
    write_assembly_step(
        step_path,
        parts=[(product_id, build_plate_solid(l=10.0, w=10.0, t=2.0))],
    )

    entries = [
        PartManifestEntry(
            part=StepPart(
                product_id=product_id,
                name="SVG Plate",
                description="plate for /step-svg test",
                source="nauo",
            ),
            classification=_classification("plaat", 0.90),
            features=_features(),
            quantity=1,
            flat_dxf_path=None,
        ),
    ]
    manifest = AssemblyManifest(
        source_path=str(step_path),
        source_mtime=step_path.stat().st_mtime,
        source_size=step_path.stat().st_size,
        model_version="rules-1.0.0",
        generated_at="2026-05-15T12:00:00Z",
        parts=entries,
        notes="step-svg route test",
    )
    manifest_path = out_dir / "manifest.xml"
    write_xml(manifest, manifest_path)

    app = create_app(manifest_path, out_dir=out_dir)
    app.config["TESTING"] = True
    return app.test_client(), product_id


def test_step_svg_route_returns_200_and_svg_content_type(tmp_path: Path):
    """Happy path: /step-svg/<known_pid> returns 200, image/svg+xml, with <svg/polyline."""
    client, product_id = _build_step_svg_client(tmp_path)
    resp = client.get(f"/step-svg/{product_id}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    ctype = resp.headers["Content-Type"].lower()
    assert "image/svg+xml" in ctype
    body = resp.get_data(as_text=True)
    assert "<svg" in body
    assert "<polyline" in body


def test_step_svg_route_respects_view_query_arg(tmp_path: Path):
    """The ``view`` query argument selects which projection to render."""
    client, product_id = _build_step_svg_client(tmp_path)
    resp = client.get(f"/step-svg/{product_id}?view=top")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The renderer labels each view with the uppercased view name.
    assert "TOP" in body


def test_step_svg_unknown_product_id_returns_404(tmp_path: Path):
    """Missing product_id -> 404 (no projection attempted)."""
    client, _ = _build_step_svg_client(tmp_path)
    resp = client.get("/step-svg/no-such-product")
    assert resp.status_code == 404


def test_step_svg_missing_source_step_returns_404(client):
    """The default ``manifest_setup`` references a non-existent STEP source path.

    The route must respond with 404 (source STEP not available) rather than
    crashing when ``manifest.source_path`` does not exist on disk.
    """
    resp = client.get("/step-svg/ASM-0042-12")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GLB routes
# ---------------------------------------------------------------------------


# A minimal but structurally valid GLB blob: 12-byte header + empty JSON chunk
# carrying ``{}``. The route handler doesn't inspect the body so the test only
# needs the magic bytes to flow through unchanged.
_FAKE_GLB = (
    b"glTF"  # magic
    + (2).to_bytes(4, "little")  # version
    + (12 + 8 + 4).to_bytes(4, "little")  # total length: header + chunk hdr + payload
    + (4).to_bytes(4, "little")  # chunk length
    + (0x4E4F534A).to_bytes(4, "little")  # 'JSON'
    + b"{}  "  # padded JSON payload
)


@pytest.fixture
def glb_client(client, manifest_setup, monkeypatch):
    """Client variant with the GLB builders stubbed out.

    Avoids loading OCP / running the unfold probe so the route tests stay
    millisecond-scale. The stubs return a known-good GLB blob; missing-part
    and missing-source branches still exercise the route's own logic.
    """
    from manufacturing_pipeline.web import server as server_mod

    monkeypatch.setattr(server_mod, "folded_glb", lambda *a, **kw: _FAKE_GLB)
    monkeypatch.setattr(server_mod, "unfolded_glb", lambda *a, **kw: _FAKE_GLB)

    # ``/glb/unfolded`` also touches ``find_solid_for_part`` and
    # ``UnfoldProbe.compute_flat_pattern`` before reaching ``unfolded_glb``.
    # Stub both so we don't pay the OCP load cost.
    import manufacturing_pipeline.geometry.unfold_probe as unfold_probe
    from manufacturing_pipeline.web import step_to_svg

    monkeypatch.setattr(step_to_svg, "find_solid_for_part", lambda *a, **kw: object())
    monkeypatch.setattr(
        unfold_probe.UnfoldProbe,
        "compute_flat_pattern",
        lambda self, solid: {
            "outer_contour": [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
            "holes": [],
            "bend_lines": [],
            "thickness": 2.0,
            "bbox": (0.0, 0.0, 10.0, 10.0),
        },
    )

    # The routes additionally require ``manifest.source_path`` to be a file,
    # so touch a dummy file at that location.
    source = Path(manifest_setup["manifest_path"]).parent.parent / "asm.stp"
    source.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")
    return client


def test_glb_folded_happy_path_returns_gltf_binary(glb_client, manifest_setup):
    """A known product_id returns 200, model/gltf-binary, and starts with glTF magic."""
    pid = manifest_setup["entries"][0].part.product_id
    resp = glb_client.get(f"/glb/folded/{pid}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.headers["Content-Type"] == "model/gltf-binary"
    body = resp.get_data()
    assert body.startswith(b"glTF")
    assert len(body) > 12


def test_glb_folded_unknown_product_id_returns_404(glb_client):
    """An unknown product_id short-circuits before hitting the mesh builder."""
    resp = glb_client.get("/glb/folded/no-such-id")
    assert resp.status_code == 404


def test_glb_folded_missing_source_step_returns_404(client, manifest_setup, monkeypatch):
    """When ``manifest.source_path`` is not on disk, the route 404s."""
    # The default fixture's source_path points at a path that does not exist
    # on disk, so this exercises that branch without any additional setup.
    pid = manifest_setup["entries"][0].part.product_id
    resp = client.get(f"/glb/folded/{pid}")
    assert resp.status_code == 404


def test_glb_folded_builder_failure_returns_500(client, manifest_setup, monkeypatch, tmp_path):
    """When the builder returns ``None``, the route surfaces a 500 error."""
    from manufacturing_pipeline.web import server as server_mod

    # Make the source path resolvable so we get past the 404 source check.
    source = Path(manifest_setup["manifest_path"]).parent.parent / "asm.stp"
    source.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")

    monkeypatch.setattr(server_mod, "folded_glb", lambda *a, **kw: None)

    pid = manifest_setup["entries"][0].part.product_id
    resp = client.get(f"/glb/folded/{pid}")
    assert resp.status_code == 500


def test_glb_unfolded_happy_path_returns_gltf_binary(glb_client, manifest_setup):
    """The unfolded route returns 200 with model/gltf-binary on a stubbed unfold."""
    pid = manifest_setup["entries"][0].part.product_id
    resp = glb_client.get(f"/glb/unfolded/{pid}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.headers["Content-Type"] == "model/gltf-binary"
    body = resp.get_data()
    assert body.startswith(b"glTF")


def test_glb_unfolded_unknown_product_id_returns_404(glb_client):
    """Unknown product_id 404s before touching the unfold probe."""
    resp = glb_client.get("/glb/unfolded/no-such-id")
    assert resp.status_code == 404


def test_glb_unfolded_missing_source_step_returns_404(client, manifest_setup):
    """When the source STEP file is missing, /glb/unfolded 404s up front."""
    pid = manifest_setup["entries"][0].part.product_id
    resp = client.get(f"/glb/unfolded/{pid}")
    assert resp.status_code == 404


def test_glb_unfolded_no_solid_returns_500(client, manifest_setup, monkeypatch):
    """When ``find_solid_for_part`` returns None, the inner ``abort(404)`` is
    caught by the route's broad ``except Exception`` and surfaces as a 500.

    This pins the current observable behaviour; if the route is later refined
    to let ``HTTPException`` bubble through, update both assertion and docstring.
    """
    from manufacturing_pipeline.web import step_to_svg

    source = Path(manifest_setup["manifest_path"]).parent.parent / "asm.stp"
    source.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")

    monkeypatch.setattr(step_to_svg, "find_solid_for_part", lambda *a, **kw: None)

    pid = manifest_setup["entries"][0].part.product_id
    resp = client.get(f"/glb/unfolded/{pid}")
    assert resp.status_code == 500


def test_glb_unfolded_empty_unfold_returns_500(client, manifest_setup, monkeypatch):
    """Empty unfold result: same wrap-as-500 path as the no-solid case."""
    import manufacturing_pipeline.geometry.unfold_probe as unfold_probe
    from manufacturing_pipeline.web import step_to_svg

    source = Path(manifest_setup["manifest_path"]).parent.parent / "asm.stp"
    source.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")

    monkeypatch.setattr(step_to_svg, "find_solid_for_part", lambda *a, **kw: object())
    monkeypatch.setattr(
        unfold_probe.UnfoldProbe,
        "compute_flat_pattern",
        lambda self, solid: {"outer_contour": [], "holes": [], "bend_lines": []},
    )

    pid = manifest_setup["entries"][0].part.product_id
    resp = client.get(f"/glb/unfolded/{pid}")
    assert resp.status_code == 500
