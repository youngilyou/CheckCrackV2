"""Writes the automatic floor-line suggestion `{facade_id}_floors.json` for a finished COLMAP/Dense result folder
(src/geometry/floor_estimate.py). An existing operator confirmation is kept.

Usage: python tools/suggest_floors.py <output_dir> <facade_id>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.geometry.floor_estimate import write_floor_suggestion  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    path = write_floor_suggestion(argv[0], argv[1])
    if path is None:
        print("no floor suggestion: not a COLMAP/Dense result folder (needs *_scale_colmap.json + *_analysis_colmap*.tif)")
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps(data["suggested"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
