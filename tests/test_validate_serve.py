"""Tests for the corpus serve-corpus app (corpus index + per-file drill-down).

These tests exercise the WSGI app returned by
:func:`manufacturing_pipeline.serve_corpus.create_corpus_app` through
:class:`werkzeug.test.Client` so no real ports are bound. One smoke test
spawns the CLI in a subprocess and curls the bound port to confirm the
end-to-end wiring works as advertised.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("flask")
pytest.importorskip("OCP")

from werkzeug.test import Client  # noqa: E402

from manufacturing_pipeline.batch import _safe_dir_name  # noqa: E402
from manufacturing_pipeline.serve_corpus import create_corpus_app  # noqa: E402
from manufacturing_pipeline.validate import (  # noqa: E402
    render_html_report,
    validate_corpus,
)
from tests.fixtures.synthetic_steps import (  # noqa: E402
    write_flat_plate_step,
    write_lbracket_step,
    write_profile_step,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_two_steps(directory: Path) -> tuple[Path, Path]:
    """Write two distinct synthetic STEPs into ``directory``. Returns paths."""
    directory.mkdir(parents=True, exist_ok=True)
    a = write_flat_plate_step(directory / "plate.step", l=100, w=50, t=2.0)
    b = write_profile_step(
        directory / "rhs.step", family="RHS", h=80, b=40, t=3.0, length=300
    )
    return a, b


def _seed_lbracket(directory: Path) -> Path:
    """Write a single sheet-metal L-bracket STEP into ``directory``.

    An L-bracket is ``unfoldable``, so its classification trace carries
    cross-term contributions (e.g. ``unfoldable,seamed_tube``) whose
    ``Contribution.value`` is a tuple-string such as ``"(True, False)"``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    return write_lbracket_step(directory / "lbracket.step")


def _only_product_id(out_dir: Path, safe_name: str) -> str:
    """Read the lone part's product_id out of a per-file manifest."""
    from manufacturing_pipeline.io.xml_writer import read_xml

    manifest = read_xml(out_dir / safe_name / "manifest.xml")
    assert manifest.parts, f"manifest {safe_name} has no parts"
    return manifest.parts[0].part.product_id


def _free_port() -> int:
    """Allocate an unused TCP port by binding to 0 and reading the assignment."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_url(url: str, timeout: float = 30.0) -> int:
    """Poll ``url`` until it returns any HTTP response, or raise."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                return int(resp.status)
        except (urllib.error.URLError, OSError) as exc:
            last_err = exc
            time.sleep(0.2)
    raise RuntimeError(f"server at {url} never came up: {last_err!r}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_validate_corpus_with_write_outputs_creates_per_file_manifests(
    tmp_path: Path,
) -> None:
    """Two files in, two ``<safe_name>/manifest.xml`` files out under out_dir."""
    corpus = tmp_path / "corpus"
    a, b = _seed_two_steps(corpus)
    out_dir = tmp_path / "report"

    report = validate_corpus(
        corpus, workers=1, out_dir=out_dir, write_outputs=True
    )

    assert report.total_files == 2
    safe_a = _safe_dir_name(a.stem)
    safe_b = _safe_dir_name(b.stem)
    assert (out_dir / safe_a / "manifest.xml").is_file()
    assert (out_dir / safe_b / "manifest.xml").is_file()

    # The CorpusFile records carry the safe_name so the HTML can link rows.
    safe_names = {f.safe_name for f in report.files}
    assert safe_a in safe_names
    assert safe_b in safe_names


def test_corpus_index_returns_200_with_both_rows_linked(tmp_path: Path) -> None:
    """GET / on the corpus app renders HTML with anchor tags per file."""
    corpus = tmp_path / "corpus"
    _seed_two_steps(corpus)
    out_dir = tmp_path / "report"
    validate_corpus(corpus, workers=1, out_dir=out_dir, write_outputs=True)

    client = Client(create_corpus_app(out_dir))
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "STEP corpus validation" in body
    # The top-of-page note should be present in link mode.
    assert "Click any row to drill" in body
    # Both safe-name links are emitted.
    safe_a = _safe_dir_name("plate")
    safe_b = _safe_dir_name("rhs")
    assert f'href="/file/{safe_a}/"' in body
    assert f'href="/file/{safe_b}/"' in body


