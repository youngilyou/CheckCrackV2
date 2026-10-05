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

import numpy as np


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


def view_orthogonality(H: np.ndarray, point_raw_xy: tuple[float, float]) -> float:
    """0..1 score for "how front-on/undistorted does THIS image see this
    point" -- 1.0 = the local raw-pixel -> canvas-pixel mapping is locally a
    pure rotation+uniform-scale (similarity transform: a near-orthogonal
    shot), 0.0 = maximally sheared/foreshortened (a heavily oblique shot).

    2026-09-18, user report (BACK facade, "원본 보기"의 대표 사진으로 회전된/
    기울어진 사진(DJI_0089)이 골라졌는데, 같은 크랙을 더 정면으로 찍은
    DJI_0094/DJI_0095가 있었음): 기존엔 owned_pixel_count(그 사진 자체에서
    크랙이 차지하는 raw 픽셀 면적)만으로 대표 사진을 골랐는데, 이건 촬영
    각도와 무관하다 -- 기울어진(oblique) 사진일수록 원근 때문에 크랙이 더
    길게/크게 찍혀 오히려 나쁜 사진이 1순위로 뽑히는 역효과가 있었다.

    Derivation: for a planar homography H (raw-pixel -> canvas-pixel, w =
    H[2,0]*u + H[2,1]*v + H[2,2]), the 2x2 Jacobian of that mapping at (u,v)
    is
        J = (1/w) * [[H00 - x*H20, H01 - x*H21], [H10 - y*H20, H11 - y*H21]]
    (x, y are the projected canvas coordinates at (u,v); same w/x/y this
    module's mm-scale math already derives from H, just kept as a 2x2 matrix
    instead of collapsed to the scalar area-magnification factor). Its two
    singular values s1 >= s2 are the local magnification along the mapping's
    two principal axes -- equal (s2/s1 = 1) exactly when the local map is a
    similarity transform (no shear/anisotropic stretch, i.e. this patch of
    the photo is being viewed close to perpendicular); the more oblique the
    view, the more s1 and s2 diverge. s2/s1 is scale-invariant (a similarity
    transform scaled up/down still gives ratio 1), so it isolates "how
    distorted" from "how big/close" -- exactly the axis pixel-area alone
    couldn't see. Returns 0.0 (worst) if H is degenerate at this point
    rather than dividing by ~0."""
    u, v = point_raw_xy
    w = H[2, 0] * u + H[2, 1] * v + H[2, 2]
    if abs(w) < 1e-9:
        return 0.0
    x = (H[0, 0] * u + H[0, 1] * v + H[0, 2]) / w
    y = (H[1, 0] * u + H[1, 1] * v + H[1, 2]) / w
    jac = np.array([
        [H[0, 0] - x * H[2, 0], H[0, 1] - x * H[2, 1]],
        [H[1, 0] - y * H[2, 0], H[1, 1] - y * H[2, 1]],
    ]) / w
    try:
        singular_values = np.linalg.svd(jac, compute_uv=False)
    except np.linalg.LinAlgError:
        return 0.0
    s1, s2 = float(singular_values[0]), float(singular_values[1])
    if s1 <= 1e-12:
        return 0.0
    return max(0.0, min(1.0, s2 / s1))


# 폭 등급 판정 결과가 "해상도 부족"일 때 Crack.severity_note에 들어가는 고정 접두어 --
# 보고서/뷰어가 이 문자열로 집계하므로 바꾸면 pdf_report.py도 같이 바꿀 것.
SEVERITY_NOTE_LOW_RESOLUTION = "판정불가(해상도부족)"


def grade_severity(
    max_width_mm: float | None,
    width_mm_per_px: float | None,
    cfg,
) -> tuple[str | None, str | None]:
    """0.3mm 폭 기준 등급 -- (severity, severity_note).

    2026-10-05 (사용자 확정): 현재 촬영 데이터는 원본 1px이 벽면 약 2.6mm라,
    폭 측정(skeleton 거리변환 x2)의 최소값이 약 1px(=2~3mm)이다. 그래서
    검출된 크랙은 실제 폭과 무관하게 거의 전부 0.3mm 이상으로 측정되고
    "정밀점검대상"으로 분류돼 버린다 -- 근거 없는 판정이다(#9/#26).
    폭 기준을 판정하려면 기준 폭이 최소 수 px에 걸쳐야 하므로, 그 크랙 위치의
    해상도(mm/px)가 measurement.max_mm_per_px_for_severity보다 거칠면 등급을
    매기지 않고 severity=None + note=SEVERITY_NOTE_LOW_RESOLUTION을 남긴다.
    mm 값 자체는 그대로 출력하되 "추정값"이다(보고서에 명시).

    - max_width_mm 없음(스케일 없음): (None, None) -- 기존 동작 그대로.
    - 해상도 값을 모름: 판정 근거가 없으므로 해상도 부족과 같이 취급.
    """
    if max_width_mm is None:
        return None, None
    limit = float(getattr(cfg.measurement, "max_mm_per_px_for_severity", 0.15))
    if width_mm_per_px is None or width_mm_per_px > limit:
        if width_mm_per_px is None:
            return None, f"{SEVERITY_NOTE_LOW_RESOLUTION}: 해상도 정보 없음"
        return None, f"{SEVERITY_NOTE_LOW_RESOLUTION}: {width_mm_per_px:.2f} mm/px > 기준 {limit:.2f} mm/px"
    threshold = float(cfg.measurement.crack_width_threshold_mm)
    return ("정밀점검대상" if max_width_mm >= threshold else "경미"), None
