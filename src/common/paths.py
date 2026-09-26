"""Path helpers shared by the crack tools.

`{facade_id}_source_images.json` stores each photo's ABSOLUTE path as it was on the machine that ran the
stitching. On another computer (different drive letter / folder) those paths do not exist, which used to
break crack detection (silently falling back to mosaic tiling), the viewer's original-photo panel and the
click locator. `resolve_source_image` finds the same file name near the run's output folder instead.
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_source_image(file_path: str, output_dir: str | Path) -> str | None:
    """Return an existing path for this photo, or None.

    Order: the stored path as is -> $CHECKCRACK_IMAGES_DIR/<name> -> <name> in the output folder's
    parents (the normal layout is <photos>/output/Vnnn/, so the photos sit 2 levels above) -> a sibling
    `images` folder. Only the file NAME is matched -- a photo is never substituted by a different one.
    """
    if file_path and Path(file_path).exists():
        return file_path
    # Windows paths written on another machine contain backslashes even when read on Linux; take the last part.
    name = str(file_path).replace("\\", "/").rsplit("/", 1)[-1]
    if not name:
        return None
    candidates: list[Path] = []
    env_dir = os.environ.get("CHECKCRACK_IMAGES_DIR")
    if env_dir:
        candidates.append(Path(env_dir) / name)
    out = Path(output_dir)
    for parent in [out.parent, out.parent.parent, out.parent.parent.parent]:
        candidates.append(parent / name)
        candidates.append(parent / "images" / name)
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None
