"""General safety-net for automatic COLMAP image-exclusion rules -- any rule
that removes images before bundle adjustment (off-wall detection, rotation-
outlier detection, or any future one), not specific to one facade or one
exclusion rule.

Confirmed real, 2026-09-15/16 (BACK facade, discovered across two separate
incidents the same day -- see memory/colmap_global_exclusion_risk.md):
COLMAP bundle adjustment is a JOINT optimization over every registered
image's pose at once. `_detect_off_wall_images` and the now-disabled
`_detect_rotation_outlier_images` both only checked that OTHER images
already redundantly cover the excluded candidate's OWN canvas footprint --
neither checked whether removing it leaves some OTHER, unrelated canvas
region with too little independent coverage left, letting a marginal image
win seam ownership there by default where it previously would have lost to
a better-covered alternative.

Three more direct per-image geometric signals were tried and FAILED to
discriminate the real, confirmed-bad images (DJI_0076/0078/0079/0080/0120
in the BACK case) from ordinary ones, each measured against real data before
being discarded (2026-09-16, this same investigation):
  - Camera position shift between the two reconstructions: all under 6cm,
    no correlation with which images actually caused visible artifacts.
  - Camera rotation shift: all under 0.07 degrees, same non-result.
  - Raw projected-footprint area: EVERY image (not just the bad ones) shows
    a large (700-950 sq m) footprint on an oblique-orbit facade like this
    one -- footprint size alone doesn't separate a genuinely bad image from
    a normal wide-angle one.
  - Local projective scale distortion (canvas-area per image-pixel,
    relative to the image's own optical center): ratio ~1.0 everywhere for
    the confirmed-bad image -- its own homography is not internally
    distorted, so this signal has nothing to detect either.

What DID work, validated against real data: comparing per-location
COVERAGE COUNT (how many images' frustums include that physical point) at
shared REAL-WORLD points (not canvas pixels, which live in different,
incompatible coordinate systems across two reconstructions with different
plane fits) between the baseline (before exclusion) and trial (after
exclusion) reconstruction. Confirmed-bad regions showed real drops (12.0->
8.8 covering images, 18.7->7.3); a confirmed-good, unaffected region showed
none (9.6->9.75, noise). This is the actual mechanism: local redundancy
loss, not any property of one image's own geometry.

Bounded cost: this needs exactly the two reconstructions the pipeline
already computes for every facade (stage1 baseline, stage2 trial) -- no
extra COLMAP run for DETECTION. One extra COLMAP mapping+rectify pass is
needed only if rescue candidates are found, to verify the fix, regardless
of how many candidates the original exclusion rule proposed (BACK alone had
53) -- not one extra run per candidate, which would be impractical.

Rescue candidate selection (which excluded images to un-exclude once a
redundancy-loss region is flagged) also went through one rejected attempt:
"does the excluded candidate's own camera FRUSTUM include a flagged point"
rescued 51 of 53 excluded images on the BACK case -- nearly everything,
because frustum inclusion is purely a function of camera pose/FOV and
doesn't know whether the real captured content there is wall or sky, so
almost any image aimed roughly at the facade "contains" most flagged points
regardless of what it actually photographed. Requiring the candidate to
have real triangulated ON-WALL 3D points (same signal
`_detect_off_wall_images` already uses) actually near the flagged points,
with a minimum count to reject single-stray-point noise, narrowed this to
13 -- see `find_rescue_candidates`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


def coverage_count_at_world_points(reconstruction, world_points: np.ndarray) -> np.ndarray:
    """For each Nx3 real-world (UTM) point, how many of `reconstruction`'s
    registered images have that point inside their frame bounds and in
    front of the camera. Purely geometric (camera intrinsics/pose only) --
    doesn't need the point to be an actual triangulated SfM point."""
    counts = np.zeros(len(world_points), dtype=np.int32)
    for img in reconstruction.images.values():
        K = img.camera.calibration_matrix()
        pose = img.cam_from_world()
        R = pose.rotation.matrix()
        t = np.asarray(pose.translation)
        w, h = img.camera.width, img.camera.height
        cam_pts = (R @ world_points.T).T + t
        in_front = cam_pts[:, 2] > 1e-6
        safe_z = np.where(in_front, cam_pts[:, 2], 1.0)
        proj = (K @ cam_pts.T).T
        proj_xy = proj[:, :2] / safe_z[:, None]
        in_bounds = (
            in_front
            & (proj_xy[:, 0] >= 0) & (proj_xy[:, 0] < w)
            & (proj_xy[:, 1] >= 0) & (proj_xy[:, 1] < h)
        )
        counts += in_bounds.astype(np.int32)
    return counts


def _on_wall_points_by_image(reconstruction, plane, plane_distance_m: float = 3.0) -> dict[str, np.ndarray]:
    """Each registered image's own triangulated 3D points that land near the
    facade plane -- same "is this real wall evidence" signal
    `_detect_off_wall_images` already uses (reused deliberately, not
    reinvented: a candidate's FRUSTUM merely containing a flagged point was
    tried first and rejected -- see module docstring -- because frustum
    inclusion doesn't know whether the real captured content there is wall
    or sky; actual triangulated on-wall points do)."""
    from pathlib import Path

    normal = np.cross(plane.e_u, plane.e_v)
    normal = normal / np.linalg.norm(normal)

    by_image: dict[str, list] = {}
    for img in reconstruction.images.values():
        pts = []
        for p in img.points2D:
            if not p.has_point3D() or p.point3D_id not in reconstruction.points3D:
                continue
            xyz = reconstruction.points3D[p.point3D_id].xyz
            if abs(float(np.dot(xyz - plane.origin, normal))) <= plane_distance_m:
                pts.append(xyz)
        if pts:
            by_image[Path(img.name).stem] = np.asarray(pts)
    return by_image


