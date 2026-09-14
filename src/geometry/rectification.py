"""Facade-plane rectification from calibrated camera poses (CLAUDE.local.md #13).

The pairwise-homography chain (stitching/graph.py + warp.py) has no way to
enforce *global* consistency — each pair only agrees locally, so alignment
error accumulates hop by hop (global_drift_score, graph.py). That's exactly
what triggers the COLMAP fallback (#12). This module is the other half of
that fallback: once COLMAP has recovered real per-image camera poses and
calibrated intrinsics, every registered image's pixel->facade-plane mapping
is a single closed-form homography computed straight from geometry — there
is no chain to drift, because each image is placed independently against
the *plane*, not against its neighbors.

The facade plane itself is exactly #13's local coordinate frame: origin,
u-axis (horizontal, = the footprint edge's tangent), v-axis (vertical), and
implicitly the outward normal — all taken from the footprint segment
(building/facade_segmenter.py), not re-derived from the SfM point cloud.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pycolmap
import pyproj

from src.building.facade_segmenter import FacadeSegment
from src.common.imageio import imread_unicode
from src.common.types import ImageMetadata, StitchQualityReport
from src.stitching.blend import blend_analysis, blend_visual, build_owner_map, compute_seam_masks
from src.stitching.mosaic import MosaicResult, paste_max
from src.stitching.warp import SourceTransform, WarpedImage


@dataclass
class FacadePlane:
    origin: np.ndarray  # (3,) UTM (x, y, z) meters — the facade-local (0,0)
    e_u: np.ndarray  # (3,) unit vector, horizontal along the facade (footprint tangent)
    e_v: np.ndarray  # (3,) unit vector, "down" in the rendered canvas (0,0,-1) — see facade_plane_from_segment
    px_per_m: float
    width_m: float  # canvas u-extent — fixed to the real facade span, not auto-derived
    height_m: float  # canvas v-extent

    @property
    def normal(self) -> np.ndarray:
        """Outward unit normal, right-handed from (e_u, e_v) -- used by
        build_plane_offset_grid/lookup_offset to measure how far a real
        COLMAP-triangulated point sits off the idealized flat plane."""
        n = np.cross(self.e_u, self.e_v)
        return n / np.linalg.norm(n)


def facade_plane_from_segment(
    segment: FacadeSegment,
    reference_altitudes_m: list[float],
    px_per_m: float = 100.0,
    height_m: float = 40.0,
) -> FacadePlane:
    """`px_per_m=100` (1cm/px) and `height_m=40` are PoC starting points tuned
    for these ~15-50m-standoff DJI photos and this mid-rise building, not
    universal constants — revisit with real facade-inspection GSDs and (once
    available) actual ground elevation instead of the camera-altitude proxy
    used here for the v=0 reference.

    The canvas is deliberately bounded to the segment's real length and a
    generous height, rather than auto-derived from where every image's rays
    hit the infinite plane: an oblique photo also sees sky, ground and
    neighboring surfaces, none of which are on this plane, and projecting
    them onto it anyway sends their canvas position to extreme/unbounded
    coordinates.
    """
    z0 = (min(reference_altitudes_m) - height_m / 2) if reference_altitudes_m else 0.0
    origin = np.array([segment.start[0], segment.start[1], z0])
    e_u = np.array([segment.tangent[0], segment.tangent[1], 0.0])
    e_u = e_u / np.linalg.norm(e_u)
    e_v = np.array([0.0, 0.0, -1.0])  # world +z (up) -> canvas -v (toward row 0), so "up" renders up
    return FacadePlane(
        origin=origin, e_u=e_u, e_v=e_v, px_per_m=px_per_m, width_m=segment.length_m, height_m=height_m
    )


def estimate_utm_epsg(catalog: list[ImageMetadata]) -> int | None:
    """Standard 6-degree WGS84 UTM zone from the average GPS position of a
    set of images. Phase 2/3 (run_building_poc) always has an operator-
    supplied `utm_epsg` alongside the footprint file; Phase 1 (run_facade_poc)
    has neither a footprint nor an EPSG, so this derives one from the only
    location data that already exists -- each image's own EXIF GPS -- rather
    than requiring the operator to look up a zone number by hand. Returns
    None (never a guessed zone) if no image in the catalog has GPS at all."""
    lats = [m.gps.latitude for m in catalog if m.gps.latitude is not None]
    lons = [m.gps.longitude for m in catalog if m.gps.longitude is not None]
    if not lats or not lons:
        return None
    lat, lon = sum(lats) / len(lats), sum(lons) / len(lons)
    zone = int((lon + 180) / 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def _camera_center(img: "pycolmap.Image") -> np.ndarray:
    """World-frame camera center from cam_from_world (X_cam = R@X_world + t
    means the camera sits at R.T @ (-t) in world coordinates) -- same R/t
    extraction rectify_images already uses below."""
    pose = img.cam_from_world()
    R = pose.rotation.matrix()
    t = np.asarray(pose.translation)
    return R.T @ (-t)


def _camera_forward(img: "pycolmap.Image") -> np.ndarray:
    """World-frame camera VIEWING direction (unit vector) -- COLMAP's camera
    convention looks down its own +Z axis, so (mirroring _camera_center's own
    R.T-based inverse) the world-frame forward direction is R.T @ [0,0,1]."""
    pose = img.cam_from_world()
    R = pose.rotation.matrix()
    forward = R.T @ np.array([0.0, 0.0, 1.0])
    return forward / np.linalg.norm(forward)


def _principal_direction(points_3d: np.ndarray, k: float = 3.0, max_iters: int = 5) -> np.ndarray:
    """Largest-variance direction through a point set (e.g. a flight's
    camera centers) via iterative trimmed SVD -- the "track"
    facade_plane_from_reconstruction aligns its u-axis to.

    A plain single-shot SVD is not robust to outlier centers: a handful of
    cameras with badly-estimated poses (COLMAP's registration/bundle
    adjustment is known non-deterministic run-to-run, #10-ish real failures
    seen 2026-09-10/11) can skew the axis itself, not just the extent along
    it -- and `_robust_range` downstream only cleans up the *projection*
    onto whatever axis it's given, so a skewed axis still produces a skewed
    (and much too large) projected extent. Confirmed real, 2026-09-11: on
    one actual 150-image FRONT reconstruction, the single-shot axis gave a
    robust-range u-extent of ~210m against a physically-plausible ~60m --
    same reconstruction, same `_robust_range`, only the axis differed.

    Mirrors `_robust_range`'s own IQR philosophy (k=3.0, "extreme outlier"
    boxplot threshold) instead of introducing a different robust-fit method:
    fit an axis, project every original point onto it, drop the ones outside
    the IQR range, refit on the survivors, and repeat until the inlier set
    stops shrinking (or `max_iters` is reached)."""
    subset = points_3d
    axis = None
    for _ in range(max_iters):
        mean = subset.mean(axis=0)
        _, _, vt = np.linalg.svd(subset - mean)
        axis = vt[0]
        proj = (points_3d - mean) @ axis
        lo, hi = _robust_range(proj, k=k)
        inliers = points_3d[(proj >= lo) & (proj <= hi)]
        if inliers.shape[0] == subset.shape[0] or inliers.shape[0] < 2:
            break
        subset = inliers
    return axis


def _robust_range(values: np.ndarray, k: float = 3.0) -> tuple[float, float]:
    """IQR-based (min, max) -- excludes points more than `k` interquartile-ranges
    past the nearest quartile before taking the extreme values, so a handful of
    badly-triangulated COLMAP points (a mismatched feature triangulated far in
    front of/behind the real surface) can't blow up the fitted canvas size the
    way raw .min()/.max() would (confirmed real failure, 2026-09-10: a 150-image
    facade's plane came out 882m x 860m -- physically impossible for one
    building -- and the resulting canvas OOM'd trying to allocate ~63GB;
    re-confirmed 2026-09-10/11 with a fresh COLMAP run of the same 150 images
    under the exact CheckCrackViewer procedure, no shortcuts -- the crash
    reproduces cleanly on the *original* (pre-fix) code every time).
    k=3.0 is the standard "extreme outlier" boxplot threshold (vs 1.5 for a
    plain "outlier") -- deliberately conservative, since clipping real facade
    content is worse than leaving a few meters of genuine outlier margin."""
    q1, q3 = np.percentile(values, [25, 75])
    iqr = q3 - q1
    if iqr <= 1e-9:
        return float(values.min()), float(values.max())
    lo, hi = q1 - k * iqr, q3 + k * iqr
    inliers = values[(values >= lo) & (values <= hi)]
    if inliers.size == 0:
        return float(values.min()), float(values.max())
    return float(inliers.min()), float(inliers.max())


def _filter_points_near_plane(
    points: np.ndarray, centroid: np.ndarray, normal: np.ndarray, k: float = 3.0, max_iters: int = 3
) -> tuple[np.ndarray, np.ndarray]:
    """Drop points whose distance from the (u,v)-plane along `normal` puts
    them nowhere near the actual photographed surface, refitting the centroid
    from the survivors each pass -- the dominant, previously-undiagnosed
    contamination source behind the 2026-09-10/11 "canvas way too big"
    failures on this exact facade, confirmed real 2026-09-11 by direct
    inspection of one actual 150-image FRONT reconstruction: an outdoor UE
    capture also has sky/distant terrain in the background of most oblique
    shots, and COLMAP incidentally triangulates some of *that* too. Since
    it's optically at a wildly different depth than the ~30-40m facade
    standoff, those points land from -150m to over -1500m off the true
    facade plane (signed distance from the raw whole-cloud centroid) --
    while 88% of all points sat within a ~1.5m-wide band around one constant
    offset (the real surface). The raw centroid (a straight mean over both
    clusters) sits nowhere near that real cluster, and every extent computed
    from it inherits the corruption: on that reconstruction this filter took
    the canvas from 210m x 107m down to 56m x 37m, matching the known-good
    (different, 140-image) run's ~60m facade width. `_robust_range` alone
    (applied only to the final u/v projections) does not fix this -- the
    background points are numerous and spread widely enough that a k=3.0 IQR
    on u/v still keeps a large fraction of them; filtering by plane distance
    directly is what actually separates the two clusters, because that's the
    dimension they differ in by orders of magnitude."""
    subset = points
    centroid_est = centroid
    for _ in range(max_iters):
        dist = (points - centroid_est) @ normal
        lo, hi = _robust_range(dist, k=k)
        inliers = points[(dist >= lo) & (dist <= hi)]
        if inliers.shape[0] == subset.shape[0] or inliers.shape[0] < 10:
            break
        subset = inliers
        centroid_est = subset.mean(axis=0)
    return subset, centroid_est


def facade_plane_from_reconstruction(
    reconstruction: pycolmap.Reconstruction,
    px_per_m: float = 100.0,
    padding_m: float = 2.0,
) -> FacadePlane:
    """Footprint-free counterpart to facade_plane_from_segment, for a Phase 1
    run_facade_poc call that has no footprint file to take #13's local
    coordinate frame from at all. Fits the plane straight from COLMAP's own
    triangulated 3D points (already bundle-adjusted, representing the actual
    photographed surface) via SVD -- the least-variance direction is the
    plane normal. Only meaningful once `reconstruction` has already been
    aligned to real, metric, gravity-aligned UTM+altitude coordinates via
    align_reconstruction_to_utm; this fit is purely geometric and has no
    other source of scale or orientation.

    Orientation: if the fitted normal is mostly horizontal, this is the
    common facade case (#0/#4: one Facade Segment is one roughly-planar
    wall) -- e_v is forced to true world-up (matching
    facade_plane_from_segment's own convention exactly) rather than trusting
    SVD's second axis, which has no reason to already be vertical. Either
    way, e_u follows the flight track's own principal direction (PCA on the
    registered camera centers, projected onto the plane) rather than the
    point cloud's own PCA axis -- a drone pass flies roughly parallel to
    whatever it's photographing (a wall's edge, or a roof's long side), so
    the track is a far more natural "horizontal" than an axis derived purely
    from the (capture-direction-agnostic) point cloud shape. Skipping this
    for the rooftop/plan-view case specifically was an earlier bug: using
    the point cloud's raw PCA axis there produced a canvas rotated at a
    fairly arbitrary angle, with no relation to the building or the flight
    (caught by a user screenshot showing an oddly-tilted rectified mosaic).

    This is exactly the case that motivated adding this function at all: a
    facade run whose image set spans a rooftop nadir pass plus a few oblique
    shots of the facade edge below it produced COLMAP camera poses that are
    individually correct, but forcing all of them through one 2D homography
    *chain* (stitching/graph.py) tore the mosaic across the roof/wall
    boundary. Placing each image independently against a real 3D-fitted
    plane instead has no chain to drift.

    Two robustness fixes on top of the original point-cloud-SVD design
    (neither changes it for the common case), both from real 2026-09-10/11
    failures on this exact facade, re-confirmed via the unmodified CheckCrackViewer
    procedure with no shortcuts before being reapplied here: the canvas EXTENT
    (width_m/height_m/origin, previously raw min()/max() over u/v) is now
    outlier-rejected via `_robust_range` (a canvas-allocation OOM crash); and
    the wall-vs-roof orientation decision now prefers a camera-viewing-direction
    estimate over the point cloud's own SVD normal when enough oblique shots
    exist to compute one (the point cloud can be numerically dominated by a
    densely-triangulated roof even when most photos actually face the wall --
    see the normal-override comment below).
    """
    points = np.array([p.xyz for p in reconstruction.points3D.values()])
    if points.shape[0] < 10:
        raise ValueError(f"too few triangulated points ({points.shape[0]}) to fit a facade plane")

    centroid = points.mean(axis=0)
    _, _, vt = np.linalg.svd(points - centroid)
    normal = vt[2]

    # Camera-viewing-direction override for the wall-vs-roof decision below
    # (confirmed real failure, 2026-09-10, FRONT facade): the point cloud's
    # own SVD normal can come out near-vertical -- misclassifying an actual
    # wall capture as "rooftop" -- whenever nadir/rooftop images happen to
    # triangulate far more points than the oblique wall-facing shots (a large
    # flat roof triangulates densely; a few oblique facade shots triangulate
    # comparatively sparsely). That point-COUNT imbalance has nothing to do
    # with what the operator actually pointed the camera at. Each registered
    # image's own pose, by contrast, is independently and precisely recovered
    # by COLMAP with no such count bias, so classifying cameras by how
    # steeply they look down and averaging the oblique ones' viewing
    # direction is a far more faithful read of intent than the point cloud's
    # own (population-skewed) shape. Only overrides when there's an actual
    # mix (>=4 oblique shots) to detect -- an all-nadir or all-oblique set
    # already gets the right answer from the point-cloud SVD alone.
    forwards = np.array([_camera_forward(img) for img in reconstruction.images.values()])
    is_oblique = forwards[:, 2] > -np.cos(np.deg2rad(45))  # >45 deg off straight-down
    oblique_count = int(is_oblique.sum())
    if oblique_count >= 4:
        camera_normal = -forwards[is_oblique].mean(axis=0)
        norm_len = np.linalg.norm(camera_normal)
        if norm_len > 1e-6:
            normal = camera_normal / norm_len

    # Drop background/terrain points that got incidentally triangulated along
    # with the real facade surface (see _filter_points_near_plane) -- must
    # happen before centroid/extent are computed from `points`, since those
    # background points are exactly what corrupts both.
    points, centroid = _filter_points_near_plane(points, centroid, normal)

    centers = np.array([_camera_center(img) for img in reconstruction.images.values()])
    track = _principal_direction(centers) if centers.shape[0] >= 2 else vt[0]
    e_u = track - np.dot(track, normal) * normal
    if np.linalg.norm(e_u) < 1e-6:
        e_u = vt[0] - np.dot(vt[0], normal) * normal
    e_u = e_u / np.linalg.norm(e_u)

    # Fix e_u's sign to a deterministic, run-independent convention -- SVD
    # (both `_principal_direction`'s and the vt[0] fallback) fits a LINE, not
    # a directed vector, so its sign is essentially arbitrary numerical noise
    # that can flip between two runs of the *same* images (COLMAP's own
    # registration is already known non-deterministic run-to-run). Confirmed
    # real, 2026-09-12 (BACK facade): two runs a few minutes apart rendered
    # the identical wall mirrored left-right with no code change at all.
    # DJI filenames increment sequentially in real capture order regardless
    # of which images later get registered/excluded (by COLMAP itself or
    # _detect_off_wall_images), so orienting e_u to agree with "capture order
    # increases toward +u" is a stable, deterministic tiebreak that doesn't
    # depend on COLMAP's internal SVD numerics -- covariance sign between
    # capture order and each camera's own projection onto the (unsigned)
    # track, not just first-vs-last (robust to a couple of out-of-sequence
    # registrations). Best-effort: a flight path that reverses direction
    # mid-facade isn't monotonic in the first place, but that's already an
    # unusual capture pattern this convention has no worse an answer for than
    # the previous (literally random) one did.
    image_ids = [Path(img.name).stem for img in reconstruction.images.values()]
    if len(image_ids) >= 2:
        order = np.argsort(image_ids)  # capture order, stable across runs/exclusions
        capture_index = np.empty(len(image_ids))
        capture_index[order] = np.arange(len(image_ids))
        projection = centers @ e_u
        covariance = float(np.dot(capture_index - capture_index.mean(), projection - projection.mean()))
        if covariance < 0:
            e_u = -e_u

    world_up = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(normal, world_up))) < 0.5:
        # vertical wall -- v is true world-up (matches facade_plane_from_segment's
        # own convention exactly), so "up" always renders up regardless of which
        # way the flight track happened to point.
        e_v = np.array([0.0, 0.0, -1.0])
    else:
        # rooftop/plan-view -- no natural "up" to anchor to, so v is whatever
        # stays perpendicular to the track-aligned u within the plane.
        e_v = np.cross(normal, e_u)
        e_v = e_v / np.linalg.norm(e_v)

    u = (points - centroid) @ e_u
    v = (points - centroid) @ e_v
    u_min, u_max = _robust_range(u)
    v_min, v_max = _robust_range(v)
    width_m = (u_max - u_min) + 2 * padding_m
    height_m = (v_max - v_min) + 2 * padding_m
    origin = centroid + e_u * (u_min - padding_m) + e_v * (v_min - padding_m)

    return FacadePlane(origin=origin, e_u=e_u, e_v=e_v, px_per_m=px_per_m, width_m=width_m, height_m=height_m)


def align_reconstruction_to_utm(
    reconstruction: pycolmap.Reconstruction,
    by_id: dict[str, ImageMetadata],
    utm_epsg: int,
    min_common_images: int = 3,
) -> bool:
    """Align COLMAP's arbitrary-frame reconstruction to real-world UTM+altitude
    meters using each registered image's own GPS as a location prior.
    Mutates `reconstruction` in place. Returns False (reconstruction is left
    untouched) if there isn't enough GPS coverage or alignment fails — never
    proceeds with an unaligned/unscaled reconstruction, since every downstream
    plane-projection distance would silently be wrong.
    """
    transformer = pyproj.Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    names: list[str] = []
    locations: list[list[float]] = []
    for img in reconstruction.images.values():
        meta = by_id.get(Path(img.name).stem)
        if meta is None or meta.gps.latitude is None or meta.gps.altitude_m is None:
            continue
        x, y = transformer.transform(meta.gps.longitude, meta.gps.latitude)
        names.append(img.name)
        locations.append([x, y, meta.gps.altitude_m])

    if len(names) < min_common_images:
        return False

    sim3d = pycolmap.align_reconstruction_to_locations(
        reconstruction, names, np.array(locations), min_common_images, pycolmap.RANSACOptions()
    )
    if sim3d is None:
        return False
    reconstruction.transform(sim3d)
    return True


def _camera_to_facade_homography(K: np.ndarray, R: np.ndarray, t: np.ndarray, plane: FacadePlane) -> np.ndarray:
    """Closed-form undistorted-image-pixel -> facade-plane-pixel homography.

    For X_world = origin + u*e_u + v*e_v (the plane, parameterized in
    facade-canvas pixels), a calibrated pinhole camera images it as
    K @ (R @ X_world + t). Collecting the u, v and constant terms into
    columns gives the facade->image homography directly; we want the
    inverse direction for warpPerspective(src=image, ..., dst=facade canvas).
    """
    e_u_px = plane.e_u / plane.px_per_m
    e_v_px = plane.e_v / plane.px_per_m
    col_u = K @ (R @ e_u_px)
    col_v = K @ (R @ e_v_px)
    col_o = K @ (R @ plane.origin + t)
    facade_to_image = np.column_stack([col_u, col_v, col_o])
    return np.linalg.inv(facade_to_image)


@dataclass
class PlaneOffsetGrid:
    """Coarse canvas-space grid of how far the REAL photographed surface sits
    off the idealized flat `FacadePlane`, in meters along the plane normal --
    built directly from COLMAP's own triangulated 3D points (already
    bundle-adjusted), not guessed. `grid` only has entries for cells that had
    enough real near-plane points to trust (see build_plane_offset_grid) --
    a missing cell is not "zero offset", it's "no measurement here", and
    lookup_offset treats the two differently (a missing cell contributes
    nothing to the interpolation rather than silently voting for zero)."""

    cell_px: int
    grid: dict[tuple[int, int], float]  # (cell_x, cell_y) -> median signed offset (m)


def build_plane_offset_grid(
    reconstruction: pycolmap.Reconstruction,
    plane: FacadePlane,
    cell_px: int = 200,
    min_points_per_cell: int = 5,
    max_offset_m: float = 3.0,
    smoothing_radius_m: float = 12.0,
    min_coverage_fraction: float = 0.15,
    clamp_offset_m: float = 0.6,
) -> PlaneOffsetGrid:
    """Bins every triangulated 3D point by which canvas cell its (u, v)
    projection onto the plane falls into, takes the MEDIAN signed distance
    along the plane normal per cell (median, not mean, so a minority of
    stray/mismatched points within one cell can't drag it), then Gaussian-
    smooths that per-cell median grid over a wide (`smoothing_radius_m`)
    neighborhood before returning it.

    The smoothing step is not optional polish -- without it this grid is
    dominated by real small-scale building relief (balcony railings, AC
    units, window frames: all genuinely NOT coplanar with the idealized flat
    wall, sticking out anywhere from a few cm to over a meter), not
    reconstruction error. Confirmed real, 2026-09-14 (BACK facade): per-point
    offset std was ~0.56m with essentially zero correlation to triangulation
    track length (-0.006, tested from 2 to 10+ views per point) -- ruling out
    "more views would fix it" per-point noise as the cause. Using those raw
    per-cell medians directly (as an earlier version of this function did)
    rendered visibly WAVY window lines across ordinary, structurally sound
    interior wall regions that had no problem before any correction was
    applied at all -- confirmed by direct before/after crop comparison.
    Applying Gaussian smoothing at increasing radii on the same real grid
    showed the offset signal decay smoothly toward zero with NO plateau
    (std 0.56 raw -> 0.14 @ 6m -> 0.06 @ 12m -> 0.015 @ 20m -> 0.002 @ 30m) --
    the complete absence of a floor means there is no broad, low-frequency
    "this whole wall section is off the plane" signal being thrown away here,
    only balcony/fixture-scale detail being averaged out, which is exactly
    the content this correction was never meant to touch in the first place
    (see `_warp_image_smooth`'s own docstring: canvas content is meant to
    read as the flat wall, not each protrusion warped forward by its own
    depth). `smoothing_radius_m=12.0` sits past the point where that decay
    curve has already erased essentially all of the balcony-scale noise
    (0.56 -> 0.06, ~9x) while still being narrow enough to respond to an
    actual broad deviation if a future facade's reconstruction has a real one.

    `max_offset_m` drops points before binning that are nowhere near the
    plane at all (sky/terrain incidentally triangulated alongside the real
    facade -- the same contamination `_filter_points_near_plane` already
    guards against when fitting the plane itself, reused here with the same
    default distance `_detect_off_wall_images` uses for its own on-wall
    check) -- otherwise a handful of background points landing in the same
    2D cell as real wall points could still bias that cell's median.

    A raw cell with fewer than `min_points_per_cell` surviving points does
    not contribute to the smoothed grid at all (neither numerator nor
    denominator), and a smoothed cell whose local Gaussian-weighted coverage
    fraction (`min_coverage_fraction` -- cv2.GaussianBlur's kernel is
    normalized, so blurring the 0/1 "has data" field directly gives the
    fraction of nearby mass that has real data, not a raw count) is too thin
    is left OUT of the final `grid` entirely (not set to 0) -- this is what
    lets `lookup_offset` fade
    smoothly back to the untouched flat-plane behavior in regions with no
    real nearby measurement (e.g. a building corner viewed edge-on, which
    isn't on this plane at all and has no business getting a "correction"
    invented for it) instead of extrapolating into territory the grid was
    never told anything about. `clamp_offset_m` is a final physical sanity
    bound on top of the smoothing (real measured deviations seen on this
    project so far have topped out around 0.5-0.6m) -- a defense-in-depth
    guard, not the primary mechanism, since smoothing alone already keeps
    typical values far below it."""
    normal = plane.normal
    buckets: dict[tuple[int, int], list[float]] = {}
    for point in reconstruction.points3D.values():
        vec = point.xyz - plane.origin
        offset = float(np.dot(vec, normal))
        if abs(offset) > max_offset_m:
            continue
        u_m = float(np.dot(vec, plane.e_u))
        v_m = float(np.dot(vec, plane.e_v))
        canvas_x = u_m * plane.px_per_m
        canvas_y = v_m * plane.px_per_m
        cell = (int(np.floor(canvas_x / cell_px)), int(np.floor(canvas_y / cell_px)))
        buckets.setdefault(cell, []).append(offset)

    raw_cells = {
        cell: float(np.median(values))
        for cell, values in buckets.items()
        if len(values) >= min_points_per_cell
    }
    if not raw_cells:
        return PlaneOffsetGrid(cell_px=cell_px, grid={})

    xs = [c[0] for c in raw_cells]
    ys = [c[1] for c in raw_cells]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    value_arr = np.zeros((y_max - y_min + 1, x_max - x_min + 1), dtype=np.float32)
    weight_arr = np.zeros_like(value_arr)
    for (cx, cy), v in raw_cells.items():
        value_arr[cy - y_min, cx - x_min] = v
        weight_arr[cy - y_min, cx - x_min] = 1.0

    sigma_cells = max(1e-3, (smoothing_radius_m * plane.px_per_m) / cell_px)
    ksize = int(sigma_cells * 6) | 1  # odd, ~6 sigma so the kernel isn't visibly truncated
    smoothed_num = cv2.GaussianBlur(value_arr * weight_arr, (ksize, ksize), sigma_cells)
    smoothed_den = cv2.GaussianBlur(weight_arr, (ksize, ksize), sigma_cells)

    grid: dict[tuple[int, int], float] = {}
    trusted = smoothed_den >= min_coverage_fraction
    ys_idx, xs_idx = np.where(trusted)
    for row, col in zip(ys_idx, xs_idx):
        value = float(smoothed_num[row, col] / smoothed_den[row, col])
        value = float(np.clip(value, -clamp_offset_m, clamp_offset_m))
        grid[(col + x_min, row + y_min)] = value

    return PlaneOffsetGrid(cell_px=cell_px, grid=grid)


def lookup_offset(offset_grid: PlaneOffsetGrid, canvas_x: float, canvas_y: float) -> float:
    """Bilinear interpolation of the plane-offset grid at a canvas-pixel
    location, TRUST-WEIGHTED so a grid cell with no real measurement (absent
    from `offset_grid.grid`) contributes zero weight instead of a guessed
    value -- confirmed real, 2026-09 (BACK facade): an earlier version that
    filled gaps via nearest-neighbor ring averaging invented plausible-but-
    arbitrary offsets in genuinely off-plane regions (a building corner
    viewed edge-on), and those guesses differed inconsistently between
    neighboring source images, producing a visible zigzag seam that wasn't
    there before any correction was applied at all. Renormalizing over only
    the corners that DO have data makes the correction fade smoothly to 0
    (= the original flat-plane result) as a query point moves away from real
    measurements, rather than extrapolating into territory the grid was
    never told anything about. Cell size is `offset_grid.cell_px`; the 4
    cell CENTERS surrounding (canvas_x, canvas_y) are the interpolation
    corners."""
    cell_px = offset_grid.cell_px
    gx = canvas_x / cell_px - 0.5
    gy = canvas_y / cell_px - 0.5
    cx0, cy0 = int(np.floor(gx)), int(np.floor(gy))
    fx, fy = gx - cx0, gy - cy0

    total_weight = 0.0
    total_value = 0.0
    for dx, dy, w in (
        (0, 0, (1 - fx) * (1 - fy)),
        (1, 0, fx * (1 - fy)),
        (0, 1, (1 - fx) * fy),
        (1, 1, fx * fy),
    ):
        value = offset_grid.grid.get((cx0 + dx, cy0 + dy))
        if value is None:
            continue
        total_weight += w
        total_value += w * value

    if total_weight < 1e-6:
        return 0.0
    return total_value / total_weight


def _build_trust_mask(
    img: "pycolmap.Image",
    reconstruction: pycolmap.Reconstruction,
    plane: FacadePlane,
    src_w: int,
    src_h: int,
    cell_px: int = 200,
    min_points_per_cell: int = 3,
    distrust_offset_m: float = 3.0,
) -> np.ndarray:
    """Per-SOURCE-image mask (src_h, src_w), 255 = trusted, 0 = distrusted --
    NOT a second 3D plane, but a cheap, well-scoped proxy for the same idea:
    stop a source image's genuinely off-plane content (a building corner
    viewed edge-on, sky/terrain past the roofline) from ever COMPETING for
    canvas ownership against a same-region image that actually shows the
    flat wall there, without needing dense per-pixel depth (not available in
    this SIFT-sparse pipeline) or a second explicit plane fit.

    Confirmed real, 2026-09 (BACK facade): the drone's corner-orbit pass
    produced NINE consecutive images (DJI_0048-0056) that each mix real
    on-wall content with varying amounts of corner/side-wall content in the
    SAME frame (on-wall vs far-off-plane 2D-3D point fraction ranged
    continuously from 0.10 to 0.82 across them, no clean few-outliers split)
    -- confirmed via direct visual inspection that whichever of these images
    the seam-selection graph-cut happened to pick as owner for a given canvas
    region determined whether that region rendered flat (good) or distorted/
    "3D-looking" (bad), alternating floor to floor down the corner column.
    Excluding whole images (the DJI_0089/0131 rotation-outlier approach) is
    unsafe here since ALL nine images have SOME real, needed on-wall content
    -- only each one's own off-plane REGION should be devalued.

    Bins this image's own 2D-3D correspondences (img.points2D, the same SfM
    data _detect_off_wall_images/build_plane_offset_grid already use) into a
    coarse SOURCE-PIXEL grid and marks a cell distrusted only when it has
    enough points (`min_points_per_cell`) AND their median |offset| from the
    plane exceeds `distrust_offset_m` (same 3.0m convention used elsewhere
    in this file for "clearly not on this wall"). A cell with too few points
    to judge defaults to TRUSTED -- sparse SIFT coverage is naturally uneven,
    and treating "no evidence" as "guilty" would erase large innocent
    regions purely for lacking feature matches, not for any actual off-plane
    signal."""
    normal = plane.normal
    buckets: dict[tuple[int, int], list[float]] = {}
    for p in img.points2D:
        if not p.has_point3D() or p.point3D_id not in reconstruction.points3D:
            continue
        point3d = reconstruction.points3D[p.point3D_id]
        offset = float(np.dot(point3d.xyz - plane.origin, normal))
        px, py = p.xy
        cell = (int(px // cell_px), int(py // cell_px))
        buckets.setdefault(cell, []).append(offset)

    n_cols = int(np.ceil(src_w / cell_px)) + 1
    n_rows = int(np.ceil(src_h / cell_px)) + 1
    trust_small = np.full((n_rows, n_cols), 255, dtype=np.uint8)
    for (cx, cy), offsets in buckets.items():
        if len(offsets) < min_points_per_cell:
            continue
        if not (0 <= cy < n_rows and 0 <= cx < n_cols):
            continue
        if float(np.median(np.abs(offsets))) > distrust_offset_m:
            trust_small[cy, cx] = 0

    return cv2.resize(trust_small, (src_w, src_h), interpolation=cv2.INTER_NEAREST)


def _warp_image_smooth(
    raw: np.ndarray,
    K: np.ndarray,
    R: np.ndarray,
    t: np.ndarray,
    plane: FacadePlane,
    offset_grid: PlaneOffsetGrid,
    roi: tuple[int, int, int, int],  # (x0, y0, x1, y1) in full-canvas pixel coords
    sample_step_px: int = 24,
    src_trust_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-pixel remap warp of one source image into its canvas ROI, using a
    LOCALLY plane-offset-corrected world point at every sampled canvas
    location instead of one constant flat-plane homography for the whole
    image -- world_point(canvas_x, canvas_y) = plane.origin + u_m*e_u +
    v_m*e_v + lookup_offset(...)*normal, projected into this camera via
    K/R/t. Where `offset_grid` has no data nearby, lookup_offset returns 0
    and this reduces to exactly the same point the flat-plane homography
    (`_camera_to_facade_homography`) would have used.

    Builds the correspondence only on a COARSE `sample_step_px` grid (not
    once per output pixel -- that would mean one K@(R@X+t) projection per
    pixel, prohibitively slow) and bilinearly upsamples via cv2.resize to a
    dense per-pixel map before a single cv2.remap() call. This is
    deliberately NOT a piecewise hard-paste of per-cell homographies (an
    earlier, abandoned attempt at 200px and then 64px cells): a hard cell
    boundary is a real discontinuity in the warp itself and produced a
    visible torn seam / jagged staircase at the wall/roofline boundary. A
    dense, smoothly-interpolated remap has no such boundary anywhere by
    construction, since every output pixel gets its own smoothly-blended
    correction instead of inheriting one whole cell's single value.

    `src_trust_mask` (see _build_trust_mask), if given, is used as this
    image's valid-pixel mask INSTEAD of the default all-255 mask -- so a
    region this specific image's own SfM points flag as off-plane never
    reaches the seam/blend step as this image's content, without touching
    `warped_img` at all (other images can still legitimately win that same
    canvas region if their own content there is trustworthy)."""
    x0, y0, x1, y1 = roi
    local_w, local_h = x1 - x0, y1 - y0
    src_h, src_w = raw.shape[:2]

    n_cols = max(2, int(np.ceil(local_w / sample_step_px)) + 1)
    n_rows = max(2, int(np.ceil(local_h / sample_step_px)) + 1)
    xs = np.linspace(x0, x1 - 1, n_cols)
    ys = np.linspace(y0, y1 - 1, n_rows)

    map_x_coarse = np.zeros((n_rows, n_cols), dtype=np.float32)
    map_y_coarse = np.zeros((n_rows, n_cols), dtype=np.float32)
    Rt = R @ plane.origin + t
    R_eu = R @ plane.e_u
    R_ev = R @ plane.e_v
    R_normal = R @ plane.normal
    for i, canvas_y in enumerate(ys):
        for j, canvas_x in enumerate(xs):
            u_m = canvas_x / plane.px_per_m
            v_m = canvas_y / plane.px_per_m
            offset_m = lookup_offset(offset_grid, canvas_x, canvas_y)
            cam_point = Rt + u_m * R_eu + v_m * R_ev + offset_m * R_normal
            src_h_coord = K @ cam_point
            if src_h_coord[2] <= 1e-9:
                map_x_coarse[i, j] = -1.0
                map_y_coarse[i, j] = -1.0
            else:
                map_x_coarse[i, j] = src_h_coord[0] / src_h_coord[2]
                map_y_coarse[i, j] = src_h_coord[1] / src_h_coord[2]

    map_x = cv2.resize(map_x_coarse, (local_w, local_h), interpolation=cv2.INTER_LINEAR)
    map_y = cv2.resize(map_y_coarse, (local_w, local_h), interpolation=cv2.INTER_LINEAR)

    warped_img = cv2.remap(
        raw, map_x, map_y, interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT, borderValue=0,
    )
    src_mask = src_trust_mask if src_trust_mask is not None else np.full((src_h, src_w), 255, dtype=np.uint8)
    warped_mask = cv2.remap(
        src_mask, map_x, map_y, interpolation=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT, borderValue=0,
    )
    return warped_img, warped_mask


