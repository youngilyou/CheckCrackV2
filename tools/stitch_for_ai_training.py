"""Stitch one folder of images for CheckCrackViewer's "AI 학습" tab.

Usage:
    python tools/stitch_for_ai_training.py <images_folder> [facade_name] [--in-place]

Deliberately a SEPARATE script from tools/stitch_folder.py, not a shared call
into it -- stitch_folder.py is the general-purpose CLI tool and is expected to
grow its own flags/behavior over time; the AI 학습 tab's pipeline call should
not shift underneath it when that happens. Both currently wrap the same
src.pipeline.runner.run_facade_poc with the same sensible defaults, but are
free to diverge (e.g. different config, different fallback behavior) without
touching each other.

Wraps src.pipeline.runner.run_facade_poc:
  - facade_name defaults to the folder's own name
  - output goes to facades/<facade_name>/output/, or <images_folder>/output/
    with --in-place
  - uses config/pipeline.yaml (production thresholds)
  - COLMAP fallback (#12) runs automatically if drift is detected
"""

from __future__ import annotations

import sys
from pathlib import Path

# Same rationale as stitch_folder.py: CheckCrackViewer spawns this with
# stdout/stderr redirected to a pipe, which falls back to the Windows
# console's codepage (cp949) instead of UTF-8 otherwise.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline.runner import run_facade_poc  # noqa: E402


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--in-place"]
    in_place = "--in-place" in sys.argv[1:]

    if len(args) < 1:
        print("usage: python tools/stitch_for_ai_training.py <images_folder> [facade_name] [--in-place]")
        sys.exit(1)

    images_dir = Path(args[0])
    if not images_dir.is_dir():
        print(f"not a folder: {images_dir}")
        sys.exit(1)

    facade_name = args[1] if len(args) > 1 else images_dir.name
    output_dir = images_dir / "output" if in_place else None

    print(f"stitching '{images_dir}' as facade '{facade_name}' ...")
    out = run_facade_poc(
        facade_id=facade_name,
        images_dir=images_dir,
        output_root="facades",
        config_path="config/pipeline.yaml",
        output_dir=output_dir,
    )
    if out is None:
        print("failed: no image pair passed the geometry quality gate — check logs/pipeline.log")
        sys.exit(1)

    print(f"done: {out}")
    print(f"  - {facade_name}_analysis.tif / {facade_name}_visual.tif")
    print(f"  - {facade_name}_quality_report.json (check needs_colmap_fallback)")
    print(f"  - if COLMAP ran: {facade_name}_colmap_report.json")


if __name__ == "__main__":
    main()
