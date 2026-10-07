"""Re-apply the Dense wall-texture finish (src/geometry/depth_fill.py::finish_hybrid_mosaic -- every dense
pixel re-taken from its own original photo via the depth maps) to an ALREADY finished `*_colmap_dense`
result, without re-running Dense Stereo.

Why (2026-10-08): remote-analysis runs extract photos to non-ASCII folders (e.g. ...\\extracted\\수목토_1100_1\\)
and depth_fill used cv2.imread, which returns None for such paths on Windows -- the finish silently did
nothing (log `WALL_FINISH_APPLIED texture_px=0`) and the mosaic kept the raw point-splat look. Fixed in
depth_fill (imread_unicode); this tool repairs results produced before the fix.

Backs up the files it rewrites into `_backup_before_wallfinish/` first (refuses if that folder exists).
Needs the run's depth maps (colmap_dense/dense, kept after the Dense cleanup) and `{facade}_depth_mapping.json`.

  python tools/apply_wall_finish.py <version_output_dir> <facade_id> [<images_dir>]
  (images_dir defaults to two levels up: photos\\output\\Vnnn\\)
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402

from src.common.imageio import imread_unicode, imwrite_unicode  # noqa: E402
from src.geometry.depth_fill import finish_hybrid_mosaic  # noqa: E402
from src.geometry.depth_mapping import load_mapper_from_sidecar  # noqa: E402


def main() -> int:
    out_dir = Path(sys.argv[1])
    fid = sys.argv[2]
    images_dir = Path(sys.argv[3]) if len(sys.argv) > 3 else out_dir.parent.parent
    mapper = load_mapper_from_sidecar(out_dir, fid)
    if mapper is None:
        print("no usable depth_mapping sidecar / depth maps -- cannot apply")
        return 1

    sfx = "_colmap_dense"
    vis_p = out_dir / f"{fid}_visual{sfx}.tif"
    ana_p = out_dir / f"{fid}_analysis{sfx}.tif"
    flat_p = out_dir / f"{fid}_visual_colmap.tif"
    owner_p = out_dir / f"{fid}_seam_owner_map{sfx}.png"
    index_p = out_dir / f"{fid}_seam_owner_index{sfx}.json"
    backup = out_dir / "_backup_before_wallfinish"
    if backup.exists():
        print(f"{backup} already exists -- restore or delete it first")
        return 1

    visual = imread_unicode(str(vis_p), cv2.IMREAD_COLOR)
    flat = imread_unicode(str(flat_p), cv2.IMREAD_COLOR)
    if visual is None or flat is None:
        print("missing dense or flat mosaic")
        return 1
    owner = imread_unicode(str(owner_p), cv2.IMREAD_UNCHANGED) if owner_p.exists() else None
    index = json.loads(index_p.read_text(encoding="utf-8")) if index_p.exists() else []
    if isinstance(index, dict):  # tolerate {"index": [...]} style
        index = index.get("index", [])

    hybrid = SimpleNamespace(visual_image=visual, analysis_image=visual, observed_mask=None, quality=None,
                             source_transforms=None, seam_owner_map=owner, seam_owner_index=list(index))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = finish_hybrid_mosaic(hybrid, SimpleNamespace(visual_image=flat), mapper, images_dir,
                                  logger=logging.getLogger("apply_wall_finish"))

    backup.mkdir()
    for p in (vis_p, ana_p, owner_p, index_p):
        if p.exists():
            shutil.copy2(p, backup / p.name)
    imwrite_unicode(str(vis_p), result.visual_image)
    imwrite_unicode(str(ana_p), result.analysis_image)
    if result.seam_owner_map is not None:
        imwrite_unicode(str(owner_p), result.seam_owner_map)
        index_p.write_text(json.dumps(result.seam_owner_index, ensure_ascii=False), encoding="utf-8")
    print(f"done -- originals in {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
