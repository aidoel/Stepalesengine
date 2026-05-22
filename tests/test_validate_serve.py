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
    write_profile_step,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_two_steps(directory: Path) -> tuple[Path, Path]:
    """Write two distinct synthetic STEPs into ``directory``. Returns paths."""
    directory.mkdir(parents=True, exist_ok=True)
    a = write_flat_plate_step(directory / "plate.step", l=100, w=50, t=2.0)
    b = write_profile_step(directory / "rhs.step", family="RHS", h=80, b=40, t=3.0, length=300)
    return a, b


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

    report = validate_corpus(corpus, workers=1, out_dir=out_dir, write_outputs=True)

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
    report = validate_corpus(corpus, workers=1, out_dir=out_dir, write_outputs=True)

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

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2.0) as resp:
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
