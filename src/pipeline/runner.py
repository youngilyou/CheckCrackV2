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
    estimate_utm_epsg,
    facade_plane_from_reconstruction,
    facade_plane_from_segment,
    rectify_and_blend,
)
from src.matching.loftr_matcher import MatchTimeoutError, TimeoutLoFTRMatcher
from src.matching.pair_selector import select_pairs
from src.sfm.colmap_runner import run_colmap
from src.stitching.mosaic import stitch_facade


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
    output dir, or None if there weren't enough passing pairs to stitch anything."""
    output_dir = output_dir_override if output_dir_override is not None else output_root / facade_id / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    by_id = {m.image_id: m for m in catalog}

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
        log_event(
            logger, "warning", "facade needs COLMAP fallback, stitch is NEEDS_MANUAL_REVIEW",
            stage="NEEDS_MANUAL_REVIEW", facade_id=facade_id,
            reasons=result.quality.colmap_fallback_reasons,
        )
        if run_colmap_fallback:
            t_colmap = time.time()
            source_dirs = {str(Path(by_id[iid].file_path).parent) for iid in images.keys()}
            if len(source_dirs) != 1:
                log_event(
                    logger, "warning", "facade images span multiple source dirs, skipping COLMAP",
                    facade_id=facade_id, source_dirs=list(source_dirs),
                )
            else:
                colmap_images_dir = next(iter(source_dirs))
                colmap_filenames = [Path(by_id[iid].file_path).name for iid in images.keys()]
                try:
                    colmap_result = run_colmap(
                        facade_id, colmap_images_dir, colmap_filenames,
                        workspace_dir=output_dir.parent / "colmap", logger=logger,
                    )
                    log_event(
                        logger, "info", "COLMAP fallback complete",
                        stage="COLMAP_FALLBACK", facade_id=facade_id,
                        elapsed_s=round(time.time() - t_colmap, 2),
                        num_images_requested=colmap_result.num_images_requested,
                        num_images_registered=colmap_result.num_images_registered,
                        num_points3d=colmap_result.num_points3d,
                        mean_reprojection_error_px=colmap_result.mean_reprojection_error_px,
                    )
                    atomic_write_json(output_dir / f"{facade_id}_colmap_report.json", asdict(colmap_result))

                    # #13: use the recovered poses to rectify onto the real facade
                    # plane instead of the drifting homography chain — needs enough
                    # images registered to trust the plane fit, and *some* way to
                    # get the reconstruction into real, gravity-aligned UTM+altitude
                    # meters (align_reconstruction_to_utm's own requirement). Phase 2/3
                    # (run_building_poc) always has an operator-supplied utm_epsg
                    # alongside the footprint; Phase 1 (run_facade_poc) has neither, so
                    # it derives a UTM zone from the images' own GPS instead — the
                    # rectification math itself doesn't care which source utm_epsg
                    # came from, only that it exists.
                    if colmap_result.sparse_dir and colmap_result.num_images_registered >= 4:
                        try:
                            import pycolmap

                            effective_utm_epsg = utm_epsg if utm_epsg is not None else estimate_utm_epsg(catalog)
                            if effective_utm_epsg is None:
                                log_event(
                                    logger, "warning", "no GPS on any image, cannot align COLMAP reconstruction for rectification",
                                    facade_id=facade_id,
                                )
                            else:
                                reconstruction = pycolmap.Reconstruction(colmap_result.sparse_dir)
                                aligned = align_reconstruction_to_utm(reconstruction, by_id, effective_utm_epsg)
                                if not aligned:
                                    log_event(
                                        logger, "warning", "COLMAP reconstruction has too little GPS coverage to align to UTM, skipping rectification",
                                        facade_id=facade_id,
                                    )
                                else:
                                    # segment is not None -> Phase 2/3, a real footprint
                                    # edge defines the plane (#4.2's priority tiers).
                                    # segment is None -> Phase 1, no footprint exists at
                                    # all, so the plane is fitted straight from COLMAP's
                                    # own triangulated points instead (exactly the fix
                                    # for a facade run whose image set spans a rooftop
                                    # nadir pass plus a few oblique facade-edge shots,
                                    # which tore the old homography-chain mosaic across
                                    # the roof/wall boundary).
                                    if segment is not None:
                                        reference_altitudes = [
                                            by_id[Path(img.name).stem].gps.altitude_m
                                            for img in reconstruction.images.values()
                                            if Path(img.name).stem in by_id and by_id[Path(img.name).stem].gps.altitude_m is not None
                                        ]
                                        plane = facade_plane_from_segment(segment, reference_altitudes)
                                    else:
                                        plane = facade_plane_from_reconstruction(reconstruction)

                                    t_rect = time.time()
                                    rect_result = rectify_and_blend(
                                        facade_id, reconstruction, plane, colmap_images_dir, cfg,
                                        colmap_mean_reprojection_error_px=colmap_result.mean_reprojection_error_px,
                                    )
                                    if rect_result.analysis_image is not None:
                                        imwrite_unicode(output_dir / f"{facade_id}_analysis_colmap.tif", rect_result.analysis_image)
                                    if rect_result.visual_image is not None:
                                        imwrite_unicode(output_dir / f"{facade_id}_visual_colmap.tif", rect_result.visual_image)
                                    imwrite_unicode(output_dir / f"{facade_id}_observed_mask_colmap.tif", rect_result.observed_mask)
                                    atomic_write_json(output_dir / f"{facade_id}_quality_report_colmap.json", asdict(rect_result.quality))
                                    _write_source_transform_artifacts(output_dir, facade_id, "_colmap", rect_result)
                                    log_event(
                                        logger, "info", "COLMAP-rectified mosaic complete",
                                        stage="RECTIFIED_COLMAP", facade_id=facade_id,
                                        elapsed_s=round(time.time() - t_rect, 2),
                                        coverage_ratio=rect_result.quality.coverage_ratio,
                                        image_count=rect_result.quality.image_count,
                                    )
                        except Exception as exc:
                            log_event(
                                logger, "warning", "COLMAP-pose rectification failed",
                                facade_id=facade_id, error=str(exc),
                            )
                except ImportError:
                    log_event(
                        logger, "warning", "pycolmap not installed, cannot run COLMAP fallback",
                        facade_id=facade_id,
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
