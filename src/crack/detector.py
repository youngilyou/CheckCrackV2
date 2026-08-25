"""YOLO crack segmentation inference, one tile at a time (CLAUDE.local.md #22).

CoreInterface's `CrackDetector.infer_tile` (#30) — kept as a thin wrapper so
the model backend (today: Ultralytics YOLOv8-seg finetuned on CUBIT-Seg,
training/crack_seg/train.py) isn't hard-baked into the tiling/merge code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from src.common.config import Config
from src.crack.tiler import Tile


@dataclass
class CrackDetection:
    tile_id: str
    facade_id: str
    polygon_tile_px: np.ndarray  # (N, 2) float, tile-local pixel coords
    confidence: float


class CrackDetector:
    def __init__(self, model_path: str, cfg: Config, device: str | None = None):
        from ultralytics import YOLO

        self.model = YOLO(model_path)
        self.confidence = float(cfg.crack.confidence)
        self.min_polygon_area_px = float(cfg.crack.min_polygon_area_px)
        self.device = device

    def infer_tile(self, tile: Tile) -> list[CrackDetection]:
        import cv2

        results = self.model.predict(
            tile.image, conf=self.confidence, imgsz=max(tile.width, tile.height),
            device=self.device, verbose=False,
        )[0]

        detections: list[CrackDetection] = []
        if results.masks is None:
            return detections

        confs = results.boxes.conf.detach().cpu().numpy()
        for polygon, conf in zip(results.masks.xy, confs):
            if cv2.contourArea(polygon.astype(np.float32)) < self.min_polygon_area_px:
                continue
            detections.append(
                CrackDetection(
                    tile_id=tile.tile_id, facade_id=tile.facade_id,
                    polygon_tile_px=polygon.astype(np.float64), confidence=float(conf),
                )
            )
        return detections
