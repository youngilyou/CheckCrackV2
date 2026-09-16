"""COLMAP fallback: trigger decision + real SfM execution (CLAUDE.local.md #12).

COLMAP is never required by default (#10, #43.12 says it's a precision path,
not baseline) — `should_run_colmap` decides *when* Phase 1's plain
homography-chain stitch is untrustworthy enough to warrant it.

Only two of #12's four trigger conditions are computed here today:
`global_drift_score` (cycle-consistency check, graph.py) and coverage gap.
Repeated-pattern failure and large-parallax detection need signals this
pipeline doesn't compute yet, so they are not silently assumed false —
`should_run_colmap` says so explicitly rather than pretending coverage.

`run_colmap` does real SfM via pycolmap (SIFT extraction -> exhaustive
matching -> incremental mapping + bundle adjustment) and returns actual
recovered camera poses/points, never a fabricated result. It stops at
producing the reconstruction (#12's "intrinsics, extrinsics, sparse
points, bundle-adjusted poses") — feeding those poses back into facade
rectification (#13) to replace the 2D homography chain is further work,
not done by this function.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.common.config import Config
from src.common.types import StitchQualityReport


def should_run_colmap(quality: StitchQualityReport, cfg: Config) -> tuple[bool, list[str]]:
    """Return (needs_colmap, reasons). Empty reasons means no trigger fired."""
    ccfg = cfg.colmap
    reasons: list[str] = []

    if not bool(ccfg.enabled):
        return False, reasons

    max_drift = float(ccfg.max_drift_px)
    if quality.global_drift_score_px is not None and quality.global_drift_score_px > max_drift:
        reasons.append(
            f"global_drift_score_px={quality.global_drift_score_px:.1f} > max_drift_px={max_drift}"
        )

    # A facade can pass the *mean* drift check while its single worst seam
    # is still badly misaligned (observed: mean=9.15px passed a 10px gate,
    # but max=26.9px produced a clearly visible wavy/bent line in the
    # mosaic) — mean alone hides exactly that kind of one-bad-seam defect.
    max_seam_drift = float(ccfg.max_seam_drift_px) if "max_seam_drift_px" in ccfg else max_drift * 2
    if quality.max_drift_score_px is not None and quality.max_drift_score_px > max_seam_drift:
        reasons.append(
            f"max_drift_score_px={quality.max_drift_score_px:.1f} > max_seam_drift_px={max_seam_drift}"
        )

    max_gap = float(ccfg.max_coverage_gap_ratio)
    if quality.coverage_ratio is not None:
        gap = 1.0 - quality.coverage_ratio
        if gap > max_gap:
            reasons.append(f"coverage_gap_ratio={gap:.2f} > max_coverage_gap_ratio={max_gap}")

    # #12 also lists repeated-pattern failure and large camera-distance/
    # parallax change as triggers. Neither is detected yet (would need
    # window-pattern-aware match auditing and per-pair baseline/parallax
    # estimates this pipeline doesn't compute), so they cannot fire here —
    # documented instead of silently treated as "not present".

    return len(reasons) > 0, reasons


@dataclass
class ColmapResult:
    facade_id: str
    num_images_requested: int
    num_images_registered: int
    registered_image_names: list = field(default_factory=list)
    num_points3d: int = 0
    mean_reprojection_error_px: float | None = None
    sparse_dir: str | None = None


def run_colmap(
    facade_id: str,
    images_dir: str | Path,
    image_filenames: list[str],
    workspace_dir: str | Path,
    logger=None,
    catalog: list | None = None,
    cfg=None,
    matcher=None,
) -> ColmapResult:
    """Run feature extraction -> matching -> incremental SfM for one facade's
    image set. `image_filenames` are names within `images_dir` (matches
    `pycolmap.extract_features`' `image_names` filter — this lets a facade's
    subset of a shared capture folder be reconstructed without copying
    files). Requires `pycolmap` (pip install pycolmap); raises ImportError if
    it's missing rather than faking a result.

    Matching stage: SIFT `pycolmap.match_exhaustive` by default. If `catalog`
    (the facade's `list[ImageMetadata]`, needed for pair selection + GPS),
    `cfg` (needed for `cfg.colmap.use_loftr_matching` + LoFTR/pair-selection
    settings) and `matcher` (a caller-owned `TimeoutLoFTRMatcher`) are all
    given AND `cfg.colmap.use_loftr_matching` is truthy, matching is done via
    `src/matching/loftr_colmap_bridge.py` instead — LoFTR is far more robust
    to the repetitive facade patterns (round vents/holes) that confuse SIFT
    into triangulating a spurious, visibly duplicated 3D point (root-caused
    2026-09-16, BACK facade). This is a general, config-driven switch: it
    applies identically to every facade's COLMAP call, never to one image ID
    or location specifically. Any caller that omits `catalog`/`cfg`/`matcher`
    (e.g. `extend_colmap_with_fixed_poses`'s own internal use, or exploratory
    scripts) gets the original SIFT behavior unchanged.

    `logger`, if given, gets phase-boundary events (extraction/matching/
    mapping start) plus a per-image event during incremental mapping via
    pycolmap's `next_image_callback` — that callback fires once per image
    registered *after* the initial seed pair (confirmed empirically: 11
    images registered fired it 9 times) and carries no image identity, just
    "another one landed", so the progress string is a plain counter, not a
    named image. Without a logger this runs exactly as before (silent,
    caller only sees the final ColmapResult) — CheckCrackViewer only shows
    live COLMAP progress for callers that pass one.
    """
    import pycolmap

    from src.common.logging import log_event

    workspace_dir = Path(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    database_path = workspace_dir / "database.db"
    if database_path.exists():
        database_path.unlink()  # pycolmap errors on a stale/partial db from a prior run
    sparse_dir = workspace_dir / "sparse"
    sparse_dir.mkdir(exist_ok=True)

    if logger:
        log_event(
            logger, "info", "CM 특징점 추출 시작",
            stage="COLMAP_EXTRACT", facade_id=facade_id, image_count=len(image_filenames),
        )
    # Defaults are num_threads=-1 (one SIFT extractor thread per CPU core) and
    # max_image_size=-1 (no downscale, i.e. full original resolution). For a
    # batch of full-size DJI photos (~20MP each) that combination was observed
    # to balloon past 33GB of RAM on a 33-image facade and crash with a native
    # access violation — this only feeds camera-pose recovery (#12/#13), not
    # the final crack-analysis mosaic, so it doesn't need full resolution the
    # way the stitched output does (#8).
    extraction_options = pycolmap.FeatureExtractionOptions(num_threads=4, max_image_size=3200)
    pycolmap.extract_features(
        database_path=database_path,
        image_path=images_dir,
        image_names=image_filenames,
        extraction_options=extraction_options,
    )

    use_loftr = (
        catalog is not None and cfg is not None and matcher is not None
        and "colmap" in cfg and "use_loftr_matching" in cfg.colmap and bool(cfg.colmap.use_loftr_matching)
    )
    if use_loftr:
        if logger:
            log_event(logger, "info", "CM 매칭 시작 (LoFTR)", stage="COLMAP_MATCH", facade_id=facade_id)
        from src.matching.loftr_colmap_bridge import match_database_with_loftr

        match_database_with_loftr(
            database_path, images_dir, image_filenames, catalog, cfg, matcher,
            workspace_dir=workspace_dir, logger=logger,
        )
    else:
        if logger:
            log_event(logger, "info", "CM 특징점 매칭 시작 (SIFT)", stage="COLMAP_MATCH", facade_id=facade_id)
        pycolmap.match_exhaustive(database_path=database_path)

    if logger:
        log_event(logger, "info", "CM SfM 재구성 시작", stage="COLMAP_MAPPING", facade_id=facade_id)

    registered = {"n": 0}

    def _on_next_image() -> None:
        registered["n"] += 1
        if logger:
            log_event(
                logger, "info", "CM 이미지 등록 중",
                stage="COLMAP_MAPPING_PROGRESS", facade_id=facade_id,
                progress=f"~{registered['n'] + 2}/{len(image_filenames)}",  # +2: the unreported initial seed pair
            )

    reconstructions = pycolmap.incremental_mapping(
        database_path=database_path,
        image_path=images_dir,
        output_path=sparse_dir,
        next_image_callback=_on_next_image,
    )

    if not reconstructions:
        return ColmapResult(
            facade_id=facade_id,
            num_images_requested=len(image_filenames),
            num_images_registered=0,
        )

    best = max(reconstructions.values(), key=lambda r: r.num_reg_images())
    best_dir = sparse_dir / "best"
    best_dir.mkdir(exist_ok=True)
    best.write(best_dir)  # persist chosen reconstruction for provenance (#39)

    errors = [p.error for p in best.points3D.values() if p.has_error]
    mean_error = sum(errors) / len(errors) if errors else None

    return ColmapResult(
        facade_id=facade_id,
        num_images_requested=len(image_filenames),
        num_images_registered=best.num_reg_images(),
        registered_image_names=sorted(img.name for img in best.images.values()),
        num_points3d=best.num_points3D(),
        mean_reprojection_error_px=mean_error,
        sparse_dir=str(best_dir),
    )


def extend_colmap_with_fixed_poses(
    facade_id: str,
    images_dir: str | Path,
    old_reconstruction,
    new_filenames: list[str],
    workspace_dir: str | Path,
    logger=None,
):
    """Register `new_filenames` into `old_reconstruction` WITHOUT letting
    bundle adjustment touch any already-known pose except the few genuinely
    close neighbors of the new images -- the general fix for the exclusion-
    rescue side effect confirmed real 2026-09-16 (see
    geometry/exclusion_safety.py's docstring and memory/
    colmap_global_exclusion_risk.md): re-running a full fresh COLMAP
    reconstruction with images added back (what this function replaces)
    moved a completely unrelated image (DJI_0118, ~0.6m) because
    incremental_mapping's periodic GLOBAL bundle adjustment re-optimizes
    every registered image's pose, not just the ones connected to whatever
    changed. This function never calls global BA -- only
    `IncrementalMapper.adjust_local_bundle`, which (per pycolmap's own
    docs) "only images connected to the reference image are optimized" --
    validated on BACK: of 68 old images, only 8-10 genuinely nearby ones
    moved (under 1m, mostly under 10cm) while the rest (including the
    unrelated DJI_0118) stayed at literally sub-millimeter precision.

    `old_reconstruction` MUST already be aligned to the target real-world
    frame (align_reconstruction_to_utm) -- this function doesn't touch scale/
    gauge at all, the new images simply inherit whatever gauge the old
    reconstruction's own points are already in via ordinary PnP registration
    against them. It is read for poses only; not mutated.

    Returns (ColmapResult, pycolmap.Reconstruction | None) -- the
    Reconstruction is the ACTUAL LIVE OBJECT this function built and
    registered images into (already in the caller's target gauge, ready for
    facade_plane_from_reconstruction/rectify_images). Confirmed real,
    2026-09-16: reloading a `.write()`'d copy of this specific kind of
    hand-assembled reconstruction from disk was observed to be pathologically
    slow and memory-hungry (tens of GB, root cause not identified -- unlike
    every OTHER reconstruction this project loads from disk all day, which
    is fast) even though the saved data itself checked out completely normal
    (no NaN/outlier points, sane counts). This function still writes a copy
    to `workspace_dir/sparse_extended` for provenance (#39), but the CALLER
    MUST use the returned in-memory Reconstruction directly and must NOT
    reload that saved copy -- until the reload issue is root-caused, treat
    that file as inspection-only, not a data source."""
    import pycolmap

    from src.common.logging import log_event

    workspace_dir = Path(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    database_path = workspace_dir / "database.db"
    if database_path.exists():
        database_path.unlink()

    old_filenames = sorted(img.name for img in old_reconstruction.images.values())
    all_filenames = sorted(set(old_filenames) | set(new_filenames))

    if logger:
        log_event(
            logger, "info", "CM 구조 후보 특징점 추출 시작 (포즈 고정 확장)",
            stage="COLMAP_EXTEND_EXTRACT", facade_id=facade_id,
            old_count=len(old_filenames), new_count=len(new_filenames),
        )
    extraction_options = pycolmap.FeatureExtractionOptions(num_threads=4, max_image_size=3200)
    pycolmap.extract_features(
        database_path=database_path, image_path=images_dir,
        image_names=all_filenames, extraction_options=extraction_options,
    )

    if logger:
        log_event(logger, "info", "CM 구조 후보 매칭 시작", stage="COLMAP_EXTEND_MATCH", facade_id=facade_id)
    pycolmap.match_exhaustive(database_path=database_path)

    db = pycolmap.Database.open(database_path)
    cache = pycolmap.DatabaseCache.create(db, pycolmap.DatabaseCacheOptions())

    # Assemble a reconstruction whose cameras/rigs/frames/images are all
    # consistently keyed to THIS database's own ID scheme from the start --
    # avoids transcribe_image_ids_to_database's frame/image ID mismatch
    # (confirmed real: it remaps image IDs but not frame IDs, producing
    # frame-ID collisions between old and new images sharing the same
    # numeric ID under two different numbering schemes).
    recon = pycolmap.Reconstruction()
    for cam in cache.cameras.values():
        recon.add_camera_with_trivial_rig(cam)
    for frame in cache.frames.values():
        recon.add_frame(frame)
    for image in cache.images.values():
        recon.add_image(image)

    old_poses_by_name = {img.name: img.cam_from_world() for img in old_reconstruction.images.values()}
    n_applied = 0
    for image in recon.images.values():
        pose = old_poses_by_name.get(image.name)
        if pose is None:
            continue
        recon.frame(image.frame_id).set_cam_from_world(image.camera_id, pose)
        recon.register_frame(image.frame_id)
        n_applied += 1
    if logger:
        log_event(
            logger, "info", "기존 포즈 고정 적용 완료", stage="COLMAP_EXTEND_POSES_FIXED",
            facade_id=facade_id, applied=n_applied, expected=len(old_filenames),
        )

    mapper_options = pycolmap.IncrementalMapperOptions()
    ba_options = pycolmap.BundleAdjustmentOptions()
    tri_options = pycolmap.IncrementalTriangulatorOptions()
    mapper = pycolmap.IncrementalMapper(cache)
    mapper.begin_reconstruction(recon)

    # Triangulate the old (already-posed, not-yet-triangulated-in-THIS-
    # database) images first -- register_next_image needs existing 3D
    # points to solve PnP against for the new images, and this fresh
    # reconstruction starts with zero points regardless of the old images'
    # poses being known.
    for image in recon.images.values():
        if image.name in old_poses_by_name:
            mapper.triangulate_image(tri_options, image.image_id)

    new_name_set = set(new_filenames)
    registered_new: list[str] = []
    for image in sorted(recon.images.values(), key=lambda im: im.name):
        if image.name not in new_name_set:
            continue
        ok = mapper.register_next_image(mapper_options, image.image_id)
        if logger:
            log_event(
                logger, "info", "구조 후보 등록 시도",
                stage="COLMAP_EXTEND_REGISTER", facade_id=facade_id,
                image_name=image.name, registered=ok,
            )
        if not ok:
            continue
        mapper.triangulate_image(tri_options, image.image_id)
        mapper.adjust_local_bundle(mapper_options, ba_options, tri_options, image.image_id, set())
        registered_new.append(image.name)

    mapper.end_reconstruction(discard=False)

    out_dir = workspace_dir / "sparse_extended"
    out_dir.mkdir(exist_ok=True)
    try:
        recon.write(out_dir)  # provenance only (#39) -- see docstring: do not reload this
    except Exception as exc:
        if logger:
            log_event(logger, "warning", "확장 재구성 저장 실패 (무시하고 계속)", facade_id=facade_id, error=str(exc))

    errors = [p.error for p in recon.points3D.values() if p.has_error]
    mean_error = sum(errors) / len(errors) if errors else None

    result = ColmapResult(
        facade_id=facade_id,
        num_images_requested=len(new_filenames),
        num_images_registered=len(old_filenames) + len(registered_new),
        registered_image_names=sorted(old_filenames + registered_new),
        num_points3d=recon.num_points3D(),
        mean_reprojection_error_px=mean_error,
        sparse_dir=str(out_dir),
    )
    if logger:
        log_event(
            logger, "info", "구조 후보 포즈 고정 확장 완료", stage="COLMAP_EXTEND_DONE",
            facade_id=facade_id, requested=len(new_filenames), registered=len(registered_new),
            failed=sorted(new_name_set - set(registered_new)),
        )
    return result, recon