def find_rescue_candidates(
    baseline_reconstruction,
    trial_plane,
    flagged_points: np.ndarray,
    excluded_ids: set[str],
    radius_m: float = 3.0,
    min_evidence_points: int = 5,
) -> set[str]:
    """Which excluded candidates have enough REAL on-wall SfM evidence near
    the flagged (redundancy-loss) points to plausibly fill the gap.
    `min_evidence_points=5` matches this codebase's existing
    `min_points_per_cell` convention (geometry/rectification.py's
    build_plane_offset_grid) -- a single stray triangulated point isn't
    reliable evidence of genuine wall coverage; confirmed real, 2026-09-16:
    an unfiltered version (>=1 point) rescued candidates on as little as 1
    total on-wall point, clearly noise, not real coverage."""
    if len(flagged_points) == 0 or not excluded_ids:
        return set()

    on_wall = _on_wall_points_by_image(baseline_reconstruction, trial_plane)
    rescued: set[str] = set()
    for image_id in excluded_ids:
        pts = on_wall.get(image_id)
        if pts is None:
            continue
        d = np.linalg.norm(flagged_points[:, None, :] - pts[None, :, :], axis=2)
        n_near = int((d.min(axis=0) <= radius_m).sum())
        if n_near >= min_evidence_points:
            rescued.add(image_id)
    return rescued


def build_world_point_grid(plane, tile_m: float = 2.0) -> np.ndarray:
    """Grid of real-world (UTM) points spanning `plane`'s own facade extent,
    spaced `tile_m` apart -- the shared reference frame both reconstructions'
    coverage gets compared against, since canvas PIXEL coordinates differ
    between two reconstructions with different plane fits (different point
    clouds -> different SVD/PCA fit) even for the same physical wall."""
    us = np.arange(0, max(plane.width_m, tile_m), tile_m)
    vs = np.arange(0, max(plane.height_m, tile_m), tile_m)
    grid_u, grid_v = np.meshgrid(us, vs)
    grid_u, grid_v = grid_u.ravel(), grid_v.ravel()
    return plane.origin[None, :] + grid_u[:, None] * plane.e_u[None, :] + grid_v[:, None] * plane.e_v[None, :]


@dataclass
class ExclusionSafetyResult:
    world_points: np.ndarray
    baseline_counts: np.ndarray
    trial_counts: np.ndarray
    flagged_mask: np.ndarray
    rescued_ids: set[str] = field(default_factory=set)

    @property
    def needs_rerun(self) -> bool:
        return bool(self.rescued_ids)


def check_exclusion_safety(
    baseline_reconstruction,
    trial_reconstruction,
    trial_plane,
    excluded_ids: set[str],
    tile_m: float = 2.0,
    min_absolute_drop: int = 3,
    min_baseline_count: int = 4,
    rescue_radius_m: float = 3.0,
    min_evidence_points: int = 5,
) -> ExclusionSafetyResult:
    """Compare coverage-count redundancy between the baseline (all images)
    and trial (candidates excluded) reconstructions at a shared world-point
    grid derived from the trial's own facade plane. A point is flagged when
    it lost at least `min_absolute_drop` covering images AND had at least
    `min_baseline_count` to begin with (so naturally-thin edge regions,
    where any change looks large only in relative terms, aren't flagged just
    for existing near the canvas boundary).

    `min_absolute_drop=3` and `min_baseline_count=4` are starting points
    validated against ONE real facade (BACK) -- confirmed to separate its
    two known-bad regions (drops of 3.2 and 11.3) from its one known-good,
    unaffected region (drop of -0.1) -- not yet cross-checked against a
    second facade's own overlap/density characteristics, so treat these as
    provisional until more real data exists, same as this project's other
    initial thresholds (CLAUDE.local.md #33's own convention).

    For every flagged point, whichever EXCLUDED candidates cover it in the
    baseline reconstruction are added to `rescued_ids` -- these are the
    images whose restoration could plausibly fill the redundancy gap that
    caused the flag."""
    world_points = build_world_point_grid(trial_plane, tile_m=tile_m)
    baseline_counts = coverage_count_at_world_points(baseline_reconstruction, world_points)
    trial_counts = coverage_count_at_world_points(trial_reconstruction, world_points)

    drop = baseline_counts - trial_counts
    flagged_mask = (drop >= min_absolute_drop) & (baseline_counts >= min_baseline_count)

    flagged_points = world_points[flagged_mask]
    rescued_ids = find_rescue_candidates(
        baseline_reconstruction, trial_plane, flagged_points, excluded_ids,
        radius_m=rescue_radius_m, min_evidence_points=min_evidence_points,
    )

    return ExclusionSafetyResult(
        world_points=world_points,
        baseline_counts=baseline_counts,
        trial_counts=trial_counts,
        flagged_mask=flagged_mask,
        rescued_ids=rescued_ids,
    )
