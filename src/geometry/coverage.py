"""Facade capture-overlap verification (CLAUDE.local.md #3.4 / TTA 촬영 중복도
기준: 종방향(비행 방향) >=80%, 횡방향(다음 줄로 이동) 70~80%, 최소 70%).

Confirmed real gap, 2026-09-15: this project's own config/pipeline.yaml had NO
`capture:` section and no code anywhere checked whether a facade's actual
captured images met CLAUDE.local.md #3.4's own stated overlap targets --
overlap was assumed correct by mission planning alone, with nothing in the
pipeline able to say after the fact "this gap needs a reshoot." This module
is that check.

Runs on stage-1's own already-rectified per-image ROIs (pipeline/runner.py's
`_estimate_coverage_ratio` already calls `rectify_images` once for the
off-wall/coverage checks -- this reuses that same `warped` dict instead of
rectifying a second time).

Methodology: classification of "horizontal" (종방향, same-pass) vs "vertical"
(횡방향, cross-row) is done GEOMETRICALLY from each image's own rectified
canvas position, not from capture-order timestamps. An earlier
timestamp-order-based version was tried and broken by a synthetic self-test
(2026-09-15): the single image pair at the end of one row and the start of
the next is temporally consecutive but spatially a row change, so
"temporally consecutive = horizontal" misclassified exactly that pair, and
"not temporally adjacent = vertical" also wrongly swept in same-row images
more than one frame apart (common in any row with >2 images at real overlap
ratios). Canvas position has neither problem: for facade image i and any
other image j, whichever axis (canvas x vs canvas y) their centers differ
more along tells you whether j is "further along the same pass" (x-dominant)
or "a different pass/row" (y-dominant) -- true regardless of capture order.

For each image, only its NEAREST neighbor in each direction category is used
(closest horizontal-dominant neighbor, closest vertical-dominant neighbor) --
overlap with a same-row image several frames away is expected to be small
and is not a meaningful signal either way.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from src.stitching.warp import WarpedImage


@dataclass
class OverlapGap:
    image_a: str
    image_b: str
    kind: str  # "horizontal" (종방향/same-pass) | "vertical" (횡방향/cross-row)
    overlap_ratio: float


@dataclass
class OverlapReport:
    facade_id: str
    horizontal_overlap_mean: float | None = None
    horizontal_overlap_min: float | None = None
    vertical_overlap_mean: float | None = None
    vertical_overlap_min: float | None = None
    horizontal_target: float = 0.80
    vertical_target: float = 0.80
    minimum_overlap: float = 0.70
    gaps: list[OverlapGap] = field(default_factory=list)
    needs_retake: bool = False
    note: str | None = None


def _center(w: WarpedImage) -> tuple[float, float]:
    x, y = w.corner
    ww, hh = w.size
    return x + ww / 2.0, y + hh / 2.0


def _mask_overlap_ratio(a: WarpedImage, b: WarpedImage) -> float:
    """Fraction of the SMALLER image's own valid-pixel mask that's also
    covered by the other -- the standard photogrammetry overlap-% convention
    (relative to one frame's own footprint, not the union)."""
    ax, ay = a.corner
    aw, ah = a.size
    bx, by = b.corner
    bw, bh = b.size
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    a_crop = a.mask[y0 - ay : y1 - ay, x0 - ax : x1 - ax]
    b_crop = b.mask[y0 - by : y1 - by, x0 - bx : x1 - bx]
    inter = int(np.count_nonzero((a_crop > 0) & (b_crop > 0)))
    if inter == 0:
        return 0.0
    a_area = int(np.count_nonzero(a.mask))
    b_area = int(np.count_nonzero(b.mask))
    smaller = min(a_area, b_area)
    return 0.0 if smaller == 0 else inter / smaller


def compute_overlap_report(
    facade_id: str,
    warped: dict[str, WarpedImage],
    horizontal_overlap_target: float = 0.80,
    vertical_overlap_target: float = 0.80,
    minimum_overlap: float = 0.70,
) -> OverlapReport:
    ids = list(warped.keys())
    if len(ids) < 2:
        return OverlapReport(
            facade_id=facade_id, horizontal_target=horizontal_overlap_target,
            vertical_target=vertical_overlap_target, minimum_overlap=minimum_overlap,
            note="fewer than 2 images -- nothing to compare",
        )
    centers = {iid: _center(warped[iid]) for iid in ids}

    horiz_ratios: list[float] = []
    vert_ratios: list[float] = []
    reported_pairs: set[tuple[str, str, str]] = set()
    gaps: list[OverlapGap] = []

    for a_id in ids:
        ax, ay = centers[a_id]
        best_h: tuple[float, str | None] = (math.inf, None)
        best_v: tuple[float, str | None] = (math.inf, None)
        for b_id in ids:
            if b_id == a_id:
                continue
            bx, by = centers[b_id]
            dx, dy = bx - ax, by - ay
            dist = math.hypot(dx, dy)
            if abs(dx) >= abs(dy):
                if dist < best_h[0]:
                    best_h = (dist, b_id)
            else:
                if dist < best_v[0]:
                    best_v = (dist, b_id)

        if best_h[1] is not None:
            ratio = _mask_overlap_ratio(warped[a_id], warped[best_h[1]])
            horiz_ratios.append(ratio)
            if ratio < minimum_overlap:
                pair = tuple(sorted((a_id, best_h[1])))
                key = (pair[0], pair[1], "horizontal")
                if key not in reported_pairs:
                    reported_pairs.add(key)
                    gaps.append(OverlapGap(image_a=pair[0], image_b=pair[1], kind="horizontal", overlap_ratio=round(ratio, 4)))

        if best_v[1] is not None:
            ratio = _mask_overlap_ratio(warped[a_id], warped[best_v[1]])
            vert_ratios.append(ratio)
            if ratio < minimum_overlap:
                pair = tuple(sorted((a_id, best_v[1])))
                key = (pair[0], pair[1], "vertical")
                if key not in reported_pairs:
                    reported_pairs.add(key)
                    gaps.append(OverlapGap(image_a=pair[0], image_b=pair[1], kind="vertical", overlap_ratio=round(ratio, 4)))

    return OverlapReport(
        facade_id=facade_id,
        horizontal_overlap_mean=float(np.mean(horiz_ratios)) if horiz_ratios else None,
        horizontal_overlap_min=float(np.min(horiz_ratios)) if horiz_ratios else None,
        vertical_overlap_mean=float(np.mean(vert_ratios)) if vert_ratios else None,
        vertical_overlap_min=float(np.min(vert_ratios)) if vert_ratios else None,
        horizontal_target=horizontal_overlap_target,
        vertical_target=vertical_overlap_target,
        minimum_overlap=minimum_overlap,
        gaps=gaps,
        needs_retake=len(gaps) > 0,
        note=None if vert_ratios else "no cross-row (vertical-dominant) neighbor found for any image -- likely a single-row/single-pass capture",
    )
