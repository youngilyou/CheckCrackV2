"""Phase 1/2 pipeline runner (CLAUDE.local.md #40, #32).

Two entry points:
  - `run_facade_poc`: Phase 1 shortcut — one image folder treated as one
    manually-assigned facade (#3.1 "1 Facade = 1 Flight").
  - `run_building_poc`: Phase 2 — real footprint-based facade
    classification (#6) splits one folder into several independent
    Facade_NNN groups, each stitched separately (#1: never one 360 mosaic).

Both delegate the per-facade MATCHED -> GEOMETRY_SOLVED -> STITCHED work to
`_run_facade_pipeline`. Ground-camera fusion (#16-19) and crack inference
(#22-27) are later phases and are not run here.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np

from src.building.facade_classifier import classify_images
from src.building.facade_segmenter import FacadeSegment, build_segments
from src.building.footprint import load_footprint_utm_txt
from src.capture.image_catalog import build_catalog, save_catalog
from src.common.atomic_io import atomic_write_json
from src.common.config import Config, load_config
from src.common.imageio import imread_unicode, imwrite_unicode
from src.common.logging import get_logger, log_event
from src.common.types import GeometryFailureCode, GeometryResult, ImageMetadata
from src.geometry.coverage import compute_overlap_report
from src.geometry.homography import estimate_homography
from src.geometry.quality import apply_quality_gate
from src.geometry.rectification import (
    align_reconstruction_to_utm,
    estimate_utm_epsg,
    facade_plane_from_reconstruction,
    facade_plane_from_segment,
    rectify_and_blend,
    rectify_images,
)
from src.matching.loftr_matcher import MatchTimeoutError, TimeoutLoFTRMatcher
from src.matching.pair_selector import select_pairs
from src.sfm.colmap_runner import run_colmap
from src.stitching.mosaic import stitch_facade


def _detect_off_wall_images(
    reconstruction: "pycolmap.Reconstruction",
    plane,
    min_gap_ratio: float = 2.5,
    max_exclude_fraction: float = 0.5,
    plane_distance_m: float = 3.0,
) -> set[str]:
    """Flags registered images whose 2D-3D observations barely touch the
    fitted facade plane -- confirmed real, 2026-09-12 (BACK facade, user-
    reported roofline artifacts + a misaligned corner): a shot aimed mostly
    at sky/rooftop-equipment/distant background still gets plenty of COLMAP
    feature matches and triangulated points (thousands, in the confirmed
    case), just essentially NONE of them near the actual wall plane --
    unlike a coverage-count or per-image color-variance signal (both tried
    on this same facade and confirmed unreliable, see rectification.py's
    _mask_disagreement_islands docstring for that dead end), this reuses
    SfM's own triangulation as the "is this image actually looking at the
    wall" signal, since it's the one thing this pipeline already computes
    that's grounded in real 3D geometry rather than 2D pixel statistics.
    Excluding these before a second COLMAP pass keeps their unrelated
    background matches from ever entering the pose graph, instead of only
    filtering pixels after the fact.

    Rather than a fixed absolute point-count threshold (only valid for one
    facade's own scale/GSD/point density), this looks for the largest
    RELATIVE gap in the sorted per-image on-wall-point counts and cuts
    there -- confirmed on real BACK data: a ~40-image low cluster (0-250
    points) sat behind a >3x gap from the rest (775+), a clear "these images
    see something completely different" split, not a smooth continuum.
    Candidate cut points are restricted to the lower `max_exclude_fraction`
    of images (a real split separates a MINORITY of bad shots from the
    majority, not the reverse) and the gap must clear `min_gap_ratio`
    (CLAUDE.local.md's own "extreme, not mild, outlier" convention) or
    nothing is excluded at all -- a facade whose images are all consistently
    on-target shouldn't lose any coverage just because SOME gap, however
    small, exists somewhere in the sorted list."""
    normal = np.cross(plane.e_u, plane.e_v)
    normal = normal / np.linalg.norm(normal)

    counts: dict[str, int] = {}
    for img in reconstruction.images.values():
        image_id = Path(img.name).stem
        on_wall = 0
        for p in img.points2D:
            if not p.has_point3D() or p.point3D_id not in reconstruction.points3D:
                continue
            point3d = reconstruction.points3D[p.point3D_id]
            if abs(float(np.dot(point3d.xyz - plane.origin, normal))) < plane_distance_m:
                on_wall += 1
        counts[image_id] = on_wall

    sorted_items = sorted(counts.items(), key=lambda item: item[1])
    n = len(sorted_items)
    if n < 4:
        return set()

    max_cut = max(1, int(n * max_exclude_fraction))
    best_gap_ratio = 1.0
    best_cut_idx = 0  # exclude sorted_items[:best_cut_idx]
    for i in range(1, min(max_cut, n - 1) + 1):
        lo = sorted_items[i - 1][1]
        hi = sorted_items[i][1]
        ratio = (hi + 1) / (lo + 1)
        if ratio > best_gap_ratio:
            best_gap_ratio = ratio
            best_cut_idx = i

    excluded = set()
    if best_gap_ratio >= min_gap_ratio and best_cut_idx > 0:
        excluded = {image_id for image_id, _ in sorted_items[:best_cut_idx]}

    # Absolute floor, independent of the relative-gap search above: an image
    # with exactly 0 on-wall points needs no ratio reasoning at all -- it is
    # definitionally not showing the wall. Confirmed real, 2026-09-17 (BACK
    # facade, LoFTR matching): the relative-gap search alone missed a clean
    # 29-image cluster of EXACT ZEROS, because LoFTR's much smaller overall
    # point budget (capped to stay COLMAP-tractable, see
    # loftr_colmap_bridge.py) turned what used to be a sharp cliff (SIFT:
    # ~250 vs 775+, a >3x jump) into a smooth 0->419 ramp with no single gap
    # clearing `min_gap_ratio` anywhere -- every adjacent-rank ratio near the
    # zeros stayed under threshold (2.0 at best) even though "zero" itself is
    # an unambiguous signal no ratio should be needed to see. Still respects
    # `max_exclude_fraction` (a facade whose images are ALL near-zero -- e.g.
    # a broken reconstruction -- shouldn't lose every image to this rule).
    zero_ids = {image_id for image_id, count in sorted_items if count == 0}
    if zero_ids and len(zero_ids) <= max_cut:
        excluded |= zero_ids

    return excluded


def _in_plane_roll_deg(img: "pycolmap.Image", plane) -> float:
    """This camera's own in-plane rotation ("roll" relative to the facade),
    in degrees -- projects the camera's world-frame "up" vector onto the
    facade plane and measures its angle against the plane's own (e_u, e_v)
    basis. A drone flying a smooth path (including orbiting a building
    corner) produces a smoothly-varying sequence of this angle when images
    are read in DJI capture order; see _detect_rotation_outlier_images."""
    R = img.cam_from_world().rotation.matrix()
    cam_up_world = R.T @ np.array([0.0, -1.0, 0.0])
    normal = plane.normal
    in_plane = cam_up_world - np.dot(cam_up_world, normal) * normal
    comp_u = float(np.dot(in_plane, plane.e_u))
    comp_v = float(np.dot(in_plane, plane.e_v))
    return float(np.degrees(np.arctan2(comp_u, comp_v)))


def _wrap_deg(angle_deg: float) -> float:
    """Wrap to (-180, 180] so a jump like 179 -> -179 reads as +2, not -358."""
    return (angle_deg + 180.0) % 360.0 - 180.0


def _detect_rotation_outlier_images(
    reconstruction: "pycolmap.Reconstruction",
    plane,
    images_dir: str,
    keep_ids: set[str],
    min_jump_deg: float = 5.0,
    min_redundant_fraction: float = 0.9,
) -> set[str]:
    """Flags a registered image whose in-plane roll (`_in_plane_roll_deg`)
    reverses direction relative to its DJI-capture-order neighbors -- jumping
    sharply one way in, then sharply back out, rather than continuing the
    same direction its neighbors are already rotating in -- AND only
    excludes it if other images already substantially cover the same canvas
    region, so a genuinely unique viewpoint never silently loses coverage
    just because its pose looks unusual.

    Confirmed real, 2026-09 (BACK facade, DJI_0089/DJI_0131): both sit inside
    a drone-orbiting-a-corner maneuver (two clusters of rapidly-changing
    roll), but unlike their neighbors, which smoothly interpolate (same-sign
    jump in and out), each one jumps sharply in one direction and then
    sharply back (+7.48 deg in, -12.18 deg out for DJI_0089; +7.18 deg in,
    -12.36 deg out for DJI_0131 -- both re-derived here from a fresh COLMAP
    run and matching the original manually-found numbers almost exactly) --
    a "notch" in an otherwise smooth sequence, consistent with a
    less-reliable pose estimate rather than a real physical rotation (no real
    drone orbit reverses its own sweep direction for exactly one frame and
    then immediately continues the original sweep). Manually excluding just
    these two from the stitch input fixed a seam-misalignment artifact at the
    exact canvas location their projected footprint touched (confirmed via a
    direct before/after crop comparison of a full fresh COLMAP run with/
    without them) -- this generalizes that one-off manual test into an
    automatic rule.

    Sign reversal (`jump_in * jump_out < 0`), not distance from a linear
    interpolation of the two neighbors, is what actually isolates this
    pattern -- a naive "how far is this image's angle from the straight-line
    interpolation of its neighbors" check was tried first and MISSED both
    known cases, because DJI_0089/DJI_0131's own angle sits right at the
    +-180 deg wrap boundary (-177 deg, i.e. having continued essentially the
    same direction as its jump_in past +180), which happens to land close to
    that interpolated midpoint even though the jump_in/jump_out relationship
    is clearly anomalous. Re-verified against this exact sequence: the sign-
    reversal rule isolates exactly DJI_0089 and DJI_0131 and nothing else out
    of all 68 registered images.

    `min_jump_deg` requires BOTH the incoming and outgoing angle jumps to be
    meaningfully large before even considering a candidate -- during a
    straight, non-orbiting flight leg the roll barely changes at all, and
    ordinary pose-estimation jitter there should never trigger this.

    The redundant-coverage check reuses `rectify_images`'s own per-image
    masks (expensive -- loads and warps every image -- but only ever paid
    when there's at least one rotation-angle candidate to check; the common
    case of a facade with no such candidates returns immediately without
    calling it at all, mirroring _detect_off_wall_images's cheap default
    path)."""
    images_by_id = {
        Path(img.name).stem: img
        for img in reconstruction.images.values()
        if Path(img.name).stem in keep_ids
    }
    if len(images_by_id) < 3:
        return set()

    ordered_ids = sorted(images_by_id.keys())
    angles = {iid: _in_plane_roll_deg(images_by_id[iid], plane) for iid in ordered_ids}

    candidates: set[str] = set()
    for i in range(1, len(ordered_ids) - 1):
        prev_id, cur_id, next_id = ordered_ids[i - 1], ordered_ids[i], ordered_ids[i + 1]
        jump_in = _wrap_deg(angles[cur_id] - angles[prev_id])
        jump_out = _wrap_deg(angles[next_id] - angles[cur_id])
        if abs(jump_in) < min_jump_deg or abs(jump_out) < min_jump_deg:
            continue
        if jump_in * jump_out < 0:
            candidates.add(cur_id)

    if not candidates:
        return set()

    warped, canvas_size, _ = rectify_images(reconstruction, plane, images_dir)
    canvas_w, canvas_h = canvas_size
    others_coverage = np.zeros((canvas_h, canvas_w), dtype=bool)
    for iid, w in warped.items():
        if iid in candidates or iid not in images_by_id:
            continue
        x, y = w.corner
        ww, hh = w.size
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(canvas_w, x + ww), min(canvas_h, y + hh)
        if x1 <= x0 or y1 <= y0:
            continue
        others_coverage[y0:y1, x0:x1] |= w.mask[y0 - y : y1 - y, x0 - x : x1 - x] > 0

    excluded: set[str] = set()
    for iid in candidates:
        w = warped.get(iid)
        if w is None:
            continue
        x, y = w.corner
        ww, hh = w.size
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(canvas_w, x + ww), min(canvas_h, y + hh)
        if x1 <= x0 or y1 <= y0:
            continue
        own_mask = w.mask[y0 - y : y1 - y, x0 - x : x1 - x] > 0
        own_area = int(own_mask.sum())
        if own_area == 0:
            continue
        redundant_area = int((own_mask & others_coverage[y0:y1, x0:x1]).sum())
        if redundant_area / own_area >= min_redundant_fraction:
            excluded.add(iid)

    return excluded


def _run_colmap_mapping_only(
    facade_id: str,
    colmap_images_dir: str,
    colmap_filenames: list[str],
    workspace_dir: Path,
    cfg: Config,
    logger,
    by_id: dict[str, ImageMetadata],
    catalog: list[ImageMetadata],
    utm_epsg: int | None,
    segment: FacadeSegment | None,
    matcher: TimeoutLoFTRMatcher | None = None,
):
    """COLMAP mapping + UTM alignment + facade-plane fit, WITHOUT
    rectify_and_blend (no seam-finding/blending -- the expensive part). Used
    for stage 1 (2026-09-12, 사용자 확정: "1단계는 mapping만") where the only
    thing needed is a real, metric reconstruction+plane to feed
    _detect_off_wall_images and _estimate_coverage_ratio -- this result is
    never itself a final deliverable, so paying for a full render would be
    pure waste. `_run_colmap_and_rectify_once` (stage 2, the actual final
    COLMAP output) builds on top of this same function rather than
    duplicating it.

    Returns (colmap_result, reconstruction, plane) -- reconstruction/plane
    are None if that stage wasn't reached/didn't succeed (not enough images
    registered, no GPS to align to UTM, etc.). Raises ImportError if
    pycolmap itself isn't installed (caller's concern)."""
    import pycolmap

    colmap_result = run_colmap(
        facade_id, colmap_images_dir, colmap_filenames,
        workspace_dir=workspace_dir, logger=logger,
        catalog=catalog, cfg=cfg, matcher=matcher,
    )
    reconstruction = None
    plane = None
    if colmap_result.sparse_dir and colmap_result.num_images_registered >= 4:
        try:
            effective_utm_epsg = utm_epsg if utm_epsg is not None else estimate_utm_epsg(catalog)
            if effective_utm_epsg is None:
                log_event(
                    logger, "warning", "no GPS on any image, cannot align CM reconstruction for rectification",
                    facade_id=facade_id,
                )
            else:
                reconstruction = pycolmap.Reconstruction(colmap_result.sparse_dir)
                aligned = align_reconstruction_to_utm(reconstruction, by_id, effective_utm_epsg)
                if not aligned:
                    log_event(
                        logger, "warning", "CM reconstruction has too little GPS coverage to align to UTM, skipping rectification",
                        facade_id=facade_id,
                    )
                    reconstruction = None
                else:
                    if segment is not None:
                        reference_altitudes = [
                            by_id[Path(img.name).stem].gps.altitude_m
                            for img in reconstruction.images.values()
                            if Path(img.name).stem in by_id and by_id[Path(img.name).stem].gps.altitude_m is not None
                        ]
                        plane = facade_plane_from_segment(segment, reference_altitudes)
                    else:
                        plane = facade_plane_from_reconstruction(reconstruction)
        except Exception as exc:
            log_event(logger, "warning", "CM 정렬/평면 계산 실패", facade_id=facade_id, error=str(exc))
            reconstruction = None
            plane = None
    return colmap_result, reconstruction, plane


def _estimate_coverage_ratio(reconstruction, plane, images_dir: str):
    """Cheap coverage_ratio estimate: warps just each image's binary mask onto
    the facade plane (rectify_images) and unions them via paste_max -- skips
    rectify_and_blend's expensive seam-finding/blending entirely, since this
    exists only as a baseline for the stage-1-vs-stage-2 coverage safety net
    (2026-09-12, 사용자 승인): "이번엔(BACK) coverage가 줄지 않아 안전했다"는
    사실이 "항상 안전하다"를 보장하지 않으므로, 필터링 후 coverage_ratio가
    필터링 전보다 떨어지면 명시적으로 경고한다 -- 제외된 이미지가 실은 어떤
    벽면을 고유하게 커버하고 있었을 가능성 신호.

    Returns (coverage_ratio, warped, canvas_size) -- `warped` is also handed
    to geometry/coverage.py's compute_overlap_report by this function's
    caller (2026-09-15) so that check doesn't pay for a second rectify_images
    pass just to get the same per-image ROIs/masks this one already computed."""
    from src.stitching.mosaic import paste_max

    warped, canvas_size, _ = rectify_images(reconstruction, plane, images_dir)
    canvas_w, canvas_h = canvas_size
    if canvas_w <= 0 or canvas_h <= 0:
        return None, warped, canvas_size
    observed = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    for w in warped.values():
        paste_max(observed, w.mask, w.corner)
    coverage_ratio = float(np.count_nonzero(observed)) / float(observed.size)
    return coverage_ratio, warped, canvas_size


def _run_colmap_and_rectify_once(
    facade_id: str,
    colmap_images_dir: str,
    colmap_filenames: list[str],
    workspace_dir: Path,
    cfg: Config,
    logger,
    by_id: dict[str, ImageMetadata],
    catalog: list[ImageMetadata],
    utm_epsg: int | None,
    segment: FacadeSegment | None,
    matcher: TimeoutLoFTRMatcher | None = None,
):
    """One COLMAP-mapping + plane-rectification attempt for the given image
    list. Returns (colmap_result, reconstruction, plane, rect_result) -- any
    field past colmap_result can be None if that stage wasn't reached/didn't
    succeed (not enough images registered, no GPS to align to UTM, etc.),
    mirroring the single-attempt code this replaces so a retry attempt fails
    exactly as gracefully as the original one did. Raises ImportError if
    pycolmap itself isn't installed (caller's concern, same as before)."""
    colmap_result, reconstruction, plane = _run_colmap_mapping_only(
        facade_id, colmap_images_dir, colmap_filenames, workspace_dir, cfg, logger, by_id, catalog, utm_epsg, segment,
        matcher=matcher,
    )
    rect_result = None
    if reconstruction is not None and plane is not None:
        try:
            rect_result = rectify_and_blend(
                facade_id, reconstruction, plane, colmap_images_dir, cfg,
                colmap_mean_reprojection_error_px=colmap_result.mean_reprojection_error_px,
            )
        except Exception as exc:
            log_event(logger, "warning", "CM-pose rectification failed", facade_id=facade_id, error=str(exc))
    return colmap_result, reconstruction, plane, rect_result


def _run_facade_pipeline(
    facade_id: str,
    catalog: list[ImageMetadata],
    matcher: TimeoutLoFTRMatcher,
    cfg: Config,
    output_root: Path,
    logger,
    run_colmap_fallback: bool = True,
    segment: FacadeSegment | None = None,
    utm_epsg: int | None = None,
    output_dir_override: Path | None = None,
) -> Path | None:
    """MATCHED -> GEOMETRY_SOLVED -> STITCHED for one facade's image set. Returns the
    output dir, or None if there weren't enough passing pairs to stitch anything.

    2026-09-12 재구조화 (사용자 확정, CLAUDE.local.md 원칙 #10 명시적 오버라이드): COLMAP은
    더 이상 "H체인 품질 게이트 실패시에만 도는 폴백"이 아니다. `run_colmap_fallback=True`인
    한(이름은 과거 의미의 잔재로 그대로 둠) 모든 facade에서:
      1단계 -- 전체 원본 이미지로 COLMAP mapping만(rectify 생략) 먼저 무조건 실행해
      "이 이미지가 실제로 벽을 찍었는가"(`_detect_off_wall_images`)를 판정하고 필터링.
      2단계 -- 필터링된 이미지로, H체인의 `needs_colmap_fallback` 판정과 무관하게 COLMAP을
      처음부터 완전히 다시 실행(mapping+rectify)해 최종 COLMAP 산출물을 만든다.
    시간 비용(모든 facade가 COLMAP을 최소 두 번 거침)은 사용자가 "정확성이 중요하다,
    시간은 상관없다"며 명시적으로 감수하기로 확정한 것. 1단계/2단계 각각의 coverage를
    비교하는 안전장치도 여기 포함(아래 참고) -- "이번엔 우연히 안전했다"를 "항상
    안전하다"로 착각하지 않기 위함."""
    output_dir = output_dir_override if output_dir_override is not None else output_root / facade_id / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    by_id = {m.image_id: m for m in catalog}
    # Full, unfiltered catalog/by_id -- kept aside because `catalog`/`by_id` get
    # reassigned below as off-wall (and formerly rotation-outlier) exclusion
    # filters them; geometry/exclusion_safety.py's rescue step needs to pull
    # a rescued image's metadata back from the ORIGINAL set, not the filtered one.
    full_catalog = list(catalog)

    # === 1단계 COLMAP: 전체 이미지, mapping만, 벽면 미노출 이미지 자동 감지/필터링 ===
    stage1_coverage_estimate: float | None = None
    if run_colmap_fallback:
        source_dirs = {str(Path(m.file_path).parent) for m in catalog}
        if len(source_dirs) != 1:
            log_event(
                logger, "warning", "facade images span multiple source dirs, skipping CM entirely",
                facade_id=facade_id, source_dirs=list(source_dirs),
            )
            run_colmap_fallback = False
        else:
            colmap_images_dir = next(iter(source_dirs))
            colmap_filenames = [Path(m.file_path).name for m in catalog]
            try:
                t_stage1 = time.time()
                # 1단계는 의도적으로 SIFT 그대로 유지 -- LoFTR로 바꾸면 배경(먼 산/지형)을
                # SIFT보다 훨씬 잘 매칭해서(2026-09-17 실측: 같은 121장 기준 캐노니컬 포인트가
                # SIFT 79k -> LoFTR 99만개) 필터링 전 평면 피팅이 심하게 부풀고(109m -> 330m+),
                # 그 왜곡된 평면 때문에 벽을 정상적으로 찍은 이미지(DJI_0117 등)까지
                # _detect_off_wall_images가 "벽 아님"으로 오판하는 문제를 실측으로 확인
                # (키포인트 개수 상한/공간균등 샘플링 둘 다 시도했지만 근본 원인은 매칭 자체가
                # 아니라 배경 콘텐츠 자체를 SIFT보다 더 많이 정확하게 잡아내는 것이라 해결 안 됨).
                # SIFT는 배경을 이만큼 안 잡아서 1단계(필터링 판단용) 평면은 항상 정상 범위였음
                # -- 1단계는 SIFT로 필터링만 담당하고, LoFTR은 이미 필터링된 이미지로 도는
                # 2단계(_run_colmap_and_rectify_once)에만 적용한다.
                stage1_colmap_result, stage1_reconstruction, stage1_plane = _run_colmap_mapping_only(
                    facade_id, colmap_images_dir, colmap_filenames,
                    output_dir / "colmap_stage1", cfg, logger, by_id, catalog, utm_epsg, segment,
                )
                log_event(
                    logger, "info", "1단계 COLMAP(필터용) 완료",
                    stage="COLMAP_STAGE1", facade_id=facade_id,
                    elapsed_s=round(time.time() - t_stage1, 2),
                    num_images_requested=stage1_colmap_result.num_images_requested,
                    num_images_registered=stage1_colmap_result.num_images_registered,
                    num_points3d=stage1_colmap_result.num_points3d,
                    mean_reprojection_error_px=stage1_colmap_result.mean_reprojection_error_px,
                )
                if stage1_reconstruction is not None and stage1_plane is not None:
                    stage1_warped = None
                    try:
                        stage1_coverage_estimate, stage1_warped, _ = _estimate_coverage_ratio(
                            stage1_reconstruction, stage1_plane, colmap_images_dir,
                        )
                    except Exception as exc:
                        log_event(logger, "warning", "1단계 coverage 추정 실패", facade_id=facade_id, error=str(exc))

                    if stage1_warped:
                        try:
                            overlap_cfg = cfg.capture if "capture" in cfg else None
                            overlap_report = compute_overlap_report(
                                facade_id, stage1_warped,
                                horizontal_overlap_target=float(overlap_cfg.horizontal_overlap_target) if overlap_cfg else 0.80,
                                vertical_overlap_target=float(overlap_cfg.vertical_overlap_target) if overlap_cfg else 0.80,
                                minimum_overlap=float(overlap_cfg.minimum_overlap) if overlap_cfg else 0.70,
                            )
                            atomic_write_json(output_dir / f"{facade_id}_overlap_report.json", asdict(overlap_report))
                            if overlap_report.needs_retake:
                                log_event(
                                    logger, "warning", "중복도 미달 구간 감지 -- 재촬영 검토 필요",
                                    stage="OVERLAP_CHECK", facade_id=facade_id,
                                    gap_count=len(overlap_report.gaps),
                                    gaps=[asdict(g) for g in overlap_report.gaps][:20],
                                    horizontal_overlap_mean=overlap_report.horizontal_overlap_mean,
                                    vertical_overlap_mean=overlap_report.vertical_overlap_mean,
                                )
                            else:
                                log_event(
                                    logger, "info", "중복도 검증 통과",
                                    stage="OVERLAP_CHECK", facade_id=facade_id,
                                    horizontal_overlap_mean=overlap_report.horizontal_overlap_mean,
                                    vertical_overlap_mean=overlap_report.vertical_overlap_mean,
                                )
                        except Exception as exc:
                            log_event(logger, "warning", "중복도 검증 실패", facade_id=facade_id, error=str(exc))

                    off_wall_ids = _detect_off_wall_images(stage1_reconstruction, stage1_plane)
                    if off_wall_ids:
                        log_event(
                            logger, "info", "벽면이 거의 안 보이는 이미지 자동 감지 -- 제외",
                            stage="OFF_WALL_DETECTED", facade_id=facade_id,
                            excluded_count=len(off_wall_ids), excluded_image_ids=sorted(off_wall_ids),
                        )
                        catalog = [m for m in catalog if m.image_id not in off_wall_ids]
                        by_id = {m.image_id: m for m in catalog}

                    # 2026-09-15 사용자 확정: _detect_rotation_outlier_images(11376db) 자동 제외
                    # 비활성화. BACK facade 실측으로 확인된 문제 -- 이 함수의 "중복 커버리지"
                    # 안전장치는 제외 대상(DJI_0089/DJI_0131) 자기 자신의 캔버스 위치만
                    # 확인하는데, COLMAP 번들조정은 전체 이미지를 동시에 푸는 전역 최적화라
                    # 이 두 장을 빼면 그 둘과 물리적으로 안 겹치는 다른 위치(BACK 반대쪽
                    # 코너, DJI_0162/DJI_0164 부근)의 상대 포즈까지 같이 틀어짐 -- 실측으로
                    # V005(이 두 장 포함, 68장 등록)는 그 코너가 깨끗했고, 11376db 도입 이후
                    # (66장, 이 두 장 제외)는 같은 코너가 서로 어긋난 두 이미지로 쪼개져
                    # 깨짐을 확인. DJI_0089/DJI_0131을 다시 넣고 COLMAP을 재실행하니 그
                    # 코너가 다시 V005 수준으로 복구됨(직접 재현 확인). 이 두 장 자체는 실제
                    # DJI XMP GimbalRollDegree가 이웃 프레임 대비 딱 한 프레임만 -10도가량
                    # 튀었다가 바로 복귀하는 순간적 짐벌 흔들림 구간 -- 원래 이 함수가
                    # 고치려던 문제(그 두 장 자신의 위치에서의 아티팩트)는 여전히 실재할 수
                    # 있으므로 이 함수 자체는 남겨두되(향후 수동 검토/튜닝용), 자동 실행에서는
                    # 제외 -- 전역 최적화에 예측 못한 부작용을 주는 자동 규칙을 로컬 안전장치
                    # 하나만 믿고 파이프라인에 그대로 두는 것이 더 위험하다는 판단.
            except ImportError:
                log_event(logger, "warning", "pycolmap not installed, skipping CM entirely", facade_id=facade_id)
                run_colmap_fallback = False
            except Exception as exc:
                # run_colmap()/COLMAP 내부가 여기서 그대로 예외를 던질 수 있음(자체
                # try/except 없음, colmap_runner.py 확인) -- 예전엔 COLMAP이 조건부
                # 폴백이라 실패해도 H체인 결과는 그대로 살아남았는데, 지금은 COLMAP이
                # H체인보다 먼저 도는 필수 단계가 됐으므로, 여기서 안 잡으면 1단계
                # COLMAP 실패가 H체인까지 통째로 크래시시킨다 -- 필터링 없이 전체
                # catalog로 H체인은 계속 진행하도록 방어.
                log_event(
                    logger, "warning", "1단계 COLMAP 실패 -- 필터링 없이 전체 이미지로 계속 진행",
                    stage="COLMAP_STAGE1_FAILED", facade_id=facade_id, error=str(exc),
                )

    # 2026-09-17 확정 (사용자 승인, 실측 검증 완료 -- test_e2e_sift_stage1_loftr_stage2.py:
    # H체인 없이 SIFT 1단계 -> 오프월 필터 -> LoFTR 2단계만으로 68장 정상 재구성,
    # coverage 92.3%, 에러 없음): COLMAP 1단계가 성공하면 H체인(전체 쌍 LoFTR 매칭 +
    # 품질 게이트 + 그래프 스티칭)을 아예 건너뛴다. COLMAP이 모든 facade에서 항상
    # 도는 필수 단계가 된 이상(2026-09-12 재구조화) H체인의 "품질 게이트 실패시
    # 폴백" 역할은 이미 없어졌고, 남은 유일한 역할이었던 "2단계에 넘길 이미지 목록
    # 제공"(기존엔 needed_ids = H체인 품질 게이트 통과 이미지)도 COLMAP 1단계
    # 자체의 벽면-미노출 판정(catalog, 위에서 이미 필터링됨)으로 그대로 대체된다 --
    # H체인은 COLMAP 전체 쌍 매칭이라 시간이 오래 걸리는데(1단계 COLMAP보다 오래
    # 걸리는 경우도 실측 확인됨) 이제 그 결과를 쓰는 곳이 없으므로 계산 자체가
    # 낭비. COLMAP 1단계가 실패한 경우(아래 skip_h_chain=False)는 H체인이 유일한
    # 산출물이 되므로 기존 그대로 전체 실행 -- 원래의 폴백 역할은 그대로 유지된다.
    skip_h_chain = bool(run_colmap_fallback) and stage1_reconstruction is not None and stage1_plane is not None

    preview_state = {"prev_path": None}
    geometry_results: list[GeometryResult] = []
    result = None

    if skip_h_chain:
        images = {}
        for m in catalog:
            img = imread_unicode(m.file_path, cv2.IMREAD_COLOR)
            if img is None:
                log_event(logger, "warning", "failed to read image", image_id=m.image_id)
                continue
            images[m.image_id] = img
        log_event(
            logger, "info", "COLMAP 1단계 성공 -- H체인 매칭 생략",
            stage="H_CHAIN_SKIPPED", facade_id=facade_id, image_count=len(images),
        )
    else:
        t0 = time.time()
        pairs = select_pairs(catalog, cfg)
        log_event(
            logger, "info", "pair graph built",
            stage="PAIR_GRAPH_BUILT", facade_id=facade_id, pair_count=len(pairs),
            elapsed_s=round(time.time() - t0, 2),
        )

        t0 = time.time()
        for i, pair in enumerate(pairs):
            path_a = by_id[pair.image_a].file_path
            path_b = by_id[pair.image_b].file_path
            try:
                match = matcher.match(path_a, path_b)
            except MatchTimeoutError as exc:
                geom = GeometryResult(
                    image_a=pair.image_a, image_b=pair.image_b,
                    status=GeometryFailureCode.MATCH_TIMEOUT.value,
                )
                geometry_results.append(geom)
                log_event(
                    logger, "warning", "pair match timed out, skipping",
                    stage="MATCH_GEOMETRY", facade_id=facade_id,
                    image_a=pair.image_a, image_b=pair.image_b,
                    status=geom.status, progress=f"{i + 1}/{len(pairs)}", error=str(exc),
                )
                continue

            if match.num_matches < int(cfg.geometry.min_matches):
                geom = GeometryResult(
                    image_a=pair.image_a, image_b=pair.image_b,
                    status=GeometryFailureCode.LOW_MATCH.value, num_matches=match.num_matches,
                )
            else:
                geom = estimate_homography(match, inl_th_px=float(cfg.geometry.ransac_reproj_threshold_px))
                geom = apply_quality_gate(geom, cfg)
            geometry_results.append(geom)

            log_event(
                logger, "info", "pair processed",
                stage="MATCH_GEOMETRY", facade_id=facade_id,
                image_a=pair.image_a, image_b=pair.image_b,
                matches=geom.num_matches, inliers=geom.num_inliers,
                inlier_ratio=round(geom.inlier_ratio, 4),
                median_reproj_px=(
                    round(geom.median_reprojection_error_px, 3)
                    if geom.median_reprojection_error_px is not None
                    else None
                ),
                status=geom.status,
                progress=f"{i + 1}/{len(pairs)}",
            )
        log_event(
            logger, "info", "matching + geometry complete",
            stage="GEOMETRY_SOLVED", facade_id=facade_id,
            ok_count=sum(1 for g in geometry_results if g.status == "OK"),
            failed_count=sum(1 for g in geometry_results if g.status != "OK"),
            elapsed_s=round(time.time() - t0, 2),
        )

        t0 = time.time()
        needed_ids = {g.image_a for g in geometry_results if g.status == "OK"} | {
            g.image_b for g in geometry_results if g.status == "OK"
        }
        if not needed_ids:
            log_event(
                logger, "warning", "facade has no geometry edge passing the quality gate, skipping stitch",
                stage="FAILED_GEOMETRY", facade_id=facade_id,
            )
            return None

        images = {}
        for image_id in needed_ids:
            img = imread_unicode(by_id[image_id].file_path, cv2.IMREAD_COLOR)
            if img is None:
                log_event(logger, "warning", "failed to read image", image_id=image_id)
                continue
            images[image_id] = img

        def _on_preview(canvas, i: int, total: int) -> None:
            new_path = output_dir / f"{facade_id}_live_preview_{i:03d}.jpg"
            imwrite_unicode(new_path, canvas, [cv2.IMWRITE_JPEG_QUALITY, 85])
            prev_path = preview_state["prev_path"]
            if prev_path is not None and prev_path.exists():
                try:
                    prev_path.unlink()
                except OSError:
                    pass
            preview_state["prev_path"] = new_path
            log_event(
                logger, "info", "미리보기 갱신",
                stage="PREVIEW_UPDATED", facade_id=facade_id,
                progress=f"{i}/{total}", preview_path=str(new_path),
            )

        result = stitch_facade(facade_id, images, geometry_results, cfg, on_preview=_on_preview)
        log_event(
            logger, "info", "facade stitched",
            stage="STITCHED",
            elapsed_s=round(time.time() - t0, 2), **asdict(result.quality),
        )
        if result.quality.needs_colmap_fallback:
            # 2026-09-12부터 이 값은 더 이상 COLMAP 실행 여부의 게이트가 아니다(2단계
            # COLMAP은 run_colmap_fallback인 한 항상 실행됨) -- H체인 자체 품질이
            # 부족했다는 진단 정보로만 남겨둔다.
            log_event(
                logger, "warning", "H체인 자체로는 품질 기준 미달 (COLMAP 2단계는 별도로 항상 실행됨)",
                stage="NEEDS_MANUAL_REVIEW", facade_id=facade_id,
                reasons=result.quality.colmap_fallback_reasons,
            )

    # === 2단계 COLMAP: 필터링된 이미지로 완전히 새로 실행(mapping+rectify), H체인
    # 품질 판정과 무관하게 항상 실행, 결과는 H체인 결과와 함께 항상 저장 ===
    if run_colmap_fallback:
        t_colmap = time.time()
        source_dirs = {str(Path(by_id[iid].file_path).parent) for iid in images.keys()}
        if len(source_dirs) != 1:
            log_event(
                logger, "warning", "facade images span multiple source dirs, skipping CM",
                facade_id=facade_id, source_dirs=list(source_dirs),
            )
        else:
            colmap_images_dir = next(iter(source_dirs))
            colmap_filenames = [Path(by_id[iid].file_path).name for iid in images.keys()]
            try:
                # workspace_dir lives *inside* this run's own output_dir (not
                # output_dir.parent) so two different --output-dir runs of the
                # same facade (different versions, or repeated test trials)
                # never share one COLMAP workspace -- run_colmap() always wipes
                # database.db at start (colmap_runner.py), so a shared location
                # was never actually reused for anything, only a collision risk
                # if two runs' lifetimes ever overlapped or a debugging script
                # reused a stale one from a different run.
                colmap_result, reconstruction, plane, rect_result = _run_colmap_and_rectify_once(
                    facade_id, colmap_images_dir, colmap_filenames,
                    output_dir / "colmap", cfg, logger, by_id, catalog, utm_epsg, segment,
                    matcher=matcher,
                )
                log_event(
                    logger, "info", "2단계 COLMAP(최종) 완료",
                    stage="COLMAP_STAGE2", facade_id=facade_id,
                    elapsed_s=round(time.time() - t_colmap, 2),
                    num_images_requested=colmap_result.num_images_requested,
                    num_images_registered=colmap_result.num_images_registered,
                    num_points3d=colmap_result.num_points3d,
                    mean_reprojection_error_px=colmap_result.mean_reprojection_error_px,
                )
                atomic_write_json(output_dir / f"{facade_id}_colmap_report.json", asdict(colmap_result))

                # 2026-09-16 사용자 확정("범용적이어야 합니다, 한곳에 특화되면 안됨"): off_wall_ids
                # 제외가 이 facade의 다른 위치(DJI_0076/78/79/80/120류) 포즈 품질을 실제로
                # 떨어뜨렸는지 검증 -- src/geometry/exclusion_safety.py 참고(위치/회전/투영
                # 면적/국소 스케일 왜곡 4가지 시도 실패 후 실측으로 확정한 방식: 실세계 공유
                # 좌표에서의 커버리지 카운트 감소 + 감소 지점 근처 실제 on-wall 포인트 증거
                # 기반 구조 후보 선정). off_wall_ids가 비어있으면(제외 자체가 없었으면) 건너뜀.
                if off_wall_ids and stage1_reconstruction is not None and stage1_plane is not None and reconstruction is not None:
                    try:
                        from src.geometry.exclusion_safety import check_exclusion_safety

                        safety = check_exclusion_safety(
                            stage1_reconstruction, reconstruction, plane, off_wall_ids,
                        )
                        log_event(
                            logger, "info", "제외 안전성 검증 완료",
                            stage="EXCLUSION_SAFETY_CHECK", facade_id=facade_id,
                            flagged_point_count=int(safety.flagged_mask.sum()),
                            total_point_count=len(safety.flagged_mask),
                            rescued_count=len(safety.rescued_ids),
                        )
                        if safety.needs_rerun:
                            # 2026-09-16 사용자 확정: 자동 재실행 비활성화. 실측으로 확인된 문제 --
                            # 구조 후보를 재포함해 COLMAP을 다시 돌리는 것 자체가 번들조정 전체를
                            # 다시 계산시켜, 이번엔 DJI_0120류가 고쳐지는 대신 무관했던 DJI_0118이
                            # 새로 어긋나는 부작용이 실제로 발생함(V011에서 확인, 재현 가능).
                            # 즉 "재실행하면 어디가 새로 깨질지"를 사전에 안전하게 보장할 방법이
                            # 없어서, 자동으로 고치는 대신 감지·기록만 하고 실제 재실행은 사람이
                            # 검토 후 수동으로 결정하도록 함(회전 이상치 규칙과 동일한 원칙).
                            atomic_write_json(
                                output_dir / f"{facade_id}_exclusion_safety_flags.json",
                                {"rescued_candidate_ids": sorted(safety.rescued_ids),
                                 "flagged_point_count": int(safety.flagged_mask.sum()),
                                 "total_point_count": len(safety.flagged_mask)},
                            )
                            log_event(
                                logger, "warning",
                                "벽면 미노출 제외가 다른 위치의 커버리지 중복도를 떨어뜨림 -- "
                                "자동 재실행은 비활성화됨(재실행 자체가 새 부작용을 만들 수 있음 "
                                "확인됨), 수동 검토 권장",
                                stage="EXCLUSION_SAFETY_FLAGGED", facade_id=facade_id,
                                rescue_candidate_ids=sorted(safety.rescued_ids),
                            )
                    except Exception as exc:
                        log_event(logger, "warning", "제외 안전성 검증 실패 -- 건너뜀", facade_id=facade_id, error=str(exc))

                if rect_result is not None:
                    # 안전장치 (2026-09-12, 사용자 승인): 1단계(필터 전 전체) coverage 추정치보다
                    # 2단계(필터 후) 실제 coverage_ratio가 낮으면, 제외된 이미지가 실은 다른
                    # 이미지가 못 찍은 벽면을 고유하게 커버하고 있었을 가능성 신호 -- "이번엔
                    # 우연히 안전했다"(BACK: coverage가 줄지 않고 오히려 늘어남)를 "항상
                    # 안전하다"로 착각하지 않기 위함. 두 값이 서로 다른(필터 전/후로 이미지
                    # 구성이 다른) 캔버스 크기/bbox 기준이라 완벽히 동일한 척도는 아니지만,
                    # 실질적인 경고 신호로는 충분하다.
                    if stage1_coverage_estimate is not None and rect_result.quality.coverage_ratio < stage1_coverage_estimate:
                        log_event(
                            logger, "warning",
                            "필터링 후 coverage가 필터 전보다 낮음 -- 제외된 이미지가 "
                            "고유하게 커버하던 벽면이 있었을 수 있음, 수동 확인 권장",
                            stage="OFF_WALL_COVERAGE_REGRESSION", facade_id=facade_id,
                            stage1_coverage_estimate=round(stage1_coverage_estimate, 4),
                            stage2_coverage_ratio=round(rect_result.quality.coverage_ratio, 4),
                        )

                    if rect_result.analysis_image is not None:
                        imwrite_unicode(output_dir / f"{facade_id}_analysis_colmap.tif", rect_result.analysis_image)
                    if rect_result.visual_image is not None:
                        imwrite_unicode(output_dir / f"{facade_id}_visual_colmap.tif", rect_result.visual_image)
                    imwrite_unicode(output_dir / f"{facade_id}_observed_mask_colmap.tif", rect_result.observed_mask)
                    atomic_write_json(output_dir / f"{facade_id}_quality_report_colmap.json", asdict(rect_result.quality))
                    # 2026-09-11, 사용자 결정: RTK/알려진 마커 등 진짜 측량급
                    # calibration이 아직 없어 우선 COLMAP+일반 GPS EXIF 정렬
                    # (align_reconstruction_to_utm) 스케일을 그대로 쓰기로 함
                    # -- CLAUDE.local.md #26의 승인된 소스 목록(Surveyed control
                    # point/Known marker/BIM-CAD/RTK-GCP)엔 없는, 정밀도가 더
                    # 낮은 소스라는 걸 알고 쓰는 것이므로 reference_object_type에
                    # 그 출처를 남겨 나중에 실측 정밀도 요구가 생기면 구분 가능하게
                    # 함. plane.px_per_m은 그 자체로 오차가 있는 게 아니라(캔버스
                    # 해상도를 정의하는 상수, 지금은 항상 100.0) "그 px가 실제 몇
                    # m인지"의 신뢰도가 GPS 정렬 품질에 달려있다는 뜻.
                    atomic_write_json(
                        output_dir / f"{facade_id}_scale_colmap.json",
                        {
                            "px_per_m": plane.px_per_m,
                            "calibrated": True,
                            "reference_object_type": "gps_colmap_alignment",
                            "reference_length_mm": None,
                        },
                    )
                    _write_source_transform_artifacts(output_dir, facade_id, "_colmap", rect_result)
                    log_event(
                        logger, "info", "CM-rectified mosaic complete",
                        stage="RECTIFIED_COLMAP", facade_id=facade_id,
                        elapsed_s=round(time.time() - t_colmap, 2),
                        coverage_ratio=rect_result.quality.coverage_ratio,
                        image_count=rect_result.quality.image_count,
                    )
            except ImportError:
                log_event(
                    logger, "warning", "pycolmap not installed, cannot run CM",
                    facade_id=facade_id,
                )
            except Exception as exc:
                # H체인 결과(_analysis.tif 등)는 이 시점에 이미 저장 대상이 확정돼
                # 있으므로(아래 코드가 계속 실행됨), 2단계 COLMAP이 예기치 않게
                # 실패해도 H체인 산출물까지 잃지 않도록 여기서 잡는다.
                log_event(
                    logger, "warning", "2단계 COLMAP 실패 -- H체인 결과만 저장됨",
                    stage="COLMAP_STAGE2_FAILED", facade_id=facade_id, error=str(exc),
                )

    # H체인이 생략된 경우(skip_h_chain) result는 None -- H체인 전용 산출물
    # (_analysis.tif/_visual.tif/_observed_mask.tif/_quality_report.json/
    # source-transform)은 만들 근거 자체가 없으므로 쓰지 않는다. CheckCrackViewer
    # 쪽에서 이 파일들이 없을 때의 표시를 별도로 점검해야 함 -- 지금까지는 H체인이
    # 항상 돌아서 한 번도 없었던 적이 없었다.
    if result is not None:
        if result.analysis_image is not None:
            imwrite_unicode(output_dir / f"{facade_id}_analysis.tif", result.analysis_image)
        if result.visual_image is not None:
            imwrite_unicode(output_dir / f"{facade_id}_visual.tif", result.visual_image)
        imwrite_unicode(output_dir / f"{facade_id}_observed_mask.tif", result.observed_mask)

        atomic_write_json(output_dir / f"{facade_id}_quality_report.json", asdict(result.quality))
        _write_source_transform_artifacts(output_dir, facade_id, "", result)

    failed_pairs = [_asdict_pair(g) for g in geometry_results if g.status != "OK"]
    atomic_write_json(output_dir / f"{facade_id}_failed_pairs.json", failed_pairs)

    source_images = [
        {"image_id": iid, "file_path": by_id[iid].file_path} for iid in sorted(images.keys())
    ]
    atomic_write_json(output_dir / f"{facade_id}_source_images.json", source_images)

    # The live preview is superseded by the real analysis/visual mosaics now
    # that they exist — leaving it behind would be confusing clutter next to
    # the actual output (CLAUDE.local.md #28's output layout doesn't include it).
    if preview_state["prev_path"] is not None and preview_state["prev_path"].exists():
        try:
            preview_state["prev_path"].unlink()
        except OSError:
            pass

    log_event(logger, "info", "facade complete", stage="DONE", facade_id=facade_id, output_dir=str(output_dir))
    return output_dir


def _make_matcher(cfg: Config) -> TimeoutLoFTRMatcher:
    timeout_s = float(cfg.loftr.pair_timeout_s) if "pair_timeout_s" in cfg.loftr else 60.0
    return TimeoutLoFTRMatcher(cfg, timeout_s=timeout_s)


def _write_source_transform_artifacts(output_dir: Path, facade_id: str, suffix: str, result) -> None:
    """Persists per-image homography + seam-ownership map alongside the
    mosaic so a later, completely separate crack-detection run
    (tools/detect_cracks_folder.py) can compute each crack's
    source_observations (which original photo(s) it's actually visible in,
    and where) -- there is no in-memory link from that run back to this one,
    see MosaicResult's own doc comment. `suffix` mirrors the existing
    "_colmap" naming convention used for _analysis_colmap.tif etc., so
    detect_cracks_folder.py's existing COLMAP-preferred file-picking logic
    picks the matching homographies/seam-owner files for whichever analysis
    mosaic it actually loads."""
    if result.source_transforms is None or result.seam_owner_map is None:
        return
    homographies_payload = {
        image_id: {"H": st.H.tolist(), "width": st.width, "height": st.height}
        for image_id, st in result.source_transforms.items()
    }
    atomic_write_json(output_dir / f"{facade_id}_homographies{suffix}.json", homographies_payload)
    imwrite_unicode(output_dir / f"{facade_id}_seam_owner_map{suffix}.png", result.seam_owner_map)
    atomic_write_json(output_dir / f"{facade_id}_seam_owner_index{suffix}.json", result.seam_owner_index)


def _asdict_pair(g: GeometryResult) -> dict:
    return {
        "image_a": g.image_a, "image_b": g.image_b, "status": g.status,
        "num_matches": g.num_matches, "num_inliers": g.num_inliers,
        "inlier_ratio": round(g.inlier_ratio, 4),
    }


def run_facade_poc(
    facade_id: str,
    images_dir: str | Path,
    output_root: str | Path,
    config_path: str | Path = "config/pipeline.yaml",
    limit: int | None = None,
    run_colmap_fallback: bool = True,
    output_dir: str | Path | None = None,
) -> Path | None:
    """Phase 1: whole `images_dir` is manually assigned to one facade (#3.1).

    `output_dir`, if given, overrides the usual `output_root/facade_id/output`
    layout (#28) and writes straight there instead — used when a folder was
    picked directly (e.g. from the Viewer's "+ 폴더" flow) and the result is
    expected right next to the source images, not centralized under the
    project's facades/ tree.
    """
    cfg = load_config(config_path)
    output_root = Path(output_root)
    logger = get_logger("pipeline", log_dir="logs")

    t0 = time.time()
    catalog = build_catalog(images_dir)
    if limit is not None:
        catalog = catalog[:limit]
    for meta in catalog:
        meta.mission.facade_hint = facade_id
    save_catalog(catalog, "metadata/images.parquet")
    log_event(
        logger, "info", "metadata parsed",
        stage="METADATA_PARSED", facade_id=facade_id, image_count=len(catalog),
        elapsed_s=round(time.time() - t0, 2),
    )

    matcher = _make_matcher(cfg)
    try:
        return _run_facade_pipeline(
            facade_id, catalog, matcher, cfg, output_root, logger, run_colmap_fallback,
            output_dir_override=Path(output_dir) if output_dir is not None else None,
        )
    finally:
        matcher.close()


def run_building_poc(
    building_id: str,
    images_dir: str | Path,
    footprint_path: str | Path,
    utm_epsg: int,
    output_root: str | Path,
    config_path: str | Path = "config/pipeline.yaml",
    limit: int | None = None,
    min_images_per_facade: int = 5,
    only_facades: list[str] | None = None,
    run_colmap_fallback: bool = True,
) -> dict[str, Path | None]:
    """Phase 2: classify images against the real building footprint (#6), then
    stitch each Facade segment with enough images independently (#1)."""
    cfg = load_config(config_path)
    output_root = Path(output_root)
    logger = get_logger("pipeline", log_dir="logs")

    t0 = time.time()
    catalog = build_catalog(images_dir)
    if limit is not None:
        catalog = catalog[:limit]
    log_event(
        logger, "info", "metadata parsed",
        stage="METADATA_PARSED", building_id=building_id, image_count=len(catalog),
        elapsed_s=round(time.time() - t0, 2),
    )

    footprint = load_footprint_utm_txt(footprint_path, utm_epsg)
    segments = build_segments(footprint, building_id, cfg)
    assignments = classify_images(catalog, segments, cfg, utm_epsg)

    Path("metadata").mkdir(exist_ok=True)
    report = [
        {"image_id": a.image_id, "facades": a.facades, "role": a.role, "scores": a.scores}
        for a in assignments
    ]
    atomic_write_json(f"metadata/{building_id}_facade_assignment.json", report)

    facade_to_images: dict[str, list[str]] = {}
    for a in assignments:
        for fid in a.facades:
            facade_to_images.setdefault(fid, []).append(a.image_id)

    log_event(
        logger, "info", "facade classification complete",
        stage="FACADE_ASSIGNED", building_id=building_id,
        segment_count=len(segments),
        assigned_image_count=sum(1 for a in assignments if a.facades),
        unassigned_image_count=sum(1 for a in assignments if not a.facades),
        per_facade_image_counts={fid: len(ids) for fid, ids in facade_to_images.items()},
    )

    by_id = {m.image_id: m for m in catalog}
    matcher = _make_matcher(cfg)
    results: dict[str, Path | None] = {}
    try:
        for seg in segments:
            if only_facades is not None and seg.facade_id not in only_facades:
                continue
            image_ids = facade_to_images.get(seg.facade_id, [])
            if len(image_ids) < min_images_per_facade:
                log_event(
                    logger, "info", "facade skipped: below min_images_per_facade",
                    facade_id=seg.facade_id, image_count=len(image_ids), min_required=min_images_per_facade,
                )
                continue
            sub_catalog = [by_id[iid] for iid in image_ids]
            results[seg.facade_id] = _run_facade_pipeline(
                seg.facade_id, sub_catalog, matcher, cfg, output_root, logger, run_colmap_fallback,
                segment=seg, utm_epsg=utm_epsg,
            )
    finally:
        matcher.close()

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="CheckCrack facade stitch pipeline")
    parser.add_argument("command", choices=["run", "run-building"])
    parser.add_argument("--facade", help="required for 'run'")
    parser.add_argument("--building", help="required for 'run-building'")
    parser.add_argument("--footprint", help="required for 'run-building'")
    parser.add_argument("--utm-epsg", type=int, default=32610, help="run-building: UTM zone of --footprint")
    parser.add_argument("--images-dir", required=True)
    parser.add_argument("--output-dir", default="facades")
    parser.add_argument("--config", default="config/pipeline.yaml")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--min-images-per-facade", type=int, default=5)
    parser.add_argument("--only-facades", nargs="*", default=None)
    parser.add_argument("--no-colmap-fallback", action="store_true", help="skip running COLMAP even if triggered")
    args = parser.parse_args()

    if args.command == "run":
        if not args.facade:
            parser.error("--facade is required for 'run'")
        out = run_facade_poc(
            facade_id=args.facade,
            images_dir=args.images_dir,
            output_root=args.output_dir,
            config_path=args.config,
            limit=args.limit,
            run_colmap_fallback=not args.no_colmap_fallback,
        )
        print(f"done: {out}")
    else:
        if not args.building or not args.footprint:
            parser.error("--building and --footprint are required for 'run-building'")
        out = run_building_poc(
            building_id=args.building,
            images_dir=args.images_dir,
            footprint_path=args.footprint,
            utm_epsg=args.utm_epsg,
            output_root=args.output_dir,
            config_path=args.config,
            limit=args.limit,
            min_images_per_facade=args.min_images_per_facade,
            only_facades=args.only_facades,
            run_colmap_fallback=not args.no_colmap_fallback,
        )
        print(f"done: {out}")


if __name__ == "__main__":
    main()
