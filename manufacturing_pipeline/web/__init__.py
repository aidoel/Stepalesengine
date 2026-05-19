"""Minimal local web app for browsing a stepalesengine manifest.xml.

The app is intended for local development inspection only — it has no
authentication, no production hardening, and no JS framework. See
:func:`manufacturing_pipeline.web.server.create_app` for the public entry
point and :func:`manufacturing_pipeline.web.dxf_to_svg.dxf_to_svg` for the
inline DXF preview renderer.
"""

from manufacturing_pipeline.web.dxf_to_svg import dxf_to_svg
from manufacturing_pipeline.web.server import create_app, main

__all__ = ["create_app", "main", "dxf_to_svg"]
