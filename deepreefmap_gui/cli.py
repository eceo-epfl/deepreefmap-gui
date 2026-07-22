"""Console entry point for the desktop application."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="deepreefmap-gui",
        description="DeepReefMap desktop application",
    )
    parser.add_argument(
        "run_dir",
        nargs="?",
        type=Path,
        default=None,
        help="Existing run directory (contains run_manifest.json) to open on startup",
    )
    args = parser.parse_args()
    if args.run_dir is not None and not args.run_dir.is_dir():
        parser.error(f"run directory does not exist: {args.run_dir}")

    from deepreefmap_gui.app import launch

    launch(view_run_dir=args.run_dir)
