"""Per-original Crack Detection -> Facade coordinate mapping -> Duplicate
Association -> Crack Continuation Association (SmartCrack V2 design review,
2026-08-26).

This is the alternative entry point to crack/pipeline.py's mosaic-tile-based
detect_cracks(): instead of detecting on the stitched mosaic, detect
separately on each original DJI photo (so nothing about crack detection
depends on stitching/blending quality) and only bring the results together
at the very end, by forward-projecting each original's crack polygons into
the same facade/mosaic coordinate space the stitching stage already uses.

Coordinate direction (verified against real stitched output before writing
this module -- round-trip identity test against real testImg
homographies.json, forward then inverse reproduces the original point to
float precision): stitching/warp.py's H maps SOURCE -> FACADE (this is what
warp_images() uses to paint each original onto the mosaic canvas).
crack/pipeline.py's _compute_source_observations already uses H^-1 the
other way (facade -> source) to trace a mosaic-detected crack back to its
original photo. This module does the mirror-image operation: apply H
forward to a crack polygon that was detected directly on the original,
landing it in facade space -- same H, opposite direction.

Two distinct problems, not one (this distinction was the main gap in the
first draft of the SmartCrack V2 design and was added after review):

  A. Duplicate Association -- the SAME crack was captured *in full* by two+
     overlapping originals (capture overlap is 70-80% by design, #3.4).
     Their projected polygons substantially overlap in facade space ->
     union-find merge by IoU (same STRtree/union-find pattern already used
     for tile-overlap merging in merge_tiles.py -- this is that same
     algorithm applied one level up, across source images instead of tiles).

  B. Crack Continuation Association -- a crack was split by a photo
     boundary with little/no overlap, so each original only shows one half.
     These do NOT overlap in facade space, so Duplicate Association can't
     catch them. Caught instead by looking at skeleton *endpoints*: if two
     un-merged fragments have endpoints that are close together (small gap)
     and whose local tangent directions both point toward each other
     (continuing the same line, not two unrelated cracks that happen to be
     near each other), they're joined into one crack.

Width/Length/Area are computed exactly once, on the FINAL merged geometry
per crack (never summed across per-original fragments, and never computed
before continuation-linking finishes) -- computing these per-original-
fragment first would double-count Duplicate overlaps and truncate
Continuation-split cracks to half their real length. Per-original width
observations are still kept (as SourceObservation provenance), just not
treated as the final value.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from src.common.config import Config
from src.common.types import Crack, SourceObservation
from src.crack.detector import CrackDetection, CrackDetector
from src.crack.measurement import ScaleInfo, to_mm, to_mm2
from src.crack.merge_tiles import CrackPolygon, merge_detections
from src.crack.skeleton import measure_polygon
from src.crack.tiler import Tile, tile_mosaic


@dataclass
class SourceCrackFragment:
    """One crack polygon, still tied to the single original photo it came
    from, already projected into facade/mosaic coordinates for
    association -- but polygon_px_in_source is kept too, since that's
    exactly what SourceObservation needs and there is no need to re-derive
    it via inverse-homography the way the mosaic-first path does."""

    image_id: str
    source_crack_id: str  # e.g. "DJI_0192_C000001" -- unique per source image
    polygon_px_in_source: np.ndarray  # (N, 2), that image's own pixel space
    polygon_px_facade: np.ndarray  # (N, 2), projected facade/mosaic space
    confidence: float


def project_to_facade(polygon_px: np.ndarray, H: np.ndarray) -> np.ndarray:
    """Forward: source(original) pixel -> facade/mosaic pixel. See module
    docstring for why this is the forward (not inverse) direction."""
    pts = polygon_px.astype(np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(pts, H).reshape(-1, 2)


def detect_cracks_in_original(
    image_id: str,
    image: np.ndarray,
    cfg: Config,
    detector: CrackDetector,
    illumination_device: str = "cuda",
) -> list[CrackPolygon]:
    """Runs the same tile/detect/merge path crack/pipeline.py uses on a
    mosaic, but on a single original DJI photo instead. An original photo is
    "fully observed" by definition (it's a direct camera capture, not a
    facade mosaic with occlusion-fusion gaps) so tile_mosaic's
    observed_mask is all-ones here -- no tile gets skipped for occlusion.

    Illumination correction (`cfg.illumination.mode` -- ORIGINAL/LIGHT/
    RETINEX/PHASR, 1차 마무리 2026-08-26) runs FIRST, once, on the whole
    original image, before tiling for detection -- never the other way
    around, since tiling first would mean each illumination backend sees
    only a fragment of the photo's own brightness context (this is exactly
    the mistake that made PhaSR's per-tile camera-intrinsics bug possible;
    illumination correction and crack-detection tiling are kept as two
    separate full-image-then-tile passes, not fused into one tiling pass).
    Correction never moves a pixel (#3.3), so polygon coordinates coming out
    of tile_mosaic/merge_detections below are still the original image's own
    pixel coordinates with zero extra transform needed.

    Returns polygons in the source image's own pixel space (0..width,
    0..height), crack_id-prefixed with image_id (merge_detections' facade_id
    param is just a namespace string, so passing image_id here gives ids
    like "DJI_0192_C000000" -- reads directly as "crack #0 in DJI_0192").
    """
    from src.illumination.dispatch import correct_illumination_any

    corrected = correct_illumination_any(image, str(cfg.illumination.mode), cfg, device=illumination_device)

    observed_mask = np.ones(corrected.shape[:2], dtype=np.uint8)
    tiles = tile_mosaic(image_id, corrected, observed_mask, cfg)
    tiles_by_id: dict[str, Tile] = {t.tile_id: t for t in tiles}

    detections: list[CrackDetection] = []
    for tile in tiles:
        detections.extend(detector.infer_tile(tile))

    return merge_detections(tiles_by_id, detections, facade_id=image_id)


def project_fragments(
    fragments_by_image: dict[str, list[CrackPolygon]],
    source_transforms: dict[str, dict],
) -> list[SourceCrackFragment]:
    """Projects every per-original CrackPolygon into facade space. Images
    with no saved transform (shouldn't happen for a facade that stitched
    successfully, but never assumed) are skipped, not fabricated."""
    out: list[SourceCrackFragment] = []
    for image_id, polygons in fragments_by_image.items():
        transform = source_transforms.get(image_id)
        if transform is None:
            continue
        H = np.asarray(transform["H"], dtype=np.float64)
        for poly in polygons:
            out.append(
                SourceCrackFragment(
                    image_id=image_id,
                    source_crack_id=poly.crack_id,
                    polygon_px_in_source=poly.polygon_px,
                    polygon_px_facade=project_to_facade(poly.polygon_px, H),
                    confidence=poly.confidence,
                )
            )
    return out


def _to_shapely(poly_px: np.ndarray) -> Polygon | None:
    if len(poly_px) < 3:
        return None
    poly = Polygon(poly_px)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly if (not poly.is_empty and poly.is_valid and poly.geom_type == "Polygon") else None


def associate_duplicates(
    fragments: list[SourceCrackFragment],
    iou_threshold: float,
    max_center_distance_px: float,
) -> list[list[int]]:
    """Union-find groups of fragment indices that are the SAME crack seen in
    full by >=2 overlapping originals. 1st-cut implementation: geometric
    overlap (IoU) or close centers only -- the design doc's own "2nd-tier"
    refinement (add direction/shape-similarity checks once real data shows
    over-merging of parallel-but-distinct cracks) is a documented extension
    point, not implemented here yet."""
    n = len(fragments)
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

    polys = [_to_shapely(f.polygon_px_facade) for f in fragments]
    centers = [p.centroid.coords[0] if p is not None else None for p in polys]
    valid_idx = [i for i, p in enumerate(polys) if p is not None]
    if not valid_idx:
        return [[i] for i in range(n)]

    tree = STRtree([polys[i] for i in valid_idx])
    for i in valid_idx:
        for local_j in tree.query(polys[i]):
            j = valid_idx[int(local_j)]
            if j <= i:
                continue
            # Don't associate two fragments from the SAME source image --
            # merge_detections already merged same-image tile overlaps.
            if fragments[i].image_id == fragments[j].image_id:
                continue
            inter = polys[i].intersection(polys[j]).area
            union_area = polys[i].union(polys[j]).area
            iou = inter / union_area if union_area > 0 else 0.0
            center_dist = float(np.hypot(centers[i][0] - centers[j][0], centers[i][1] - centers[j][1]))
            if iou >= iou_threshold or (inter > 0 and center_dist <= max_center_distance_px):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _skeleton_neighbors(skeleton_px: np.ndarray) -> dict[tuple[int, int], list[tuple[int, int]]]:
    """8-connectivity adjacency over skeleton pixels (same neighbor test
    skeleton.py's _skeleton_arc_length uses, just building an explicit
    graph here instead of only summing edge lengths)."""
    coords = {(int(x), int(y)) for x, y in skeleton_px}
    neighbors: dict[tuple[int, int], list[tuple[int, int]]] = {p: [] for p in coords}
    for x, y in coords:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            q = (x + dx, y + dy)
            if q in coords:
                neighbors[(x, y)].append(q)
    return neighbors


def _find_endpoints(skeleton_px: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """Returns the two extreme points of a (roughly linear, non-branching)
    skeleton polyline. Prefers true graph endpoints (pixels with exactly one
    8-connected neighbor); falls back to the two mutually-farthest points
    if the skeleton branches/loops (rare for a single crack, but never
    crashes on it -- just degrades to the farthest-pair approximation)."""
    if len(skeleton_px) < 2:
        return None
    neighbors = _skeleton_neighbors(skeleton_px)
    degree1 = [p for p, nbrs in neighbors.items() if len(nbrs) == 1]
    if len(degree1) == 2:
        return np.array(degree1[0], dtype=np.float64), np.array(degree1[1], dtype=np.float64)

    # Fallback: farthest pair by brute force (skeletons are thin, so N here
    # is bounded by crack length in px -- fine at the sizes this runs at).
    pts = skeleton_px.astype(np.float64)
    best_dist, best_pair = -1.0, (0, min(1, len(pts) - 1))
    for i in range(len(pts)):
        d = np.hypot(pts[:, 0] - pts[i, 0], pts[:, 1] - pts[i, 1])
        j = int(np.argmax(d))
        if d[j] > best_dist:
            best_dist, best_pair = float(d[j]), (i, j)
    return pts[best_pair[0]], pts[best_pair[1]]


def _tangent_at_endpoint(
    skeleton_px: np.ndarray, endpoint: np.ndarray, window_px: float
) -> np.ndarray:
    """Local direction at one endpoint, pointing OUTWARD (away from the rest
    of the skeleton) -- estimated from every skeleton point within
    window_px of the endpoint via a simple centroid-direction vector (not a
    full PCA fit -- the skeleton is already thin/1px, so the point spread
    near an endpoint is itself close to the tangent direction)."""
    d = np.hypot(skeleton_px[:, 0] - endpoint[0], skeleton_px[:, 1] - endpoint[1])
    nearby = skeleton_px[d <= window_px]
    if len(nearby) < 2:
        return np.array([0.0, 0.0])
    centroid = nearby.mean(axis=0)
    direction = endpoint - centroid  # outward: from the interior centroid toward the endpoint
    norm = np.hypot(direction[0], direction[1])
    return direction / norm if norm > 1e-6 else np.array([0.0, 0.0])


@dataclass
class _Fragment:
    """A duplicate-merged (or singleton) crack, in facade space, ready for
    Continuation Association."""

    polygon_px: np.ndarray
    source_indices: list[int]  # indices into the original SourceCrackFragment list
    skeleton_px: np.ndarray
    endpoints: tuple[np.ndarray, np.ndarray] | None


def _build_fragments(
    dup_groups: list[list[int]], fragments: list[SourceCrackFragment]
) -> list[_Fragment]:
    out: list[_Fragment] = []
    for group in dup_groups:
        polys = [_to_shapely(fragments[i].polygon_px_facade) for i in group]
        polys = [p for p in polys if p is not None]
        if not polys:
            continue
        merged = unary_union(polys)
        if merged.geom_type == "MultiPolygon":
            merged = max(merged.geoms, key=lambda g: g.area)
        if merged.geom_type != "Polygon" or merged.is_empty:
            continue
        polygon_px = np.array(merged.exterior.coords)
        measurement = measure_polygon(polygon_px)
        skeleton_px = measurement.skeleton_px if measurement is not None else np.empty((0, 2))
        endpoints = _find_endpoints(skeleton_px) if len(skeleton_px) >= 2 else None
        out.append(
            _Fragment(polygon_px=polygon_px, source_indices=group, skeleton_px=skeleton_px, endpoints=endpoints)
        )
    return out


def associate_continuations(
    dup_merged: list[_Fragment],
    max_gap_px: float,
    max_tangent_angle_deg: float,
    tangent_window_px: float,
) -> list[list[int]]:
    """Union-find over already-duplicate-merged fragments, joining pairs
    whose nearest endpoints are close (small gap) AND whose tangent
    directions both point toward each other (continuing one line, not two
    unrelated cracks that happen to pass near each other)."""
    n = len(dup_merged)
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

    max_angle_rad = np.radians(max_tangent_angle_deg)

    for i in range(n):
        frag_i = dup_merged[i]
        if frag_i.endpoints is None:
            continue
        for j in range(i + 1, n):
            frag_j = dup_merged[j]
            if frag_j.endpoints is None:
                continue

            best = None
            for ep_i in frag_i.endpoints:
                for ep_j in frag_j.endpoints:
                    gap = float(np.hypot(ep_i[0] - ep_j[0], ep_i[1] - ep_j[1]))
                    if best is None or gap < best[0]:
                        best = (gap, ep_i, ep_j)
            if best is None:
                continue
            gap, ep_i, ep_j = best
            if gap > max_gap_px or gap < 1e-6:
                continue

            tan_i = _tangent_at_endpoint(frag_i.skeleton_px, ep_i, tangent_window_px)
            tan_j = _tangent_at_endpoint(frag_j.skeleton_px, ep_j, tangent_window_px)
            if np.hypot(*tan_i) < 1e-6 or np.hypot(*tan_j) < 1e-6:
                continue

            connector = (ep_j - ep_i) / gap
            # Both tangents should point roughly along the connector (one
            # forward, one backward -- i.e. toward each other), continuing
            # a single line rather than meeting at an angle.
            angle_i = np.arccos(np.clip(np.dot(tan_i, connector), -1.0, 1.0))
            angle_j = np.arccos(np.clip(np.dot(tan_j, -connector), -1.0, 1.0))
            if angle_i <= max_angle_rad and angle_j <= max_angle_rad:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def build_final_cracks(
    fragments_by_image: dict[str, list[CrackPolygon]],
    source_transforms: dict[str, dict],
    facade_id: str,
    building_id: str,
    cfg: Config,
    scale: ScaleInfo,
) -> list[Crack]:
    """Orchestrates the full Phase-2 path: project every original's crack
    detections into facade space, run Duplicate Association, then Crack
    Continuation Association on what's left, then measure each FINAL
    merged geometry exactly once (never per-fragment -- see module
    docstring)."""
    mv_cfg = cfg.multiview

    fragments = project_fragments(fragments_by_image, source_transforms)
    if not fragments:
        return []

    dup_groups = associate_duplicates(
        fragments,
        iou_threshold=float(mv_cfg.duplicate_iou_threshold),
        max_center_distance_px=float(mv_cfg.duplicate_max_center_distance_px),
    )
    dup_merged = _build_fragments(dup_groups, fragments)

    cont_groups = associate_continuations(
        dup_merged,
        max_gap_px=float(mv_cfg.continuation_max_gap_px),
        max_tangent_angle_deg=float(mv_cfg.continuation_max_tangent_angle_deg),
        tangent_window_px=float(mv_cfg.continuation_tangent_window_px),
    )

    cracks: list[Crack] = []
    for k, group in enumerate(cont_groups):
        merged_frags = [dup_merged[i] for i in group]
        polys = [_to_shapely(f.polygon_px) for f in merged_frags]
        polys = [p for p in polys if p is not None]
        if not polys:
            continue
        # Continuation-linked fragments may not literally touch (that's the
        # whole point -- they were split by a photo boundary with little/no
        # overlap), so bridge them with a small buffer before union so the
        # merged shape is one connected polygon, not a MultiPolygon.
        gap_px = float(mv_cfg.continuation_max_gap_px)
        if len(polys) > 1:
            merged = unary_union([p.buffer(gap_px / 2.0) for p in polys]).buffer(-gap_px / 2.0)
        else:
            merged = polys[0]
        if merged.geom_type == "MultiPolygon":
            merged = max(merged.geoms, key=lambda g: g.area)
        if merged.geom_type != "Polygon" or merged.is_empty:
            continue

        polygon_px = np.array(merged.exterior.coords)
        measurement = measure_polygon(polygon_px)
        if measurement is None:
            continue

        source_indices = [i for f in merged_frags for i in f.source_indices]
        source_fragments = [fragments[i] for i in source_indices]
        confidence = max(f.confidence for f in source_fragments)

        source_observations = []
        for f in source_fragments:
            src_poly = _to_shapely(f.polygon_px_in_source)
            source_observations.append(
                SourceObservation(
                    image_id=f.image_id,
                    bbox_px_in_source=(
                        float(f.polygon_px_in_source[:, 0].min()),
                        float(f.polygon_px_in_source[:, 1].min()),
                        float(f.polygon_px_in_source[:, 0].max()),
                        float(f.polygon_px_in_source[:, 1].max()),
                    ),
                    polygon_px_in_source=f.polygon_px_in_source,
                    owned_pixel_count=int(round(src_poly.area)) if src_poly is not None else 0,
                )
            )
        source_observations.sort(key=lambda o: o.owned_pixel_count, reverse=True)
        source_image_ids = sorted({f.image_id for f in source_fragments})

        x0, y0 = polygon_px.min(axis=0)
        x1, y1 = polygon_px.max(axis=0)
        max_width_mm = to_mm(measurement.max_width_px, scale)
        severity = None
        if max_width_mm is not None:
            severity = (
                "정밀점검대상"
                if max_width_mm >= float(cfg.measurement.crack_width_threshold_mm)
                else "경미"
            )

        cracks.append(
            Crack(
                crack_id=f"{facade_id}_MV{k:06d}",
                building_id=building_id,
                facade_id=facade_id,
                bbox_px=(float(x0), float(y0), float(x1), float(y1)),
                polygon_px=polygon_px,
                skeleton_px=measurement.skeleton_px,
                length_px=measurement.length_px,
                max_width_px=measurement.max_width_px,
                mean_width_px=measurement.mean_width_px,
                area_px=float(merged.area),
                confidence=confidence,
                observation_state="OBSERVED",
                length_mm=to_mm(measurement.length_px, scale),
                max_width_mm=max_width_mm,
                area_mm2=to_mm2(float(merged.area), scale),
                severity=severity,
                source_tile_ids=[],
                source_image_ids=source_image_ids,
                source_observations=source_observations,
            )
        )
    return cracks
