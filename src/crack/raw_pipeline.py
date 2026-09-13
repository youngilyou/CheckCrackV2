"""Raw-photo-first crack detection (2026-09-12 재설계, CLAUDE.local.md #21-27의
"완성된 모자이크를 타일링해서 검사"에서 "원본 개별 사진에서 먼저 검사, 결과를
캔버스 좌표로 병합"으로 전환 -- 상세 배경/결정 근거는 CLAUDE.local.md의 2026-09-12
세션 기록 참고).

기존(`src/crack/pipeline.py::detect_cracks`)은 이미 완성된 스티칭 모자이크
(`_analysis.tif`/`_analysis_colmap.tif`) 하나를 타일링해서 검사했다. 이 방식은
CLAUDE.local.md 원래 스펙(#21-23)과는 일치하지만, 두 가지 실제 문제가 있었다:
(1) 자동 이미지 필터링(_detect_off_wall_images)으로 제외된 원본의 크랙 정보가
모자이크 자체에 없어서 완전히 유실될 수 있음, (2) 모자이크는 각 원본을 평면에
투영+블렌딩한 결과라 seam/blend 아티팩트(이번 세션에서 겪은 disagreement-island,
재발코니 왜곡 등)가 검출을 오염시킬 수 있음.

새 설계: 원본 사진 각각(다운사이즈 절대 금지, 모자이크와 동일한 타일링 설정
재사용)에서 독립적으로 검출 -> 2단계 COLMAP이 만든 최종 호모그래피로 각 크랙의
"위치"만 캔버스 좌표로 변환(병합/매칭/최종 폴리곤 표시용) -> 여러 원본에 겹쳐
찍힌 같은 크랙은 캔버스 좌표에서 IoU로 병합(merge_tiles.group_overlapping_polygons
재사용) -> "측정값"(길이/폭/면적)은 캔버스가 아니라 각 원본 자신의 픽셀 해상도 +
그 크랙 위치의 로컬 스케일(호모그래피 야코비안 기반, 이 세션에서 다른 진단 목적
으로 썼던 것과 같은 수식)로 계산한 뒤 평균 -- 캔버스의 전역 px_per_m(Phase 1
기준 100px/m=1cm/px)은 드론 원본의 실제 해상도(보통 mm 단위 GSD)보다 거칠어서,
캔버스에서 측정하면 원본의 정밀도를 잃기 때문."""

from __future__ import annotations

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

from src.common.config import Config
from src.common.types import Crack, SourceObservation
from src.crack.detector import CrackDetection, CrackDetector
from src.crack.measurement import ScaleInfo, to_mm, to_mm2
from src.crack.merge_tiles import CrackPolygon, group_overlapping_polygons, match_crack_ids, merge_detections
from src.crack.skeleton import measure_polygon
from src.crack.tiler import tile_mosaic


def detect_cracks_in_image(
    facade_id: str,
    image_id: str,
    raw_image: np.ndarray,
    cfg: Config,
    detector: CrackDetector,
) -> list[CrackPolygon]:
    """One raw source photo -> tile (다운사이즈 없음, 모자이크와 동일한
    tiling.tile_width/height/overlap_px 설정 재사용) -> YOLO 추론 -> 이 사진
    내부의 타일 겹침만 병합(merge_detections 그대로 재사용). 반환되는
    CrackPolygon.polygon_px는 이 사진 자신의 원본 픽셀 좌표계(캔버스 아님).

    observed_mask는 raw photo 전체가 실제 카메라 픽셀이므로(모자이크처럼
    미관측 구멍이 없음) 전부 관측됨(255)으로 채워서 tile_mosaic의 기존
    skip_if_observed_ratio_below 게이트를 그대로 통과시킨다 -- 별도 분기 불필요."""
    h, w = raw_image.shape[:2]
    observed_mask = np.full((h, w), 255, dtype=np.uint8)
    tiles = tile_mosaic(facade_id, raw_image, observed_mask, cfg)
    if not tiles:
        return []
    tiles_by_id = {t.tile_id: t for t in tiles}

    detections: list[CrackDetection] = []
    for tile in tiles:
        detections.extend(detector.infer_tile(tile))

    return merge_detections(tiles_by_id, detections, facade_id)


