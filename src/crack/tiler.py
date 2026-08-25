"""Overlap-tile a facade mosaic for crack inference (CLAUDE.local.md #21).

A facade mosaic can be tens of thousands of pixels wide; #43.6 forbids
resizing that down to one YOLO input (a millimeter-wide crack would vanish).
Tiles overlap so a crack that crosses a tile boundary is still whole in at
least one neighboring tile, and tiles that are almost entirely unobserved
(occluded facade, #20) are skipped rather than fed to the model.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.common.config import Config


@dataclass
class Tile:
    tile_id: str
    facade_id: str
    x0: int
    y0: int
    width: int
    height: int
    image: np.ndarray
    observed_ratio: float


def tile_mosaic(facade_id: str, image: np.ndarray, observed_mask: np.ndarray, cfg: Config) -> list[Tile]:
    tcfg = cfg.tiling
    tile_w, tile_h = int(tcfg.tile_width), int(tcfg.tile_height)
    overlap = int(tcfg.overlap_px)
    skip_below = float(tcfg.skip_if_observed_ratio_below)
    stride_x, stride_y = max(1, tile_w - overlap), max(1, tile_h - overlap)

    h, w = image.shape[:2]
    tiles: list[Tile] = []

    y0 = 0
    while True:
        y1 = min(y0 + tile_h, h)
        x0 = 0
        while True:
            x1 = min(x0 + tile_w, w)

            mask_crop = observed_mask[y0:y1, x0:x1]
            observed_ratio = float(np.count_nonzero(mask_crop)) / float(mask_crop.size)
            if observed_ratio >= skip_below:
                tiles.append(
                    Tile(
                        tile_id=f"{facade_id}_T{y0:06d}_{x0:06d}",
                        facade_id=facade_id,
                        x0=x0, y0=y0, width=x1 - x0, height=y1 - y0,
                        image=image[y0:y1, x0:x1],
                        observed_ratio=observed_ratio,
                    )
                )

            if x1 >= w:
                break
            x0 += stride_x
        if y1 >= h:
            break
        y0 += stride_y

    return tiles
