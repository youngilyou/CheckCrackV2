"""Run crack detection on a stitched facade and save the results next to the
mosaic. Two detection pipelines exist (CLAUDE.local.md's 2026-09-12 raw-photo-
first redesign):

- Raw-photo-first (src/crack/raw_pipeline.py): each surviving source photo is
  tiled+detected independently at its own native resolution, then positioned
  onto the canvas via the stage-2 COLMAP homography and merged across images.
  Used whenever this facade has both a COLMAP scale (`{facade_id}_scale_colmap.
  json`, calibrated=True) and stage-2 homographies (`{facade_id}_homographies_
  colmap.json`) -- i.e. the full 2-stage COLMAP pipeline succeeded for it.
- Mosaic-tiling (src/crack/pipeline.py, CLAUDE.local.md #21-27, the original
  spec): tiles the already-stitched mosaic directly. Used as a fallback for
  any facade without both of the above (COLMAP fallback, or only H-chain).

Both write the exact same {facade_id}_cracks*.json schema below -- which
pipeline ran is recorded only in the log, not in the JSON.

Two SEPARATE trained models, always both run (2026-09-13, per explicit user
decision -- "1차/2차 각각 모델이 있고, 돌릴 때 각각 모델을 사용"):
- 1차 (config/pipeline.yaml's `crack.model`, "모든 크랙 표시") -- the original
  detector, flags every crack-shaped candidate including window frame/panel
  joint/building corner false positives. Written to {facade_id}_cracks.json
  (unchanged filename -- existing viewer/report consumers keep working as-is).
- 2차 (`crack.model_v2`, "구조물 오탐 제외") -- a SEPARATE checkpoint
  fine-tuned from 1차 with confirmed-false-positive hard negatives
  (training_data_structural_fp_v2/) mixed with real crack512 positives (see
  training/crack_seg/finetune_hard_negatives_full_positive.py and that
  config key's own comment for why positives had to be mixed in -- hard
  negatives alone regressed real-crack recall). Written to
  {facade_id}_cracks_v2.json (new file, separate from 1차's).
  Skipped gracefully (log only, not a hard error) if `crack.model_v2` is
  absent from config or its checkpoint file doesn't exist -- lets this script
  keep working unchanged against an older config that predates 2차.
3차 (육안 검토, 관리자가 1차/2차 결과를 비교해 최종 판정) is a manual step
outside this script's scope.

Usage:
    python tools/detect_cracks_folder.py <facade_output_dir> [facade_id] [--model PATH] [--model-v2 PATH] [--building-id ID]

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

Calibration: reads {facade_id}_scale_colmap.json (written by pipeline/runner.py
right after a successful COLMAP rectification) when present -- this facade's
COLMAP+GPS-EXIF alignment (align_reconstruction_to_utm) is being used as the
pixel-to-meter scale (operator decision, 2026-09-11: ordinary GPS accepted for
now, not RTK/surveyed-marker grade -- see ScaleInfo.reference_object_type in
the written JSON for provenance). Facades with no COLMAP rectification (no
such file) still get ScaleInfo.calibrated=False, i.e. every *_mm field null,
never invented (CLAUDE.local.md #9/#26).

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
from src.crack.raw_pipeline import detect_cracks_from_raw_images  # noqa: E402


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
    parser.add_argument("--model", default=None, help="1차 override -- defaults to config/pipeline.yaml's crack.model")
    parser.add_argument("--model-v2", default=None, help="2차 override -- defaults to config/pipeline.yaml's crack.model_v2")
    parser.add_argument("--skip-v2", action="store_true", help="run 1차 only, same as before 2차 existed")
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
    raw_image_paths: dict[str, str] = {}
    if source_images_path.exists():
        source_entries = json.loads(source_images_path.read_text(encoding="utf-8"))
        source_image_ids = [entry["image_id"] for entry in source_entries]
        raw_image_paths = {entry["image_id"]: entry["file_path"] for entry in source_entries}

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

    model_path_v1 = args.model or str(cfg.crack.model)
    if not Path(model_path_v1).exists():
        print(f"1차 model checkpoint not found: {model_path_v1}")
        sys.exit(1)

    # 2차 is opt-in-by-config-presence, not a hard requirement -- an older
    # config without crack.model_v2 (or --skip-v2) just runs 1차 alone,
    # exactly as this script behaved before 2차 existed.
    model_path_v2: str | None = None
    if not args.skip_v2:
        model_path_v2 = args.model_v2 or (str(cfg.crack.model_v2) if "model_v2" in cfg.crack else None)
        if model_path_v2 is not None and not Path(model_path_v2).exists():
            print(f"2차 model checkpoint not found: {model_path_v2} -- skipping 2차 for this run")
            model_path_v2 = None

    # scale_colmap.json only exists for a facade that actually went through
    # COLMAP rectification (pipeline/runner.py writes it right after) -- a
    # facade stitched only via the plain H-chain has no geometric scale basis
    # at all, so it correctly falls through to the uncalibrated/px-only case
    # (#9/#26: no calibration, no mm -- never guessed).
    scale_path = output_dir / f"{facade_id}_scale_colmap.json"
    if scale_path.exists():
        scale_data = json.loads(scale_path.read_text(encoding="utf-8"))
        scale = ScaleInfo(
            px_per_m=scale_data.get("px_per_m"),
            calibrated=bool(scale_data.get("calibrated", False)),
            reference_object_type=scale_data.get("reference_object_type"),
            reference_length_mm=scale_data.get("reference_length_mm"),
        )
    else:
        scale = ScaleInfo(px_per_m=None, calibrated=False)

    # Raw-photo-first pipeline needs BOTH a real metric canvas scale (COLMAP,
    # not the H-chain-only uncalibrated case) and this facade's stage-2
    # per-image homographies -- source_transforms already prefers the colmap
    # variant when present (see the _pick() call above), so in practice these
    # two conditions are true/false together (both come from the same
    # successful stage-2 COLMAP run). Anything short of that (no COLMAP, or
    # COLMAP fallback failed) falls back to mosaic-tiling exactly as before --
    # never guessing a canvas_px_per_m for local_scale_info to divide by
    # (CLAUDE.local.md #9/#26).
    use_raw_pipeline = scale.calibrated and scale.px_per_m is not None and source_transforms is not None
    raw_image_paths_available = {
        image_id: raw_image_paths[image_id]
        for image_id in (source_transforms or {})
        if image_id in raw_image_paths
    }
    if use_raw_pipeline and not raw_image_paths_available:
        use_raw_pipeline = False  # homographies exist but source JPEGs are gone -- fall back rather than error

    def run_pass(version_label: str, model_path: str, cracks_path: Path, mask_path_out: Path) -> int:
        # Stable crack_ids across re-runs against this SAME mosaic -- each
        # version's own prior *_cracks*.json, never mixed across 1차/2차 (a
        # crack_id stable in 1차 has no bearing on 2차's own numbering, they
        # are independent detection runs that happen to share a facade).
        previous_cracks: list[dict] | None = None
        if cracks_path.exists():
            try:
                previous_cracks = json.loads(cracks_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                previous_cracks = None  # corrupt/partial file -- fall back to fresh IDs

        t0 = time.time()
        log_event(
            logger, "info", "crack detection started",
            stage="CRACK_DETECT_STARTED", facade_id=facade_id, model=model_path,
            model_version=version_label, mosaic_shape=list(analysis_image.shape[:2]),
            pipeline="raw_photo" if use_raw_pipeline else "mosaic_tile",
        )

        if use_raw_pipeline:
            cracks = detect_cracks_from_raw_images(
                facade_id=facade_id,
                building_id=args.building_id,
                image_paths=raw_image_paths_available,
                source_transforms=source_transforms,
                canvas_px_per_m=scale.px_per_m,
                cfg=cfg,
                model_path=model_path,
                device=args.device,
                previous_cracks=previous_cracks,
            )
        else:
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
            stage="CRACK_DETECTED", facade_id=facade_id, model_version=version_label,
            crack_count=len(cracks), elapsed_s=round(time.time() - t0, 2),
        )

        if previous_cracks:
            prev_ids = {c["crack_id"] for c in previous_cracks}
            new_ids = {c.crack_id for c in cracks}
            log_event(
                logger, "info", "crack id matching",
                stage="CRACK_ID_MATCHED", facade_id=facade_id, model_version=version_label,
                matched=len(prev_ids & new_ids), new=len(new_ids - prev_ids),
                retired=len(prev_ids - new_ids),
            )

        # Visual sanity-check overlay -- polygons + skeleton drawn on a copy of
        # the analysis mosaic. Not consumed by anything downstream; just for a
        # human to glance at and confirm the detections land where expected.
        overlay = analysis_image.copy()
        for crack in cracks:
            pts = np.round(crack.polygon_px).astype(np.int32).reshape(-1, 1, 2)
            cv2.polylines(overlay, [pts], isClosed=True, color=(0, 0, 255), thickness=3)
            skel_pts = np.round(crack.skeleton_px).astype(np.int32)
            for x, y in skel_pts:
                overlay[max(0, y - 1):y + 2, max(0, x - 1):x + 2] = (0, 255, 255)
        imwrite_unicode(mask_path_out, overlay)

        # Flat schema -- matches viewer/CheckCrackViewer/Models/CrackResultModel.cs
        # exactly (crack_id/facade_id/length_px/.../observation_state/source_image_ids
        # at the top level). The viewer's FacadeOutputScanner already reads
        # {facade_id}_cracks.json (1차) expecting this shape; it silently ignores
        # any extra fields (System.Text.Json default behavior), so bbox/polygon/
        # skeleton/position/building_id/tile ids are kept too for future overlay
        # UI use without breaking the current parser. {facade_id}_cracks_v2.json
        # (2차) uses the identical schema -- no viewer reads it yet (3차 육안
        # 검토 UI wiring is a separate, not-yet-done task).
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
        return len(cracks)

    count_v1 = run_pass(
        "v1", model_path_v1,
        output_dir / f"{facade_id}_cracks.json",
        output_dir / f"{facade_id}_crack_mask.tif",
    )
    print(f"[1차/모든크랙] detected {count_v1} crack(s)")
    print(f"  - {output_dir / f'{facade_id}_cracks.json'}")
    print(f"  - {output_dir / f'{facade_id}_crack_mask.tif'}")

    if model_path_v2 is not None:
        count_v2 = run_pass(
            "v2", model_path_v2,
            output_dir / f"{facade_id}_cracks_v2.json",
            output_dir / f"{facade_id}_crack_mask_v2.tif",
        )
        print(f"[2차/구조물오탐제외] detected {count_v2} crack(s)")
        print(f"  - {output_dir / f'{facade_id}_cracks_v2.json'}")
        print(f"  - {output_dir / f'{facade_id}_crack_mask_v2.tif'}")
    else:
        print("[2차/구조물오탐제외] skipped -- no crack.model_v2 configured/found, or --skip-v2")


if __name__ == "__main__":
    main()
