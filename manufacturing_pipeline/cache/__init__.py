"""Disk-level cache for the analyze pipeline.

Re-running :func:`manufacturing_pipeline.pipeline.analyze_assembly.analyze` on
an unchanged STEP file should hit this cache and complete in milliseconds.

The cache key is derived from ``(resolved_path, mtime_ns, size, version)`` so
any change to the input file or to the pipeline's ``MODEL_VERSION`` invalidates
prior entries automatically.
"""

from .disk import PipelineCache

__all__ = ["PipelineCache"]
