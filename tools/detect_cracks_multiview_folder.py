"""Phase-2 crack detection: detect on each ORIGINAL photo separately (not
the stitched mosaic), project into facade coordinates via the stitching
stage's own per-image homography, then Duplicate/Continuation Association
(src/crack/multiview.py, SmartCrack V2 design, 2026-08-26).

Per-original detection itself uses `src/crack/shadow_recovery.py`'s
selective shadow-ROI recovery pipeline (settled as the default here,
2026-08-26, replacing whole-image illumination correction):
    ORIGINAL (no correction) detection = base pass
        -> find small ROIs where detection is genuinely hard (near-miss
           candidates that sit in a real shadow region, not just "this
           pixel is dark" -- see that module's docstring for why the
           naive version doesn't localize anything on a photo with a
           widespread repeating shadow pattern)
        -> RETINEX then PhaSR re-detection on just those ROIs, additive
           only (never overrules a base-pass detection)
`multiview.py`'s whole-image `detect_cracks_in_original` still exists and
still works (e.g. for an A/B comparison against this default), just isn't
what this CLI calls anymore.

This is the alternative to tools/detect_cracks_folder.py's mosaic-tile
path -- run one or the other (or both, for the A/B cross-validation signal
noted in project_checkcrack_status.md's 3-stage filter design), not a
replacement.

Usage:
    python tools/detect_cracks_multiview_folder.py <facade_output_dir> [facade_id] [--model PATH] [--building-id ID] [--device DEVICE]

<facade_output_dir> is the same "output/" folder stitch_folder.py /
stitch_for_ai_training.py already wrote:
    {facade_id}_source_images.json   -- image_id -> file_path (REQUIRED --
                                         this is how the original photos are
                                         found; no separate images-dir
                                         argument needed)
    {facade_id}_homographies.json (or _colmap variant)  -- REQUIRED, this is
                                         what makes per-original detection
                                         possible at all (source->facade H
                                         per image_id)
    {facade_id}_analysis.tif (or _colmap variant)  -- optional, only used to
                                         draw the visual sanity-check overlay

Writes:
    {facade_id}_cracks_multiview.json      -- same flat schema as
                                               detect_cracks_folder.py's
                                               {facade_id}_cracks.json
                                               (crack_id prefixed "_MV" so
                                               the two detection paths'
                                               ids never collide if both are
                                               ever loaded together)
    {facade_id}_crack_mask_multiview.tif   -- visual overlay on the analysis
                                               mosaic, if present (facade
                                               coordinates ARE mosaic
                                               coordinates, so this draws
                                               directly onto it)

Calibration: no COLMAP+UTM rectification scale threaded through here either
(same as detect_cracks_folder.py) -- every *_mm field is null, never
invented (CLAUDE.local.md #9/#26).
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
from src.crack.detector import CrackDetector  # noqa: E402
from src.crack.measurement import ScaleInfo  # noqa: E402
from src.crack.merge_tiles import CrackPolygon  # noqa: E402
from src.crack.multiview import build_final_cracks  # noqa: E402
from src.crack.shadow_recovery import detect_cracks_with_shadow_recovery  # noqa: E402


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
    parser.add_argument("--device", default="cuda", help="the shadow-recovery ROI pass's PhaSR step needs a GPU")
    args = parser.parse_args()

    output_dir: Path = args.output_dir
    if not output_dir.is_dir():
        print(f"not a folder: {output_dir}")
        sys.exit(1)

    facade_id = args.facade_id or output_dir.parent.name
    cfg = load_config(args.config)
    logger = get_logger("pipeline", log_dir="logs")

    source_images_path = output_dir / f"{facade_id}_source_images.json"
    homographies_path = _pick(output_dir, facade_id, "_homographies_colmap.json", "_homographies.json")

    if not source_images_path.exists():
        print(f"missing {source_images_path} -- stitch this facade first")
        sys.exit(1)
    if homographies_path is None:
        print(f"missing homographies json in {output_dir} -- this facade predates per-image H persistence, "
              f"per-original detection is not possible for it (fall back to detect_cracks_folder.py)")
        sys.exit(1)

    source_images = json.loads(source_images_path.read_text(encoding="utf-8"))
    source_transforms = json.loads(homographies_path.read_text(encoding="utf-8"))

    model_path = args.model or str(cfg.crack.model)
    if not Path(model_path).exists():
        print(f"model checkpoint not found: {model_path}")
        sys.exit(1)

    device = args.device
    detector = CrackDetector(model_path, cfg, device=None if device == "cpu" else device)

    scale = ScaleInfo(px_per_m=None, calibrated=False)

    t0 = time.time()
    log_event(
        logger, "info", "multiview crack detection started",
        stage="CRACK_DETECT_MV_STARTED", facade_id=facade_id, model=model_path,
        image_count=len(source_images),
    )

    fragments_by_image: dict[str, list[CrackPolygon]] = {}
    for entry in source_images:
        image_id, file_path = entry["image_id"], entry["file_path"]
        if image_id not in source_transforms:
            continue  # image didn't make it into the final stitch -- no H to project with
        image = imread_unicode(Path(file_path), cv2.IMREAD_COLOR)
        if image is None:
            print(f"  skip {image_id}: could not read {file_path}")
            continue
        polygons = detect_cracks_with_shadow_recovery(image_id, image, cfg, detector, illumination_device=device)
        fragments_by_image[image_id] = polygons
        print(f"  {image_id}: {len(polygons)} candidate polygon(s)")

    cracks = build_final_cracks(
        fragments_by_image, source_transforms, facade_id=facade_id, building_id=args.building_id,
        cfg=cfg, scale=scale,
    )

    log_event(
        logger, "info", "multiview crack detection complete",
        stage="CRACK_DETECTED_MV", facade_id=facade_id, crack_count=len(cracks),
        elapsed_s=round(time.time() - t0, 2),
    )

    # Visual sanity-check overlay onto the analysis mosaic, if present --
    # facade coordinates ARE mosaic coordinates, drawn the same way
    # detect_cracks_folder.py does.
    analysis_path = _pick(output_dir, facade_id, "_analysis_colmap.tif", "_analysis.tif")
    if analysis_path is not None:
        overlay = imread_unicode(analysis_path, cv2.IMREAD_COLOR)
        if overlay is not None:
            for crack in cracks:
                pts = np.round(crack.polygon_px).astype(np.int32).reshape(-1, 1, 2)
                cv2.polylines(overlay, [pts], isClosed=True, color=(0, 0, 255), thickness=3)
                skel_pts = np.round(crack.skeleton_px).astype(np.int32)
                for x, y in skel_pts:
                    overlay[max(0, y - 1):y + 2, max(0, x - 1):x + 2] = (0, 255, 255)
            imwrite_unicode(output_dir / f"{facade_id}_crack_mask_multiview.tif", overlay)

    # Same flat schema as detect_cracks_folder.py's {facade_id}_cracks.json
    # (viewer/CheckCrackViewer/Models/CrackResultModel.cs-compatible).
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

    cracks_path = output_dir / f"{facade_id}_cracks_multiview.json"
    atomic_write_json(cracks_path, payload)

    print(f"detected {len(cracks)} crack(s) (multiview)")
    print(f"  - {cracks_path}")
    if analysis_path is not None:
        print(f"  - {output_dir / f'{facade_id}_crack_mask_multiview.tif'}")


if __name__ == "__main__":
    main()