def local_scale_info(H: np.ndarray, canvas_px_per_m: float, point_raw_xy: tuple[float, float]) -> ScaleInfo:
    """ScaleInfo carrying a LOCAL px_per_m (valid only near `point_raw_xy` in
    this specific raw image), instead of the canvas's single global one --
    plug straight into the existing to_mm/to_mm2 (unchanged) for measurement
    computed on a RAW-image polygon.

    Derivation: for a planar homography H (raw-pixel -> canvas-pixel, w =
    H[2,0]*u + H[2,1]*v + H[2,2]), the local AREA magnification factor at
    (u,v) is area_scale = |det(H) / w^3| (canvas-px^2 per raw-px^2) -- same
    Jacobian-determinant identity used earlier this session to test (and
    reject, for that unrelated purpose) local-warp-scale as an off-plane-
    content signal; here it's exactly the right tool, since we WANT the
    local metric scale, not a uniformity check. Linear magnification is
    sqrt(area_scale) (canvas-px per raw-px). The canvas itself has a fixed,
    real-metric px_per_m (from COLMAP+GPS-UTM alignment), so:
        mm_per_raw_px = sqrt(area_scale) * 1000 / canvas_px_per_m
    Expressing that as an equivalent "local px_per_m" (so it plugs into
    to_mm's existing `1000 / px_per_m` formula unmodified):
        local_px_per_m = canvas_px_per_m / sqrt(area_scale)
    Returns calibrated=False (px_per_m=None) if H is degenerate at this
    point (w~0, a genuine edge case at the extreme horizon of the plane
    projection) rather than dividing by ~0."""
    u, v = point_raw_xy
    w = H[2, 0] * u + H[2, 1] * v + H[2, 2]
    if abs(w) < 1e-9:
        return ScaleInfo(px_per_m=None, calibrated=False)
    det_h = float(np.linalg.det(H))
    area_scale = abs(det_h / (w**3))
    if area_scale <= 1e-12:
        return ScaleInfo(px_per_m=None, calibrated=False)
    linear_scale = area_scale**0.5
    return ScaleInfo(
        px_per_m=canvas_px_per_m / linear_scale,
        calibrated=True,
        reference_object_type="colmap_local_homography_jacobian",
        reference_length_mm=None,
    )


