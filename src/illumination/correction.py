"""Shadow/illumination correction for crack detection input (SmartCrack V2
design review, 2026-08-26, section 4).

Goal per the design doc's section 3.3/4.1: make brightness more uniform
across a facade photo (so a shadowed strip isn't a blind spot for the crack
detector) WITHOUT moving a single pixel -- no resize/crop/warp/perspective
transform, ever. That's what lets a crack polygon detected on a corrected
copy be treated as being in the *original* JPEG's own pixel coordinates
with zero extra transform (crack/multiview.py's whole per-original-detection
design depends on this).

Classical (non-learned) single-scale Retinex approach, chosen over deep
low-light-enhancement models (Zero-DCE/Retinexformer/etc.) after review,
2026-08-26: those are trained on night/indoor low-light photos, a different
domain than sunlit outdoor facades with local shadows, and would need
separate license verification for commercial use. This has neither problem
-- it's OpenCV/numpy math, deterministic, and requires no training data.

Algorithm (section 4.1's recommended flow):
  1. BGR -> LAB, take the L (lightness) channel alone -- never touch A/B,
     so color/hue is never altered, only brightness.
  2. Estimate a *low-frequency* illumination map from L via a large-kernel
     Gaussian blur -- this is the "ambient lighting" component. What's left
     (L / illumination) is approximately the true surface reflectance.
  3. Correction factor = target_mid_gray / illumination_map, clipped to a
     mode-dependent range so a very dark shadow patch doesn't get amplified
     into noise, and a well-lit patch is barely touched.
  4. Blend the corrected L back with the original L using a soft weight
     that's strongest exactly where the illumination map is darkest (real
     shadow) and fades to ~0 in already-normal-brightness areas -- so this
     never "flattens" a photo that had no shadow problem to begin with.
  5. Recombine with the untouched A/B channels, LAB -> BGR.

MODE 0/1/2 (doc section 4.2) are the same algorithm at different strengths,
not three different algorithms -- that's what makes the A/B comparison
harness (crack/validation.py) meaningful: MODE 1 vs 2 isolates "how much
correction is too much", not "which unrelated method wins".
"""

from __future__ import annotations

import cv2
import numpy as np

from src.common.config import Config

MODE_ORIGINAL = "ORIGINAL"
MODE_LIGHT = "LIGHT"
MODE_RETINEX = "RETINEX"

_KNOWN_MODES = (MODE_ORIGINAL, MODE_LIGHT, MODE_RETINEX)


def _illumination_map(l_channel: np.ndarray, kernel_frac: float) -> np.ndarray:
    """Low-frequency brightness estimate via a large Gaussian blur. kernel
    size is a fraction of the image's shorter side so it scales with
    resolution instead of being a fixed pixel count (a 5280x3956 DJI
    original and a smaller test crop both get a "large relative to the
    whole photo" blur, not a fixed-and-wrong absolute one)."""
    h, w = l_channel.shape[:2]
    k = int(round(min(h, w) * kernel_frac))
    k = k + 1 if k % 2 == 0 else k  # GaussianBlur requires an odd kernel size
    k = max(k, 3)
    return cv2.GaussianBlur(l_channel.astype(np.float32), (k, k), 0)


def vegetation_mask(image_bgr: np.ndarray, hue_low: int = 35, hue_high: int = 85, min_saturation: int = 40) -> np.ndarray:
    """Boolean (H, W) -- True where a pixel is plausibly foliage (green
    hue, real saturation so gray/white noise doesn't false-positive).
    Needed because shadow_darkness_mask_local's fine-vs-coarse local
    contrast can't tell a genuine cast shadow from a leaf's own natural
    light/dark texture -- both look identical to a pure-brightness
    contrast test. Confirmed necessary on a real photo (2026-08-26): without
    this, tree-canopy pixels scored just as "shadow" as an actual balcony
    shadow stripe, and a naive ROI pass over the raw mask produced one
    giant ROI spanning the whole tree (roughly a third of the photo)."""
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    hue, sat = hsv[:, :, 0], hsv[:, :, 1]
    return (hue >= hue_low) & (hue <= hue_high) & (sat >= min_saturation)


