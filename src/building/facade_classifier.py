"""Facade auto-classification (CLAUDE.local.md #6).

Assigns each image to one or more FacadeSegments from GPS position +
gimbal view direction + footprint geometry (#6: "Mission/Waypoint Hint +
Drone GPS + Building Footprint + Gimbal/Camera Direction"). This project
has no waypoint/mission database yet, so `waypoint_score` is a neutral
constant with a default weight of 0 in config — it is not fabricated as a
real signal, just present so the score formula in #6.3 matches the spec
and can pick up real waypoint data later.

Flow follows #6.1 -> #6.2 -> #6.3: generate nearby-segment candidates,
hard-filter by view angle (#6.2), then rank survivors by the weighted
score and threshold at `minimum_score` (#6.3). Below-threshold images are
UNASSIGNED rather than force-matched to the nearest segment (#6.1: "단순
nearest만으로 확정하지 않는다").
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pyproj

from src.building.facade_segmenter import FacadeSegment
from src.common.config import Config
from src.common.types import ImageMetadata


@dataclass
class FacadeAssignment:
    image_id: str
    facades: list[str] = field(default_factory=list)
    role: str = "UNASSIGNED"  # PRIMARY | CORNER_OVERLAP | UNASSIGNED
    scores: dict = field(default_factory=dict)  # facade_id -> combined score, diagnostics


def _point_to_segment(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    """(perpendicular distance to the line a->b, projection param t; t in [0,1] means within the segment)."""
    px, py = p
    ax, ay = a
    bx, by = b
    abx, aby = bx - ax, by - ay
    seg_len2 = abx * abx + aby * aby
    if seg_len2 < 1e-9:
        return math.hypot(px - ax, py - ay), 0.0
    t = ((px - ax) * abx + (py - ay) * aby) / seg_len2
    proj_x, proj_y = ax + t * abx, ay + t * aby
    return math.hypot(px - proj_x, py - proj_y), t


def _bearing_to_vector(bearing_deg: float) -> tuple[float, float]:
    rad = math.radians(bearing_deg)
    return (math.sin(rad), math.cos(rad))  # (east, north) — matches footprint's normal-bearing convention


def classify_images(
    catalog: list[ImageMetadata],
    segments: list[FacadeSegment],
    cfg: Config,
    utm_epsg: int,
) -> list[FacadeAssignment]:
    acfg = cfg.facade_assignment
    max_view_angle = float(acfg.max_camera_to_target_angle_deg)
    corner_angle = float(acfg.corner_duplicate_angle_deg)
    min_score = float(acfg.minimum_score)
    w_position = float(acfg.w_position)
    w_view = float(acfg.w_view)
    w_waypoint = float(acfg.w_waypoint)
    w_distance = float(acfg.w_distance)
    ideal_standoff = float(acfg.ideal_standoff_m)
    max_standoff = float(acfg.max_standoff_m)

    transformer = pyproj.Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)

    results: list[FacadeAssignment] = []
    for meta in catalog:
        if meta.gps.latitude is None or meta.gps.longitude is None or meta.gimbal_pose.yaw_deg is None:
            results.append(FacadeAssignment(image_id=meta.image_id))
            continue

        x, y = transformer.transform(meta.gps.longitude, meta.gps.latitude)
        camera_dir = _bearing_to_vector(meta.gimbal_pose.yaw_deg)

        candidates: list[tuple[str, float, float]] = []  # (facade_id, score, view_angle_deg)
        for seg in segments:
            perp, t = _point_to_segment((x, y), seg.start, seg.end)
            if perp > max_standoff:
                continue

            # #6.2 View Validation: camera should look roughly opposite the outward normal.
            target_dir = (-seg.outward_normal[0], -seg.outward_normal[1])
            cos_angle = max(-1.0, min(1.0, camera_dir[0] * target_dir[0] + camera_dir[1] * target_dir[1]))
            view_angle = math.degrees(math.acos(cos_angle))
            if view_angle > max_view_angle:
                continue

            overshoot = max(0.0, -t, t - 1.0)  # 0 while the drone is abeam the segment's own span
            position_score = max(0.0, 1.0 - overshoot)
            view_score = max(0.0, cos_angle)
            distance_score = max(0.0, 1.0 - abs(perp - ideal_standoff) / max(ideal_standoff, 1e-6))
            waypoint_score = 0.5  # neutral placeholder, see module docstring

            score = (
                w_position * position_score
                + w_view * view_score
                + w_waypoint * waypoint_score
                + w_distance * distance_score
            )
            candidates.append((seg.facade_id, score, view_angle))

        candidates.sort(key=lambda c: -c[1])
        scores = {fid: s for fid, s, _ in candidates}

        if not candidates or candidates[0][1] < min_score:
            results.append(FacadeAssignment(image_id=meta.image_id, scores=scores))
            continue

        best_id, _, _ = candidates[0]
        assigned = [best_id]
        role = "PRIMARY"
        if len(candidates) > 1:
            second_id, second_score, second_view_angle = candidates[1]
            # #6.4 Corner image: don't force a single facade when a second
            # segment is nearly as good a view — keep both as a DB relation.
            if second_score >= min_score and second_view_angle <= corner_angle:
                assigned.append(second_id)
                role = "CORNER_OVERLAP"

        results.append(FacadeAssignment(image_id=meta.image_id, facades=assigned, role=role, scores=scores))

    return results
