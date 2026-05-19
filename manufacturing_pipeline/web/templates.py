"""Template loader for the local web UI.

Reads Jinja/CSS templates from :mod:`manufacturing_pipeline.web.templates`
(the directory next to this module) and caches each file in-process. The
loader works both in a source checkout and after ``pip install -e .``, as
long as the templates directory is shipped as package data.
"""

from __future__ import annotations

from pathlib import Path

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_CACHE: dict[str, str] = {}


def load(name: str) -> str:
    """Read a template by basename (e.g. ``'index.html.jinja'``). Cached."""
    if name not in _CACHE:
        _CACHE[name] = (_TEMPLATES_DIR / name).read_text(encoding="utf-8")
    return _CACHE[name]
