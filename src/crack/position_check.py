"""Measured position error of a crack marker on the displayed mosaic.

사용자 요구(2026-09-26): "스티칭에서 크랙 번호를 선택하면 원본 크랙 위치 표시 오차를 볼 수 있도록".
Definition (no reference to any model output, purely image content): take the ORIGINAL photo's
pixels around the crack, place them on the canvas with the SAME mapping that placed the crack
(`map_fn`), and see how far that placed patch has to be shifted to line up with the DISPLAYED mosaic
(normalized cross-correlation, sub-pixel). If the crack marker is where the crack really is in the
displayed mosaic, the shift is ~0; a marker that sits d px away from the crack's real place in the
mosaic reads as a d px shift here. Returned in canvas px (1 canvas px = 1/px_per_m m).

Nothing is invented: low-contrast windows, low correlation, or a best shift on the search border
return None ("cannot verify"), never a made-up number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import cv2
import numpy as np

MapFn = Callable[[np.ndarray], "tuple[np.ndarray, np.ndarray]"]  # raw (N,2) -> (canvas (N,2), valid (N,))


@dataclass(frozen=True)
class PositionCheck:
    dx: float  # canvas px, mosaic-content minus marker placement (x)
    dy: float
    ncc: float

    @property
    def offset_px(self) -> float:
        return float(np.hypot(self.dx, self.dy))


def check_position(
    map_fn: MapFn,
    raw_gray: np.ndarray,
    raw_center_xy: tuple[float, float],
    canvas_center_xy: tuple[float, float],
    mosaic_gray: np.ndarray,
    px_per_raw: float,
    template_radius: int = 48,
    search_radius: int = 24,
    min_ncc: float = 0.5,
    min_template_std: float = 4.0,
) -> PositionCheck | None:
    """`px_per_raw`: approximate canvas px per raw px near the crack (sets how big a raw window is
    needed to fill the canvas template window). `canvas_center_xy`: where `map_fn` puts the crack."""
    cx, cy = int(round(canvas_center_xy[0])), int(round(canvas_center_xy[1]))
    R, S = template_radius, search_radius
    mh, mw = mosaic_gray.shape[:2]
    span = R + S
    if cx - span < 0 or cy - span < 0 or cx + span >= mw or cy + span >= mh:
        return None

    half_raw = int(np.ceil(1.4 * R / max(px_per_raw, 1e-3)))
    rh, rw = raw_gray.shape[:2]
    x0, x1 = int(max(0, raw_center_xy[0] - half_raw)), int(min(rw, raw_center_xy[0] + half_raw))
    y0, y1 = int(max(0, raw_center_xy[1] - half_raw)), int(min(rh, raw_center_xy[1] + half_raw))
    if x1 - x0 < 16 or y1 - y0 < 16:
        return None
    step = 2  # canvas is ~4x coarser than the raw photo: 2 px sampling still over-samples every canvas px
    xs, ys = np.meshgrid(np.arange(x0, x1, step), np.arange(y0, y1, step))
    pts = np.column_stack([xs.ravel(), ys.ravel()]).astype(np.float64)
    canvas_xy, valid = map_fn(pts)
    if valid.sum() < 100:
        return None
    vals = raw_gray[pts[valid, 1].astype(np.int64), pts[valid, 0].astype(np.int64)].astype(np.float64)
    cxy = canvas_xy[valid]

    # Splat (average) the photo's pixels into a canvas window of the template's size.
    n = 2 * R + 1
    ix = np.round(cxy[:, 0] - (cx - R)).astype(np.int64)
    iy = np.round(cxy[:, 1] - (cy - R)).astype(np.int64)
    inside = (ix >= 0) & (ix < n) & (iy >= 0) & (iy < n)
    if inside.sum() < 200:
        return None
    flat_idx = iy[inside] * n + ix[inside]
    sums = np.bincount(flat_idx, weights=vals[inside], minlength=n * n)
    counts = np.bincount(flat_idx, minlength=n * n)
    filled = counts > 0
    if filled.mean() < 0.6:
        return None  # the photo does not cover enough of this canvas window
    patch = np.zeros(n * n, dtype=np.float32)
    patch[filled] = (sums[filled] / counts[filled]).astype(np.float32)
    patch = patch.reshape(n, n)
    holes = (~filled).reshape(n, n).astype(np.uint8) * 255
    if holes.any():
        patch = cv2.inpaint(np.clip(patch, 0, 255).astype(np.uint8), holes, 3, cv2.INPAINT_TELEA).astype(np.float32)
    if patch.std() < min_template_std:
        return None

    search = mosaic_gray[cy - span: cy + span + 1, cx - span: cx + span + 1].astype(np.float32)
    res = cv2.matchTemplate(search, patch, cv2.TM_CCOEFF_NORMED)
    _, best, _, loc = cv2.minMaxLoc(res)
    a, b = loc[0], loc[1]
    if best < min_ncc or a in (0, res.shape[1] - 1) or b in (0, res.shape[0] - 1):
        return None

    def parabola(l: float, c: float, r: float) -> float:
        d = l - 2 * c + r
        return 0.0 if abs(d) < 1e-9 else float(np.clip(0.5 * (l - r) / d, -0.5, 0.5))

    dx = (a - S) + parabola(res[b, a - 1], res[b, a], res[b, a + 1])
    dy = (b - S) + parabola(res[b - 1, a], res[b, a], res[b + 1, a])
    return PositionCheck(dx=float(dx), dy=float(dy), ncc=float(best))


def compute_position_checks(
    cracks,
    image_paths: dict[str, str],
    source_transforms: dict[str, dict],
    mosaic_bgr: np.ndarray,
    canvas_px_per_m: float | None,
    calibrated: bool,
    depth_mapper=None,
) -> dict[str, dict | None]:
    """crack_id -> position_check dict (JSON-ready) for each crack's primary source photo
    (source_observations[0], the one 원본 보기 shows), or None when it cannot be verified.

    Placement used is the same one that placed the crack: the photo's own depth when the run has a
    DepthCanvasMapper and this photo has depth there, otherwise the flat homography ("flat"), so the
    number always describes the marker actually drawn. mm only when the canvas scale is calibrated
    (CLAUDE.local.md #9/#26)."""
    from src.common.imageio import imread_unicode

    mosaic_gray = cv2.cvtColor(mosaic_bgr, cv2.COLOR_BGR2GRAY)
    by_image: dict[str, list] = {}
    for crack in cracks:
        if crack.source_observations:
            by_image.setdefault(crack.source_observations[0].image_id, []).append(crack)

    results: dict[str, dict | None] = {c.crack_id: None for c in cracks}
    for image_id, group in by_image.items():
        path = image_paths.get(image_id)
        transform = source_transforms.get(image_id)
        raw = imread_unicode(path, cv2.IMREAD_GRAYSCALE) if path else None
        if raw is None or transform is None:
            continue
        H = np.asarray(transform["H"], dtype=np.float64)
        use_depth = depth_mapper is not None and depth_mapper.has_image(image_id)

        def flat_fn(pts: np.ndarray, _H=H):
            return cv2.perspectiveTransform(pts.reshape(-1, 1, 2), _H).reshape(-1, 2), np.ones(len(pts), dtype=bool)

        def depth_fn(pts: np.ndarray, _id=image_id):
            return depth_mapper.map_points(_id, pts, depth_radius=1)

        for crack in group:
            obs = crack.source_observations[0]
            polygon_raw = np.asarray(obs.polygon_px_in_source, dtype=np.float64)
            centroid = polygon_raw.mean(axis=0)
            method, map_fn = "flat", flat_fn
            if use_depth:
                _, ok = depth_mapper.map_points(image_id, centroid[None, :], depth_radius=1)
                if ok[0]:
                    method, map_fn = "depth", depth_fn
            probe = np.array([centroid, centroid + [20, 0], centroid + [0, 20]])
            xy, ok = map_fn(probe)
            if not ok.all():
                continue
            ppr = float(np.sqrt(abs(np.cross(xy[1] - xy[0], xy[2] - xy[0]))) / 20.0)
            if not np.isfinite(ppr) or ppr <= 1e-3:
                continue
            check = check_position(
                map_fn, raw, (float(centroid[0]), float(centroid[1])),
                (float(xy[0][0]), float(xy[0][1])), mosaic_gray, ppr,
            )
            if check is None:
                continue
            results[crack.crack_id] = {
                "offset_px": round(check.offset_px, 2), "dx": round(check.dx, 2), "dy": round(check.dy, 2),
                "ncc": round(check.ncc, 3), "method": method, "image_id": image_id,
                "offset_mm": (round(check.offset_px / canvas_px_per_m * 1000.0, 1)
                              if calibrated and canvas_px_per_m else None),
            }
    return results