def rectify_images(
    reconstruction: pycolmap.Reconstruction,
    plane: FacadePlane,
    images_dir: str | Path,
) -> tuple[dict[str, WarpedImage], tuple[int, int], dict[str, SourceTransform]]:
    """Undistort + plane-project every registered image onto one fixed,
    plane-sized canvas, cropped to each image's own local ROI (its projected
    footprint intersected with the canvas bounds) -- the same trick warp.py's
    H-chain path already uses. This function used to warp every image to the
    FULL canvas size (corner always (0,0)) on the reasoning that warp.py's
    ROI trick exists only to bound a canvas a *drifting chain* could blow up
    arbitrarily, which doesn't apply here since the canvas is fixed by the
    known facade span. True, but irrelevant to a different cost that same
    choice was paying: a facade with many registered images and a large
    canvas means holding all of them as full-canvas-sized buffers
    simultaneously in `warped` is itself a huge amount of memory, regardless
    of how correctly the canvas size was computed (confirmed real failure,
    2026-09-10: 150 images on a correctly-sized canvas still OOM'd, this time
    on a much smaller single allocation -- a sign of memory pressure/
    fragmentation from holding every image at full-canvas size, not a
    miscomputed size). Each image only ever actually covers a small fraction
    of a facade this size, so cropping to its own footprint (like warp.py
    does) fixes that without changing anything about how the canvas itself
    is sized or bounded.

    Third return value: per-image SourceTransform (facade-canvas homography +
    this source image's own (width, height)) -- `H` is always stored in
    full-canvas coordinates (never the local-ROI-shifted version used for the
    actual warp below), so nothing downstream that reads source_transforms
    (e.g. crack/pipeline.py's source_observations, which inverts H to map a
    mosaic crack location back to this source image's own pixel coordinates)
    needs to know about the local crop at all. Note H lands in
    *undistorted*-image pixel space when SIMPLE_RADIAL undistortion was
    applied above, not exactly the raw on-disk JPEG's pixel space (same
    (width, height) either way -- cv2.undistort preserves image dimensions)."""
    canvas_w = max(1, int(round(plane.width_m * plane.px_per_m)))
    canvas_h = max(1, int(round(plane.height_m * plane.px_per_m)))

    # Built once from the reconstruction's own triangulated points, reused for
    # every image below -- see build_plane_offset_grid's docstring for why a
    # cell needs `min_points_per_cell` real near-plane points to be trusted at
    # all (an empty/sparse grid makes every lookup_offset() call return 0.0,
    # which makes _warp_image_smooth reduce to exactly the old flat-plane
    # warpPerspective result -- this is a strict generalization, not a
    # behavior change, wherever there isn't enough real 3D data to correct
    # anything).
    offset_grid = build_plane_offset_grid(reconstruction, plane)

    warped: dict[str, WarpedImage] = {}
    source_transforms: dict[str, SourceTransform] = {}
    for img in reconstruction.images.values():
        image_id = Path(img.name).stem
        raw = imread_unicode(Path(images_dir) / img.name, cv2.IMREAD_COLOR)
        if raw is None:
            continue

        cam = img.camera
        K = cam.calibration_matrix()
        if cam.model.name == "SIMPLE_RADIAL" and abs(float(cam.params[3])) > 1e-9:
            k = float(cam.params[3])
            raw = cv2.undistort(raw, K, np.array([k, 0.0, 0.0, 0.0], dtype=np.float64))

        pose = img.cam_from_world()
        R = pose.rotation.matrix()
        t = np.asarray(pose.translation)
        H = _camera_to_facade_homography(K, R, t, plane)

        src_h, src_w = raw.shape[:2]
        trust_mask = _build_trust_mask(img, reconstruction, plane, src_w, src_h)
        corners_img = np.array(
            [[0, 0], [src_w, 0], [src_w, src_h], [0, src_h]], dtype=np.float64
        ).reshape(-1, 1, 2)
        corners_canvas = cv2.perspectiveTransform(corners_img, H).reshape(-1, 2)
        x0 = max(0, int(np.floor(corners_canvas[:, 0].min())))
        y0 = max(0, int(np.floor(corners_canvas[:, 1].min())))
        x1 = min(canvas_w, int(np.ceil(corners_canvas[:, 0].max())))
        y1 = min(canvas_h, int(np.ceil(corners_canvas[:, 1].max())))
        if x1 <= x0 or y1 <= y0:
            continue  # this image's footprint doesn't actually land on the canvas
        local_w, local_h = x1 - x0, y1 - y0

        # Plane-offset-corrected smooth remap (not warpPerspective(H_local))
        # for the actual pixel content -- H itself is still what's stored
        # below in SourceTransform, since crack/pipeline.py's later inversion
        # back to source-image pixels is out of scope for this pass (visual
        # mosaic quality only).
        warped_img, warped_mask = _warp_image_smooth(
            raw, K, R, t, plane, offset_grid, (x0, y0, x1, y1), src_trust_mask=trust_mask,
        )

        warped[image_id] = WarpedImage(image=warped_img, mask=warped_mask, corner=(x0, y0), size=(local_w, local_h))
        source_transforms[image_id] = SourceTransform(H=H, width=src_w, height=src_h)

    return warped, (canvas_w, canvas_h), source_transforms


