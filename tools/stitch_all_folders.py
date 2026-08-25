"""Stitch every subfolder of a parent folder as its own independent facade.

Usage:
    python tools/stitch_all_folders.py <parent_folder>

Expects a layout like:
    MyBuilding/
      left/
      right/
      front/
      back/
      top/

Each subfolder is stitched separately (facade_id = subfolder name) — they
are never merged into one mosaic (CLAUDE.local.md #1: a building is never
one 360-degree stitch). This is the batch form of stitch_folder.py for
someone who already organized captures as "1 folder = 1 side".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline.runner import run_facade_poc  # noqa: E402

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python tools/stitch_all_folders.py <parent_folder>")
        sys.exit(1)

    parent = Path(sys.argv[1])
    if not parent.is_dir():
        print(f"not a folder: {parent}")
        sys.exit(1)

    subfolders = sorted(
        p for p in parent.iterdir() if p.is_dir() and any(f.suffix.lower() in _IMAGE_EXTS for f in p.glob("*"))
    )
    if not subfolders:
        print(f"no subfolders with images found under {parent}")
        sys.exit(1)

    print(f"found {len(subfolders)} side(s): {', '.join(p.name for p in subfolders)}")

    results: dict[str, Path | None] = {}
    for sub in subfolders:
        facade_name = sub.name
        print(f"\n=== {facade_name} ({sub}) ===")
        try:
            out = run_facade_poc(
                facade_id=facade_name,
                images_dir=sub,
                output_root="facades",
                config_path="config/pipeline.yaml",
            )
        except Exception as exc:
            print(f"  FAILED: {exc}")
            out = None
        results[facade_name] = out
        print(f"  -> {out if out else 'FAILED (see logs/pipeline.log)'}")

    print("\n=== summary ===")
    for name, out in results.items():
        print(f"  {name}: {'OK -> ' + str(out) if out else 'FAILED'}")


if __name__ == "__main__":
    main()
