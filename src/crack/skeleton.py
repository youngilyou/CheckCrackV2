"""Crack length/width from a polygon (CLAUDE.local.md #25).

Length is skeleton arc length, not polygon perimeter or bbox diagonal.
Width is explicitly NOT bbox width (#25 forbids it) — it's 2x the
distance-transform value sampled along the skeleton (the standard
medial-axis width estimate), so a long diagonal crack doesn't get
mis-measured as "wide" just because its bounding box is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np
from skimage.morphology import skeletonize


@dataclass
class CrackMeasurement:
    length_px: float
    max_width_px: float
    mean_width_px: float
    skeleton_px: np.ndarray  # (N, 2) facade-global pixel coords


def _skeleton_arc_length(skeleton_mask: np.ndarray) -> float:
    ys, xs = np.where(skeleton_mask)
    coords = set(zip(xs.tolist(), ys.tolist()))
    length = 0.0
    for x, y in coords:
        for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
            if (x + dx, y + dy) in coords:
                length += math.hypot(dx, dy)
    return length


def measure_polygon(polygon_px: np.ndarray) -> CrackMeasurement | None:
    min_xy = polygon_px.min(axis=0)
    max_xy = polygon_px.max(axis=0)
    pad = 2
    w = int(np.ceil(max_xy[0] - min_xy[0])) + 2 * pad
    h = int(np.ceil(max_xy[1] - min_xy[1])) + 2 * pad
    if w <= 0 or h <= 0:
        return None

    local_poly = np.round(polygon_px - min_xy + pad).astype(np.int32)
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(mask, [local_poly], 255)
    if mask.sum() == 0:
        return None

    skeleton = skeletonize(mask > 0)
    ys, xs = np.where(skeleton)
    if len(xs) < 2:
        return None

    dist = cv2.distanceTransform((mask > 0).astype(np.uint8), cv2.DIST_L2, 5)
    widths = 2.0 * dist[ys, xs]

    skeleton_global = np.column_stack([xs, ys]).astype(np.float64) + (min_xy - pad)

    return CrackMeasurement(
        length_px=_skeleton_arc_length(skeleton),
        max_width_px=float(widths.max()),
        mean_width_px=float(widths.mean()),
        skeleton_px=skeleton_global,
    )