def test_corpus_file_route_delegates_to_per_file_viewer(tmp_path: Path) -> None:
    """GET /file/<safe>/ proxies to the per-file viewer index page."""
    corpus = tmp_path / "corpus"
    _seed_two_steps(corpus)
    out_dir = tmp_path / "report"
    validate_corpus(corpus, workers=1, out_dir=out_dir, write_outputs=True)

    client = Client(create_corpus_app(out_dir))
    safe_a = _safe_dir_name("plate")
    resp = client.get(f"/file/{safe_a}/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The per-file viewer index renders the BOM table headers.
    assert "Product ID" in body
    assert "<table" in body
    # The per-file viewer's API endpoint should also delegate.
    api_resp = client.get(f"/file/{safe_a}/api/manifest")
    assert api_resp.status_code == 200
    assert "application/json" in api_resp.headers["Content-Type"]


def test_corpus_unknown_safe_name_returns_404(tmp_path: Path) -> None:
    """GET /file/nonexistent/ falls through to the outer app's 404 handler."""
    corpus = tmp_path / "corpus"
    _seed_two_steps(corpus)
    out_dir = tmp_path / "report"
    validate_corpus(corpus, workers=1, out_dir=out_dir, write_outputs=True)

    client = Client(create_corpus_app(out_dir))
    resp = client.get("/file/nonexistent/")
    assert resp.status_code == 404


def test_corpus_app_without_write_outputs_serves_index_unlinked(
    tmp_path: Path,
) -> None:
    """Without per-file manifests the index renders but rows aren't linked."""
    out_dir = tmp_path / "empty_report"
    out_dir.mkdir()

    client = Client(create_corpus_app(out_dir))
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "STEP corpus validation" in body
    # No drill-down note when there are no per-file manifests.
    assert "Click any row to drill" not in body
    assert 'href="/file/' not in body


def test_render_html_report_link_mode_emits_anchor_tags(tmp_path: Path) -> None:
    """render_html_report(..., link_mode=True) wraps paths in <a href>."""
    corpus = tmp_path / "corpus"
    _seed_two_steps(corpus)
    out_dir = tmp_path / "report"
    report = validate_corpus(
        corpus, workers=1, out_dir=out_dir, write_outputs=True
    )

    html_path = render_html_report(report, tmp_path / "report.html")
    text = html_path.read_text(encoding="utf-8")
    # At least one anchor pointing into /file/<safe_name>/
    safe_a = _safe_dir_name("plate")
    safe_b = _safe_dir_name("rhs")
    assert f'href="/file/{safe_a}/"' in text or f'href="/file/{safe_b}/"' in text
    assert "Click any row to drill" in text


def test_render_html_report_no_link_mode_keeps_self_contained(
    tmp_path: Path,
) -> None:
    """Without write_outputs the HTML stays self-contained (no internal links)."""
    corpus = tmp_path / "corpus"
    _seed_two_steps(corpus)
    report = validate_corpus(corpus, workers=1, write_outputs=False)

    html_path = render_html_report(report, tmp_path / "report.html")
    text = html_path.read_text(encoding="utf-8")
    assert "STEP corpus validation" in text
    assert 'href="/file/' not in text
    assert "Click any row to drill" not in text


@pytest.mark.slow
def test_serve_corpus_cli_smoke_responds_on_port(tmp_path: Path) -> None:
    """Spawn ``stepalesengine serve-corpus`` and curl its index."""
    corpus = tmp_path / "corpus"
    _seed_two_steps(corpus)
    out_dir = tmp_path / "report"
    validate_corpus(corpus, workers=1, out_dir=out_dir, write_outputs=True)

    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "manufacturing_pipeline.cli",
            "serve-corpus",
            str(out_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        status = _wait_for_url(f"http://127.0.0.1:{port}/", timeout=30.0)
        assert status == 200

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/", timeout=2.0
        ) as resp:
            body = resp.read().decode("utf-8")
        assert "STEP corpus validation" in body
        safe_a = _safe_dir_name("plate")
        assert f"/file/{safe_a}/" in body
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


def test_per_file_part_detail_urls_are_mount_prefixed(tmp_path: Path) -> None:
    """A per-file part-detail page resolves its asset URLs under the mount.

    The detail/index templates now build links with Flask ``url_for()``. Under
    the ``/file/<safe>/`` corpus mount that yields mount-prefixed URLs; a bare
    absolute ``/glb/folded/<id>`` (the pre-fix output) would 404 because the
    dispatcher only routes ``/file/<safe>/...`` to the per-file viewer.
    """
    corpus = tmp_path / "corpus"
    _seed_lbracket(corpus)
    out_dir = tmp_path / "report"
    validate_corpus(corpus, workers=1, out_dir=out_dir, write_outputs=True)

    safe = _safe_dir_name("lbracket")
    product_id = _only_product_id(out_dir, safe)

    client = Client(create_corpus_app(out_dir))
    quoted = urllib.parse.quote(product_id, safe="")
    resp = client.get(f"/file/{safe}/part/{quoted}")
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]

    body = resp.get_data(as_text=True)
    # The 3D viewer src must be mount-prefixed...
    assert f"/file/{safe}/glb/folded/" in body
    # ...and never a bare absolute path that the dispatcher cannot route.
    assert '"/glb/folded/' not in body

    # The bare unprefixed GLB route 404s through the corpus app: it never
    # reaches the per-file viewer because it lacks the /file/<safe>/ prefix.
    bare = client.get(f"/glb/folded/{quoted}")
    assert bare.status_code == 404