def _polygon_to_canvas(polygon_raw_px: np.ndarray, H: np.ndarray) -> np.ndarray:
    """Raw-image pixel polygon -> canvas pixel polygon via this image's own
    homography (the SAME direction SourceTransform.H is defined in -- see
    rectification.py's _camera_to_facade_homography -- unlike
    crack/pipeline.py's _compute_source_observations, which inverts it the
    other way)."""
    import cv2

    pts = polygon_raw_px.astype(np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(pts, H).reshape(-1, 2)


def _to_shapely(poly_px: np.ndarray) -> Polygon | None:
    if len(poly_px) < 3:
        return None
    poly = Polygon(poly_px)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly if (not poly.is_empty and poly.is_valid and poly.geom_type == "Polygon") else None


def cv2_contour_area(polygon_px: np.ndarray) -> float:
    """polygon_px area in whatever pixel space it's given (raw or canvas),
    via the same shoelace-formula-backed OpenCV helper the rest of this
    codebase already uses for polygon area."""
    import cv2

    return float(cv2.contourArea(polygon_px.astype(np.float32)))


def detect_cracks_from_raw_images(
    facade_id: str,
    building_id: str,
    image_paths: dict[str, str],
    source_transforms: dict[str, dict],
    canvas_px_per_m: float,
    cfg: Config,
    model_path: str,
    device: str | None = None,
    previous_cracks: list[dict] | None = None,
    cross_image_iou_threshold: float | None = None,
) -> list[Crack]:
    """Full raw-photo-first pipeline: `image_paths`(image_id -> file path,
    already filtered to the surviving/good image set -- see
    pipeline/runner.py's _detect_off_wall_images) each independently
    tiled+detected -> transformed into canvas coordinates via
    `source_transforms`(image_id -> {"H", "width", "height"}, the stage-2
    COLMAP homographies -- same JSON shape crack/pipeline.py's
    _compute_source_observations already reads) -> cross-image duplicate
    merge (같은 crack_id_match_iou_threshold... 아님, 별도 파라미터: 사용자
    확정 "merge_tiles.py와 동일 임계치 사용" -- 기본값은 merge_detections의
    기본 iou_threshold=0.2와 통일) -> length/width/area는 각 원본 자신의
    로컬 스케일로 측정 후 평균, source_observations는 이 병합 결과 자체로
    직접 채움(더 이상 역방향 조회 아님)."""
    from src.common.logging import get_logger, log_event

    logger = get_logger("pipeline", log_dir="logs")
    iou_threshold = cross_image_iou_threshold if cross_image_iou_threshold is not None else 0.2

    detector = CrackDetector(model_path, cfg, device=device)

    # Step 1: 원본 사진 각각 독립 검출 (raw pixel space, 이미지 내부 타일 겹침만 병합됨).
    per_image_polys: dict[str, list[CrackPolygon]] = {}
    for image_id, path in image_paths.items():
        transform = source_transforms.get(image_id)
        if transform is None:
            continue  # 이 이미지는 2단계 호모그래피가 없음 -- 캔버스에 위치시킬 수 없어 스킵
        import cv2

        from src.common.imageio import imread_unicode

        raw_image = imread_unicode(path, cv2.IMREAD_COLOR)
        if raw_image is None:
            log_event(logger, "warning", "원본 이미지 로드 실패", image_id=image_id)
            continue
        polys = detect_cracks_in_image(facade_id, image_id, raw_image, cfg, detector)
        if polys:
            per_image_polys[image_id] = polys
        log_event(
            logger, "info", "원본 사진 크랙 검출 완료",
            stage="RAW_CRACK_DETECTED", facade_id=facade_id, image_id=image_id,
            crack_count=len(polys),
        )

    # Step 2: 각 원본의 크랙 폴리곤을 캔버스 좌표로 변환 (위치/병합 매칭용).
    items: list[tuple[Polygon, float, str]] = []  # (canvas polygon, confidence, image_id)
    item_meta: list[tuple[str, np.ndarray]] = []  # (image_id, polygon_raw_px) 같은 인덱스로 대응
    for image_id, polys in per_image_polys.items():
        H = np.asarray(source_transforms[image_id]["H"], dtype=np.float64)
        for poly in polys:
            canvas_poly_px = _polygon_to_canvas(poly.polygon_px, H)
            shapely_poly = _to_shapely(canvas_poly_px)
            if shapely_poly is None:
                continue
            items.append((shapely_poly, poly.confidence, image_id))
            item_meta.append((image_id, poly.polygon_px))

    if not items:
        return []

    # Step 3: 캔버스 좌표에서 서로 다른 원본 간 같은 크랙 병합 (merge_tiles.py와 동일 알고리즘/임계치).
    groups = group_overlapping_polygons(items, iou_threshold)

    merged_for_id_matching: list[CrackPolygon] = []
    group_members: list[list[int]] = []
    for k, idxs in enumerate(groups):
        polys = [items[i][0] for i in idxs]
        confs = [items[i][1] for i in idxs]
        merged_geom = unary_union(polys)
        if merged_geom.geom_type == "MultiPolygon":
            merged_geom = max(merged_geom.geoms, key=lambda g: g.area)
        merged_for_id_matching.append(
            CrackPolygon(
                crack_id=f"{facade_id}_C{k:06d}",
                facade_id=facade_id,
                polygon_px=np.array(merged_geom.exterior.coords),
                confidence=max(confs),
                area_px=float(merged_geom.area),
                source_tile_ids=sorted({items[i][2] for i in idxs}),  # 여기서는 tile_id 자리에 image_id를 재사용
            )
        )
        group_members.append(idxs)

    # 크랙 ID 안정성 매칭 -- 기존 로직 그대로 재사용 (사용자 확정: "기존 유지").
    id_match_threshold = float(getattr(cfg.measurement, "crack_id_match_iou_threshold", 0.3))
    match_crack_ids(merged_for_id_matching, previous_cracks, iou_threshold=id_match_threshold)

    width_threshold_mm = float(cfg.measurement.crack_width_threshold_mm)
    source_image_ids = sorted(image_paths.keys())

    cracks: list[Crack] = []
    for merged_poly, idxs in zip(merged_for_id_matching, group_members):
        # 캔버스 측정 -- 위치/표시(길이_px/폭_px/스켈레톤 오버레이)용, 정밀 mm 측정용 아님.
        canvas_measurement = measure_polygon(merged_poly.polygon_px)
        if canvas_measurement is None:
            continue

        # mm 측정 -- 각 원본 자신의 raw 픽셀 + 그 지점의 로컬 스케일로, 그룹 내 평균.
        length_mm_values: list[float] = []
        width_mm_values: list[float] = []
        area_mm2_values: list[float] = []
        source_observations: list[SourceObservation] = []
        for i in idxs:
            image_id, polygon_raw_px = item_meta[i]
            raw_measurement = measure_polygon(polygon_raw_px)
            if raw_measurement is None:
                continue
            H = np.asarray(source_transforms[image_id]["H"], dtype=np.float64)
            centroid_raw = polygon_raw_px.mean(axis=0)
            scale = local_scale_info(H, canvas_px_per_m, (float(centroid_raw[0]), float(centroid_raw[1])))
            l_mm = to_mm(raw_measurement.length_px, scale)
            w_mm = to_mm(raw_measurement.max_width_px, scale)
            a_mm2 = to_mm2(cv2_contour_area(polygon_raw_px), scale)
            if l_mm is not None:
                length_mm_values.append(l_mm)
            if w_mm is not None:
                width_mm_values.append(w_mm)
            if a_mm2 is not None:
                area_mm2_values.append(a_mm2)

            width, height = int(source_transforms[image_id]["width"]), int(source_transforms[image_id]["height"])
            x0, y0 = polygon_raw_px.min(axis=0)
            x1, y1 = polygon_raw_px.max(axis=0)
            source_observations.append(
                SourceObservation(
                    image_id=image_id,
                    bbox_px_in_source=(
                        round(float(max(0, x0)), 1), round(float(max(0, y0)), 1),
                        round(float(min(width, x1)), 1), round(float(min(height, y1)), 1),
                    ),
                    polygon_px_in_source=polygon_raw_px.round(1),
                    owned_pixel_count=int(round(cv2_contour_area(polygon_raw_px))),
                )
            )
        source_observations.sort(key=lambda o: o.owned_pixel_count, reverse=True)

        length_mm = sum(length_mm_values) / len(length_mm_values) if length_mm_values else None
        max_width_mm = sum(width_mm_values) / len(width_mm_values) if width_mm_values else None
        area_mm2 = sum(area_mm2_values) / len(area_mm2_values) if area_mm2_values else None

        severity = None
        if max_width_mm is not None:
            severity = "정밀점검대상" if max_width_mm >= width_threshold_mm else "경미"

        x0, y0 = merged_poly.polygon_px.min(axis=0)
        x1, y1 = merged_poly.polygon_px.max(axis=0)
        cracks.append(
            Crack(
                crack_id=merged_poly.crack_id,
                building_id=building_id,
                facade_id=facade_id,
                bbox_px=(float(x0), float(y0), float(x1), float(y1)),
                polygon_px=merged_poly.polygon_px,
                skeleton_px=canvas_measurement.skeleton_px,
                length_px=canvas_measurement.length_px,
                max_width_px=canvas_measurement.max_width_px,
                mean_width_px=canvas_measurement.mean_width_px,
                area_px=merged_poly.area_px,
                confidence=merged_poly.confidence,
                observation_state="OBSERVED",
                length_mm=length_mm,
                max_width_mm=max_width_mm,
                area_mm2=area_mm2,
                severity=severity,
                source_tile_ids=[],  # 원본 기준 검출엔 모자이크 타일 개념이 없음
                source_image_ids=source_image_ids,
                source_observations=source_observations,
            )
        )
    return cracks
