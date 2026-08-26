"""Single entry point covering all illumination MODEs (classical MODE 0/1/2
in correction.py + PhaSR MODE 3 in phasr_wrapper.py), so callers (e.g.
crack/multiview.py's per-original detection) don't need to know which
backend a given cfg.illumination.mode resolves to -- config value change =
different backend, no caller code changes (2026-08-26 design decision).

Split into its own module rather than folded into correction.py to avoid a
circular import: phasr_wrapper.py already imports shadow_darkness_mask_local
from correction.py, so correction.py can't import phasr_wrapper.py back.
"""

from __future__ import annotations

import numpy as np

from src.common.config import Config
from src.illumination.correction import MODE_LIGHT, MODE_ORIGINAL, MODE_RETINEX, correct_illumination

MODE_PHASR = "PHASR"


def correct_illumination_any(image_bgr: np.ndarray, mode: str, cfg: Config, device: str = "cuda") -> np.ndarray:
    if mode in (MODE_ORIGINAL, MODE_LIGHT, MODE_RETINEX):
        return correct_illumination(image_bgr, mode, cfg)
    if mode == MODE_PHASR:
        from src.illumination.phasr_wrapper import correct_illumination_phasr_shadow_masked

        icfg = cfg.illumination
        return correct_illumination_phasr_shadow_masked(
            image_bgr,
            device=device,
            tile_size=int(icfg.phasr_tile_size),
            overlap=int(icfg.phasr_overlap),
            blend_strength=float(icfg.phasr_blend_strength),
            shadow_fine_kernel_frac=float(icfg.phasr_shadow_fine_kernel_frac),
            shadow_coarse_kernel_frac=float(icfg.phasr_shadow_coarse_kernel_frac),
            shadow_gain=float(icfg.phasr_shadow_gain),
        )
    raise ValueError(f"unknown illumination mode '{mode}'")
