"""Facade mosaic -> Crack entities: tile -> infer -> merge -> measure (CLAUDE.local.md #21-27).

Only run on the `analysis` mosaic (minimal blending, crack detail
preserved, #14) — never the exposure/seam-blended `visual` one.

Every tile this runs on already passed the tiler's
`skip_if_observed_ratio_below` gate, so every crack found here is by
definition within an OBSERVED region (#20: "NO_CRACK => OBSERVED == true"
implies the converse for CRACK too) — `observation_state` is always
"OBSERVED".

`source_image_ids` is facade-level provenance (the full list of images
that went into this facade's mosaic, already written as
`{facade_id}_source_images.json` by the stitching stage) rather than
per-crack. `source_observations` (below) IS the per-crack pin to the exact
1-2 source photos that cover its pixels, computed from the seam-ownership
map + per-image homography the stitching stage persists alongside the
mosaic (`{facade_id}_seam_owner_map.png`/`_homographies.json` --
stitching/mosaic.py's MosaicResult, written by pipeline/runner.py). Both
are optional (`None` from an older facade stitched before this existed) --
`source_observations` degrades to an empty list per crack in that case,
never fabricated/approximated from source_image_ids.
"""

from __future__ import annotations

import cv2
import numpy as np

from src.common.config import Config
from src.common.types import Crack, SourceObservation
from src.crack.detector import CrackDetector
from src.crack.measurement import ScaleInfo, to_mm, to_mm2
from src.crack.merge_tiles import match_crack_ids, merge_detections
from src.crack.skeleton import measure_polygon
from src.crack.tiler import tile_mosaic


def _compute_source_observations(
    polygon_px: np.ndarray,
    seam_owner_map: np.ndarray | None,
    seam_owner_index: list[str] | None,
    source_transforms: dict[str, dict] | None,
    min_overlap_px: int,
) -> list[SourceObservation]:
    """Intersects this crack's mosaic-space polygon against the seam
    ownership map to find which source image(s) actually contributed the
    pixels under this crack (not just which images geometrically overlap
    that area), then maps the crack into each qualifying image's own pixel
    space via that image's homography inverse. See SourceObservation's own
    doc comment for the output shape/ordering."""
    if seam_owner_map is None or seam_owner_index is None or source_transforms is None:
        return []

    canvas_h, canvas_w = seam_owner_map.shape[:2]
    x0f, y0f = polygon_px.min(axis=0)
    x1f, y1f = polygon_px.max(axis=0)
    x0, y0 = max(0, int(np.floor(x0f))), max(0, int(np.floor(y0f)))
    x1, y1 = min(canvas_w, int(np.ceil(x1f)) + 1), min(canvas_h, int(np.ceil(y1f)) + 1)
    if x1 <= x0 or y1 <= y0:
        return []

    # Rasterize the crack polygon into a local (bbox-sized) mask so pixel
    # ownership is checked only where the crack itself actually is, not
    # across its whole bounding box (a diagonal crack's bbox can be mostly
    # empty space that a *different* neighboring source image owns).
    local_mask = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
    local_poly = np.round(polygon_px - np.array([x0, y0])).astype(np.int32).reshape(-1, 1, 2)
    cv2.fillPoly(local_mask, [local_poly], 255)

    owner_crop = seam_owner_map[y0:y1, x0:x1]
    owned = owner_crop[local_mask > 0]
    labels, counts = np.unique(owned, return_counts=True)

    observations: list[SourceObservation] = []
    for label, count in zip(labels, counts):
        if label == 0 or int(count) < min_overlap_px:
            continue
        image_id = seam_owner_index[int(label) - 1]
        transform = source_transforms.get(image_id)
        if transform is None:
            continue
        H = np.asarray(transform["H"], dtype=np.float64)
        width, height = int(transform["width"]), int(transform["height"])
        try:
            H_inv = np.linalg.inv(H)
        except np.linalg.LinAlgError:
            continue

        pts_src = cv2.perspectiveTransform(
            polygon_px.astype(np.float64).reshape(-1, 1, 2), H_inv
        ).reshape(-1, 2)
        pts_src[:, 0] = np.clip(pts_src[:, 0], 0, width - 1)
        pts_src[:, 1] = np.clip(pts_src[:, 1], 0, height - 1)
        sx0, sy0 = pts_src.min(axis=0)
        sx1, sy1 = pts_src.max(axis=0)

        observations.append(
            SourceObservation(
                image_id=image_id,
                bbox_px_in_source=(round(float(sx0), 1), round(float(sy0), 1), round(float(sx1), 1), round(float(sy1), 1)),
                polygon_px_in_source=pts_src.round(1),
                owned_pixel_count=int(count),
            )
        )

    observations.sort(key=lambda o: o.owned_pixel_count, reverse=True)
    return observations


