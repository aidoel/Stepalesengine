"""``calibrate`` subcommand: sweep classifier weights/thresholds on a labelled corpus."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ._common import EXIT_BAD_PATH, EXIT_NO_PARTS, EXIT_OK


def register(subparsers) -> argparse.ArgumentParser:
    """Register the ``calibrate`` parser. Returns the parser."""
    calibrate = subparsers.add_parser(
        "calibrate", help="sweep classifier weights/thresholds on a labelled corpus"
    )
    calibrate.add_argument(
        "labels_csv",
        help="CSV with columns: step_path, product_id, expected_label",
    )
    calibrate.add_argument(
        "--output",
        default=None,
        help="path to write the winning parameters as JSON",
    )
    calibrate.set_defaults(func=run)
    return calibrate


def run(args: argparse.Namespace) -> int:
    """Execute the calibrate subcommand. Returns exit code."""
    import json

    from manufacturing_pipeline.calibration import load_labels, sweep

    labels_path = Path(args.labels_csv).expanduser().resolve()
    if not labels_path.exists():
        print(f"error: labels CSV not found: {labels_path}", file=sys.stderr)
        return EXIT_BAD_PATH

    parts = load_labels(labels_path)
    if not parts:
        print("error: no labelled rows in CSV", file=sys.stderr)
        return EXIT_NO_PARTS

    result = sweep(parts)

    summary = {
        "best_temperature": result.best_temperature,
        "best_margin_thr": result.best_margin_thr,
        "best_conf_thr": result.best_conf_thr,
        "best_cost": result.best_cost,
        "best_weights": result.best_weights,
        "confusion": {f"{t}->{p}": c for (t, p), c in result.confusion.items()},
        "grid_size": len(result.grid),
    }

    if args.output:
        out_path = Path(args.output).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("calibration sweep complete")
    print(f"  corpus size:   {len(parts)}")
    print(f"  grid cells:    {len(result.grid)}")
    print(f"  best cost:     {result.best_cost:.4f}")
    print(f"  temperature:   {result.best_temperature}")
    print(f"  margin_thr:    {result.best_margin_thr}")
    print(f"  conf_thr:      {result.best_conf_thr}")
    print("  confusion:")
    for (true, pred), count in sorted(result.confusion.items()):
        print(f"    {true:>10} -> {pred:<10} {count}")
    if args.output:
        print(f"  wrote:         {args.output}")
    return EXIT_OK
