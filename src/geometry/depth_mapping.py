"""Depth-aware raw-photo-pixel -> facade-canvas mapping.

Why this exists (2026-09-26, 사용자 실사용 발견 -- V008 FRONT, "원본 크랙 위치와 스티칭 크랙 위치
오차가 엄청 큼, 사업 불가 수준"): crack detection runs on the ORIGINAL photos
(src/crack/raw_pipeline.py), and each detected polygon used to be placed on the canvas with that
photo's flat-plane homography (rectification.py::_camera_to_facade_homography), i.e. as if every
pixel lay exactly ON the fitted facade plane. A real wall is not one plane (window recesses,
panel relief, balconies, vents, pipes), so a point that sits d meters off the plane lands on the
canvas at a spot shifted by roughly d * tan(view angle) -- and that shift DIFFERS from photo to
photo. Measured on V008: two photos of the same wall content disagree by a median ~10 px
(= ~10 cm, up to 20+ cm; canvas 1 cm/px) once both are placed by their homographies.
Meanwhile the displayed dense-stereo mosaic (dense_stereo.py) is rendered from REAL 3D
points, so it is parallax-correct -- a crack placed by the flat homography does not sit where the
same content appears in the displayed mosaic, and the same physical crack seen by two photos is
placed in two different spots (breaking cross-photo merge as well).

Fix: place each raw pixel by its own measured depth. COLMAP patch-match already produced a
per-photo depth map (`dense/stereo/depth_maps/<name>.geometric.bin`) for every registered photo.
raw pixel -> normalized camera ray (lens undistortion) -> undistorted-image pixel -> depth ->
3D point in COLMAP's native frame -> UTM (Sim3d) -> facade plane (u, v) -> canvas pixel. This is
exactly the projection dense_stereo._render_dense_canvas applies to fused points, so a crack
lands where the displayed dense mosaic draws that same surface.

No new pixel is ever invented: a raw pixel whose depth is missing/invalid (textureless wall,
sky, reflections, or > WALL_BAND_M from the plane) is reported invalid, and callers fall back to
the flat homography for it (explicitly flagged), never to a guess."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SIDECAR_SUFFIX = "_depth_mapping.json"
DEFAULT_WALL_BAND_M = 3.0  # same clutter band dense_stereo.WALL_BAND_M uses


def read_colmap_array(path: str | Path) -> np.ndarray:
    """COLMAP dense depth/normal map (.bin, "width&height&channels&" ascii header then float32,
    column-major). Returns (H, W) for one channel, (H, W, C) otherwise."""
    with open(path, "rb") as f:
        header = b""
        delimiters = 0
        while delimiters < 3:
            byte = f.read(1)
            if not byte:
                raise ValueError(f"truncated COLMAP array header: {path}")
            header += byte
            if byte == b"&":
                delimiters += 1
        width, height, channels = (int(x) for x in header.decode("ascii").strip("&").split("&"))
        data = np.fromfile(f, dtype=np.float32)
    array = data.reshape((width, height, channels), order="F")
    array = np.transpose(array, (1, 0, 2))
    return array.squeeze()


@dataclass
class _ImageGeometry:
    raw_model: str
    raw_params: np.ndarray  # raw (distorted) camera params, COLMAP order
    und_fx: float
    und_fy: float
    und_cx: float
    und_cy: float
    und_width: int
    und_height: int
    R: np.ndarray  # native cam_from_world rotation (3,3)
    t: np.ndarray  # native cam_from_world translation (3,)


def _undistort_normalized(model: str, params: np.ndarray, xy: np.ndarray) -> np.ndarray:
    """Raw distorted pixel (N,2) -> undistorted normalized image coordinates (N,2)."""
    if model in ("SIMPLE_RADIAL", "SIMPLE_RADIAL_FISHEYE"):
        f, cx, cy, k = params[0], params[1], params[2], params[3]
        fx = fy = f
        k1, k2 = k, 0.0
    elif model == "RADIAL":
        f, cx, cy, k1, k2 = params[0], params[1], params[2], params[3], params[4]
        fx = fy = f
    elif model == "PINHOLE":
        fx, fy, cx, cy = params[0], params[1], params[2], params[3]
        k1 = k2 = 0.0
    elif model == "SIMPLE_PINHOLE":
        f, cx, cy = params[0], params[1], params[2]
        fx = fy = f
        k1 = k2 = 0.0
    else:
        raise NotImplementedError(f"camera model {model} not supported by depth_mapping")
    xd = (xy[:, 0] - cx) / fx
    yd = (xy[:, 1] - cy) / fy
    xu, yu = xd.copy(), yd.copy()
    if k1 != 0.0 or k2 != 0.0:
        for _ in range(12):  # fixed-point inversion of x_d = x_u * (1 + k1 r^2 + k2 r^4)
            r2 = xu * xu + yu * yu
            factor = 1.0 + k1 * r2 + k2 * r2 * r2
            xu = xd / factor
            yu = yd / factor
    return np.column_stack([xu, yu])


class DepthCanvasMapper:
    """raw-photo pixel -> canvas pixel using each photo's own COLMAP depth map."""

    def __init__(
        self,
        native_sparse_dir: str | Path,
        dense_dir: str | Path,
        sim3d_scale: float,
        sim3d_rotation: np.ndarray,
        sim3d_translation: np.ndarray,
        plane_origin: np.ndarray,
        plane_e_u: np.ndarray,
        plane_e_v: np.ndarray,
        px_per_m: float,
        wall_band_m: float = DEFAULT_WALL_BAND_M,
    ) -> None:
        import pycolmap

        self.dense_dir = Path(dense_dir)
        self.s = float(sim3d_scale)
        self.Rs = np.asarray(sim3d_rotation, dtype=np.float64)
        self.Ts = np.asarray(sim3d_translation, dtype=np.float64)
        self.origin = np.asarray(plane_origin, dtype=np.float64)
        self.e_u = np.asarray(plane_e_u, dtype=np.float64)
        self.e_v = np.asarray(plane_e_v, dtype=np.float64)
        normal = np.cross(self.e_u, self.e_v)
        self.normal = normal / np.linalg.norm(normal)
        self.px_per_m = float(px_per_m)
        self.wall_band_m = float(wall_band_m)

        native = pycolmap.Reconstruction(str(native_sparse_dir))
        dense = pycolmap.Reconstruction(str(self.dense_dir / "sparse"))
        raw_by_name = {Path(i.name).stem: i for i in native.images.values()}
        self._geom: dict[str, _ImageGeometry] = {}
        for img in dense.images.values():
            stem = Path(img.name).stem
            raw_img = raw_by_name.get(stem)
            if raw_img is None:
                continue
            und_cam = img.camera
            if und_cam.model.name != "PINHOLE":
                continue
            pose = img.cam_from_world()
            # copy=True is REQUIRED: np.asarray() of a pycolmap array can be a zero-copy VIEW into the
            # Reconstruction's C++ memory, which is freed when `native`/`dense` go out of scope at the
            # end of __init__ -- the views then read recycled memory (seen as NaN / garbage, only
            # sometimes, e.g. inside the crack-detection process after a large model was loaded).
            self._geom[stem] = _ImageGeometry(
                raw_model=str(raw_img.camera.model.name),
                raw_params=np.array(raw_img.camera.params, dtype=np.float64, copy=True),
                und_fx=float(und_cam.params[0]), und_fy=float(und_cam.params[1]),
                und_cx=float(und_cam.params[2]), und_cy=float(und_cam.params[3]),
                und_width=int(und_cam.width), und_height=int(und_cam.height),
                R=np.array(pose.rotation.matrix(), dtype=np.float64, copy=True),
                t=np.array(pose.translation, dtype=np.float64, copy=True),
            )
        self._depth_cache: dict[str, np.ndarray | None] = {}

    def has_image(self, image_id: str) -> bool:
        return image_id in self._geom and self._depth(image_id) is not None

    def _depth(self, image_id: str) -> np.ndarray | None:
        if image_id in self._depth_cache:
            return self._depth_cache[image_id]
        if len(self._depth_cache) >= 6:  # bounded: a 2640-wide float32 depth map is ~20 MB
            self._depth_cache.pop(next(iter(self._depth_cache)))
        depth = None
        path = self.dense_dir / "stereo" / "depth_maps" / f"{image_id}.JPG.geometric.bin"
        if not path.exists():
            candidates = list((self.dense_dir / "stereo" / "depth_maps").glob(f"{image_id}.*.geometric.bin"))
            path = candidates[0] if candidates else path
        if path.exists():
            try:
                depth = self._fill_small_holes(
                    read_colmap_array(path).astype(np.float32), self.fill_max_px,
                    self.wide_fill_max_px, self.fill_agree_tolerance_m,
                )
            except (OSError, ValueError):
                depth = None
        self._depth_cache[image_id] = depth
        return depth

    @staticmethod
    def _fill_small_holes(
        depth: np.ndarray, max_px: float, wide_max_px: float = 0.0, agree_tolerance_m: float = 0.3,
    ) -> np.ndarray:
        """COLMAP's geometric depth map zeroes every pixel that fails the multi-view consistency
        check -- on FRONT ~35-40% of a photo, concentrated on textureless / weathered wall, exactly
        where cracks and stains are. A wall's depth is locally smooth, so a hole is filled with the
        NEAREST measured depth, but only within `max_px` depth-map pixels (1 depth px = 2 raw px at
        the 2640-wide undistorted size): anything farther from a real measurement stays invalid and
        the caller falls back to the flat homography, flagged -- never a guess.

        `wide_max_px` (2026-09-28, 사용자 제안 -- BACK 실사용 "측정 불가" 지점 여러 건에서 확인): a
        gap up to `max_px` from its single NEAREST valid neighbour is one thing, but a wider gap
        BRACKETED by valid depth on two independent sides is a stronger, differently-justified case --
        if the two sides agree, the surface between them is almost certainly one continuous real wall
        patch (not a guess: it's two real measurements bounding the same value), and if they disagree
        it's a real discontinuity (wall meeting sky/parapet) that must NOT be bridged. Measured on
        DJI_0067 (BACK): pixels with a valid neighbour on both sides split cleanly in two -- 91,089
        agreed within 3mm (median) / 9mm (p90), 52,041 disagreed by 12.9m (median) / 19.8m (p90) -- no
        ambiguous middle ground, so `agree_tolerance_m` only has to sit anywhere between those two
        clusters. Checks vertical (column) and horizontal (row) bracketing independently and accepts
        either; a pixel with only a one-sided (or no) valid neighbour within `wide_max_px` is left
        exactly as `max_px`-only filling would leave it."""
        invalid = ~(depth > 0)
        if not invalid.any() or invalid.all():
            return depth
        from scipy import ndimage

        out = depth.astype(np.float32).copy()
        if max_px > 0:
            dist, (iy, ix) = ndimage.distance_transform_edt(invalid, return_indices=True)
            near_ok = invalid & (dist <= max_px)
            out[near_ok] = depth[iy, ix][near_ok]

        remaining = ~(out > 0)
        if wide_max_px > max_px and remaining.any():
            def forward_fill(arr: np.ndarray, axis: int) -> tuple[np.ndarray, np.ndarray]:
                """(value, gap_px) of the nearest valid pixel at an EARLIER index along `axis` in
                `arr` (NaN / 0 where none yet), via one cumulative forward pass. Pure-Python loop over
                one axis length (~2000-2600 for these depth maps), each step one vectorised row/column
                op -- `bracket` below flips `arr` to get the "later index" side from the same helper."""
                val = np.where(arr > 0, arr, np.nan)
                gap = np.zeros(val.shape, dtype=np.float32)
                for i in range(1, val.shape[axis]):
                    prev_v, cur_v = np.take(val, i - 1, axis=axis), np.take(val, i, axis=axis)
                    hole = np.isnan(cur_v)
                    new_v = np.where(hole, prev_v, cur_v)
                    new_g = np.where(hole, np.take(gap, i - 1, axis=axis) + 1, 0.0)
                    if axis == 0:
                        val[i], gap[i] = new_v, new_g
                    else:
                        val[:, i], gap[:, i] = new_v, new_g
                return val, gap

            def bracket(axis: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
                """Nearest valid neighbour on EACH side along `axis`: (lo_val, lo_gap, hi_val, hi_gap)
                -- lo = earlier index (e.g. above/left), hi = later index (below/right)."""
                lo_val, lo_gap = forward_fill(out, axis)
                flip = tuple(slice(None, None, -1) if a == axis else slice(None) for a in range(out.ndim))
                hi_val_rev, hi_gap_rev = forward_fill(out[flip], axis)
                return lo_val, lo_gap, hi_val_rev[flip], hi_gap_rev[flip]

            def accept(lo_val, lo_gap, hi_val, hi_gap):
                have_both = ~np.isnan(lo_val) & ~np.isnan(hi_val) & ((lo_gap + hi_gap) <= wide_max_px)
                agree = have_both & (np.abs(lo_val - hi_val) <= agree_tolerance_m)
                return agree, (lo_val + hi_val) / 2.0

            v_ok, v_mean = accept(*bracket(0))
            h_ok, h_mean = accept(*bracket(1))

            fill_v = remaining & v_ok
            out[fill_v] = v_mean[fill_v]
            fill_h = remaining & ~fill_v & h_ok
            out[fill_h] = h_mean[fill_h]

        return out

    fill_max_px: float = 40.0       # ~20 cm of wall at ~0.5 cm per depth px on this FRONT
    wide_fill_max_px: float = 150.0  # ~75 cm total gap (both sides combined) -- see _fill_small_holes
    fill_agree_tolerance_m: float = 0.3  # real disagreement seen was 12m+; this only has to clear that

    def map_points(
        self, image_id: str, xy_raw: np.ndarray, depth_radius: int = 2,
    ) -> tuple[np.ndarray, np.ndarray]:
        """(N,2) raw pixels -> (canvas_xy (N,2) float, valid (N,) bool). Invalid rows are NaN."""
        xy_raw = np.asarray(xy_raw, dtype=np.float64).reshape(-1, 2)
        n = len(xy_raw)
        out = np.full((n, 2), np.nan)
        valid = np.zeros(n, dtype=bool)
        geom = self._geom.get(image_id)
        depth = self._depth(image_id) if geom is not None else None
        if geom is None or depth is None or n == 0:
            return out, valid

        xn = _undistort_normalized(geom.raw_model, geom.raw_params, xy_raw)
        ux = geom.und_fx * xn[:, 0] + geom.und_cx
        uy = geom.und_fy * xn[:, 1] + geom.und_cy

        dh, dw = depth.shape[:2]
        z = self._sample_depth(
            depth, ux * (dw / geom.und_width), uy * (dh / geom.und_height), radius=depth_radius,
        )
        ok = np.isfinite(z) & (z > 0)
        if not ok.any():
            return out, valid

        cam = np.column_stack([xn[ok, 0] * z[ok], xn[ok, 1] * z[ok], z[ok]])
        world = (cam - geom.t) @ geom.R  # R^T (Xc - t), row-vector form
        utm = self.s * (world @ self.Rs.T) + self.Ts
        rel = utm - self.origin
        near_wall = np.abs(rel @ self.normal) <= self.wall_band_m
        canvas = np.column_stack([rel @ self.e_u, rel @ self.e_v]) * self.px_per_m

        idx = np.flatnonzero(ok)[near_wall]
        out[idx] = canvas[near_wall]
        valid[idx] = True
        return out, valid

    def map_points_on_plane(self, image_id: str, xy_raw: np.ndarray) -> np.ndarray:
        """Same geometry with NO depth: intersect each pixel's viewing ray with the facade plane.
        This is by construction what the flat homography computes, so it is the independent check
        that the plane / Sim3d / intrinsics this mapper was built with are the ones the run used
        (see tools/make_depth_mapping_sidecar.py). Returns (N,2) canvas px (NaN if no image)."""
        xy_raw = np.asarray(xy_raw, dtype=np.float64).reshape(-1, 2)
        geom = self._geom.get(image_id)
        if geom is None:
            return np.full((len(xy_raw), 2), np.nan)
        xn = _undistort_normalized(geom.raw_model, geom.raw_params, xy_raw)
        center_native = -geom.R.T @ geom.t
        center = self.s * (self.Rs @ center_native) + self.Ts
        dirs_native = np.column_stack([xn, np.ones(len(xn))]) @ geom.R  # R^T [x, y, 1]
        dirs = dirs_native @ self.Rs.T
        denom = dirs @ self.normal
        with np.errstate(all="ignore"):
            lam = ((self.origin - center) @ self.normal) / denom
        pts = center + lam[:, None] * dirs
        rel = pts - self.origin
        return np.column_stack([rel @ self.e_u, rel @ self.e_v]) * self.px_per_m

    @staticmethod
    def _sample_depth(depth: np.ndarray, x: np.ndarray, y: np.ndarray, radius: int = 2) -> np.ndarray:
        """Median of the VALID (>0) depths in a (2r+1)^2 window around each point -- robust to a
        single-pixel dropout on a crack edge, and never averages across an invalid (0) hole."""
        h, w = depth.shape[:2]
        xi = np.clip(np.round(x).astype(np.int64), 0, w - 1)
        yi = np.clip(np.round(y).astype(np.int64), 0, h - 1)
        inside = (x >= 0) & (x <= w - 1) & (y >= 0) & (y <= h - 1)
        stack = []
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                yy = np.clip(yi + dy, 0, h - 1)
                xx = np.clip(xi + dx, 0, w - 1)
                v = depth[yy, xx].astype(np.float64)
                v[v <= 0] = np.nan
                stack.append(v)
        with np.errstate(all="ignore"):
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                med = np.nanmedian(np.stack(stack, axis=0), axis=0)
        # Require a majority of the window to be valid, otherwise this is a hole edge, not a measurement.
        valid_count = np.sum(np.isfinite(np.stack(stack, axis=0)), axis=0)
        med[valid_count < (len(stack) // 2 + 1)] = np.nan
        med[~inside] = np.nan
        return med

    def map_polygon(
        self, image_id: str, polygon_raw_px: np.ndarray, H_flat: np.ndarray | None,
    ) -> tuple[np.ndarray, str]:
        """Polygon (M,2 raw px) -> canvas polygon. Returns (canvas_polygon, method) where method is
        "depth" (every vertex from its depth), "depth+flat" (some vertices had no depth; they use
        the flat homography shifted by the median depth-vs-flat displacement of this polygon's own
        valid vertices, so the shape stays coherent) or "flat" (fewer than half had depth -- pure
        homography, the pre-fix behavior, flagged so the caller/UI can tell)."""
        import cv2

        poly = np.asarray(polygon_raw_px, dtype=np.float64).reshape(-1, 2)
        canvas, valid = self.map_points(image_id, poly)
        flat = None
        if H_flat is not None:
            flat = cv2.perspectiveTransform(poly.reshape(-1, 1, 2), H_flat).reshape(-1, 2)
        if valid.all():
            return canvas, "depth"
        if flat is None:
            return canvas[valid], "depth"  # no fallback available: use only measured vertices
        if valid.sum() * 2 < len(poly):
            return flat, "flat"
        shift = np.median(canvas[valid] - flat[valid], axis=0)
        merged = flat + shift
        merged[valid] = canvas[valid]
        return merged, "depth+flat"


def write_sidecar(
    output_dir: str | Path, facade_id: str, *, native_sparse_dir: str | Path, dense_dir: str | Path,
    sim3d, plane,
) -> Path:
    """Persist everything DepthCanvasMapper needs, next to the mosaic, so tools/detect_cracks_folder.py
    (a separate process that runs after stitching) can rebuild the mapper. Paths are stored relative
    to output_dir when possible."""
    output_dir = Path(output_dir)

    def rel(p: str | Path) -> str:
        p = Path(p)
        try:
            return str(p.resolve().relative_to(output_dir.resolve()))
        except ValueError:
            return str(p)

    data = {
        "version": 1,
        "native_sparse_dir": rel(native_sparse_dir),
        "dense_dir": rel(dense_dir),
        "sim3d": {
            "scale": float(sim3d.scale),
            "rotation": np.asarray(sim3d.rotation.matrix()).tolist(),
            "translation": np.asarray(sim3d.translation).tolist(),
        },
        "plane": {
            "origin": np.asarray(plane.origin).tolist(),
            "e_u": np.asarray(plane.e_u).tolist(),
            "e_v": np.asarray(plane.e_v).tolist(),
            "px_per_m": float(plane.px_per_m),
            "width_m": float(plane.width_m),
            "height_m": float(plane.height_m),
        },
    }
    path = output_dir / f"{facade_id}{SIDECAR_SUFFIX}"
    from src.common.atomic_io import atomic_write_json

    atomic_write_json(path, data)
    return path


def load_mapper_from_sidecar(output_dir: str | Path, facade_id: str) -> DepthCanvasMapper | None:
    """None (never raises) when this run has no sidecar / its dense workspace is gone -- callers then
    keep the pre-fix flat-homography behavior and must say so."""
    output_dir = Path(output_dir)
    path = output_dir / f"{facade_id}{SIDECAR_SUFFIX}"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))

        def resolve(p: str) -> Path:
            q = Path(p)
            return q if q.is_absolute() else output_dir / q

        native_dir, dense_dir = resolve(data["native_sparse_dir"]), resolve(data["dense_dir"])
        if not (native_dir.exists() and (dense_dir / "sparse").exists()
                and (dense_dir / "stereo" / "depth_maps").exists()):
            return None
        plane, sim = data["plane"], data["sim3d"]
        return DepthCanvasMapper(
            native_sparse_dir=native_dir, dense_dir=dense_dir,
            sim3d_scale=sim["scale"], sim3d_rotation=np.array(sim["rotation"]),
            sim3d_translation=np.array(sim["translation"]),
            plane_origin=np.array(plane["origin"]), plane_e_u=np.array(plane["e_u"]),
            plane_e_v=np.array(plane["e_v"]), px_per_m=plane["px_per_m"],
        )
    except (OSError, ValueError, KeyError, NotImplementedError, RuntimeError):
        return None
