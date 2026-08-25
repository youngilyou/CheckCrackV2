"""mm conversion, gated on real calibration (CLAUDE.local.md #26).

Never invent a pixel-to-mm scale (#43.9). A facade mosaic only carries a
trustworthy metric scale when it came from COLMAP-pose rectification onto
a plane with a known px_per_m tied to real GPS-aligned UTM meters
(geometry/rectification.py). The plain homography-chain mosaic
(stitching/warp.py) has no metric scale at all — its pixel grid is only
self-consistent, not tied to real-world distance.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScaleInfo:
    px_per_m: float | None
    calibrated: bool
    # Provenance for *why* px_per_m is trustworthy (or None if the scale came
    # from COLMAP/UTM alone with no physical reference object) -- lets a
    # report cite what the mm numbers are actually based on, per the operator
    # capture guide's reference-marker fields (scale marker/ArUco/AprilTag/
    # crack gauge/known window dimension, etc.).
    reference_object_type: str | None = None
    reference_length_mm: float | None = None


def mm_per_px(scale: ScaleInfo) -> float | None:
    if not scale.calibrated or not scale.px_per_m:
        return None
    return 1000.0 / scale.px_per_m


def to_mm(value_px: float | None, scale: ScaleInfo) -> float | None:
    if value_px is None:
        return None
    factor = mm_per_px(scale)
    if factor is None:
        return None
    return value_px * factor


def to_mm2(area_px2: float | None, scale: ScaleInfo) -> float | None:
    """Area scales with the *square* of the linear mm-per-px factor."""
    if area_px2 is None:
        return None
    factor = mm_per_px(scale)
    if factor is None:
        return None
    return area_px2 * (factor**2)
