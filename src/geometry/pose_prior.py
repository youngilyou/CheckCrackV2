"""GPS + gimbal pose-prior facade rectification.

Used when COLMAP's reconstruction disagrees with the photos' own GPS (see
rectification.gps_alignment_residuals_m). Confirmed real, 2026-09-21 (LEFT
facade): a narrow wall shot as a few vertical passes leaves the cameras almost
collinear and the scene a single repetitive plane -- a degenerate SfM
configuration whose poses/scale come out wrong (GPS residual mean 7 m, camera-
facing-down collapse) even at 0.6 px reprojection error. In that situation the
DJI metadata itself is the better pose source: camera centre from GPS, viewing
direction from gimbal yaw/pitch/roll, intrinsics from the XMP-calibrated focal
length. The one quantity metadata cannot supply is the wall distance, so it is
measured from image disparity between same-altitude photos taken from
different lateral positions (disparity = f * baseline / distance), with the
baseline taken from GPS.
"""

from __future__ import annotations

import itertools
import math
import random
from pathlib import Path

import cv2
import numpy as np
import pyproj

from src.common.imageio import imread_unicode
from src.common.types import ImageMetadata
from src.geometry.rectification import (
    FacadePlane,
    _camera_to_facade_homography,
    blend_rectified_images,
)
from src.stitching.mosaic import MosaicResult
from src.stitching.warp import SourceTransform, WarpedImage

MAX_CANVAS_SIDE_PX = 20000


