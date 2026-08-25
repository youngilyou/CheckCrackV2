"""Facade segment construction from a building footprint (CLAUDE.local.md #4.3).

Each footprint edge is a candidate Facade segment; adjacent same-ring edges
whose outward-normal angle differs by less than `merge_if_normal_angle_delta_deg`
are merged into one (a config knob per #4.3, not a fixed rule — this project
never treats N/E/S/W as the real segmentation unit, #4.1). Segments shorter
than `min_segment_length_m` are dropped rather than force-kept as noise.
`direction_group` is metadata only, never the Facade id.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.building.footprint import BuildingFootprint, FootprintEdge
from src.common.config import Config


@dataclass
class FacadeSegment:
    facade_id: str
    building_id: str
    ring_id: int
    start: tuple[float, float]
    end: tuple[float, float]
    length_m: float
    tangent: tuple[float, float]
    outward_normal: tuple[float, float]
    direction_group: str


def _normal_bearing_deg(normal: tuple[float, float]) -> float:
    """Compass bearing (0=North/+y, clockwise positive) of the outward normal — same
    convention as DJI's GimbalYawDegree, so it's directly comparable to camera heading."""
    return math.degrees(math.atan2(normal[0], normal[1])) % 360.0


def _direction_group(bearing_deg: float) -> str:
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = int(((bearing_deg + 22.5) % 360) / 45)
    return dirs[idx]


def _angle_delta(a_deg: float, b_deg: float) -> float:
    return abs((a_deg - b_deg + 180) % 360 - 180)


def _merge_adjacent(edges: list[FootprintEdge], merge_deg: float) -> list[FootprintEdge]:
    if not edges:
        return edges
    merged: list[FootprintEdge] = []
    current = edges[0]
    for nxt in edges[1:]:
        delta = _angle_delta(_normal_bearing_deg(current.outward_normal), _normal_bearing_deg(nxt.outward_normal))
        if delta <= merge_deg:
            dx, dy = nxt.end[0] - current.start[0], nxt.end[1] - current.start[1]
            length = math.hypot(dx, dy)
            tangent = (dx / length, dy / length) if length > 1e-6 else current.tangent
            current = FootprintEdge(
                current.ring_id, current.edge_index, current.start, nxt.end, length, tangent, current.outward_normal
            )
        else:
            merged.append(current)
            current = nxt
    merged.append(current)
    return merged


def build_segments(footprint: BuildingFootprint, building_id: str, cfg: Config) -> list[FacadeSegment]:
    scfg = cfg.facade_segmentation
    merge_deg = float(scfg.merge_if_normal_angle_delta_deg)
    min_len = float(scfg.min_segment_length_m)

    segments: list[FacadeSegment] = []
    counter = 1
    ring_ids = sorted({e.ring_id for e in footprint.edges})
    for ring_id in ring_ids:
        ring_edges = [e for e in footprint.edges if e.ring_id == ring_id]
        for e in _merge_adjacent(ring_edges, merge_deg):
            if e.length_m < min_len:
                continue
            bearing = _normal_bearing_deg(e.outward_normal)
            segments.append(
                FacadeSegment(
                    facade_id=f"F{counter:03d}",
                    building_id=building_id,
                    ring_id=ring_id,
                    start=e.start,
                    end=e.end,
                    length_m=e.length_m,
                    tangent=e.tangent,
                    outward_normal=e.outward_normal,
                    direction_group=_direction_group(bearing),
                )
            )
            counter += 1
    return segments
