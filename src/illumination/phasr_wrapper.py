"""MODE 3 wrapper: PhaSR (CVPR 2026, vendored into phasr_vendor/) as an
illumination-correction backend behind the same correct_illumination()-style
interface as src/illumination/correction.py's classical MODE 0/1/2.

Verified before writing this wrapper (2026-08-26, direct read of
phasr_vendor/test.py): PhaSR pads its input to a multiple of the model's
window size, runs inference, then crops the output back to the exact
original height/width -- the eval_size/cv2.resize step in test.py only
touches a throwaway copy used for computing PSNR/SSIM metrics, never the
actual restored image. So this wrapper's output is genuinely full original
resolution / same pixel coordinates, unlike SID (rejected as MODE 4 after
code review found its released inference path hard-resizes to 256x256 with
no path back to full resolution without re-deriving the blend ourselves).

PhaSR needs three things beyond its own weights, per its own README and
calculate_depth_normal.py (all vendored as siblings under phasr_vendor/,
2026-08-26 -- "모든 소스는 CheckCrackV2 안에서" requirement):
  - Depth-Anything-V2/ (vendored repo + downloaded vitl checkpoint) for a
    per-image relative depth map.
  - dinov2/ (vendored repo, dinov2_vitl14 loaded via torch.hub with
    source="local") for semantic features (GSRA's "semantic" half).
  - A simple Sobel-based depth->normal conversion (calculate_depth_normal.py
    reimplemented here in float32 directly, skipping its own uint8 npy
    round-trip -- reading that script closely, its saved uint8 normal maps
    and its own test.py's load_normal()/process_normal() don't actually
    agree on a value range; recomputing normals directly in float here
    sidesteps that mismatch instead of reproducing it).

Checkpoint used: Ambient6K (win_size=8, per the repo's own README note
"use 8 for Ambient6K/INS checkpoints, 16 for ISTD/WSRD") -- chosen over
ISTD/ISTD+/WSRD because Ambient6K is the one of PhaSR's four released
checkpoints trained across varied/multi-source ambient lighting rather than
a single fixed studio shadow setup, closest in spirit to outdoor daylight
facade shadows (still unverified on our own domain -- exactly what the
0.3mm validation harness's MODE comparison is for).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Must import PIL before torch/torchvision on Windows: once torchvision's own
# bundled native DLLs (libjpeg/zlib) are loaded first, they shadow the ones
# Pillow's _imaging.pyd needs and it fails to load -- confirmed directly
# (2026-08-26): `from PIL import Image` alone works fine, but the exact same
# statement raises ImportError when it happens transitively inside
# torchvision.datasets after torchvision has already loaded. Importing PIL
# first locks in the correct DLL before torchvision gets a chance to.
import PIL.Image  # noqa: F401

import cv2
import numpy as np
import torch
import torch.nn.functional as F

from src.illumination.correction import shadow_darkness_mask_local

_VENDOR_DIR = Path(__file__).parent / "phasr_vendor"
_DEPTH_ANYTHING_DIR = _VENDOR_DIR / "Depth-Anything-V2"
_DINOV2_DIR = _VENDOR_DIR / "dinov2"
_DEPTH_CHECKPOINT = _DEPTH_ANYTHING_DIR / "checkpoints" / "depth_anything_v2_vitl.pth"
_PHASR_CHECKPOINT = _VENDOR_DIR / "checkpoints" / "Ambient6K" / "Ambient6K_model_best.pth"
_WIN_SIZE = 8  # Ambient6K/INS checkpoints; ISTD/WSRD would need 16

_DEPTH_CONFIG = {"encoder": "vitl", "features": 256, "out_channels": [256, 512, 1024, 1024]}

_state: dict = {}  # lazy-loaded models, keyed by name -- loaded once per process


def _load_models(device: str) -> None:
    if _state:
        return

    for p in (_VENDOR_DIR, _DEPTH_ANYTHING_DIR):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))

    from depth_anything_v2.dpt import DepthAnythingV2
    from model import PhaSR
    from utils.model_utils import load_checkpoint

    depth_model = DepthAnythingV2(**_DEPTH_CONFIG)
    depth_model.load_state_dict(torch.load(_DEPTH_CHECKPOINT, map_location="cpu"))
    depth_model = depth_model.to(device).eval()

    dino_model = torch.hub.load(str(_DINOV2_DIR), "dinov2_vitl14", source="local").to(device).eval()

    phasr_model = PhaSR(img_size=256, embed_dim=32, win_size=_WIN_SIZE, token_projection="linear", token_mlp="leff")
    load_checkpoint(phasr_model, str(_PHASR_CHECKPOINT))
    phasr_model = phasr_model.to(device).eval()

    _state["device"] = device
    _state["depth_model"] = depth_model
    _state["dino_model"] = dino_model
    _state["phasr_model"] = phasr_model


def _depth_and_normal(image_bgr: np.ndarray, device: str) -> tuple[np.ndarray, np.ndarray]:
    """Relative depth (H, W) float32 and surface normal (H, W, 3) float32 in
    [-1, 1], straight from Depth-Anything-V2's output -- no lossy uint8
    round-trip through disk the way calculate_depth_normal.py's own script
    does (see module docstring)."""
    depth_model = _state["depth_model"]
    with torch.inference_mode():
        depth = depth_model.infer_image(image_bgr)  # (H, W) float32, arbitrary relative scale
    depth = (depth - depth.min()) / max(depth.max() - depth.min(), 1e-6)

    dx = cv2.Sobel(depth, cv2.CV_32F, 1, 0, ksize=5)
    dy = cv2.Sobel(depth, cv2.CV_32F, 0, 1, ksize=5)
    dz = -np.ones_like(dx)
    normal = np.stack((dx, dy, dz), axis=-1)
    normal = normal / (np.linalg.norm(normal, axis=-1, keepdims=True) + 1e-6)
    return depth.astype(np.float32), normal.astype(np.float32)


def _depth_to_point(depth: np.ndarray, fov_deg: float = 60.0) -> np.ndarray:
    """Same fixed-FOV pinhole back-projection PhaSR's own utils.image_utils
    uses -- an approximation (no real per-camera intrinsics), but faithfully
    matching what the released checkpoint was actually trained against."""
    height, width = depth.shape
    fov_rad = np.deg2rad(fov_deg)
    focal = width / (2 * np.tan(fov_rad / 2))
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    x, y = np.meshgrid(range(width), range(height))
    z = depth
    x3d = (x - cx) * z / focal
    y3d = (y - cy) * z / focal
    return np.stack([x3d, y3d, z], axis=-1).astype(np.float32)


def _dino_features(dino_model, image_tensor: torch.Tensor):
    dps = 14
    upsampled = F.interpolate(
        image_tensor, size=(int(image_tensor.shape[2] * dps / 8), int(image_tensor.shape[3] * dps / 8)),
        mode="bilinear", align_corners=False,
    )
    return dino_model.get_intermediate_layers(upsampled, 4, True)


def _correct_tile(image_bgr: np.ndarray, device: str) -> np.ndarray:
    """Runs PhaSR on ONE small tile -- same-size/same-coordinate contract,
    but only safe to call at tile resolution. A full 5280x3956 DJI original
    fed directly here tries to allocate ~80GiB (confirmed by an actual CUDA
    OOM, 2026-08-26) because PhaSR's own model.py upsamples DINO features
    back up to the image's own full padded resolution at a late fusion
    stage (`F.interpolate(DINO_Mat_features[0], size=(H0, W0), ...)`) --
    this is NOT the same as the 14/8 ratio in _dino_features (that one is a
    fixed, training-time-locked alignment ratio between DINO's patch grid
    and PhaSR's own stride-8 internal feature map, not a free parameter);
    it's a second, independent upsample back to full image size that simply
    doesn't fit in 16GB at native DJI resolution. Tiling (see
    correct_illumination_phasr below) is the fix -- same principle already
    used for crack detection itself (#8/#21: tile, never resize, a big
    image down to one model input)."""
    _load_models(device)
    phasr_model = _state["phasr_model"]
    dino_model = _state["dino_model"]

    depth, normal = _depth_and_normal(image_bgr, device)
    point = _depth_to_point(depth)
    point = point / (2 * point[:, :, 2].mean() + 1e-6)  # same normalization test.py applies

    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    img_t = torch.from_numpy(image_rgb).permute(2, 0, 1).unsqueeze(0).to(device)
    point_t = torch.from_numpy(point).permute(2, 0, 1).unsqueeze(0).float().to(device)
    normal_t = torch.from_numpy(normal).permute(2, 0, 1).unsqueeze(0).float().to(device)

    # Ceiling-division to the next multiple of m -- NOT ((h+m)//m)*m (that
    # formula always adds a full extra window even when h is already a
    # multiple of m). Padding mode is "replicate" (edge value), not
    # "reflect": reflect-padding requires the pad amount to be strictly
    # less than the input's own size in that dimension, which real
    # shadow-recovery ROI crops can violate (confirmed: a 64px-tall crop
    # needing 64px of padding crashed reflect-pad outright) -- replicate has
    # no such restriction and the padded region gets cropped back off
    # immediately after inference anyway, so the choice barely matters.
    m = 8 * _WIN_SIZE
    h, w = img_t.shape[2], img_t.shape[3]
    pad_h, pad_w = ((h - 1) // m + 1) * m, ((w - 1) // m + 1) * m
    img_p = F.pad(img_t, (0, pad_w - w, 0, pad_h - h), "replicate")
    point_p = F.pad(point_t, (0, pad_w - w, 0, pad_h - h), "replicate")
    normal_p = F.pad(normal_t, (0, pad_w - w, 0, pad_h - h), "replicate")

    with torch.no_grad(), torch.amp.autocast("cuda" if device.startswith("cuda") else "cpu", dtype=torch.bfloat16):
        dino_feats = _dino_features(dino_model, img_p)
        out = phasr_model(img_p, dino_feats, point_p, normal_p)
    out = out.float().clamp(0, 1)[:, :, :h, :w]

    out_rgb = (out[0].cpu().numpy().transpose(1, 2, 0) * 255).round().astype(np.uint8)
    return cv2.cvtColor(out_rgb, cv2.COLOR_RGB2BGR)


def _tile_bounds(size: int, tile: int, overlap: int) -> list[tuple[int, int]]:
    stride = max(1, tile - overlap)
    bounds = []
    pos = 0
    while True:
        end = min(pos + tile, size)
        bounds.append((end - tile if end - tile >= 0 else 0, end))
        if end >= size:
            break
        pos += stride
    # de-duplicate in case the last step landed on the same window twice
    seen = set()
    out = []
    for b in bounds:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def _feather_weight(h: int, w: int, overlap: int) -> np.ndarray:
    """1.0 in the tile's interior, raised-cosine ramp down to ~0 within
    `overlap` pixels of each edge -- so overlapping tiles blend smoothly
    instead of showing a seam at tile boundaries."""
    ramp = max(overlap, 1)
    wy = np.ones(h, dtype=np.float32)
    wx = np.ones(w, dtype=np.float32)
    r = np.minimum(np.arange(h), h - 1 - np.arange(h))
    wy = np.where(r < ramp, 0.5 - 0.5 * np.cos(np.pi * (r + 0.5) / ramp), 1.0).astype(np.float32)
    r = np.minimum(np.arange(w), w - 1 - np.arange(w))
    wx = np.where(r < ramp, 0.5 - 0.5 * np.cos(np.pi * (r + 0.5) / ramp), 1.0).astype(np.float32)
    return (wy[:, None] * wx[None, :]).astype(np.float32)


def correct_illumination_phasr(
    image_bgr: np.ndarray, device: str = "cuda", tile_size: int = 768, overlap: int = 96
) -> np.ndarray:
    """Tiled PhaSR: same same-size/same-coordinate contract as
    correction.correct_illumination(), safe at full DJI resolution. Splits
    into overlapping tiles, runs _correct_tile on each, and blends back with
    a feathered (raised-cosine) weight so tile seams don't show."""
    h, w = image_bgr.shape[:2]
    y_bounds = _tile_bounds(h, tile_size, overlap)
    x_bounds = _tile_bounds(w, tile_size, overlap)

    accum = np.zeros((h, w, 3), dtype=np.float32)
    weight_sum = np.zeros((h, w), dtype=np.float32)

    for y0, y1 in y_bounds:
        for x0, x1 in x_bounds:
            tile = image_bgr[y0:y1, x0:x1]
            corrected = _correct_tile(tile, device)
            weight = _feather_weight(y1 - y0, x1 - x0, overlap)
            accum[y0:y1, x0:x1] += corrected.astype(np.float32) * weight[:, :, None]
            weight_sum[y0:y1, x0:x1] += weight

    weight_sum = np.clip(weight_sum, 1e-6, None)
    return np.clip(accum / weight_sum[:, :, None], 0, 255).astype(np.uint8)


