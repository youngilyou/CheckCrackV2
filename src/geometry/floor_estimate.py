"""Floor lines on a rectified (COLMAP / Dense) facade mosaic -- automatic suggestion + the shared rule that turns a
canvas row into a floor number (2026-10-09, 사용자 확정 "권장 방식").

Why it works on the COLMAP/Dense canvas: that canvas is an orthographic elevation of the wall plane (px_per_m fixed,
v axis = true vertical), so every floor is the same height in pixels from top to bottom. The H-chain mosaic is not --
floors are only computed for a canvas that has `{facade}_scale_colmap.json`.

Why it is only a suggestion (memory floor_labeling_needs_bim): the canvas alone does not know the floor NUMBERS --
the photos may not reach the ground, a roof plant room / attic band may or may not count as a floor, a lobby floor may
be taller. So:
  * suggested.pitch_px     = the repeat period of the horizontal edges (slabs / window rows) inside the wall, measured
                             from the image itself -- independent of the GPS-based metric scale
  * suggested.roof_row_px  = the first floor-joint line at/below the top of the wall region mask
                             (`{facade}_wall_region_mask_colmap.png`): the phase where, folded by the pitch, the per-row
                             MEDIAN horizontal-edge strength across the wall is highest -- a slab joint runs across every
                             column, a window edge only across some, so the median picks the joints (2026-10-09, checked
                             on the four test facades: lines land on the panel joints, not mid-window)
  * the operator confirms the top line, the pitch and the total floor count in CheckCrackViewer (confirmed block).
Without a total floor count there are no floor numbers at all (never a guessed count).

`{facade}_floors.json`:
  {"version": 1, "facade_id", "canvas_width", "canvas_height", "px_per_m",
   "suggested": {"roof_row_px", "wall_bottom_row_px", "pitch_px", "pitch_m", "pattern_score", "floors_in_view"} | null,
   "confirmed": {"roof_row_px", "pitch_px", "total_floors", "confirmed_by", "confirmed_at"} | null}
`roof_row_px` is the TOP edge of the highest counted floor (floor number = total_floors). Rows above it are roof /
sky; floor n spans [roof + (total-n)*pitch, roof + (total-n+1)*pitch).
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from src.common.atomic_io import atomic_write_json
from src.common.imageio import imread_unicode

MIN_FLOOR_PITCH_M = 2.3
MAX_FLOOR_PITCH_M = 4.2
MIN_PATTERN_SCORE = 0.2  # autocorrelation peak below this -> no reliable repeat pattern, no pitch suggested
WALL_ROW_MIN_FRACTION = 0.3  # a canvas row belongs to the wall when >= 30% of its pixels are wall


def floor_number_at(row_px: float, roof_row_px: float, pitch_px: float, total_floors: int) -> int | None:
    """Floor number of a canvas row, or None above the roof line / below floor 1 / without valid inputs."""
    if pitch_px <= 0 or total_floors <= 0 or row_px < roof_row_px:
        return None
    n = total_floors - int((row_px - roof_row_px) // pitch_px)
    return n if 1 <= n <= total_floors else None


def _wall_rows(mask: np.ndarray) -> tuple[int, int] | None:
    rows = np.where((mask > 0).mean(axis=1) >= WALL_ROW_MIN_FRACTION)[0]
    if rows.size == 0:
        return None
    return int(rows.min()), int(rows.max())


def _repeat_period(gray: np.ndarray, mask: np.ndarray, top: int, bottom: int, px_per_m: float) -> tuple[float, float] | None:
    """Vertical repeat period (px) of horizontal edges inside the wall rows, and its autocorrelation score."""
    h, w = gray.shape
    cols = np.where((mask[top:bottom + 1] > 0).mean(axis=0) >= 0.5)[0]
    if cols.size < 50:
        cols = np.arange(w // 3, 2 * w // 3)
    band = gray[top:bottom + 1][:, cols].astype(np.float32)
    profile = np.abs(np.diff(band, axis=0)).mean(axis=1)
    profile = profile - profile.mean()
    n = profile.size
    lo, hi = int(MIN_FLOOR_PITCH_M * px_per_m), int(MAX_FLOOR_PITCH_M * px_per_m)
    if n < 2 * hi or not np.any(profile):
        return None
    ac = np.correlate(profile, profile, "full")[n - 1:]
    ac = ac / ac[0]
    lag = lo + int(np.argmax(ac[lo:hi + 1]))
    score = float(ac[lag])
    # sub-pixel peak (parabola through the three samples around it)
    if 0 < lag < ac.size - 1:
        a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
        denom = a - 2 * b + c
        if denom < 0:
            return float(lag + 0.5 * (a - c) / denom), score
    return float(lag), score


def _joint_phase(gray: np.ndarray, mask: np.ndarray, top: int, bottom: int, pitch_px: float) -> float:
    """Offset (0..pitch) below `top` of the strongest across-the-wall horizontal edge, folded by the pitch."""
    cols = np.where((mask[top:bottom + 1] > 0).mean(axis=0) >= 0.5)[0]
    if cols.size < 50:
        cols = np.arange(gray.shape[1] // 3, 2 * gray.shape[1] // 3)
    blurred = cv2.GaussianBlur(gray, (0, 0), 2).astype(np.float32)
    profile = np.median(np.abs(np.diff(blurred[top:bottom + 1][:, cols], axis=0)), axis=1)
    bins = int(round(pitch_px))
    idx = (np.arange(profile.size) % pitch_px).astype(int) % bins
    folded = np.bincount(idx, weights=profile, minlength=bins) / np.maximum(1, np.bincount(idx, minlength=bins))
    smooth = np.convolve(np.r_[folded[-5:], folded, folded[:5]], np.ones(11) / 11, "same")[5:-5]
    return float(np.argmax(smooth)) + 0.5  # +0.5: the edge sits between row i and i+1 of the diff


def suggest_floors(output_dir: str | Path, facade_id: str) -> dict | None:
    """Suggestion block for this facade, or None when the canvas is not a metric COLMAP/Dense canvas."""
    output_dir = Path(output_dir)
    scale_path = output_dir / f"{facade_id}_scale_colmap.json"
    mask_path = output_dir / f"{facade_id}_wall_region_mask_colmap.png"
    mosaic_path = next((p for p in (output_dir / f"{facade_id}_analysis_colmap_dense.tif",
                                    output_dir / f"{facade_id}_analysis_colmap.tif") if p.exists()), None)
    if not scale_path.exists() or mosaic_path is None:
        return None
    px_per_m = float(json.loads(scale_path.read_text(encoding="utf-8")).get("px_per_m") or 0)
    if px_per_m <= 0:
        return None
    gray = imread_unicode(str(mosaic_path), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return None
    mask = imread_unicode(str(mask_path), cv2.IMREAD_GRAYSCALE) if mask_path.exists() else None
    if mask is None or mask.shape != gray.shape:
        mask = (gray > 0).astype(np.uint8) * 255  # no wall mask: everything the mosaic covers

    wall = _wall_rows(mask)
    suggested: dict = {"roof_row_px": None, "wall_top_row_px": None, "wall_bottom_row_px": None, "pitch_px": None,
                       "pitch_m": None, "pattern_score": None, "floors_in_view": None}
    if wall is not None:
        top, bottom = wall
        suggested["roof_row_px"] = top
        suggested["wall_top_row_px"] = top
        suggested["wall_bottom_row_px"] = bottom
        period = _repeat_period(gray, mask, top, bottom, px_per_m)
        if period is not None:
            pitch_px, score = period
            suggested["pattern_score"] = round(score, 3)
            if score >= MIN_PATTERN_SCORE:
                roof = top + _joint_phase(gray, mask, top, bottom, pitch_px)
                suggested["roof_row_px"] = round(roof, 1)
                suggested["pitch_px"] = round(pitch_px, 2)
                suggested["pitch_m"] = round(pitch_px / px_per_m, 3)
                suggested["floors_in_view"] = round((bottom - roof) / pitch_px, 2)
    return {"canvas_width": int(gray.shape[1]), "canvas_height": int(gray.shape[0]), "px_per_m": px_per_m,
            "suggested": suggested}


def write_floor_suggestion(output_dir: str | Path, facade_id: str) -> Path | None:
    """(Re)writes the suggestion in `{facade}_floors.json`, keeping an existing operator confirmation for the same
    canvas size. Returns the file path, or None when no suggestion is possible."""
    output_dir = Path(output_dir)
    result = suggest_floors(output_dir, facade_id)
    if result is None:
        return None
    path = output_dir / f"{facade_id}_floors.json"
    confirmed = None
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8-sig"))
            if (old.get("canvas_width"), old.get("canvas_height")) == (result["canvas_width"], result["canvas_height"]):
                confirmed = old.get("confirmed")
        except (json.JSONDecodeError, OSError):
            pass
    atomic_write_json(path, {"version": 1, "facade_id": facade_id, **result, "confirmed": confirmed})
    return path


def load_floor_setting(output_dir: str | Path, facade_id: str) -> dict | None:
    """The operator-confirmed floor setting ({roof_row_px, pitch_px, total_floors, ...}), or None."""
    path = Path(output_dir) / f"{facade_id}_floors.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None
    confirmed = data.get("confirmed")
    if not isinstance(confirmed, dict):
        return None
    if not all(isinstance(confirmed.get(k), (int, float)) for k in ("roof_row_px", "pitch_px", "total_floors")):
        return None
    return confirmed
