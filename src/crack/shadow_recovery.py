"""Selective shadow-ROI recovery pass (user design, 2026-08-26):

    ORIGINAL (no correction) crack detection = default
                    |
    strong-shadow ROI where detection is hard
                    |
    selective illumination correction (RETINEX first, then PhaSR)
                    | (only genuinely NEW candidates are kept)
    merged into the default result

This replaces "correct the whole photo, then detect" with "detect on the
untouched original first, then only touch the small, specific regions
where strong shadow made that hard." Two direct benefits over whole-image
correction (src/crack/multiview.py's detect_cracks_in_original still does
that, unchanged, for callers that want it):

  1. Every correction backend's artifacts (PhaSR's color drift/tile seams,
     even classical RETINEX's more conservative changes) are confined to
     small, already-flagged-as-suspect crops instead of the whole photo --
     a photo with no hard shadows never gets touched by PhaSR at all.
  2. The corrected-ROI pass is strictly ADDITIVE: a crack the base
     (uncorrected) pass already found stays exactly as the base pass found
     it (measured off real, unaltered pixels). Correction is only ever used
     to catch something the base pass MISSED, never to overrule or replace
     a detection that already exists -- consistent with the project's
     "never let a later automated step silently discard/replace evidence"
     principle (CLAUDE.local.md #6/#9 extended to this case).
"""

from __future__ import annotations

import cv2
import numpy as np

from src.common.config import Config
from src.crack.detector import CrackDetection, CrackDetector
from src.crack.merge_tiles import CrackPolygon, merge_detections
from src.crack.tiler import Tile, tile_mosaic
from src.illumination.correction import MODE_RETINEX, correct_illumination, shadow_darkness_mask_local


def _subdivide(x0: int, y0: int, x1: int, y1: int, max_area: float) -> list[tuple[int, int, int, int]]:
    """Splits an oversized bbox into a grid of cells each <= max_area, so an
    oversized shadow component still gets fully covered by the recovery
    pass instead of being dropped (losing coverage over a large shadow is
    exactly the kind of silent gap CLAUDE.local.md #8/#21 rules out for
    tiling in general)."""
    w, h = x1 - x0, y1 - y0
    if w * h <= max_area:
        return [(x0, y0, x1, y1)]
    # Roughly square cells: side length such that side^2 <= max_area.
    side = max(1, int(max_area**0.5))
    cells: list[tuple[int, int, int, int]] = []
    y = y0
    while y < y1:
        cy1 = min(y + side, y1)
        x = x0
        while x < x1:
            cx1 = min(x + side, x1)
            cells.append((x, y, cx1, cy1))
            x = cx1
        y = cy1
    return cells


def _merge_nearby(
    boxes: list[tuple[int, int, int, int]], gap_px: int
) -> list[tuple[int, int, int, int]]:
    """Union-find merge of boxes that overlap or are within gap_px of each
    other -- inflate each box by gap_px on all sides and check standard
    rectangle overlap on the inflated copies, then take the union of the
    ORIGINAL (non-inflated) boxes in each merged group. Needed because
    real shadow patterns fragment into many small, closely-spaced
    components (e.g. a repeating balcony-fin shadow pattern) that are
    obviously "the same shadow area" to a human but come out of
    connectedComponentsWithStats as dozens of separate small boxes --
    confirmed on a real photo, 2026-08-26 (128 raw ROIs before merging)."""
    n = len(boxes)
    if n == 0:
        return []
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    def inflated_overlap(a, b) -> bool:
        ax0, ay0, ax1, ay1 = a
        bx0, by0, bx1, by1 = b
        return not (
            ax1 + gap_px <= bx0 or bx1 + gap_px <= ax0 or ay1 + gap_px <= by0 or by1 + gap_px <= ay0
        )

    for i in range(n):
        for j in range(i + 1, n):
            if inflated_overlap(boxes[i], boxes[j]):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    merged: list[tuple[int, int, int, int]] = []
    for idxs in groups.values():
        xs0 = [boxes[i][0] for i in idxs]
        ys0 = [boxes[i][1] for i in idxs]
        xs1 = [boxes[i][2] for i in idxs]
        ys1 = [boxes[i][3] for i in idxs]
        merged.append((min(xs0), min(ys0), max(xs1), max(ys1)))
    return merged