def _camera_axes(yaw_deg: float, pitch_deg: float, roll_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(right, down, forward) unit vectors in ENU for a DJI gimbal reading
    (yaw clockwise from north, pitch positive = up)."""
    yaw, pitch, roll = math.radians(yaw_deg), math.radians(pitch_deg), math.radians(roll_deg)
    forward = np.array([math.sin(yaw) * math.cos(pitch), math.cos(yaw) * math.cos(pitch), math.sin(pitch)])
    right0 = np.array([math.cos(yaw), -math.sin(yaw), 0.0])
    down0 = np.cross(forward, right0)
    right = right0 * math.cos(roll) + down0 * math.sin(roll)
    down = down0 * math.cos(roll) - right0 * math.sin(roll)
    return right, down, forward


def _usable(meta: ImageMetadata) -> bool:
    return (
        meta.gps.latitude is not None
        and meta.gps.longitude is not None
        and meta.gps.altitude_m is not None
        and meta.gimbal_pose.yaw_deg is not None
        and meta.gimbal_pose.pitch_deg is not None
        and meta.camera.calibrated_focal_length_px is not None
        and meta.camera.calibrated_optical_center_x_px is not None
        and meta.camera.calibrated_optical_center_y_px is not None
    )


def _centers_enu(images: list[ImageMetadata], utm_epsg: int) -> dict[str, np.ndarray]:
    transformer = pyproj.Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    out = {}
    for m in images:
        x, y = transformer.transform(m.gps.longitude, m.gps.latitude)
        out[m.image_id] = np.array([x, y, m.gps.altitude_m])
    return out


def _mean_horizontal_forward(images: list[ImageMetadata]) -> np.ndarray:
    v = np.zeros(2)
    for m in images:
        yaw = math.radians(m.gimbal_pose.yaw_deg)
        v += np.array([math.sin(yaw), math.cos(yaw)])
    v /= np.linalg.norm(v)
    return np.array([v[0], v[1], 0.0])


def estimate_wall_distance_m(
    images: list[ImageMetadata],
    centers: dict[str, np.ndarray],
    max_pairs: int = 24,
    min_valid_pairs: int = 4,
    max_baseline_m: float = 6.0,
) -> tuple[float, int] | None:
    """Wall distance from cross-position, same-altitude photo pairs. Returns
    (distance_m, valid_pair_count) or None if too few pairs pass.

    A pair only counts when it looks like a pure translation parallel to the
    wall (homography scale ~1, disparity sign opposite to the GPS baseline,
    plenty of RANSAC inliers). Pairs dominated by distant background (sky/
    hills above the roofline) yield too-large distances, so the lower quartile
    is used -- contamination only ever biases the estimate upward."""
    focal = images[0].camera.calibrated_focal_length_px
    right, _down, forward_axis = _camera_axes(
        float(np.mean([m.gimbal_pose.yaw_deg for m in images])), 0.0, 0.0
    )
    down = np.array([0.0, 0.0, -1.0])

    candidates = []
    for a, b in itertools.combinations(images, 2):
        d = centers[b.image_id] - centers[a.image_id]
        dx, dy, dz = float(d @ right), float(d @ down), float(d @ forward_axis)
        if abs(dy) < 0.7 and 1.5 < abs(dx) < max_baseline_m and abs(dz) < 0.4:
            candidates.append((a, b, dx))
    random.Random(0).shuffle(candidates)

    sift = cv2.SIFT_create(4000)
    matcher = cv2.BFMatcher()
    cache: dict[str, tuple] = {}

    def features(meta: ImageMetadata):
        if meta.image_id not in cache:
            gray = imread_unicode(meta.file_path, cv2.IMREAD_GRAYSCALE)
            gray = cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
            cache[meta.image_id] = sift.detectAndCompute(gray, None)
        return cache[meta.image_id]

    distances: list[float] = []
    for a, b, dx in candidates[:max_pairs]:
        ka, da = features(a)
        kb, db = features(b)
        if da is None or db is None or len(ka) < 50 or len(kb) < 50:
            continue
        good = [m for m, n in matcher.knnMatch(da, db, k=2) if m.distance < 0.75 * n.distance]
        if len(good) < 50:
            continue
        pa = np.float32([ka[m.queryIdx].pt for m in good]) * 2.0
        pb = np.float32([kb[m.trainIdx].pt for m in good]) * 2.0
        H, mask = cv2.findHomography(pa, pb, cv2.RANSAC, 5.0)
        if H is None or int(mask.sum()) < 300:
            continue
        tx = float(H[0, 2])
        if abs(float(H[0, 0]) - 1.0) > 0.03 or abs(tx) < 50.0 or tx * dx >= 0:
            continue
        distances.append(focal * abs(dx) / abs(tx))

    if len(distances) < min_valid_pairs:
        return None
    return float(np.percentile(distances, 25)), len(distances)


def rectify_from_pose_prior(
    facade_id: str,
    images: list[ImageMetadata],
    utm_epsg: int,
    cfg,
    px_per_m: float = 100.0,
    padding_m: float = 0.5,
) -> tuple[FacadePlane, MosaicResult, dict] | None:
    """Full pose-prior rectified mosaic, or None if it can't be built (missing
    metadata, wall distance not measurable, absurd canvas). Third return value
    is a small report dict (wall distance, valid pair count)."""
    usable = [m for m in images if _usable(m)]
    if len(usable) < 4:
        return None
    centers = _centers_enu(usable, utm_epsg)

    measured = estimate_wall_distance_m(usable, centers)
    if measured is None:
        return None
    distance_m, valid_pairs = measured

    forward_h = _mean_horizontal_forward(usable)
    normal = -forward_h
    e_v = np.array([0.0, 0.0, -1.0])
    e_u = np.cross(normal, e_v)
    e_u /= np.linalg.norm(e_u)
    plane_point = np.mean([centers[m.image_id] for m in usable], axis=0) + distance_m * forward_h

    def pose(meta: ImageMetadata):
        right, down, forward = _camera_axes(
            meta.gimbal_pose.yaw_deg, meta.gimbal_pose.pitch_deg, meta.gimbal_pose.roll_deg or 0.0
        )
        R = np.vstack([right, down, forward])
        C = centers[meta.image_id]
        return R, -R @ C

    def intrinsics(meta: ImageMetadata) -> np.ndarray:
        f = meta.camera.calibrated_focal_length_px
        return np.array(
            [[f, 0, meta.camera.calibrated_optical_center_x_px],
             [0, f, meta.camera.calibrated_optical_center_y_px],
             [0, 0, 1.0]]
        )

    us: list[float] = []
    vs: list[float] = []
    for meta in usable:
        R, _t = pose(meta)
        K = intrinsics(meta)
        C = centers[meta.image_id]
        w, h = meta.width, meta.height
        for x, y in ((0, 0), (w, 0), (w, h), (0, h)):
            ray = R.T @ np.linalg.solve(K, np.array([x, y, 1.0]))
            denom = float(ray @ normal)
            if denom >= -1e-6:
                continue
            X = C + ray * (float((plane_point - C) @ normal) / denom)
            us.append(float((X - plane_point) @ e_u))
            vs.append(float((X - plane_point) @ e_v))
    if not us:
        return None

    u_min, u_max, v_min, v_max = min(us), max(us), min(vs), max(vs)
    width_m = (u_max - u_min) + 2 * padding_m
    height_m = (v_max - v_min) + 2 * padding_m
    if width_m * px_per_m > MAX_CANVAS_SIDE_PX or height_m * px_per_m > MAX_CANVAS_SIDE_PX:
        return None
    plane = FacadePlane(
        origin=plane_point + e_u * (u_min - padding_m) + e_v * (v_min - padding_m),
        e_u=e_u, e_v=e_v, px_per_m=px_per_m, width_m=width_m, height_m=height_m,
        scale_source="gps_pose_prior",
    )

    canvas_w = max(1, int(round(width_m * px_per_m)))
    canvas_h = max(1, int(round(height_m * px_per_m)))
    warped: dict[str, WarpedImage] = {}
    source_transforms: dict[str, SourceTransform] = {}
    for meta in usable:
        raw = imread_unicode(meta.file_path, cv2.IMREAD_COLOR)
        if raw is None:
            continue
        R, t = pose(meta)
        H = _camera_to_facade_homography(intrinsics(meta), R, t, plane)
        src_h, src_w = raw.shape[:2]
        corners = cv2.perspectiveTransform(
            np.array([[0, 0], [src_w, 0], [src_w, src_h], [0, src_h]], dtype=np.float64).reshape(-1, 1, 2), H
        ).reshape(-1, 2)
        x0 = max(0, int(np.floor(corners[:, 0].min())))
        y0 = max(0, int(np.floor(corners[:, 1].min())))
        x1 = min(canvas_w, int(np.ceil(corners[:, 0].max())))
        y1 = min(canvas_h, int(np.ceil(corners[:, 1].max())))
        if x1 <= x0 or y1 <= y0:
            continue
        local_w, local_h = x1 - x0, y1 - y0
        H_local = np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1]], dtype=np.float64) @ H
        warped_img = cv2.warpPerspective(raw, H_local, (local_w, local_h), flags=cv2.INTER_LINEAR)
        mask = cv2.warpPerspective(
            np.full((src_h, src_w), 255, dtype=np.uint8), H_local, (local_w, local_h), flags=cv2.INTER_NEAREST
        )
        warped[meta.image_id] = WarpedImage(image=warped_img, mask=mask, corner=(x0, y0), size=(local_w, local_h))
        source_transforms[meta.image_id] = SourceTransform(H=H, width=src_w, height=src_h)

    if len(warped) < 4:
        return None
    result = blend_rectified_images(facade_id, warped, (canvas_w, canvas_h), source_transforms, plane, cfg, None)
    return plane, result, {"wall_distance_m": round(distance_m, 2), "distance_valid_pairs": valid_pairs}