def _crop_to_dense_coverage(
    warped: dict[str, WarpedImage], canvas_size: tuple[int, int], min_coverage_count: int = 2, margin_px: int = 0
) -> tuple[int, int, int, int] | None:
    """Bounding box (x0, y0, x1, y1) of canvas pixels covered by at least
    `min_coverage_count` overlapping source images -- often noticeably
    smaller than the full canvas, because plenty of that canvas is real photo
    content but not *facade* content: an oblique DJI shot also frames sky
    above the roofline and ground/terrain below, and `_camera_to_facade_homography`
    projects those off-plane pixels too (it has no way to know they aren't on
    the wall). The facade surface itself is seen redundantly -- the whole
    point of a drone orbit is heavy overlap between neighboring shots -- so it
    typically has coverage_count well above 1 almost everywhere. Off-plane
    background does not: the planar homography assumption is simply wrong for
    it, so two different photos' sky/terrain pixels land at two different,
    essentially unrelated canvas positions instead of stacking up the way real
    facade pixels do. Thresholding on redundancy (not on any color/semantic
    guess about "is this sky") separates the two using a signal this pipeline
    already computes for free while pasting `warped` onto the canvas.

    Returns None when the dense region already spans (approximately) the
    whole canvas -- nothing worth cropping (also covers `warped` being empty).
    """
    canvas_w, canvas_h = canvas_size
    count = np.zeros((canvas_h, canvas_w), dtype=np.int32)
    for w in warped.values():
        x, y = w.corner
        ww, hh = w.size
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(canvas_w, x + ww), min(canvas_h, y + hh)
        if x1 <= x0 or y1 <= y0:
            continue
        count[y0:y1, x0:x1] += w.mask[y0 - y : y1 - y, x0 - x : x1 - x] > 0

    dense = count >= min_coverage_count
    if not dense.any():
        return None
    ys, xs = np.where(dense)
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    if margin_px > 0:
        x0 = max(0, x0 - margin_px)
        y0 = max(0, y0 - margin_px)
        x1 = min(canvas_w, x1 + margin_px)
        y1 = min(canvas_h, y1 + margin_px)
    if x0 <= 0 and y0 <= 0 and x1 >= canvas_w and y1 >= canvas_h:
        return None
    return x0, y0, x1, y1