def _detect_all(image_id: str, image: np.ndarray, cfg: Config, detector: CrackDetector) -> list[CrackPolygon]:
    """Shared tile+detect+merge helper over a WHOLE image (not a crop)."""
    observed_mask = np.ones(image.shape[:2], dtype=np.uint8)
    tiles = tile_mosaic(image_id, image, observed_mask, cfg)
    tiles_by_id: dict[str, Tile] = {t.tile_id: t for t in tiles}
    detections: list[CrackDetection] = []
    for tile in tiles:
        detections.extend(detector.infer_tile(tile))
    return merge_detections(tiles_by_id, detections, facade_id=image_id)


def find_hard_detection_rois(
    image_id: str,
    image: np.ndarray,
    cfg: Config,
    detector: CrackDetector,
    base_polygons: list[CrackPolygon],
) -> list[tuple[int, int, int, int]]:
    """ROI = "detection is hard here", not just "this is dark" (design
    correction, 2026-08-26): a plain shadow-darkness threshold produced
    ROIs covering ~100% of a real test photo with a widespread repeating
    balcony-shadow pattern, no matter how strict the threshold -- shadow
    darkness alone doesn't localize anything on a photo like that.

    Instead: re-run detection with the confidence threshold temporarily
    LOWERED, surfacing "near-miss" candidates the normal pass didn't quite
    clear. Keep only the near-misses that (a) do NOT substantially overlap
    an already-confident base-pass detection (no point "recovering"
    something already found) and (b) sit in a real shadow region (a
    near-miss NOT in shadow has some other reason for its low confidence --
    not this pass's job to chase). Each surviving near-miss's own bbox
    (padded) becomes an ROI -- naturally small and specific, since it's
    anchored to one actual candidate location rather than a broad mask."""
    rcfg = cfg.shadow_recovery

    original_conf = detector.confidence
    detector.confidence = original_conf * float(rcfg.low_confidence_scale)
    try:
        near_miss = _detect_all(f"{image_id}_nearmiss", image, cfg, detector)
    finally:
        detector.confidence = original_conf

    base_polys = [p for p in (_to_shapely(b.polygon_px) for b in base_polygons) if p is not None]
    shadow_mask = shadow_darkness_mask_local(image)
    h, w = image.shape[:2]
    pad = int(rcfg.roi_padding_px)
    shadow_threshold = float(rcfg.roi_threshold)

    raw_boxes: list[tuple[int, int, int, int]] = []
    for cand in near_miss:
        cand_poly = _to_shapely(cand.polygon_px)
        if cand_poly is None:
            continue
        already_found = False
        for bp in base_polys:
            inter = cand_poly.intersection(bp).area
            if inter > 0 and inter / cand_poly.union(bp).area >= 0.2:
                already_found = True
                break
        if already_found:
            continue

        cx, cy = cand_poly.centroid.x, cand_poly.centroid.y
        cxi, cyi = int(np.clip(cx, 0, w - 1)), int(np.clip(cy, 0, h - 1))
        if shadow_mask[cyi, cxi] < shadow_threshold:
            continue

        x0f, y0f = cand.polygon_px.min(axis=0)
        x1f, y1f = cand.polygon_px.max(axis=0)
        x0, y0 = max(0, int(x0f) - pad), max(0, int(y0f) - pad)
        x1, y1 = min(w, int(x1f) + pad), min(h, int(y1f) + pad)
        raw_boxes.append((x0, y0, x1, y1))

    merged_boxes = _merge_nearby(raw_boxes, int(rcfg.roi_merge_gap_px))
    max_area = float(rcfg.roi_max_area_fraction) * (h * w)
    rois: list[tuple[int, int, int, int]] = []
    for box in merged_boxes:
        rois.extend(_subdivide(*box, max_area))
    return rois


