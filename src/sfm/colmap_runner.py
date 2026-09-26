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
    matcher_backend: str | None = None,
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
    to the repetitive facade patterns (round vents/holes, balcony panels)
    that confuse SIFT into triangulating a spurious, visibly duplicated 3D
    point (root-caused 2026-09-16/17, re-confirmed 2026-09-23/24 on this same
    building's FRONT facade: 422/422 images registered vs SuperPoint+LightGlue's
    partial registration, lower reprojection error, and no commercial-license
    conflict either — SuperPoint/SuperGlue carry Magic Leap's non-commercial
    research license, LoFTR is Apache 2.0). This is a general, config-driven
    switch: it applies identically to every facade's COLMAP call, never to one
    image ID or location specifically. Any caller that omits
    `catalog`/`cfg`/`matcher` (e.g. `extend_colmap_with_fixed_poses`'s own
    internal use, or exploratory scripts) gets the original SIFT behavior
    unchanged.

    `logger`, if given, gets phase-boundary events (extraction/matching/
    mapping start) plus a per-image event during incremental mapping via
    pycolmap's `next_image_callback` — that callback fires once per image
    registered *after* the initial seed pair (confirmed empirically: 11
    images registered fired it 9 times) and carries no image identity, just
    "another one landed", so the progress string is a plain counter, not a
    named image. Without a logger this runs exactly as before (silent,
    caller only sees the final ColmapResult) — CheckCrackViewer only shows
    live COLMAP progress for callers that pass one.

    `matcher_backend` (2026-09-24, explicit user request to make the matcher
    a user-facing choice, license concerns disregarded for this toggle):
    `"sift"` / `"loftr"` / `"hloc"`, or `None` to fall back to
    `cfg.colmap.matcher_backend` if set, else the legacy
    `cfg.colmap.use_loftr_matching` boolean, else `"sift"`. `"hloc"` runs
    SuperPoint+LightGlue via `src/matching/hloc_bridge.py` (a separate-env
    subprocess — see that module's docstring) and does not need `matcher`
    (no persistent GPU worker to reuse, unlike LoFTR).
    """
    import pycolmap

    from src.common.logging import log_event

    resolved_backend = (matcher_backend or "").lower() or None
    if resolved_backend is None:
        if catalog is not None and cfg is not None and "colmap" in cfg and "matcher_backend" in cfg.colmap:
            resolved_backend = str(cfg.colmap.matcher_backend).lower()
        elif (
            catalog is not None and cfg is not None and matcher is not None
            and "colmap" in cfg and "use_loftr_matching" in cfg.colmap and bool(cfg.colmap.use_loftr_matching)
        ):
            resolved_backend = "loftr"
        else:
            resolved_backend = "sift"

    if resolved_backend == "hloc" and (catalog is None or cfg is None):
        if logger:
            log_event(
                logger, "warning", "matcher_backend=hloc requested but catalog/cfg missing -- SIFT로 대체",
                stage="COLMAP_MATCH", facade_id=facade_id,
            )
        resolved_backend = "sift"
    if resolved_backend == "loftr" and (catalog is None or cfg is None or matcher is None):
        if logger:
            log_event(
                logger, "warning", "matcher_backend=loftr requested but catalog/cfg/matcher missing -- SIFT로 대체",
                stage="COLMAP_MATCH", facade_id=facade_id,
            )
        resolved_backend = "sift"

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

    # Fixed, known intrinsics for a capture whose GPS positions lie on one line (2026-09-26, LEFT: the
    # drone climbs/descends straight along a flat wall). COLMAP's default gives every image its OWN
    # camera and refines focal length + distortion per image; on such a nearly degenerate capture focal
    # length and depth trade off, so the focals drift apart (measured on LEFT: 581-18642 px for ONE
    # physical camera; FRONT stays at 3730-3835) and the whole reconstruction bends (camera path 12 x
    # 10 m wide instead of the true ~1.6 m), which breaks the GPS alignment. A shared camera alone was
    # tried and is NOT enough (23 of 48 photos registered), so the calibrated intrinsics of the same camera
    # model (taken from a well-conditioned capture, config `colmap.known_intrinsics`) are shared AND fixed.
    # `colmap.single_camera`: "auto" (default: only for a collinear-GPS capture, so FRONT/BACK are
    # unchanged), true, or false. Without known intrinsics for this camera the default behavior is kept.
    fixed_intrinsics = None
    mode = "auto"
    if cfg is not None and "colmap" in cfg and "single_camera" in cfg.colmap:
        mode = str(cfg.colmap.single_camera).strip().lower()
    if mode != "false" and catalog is not None and cfg is not None and "colmap" in cfg and "known_intrinsics" in cfg.colmap:
        names = set(image_filenames)
        subset = [m for m in catalog if Path(m.file_path).name in names]
        wants_fixed = mode in ("true", "1", "yes")
        if mode == "auto" and subset:
            from src.geometry.rectification import gps_configuration_is_degenerate

            wants_fixed = gps_configuration_is_degenerate(subset)
        if wants_fixed and subset:
            known = cfg.colmap.known_intrinsics.to_dict().get(str(subset[0].camera_model))
            sizes = {(m.width, m.height) for m in subset}
            if known and sizes == {(int(known["width"]), int(known["height"]))}:
                fixed_intrinsics = known
    if fixed_intrinsics is not None and logger:
        log_event(
            logger, "info", "촬영 위치(GPS)가 한 줄 -- 카메라 하나를 공유하고 알려진 내부 파라미터로 고정해 재구성",
            stage="COLMAP_SINGLE_CAMERA", facade_id=facade_id,
            focal_px=float(fixed_intrinsics["focal_px"]), k1=float(fixed_intrinsics.get("k1", 0.0)),
        )
    pycolmap.extract_features(
        database_path=database_path,
        image_path=images_dir,
        image_names=image_filenames,
        camera_mode=pycolmap.CameraMode.SINGLE if fixed_intrinsics is not None else pycolmap.CameraMode.AUTO,
        extraction_options=extraction_options,
    )
    if fixed_intrinsics is not None:
        import numpy as np

        db = pycolmap.Database.open(str(database_path))
        try:
            for cam in db.read_all_cameras():
                cam.params = np.array([
                    float(fixed_intrinsics["focal_px"]), cam.params[1], cam.params[2],
                    float(fixed_intrinsics.get("k1", 0.0)),
                ])
                cam.has_prior_focal_length = True
                db.update_camera(cam)
        finally:
            db.close()

    if resolved_backend == "hloc":
        if logger:
            log_event(logger, "info", "CM 매칭 시작 (hloc/SuperPoint)", stage="COLMAP_MATCH", facade_id=facade_id)
        from src.matching.hloc_bridge import match_database_with_hloc

        match_database_with_hloc(
            database_path, images_dir, image_filenames, catalog, cfg,
            workspace_dir=workspace_dir, logger=logger,
        )
    elif resolved_backend == "loftr":
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

    mapping_kwargs = {}
    if fixed_intrinsics is not None:
        options = pycolmap.IncrementalPipelineOptions()
        options.ba_refine_focal_length = False
        options.ba_refine_extra_params = False
        mapping_kwargs["options"] = options
    reconstructions = pycolmap.incremental_mapping(
        database_path=database_path,
        image_path=images_dir,
        output_path=sparse_dir,
        next_image_callback=_on_next_image,
        **mapping_kwargs,
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