def correct_illumination_phasr_shadow_masked(
    image_bgr: np.ndarray,
    device: str = "cuda",
    tile_size: int = 768,
    overlap: int = 96,
    blend_strength: float = 1.0,
    shadow_fine_kernel_frac: float = 0.005,
    shadow_coarse_kernel_frac: float = 0.06,
    shadow_gain: float = 4.0,
) -> np.ndarray:
    """1차 마무리 설정 (사용자 확정, 2026-08-26) -- mitigation for the
    color-distortion/tile-seam artifacts PhaSR showed when applied to the
    whole frame, not a fix for PhaSR's own root cause (still suspected:
    per-tile depth normalized independently, per-tile camera intrinsics
    recentered on the tile instead of the original photo, bf16 precision
    loss in GSRA's attention subtraction; none of those are applied yet --
    parked for a later session). Restricts PhaSR's influence to two things
    at once:

    1. WHERE: only inside detected shadow regions
       (`shadow_darkness_mask_local` -- the LOCAL-contrast version, not the
       global-average one. Confirmed on a real photo, 2026-08-26: the
       global mask scores actual sharp cast-shadow stripes on an otherwise-
       bright rooftop as 0.0 because it only compares against the whole
       photo's average brightness -- completely missing the target while
       instead flagging uniformly-dark regions like tree canopy/windows
       that never needed correction).
    2. HOW MUCH: `blend_strength` scales the mask -- accepted final result
       used 1.0 (mask itself is already capped at 1.0 by its own gain
       parameter, so this isn't literally "100% PhaSR everywhere", only in
       pixels the mask fully saturates on).

    Known remaining issue at these settings, accepted for 1차 마무리 rather
    than blocking on: `shadow_darkness_mask_local` still mistakes leafy
    vegetation's natural high local contrast for cast shadow (a real cast
    shadow and a gap between leaves look the same to a fine-vs-coarse
    brightness comparison) -- foliage regions still pick up some of PhaSR's
    color drift. Not fixed here; a vegetation index (e.g. green-channel
    dominance) exclusion is the natural next step if this becomes a
    priority later."""
    phasr_out = correct_illumination_phasr(image_bgr, device=device, tile_size=tile_size, overlap=overlap)
    mask = shadow_darkness_mask_local(
        image_bgr,
        fine_kernel_frac=shadow_fine_kernel_frac,
        coarse_kernel_frac=shadow_coarse_kernel_frac,
        gain=shadow_gain,
    )
    weight = (mask * blend_strength)[:, :, None]
    blended = image_bgr.astype(np.float32) * (1.0 - weight) + phasr_out.astype(np.float32) * weight
    return np.clip(blended, 0, 255).astype(np.uint8)
