"""Temp-file-then-atomic-rename write helpers.

None of this pipeline's writes (mosaic TIFFs, JSON reports, parquet
catalogs) used to stage to a temp path first -- every write went straight
to the final filename (confirmed by auditing every write site in
src/pipeline/runner.py, src/capture/image_catalog.py, tools/detect_cracks_*.py).
A crash/kill mid-write -- a full-resolution mosaic TIFF write is not
instant -- could leave a half-written file sitting at the final path,
silently clobbering a previously-successful result. These helpers write to
"<path>.tmp" in the *same directory* (so the rename stays on one volume,
which is what makes os.replace atomic) and only replace the final path once
the write has fully succeeded. This is the practical, present-day version of
"실패 시 버전을 늘리지 않는다": there's no versioning scheme yet, so the thing
to protect is simply the one existing good copy at the final filename.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd


def _tmp_path(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def atomic_write_json(path: str | Path, obj: Any, *, indent: int = 2, ensure_ascii: bool = False) -> None:
    path = Path(path)
    tmp_path = _tmp_path(path)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=indent, ensure_ascii=ensure_ascii)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    path = Path(path)
    tmp_path = _tmp_path(path)
    try:
        tmp_path.write_text(text, encoding=encoding)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_parquet(df: "pd.DataFrame", path: str | Path) -> None:
    path = Path(path)
    tmp_path = _tmp_path(path)
    try:
        df.to_parquet(tmp_path, index=False)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
