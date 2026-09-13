"""Run crack detection directly on original (un-stitched) photos, one image at
a time, no tiling (CLAUDE.local.md #21's tiling is for mosaics that can be tens
of thousands of pixels wide; a single DJI still doesn't need it).

Backs the "원본 AI" viewer screen: quick per-photo crack candidate overlay, not
a precise measurement -- length/width/skeleton and facade-global coordinates
stay the job of the stitched-mosaic path (tools/detect_cracks_folder.py).

Two SEPARATE trained models, always both run (2026-09-13, same 1차/2차
convention as detect_cracks_folder.py -- see that script's own docstring for
the full rationale/measured numbers):
- 1차 (config/pipeline.yaml's `crack.model`, "모든 크랙 표시") -- written to
  <images_dir>/output/originals_cracks.json (unchanged filename).
- 2차 (`crack.model_v2`, "구조물 오탐 제외") -- written to
  <images_dir>/output/originals_cracks_v2.json (new file). Skipped gracefully
  (log only) if `crack.model_v2` is absent from config or its checkpoint
  file doesn't exist.

Usage:
    python tools/detect_cracks_images.py <images_dir> [--model PATH] [--model-v2 PATH] [--config PATH] [--device DEVICE]

Writes <images_dir>/output/originals_cracks*.json:
    [{"image_id", "file_name", "width", "height",
      "detections": [{"polygon_px": [[x,y],...], "confidence"}]}, ...]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.capture.image_catalog import scan_images  # noqa: E402
from src.common.atomic_io import atomic_write_json  # noqa: E402
from src.common.config import load_config  # noqa: E402
from src.common.imageio import imread_unicode  # noqa: E402
from src.common.logging import get_logger, log_event  # noqa: E402
from src.crack.detector import CrackDetector  # noqa: E402
from src.crack.tiler import Tile  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("images_dir", type=Path, help="folder of original photos (e.g. facades/F001 or a picked folder)")
    parser.add_argument("--model", default=None, help="1차 override -- defaults to config/pipeline.yaml's crack.model")
    parser.add_argument("--model-v2", default=None, help="2차 override -- defaults to config/pipeline.yaml's crack.model_v2")
    parser.add_argument("--skip-v2", action="store_true", help="run 1차 only, same as before 2차 existed")
    parser.add_argument("--config", default="config/pipeline.yaml")
    parser.add_argument("--device", default=None)
    args = parser.parse_args()

    images_dir: Path = args.images_dir
    if not images_dir.is_dir():
        print(f"not a folder: {images_dir}")
        sys.exit(1)

    cfg = load_config(args.config)
    logger = get_logger("pipeline", log_dir="logs")

    model_path_v1 = args.model or str(cfg.crack.model)
    if not Path(model_path_v1).exists():
        print(f"1차 model checkpoint not found: {model_path_v1}")
        sys.exit(1)

    model_path_v2: str | None = None
    if not args.skip_v2:
        model_path_v2 = args.model_v2 or (str(cfg.crack.model_v2) if "model_v2" in cfg.crack else None)
        if model_path_v2 is not None and not Path(model_path_v2).exists():
            print(f"2차 model checkpoint not found: {model_path_v2} -- skipping 2차 for this run")
            model_path_v2 = None

    image_paths = scan_images(images_dir)
    if not image_paths:
        print(f"no images found in {images_dir}")
        sys.exit(1)

    out_dir = images_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    def run_pass(version_label: str, model_path: str, out_path: Path) -> int:
        detector = CrackDetector(model_path, cfg, device=args.device)

        t0 = time.time()
        log_event(
            logger, "info", "original-image crack detection started",
            stage="ORIGINAL_CRACK_DETECT_STARTED", image_count=len(image_paths),
            model=model_path, model_version=version_label,
        )

        payload = []
        for i, path in enumerate(image_paths):
            image = imread_unicode(path)
            if image is None:
                log_event(logger, "warning", "could not read image, skipping", stage="ORIGINAL_CRACK_DETECT", file=str(path))
                continue

            h, w = image.shape[:2]
            image_id = path.stem
            tile = Tile(tile_id=image_id, facade_id="", x0=0, y0=0, width=w, height=h, image=image, observed_ratio=1.0)
            detections = detector.infer_tile(tile)

            payload.append({
                "image_id": image_id,
                "file_name": path.name,
                "width": w,
                "height": h,
                "detections": [
                    {
                        "polygon_px": np.asarray(det.polygon_tile_px).round(1).tolist(),
                        "confidence": round(det.confidence, 4),
                    }
                    for det in detections
                ],
            })

            log_event(
                logger, "info", "image processed",
                stage="ORIGINAL_CRACK_DETECT", model_version=version_label,
                progress=f"{i + 1}/{len(image_paths)}", image_id=image_id, crack_count=len(detections),
            )

        atomic_write_json(out_path, payload)

        total_detections = sum(len(entry["detections"]) for entry in payload)
        log_event(
            logger, "info", "original-image crack detection complete",
            stage="ORIGINAL_CRACK_DETECTED", model_version=version_label,
            image_count=len(payload), crack_count=total_detections, elapsed_s=round(time.time() - t0, 2),
        )
        return total_detections

    total_v1 = run_pass("v1", model_path_v1, out_dir / "originals_cracks.json")
    print(f"[1차/모든크랙] {total_v1} crack candidate(s) total")
    print(f"  - {out_dir / 'originals_cracks.json'}")

    if model_path_v2 is not None:
        total_v2 = run_pass("v2", model_path_v2, out_dir / "originals_cracks_v2.json")
        print(f"[2차/구조물오탐제외] {total_v2} crack candidate(s) total")
        print(f"  - {out_dir / 'originals_cracks_v2.json'}")
    else:
        print("[2차/구조물오탐제외] skipped -- no crack.model_v2 configured/found, or --skip-v2")


if __name__ == "__main__":
    main()