def _apply_canvas_crop(
    warped: dict[str, WarpedImage],
    source_transforms: dict[str, SourceTransform],
    bbox: tuple[int, int, int, int],
) -> tuple[dict[str, WarpedImage], dict[str, SourceTransform], tuple[int, int]]:
    """Re-expresses every warped image and source homography in the cropped
    canvas's own coordinate frame -- the same local-shift trick `rectify_images`
    already applies per image (see `local_shift`/`H_local` there), just applied
    once more for the shared canvas crop itself. `source_transforms[...].H`
    must move with the crop: crack/pipeline.py inverts it later to map a
    mosaic-space crack location back to a source image's own pixel coordinates,
    and that only stays correct if H still points into the *same* canvas frame
    the crack was actually detected in."""
    x0, y0, x1, y1 = bbox
    shift = np.array([[1.0, 0.0, -x0], [0.0, 1.0, -y0], [0.0, 0.0, 1.0]])

    new_warped: dict[str, WarpedImage] = {}
    new_transforms: dict[str, SourceTransform] = {}
    for image_id, w in warped.items():
        cx, cy = w.corner
        ww, hh = w.size
        ix0, iy0 = max(cx, x0), max(cy, y0)
        ix1, iy1 = min(cx + ww, x1), min(cy + hh, y1)
        if ix1 <= ix0 or iy1 <= iy0:
            continue  # this image's footprint falls entirely outside the dense-coverage crop
        local_image = w.image[iy0 - cy : iy1 - cy, ix0 - cx : ix1 - cx]
        local_mask = w.mask[iy0 - cy : iy1 - cy, ix0 - cx : ix1 - cx]
        new_warped[image_id] = WarpedImage(
            image=local_image, mask=local_mask, corner=(ix0 - x0, iy0 - y0), size=(ix1 - ix0, iy1 - iy0)
        )
        st = source_transforms[image_id]
        new_transforms[image_id] = SourceTransform(H=shift @ st.H, width=st.width, height=st.height)

    return new_warped, new_transforms, (x1 - x0, y1 - y0)


