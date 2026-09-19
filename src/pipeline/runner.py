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
from src.geometry.homography import estimate_homography
from src.geometry.quality import apply_quality_gate
from src.geometry.rectification import (
    align_reconstruction_to_utm,
    compute_wall_region_canvas_mask,
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
    # definitionally not showing the wall. Still respects `max_exclude_fraction`
    # (a facade whose images are ALL near-zero -- e.g. a broken reconstruction --
    # shouldn't lose every image to this rule).
    zero_ids = {image_id for image_id, count in sorted_items if count == 0}
    if zero_ids and len(zero_ids) <= max_cut:
        excluded |= zero_ids

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


def _estimate_coverage_ratio(reconstruction, plane, images_dir: str) -> float | None:
    """Cheap coverage_ratio estimate: warps just each image's binary mask onto
    the facade plane (rectify_images) and unions them via paste_max -- skips
    rectify_and_blend's expensive seam-finding/blending entirely, since this
    exists only as a baseline for the stage-1-vs-stage-2 coverage safety net
    (2026-09-12, 사용자 승인): "이번엔(BACK) coverage가 줄지 않아 안전했다"는
    사실이 "항상 안전하다"를 보장하지 않으므로, 필터링 후 coverage_ratio가
    필터링 전보다 떨어지면 명시적으로 경고한다 -- 제외된 이미지가 실은 어떤
    벽면을 고유하게 커버하고 있었을 가능성 신호."""
    from src.stitching.mosaic import paste_max

    warped, canvas_size, _ = rectify_images(reconstruction, plane, images_dir)
    canvas_w, canvas_h = canvas_size
    if canvas_w <= 0 or canvas_h <= 0:
        return None
    observed = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    for w in warped.values():
        paste_max(observed, w.mask, w.corner)
    return float(np.count_nonzero(observed)) / float(observed.size)


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
                    try:
                        stage1_coverage_estimate = _estimate_coverage_ratio(
                            stage1_reconstruction, stage1_plane, colmap_images_dir,
                        )
                    except Exception as exc:
                        log_event(logger, "warning", "1단계 coverage 추정 실패", facade_id=facade_id, error=str(exc))

                    off_wall_ids = _detect_off_wall_images(stage1_reconstruction, stage1_plane)
                    if off_wall_ids:
                        log_event(
                            logger, "info", "벽면이 거의 안 보이는 이미지 자동 감지 -- 제외",
                            stage="OFF_WALL_DETECTED", facade_id=facade_id,
                            excluded_count=len(off_wall_ids), excluded_image_ids=sorted(off_wall_ids),
                        )
                        catalog = [m for m in catalog if m.image_id not in off_wall_ids]
                        by_id = {m.image_id: m for m in catalog}
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

    t0 = time.time()
    pairs = select_pairs(catalog, cfg)
    log_event(
        logger, "info", "pair graph built",
        stage="PAIR_GRAPH_BUILT", facade_id=facade_id, pair_count=len(pairs),
        elapsed_s=round(time.time() - t0, 2),
    )

    geometry_results: list[GeometryResult] = []
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

    preview_state = {"prev_path": None}

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
                    # 2026-09-18, 사용자 요청("건물 밖에서 나오는 크랙 표시 항목은 삭제"):
                    # crack/pipeline.py가 각 크랙의 캔버스 폴리곤을 이 마스크와 대조해
                    # 벽이 아닌 배경(산/지형)에 찍힌 오탐을 걸러낸다. rect_result.
                    # source_transforms는 이미 계산된 값을 재사용할 뿐이라 rectify_images/
                    # seam/blend를 다시 돌리지 않음 -- 오늘 겪은 렌더링 경로 마스킹 회귀와
                    # 무관한, 순수 후처리 필터용 산출물.
                    if rect_result.source_transforms is not None:
                        try:
                            wall_region_mask = compute_wall_region_canvas_mask(
                                reconstruction, plane, rect_result.source_transforms,
                                (rect_result.observed_mask.shape[1], rect_result.observed_mask.shape[0]),
                            )
                            imwrite_unicode(output_dir / f"{facade_id}_wall_region_mask_colmap.png", wall_region_mask)
                        except Exception as exc:
                            log_event(
                                logger, "warning", "wall_region_mask 계산 실패 -- 건너뜀 (크랙 배경 필터 비활성화됨)",
                                facade_id=facade_id, error=str(exc),
                            )
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
