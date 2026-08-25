"""Run crack detection on an already-stitched facade (src/crack/pipeline.py's
tile -> infer -> merge -> measure chain, CLAUDE.local.md #21-27) and save the
results next to the mosaic.

Usage:
    python tools/detect_cracks_folder.py <facade_output_dir> [facade_id] [--model PATH] [--building-id ID]

<facade_output_dir> is the same "output/" folder stitch_for_ai_training.py /
stitch_folder.py already wrote (e.g. facades/<id>/output or <picked-folder>/output):
    {facade_id}_analysis.tif        (or _analysis_colmap.tif)
    {facade_id}_observed_mask.tif   (or _observed_mask_colmap.tif)
    {facade_id}_source_images.json

Writes:
    {facade_id}_crack_mask.tif   -- polygons drawn on the analysis mosaic, for a
                                     quick visual sanity check (not itself
                                     consumed by anything downstream)
    {facade_id}_cracks.json      -- one entry per Crack, schema matches
                                     CLAUDE.local.md #27's example (position/
                                     measurement/confidence/observation/
                                     source_image_ids), plus the full polygon
                                     and skeleton point lists for later overlay
                                     UI use, plus source_observations (which
                                     original photo(s) this crack is actually
                                     visible in, and where -- empty list if
                                     this facade predates {facade_id}_homographies*
                                     /_seam_owner_map*/_seam_owner_index* being
                                     written by the stitching stage).

Calibration: this script has no COLMAP+UTM rectification scale to hand off, so
ScaleInfo.calibrated is always False here -- every *_mm field in the output is
null, never invented (CLAUDE.local.md #9/#26). Real mm output needs a wiring
step that reads whatever scale rectification.py's COLMAP path actually
produced, which isn't threaded through to this script yet.

Crack ID stability: if {facade_id}_cracks.json already exists in output_dir,
this script loads it and matches new detections against it by polygon IoU
(src/crack/merge_tiles.py::match_crack_ids) so a crack that's still there
keeps its ID instead of being renumbered every re-run. This is only valid
because output_dir's analysis.tif is the same mosaic both times -- a fresh
stitch (a new version folder) has no prior cracks.json in it, so its crack
numbering simply starts over at C000000 by design, not by oversight.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.common.atomic_io import atomic_write_json  # noqa: E402
from src.common.config import load_config  # noqa: E402
from src.common.imageio import imread_unicode, imwrite_unicode  # noqa: E402
from src.common.logging import get_logger, log_event  # noqa: E402
from src.crack.measurement import ScaleInfo  # noqa: E402
from src.crack.pipeline import detect_cracks  # noqa: E402


def _pick(output_dir: Path, facade_id: str, *suffixes: str) -> Path | None:
    for suffix in suffixes:
        candidate = output_dir / f"{facade_id}{suffix}"
        if candidate.exists():
            return candidate
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path, help="facade's output/ folder (already stitched)")
    parser.add_argument("facade_id", nargs="?", help="defaults to the output_dir's parent folder name")
    parser.add_argument("--building-id", default="B000", help="no building registry yet -- placeholder id")
    parser.add_argument("--model", default=None, help="defaults to config/pipeline.yaml's crack.model")
    parser.add_argument("--config", default="config/pipeline.yaml")
    parser.add_argument("--device", default=None)
    args = parser.parse_args()

    output_dir: Path = args.output_dir
    if not output_dir.is_dir():
        print(f"not a folder: {output_dir}")
        sys.exit(1)

    facade_id = args.facade_id or output_dir.parent.name
    cfg = load_config(args.config)
    logger = get_logger("pipeline", log_dir="logs")

    # COLMAP-rectified variants take precedence when present, same precedence
    # AiTrainingViewModel already uses for display.
    analysis_path = _pick(output_dir, facade_id, "_analysis_colmap.tif", "_analysis.tif")
    mask_path = _pick(output_dir, facade_id, "_observed_mask_colmap.tif", "_observed_mask.tif")
    source_images_path = output_dir / f"{facade_id}_source_images.json"

    if analysis_path is None or mask_path is None:
        print(f"missing analysis mosaic or observed mask in {output_dir} -- stitch this facade first")
        sys.exit(1)

    analysis_image = imread_unicode(analysis_path, cv2.IMREAD_COLOR)
    observed_mask = imread_unicode(mask_path, cv2.IMREAD_GRAYSCALE)
    if analysis_image is None or observed_mask is None:
        print(f"could not read {analysis_path} / {mask_path}")
        sys.exit(1)

    source_image_ids: list[str] = []
    if source_images_path.exists():
        source_image_ids = [entry["image_id"] for entry in json.loads(source_images_path.read_text(encoding="utf-8"))]

    # source_observations inputs -- same COLMAP-preferred picking as
    # analysis/mask above, so these always match whichever mosaic variant was
    # actually loaded. All optional: an older facade stitched before this
    # feature existed simply has none of these files, and every crack's
    # source_observations comes back an empty list rather than erroring.
    homographies_path = _pick(output_dir, facade_id, "_homographies_colmap.json", "_homographies.json")
    seam_owner_map_path = _pick(output_dir, facade_id, "_seam_owner_map_colmap.png", "_seam_owner_map.png")
    seam_owner_index_path = _pick(output_dir, facade_id, "_seam_owner_index_colmap.json", "_seam_owner_index.json")

    source_transforms: dict | None = None
    if homographies_path is not None:
        source_transforms = json.loads(homographies_path.read_text(encoding="utf-8"))
    seam_owner_map = None
    if seam_owner_map_path is not None:
        seam_owner_map = imread_unicode(seam_owner_map_path, cv2.IMREAD_UNCHANGED)
    seam_owner_index: list[str] | None = None
    if seam_owner_index_path is not None:
        seam_owner_index = json.loads(seam_owner_index_path.read_text(encoding="utf-8"))

    model_path = args.model or str(cfg.crack.model)
    if not Path(model_path).exists():
        print(f"model checkpoint not found: {model_path}")
        sys.exit(1)

    # Stable crack_ids across re-runs against this SAME mosaic (a re-stitch
    # writes to a different version folder, so this only ever sees a prior
    # run's cracks.json from the identical analysis.tif -- see
    # match_crack_ids' docstring for why cross-version matching isn't done).
    cracks_path = output_dir / f"{facade_id}_cracks.json"
    previous_cracks: list[dict] | None = None
    if cracks_path.exists():
        try:
            previous_cracks = json.loads(cracks_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            previous_cracks = None  # corrupt/partial file -- fall back to fresh IDs

    # No COLMAP-pose-derived px_per_m is threaded through to this script yet
    # (see module docstring) -- pixel-only output, matching #9/#26's "no
    # calibration, no mm" rule rather than guessing a scale.
    scale = ScaleInfo(px_per_m=None, calibrated=False)

    t0 = time.time()
    log_event(
        logger, "info", "crack detection started",
        stage="CRACK_DETECT_STARTED", facade_id=facade_id, model=model_path,
        mosaic_shape=list(analysis_image.shape[:2]),
    )

    cracks = detect_cracks(
        facade_id=facade_id,
        building_id=args.building_id,
        analysis_image=analysis_image,
        observed_mask=observed_mask,
        cfg=cfg,
        model_path=model_path,
        scale=scale,
        source_image_ids=source_image_ids,
        device=args.device,
        previous_cracks=previous_cracks,
        seam_owner_map=seam_owner_map,
        seam_owner_index=seam_owner_index,
        source_transforms=source_transforms,
    )

    log_event(
        logger, "info", "crack detection complete",
        stage="CRACK_DETECTED", facade_id=facade_id, crack_count=len(cracks),
        elapsed_s=round(time.time() - t0, 2),
    )

    if previous_cracks:
        prev_ids = {c["crack_id"] for c in previous_cracks}
        new_ids = {c.crack_id for c in cracks}
        log_event(
            logger, "info", "crack id matching",
            stage="CRACK_ID_MATCHED", facade_id=facade_id,
            matched=len(prev_ids & new_ids), new=len(new_ids - prev_ids),
            retired=len(prev_ids - new_ids),
        )

    # Visual sanity-check overlay -- polygons + skeleton drawn on a copy of the
    # analysis mosaic. Not consumed by anything downstream; just for a human
    # to glance at and confirm the detections land where expected.
    overlay = analysis_image.copy()
    for crack in cracks:
        pts = np.round(crack.polygon_px).astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(overlay, [pts], isClosed=True, color=(0, 0, 255), thickness=3)
        skel_pts = np.round(crack.skeleton_px).astype(np.int32)
        for x, y in skel_pts:
            overlay[max(0, y - 1):y + 2, max(0, x - 1):x + 2] = (0, 255, 255)
    imwrite_unicode(output_dir / f"{facade_id}_crack_mask.tif", overlay)

    # Flat schema -- matches viewer/CheckCrackViewer/Models/CrackResultModel.cs
    # exactly (crack_id/facade_id/length_px/.../observation_state/source_image_ids
    # at the top level). The viewer's FacadeOutputScanner already reads
    # {facade_id}_cracks.json expecting this shape; it silently ignores any
    # extra fields (System.Text.Json default behavior), so bbox/polygon/
    # skeleton/position/building_id/tile ids are kept too for future overlay
    # UI use without breaking the current parser.
    payload = []
    for crack in cracks:
        cx = float((crack.bbox_px[0] + crack.bbox_px[2]) / 2)
        cy = float((crack.bbox_px[1] + crack.bbox_px[3]) / 2)
        payload.append({
            "crack_id": crack.crack_id,
            "facade_id": crack.facade_id,
            "length_px": round(crack.length_px, 2),
            "max_width_px": round(crack.max_width_px, 2),
            "mean_width_px": round(crack.mean_width_px, 2),
            "area_px": round(crack.area_px, 1),
            "length_mm": crack.length_mm,
            "max_width_mm": crack.max_width_mm,
            "area_mm2": crack.area_mm2,
            "confidence": round(crack.confidence, 4),
            "observation_state": crack.observation_state,
            "source_image_ids": crack.source_image_ids,
            # extra provenance/overlay fields, not read by CrackResultModel today
            "severity": crack.severity,
            "building_id": crack.building_id,
            "position": {"pixel_x": round(cx, 1), "pixel_y": round(cy, 1), "u_m": None, "v_m": None},
            "bbox_px": [round(v, 1) for v in crack.bbox_px],
            "polygon_px": crack.polygon_px.round(1).tolist(),
            "skeleton_px": crack.skeleton_px.round(1).tolist(),
            "source_tile_ids": crack.source_tile_ids,
            "source_observations": [
                {
                    "image_id": obs.image_id,
                    "bbox_px_in_source": list(obs.bbox_px_in_source),
                    "polygon_px_in_source": obs.polygon_px_in_source.tolist(),
                    "owned_pixel_count": obs.owned_pixel_count,
                }
                for obs in crack.source_observations
            ],
        })

    atomic_write_json(cracks_path, payload)

    print(f"detected {len(cracks)} crack(s)")
    print(f"  - {cracks_path}")
    print(f"  - {output_dir / f'{facade_id}_crack_mask.tif'}")


if __name__ == "__main__":
    main()