def rectify_and_blend(
    facade_id: str,
    reconstruction: pycolmap.Reconstruction,
    plane: FacadePlane,
    images_dir: str | Path,
    cfg,
    colmap_mean_reprojection_error_px: float | None = None,
) -> MosaicResult:
    """Full COLMAP-pose-rectified facade mosaic: plane-project -> seam ->
    blend (#13/#14), reusing the same seam/blend code the homography-chain
    path uses (stitching/blend.py) — the two paths only disagree about where
    each image's pixels land on the canvas, not about how to combine them
    once there. `plane` is already fully built by the caller — either
    footprint-based (facade_plane_from_segment, Phase 2/3) or fitted straight
    from the COLMAP reconstruction itself (facade_plane_from_reconstruction,
    Phase 1) — this function doesn't care which.
    """
    warped, canvas_size, source_transforms = rectify_images(reconstruction, plane, images_dir)

    # Confirmed real, 2026-09-11: even after facade_plane_from_reconstruction's
    # own background-point filtering keeps the *canvas* sized to the real
    # facade, individual oblique photos still frame sky/terrain past the
    # roofline/ground within that canvas, and that off-plane content still
    # gets projected onto it (see _crop_to_dense_coverage). Tightening to
    # where multiple photos actually agree removes most of that margin.
    bbox = _crop_to_dense_coverage(warped, canvas_size, min_coverage_count=2, margin_px=int(0.5 * plane.px_per_m))
    if bbox is not None:
        warped, source_transforms, canvas_size = _apply_canvas_crop(warped, source_transforms, bbox)

    seam_masks = compute_seam_masks(warped, canvas_size)
    seam_owner_map, seam_owner_index = build_owner_map(seam_masks, warped, canvas_size)
    scfg = cfg.stitch
    analysis_image = blend_analysis(warped, seam_masks, canvas_size) if scfg.generate_analysis_mosaic else None
    visual_image = (
        blend_visual(warped, seam_masks, canvas_size, num_bands=int(scfg.multiband_num_bands))
        if scfg.generate_visual_mosaic
        else None
    )

    canvas_w, canvas_h = canvas_size
    observed_mask = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    for w in warped.values():
        paste_max(observed_mask, w.mask, w.corner)
    coverage_ratio = float(np.count_nonzero(observed_mask)) / float(observed_mask.size)

    quality = StitchQualityReport(
        facade_id=facade_id,
        image_count=len(warped),
        matched_pair_count=0,  # not applicable — no pairwise graph in this path
        failed_pair_count=0,
        mean_inlier_ratio=None,
        median_reprojection_error_px=colmap_mean_reprojection_error_px,
        coverage_ratio=coverage_ratio,
        disconnected_components=1,
        reference_image_id=None,
        unreachable_image_ids=[],
        global_drift_score_px=None,  # no chain to drift — every image is placed independently
        max_drift_score_px=None,
        cycle_edge_count=0,
        needs_colmap_fallback=False,
    )

    return MosaicResult(
        analysis_image=analysis_image,
        visual_image=visual_image,
        observed_mask=observed_mask,
        quality=quality,
        source_transforms=source_transforms,
        seam_owner_map=seam_owner_map,
        seam_owner_index=seam_owner_index,
    )
