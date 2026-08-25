"""Tile -> facade-global coordinates + duplicate merge across overlaps (CLAUDE.local.md #23/#24).

Tiles overlap on purpose (tiler.py) so a crack near a tile boundary is
whole in at least one tile — which means the same real crack is often
detected twice, once per tile. Merge by geometric overlap (IoU on the
restored global polygons), not by tile adjacency bookkeeping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from src.crack.detector import CrackDetection
from src.crack.tiler import Tile


@dataclass
class CrackPolygon:
    crack_id: str
    facade_id: str
    polygon_px: np.ndarray  # (N, 2) facade-global pixel coords
    confidence: float  # max over merged source detections
    area_px: float = 0.0  # shapely-computed polygon area, px^2
    source_tile_ids: list = field(default_factory=list)


def restore_global_coords(tile: Tile, detection: CrackDetection) -> np.ndarray:
    """#23: X = tile_origin_x + x_tile."""
    return detection.polygon_tile_px + np.array([tile.x0, tile.y0], dtype=np.float64)


def _to_shapely(poly_px: np.ndarray) -> Polygon | None:
    if len(poly_px) < 3:
        return None
    poly = Polygon(poly_px)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly if (not poly.is_empty and poly.is_valid and poly.geom_type == "Polygon") else None


def match_crack_ids(
    merged_polygons: list[CrackPolygon],
    previous_cracks: list[dict] | None,
    iou_threshold: float = 0.3,
) -> None:
    """Reassigns crack_id on each CrackPolygon **in place** by greedy IoU match
    against a previous run's {facade_id}_cracks.json entries, so a crack that's
    still there keeps its ID across a re-run of crack detection instead of
    getting a new one every time (CLAUDE.local.md item D's "실패 시 버전을
    늘리지 않는다" spirit, extended to "재실행해도 정체성을 잃지 않는다").

    SAFE ONLY WITHIN THE SAME STITCH VERSION. previous_cracks' polygon_px is
    in the pixel space of whatever analysis.tif produced it; a re-stitch
    produces a new mosaic with no guaranteed pixel alignment to the old one
    (this codebase has no facade-local coordinate system reachable from the
    stitch_folder.py/run_facade_poc path used here). Comparing polygons
    across a re-stitch via IoU would risk silently matching coincidental
    pixel overlap as "the same crack" — a fabrication CLAUDE.local.md #6/#9
    rules out. The caller (tools/detect_cracks_folder.py) must only pass
    previous_cracks loaded from the SAME output_dir it is about to write
    into, never from a different stitch version.

    Greedy nearest-match, not a globally optimal assignment (no scipy/
    Hungarian-algorithm dependency) — acceptable at the tens-of-cracks-per-
    facade scale this runs at.
    """
    if not previous_cracks:
        return  # merge_detections already assigned fresh k:06d ids above

    prev_items: list[tuple[str, Polygon]] = []
    max_suffix = -1
    for c in previous_cracks:
        poly = _to_shapely(np.asarray(c["polygon_px"], dtype=np.float64))
        if poly is not None:
            prev_items.append((c["crack_id"], poly))
        m = re.search(r"_C(\d+)$", c["crack_id"])
        if m:
            max_suffix = max(max_suffix, int(m.group(1)))

    used_prev: set[int] = set()
    next_id = max_suffix + 1
    for cp in merged_polygons:
        new_poly = _to_shapely(cp.polygon_px)
        best_iou, best_j = 0.0, -1
        if new_poly is not None:
            for j, (_, prev_poly) in enumerate(prev_items):
                if j in used_prev:
                    continue
                inter = new_poly.intersection(prev_poly).area
                if inter == 0:
                    continue
                union_area = new_poly.union(prev_poly).area
                iou = inter / union_area if union_area > 0 else 0.0
                if iou > best_iou:
                    best_iou, best_j = iou, j
        if best_j >= 0 and best_iou >= iou_threshold:
            cp.crack_id = prev_items[best_j][0]
            used_prev.add(best_j)
        else:
            cp.crack_id = f"{cp.facade_id}_C{next_id:06d}"
            next_id += 1


def merge_detections(
    tiles_by_id: dict[str, Tile],
    detections: list[CrackDetection],
    facade_id: str,
    iou_threshold: float = 0.2,
) -> list[CrackPolygon]:
    items: list[tuple[Polygon, float, str]] = []
    for det in detections:
        poly = _to_shapely(restore_global_coords(tiles_by_id[det.tile_id], det))
        if poly is not None:
            items.append((poly, det.confidence, det.tile_id))
    if not items:
        return []

    # Union-find over polygons whose overlap ratio clears iou_threshold —
    # groups multi-tile fragments of the same crack without assuming any
    # particular tile adjacency.
    n = len(items)
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

    tree = STRtree([it[0] for it in items])
    for i, (poly, _, _) in enumerate(items):
        for j in tree.query(poly):
            j = int(j)
            if j <= i:
                continue
            other = items[j][0]
            inter = poly.intersection(other).area
            if inter == 0:
                continue
            union_area = poly.union(other).area
            if union_area > 0 and inter / union_area >= iou_threshold:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    merged: list[CrackPolygon] = []
    for k, idxs in enumerate(groups.values()):
        polys = [items[i][0] for i in idxs]
        confs = [items[i][1] for i in idxs]
        tile_ids = sorted({items[i][2] for i in idxs})
        merged_geom = unary_union(polys)
        if merged_geom.geom_type == "MultiPolygon":
            merged_geom = max(merged_geom.geoms, key=lambda g: g.area)
        merged.append(
            CrackPolygon(
                crack_id=f"{facade_id}_C{k:06d}",
                facade_id=facade_id,
                polygon_px=np.array(merged_geom.exterior.coords),
                confidence=max(confs),
                area_px=float(merged_geom.area),
                source_tile_ids=tile_ids,
            )
        )
    return merged
