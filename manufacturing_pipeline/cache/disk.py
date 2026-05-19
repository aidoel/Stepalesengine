"""Disk-backed cache for :class:`AnalyzeResult`.

Layout per entry::

    cache_dir/v<version>/<key>/manifest.xml
    cache_dir/v<version>/<key>/dxfs/<part>.dxf
    cache_dir/v<version>/<key>/meta.json

The key is a sha256 hex digest of ``(resolved_path, mtime_ns, size, version)``
so the same STEP file produces the same key as long as the file has not
been modified and the pipeline version has not bumped.

Writes are atomic: each entry is staged in a sibling ``<key>.tmp`` directory
and then moved into place with :func:`os.rename`. A pre-existing entry at the
target path is removed first to allow the rename to succeed.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from ..io.xml_writer import read_xml, write_xml

if TYPE_CHECKING:  # pragma: no cover - import-cycle guard
    from ..pipeline.analyze_assembly import AnalyzeResult

logger = logging.getLogger(__name__)


def _default_cache_root() -> Path:
    """Return the default cache root: ``$XDG_CACHE_HOME/stepalesengine`` or
    ``~/.cache/stepalesengine``."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "stepalesengine"
    return Path.home() / ".cache" / "stepalesengine"


class PipelineCache:
    """Disk-backed cache of analyze pipeline results.

    Parameters
    ----------
    cache_dir:
        Override the cache root. ``None`` falls back to the XDG default.
    version:
        Subdirectory tag (``v<version>/``) used to invalidate every entry on
        a code-level change. Pass the pipeline's ``MODEL_VERSION``.
    """

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        *,
        version: str = "1",
    ) -> None:
        root = Path(cache_dir).expanduser() if cache_dir is not None else _default_cache_root()
        self._root = root
        self._version = str(version)
        self._versioned = self._root / f"v{self._version}"

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        """The cache root (without the version subdirectory)."""
        return self._root

    @property
    def version(self) -> str:
        return self._version

    @property
    def versioned_dir(self) -> Path:
        """``<cache_dir>/v<version>``. Created lazily."""
        return self._versioned

    # ------------------------------------------------------------------
    # Key computation
    # ------------------------------------------------------------------

    def key(self, step_path: str | Path) -> str:
        """Return the cache key for a STEP file.

        Computed as ``sha256(resolved_path | mtime_ns | size | version)`` and
        returned as the hex digest.

        Raises ``FileNotFoundError`` if the path does not exist.
        """
        resolved = Path(step_path).expanduser().resolve()
        st = resolved.stat()  # raises FileNotFoundError if missing
        h = hashlib.sha256()
        h.update(str(resolved).encode("utf-8"))
        h.update(b"\0")
        h.update(str(st.st_mtime_ns).encode("utf-8"))
        h.update(b"\0")
        h.update(str(st.st_size).encode("utf-8"))
        h.update(b"\0")
        h.update(self._version.encode("utf-8"))
        return h.hexdigest()

    # ------------------------------------------------------------------
    # Lookup / store
    # ------------------------------------------------------------------

    def _entry_dir(self, key: str) -> Path:
        return self._versioned / key

    def get(self, step_path: str | Path) -> AnalyzeResult | None:
        """Return a cached :class:`AnalyzeResult`, or ``None`` if missing.

        Validates the entry by reading ``meta.json`` and ``manifest.xml``.
        Any missing or corrupt sub-file results in a miss (and the bad entry
        is left in place; ``put`` will overwrite it later).
        """
        from ..pipeline.analyze_assembly import AnalyzeResult  # late import (cycle)

        try:
            key = self.key(step_path)
        except FileNotFoundError:
            return None

        entry = self._entry_dir(key)
        manifest_path = entry / "manifest.xml"
        meta_path = entry / "meta.json"
        if not manifest_path.is_file() or not meta_path.is_file():
            return None

        try:
            manifest = read_xml(manifest_path)
            with meta_path.open("r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except Exception as exc:
            logger.warning("cache entry %s corrupt, ignoring: %s", key, exc)
            return None

        dxf_dir = entry / "dxfs"
        dxf_paths: dict[str, Path] = {}
        if dxf_dir.is_dir():
            for part_entry in manifest.parts:
                rel = part_entry.flat_dxf_path
                if not rel:
                    continue
                candidate = dxf_dir / Path(rel).name
                if candidate.is_file():
                    dxf_paths[part_entry.part.product_id] = candidate

        warnings = list(meta.get("warnings", []))

        return AnalyzeResult(
            manifest=manifest,
            manifest_path=None,
            dxf_paths=dxf_paths,
            warnings=warnings,
        )

    def put(self, step_path: str | Path, result: AnalyzeResult) -> Path:
        """Persist ``result`` under the cache key for ``step_path``.

        Writes are atomic: data is staged in a sibling ``.tmp`` directory and
        renamed into place. Returns the final entry directory path.
        """
        resolved = Path(step_path).expanduser().resolve()
        key = self.key(resolved)
        st = resolved.stat()

        self._versioned.mkdir(parents=True, exist_ok=True)

        staging = self._versioned / f"{key}.{uuid.uuid4().hex}.tmp"
        staging.mkdir(parents=True, exist_ok=True)
        try:
            # Manifest XML.
            write_xml(result.manifest, staging / "manifest.xml")

            # DXFs.
            dxfs_dir = staging / "dxfs"
            dxfs_dir.mkdir(parents=True, exist_ok=True)
            for _pid, dxf_path in result.dxf_paths.items():
                src = Path(dxf_path)
                if not src.is_file():
                    continue
                shutil.copy2(src, dxfs_dir / src.name)

            meta = {
                "source_path": str(resolved),
                "mtime_ns": int(st.st_mtime_ns),
                "size": int(st.st_size),
                "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                "warnings": list(result.warnings),
                "version": self._version,
            }
            meta_tmp = staging / "meta.json"
            with meta_tmp.open("w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=2, sort_keys=True)

            target = self._entry_dir(key)
            if target.exists():
                shutil.rmtree(target)
            os.rename(staging, target)
            return target
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def clear(self) -> int:
        """Remove every entry under the versioned directory. Returns the
        count removed."""
        if not self._versioned.is_dir():
            return 0
        count = 0
        for child in self._versioned.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                count += 1
            except OSError as exc:
                logger.warning("could not remove cache entry %s: %s", child, exc)
        return count


__all__ = ["PipelineCache"]
