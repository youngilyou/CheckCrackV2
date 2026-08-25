"""Building footprint & per-edge geometry (CLAUDE.local.md #4.2).

Footprint input priority per #4.2 is CAD/BIM > GIS footprint > manual
polygon > (future) point cloud. This loader reads a georeferenced UTM
polygon list — the "N_rings UTM" / "N_verts <attr>" / "x y" format this
project's pix4d post-processing exports use — which sits in the GIS tier.
No footprint is ever fabricated: callers must supply a real one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from shapely.geometry import Polygon
from shapely.geometry.polygon import orient


@dataclass
class FootprintEdge:
    ring_id: int
    edge_index: int
    start: tuple[float, float]  # (easting, northing) meters, UTM
    end: tuple[float, float]
    length_m: float
    tangent: tuple[float, float]  # unit vector, start -> end
    outward_normal: tuple[float, float]  # unit vector, points out of the polygon


@dataclass
class BuildingFootprint:
    utm_epsg: int
    rings: list[list[tuple[float, float]]]  # each a closed (first==last) CCW ring
    edges: list[FootprintEdge]


def _ring_edges(ring_id: int, ring: list[tuple[float, float]]) -> list[FootprintEdge]:
    pts = ring[:-1] if ring[0] == ring[-1] else ring
    edges = []
    n = len(pts)
    for i in range(n):
        start, end = pts[i], pts[(i + 1) % n]
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length < 1e-6:
            continue
        tangent = (dx / length, dy / length)
        # Ring is CCW (enforced below), so interior is on the left of travel
        # direction; rotating the tangent -90deg ((x,y) -> (y,-x)) points
        # away from the interior, i.e. outward.
        normal = (tangent[1], -tangent[0])
        edges.append(FootprintEdge(ring_id, i, start, end, length, tangent, normal))
    return edges


def load_footprint_utm_txt(path: str | Path, utm_epsg: int) -> BuildingFootprint:
    lines = [ln for ln in Path(path).read_text().strip().splitlines() if ln.strip()]
    header = lines[0].split()
    num_rings = int(header[0])
    if header[1] != "UTM":
        raise ValueError(f"unsupported footprint CRS tag: {header[1]!r} (expected UTM)")

    rings: list[list[tuple[float, float]]] = []
    idx = 1
    for _ in range(num_rings):
        n_verts = int(lines[idx].split()[0])
        idx += 1
        raw_ring = []
        for _ in range(n_verts):
            x, y = map(float, lines[idx].split())
            raw_ring.append((x, y))
            idx += 1
        ccw_ring = orient(Polygon(raw_ring), sign=1.0)
        rings.append(list(ccw_ring.exterior.coords))

    edges: list[FootprintEdge] = []
    for ring_id, ring in enumerate(rings):
        edges.extend(_ring_edges(ring_id, ring))

    return BuildingFootprint(utm_epsg=utm_epsg, rings=rings, edges=edges)
