"""Operator-drawn "this is the real front face" region (2026-09-19, user
request: 다각형 그리기로 정면만 선택). A manually-drawn override for the
automatic wall-region filter (rectification.py's compute_wall_region_canvas_mask)
-- the automatic mask stays the DEFAULT (#1 of the reviewed design: "기본은
지금 정의 기준으로 진행"); this file only matters when a human has explicitly
drawn a region for this exact facade, an on-demand correction, never a
mandatory step (a fully-automated batch run never produces one).

Deliberately NOT used to crop/mutate the stored mosaic TIFF -- CLAUDE.local.md
#11 ("원본 사진 provenance를 삭제하지 않는다") rules that out. This is purely a
crack-detection-time filter, identical in spirit and file format role to the
automatic wall_region_mask -- crack/pipeline.py and crack/raw_pipeline.py
already accept a `wall_region_mask` parameter that this can substitute for.

One facade can have MULTIPLE disjoint polygons (2026-09-19 user requirement:
"멀티 다각형 영역을 선택") -- e.g. two separate valid front-face patches split
by an obstruction -- so `polygons` is always a list, even for the common
single-polygon case."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np


def load_manual_region_mask(
    path: str | Path, canvas_size: tuple[int, int]
) -> np.ndarray | None:
    """Returns a (canvas_h, canvas_w) uint8 mask (255 = inside an operator-
    drawn polygon, 0 = outside) or None if `path` doesn't exist -- the
    "no manual override, fall back to the automatic mask" case, never
    fabricated. Vertices are stored in the SAME canvas-pixel space as the
    mosaic that was on screen when the operator drew them; if a re-stitch
    later produced a different-sized canvas (`canvas_width`/`canvas_height`
    in the file no longer match `canvas_size`), the stored polygon no longer
    means anything reliable at those coordinates -- returns None rather than
    silently rasterizing a stale shape onto a mismatched canvas (same "don't
    guess" principle as the rest of this pipeline)."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        # "utf-8-sig", not "utf-8": the C# side (ImageViewerWindow.SaveManualRegionJson)
        # writes via .NET's Encoding.UTF8, which emits a leading BOM by default --
        # confirmed real, 2026-09-19 (json.loads raised JSONDecodeError on every
        # manual_region.json the viewer had ever saved, silently swallowed by the
        # except below, so the dim overlay/crack filter never actually activated
        # even though the file existed and looked fine on disk). utf-8-sig strips
        # a BOM if present and is a no-op otherwise, so it's safe for files from
        # either writer (this module's own save_manual_region never adds one).
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None

    canvas_w, canvas_h = canvas_size
    if int(data.get("canvas_width", -1)) != canvas_w or int(data.get("canvas_height", -1)) != canvas_h:
        return None

    polygons = data.get("polygons") or []
    mask = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    any_valid = False
    for poly in polygons:
        if len(poly) < 3:
            continue
        pts = np.asarray(poly, dtype=np.float64).round().astype(np.int32).reshape(-1, 1, 2)
        cv2.fillPoly(mask, [pts], 255)
        any_valid = True

    return mask if any_valid else None


def save_manual_region(
    path: str | Path, canvas_size: tuple[int, int], polygons: list[list[tuple[float, float]]]
) -> None:
    """Writes the file `load_manual_region_mask` reads back. `polygons` is a
    list of vertex-lists, each `[(x, y), ...]` in canvas-pixel space (the
    same space CheckCrackViewer's polygon-drawing tool captured clicks in)."""
    path = Path(path)
    canvas_w, canvas_h = canvas_size
    payload = {
        "canvas_width": canvas_w,
        "canvas_height": canvas_h,
        "polygons": [[[round(float(x), 1), round(float(y), 1)] for x, y in poly] for poly in polygons],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
