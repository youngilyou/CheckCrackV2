"""Rebuild `{facade_id}_depth_mapping.json` for an output folder that finished BEFORE the pipeline
started writing it (runs up to V008). New runs write it themselves right after Dense Stereo
(pipeline/runner.py::_run_dense_hybrid_stage).

The sidecar holds the Sim3d (native COLMAP frame -> UTM) and the facade plane the run used. Both are
deterministic functions of the stage-1 reconstruction + the photos' EXIF GPS, so they are recomputed
here exactly the way the pipeline did (align_reconstruction_to_utm + facade_plane_from_reconstruction)
-- and then verified: the flat homography of a sample photo must reproduce the plane-ray
intersection of this sidecar's own geometry, otherwise the recomputed plane is not the one the
mosaic was drawn on and nothing is written.

Usage:
    python tools/make_depth_mapping_sidecar.py <output_dir> <images_dir> [facade_id]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pycolmap  # noqa: E402

from src.capture.image_catalog import build_catalog  # noqa: E402
from src.geometry.depth_mapping import DepthCanvasMapper, _undistort_normalized, write_sidecar  # noqa: E402
from src.geometry.rectification import (  # noqa: E402
    align_reconstruction_to_utm,
    estimate_utm_epsg,
    facade_plane_from_reconstruction,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir")
    parser.add_argument("images_dir")
    parser.add_argument("facade_id", nargs="?", default=None)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    facade_id = args.facade_id or Path(args.images_dir).name
    native_dir = output_dir / "colmap_stage1" / "sparse" / "best"
    dense_dir = output_dir / "colmap_dense" / "dense"
    if not native_dir.exists() or not (dense_dir / "stereo" / "depth_maps").exists():
        sys.exit(f"need {native_dir} and {dense_dir}/stereo/depth_maps")

    catalog = build_catalog(args.images_dir)
    by_id = {m.image_id: m for m in catalog}
    aligned = pycolmap.Reconstruction(str(native_dir))
    sim3d = align_reconstruction_to_utm(aligned, by_id, estimate_utm_epsg(catalog))
    if sim3d is None:
        sys.exit("GPS alignment failed -- cannot build a UTM-frame depth mapping")
    plane = facade_plane_from_reconstruction(aligned)
    print(f"plane canvas {plane.width_m * plane.px_per_m:.0f} x {plane.height_m * plane.px_per_m:.0f} px, "
          f"sim3d scale {float(sim3d.scale):.5f}")

    mapper = DepthCanvasMapper(
        native_sparse_dir=native_dir, dense_dir=dense_dir,
        sim3d_scale=float(sim3d.scale), sim3d_rotation=np.asarray(sim3d.rotation.matrix()),
        sim3d_translation=np.asarray(sim3d.translation),
        plane_origin=plane.origin, plane_e_u=plane.e_u, plane_e_v=plane.e_v, px_per_m=plane.px_per_m,
    )

    # Verification (depth-independent): the run's own flat homography IS the viewing-ray / plane
    # intersection, so this sidecar's plane + Sim3d + intrinsics must reproduce it to sub-pixel.
    homographies = json.loads((output_dir / f"{facade_id}_homographies_colmap.json").read_text(encoding="utf-8"))
    rng = np.random.default_rng(0)
    worst = 0.0
    checked = 0
    for image_id in list(homographies)[:: max(1, len(homographies) // 25)]:
        if not mapper.has_image(image_id):
            continue
        entry = homographies[image_id]
        H = np.asarray(entry["H"], dtype=np.float64)
        pts = np.column_stack([rng.uniform(0.05, 0.95, 300) * entry["width"], rng.uniform(0.05, 0.95, 300) * entry["height"]])
        ray = mapper.map_points_on_plane(image_id, pts)
        # H is defined on the UNDISTORTED image (rectification.py), so feed it undistorted pixels
        # (cv2.undistort with default newCameraMatrix keeps K: pixel = f * x_n + c).
        g = mapper._geom[image_id]
        xn = _undistort_normalized(g.raw_model, g.raw_params, pts)
        und = np.column_stack([g.raw_params[0] * xn[:, 0] + g.raw_params[1], g.raw_params[0] * xn[:, 1] + g.raw_params[2]])
        p = np.column_stack([und, np.ones(len(und))]) @ H.T
        flat = p[:, :2] / p[:, 2:3]
        worst = max(worst, float(np.nanpercentile(np.linalg.norm(ray - flat, axis=1), 99)))
        checked += 1
    if checked == 0:
        sys.exit("verification found no usable photo -- nothing written")
    print(f"verification: {checked} photos, worst 99th-percentile |ray-plane - flat homography| = {worst:.3f} canvas px")
    if worst > 1.0:
        sys.exit("recomputed plane/sim3d does NOT reproduce the run's homographies -- nothing written")

    path = write_sidecar(output_dir, facade_id, native_sparse_dir=native_dir, dense_dir=dense_dir, sim3d=sim3d, plane=plane)
    print("wrote", path)


if __name__ == "__main__":
    main()
