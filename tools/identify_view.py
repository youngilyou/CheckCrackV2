"""Human-identification-aid image views (2026-08-26 UI request, "원본 AI"/"AI 학습"
screens): when crack color blends into the wall and it's hard to tell "is this
a crack or not", let a reviewer flip the displayed photo to a processed view
that makes a thin crack line stand out.

This is NOT measurement and NOT automatic crack detection -- it never produces
a crack polygon/length/width, only a display image. The actual crack region
that gets marked/reported always stays anchored to the ORIGINAL image's pixel
coordinates; these views exist only to help a human decide yes/no while
looking at the same photo from a different angle.

Modes:
  shadow   -- illumination/shadow correction, reusing the same
              src/illumination.correct_illumination_any backend (and the same
              cfg.illumination.mode) as the main detection pipeline, so a
              crack buried in strong architectural shadow becomes visible.
  binarize -- adaptive threshold on a CLAHE-enhanced grayscale image, so a
              thin dark line separates from a similarly-colored wall.
  skeleton -- binarize, then skimage.morphology.skeletonize down to a 1px
              center-line -- makes a faint crack's continuity obvious even
              when the raw binarized region is broken into fragments.
  edges    -- Canny edge detection on the CLAHE-enhanced grayscale image.

Usage:
    python tools/identify_view.py <image> --mode shadow --out <output.png>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
from skimage.morphology import skeletonize

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.common.config import load_config  # noqa: E402
from src.common.imageio import imread_unicode, imwrite_unicode  # noqa: E402
from src.illumination.dispatch import correct_illumination_any  # noqa: E402

MODES = ("shadow", "binarize", "skeleton", "edges")



# Drone photos run 4000-8000px wide with real material texture (aggregate
# concrete, gravel roofing) that has just as much local contrast as a thin
# crack line -- adaptive threshold/Canny at native resolution is pure noise
# (confirmed on a real test photo: full-res binarize was unusable). Cracks
# are elongated features that survive downsampling; isotropic surface
# texture doesn't, so binarize/skeleton/edges all work on this capped-size
# version instead of the original -- same cap the viewer itself displays at.
_MAX_PROCESS_DIM = 1600


def _resized_for_processing(image_bgr: np.ndarray) -> np.ndarray:
    h, w = image_bgr.shape[:2]
    scale = min(1.0, _MAX_PROCESS_DIM / max(h, w))
    if scale >= 1.0:
        return image_bgr
    return cv2.resize(image_bgr, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)


def _clahe_gray(image_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def _binarize(image_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.GaussianBlur(_clahe_gray(_resized_for_processing(image_bgr)), (5, 5), 0)
    # Cracks read darker than the surrounding wall in the vast majority of
    # cases (this is a display aid, not a detector -- a false line here just
    # costs the reviewer one glance, unlike a missed/duplicated Crack entity).
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 51, 7,
    )


def render_binarize(image_bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(_binarize(image_bgr), cv2.COLOR_GRAY2BGR)


def render_skeleton(image_bgr: np.ndarray) -> np.ndarray:
    skeleton = skeletonize(_binarize(image_bgr) > 0)
    out = np.zeros((*skeleton.shape, 3), dtype=np.uint8)
    out[skeleton] = (0, 255, 255)  # yellow-on-black: high-contrast, unambiguous
    return out


def render_edges(image_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.GaussianBlur(_clahe_gray(_resized_for_processing(image_bgr)), (5, 5), 0)
    return cv2.cvtColor(cv2.Canny(gray, 40, 120), cv2.COLOR_GRAY2BGR)


def render_shadow(image_bgr: np.ndarray, config_path: str) -> np.ndarray:
    cfg = load_config(config_path)
    return correct_illumination_any(image_bgr, cfg.illumination.mode, cfg, device="cpu")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", type=Path)
    parser.add_argument("--mode", required=True, choices=MODES)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config", default="config/pipeline.yaml")
    args = parser.parse_args()

    image = imread_unicode(args.image, cv2.IMREAD_COLOR)
    if image is None:
        raise SystemExit(f"cannot read {args.image}")

    if args.mode == "shadow":
        out = render_shadow(image, args.config)
    elif args.mode == "binarize":
        out = render_binarize(image)
    elif args.mode == "skeleton":
        out = render_skeleton(image)
    else:
        out = render_edges(image)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if not imwrite_unicode(args.out, out):
        raise SystemExit(f"failed to write {args.out}")
    print(str(args.out))


if __name__ == "__main__":
    main()
