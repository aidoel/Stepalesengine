"""Parallel batch runner for the analyze pipeline.

Spawns a :class:`ProcessPoolExecutor` (using the ``spawn`` start method, which
is mandatory on macOS: OCP segfaults under ``fork``) and dispatches one
``analyze()`` call per worker. Each STEP file gets its own subdirectory under
``out_root`` so manifests and DXFs don't collide.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class BatchResult:
    """Per-file outcome from :func:`batch_analyze`.

    ``ok`` is False when the worker raised any exception. ``error`` carries
    the stringified exception in that case; otherwise it is None and the
    other fields are populated from the underlying :class:`AnalyzeReport`.
    """

    file: Path
    ok: bool
    label_counts: dict[str, int] = field(default_factory=dict)
    parts: int = 0
    duration_s: float = 0.0
    manifest_path: Path | None = None
    error: str | None = None
    warnings: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_dir_name(name: str) -> str:
    """Sanitise a STEP-file stem into a safe directory name."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip()).strip("_")
    return cleaned[:96] or "step"


def _cap_workers(workers: int) -> int:
    """Clamp the requested worker count to ``cpu_count() - 1``."""
    cpu = os.cpu_count() or 1
    ceiling = max(1, cpu - 1)
    return max(1, min(int(workers), ceiling))


# ---------------------------------------------------------------------------
# Worker entry point - MUST be top-level so ProcessPoolExecutor can pickle it
# ---------------------------------------------------------------------------


def analyze_one(
    step_path: Path,
    out_root: Path,
    *,
    write_dxf: bool,
    write_xml: bool,
    use_cache: bool,
    scorers_path: Path | None = None,
) -> BatchResult:
    """Run :func:`analyze` on one STEP file and return a :class:`BatchResult`.

    Imports happen inside the worker so the OCP-bearing modules are loaded in
    the child process (under ``spawn`` they would be loaded there anyway).
    Any exception is caught and surfaced as ``ok=False`` with the error
    message so the pool keeps running on partial failures.
    """
    step_path = Path(step_path)
    out_root = Path(out_root)
    sub_dir = out_root / _safe_dir_name(step_path.stem)

    started = time.monotonic()
    try:
        from manufacturing_pipeline.pipeline.analyze_assembly import (
            AnalyzeOptions,
            analyze,
        )

        options = AnalyzeOptions(
            out_dir=sub_dir,
            write_dxf=write_dxf,
            write_xml=write_xml,
            use_cache=use_cache,
            scorers_path=scorers_path,
        )
        result = analyze(step_path, options)
    except Exception as exc:  # noqa: BLE001 - intentional broad catch
        return BatchResult(
            file=step_path,
            ok=False,
            duration_s=time.monotonic() - started,
            error=f"{type(exc).__name__}: {exc}",
        )

    counts: dict[str, int] = {}
    for entry in result.manifest.parts:
        label = entry.classification.label
        counts[label] = counts.get(label, 0) + 1

    return BatchResult(
        file=step_path,
        ok=True,
        label_counts=counts,
        parts=len(result.manifest.parts),
        duration_s=time.monotonic() - started,
        manifest_path=result.manifest_path,
        warnings=len(result.warnings),
    )


# ---------------------------------------------------------------------------
# Public batch driver
# ---------------------------------------------------------------------------


def batch_analyze(
    paths: list[Path],
    out_root: Path,
    *,
    workers: int = 4,
    write_dxf: bool = True,
    write_xml: bool = True,
    use_cache: bool = True,
    scorers_path: Path | None = None,
    progress: Callable[[BatchResult], None] | None = None,
) -> list[BatchResult]:
    """Process a list of STEP paths in parallel.

    Each file is analyzed into ``out_root / <safe(stem)>/``. The progress
    callback (if supplied) is invoked from the main process as each future
    completes, so it doesn't need to be process-safe.

    On ``workers == 1`` the work runs in-process: this guarantees byte-identical
    behaviour with a sequential ``analyze()`` loop and avoids the spawn cost
    when the caller explicitly opted out of parallelism.
    """
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    capped = _cap_workers(workers)

    results: list[BatchResult] = []

    # Single-worker path: run inline. Identical semantics to sequential
    # analyze() and avoids the cost of spawning a Python interpreter.
    if capped == 1 or len(paths) <= 1:
        for path in paths:
            result = analyze_one(
                Path(path),
                out_root,
                write_dxf=write_dxf,
                write_xml=write_xml,
                use_cache=use_cache,
                scorers_path=scorers_path,
            )
            results.append(result)
            if progress is not None:
                progress(result)
        return results

    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=capped, mp_context=ctx) as pool:
        futures = {
            pool.submit(
                analyze_one,
                Path(path),
                out_root,
                write_dxf=write_dxf,
                write_xml=write_xml,
                use_cache=use_cache,
                scorers_path=scorers_path,
            ): Path(path)
            for path in paths
        }
        for future in as_completed(futures):
            path = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - worker shouldn't bubble
                result = BatchResult(
                    file=path,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            results.append(result)
            if progress is not None:
                progress(result)

    return results


__all__ = ["BatchResult", "analyze_one", "batch_analyze"]
