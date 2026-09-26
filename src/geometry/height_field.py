"""Depth-aware (height-field) facade rectification.

Plain plane rectification (rectification.rectify_images) projects every photo
onto ONE flat plane. A wall that is not flat -- balcony recesses ~1.2 m behind
the wall face, slabs sticking out in front of it -- then lands at a different
canvas position in every photo (parallax = depth offset x viewing angle), so
the mosaic tears exactly there. Confirmed real, 2026-09-22 (FRONT, 10 m
standoff: the same 1.2 m recess that is a small error at 21 m is roughly twice
as large at 10 m).

Here the wall is modelled as a height field over the facade canvas: a per-cell
offset d(u, v) from the reference plane along the plane normal, estimated from
COLMAP's own triangulated points. A canvas pixel is then the 3D point
origin + u*e_u + v*e_v + d(u, v)*n_in, and each source pixel is fetched by
projecting that 3D point into the camera -- so photos taken from different
positions agree on recessed and protruding surfaces too.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pandas as pd
import pycolmap
from scipy import ndimage

from src.common.imageio import imread_unicode
from src.geometry.rectification import FacadePlane, _camera_to_facade_homography
from src.stitching.warp import SourceTransform, WarpedImage


def plane_normal_into_wall(plane: FacadePlane) -> np.ndarray:
    n = np.cross(plane.e_u, plane.e_v)
    return n / np.linalg.norm(n)


def build_offset_map(
    reconstruction: pycolmap.Reconstruction,
    plane: FacadePlane,
    cell_px: int = 20,
    min_track_length: int = 3,
    max_point_error_px: float = 1.0,
    max_abs_offset_m: float = 2.5,
    fill_radius_cells: int = 4,
    corner_taper_m: float = 1.0,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Returns (offset_map, corner_mask, info).

    offset_map: d(u, v) in metres at the FULL canvas resolution (canvas_h x
    canvas_w float32), positive = further from the camera than the wall face.
    corner_mask: full-canvas bool, True where the surface likely folds away
    from the fitted facade plane (real corners/pilasters) -- see
    rectify_images_hybrid, which falls back to the flat-plane warp there.

    Cells with no reliable point within `fill_radius_cells` fall back to the
    dominant wall-face offset (mode of the point offsets) -- a textureless flat
    wall triangulates few points, and "flat wall" is the right default there.
    A 3x3 median then removes isolated bad cells without rounding recess edges."""
    canvas_w = max(1, int(round(plane.width_m * plane.px_per_m)))
    canvas_h = max(1, int(round(plane.height_m * plane.px_per_m)))
    n_in = plane_normal_into_wall(plane)

    xyz, err, track = [], [], []
    for p in reconstruction.points3D.values():
        xyz.append(p.xyz)
        err.append(p.error)
        track.append(p.track.length())
    xyz = np.asarray(xyz)
    rel = xyz - plane.origin
    u = (rel @ plane.e_u) * plane.px_per_m
    v = (rel @ plane.e_v) * plane.px_per_m
    d = rel @ n_in
    keep = (
        (u >= 0) & (u < canvas_w) & (v >= 0) & (v < canvas_h)
        & (np.abs(d) < max_abs_offset_m)
        & (np.asarray(track) >= min_track_length) & (np.asarray(err) <= max_point_error_px)
    )
    u, v, d = u[keep], v[keep], d[keep]
    if d.size < 50:
        return (
            np.zeros((canvas_h, canvas_w), dtype=np.float32),
            np.zeros((canvas_h, canvas_w), dtype=bool),
            {"points": int(d.size), "wall_offset_m": 0.0},
        )

    hist, edges = np.histogram(d, bins=np.arange(-max_abs_offset_m, max_abs_offset_m + 0.1, 0.1))
    wall_offset = float(edges[int(np.argmax(hist))] + 0.05)

    gh, gw = int(np.ceil(canvas_h / cell_px)), int(np.ceil(canvas_w / cell_px))
    df = pd.DataFrame({"cy": (v // cell_px).astype(int), "cx": (u // cell_px).astype(int), "d": d})
    grouped = df.groupby(["cy", "cx"])["d"]
    med = grouped.median()
    spread = grouped.quantile(0.75) - grouped.quantile(0.25)
    grid = np.full((gh, gw), np.nan, dtype=np.float32)
    grid[med.index.get_level_values(0), med.index.get_level_values(1)] = med.values

    valid = ~np.isnan(grid)
    dist, (iy, ix) = ndimage.distance_transform_edt(~valid, return_indices=True)
    filled = grid[iy, ix]
    filled[dist > fill_radius_cells] = wall_offset
    filled = ndimage.median_filter(filled, size=3)

    # A corner/pilaster (a fold in the surface, its own face pointing a different
    # direction than the fitted facade normal) has no valid "offset along one
    # normal" at all -- confirmed real, 2026-09-22 (FRONT, 10 m standoff): near
    # such a fold, offset shoots to +/-max_abs_offset_m within one cell (a near-
    # grazing viewing ray makes the projected source pixel extremely sensitive to
    # the assumed depth). Three attempts so far confirmed insufficient: (1)
    # pinning just the flagged cell to wall_offset left a sharp one-cell step
    # into its untouched neighbor, still jagged at grazing incidence; (2)
    # splicing the flat-plane warp back in over a dilated 2D corner blob (a
    # second render pass) just moved the tear to the splice boundary; (3) a 2D
    # per-cell smoothstep taper reduced but did not eliminate it -- the flagged
    # cells form a noisy, patchy 2D blob (not a clean region), so both the taper
    # distance field and the resulting offset gradient stayed irregular right
    # where it mattered.
    #
    # Fix: a real pilaster is a vertical architectural feature -- it runs the
    # FULL HEIGHT of the facade, independent of v. Aggregating "unreliable" per
    # U-COLUMN (the fraction of that column's cells flagged) instead of per 2D
    # cell turns the noisy blob into a clean, contiguous vertical band exactly
    # where the real fold is, with genuine per-cell noise elsewhere (an
    # occasional bad point, not a real fold) averaged away since it only ever
    # affects a few cells in any one column. The smoothstep taper is then only
    # 1D (distance in u), so it has a single, bounded, predictable slope by
    # construction -- no risk of the 2D distance-field irregularity that
    # undermined attempt 3.
    spread_grid = np.zeros((gh, gw), dtype=np.float32)
    spread_grid[spread.index.get_level_values(0), spread.index.get_level_values(1)] = spread.values
    gy, gx = np.gradient(filled)
    gradient_mag = np.hypot(gy, gx)
    unreliable = (spread_grid > 0.3) | (gradient_mag > 0.25)
    column_frac = unreliable.mean(axis=0)  # fraction of flagged cells per u-column
    corner_column = column_frac > 0.15

    taper_cells = max(1.0, corner_taper_m * plane.px_per_m / cell_px)
    dist_from_fold_1d = (
        ndimage.distance_transform_edt(~corner_column) if corner_column.any() else np.full(gw, np.inf)
    )
    weight_1d = np.clip(dist_from_fold_1d / taper_cells, 0.0, 1.0)
    weight_1d = weight_1d * weight_1d * (3.0 - 2.0 * weight_1d)  # smoothstep
    filled = wall_offset + (filled - wall_offset) * weight_1d[np.newaxis, :]
    filled = ndimage.median_filter(filled, size=3)
    unreliable = np.broadcast_to(corner_column, (gh, gw))

    full = cv2.resize(filled, (canvas_w, canvas_h), interpolation=cv2.INTER_LINEAR)
    corner_mask = cv2.resize(
        unreliable.astype(np.uint8), (canvas_w, canvas_h), interpolation=cv2.INTER_NEAREST
    ).astype(bool)

    return full.astype(np.float32), corner_mask, {
        "points": int(d.size), "wall_offset_m": round(wall_offset, 2),
        "cells": int(valid.sum()), "cells_total": int(valid.size),
        "corner_cells_pinned": int(unreliable.sum()),
        "corner_px_fraction": round(float(corner_mask.mean()), 4),
    }


def rectify_images_height_field(
    reconstruction: pycolmap.Reconstruction,
    plane: FacadePlane,
    images_dir: str | Path,
    offset_map: np.ndarray,
    roi_margin_px: int = 200,
) -> tuple[dict[str, WarpedImage], tuple[int, int], dict[str, SourceTransform]]:
    """Same contract as rectification.rectify_images, but each canvas pixel is
    sampled through the height field instead of the flat plane.

    `source_transforms` deliberately keeps the flat-plane homography: crack
    detection maps polygons from raw photos to canvas coordinates through it,
    and that contract is unchanged here."""
    canvas_w = max(1, int(round(plane.width_m * plane.px_per_m)))
    canvas_h = max(1, int(round(plane.height_m * plane.px_per_m)))
    n_in = plane_normal_into_wall(plane)
    e_u_px = plane.e_u / plane.px_per_m
    e_v_px = plane.e_v / plane.px_per_m

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
            raw = cv2.undistort(raw, K, np.array([float(cam.params[3]), 0.0, 0.0, 0.0], dtype=np.float64))

        pose = img.cam_from_world()
        R = pose.rotation.matrix()
        t = np.asarray(pose.translation)
        H_plane = _camera_to_facade_homography(K, R, t, plane)

        src_h, src_w = raw.shape[:2]
        corners = cv2.perspectiveTransform(
            np.array([[0, 0], [src_w, 0], [src_w, src_h], [0, src_h]], dtype=np.float64).reshape(-1, 1, 2), H_plane
        ).reshape(-1, 2)
        x0 = max(0, int(np.floor(corners[:, 0].min())) - roi_margin_px)
        y0 = max(0, int(np.floor(corners[:, 1].min())) - roi_margin_px)
        x1 = min(canvas_w, int(np.ceil(corners[:, 0].max())) + roi_margin_px)
        y1 = min(canvas_h, int(np.ceil(corners[:, 1].max())) + roi_margin_px)
        if x1 <= x0 or y1 <= y0:
            continue

        ys, xs = np.mgrid[y0:y1, x0:x1]
        d = offset_map[y0:y1, x0:x1].astype(np.float64)
        X = (
            plane.origin[None, None, :]
            + xs[..., None] * e_u_px[None, None, :]
            + ys[..., None] * e_v_px[None, None, :]
            + d[..., None] * n_in[None, None, :]
        )
        pc = X @ R.T + t
        z = pc[..., 2]
        safe_z = np.where(z > 1e-6, z, 1.0)
        map_x = (K[0, 0] * pc[..., 0] / safe_z + K[0, 2]).astype(np.float32)
        map_y = (K[1, 1] * pc[..., 1] / safe_z + K[1, 2]).astype(np.float32)
        inside = (z > 1e-6) & (map_x >= 0) & (map_x <= src_w - 1) & (map_y >= 0) & (map_y <= src_h - 1)
        if not inside.any():
            continue

        warped_img = cv2.remap(raw, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        warped[image_id] = WarpedImage(
            image=warped_img, mask=(inside.astype(np.uint8) * 255), corner=(x0, y0), size=(x1 - x0, y1 - y0)
        )
        source_transforms[image_id] = SourceTransform(H=H_plane, width=src_w, height=src_h)

    return warped, (canvas_w, canvas_h), source_transforms


def rectify_images_hybrid(
    reconstruction: pycolmap.Reconstruction,
    plane: FacadePlane,
    images_dir: str | Path,
    offset_map: np.ndarray,
    corner_mask: np.ndarray,
) -> tuple[dict[str, WarpedImage], tuple[int, int], dict[str, SourceTransform]]:
    """Height-field warp everywhere, except `corner_mask` canvas pixels (real
    folds/pilasters -- see build_offset_map) which reuse the ordinary flat-
    plane warp instead. Depth-along-one-normal has no valid answer right at a
    fold (a face pointing a different way than the fitted plane), and the
    height-field per-pixel projection is hypersensitive there (near-grazing
    incidence), producing a jagged tear worse than the flat baseline -- see
    build_offset_map's docstring for the full story. The flat warp has no such
    per-pixel sensitivity (one fixed homography per image), so it stays the
    better answer specifically in that band; everywhere else the height field's
    parallax correction (its actual point) still applies."""
    from src.geometry.rectification import rectify_images as rectify_images_flat

    flat_warped, canvas_size, source_transforms = rectify_images_flat(reconstruction, plane, images_dir)
    hf_warped, hf_canvas_size, _ = rectify_images_height_field(reconstruction, plane, images_dir, offset_map)
    assert canvas_size == hf_canvas_size, "flat and height-field rectification must share one canvas"
    canvas_w, canvas_h = canvas_size

    warped: dict[str, WarpedImage] = {}
    for image_id in set(flat_warped) | set(hf_warped):
        flat_w = flat_warped.get(image_id)
        hf_w = hf_warped.get(image_id)
        if hf_w is None:
            warped[image_id] = flat_w
            continue
        if flat_w is None:
            warped[image_id] = hf_w
            continue

        x0 = min(flat_w.corner[0], hf_w.corner[0])
        y0 = min(flat_w.corner[1], hf_w.corner[1])
        x1 = max(flat_w.corner[0] + flat_w.size[0], hf_w.corner[0] + hf_w.size[0])
        y1 = max(flat_w.corner[1] + flat_w.size[1], hf_w.corner[1] + hf_w.size[1])
        w, h = x1 - x0, y1 - y0

        image = np.zeros((h, w, 3), dtype=np.uint8)
        mask = np.zeros((h, w), dtype=np.uint8)
        # fx/fy, hx/hy: each source's own top-left within the shared (x0, y0)-
        # origin union box -- every slice below must be taken relative to ITS
        # OWN corner, not the union box's, or content lands at the wrong offset
        # (confirmed real, 2026-09-22: corner_mask was sliced from the union's
        # origin but then indexed as if it started at the flat sub-image's own
        # origin, silently pasting flat pixels at the wrong canvas location --
        # visible as new black gaps and a worse tear than either warp alone).
        fx, fy = flat_w.corner[0] - x0, flat_w.corner[1] - y0
        hx, hy = hf_w.corner[0] - x0, hf_w.corner[1] - y0
        image[hy : hy + hf_w.size[1], hx : hx + hf_w.size[0]] = hf_w.image
        mask[hy : hy + hf_w.size[1], hx : hx + hf_w.size[0]] = hf_w.mask

        fw_, fh_ = flat_w.size
        flat_region = corner_mask[
            flat_w.corner[1] : flat_w.corner[1] + fh_, flat_w.corner[0] : flat_w.corner[0] + fw_
        ]
        sub_image = image[fy : fy + fh_, fx : fx + fw_]
        sub_mask = mask[fy : fy + fh_, fx : fx + fw_]
        take = flat_region & (flat_w.mask > 0)
        sub_image[take] = flat_w.image[take]
        sub_mask[take] = 255

        warped[image_id] = WarpedImage(image=image, mask=mask, corner=(x0, y0), size=(w, h))

    return warped, canvas_size, source_transforms