def shadow_darkness_mask_local(
    image_bgr: np.ndarray, fine_kernel_frac: float = 0.005, coarse_kernel_frac: float = 0.06, gain: float = 4.0
) -> np.ndarray:
    """(H, W) float32 in [0, 1] -- local CAST-SHADOW contrast, as opposed to
    shadow_darkness_mask's global-average darkness. Confirmed necessary on a
    real photo (DJI_0173, 2026-08-26): a sharp diagonal shadow stripe a
    balcony casts across an otherwise-bright rooftop is only moderately
    dark in absolute terms, so shadow_darkness_mask (which compares against
    the WHOLE photo's average brightness) barely flags it while strongly
    flagging genuinely-dark-everywhere regions (tree canopy, dark window
    glass) that were never the target -- exactly backwards from what a
    "where did PhaSR actually help" mask should prioritize.

    Fix: compare each pixel's FINE-scale local brightness (small blur, just
    enough to denoise) against a COARSE-scale neighborhood average (blur
    wide enough to span a shadow stripe and the bright surface on either
    side of it). A cast shadow shows up as fine << coarse right at the
    stripe. A uniformly dark object like dark window glass shows fine ~=
    coarse (both scales agree it's dark), naturally excluding it without a
    separate rule -- but foliage does NOT behave like a uniform dark
    object: leaves and the gaps between them create real fine-vs-coarse
    contrast on their own (confirmed on a real photo, 2026-08-26 -- tree
    canopy scored just as "shadow" as an actual cast-shadow stripe), so
    vegetation needs its own explicit exclusion (`vegetation_mask`) rather
    than falling out of this formula for free.

    `gain`: confirmed by direct pixel sampling on DJI_0173 (2026-08-26) that
    the raw (coarse-fine)/coarse ratio systematically undershoots real
    contrast at narrow shadow stripes -- a stripe with true L~90 next to
    L~235 surroundings (a huge, unambiguous real shadow) only scored
    ~0.13-0.22 raw, because the stripe is narrow relative to the coarse
    kernel so the coarse average there is still pulled most of the way
    toward the bright surroundings, compressing the ratio. Sweeping fine/
    coarse kernel sizes didn't fix this (same ~0.1-0.2 range across every
    combination tried); a flat multiplicative gain on the ratio (then
    clipped back to [0,1]) does, without the false-positive noise that
    shrinking the fine kernel introduced on flat bright regions."""
    l_channel = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2LAB)[:, :, 0].astype(np.float32)
    fine = _illumination_map(l_channel, fine_kernel_frac)
    coarse = _illumination_map(l_channel, coarse_kernel_frac)
    coarse_safe = np.clip(coarse, 1.0, 255.0)
    raw = (coarse_safe - fine) / coarse_safe
    mask = np.clip(raw * gain, 0.0, 1.0).astype(np.float32)
    mask[vegetation_mask(image_bgr)] = 0.0
    return mask


def shadow_darkness_mask(image_bgr: np.ndarray, kernel_frac: float = 0.25) -> np.ndarray:
    """(H, W) float32 in [0, 1] -- how much darker than the photo's own
    typical brightness each pixel's local illumination is (0 = as bright as
    the photo's own average, 1 = as dark as the darkest illumination in the
    photo). Same formula _correct_l_channel uses for its soft blend weight,
    pulled out standalone so other correction backends (e.g. PhaSR) can
    restrict themselves to shadow regions only -- a photo's non-shadow
    majority should never be touched by a correction backend that has its
    own artifacts elsewhere in the frame."""
    l_channel = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2LAB)[:, :, 0].astype(np.float32)
    illum = _illumination_map(l_channel, kernel_frac)
    illum_safe = np.clip(illum, 1.0, 255.0)
    target = float(np.mean(illum_safe))
    return np.clip((target - illum_safe) / target, 0.0, 1.0).astype(np.float32)


def _correct_l_channel(
    l_channel: np.ndarray,
    kernel_frac: float,
    correction_clip: tuple[float, float],
    max_blend_strength: float,
) -> np.ndarray:
    l_f = l_channel.astype(np.float32)
    illum = _illumination_map(l_f, kernel_frac)
    illum_safe = np.clip(illum, 1.0, 255.0)

    target = float(np.mean(illum_safe))
    factor = np.clip(target / illum_safe, correction_clip[0], correction_clip[1])
    corrected = np.clip(l_f * factor, 0, 255)

    # Soft blend weight: 1.0 (full correction) where illumination is dark
    # (real shadow), fading toward 0.0 (leave original alone) where
    # illumination is already close to the target -- so well-lit areas of
    # the same photo aren't needlessly altered.
    darkness = np.clip((target - illum_safe) / target, 0.0, 1.0)
    weight = darkness * max_blend_strength

    blended = l_f * (1.0 - weight) + corrected * weight
    return np.clip(blended, 0, 255).astype(np.uint8)


def correct_illumination(image_bgr: np.ndarray, mode: str, cfg: Config) -> np.ndarray:
    """Returns a same-size, same-coordinate-space BGR image. MODE_ORIGINAL
    returns the input untouched (identity -- this is the "no correction"
    baseline the A/B harness compares everything else against)."""
    if mode not in _KNOWN_MODES:
        raise ValueError(f"unknown illumination mode '{mode}', expected one of {_KNOWN_MODES}")
    if mode == MODE_ORIGINAL:
        return image_bgr

    icfg = cfg.illumination
    if mode == MODE_LIGHT:
        kernel_frac = float(icfg.light_kernel_frac)
        clip = (float(icfg.light_correction_min), float(icfg.light_correction_max))
        max_blend = float(icfg.light_max_blend_strength)
    else:  # MODE_RETINEX
        kernel_frac = float(icfg.retinex_kernel_frac)
        clip = (float(icfg.retinex_correction_min), float(icfg.retinex_correction_max))
        max_blend = float(icfg.retinex_max_blend_strength)

    lab = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)
    l_corrected = _correct_l_channel(l_channel, kernel_frac, clip, max_blend)
    lab_corrected = cv2.merge([l_corrected, a_channel, b_channel])
    return cv2.cvtColor(lab_corrected, cv2.COLOR_LAB2BGR)