def _detect_in_crop(
    image_id: str,
    crop: np.ndarray,
    x_offset: int,
    y_offset: int,
    id_prefix: str,
    cfg: Config,
    detector: CrackDetector,
) -> list[CrackPolygon]:
    """Tiles/detects/merges within one crop (same path
    multiview.detect_cracks_in_original uses on a whole photo), then shifts
    every polygon back into the ORIGINAL photo's own coordinate space by
    (x_offset, y_offset) -- so callers never need to know this ran on a
    crop at all."""
    observed_mask = np.ones(crop.shape[:2], dtype=np.uint8)
    tiles = tile_mosaic(f"{image_id}_{id_prefix}", crop, observed_mask, cfg)
    tiles_by_id: dict[str, Tile] = {t.tile_id: t for t in tiles}

    detections: list[CrackDetection] = []
    for tile in tiles:
        detections.extend(detector.infer_tile(tile))

    polygons = merge_detections(tiles_by_id, detections, facade_id=f"{image_id}_{id_prefix}")
    offset = np.array([x_offset, y_offset], dtype=np.float64)
    for poly in polygons:
        poly.polygon_px = poly.polygon_px + offset
    return polygons


def _to_shapely(poly_px: np.ndarray):
    from shapely.geometry import Polygon

    if len(poly_px) < 3:
        return None
    poly = Polygon(poly_px)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly if (not poly.is_empty and poly.is_valid and poly.geom_type == "Polygon") else None


def _add_if_new(
    existing: list[CrackPolygon], candidates: list[CrackPolygon], iou_threshold: float = 0.2
) -> list[CrackPolygon]:
    """Keeps only candidates that do NOT substantially overlap anything
    already in `existing` -- the additive-only rule (see module docstring):
    this never removes or replaces an existing detection, only appends ones
    the base pass missed."""
    existing_polys = [p for p in (_to_shapely(e.polygon_px) for e in existing) if p is not None]
    kept: list[CrackPolygon] = []
    for cand in candidates:
        cand_poly = _to_shapely(cand.polygon_px)
        if cand_poly is None:
            continue
        overlaps_existing = False
        for ep in existing_polys:
            inter = cand_poly.intersection(ep).area
            if inter == 0:
                continue
            if inter / cand_poly.union(ep).area >= iou_threshold:
                overlaps_existing = True
                break
        if not overlaps_existing:
            kept.append(cand)
    return kept


def detect_cracks_with_shadow_recovery(
    image_id: str,
    image: np.ndarray,
    cfg: Config,
    detector: CrackDetector,
    illumination_device: str = "cuda",
) -> list[CrackPolygon]:
    """Default: detect on the ORIGINAL image (no illumination correction at
    all). Then find strong-shadow ROIs and re-detect just those crops,
    first with RETINEX (cheap, no GPU-heavy backbone), then with PhaSR
    (stronger but slower/riskier) -- both passes are additive-only against
    the base result (see module docstring), and additive against EACH OTHER
    too (RETINEX's new finds are added first; PhaSR only contributes
    candidates that are new against base+RETINEX combined, so the same
    real crack recovered by both doesn't get double-counted)."""
    base = _detect_all(image_id, image, cfg, detector)

    rois = find_hard_detection_rois(image_id, image, cfg, detector, base)
    combined = list(base)

    for k, (x0, y0, x1, y1) in enumerate(rois):
        crop = image[y0:y1, x0:x1]

        retinex_crop = correct_illumination(crop, MODE_RETINEX, cfg)
        retinex_candidates = _detect_in_crop(image_id, retinex_crop, x0, y0, f"RETINEXROI{k}", cfg, detector)
        new_from_retinex = _add_if_new(combined, retinex_candidates)
        combined.extend(new_from_retinex)

        from src.illumination.phasr_wrapper import correct_illumination_phasr

        phasr_crop = correct_illumination_phasr(crop, device=illumination_device)
        phasr_candidates = _detect_in_crop(image_id, phasr_crop, x0, y0, f"PHASRROI{k}", cfg, detector)
        new_from_phasr = _add_if_new(combined, phasr_candidates)
        combined.extend(new_from_phasr)

    return combined