def detect_cracks(
    facade_id: str,
    building_id: str,
    analysis_image: np.ndarray,
    observed_mask: np.ndarray,
    cfg: Config,
    model_path: str,
    scale: ScaleInfo,
    source_image_ids: list[str],
    device: str | None = None,
    previous_cracks: list[dict] | None = None,
    seam_owner_map: np.ndarray | None = None,
    seam_owner_index: list[str] | None = None,
    source_transforms: dict[str, dict] | None = None,
) -> list[Crack]:
    tiles = tile_mosaic(facade_id, analysis_image, observed_mask, cfg)
    if not tiles:
        return []
    tiles_by_id = {t.tile_id: t for t in tiles}

    detector = CrackDetector(model_path, cfg, device=device)
    detections = []
    for tile in tiles:
        detections.extend(detector.infer_tile(tile))

    merged_polygons = merge_detections(tiles_by_id, detections, facade_id)
    # Stable crack_ids across re-runs against the SAME mosaic -- see
    # match_crack_ids' docstring for why this must never be fed a previous
    # run's data from a different stitch version.
    id_match_threshold = float(
        getattr(cfg.measurement, "crack_id_match_iou_threshold", 0.3)
    )
    match_crack_ids(merged_polygons, previous_cracks, iou_threshold=id_match_threshold)
    width_threshold_mm = float(cfg.measurement.crack_width_threshold_mm)
    min_overlap_px = int(getattr(cfg.measurement, "source_observation_min_overlap_px", 20))

    cracks: list[Crack] = []
    for poly in merged_polygons:
        measurement = measure_polygon(poly.polygon_px)
        if measurement is None:
            continue
        x0, y0 = poly.polygon_px.min(axis=0)
        x1, y1 = poly.polygon_px.max(axis=0)
        max_width_mm = to_mm(measurement.max_width_px, scale)
        # Calibration-gated severity (건설 크랙검사 기준 0.3mm) -- never graded
        # from px alone, matching to_mm's own "no calibration, no mm" rule.
        severity = None
        if max_width_mm is not None:
            severity = "정밀점검대상" if max_width_mm >= width_threshold_mm else "경미"
        source_observations = _compute_source_observations(
            poly.polygon_px, seam_owner_map, seam_owner_index, source_transforms, min_overlap_px
        )
        cracks.append(
            Crack(
                crack_id=poly.crack_id,
                building_id=building_id,
                facade_id=facade_id,
                bbox_px=(float(x0), float(y0), float(x1), float(y1)),
                polygon_px=poly.polygon_px,
                skeleton_px=measurement.skeleton_px,
                length_px=measurement.length_px,
                max_width_px=measurement.max_width_px,
                mean_width_px=measurement.mean_width_px,
                area_px=poly.area_px,
                confidence=poly.confidence,
                observation_state="OBSERVED",
                length_mm=to_mm(measurement.length_px, scale),
                max_width_mm=max_width_mm,
                area_mm2=to_mm2(poly.area_px, scale),
                severity=severity,
                source_tile_ids=poly.source_tile_ids,
                source_image_ids=source_image_ids,
                source_observations=source_observations,
            )
        )
    return cracks
