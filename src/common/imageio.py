"""Unicode-safe replacements for cv2.imread/imwrite.

cv2.imread/imwrite go through OpenCV's internal ANSI (not UTF-8, not
wide-char) file path handling on Windows, so any path containing non-ASCII
characters — e.g. this project's Korean direction-group folder names
앞/뒤/좌/우 (CLAUDE.local.md #4.1) — silently mangles and fails with
"can't open/read file", even though the path is completely valid.
np.fromfile/ndarray.tofile go through Python's own (Unicode-correct) file
APIs, so decoding/encoding through an in-memory buffer sidesteps the bug
entirely. Always use these instead of cv2.imread/imwrite directly.
"""

from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np


def imread_unicode(path: str | Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def imwrite_unicode(path: str | Path, image: np.ndarray, params: list[int] | None = None) -> bool:
    path = Path(path)
    ext = path.suffix if path.suffix else ".png"
    ok, buf = cv2.imencode(ext, image, params or [])
    if not ok:
        return False
    # Write to a same-directory temp name, then atomically replace the final
    # path -- a crash/kill mid-write (large mosaics take real wall-clock
    # time) must never leave a half-written file at the final filename,
    # clobbering a previously-good result. See src/common/atomic_io.py's
    # module docstring for the full rationale (same pattern, image-specific).
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        buf.tofile(str(tmp_path))
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return True
