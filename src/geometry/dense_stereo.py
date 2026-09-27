"""Dense-stereo + flat-mosaic hybrid facade rendering (2026-09-23/24 gsplat3d
PoC, promoted to production).

Root problem this solves: `rectification.rectify_and_blend`'s flat single-
homography-per-photo warp is geometrically exact only where the wall is truly
flat. A balcony/recess a real drone standoff distance away parallax-shifts
between photos taken from different positions, tearing the flat mosaic there
(confirmed real, CLAUDE.local.md's own height_field.py history). COLMAP dense
stereo (patch_match_stereo, classical multi-view stereo -- no neural training,
no generative fill, CLAUDE.local.md #6/#19 compliant) recovers each surface
point's REAL 3D position, so photos from different positions agree on
protruding/recessed structure too -- but it fails outright (no data, not wrong
data) on texture-poor flat wall panels, where multi-view photo-consistency has
no signal to match on regardless of how long it runs (confirmed real,
2026-09-23/24: parameter tuning -- min_confidence, max_image_side up to
1600 -- changed reconstruction quality negligibly and did not change the
hole rate).

Fix: render dense stereo's own point cloud for its own strength (real depth,
no parallax tear), then fill ONLY the pixels it has no data for from the
already-computed flat mosaic (rect_result) -- exactly where the flat
assumption is least wrong anyway, since a flat, texture-poor panel has ~zero
real depth variation to get wrong in the first place. Two known, accepted
remaining limitations (see gsplat3d session memory) that further work here did
NOT resolve: (1) dense-stereo pixels look less photographically sharp than a
direct photo warp (stereo_fusion's cross-view color averaging), (2) off-plane
background (sky/terrain past the roofline) still bleeds in on the flat-mosaic
side, same as the pre-existing flat-mosaic-only pipeline.

Coordinate contract (REVISED 2026-09-25, see build_hybrid_mosaic's own
docstring for the confirmed real bug this replaces): `run_dense_stereo` must
be called on a NATIVE-scale reconstruction -- i.e. loaded fresh from the same
`sparse_dir` BEFORE `align_reconstruction_to_utm`'s in-place transform, never
the already-UTM-aligned object `plane` was fitted from. `build_hybrid_mosaic`
then applies the SAME `sim3d` (`align_reconstruction_to_utm`'s return value)
to the raw `fused.ply` points itself, exactly matching the gsplat3d
exploration scripts (`gsplat3d/finish_loftr_pipeline.py`) this was ported
from -- align-BEFORE-dense-stereo was tried first (a simpler design, no
separate similarity-transform bookkeeping needed) and confirmed broken: on
the FRONT facade, 100% of 228,224 fused points landed 50-380m from the facade
plane despite the same reconstruction's own sparse points3D correctly
clustering near it. Root cause not fully isolated (camera poses and
intrinsics both verified identical before/after undistortion), but reverting
to the align-after ordering that gsplat3d's own working scripts always used
fixed it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from src.stitching.mosaic import MosaicResult
from src.stitching.warp import SourceTransform

WALL_BAND_M = 3.0  # points farther than this from the plane are foreground/background clutter, not the wall
SPLAT_RADIUS_PX = 2  # closes sub-pixel gaps between dense points; spreads only EXISTING observed color
MIN_INPAINT_HOLE_AREA_PX = 150  # only fill small gaps this size or smaller; real windows/shadows are larger and untouched
DENSE_MAX_IMAGE_SIZE = 2640  # halved from typical 5280px DJI stills -- GPU-memory precaution, confirmed real
                              # (2026-09-23): 16GB VRAM + 21MP source images is a combination COLMAP's own
                              # patch_match_stereo commonly OOMs on; canvas render resolution (100px/m) is
                              # already coarser than either resolution's own GSD, so this costs no sharpness.
EDGE_MARGIN_M = 2.0  # excludes dense points within this many meters of the near-wall point set's OWN
                      # u-extent (not the canvas edge) -- a real building corner in view can only occur
                      # there; see _render_dense_canvas's own comment for the confirmed real defect this
                      # fixes (2026-09-25, FRONT facade V006). Empirically sufficient: the corner artifact
                      # measured there spanned u in [4.67, 5.60] relative to a u_min of 3.88 (~1.7m deep).


@dataclass
class DenseStereoResult:
    num_points_total: int
    num_points_near_wall: int
    fused_ply_path: str


def _count_depth_maps(dense_dir: Path) -> int:
    depth_dir = dense_dir / "stereo" / "depth_maps"
    if not depth_dir.exists():
        return 0
    try:
        return sum(1 for _ in depth_dir.iterdir())
    except OSError:
        return 0  # directory mid-write (file being created/renamed) -- just skip this poll tick


def run_dense_stereo(
    reconstruction: "pycolmap.Reconstruction",
    images_dir: str | Path,
    workspace_dir: str | Path,
    logger=None,
    facade_id: str = "",
    progress_interval_s: float = 60.0,
) -> DenseStereoResult | None:
    """Runs image_undistorter -> patch_match_stereo -> stereo_fusion via
    pycolmap directly (no native COLMAP.exe binary -- confirmed present in the
    pycolmap version this project already depends on, 2026-09-23: pycolmap 4.3.0
    exposes `undistort_images`/`patch_match_stereo`/`stereo_fusion` with option
    objects matching every setting the original gsplat3d shell-script version
    used). `reconstruction` must be NATIVE-scale (NOT UTM-aligned) -- see
    module docstring for the confirmed-broken alternative this replaced.

    Returns None (never raises past logging its own exception to the caller's
    log, same "no fabricated result" convention as colmap_runner.run_colmap)
    if CUDA/patch_match_stereo isn't available or the stage fails outright --
    the flat mosaic (already computed by the caller before this runs) is a
    complete, valid result on its own, so a dense-stereo failure must not lose
    it.

    2026-09-24 (사용자 요청, "CheckCrackViewer 프로그램에 로그 추가"): `patch_match_stereo`
    is a single blocking pycolmap/COLMAP call with no `next_image_callback` the way
    `incremental_mapping` has, and this stage alone took ~3.8h on a 276-image facade
    with ZERO progress visible in the Viewer while it ran (confirmed real complaint,
    same day) -- unacceptable for a multi-hour black box. It DOES write one depth map
    file per (image, geometric/photometric) pair to `dense/stereo/depth_maps/`
    incrementally as it goes (confirmed by directly `ls`-ing that folder mid-run),
    so a background daemon thread polls that folder's file count every
    `progress_interval_s` seconds and logs it as `DENSE_STEREO_PROGRESS` (same
    "X/Y" progress-string convention `colmap_runner.run_colmap`'s
    `COLMAP_MAPPING_PROGRESS` already uses) for `MainViewModel.StageLabels` to
    surface. Relies on pycolmap's C++ call releasing the GIL during the actual
    computation (standard practice for this binding, already proven true for
    `incremental_mapping`'s own callback) -- if it somehow didn't, this poller
    simply never gets scheduled and logs nothing extra, which is exactly today's
    behavior, not a regression.
    """
    import threading
    import time

    import pycolmap

    from src.common.logging import log_event

    workspace_dir = Path(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    sparse_dir = workspace_dir / "sparse_utm"
    if sparse_dir.exists():
        import shutil
        shutil.rmtree(sparse_dir)
    sparse_dir.mkdir(parents=True)
    reconstruction.write(sparse_dir)

    dense_dir = workspace_dir / "dense"
    if dense_dir.exists():
        import shutil
        shutil.rmtree(dense_dir)

    num_images = len(reconstruction.images)

    if logger:
        log_event(
            logger, "info", "Dense Stereo 이미지 언디스토트 시작",
            stage="DENSE_STEREO_UNDISTORT", facade_id=facade_id, image_count=num_images,
        )
    pycolmap.undistort_images(
        output_path=dense_dir,
        input_path=sparse_dir,
        image_path=str(images_dir),
        output_type="COLMAP",
        undistort_options=pycolmap.UndistortCameraOptions(max_image_size=DENSE_MAX_IMAGE_SIZE),
    )

    if logger:
        log_event(
            logger, "info", "Dense Stereo Depth 계산 시작 (수 시간 소요될 수 있음)",
            stage="DENSE_STEREO_PATCHMATCH", facade_id=facade_id,
            image_count=num_images, depth_maps_target=num_images * 2,
        )

    stop_polling = threading.Event()

    def _poll_progress() -> None:
        target = num_images * 2  # geometric + photometric per image
        while not stop_polling.wait(progress_interval_s):
            done = _count_depth_maps(dense_dir)
            log_event(
                logger, "info", "Dense Stereo Depth 계산 진행 중",
                stage="DENSE_STEREO_PROGRESS", facade_id=facade_id,
                progress=f"{done}/{target}",
            )

    poller = None
    if logger:
        poller = threading.Thread(target=_poll_progress, daemon=True)
        poller.start()
    try:
        pycolmap.patch_match_stereo(
            workspace_path=dense_dir,
            workspace_format="COLMAP",
            # cache_size=16: run_dense_stereo_loftr.sh's `--PatchMatchStereo.cache_size 16`
            # (pycolmap default is 32 GB; the process grew to ~22 GB RSS on FRONT with it).
            options=pycolmap.PatchMatchOptions(geom_consistency=True, cache_size=16.0),
        )
    finally:
        stop_polling.set()
        if poller is not None:
            poller.join(timeout=5.0)  # best-effort -- never block the pipeline on a logging thread

    if logger:
        log_event(
            logger, "info", "Dense Stereo 포인트 융합 시작",
            stage="DENSE_STEREO_FUSION", facade_id=facade_id,
        )
    fused_path = dense_dir / "fused.ply"
    # 2026-09-24 실사용 버그(FRONT V004, 실측 확인): output_type을 안 주면 기본값
    # "bin"이 되어 pycolmap.stereo_fusion이 output_path를 COLMAP 바이너리 재구성
    # "폴더"로 취급하려다 `colmap::ExistsDir(fused.ply)` 체크에서 크래시함(.ply는
    # 파일이지 디렉터리가 아니므로 항상 실패) -- patch_match_stereo(depth map 계산,
    # 이 facade에서 3.8시간 소요)는 이미 성공했는데 이 마지막 한 줄 때문에 dense
    # stereo 결과 전체가 날아갔던 것. output_type="PLY"를 명시해 실제 포인트클라우드
    # 파일로 쓰도록 수정.
    pycolmap.stereo_fusion(
        output_path=fused_path,
        workspace_path=dense_dir,
        workspace_format="COLMAP",
        input_type="geometric",
        output_type="PLY",
    )
    if not fused_path.exists():
        return None

    positions, _ = _read_gaussian_or_plain_ply(fused_path)
    return DenseStereoResult(
        num_points_total=len(positions),
        num_points_near_wall=len(positions),  # near-wall filtering happens at render time, not here
        fused_ply_path=str(fused_path),
    )


def _read_gaussian_or_plain_ply(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Reads a binary_little_endian PLY vertex element generically -- works for
    both COLMAP's plain (x,y,z,nx,ny,nz,red,green,blue) fused.ply and would
    tolerate extra fields if ever pointed at a different producer. Returns
    (positions float64 Nx3, colors uint8 Nx3 RGB)."""
    type_map = {
        "float": "<f4", "float32": "<f4", "double": "<f8", "float64": "<f8",
        "uchar": "u1", "uint8": "u1", "char": "i1", "int8": "i1",
        "short": "<i2", "ushort": "<u2", "int": "<i4", "uint": "<u4",
    }
    with open(path, "rb") as f:
        header = b""
        while True:
            line = f.readline()
            header += line
            if line.strip() == b"end_header":
                break
        lines = header.decode("ascii").splitlines()
        fields, n_vertices, in_vertex = [], 0, False
        for line in lines:
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "element":
                in_vertex = parts[1] == "vertex"
                if in_vertex:
                    n_vertices = int(parts[2])
            elif parts[0] == "property" and in_vertex:
                fields.append((parts[2], type_map[parts[1]]))
        dtype = np.dtype(fields)
        data = np.fromfile(f, dtype=dtype, count=n_vertices)
    positions = np.column_stack([data["x"], data["y"], data["z"]]).astype(np.float64)
    colors = np.column_stack([data["red"], data["green"], data["blue"]]).astype(np.uint8)
    return positions, colors


def _render_dense_canvas(
    positions_utm: np.ndarray, colors: np.ndarray, plane, canvas_w: int, canvas_h: int,
    camera_centers_utm: np.ndarray | None = None,
    edge_margin_m: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Z-buffer orthographic render of the near-wall dense point band onto the
    plane's own u/v pixel grid. Returns (canvas BGR uint8, has_data bool mask).
    See module docstring for why the near-wall band filter, splat radius and
    small-hole inpaint are each here (all confirmed real fixes/necessities,
    2026-09-23 gsplat3d session).

    `camera_centers_utm` (2026-09-25): used ONLY to orient the plane normal
    toward the cameras, exactly like the reference
    `gsplat3d/finish_loftr_pipeline.py` does (`if dot(cam_mean - origin,
    normal) < 0: normal = -normal`). Without it the z-buffer below (which
    lets the LARGEST d win, documented as "nearest to camera") silently
    depends on whichever way `cross(e_u, e_v)` happens to point -- confirmed
    on the FRONT facade that it points INTO the wall (cameras sit at -9.3m
    along it), i.e. production's z-order was the exact opposite of the
    reference's: a deeper surface (balcony back wall, window behind a
    railing) overwrote the nearer one wherever they overlap in (u, v).
    Omitted (None) keeps the old behavior only for callers that have no
    camera positions; `build_hybrid_mosaic` always passes them."""
    normal = np.cross(plane.e_u, plane.e_v)
    normal = normal / np.linalg.norm(normal)
    if camera_centers_utm is not None and len(camera_centers_utm) > 0:
        if np.dot(np.asarray(camera_centers_utm).mean(axis=0) - plane.origin, normal) < 0:
            normal = -normal
    rel = positions_utm - plane.origin
    d = rel @ normal
    near_wall = np.abs(d) <= WALL_BAND_M
    rel, d, colors = rel[near_wall], d[near_wall], colors[near_wall]

    u = rel @ plane.e_u
    v = rel @ plane.e_v

    # 2026-09-25 (실사용 발견, FRONT facade V006, 사용자 확정 "파고들어서 전체를 다 고치세요"):
    # a real building CORNER near this facade's own u-extent edge violates the single-flat-plane
    # assumption WALL_BAND_M alone can't catch -- confirmed via direct diagnosis: a coherent cluster
    # of points at the facade's leftmost ~1.7m (u in [4.67, 5.60] out of an overall [3.88, 59.16]
    # extent) sits well within WALL_BAND_M distance-wise, but its own locally-fitted SVD normal is
    # ~44 deg off the main plane's normal (dot=0.72) -- it is genuinely the building's chamfered
    # corner panel (CLAUDE.local.md #4.4's "곡면을 하나의 평면으로 펴지 않는다" case), not sensor
    # noise, rendered into THIS plane's canvas as a warped bright streak. Tried and REJECTED first:
    # per-point/per-cell local-normal-deviation filtering applied globally -- confirmed to gut
    # legitimate balcony/recess detail everywhere (every real balcony bay is ALSO off the flat plane
    # by design, exactly the depth variation dense stereo exists to render correctly, so a global
    # normal-deviation filter cannot tell "balcony" from "corner" apart). A real corner can only
    # occur at the very edge of what this facade's cameras actually saw, so excluding a small u
    # margin at the near-wall point set's own extent -- letting the flat mosaic's hole-fill cover
    # that margin instead (confirmed clean there, unlike dense stereo, at this exact spot) -- removes
    # the corner artifact with zero effect on interior balcony rendering (verified: full-canvas
    # visual diff outside the margin is unchanged).
    margin_m = EDGE_MARGIN_M if edge_margin_m is None else float(edge_margin_m)
    if len(u) > 0 and margin_m > 0:  # the reference procedure applies no margin (edge_margin_m=0.0)
        u_min, u_max = u.min(), u.max()
        edge_ok = (u > u_min + margin_m) & (u < u_max - margin_m)
        u, v, d, colors = u[edge_ok], v[edge_ok], d[edge_ok], colors[edge_ok]

    px = (u * plane.px_per_m).astype(np.int32)
    py = (v * plane.px_per_m).astype(np.int32)
    valid = (px >= 0) & (px < canvas_w) & (py >= 0) & (py < canvas_h)
    px, py, d, colors = px[valid], py[valid], d[valid], colors[valid]

    r = SPLAT_RADIUS_PX
    offsets = [(dx, dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1)]
    px_all = np.concatenate([px + dx for dx, dy in offsets])
    py_all = np.concatenate([py + dy for dx, dy in offsets])
    d_all = np.tile(d, len(offsets))
    colors_all = np.tile(colors, (len(offsets), 1))
    keep = (px_all >= 0) & (px_all < canvas_w) & (py_all >= 0) & (py_all < canvas_h)
    px_all, py_all, d_all, colors_all = px_all[keep], py_all[keep], d_all[keep], colors_all[keep]

    canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
    order = np.argsort(d_all)  # ascending: nearest-camera (largest d, toward the outward normal) written last, wins
    canvas[py_all[order], px_all[order]] = colors_all[order][:, ::-1]  # RGB -> BGR

    gray = cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY)
    hole_mask = (gray == 0).astype(np.uint8)
    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(hole_mask, connectivity=8)
    small_hole_mask = np.zeros_like(hole_mask)
    for label in range(1, n_labels):
        if stats[label, cv2.CC_STAT_AREA] < MIN_INPAINT_HOLE_AREA_PX:
            small_hole_mask[labels == label] = 255
    canvas = cv2.inpaint(canvas, small_hole_mask, inpaintRadius=3, flags=cv2.INPAINT_TELEA)

    has_data = np.zeros((canvas_h, canvas_w), dtype=bool)
    has_data[py_all, px_all] = True
    has_data |= small_hole_mask > 0  # inpainted pixels count as "has data" for the hole-fill decision below
    return canvas, has_data


def _assign_dense_owners(
    plane, canvas_w: int, canvas_h: int,
    camera_names: list[str], camera_centers_utm: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """For every canvas pixel, which registered camera's position (on the
    facade plane) is nearest -- a per-PIXEL owner assignment for the
    dense-stereo-filled region, cheap and image-set-independent (does not need
    a per-point visibility computation). Returns (owner_idx int32 HxW, -1
    where no camera exists at all) and the same `camera_names` list (index i
    of owner_idx maps to camera_names[i]).

    Deliberately reuses each owning camera's ALREADY-COMPUTED flat-plane
    SourceTransform (from the very same rect_result the flat mosaic used) for
    "click canvas pixel -> jump to source photo" rather than computing a new,
    depth-correct per-point reprojection: dense stereo's whole point is real
    parallax-correct POSITION, but for "which photo, roughly where in it" (the
    only thing source_observations/CheckCrackViewer's 원본 보기 actually need),
    the existing flat homography is already right for true-flat regions and
    off by the same (bounded, already-accepted) amount as the flat mosaic's
    own parallax error everywhere else -- reusing it needs no new per-point
    camera-projection math and no C# viewer changes at all."""
    from scipy.spatial import cKDTree

    cam_rel = camera_centers_utm - plane.origin
    cam_u = cam_rel @ plane.e_u
    cam_v = cam_rel @ plane.e_v
    tree = cKDTree(np.column_stack([cam_u, cam_v]))

    grid_u, grid_v = np.meshgrid(
        (np.arange(canvas_w) + 0.5) / plane.px_per_m, (np.arange(canvas_h) + 0.5) / plane.px_per_m,
    )
    _, nearest = tree.query(np.column_stack([grid_u.ravel(), grid_v.ravel()]), k=1)
    return nearest.reshape(canvas_h, canvas_w).astype(np.int32), np.array(camera_names)


def build_hybrid_mosaic(
    reconstruction: "pycolmap.Reconstruction",
    plane,
    dense_result: DenseStereoResult,
    flat_result: MosaicResult,
    canvas_size: tuple[int, int],
    sim3d: "pycolmap.Sim3d",
    edge_margin_m: float | None = None,
) -> MosaicResult:
    """Combines dense-stereo's own render (real depth, no parallax tear) with
    `flat_result` (already-computed flat mosaic) as the hole-filler, and
    builds a matching owner_map/index/source_transforms so
    src.crack.pipeline._compute_source_observations and CheckCrackViewer's
    "원본 보기" work on the hybrid result exactly as they already do on the
    flat-only one -- no new file formats, no viewer changes.

    `sim3d`: `reconstruction`/`plane` are in the UTM-aligned frame, but
    `dense_result.fused_ply_path`'s points are in `reconstruction`'s NATIVE
    (pre-alignment) frame -- `run_dense_stereo` runs on a separately-loaded,
    never-transformed copy (see `_run_facade_pipeline`'s dense-stereo block)
    specifically because running it on the already-aligned reconstruction was
    confirmed, 2026-09-25, to make patch_match_stereo/stereo_fusion produce
    points 50-380m off the real facade plane on the FRONT facade (100% of
    228k fused points, sparse points3D from the SAME reconstruction were
    correctly near-plane throughout, root cause not fully isolated but the
    align-before-vs-after ordering was the only structural difference from
    the gsplat3d exploration scripts that DO produce a correctly-aligned dense
    cloud). `sim3d` is applied to the raw points here, matching those working
    exploration scripts (`gsplat3d/finish_loftr_pipeline.py`) exactly, rather
    than to the reconstruction before dense stereo runs."""
    canvas_w, canvas_h = canvas_size
    plane_w = max(1, int(round(plane.width_m * plane.px_per_m)))
    plane_h = max(1, int(round(plane.height_m * plane.px_per_m)))
    if (canvas_w, canvas_h) != (plane_w, plane_h):
        # The dense render is drawn straight from `plane` (its origin/extent); a flat mosaic on a
        # different (e.g. coverage-cropped, origin-shifted) canvas would be silently misregistered.
        raise ValueError(
            f"flat mosaic canvas {canvas_w}x{canvas_h} != plane canvas {plane_w}x{plane_h}: frames differ"
        )
    positions_raw, colors = _read_gaussian_or_plain_ply(Path(dense_result.fused_ply_path))
    rotation = sim3d.rotation.matrix()
    positions = float(sim3d.scale) * (positions_raw @ rotation.T) + np.asarray(sim3d.translation)
    camera_centers = np.array([img.projection_center() for img in reconstruction.images.values()])
    dense_canvas, dense_has_data = _render_dense_canvas(
        positions, colors, plane, canvas_w, canvas_h, camera_centers_utm=camera_centers,
        edge_margin_m=edge_margin_m,
    )

    flat_visual = flat_result.visual_image
    flat_has_data = np.any(flat_result.observed_mask > 0, axis=-1) if flat_result.observed_mask.ndim == 3 else flat_result.observed_mask > 0

    hybrid_visual = dense_canvas.copy()
    # Reference hole criterion (finish_loftr_pipeline.py: `hole = np.all(dense == 0, axis=2) & has_data`):
    # a dense pixel is a hole iff it is pure black after splat + small-hole inpaint. Replaces the older
    # "was any splat written here" test, so the two agree exactly with the reference procedure.
    dense_has_data = np.any(dense_canvas > 0, axis=2)
    use_flat = (~dense_has_data) & flat_has_data
    if flat_visual is not None:
        hybrid_visual[use_flat] = flat_visual[use_flat]
    hybrid_analysis = hybrid_visual  # dense stereo has no separate low-blend variant; visual IS the analysis source here

    observed_mask = (dense_has_data | flat_has_data).astype(np.uint8) * 255

    camera_names = [Path(img.name).stem for img in reconstruction.images.values()]  # matches rectify_images' own image_id convention
    dense_owner_idx, owner_names = _assign_dense_owners(plane, canvas_w, canvas_h, camera_names, camera_centers)

    # Build one shared owner index: flat_result's own index first (unchanged
    # numbering for pixels it already owns), then any camera names dense-stereo
    # needs that aren't already in it.
    flat_index = list(flat_result.seam_owner_index or [])
    flat_lookup = {name: i + 1 for i, name in enumerate(flat_index)}  # 1-based, matches seam_owner_map convention
    combined_index = list(flat_index)
    name_to_combined_id: dict[str, int] = dict(flat_lookup)
    for name in owner_names:
        if name not in name_to_combined_id:
            combined_index.append(name)
            name_to_combined_id[name] = len(combined_index)

    owner_map = np.zeros((canvas_h, canvas_w), dtype=np.uint16)
    if flat_result.seam_owner_map is not None:
        # Wherever the final pixel actually came from the flat mosaic (dense had
        # no data there), keep flat's own owner id verbatim -- same numbering,
        # same source_transforms, nothing to remap.
        owner_map[use_flat] = flat_result.seam_owner_map[use_flat]

    dense_only = dense_has_data & ~use_flat
    for i, name in enumerate(owner_names):
        pixels = dense_only & (dense_owner_idx == i)
        if pixels.any():
            owner_map[pixels] = name_to_combined_id[name]

    source_transforms: dict[str, SourceTransform] = dict(flat_result.source_transforms or {})

    import copy

    quality = copy.copy(flat_result.quality)  # never mutate the flat mosaic's own already-persisted quality report
    quality.coverage_ratio = float(np.count_nonzero(observed_mask)) / float(observed_mask.size)

    return MosaicResult(
        analysis_image=hybrid_analysis,
        visual_image=hybrid_visual,
        observed_mask=observed_mask,
        quality=quality,
        source_transforms=source_transforms,
        seam_owner_map=owner_map,
        seam_owner_index=combined_index,
    )
