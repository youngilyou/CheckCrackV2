"""Run crack detection directly on original (un-stitched) photos, one image at
a time, no tiling (CLAUDE.local.md #21's tiling is for mosaics that can be tens
of thousands of pixels wide; a single DJI still doesn't need it).

Backs the "원본 AI" viewer screen: quick per-photo crack candidate overlay, not
a precise measurement -- length/width/skeleton and facade-global coordinates
stay the job of the stitched-mosaic path (tools/detect_cracks_folder.py).

Usage:
    python tools/detect_cracks_images.py <images_dir> [--model PATH] [--config PATH] [--device DEVICE]

Writes <images_dir>/output/originals_cracks.json:
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images_dir", type=Path, help="folder of original photos (e.g. facades/F001 or a picked folder)")
    parser.add_argument("--model", default=None, help="defaults to config/pipeline.yaml's crack.model")
    parser.add_argument("--config", default="config/pipeline.yaml")
    parser.add_argument("--device", default=None)
    args = parser.parse_args()

    images_dir: Path = args.images_dir
    if not images_dir.is_dir():
        print(f"not a folder: {images_dir}")
        sys.exit(1)

    cfg = load_config(args.config)
    logger = get_logger("pipeline", log_dir="logs")

    model_path = args.model or str(cfg.crack.model)
    if not Path(model_path).exists():
        print(f"model checkpoint not found: {model_path}")
        sys.exit(1)

    image_paths = scan_images(images_dir)
    if not image_paths:
        print(f"no images found in {images_dir}")
        sys.exit(1)

    detector = CrackDetector(model_path, cfg, device=args.device)

    t0 = time.time()
    log_event(
        logger, "info", "original-image crack detection started",
        stage="ORIGINAL_CRACK_DETECT_STARTED", image_count=len(image_paths), model=model_path,
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
            stage="ORIGINAL_CRACK_DETECT", progress=f"{i + 1}/{len(image_paths)}",
            image_id=image_id, crack_count=len(detections),
        )

    out_dir = images_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "originals_cracks.json"
    atomic_write_json(out_path, payload)

    total_detections = sum(len(entry["detections"]) for entry in payload)
    log_event(
        logger, "info", "original-image crack detection complete",
        stage="ORIGINAL_CRACK_DETECTED", image_count=len(payload), crack_count=total_detections,
        elapsed_s=round(time.time() - t0, 2),
    )

    print(f"processed {len(payload)} image(s), {total_detections} crack candidate(s) total")
    print(f"  - {out_path}")


if __name__ == "__main__":
    main()
