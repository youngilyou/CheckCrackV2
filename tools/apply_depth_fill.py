"""Refresh a finished FRONT-style Dense result with the depth-based roof-edge hole fill
(src/geometry/depth_fill.py). Backs up every file it rewrites into `_backup_before_depthfill/` first.

  python tools/apply_depth_fill.py <output_dir> <facade_id>
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from src.common.imageio import imread_unicode, imwrite_unicode  # noqa: E402
from src.geometry.depth_fill import apply_depth_fill  # noqa: E402
from src.geometry.depth_mapping import load_mapper_from_sidecar  # noqa: E402


def main() -> int:
    out_dir = Path(sys.argv[1])
    fid = sys.argv[2]
    mapper = load_mapper_from_sidecar(out_dir, fid)
    if mapper is None:
        print("no depth_mapping sidecar -- run tools/make_depth_mapping_sidecar.py first")
        return 1
    sfx = "_colmap_dense"
    vis_p = out_dir / f"{fid}_visual{sfx}.tif"
    ana_p = out_dir / f"{fid}_analysis{sfx}.tif"
    flat_p = out_dir / f"{fid}_visual_colmap.tif"
    owner_p = out_dir / f"{fid}_seam_owner_map{sfx}.png"
    index_p = out_dir / f"{fid}_seam_owner_index{sfx}.json"
    backup = out_dir / "_backup_before_depthfill"
    if backup.exists():
        print("backup folder already exists -- refusing to overwrite (restore or delete it first)")
        return 1
    backup.mkdir()
    for p in (vis_p, ana_p, owner_p, index_p):
        shutil.copy2(p, backup / p.name)

    visual = imread_unicode(vis_p)
    flat = imread_unicode(flat_p)
    flat_only = (visual == flat).all(axis=2) & flat.any(axis=2)  # dense had no data here: the flat mosaic filled it
    new_visual, replaced, owner_new, names = apply_depth_fill(
        mapper, out_dir / "colmap_dense" / "dense" / "fused.ply",
        out_dir / "colmap_dense" / "dense" / "images", visual, flat_only,
    )
    print("replaced", int(replaced.sum()), "of", int(flat_only.sum()), "flat-filled pixels")

    index = json.loads(index_p.read_text(encoding="utf-8"))
    lookup = {n: i + 1 for i, n in enumerate(index)}
    for n in names:
        if n not in lookup:
            index.append(n)
            lookup[n] = len(index)
    import cv2
    owner_map = imread_unicode(owner_p, cv2.IMREAD_UNCHANGED)
    for k, n in enumerate(names):
        owner_map[replaced & (owner_new == k)] = lookup[Path(n).stem]
    imwrite_unicode(vis_p, new_visual)
    imwrite_unicode(ana_p, new_visual)  # analysis == visual in the dense hybrid (see build_hybrid_mosaic)
    imwrite_unicode(owner_p, owner_map)
    index_p.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    print("done; originals in", backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