def test_diff_path_confinement_blocks_arbitrary_files(tmp_path: Path) -> None:
    """``/diff?other=`` is confined to the report root by ``diff_root``.

    The corpus app passes the report directory as ``diff_root`` to
    ``create_app``, so a public deployment cannot be coaxed into reading a
    manifest (or any file) outside its own report tree.
    """
    corpus = tmp_path / "corpus"
    _seed_two_steps(corpus)
    out_dir = tmp_path / "report"
    validate_corpus(corpus, workers=1, out_dir=out_dir, write_outputs=True)

    safe_a = _safe_dir_name("plate")
    safe_b = _safe_dir_name("rhs")

    client = Client(create_corpus_app(out_dir))

    # A path outside the report root is rejected with 403.
    escaped = client.get(f"/file/{safe_a}/diff?other=/etc/passwd")
    assert escaped.status_code == 403

    # A real sibling manifest inside the report dir is permitted (200).
    sibling = out_dir / safe_b / "manifest.xml"
    assert sibling.is_file()
    allowed = client.get(
        f"/file/{safe_a}/diff?other={urllib.parse.quote(str(sibling))}"
    )
    assert allowed.status_code == 200


def test_detail_page_survives_string_valued_contribution(tmp_path: Path) -> None:
    """The detail page renders 200 when a contribution value is a string.

    ``Contribution.value`` is typed ``float | str``; cross-term features
    render as tuple/bool strings (e.g. ``"(True, False)"``). ``detail.html.jinja``
    now guards ``'%.3f'|format`` with an ``is number`` test. An L-bracket is
    ``unfoldable``, so its trace carries ``unfoldable,seamed_tube`` cross-term
    contributions with a tuple-string value, and their large deltas land them
    in the top-5 shown on the page. Pre-fix this 500'd.
    """
    corpus = tmp_path / "corpus"
    _seed_lbracket(corpus)
    out_dir = tmp_path / "report"
    validate_corpus(corpus, workers=1, out_dir=out_dir, write_outputs=True)

    safe = _safe_dir_name("lbracket")
    product_id = _only_product_id(out_dir, safe)

    # Confirm the trace really does carry a string-valued top contribution,
    # otherwise the test would pass vacuously.
    from manufacturing_pipeline.io.xml_writer import read_xml

    manifest = read_xml(out_dir / safe / "manifest.xml")
    contributions = sorted(
        manifest.parts[0].classification.trace.contributions,
        key=lambda c: abs(c.delta),
        reverse=True,
    )[:5]
    assert any(isinstance(c.value, str) for c in contributions), (
        "expected a string-valued contribution in the top-5; got "
        f"{[(c.feature, type(c.value).__name__) for c in contributions]}"
    )

    client = Client(create_corpus_app(out_dir))
    quoted = urllib.parse.quote(product_id, safe="")
    resp = client.get(f"/file/{safe}/part/{quoted}")
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]
