"""Rebuild `*_colmap_dense.*` from an existing dense workspace -- minutes instead of the ~6 hours of dense stereo.

Use it after a change to the hybrid merge (src/geometry/dense_stereo.py::build_hybrid_mosaic) to refresh a finished
run without re-running COLMAP: it re-reads the fused point cloud (`colmap_dense/dense/fused.ply`), the flat mosaic
outputs the run already wrote, and `{facade_id}_depth_mapping.json` (Sim3d + facade plane), and rewrites the dense
mosaic, observed mask, quality report and the homography / seam-owner artifacts.

2026-09-27: written to apply the wall-extent fix (flat fill only inside the wall, no roof/terrain/sky patches) to a
finished run (FRONT V010) instead of repeating dense stereo.

The previous dense outputs are copied to `_backup_before_rebuild/` first.

Usage:
    python tools/rebuild_dense_hybrid.py <output_dir> [facade_id]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import cv2

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.common.atomic_io import atomic_write_json  # noqa: E402
from src.common.imageio import imread_unicode, imwrite_unicode  # noqa: E402
from src.common.types import StitchQualityReport  # noqa: E402
from src.geometry.dense_stereo import DenseStereoResult, build_hybrid_mosaic  # noqa: E402
from src.geometry.rectification import FacadePlane  # noqa: E402
from src.stitching.mosaic import MosaicResult  # noqa: E402
from src.stitching.warp import SourceTransform  # noqa: E402

SUFFIXES = (
    "_analysis_colmap_dense.tif", "_visual_colmap_dense.tif", "_observed_mask_colmap_dense.tif",
    "_quality_report_colmap_dense.json", "_homographies_colmap_dense.json",
    "_seam_owner_map_colmap_dense.png", "_seam_owner_index_colmap_dense.json",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("facade_id", nargs="?", default=None)
    args = parser.parse_args()
    out: Path = args.output_dir
    facade_id = args.facade_id or out.parent.name

    sidecar = json.loads((out / f"{facade_id}_depth_mapping.json").read_text(encoding="utf-8"))

    def resolve(p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else out / q

    native_dir, dense_dir = resolve(sidecar["native_sparse_dir"]), resolve(sidecar["dense_dir"])
    fused = dense_dir / "fused.ply"
    if not fused.exists():
        sys.exit(f"missing {fused} -- this run's dense workspace is gone, nothing to rebuild from")

    s = sidecar["sim3d"]
    scale, R, T = float(s["scale"]), np.array(s["rotation"], dtype=np.float64), np.array(s["translation"], dtype=np.float64)
    sim3d = SimpleNamespace(scale=scale, rotation=SimpleNamespace(matrix=lambda: R), translation=T)
    pl = sidecar["plane"]
    plane = FacadePlane(
        origin=np.array(pl["origin"]), e_u=np.array(pl["e_u"]), e_v=np.array(pl["e_v"]),
        px_per_m=float(pl["px_per_m"]), width_m=float(pl["width_m"]), height_m=float(pl["height_m"]),
    )

    # Camera centres/names in the UTM frame (all build_hybrid_mosaic needs from the reconstruction).
    import pycolmap

    native = pycolmap.Reconstruction(str(native_dir))
    images = []
    for img in native.images.values():
        center = np.array(img.projection_center(), dtype=np.float64)
        aligned_center = scale * (R @ center) + T
        images.append(SimpleNamespace(name=img.name, projection_center=(lambda c=aligned_center: c)))
    reconstruction = SimpleNamespace(images={i: im for i, im in enumerate(images)})

    flat_visual = imread_unicode(out / f"{facade_id}_visual_colmap.tif", cv2.IMREAD_COLOR)
    flat_analysis = imread_unicode(out / f"{facade_id}_analysis_colmap.tif", cv2.IMREAD_COLOR)
    observed = imread_unicode(out / f"{facade_id}_observed_mask_colmap.tif", cv2.IMREAD_UNCHANGED)
    owner_map = imread_unicode(out / f"{facade_id}_seam_owner_map_colmap.png", cv2.IMREAD_UNCHANGED)
    owner_index = json.loads((out / f"{facade_id}_seam_owner_index_colmap.json").read_text(encoding="utf-8"))
    homographies = json.loads((out / f"{facade_id}_homographies_colmap.json").read_text(encoding="utf-8"))
    quality = StitchQualityReport(**json.loads((out / f"{facade_id}_quality_report_colmap.json").read_text(encoding="utf-8")))
    flat = MosaicResult(
        analysis_image=flat_analysis, visual_image=flat_visual, observed_mask=observed, quality=quality,
        source_transforms={
            k: SourceTransform(H=np.array(v["H"], dtype=np.float64), width=int(v["width"]), height=int(v["height"]))
            for k, v in homographies.items()
        },
        seam_owner_map=owner_map, seam_owner_index=owner_index,
    )

    h, w = observed.shape[:2]
    dense_result = DenseStereoResult(num_points_total=0, num_points_near_wall=0, fused_ply_path=str(fused))
    print("rebuilding hybrid from", fused, "canvas", w, "x", h)
    hybrid = build_hybrid_mosaic(reconstruction, plane, dense_result, flat, (w, h), sim3d, edge_margin_m=0.0)

    backup = out / "_backup_before_rebuild"
    backup.mkdir(exist_ok=True)
    for suffix in SUFFIXES:
        f = out / f"{facade_id}{suffix}"
        if f.exists() and not (backup / f.name).exists():
            shutil.copy2(f, backup / f.name)

    imwrite_unicode(out / f"{facade_id}_analysis_colmap_dense.tif", hybrid.analysis_image)
    imwrite_unicode(out / f"{facade_id}_visual_colmap_dense.tif", hybrid.visual_image)
    imwrite_unicode(out / f"{facade_id}_observed_mask_colmap_dense.tif", hybrid.observed_mask)
    from dataclasses import asdict

    atomic_write_json(out / f"{facade_id}_quality_report_colmap_dense.json", asdict(hybrid.quality))
    from src.pipeline.runner import _write_source_transform_artifacts

    _write_source_transform_artifacts(out, facade_id, "_colmap_dense", hybrid)
    print("done. coverage_ratio (over the wall extent) =", round(hybrid.quality.coverage_ratio, 4),
          "| previous outputs backed up to", backup)


if __name__ == "__main__":
    main()
