"""Hole filling for the dense-stereo hybrid mosaic using each photo's OWN per-pixel depth.

Why: the dense render (fused point cloud) leaves holes where patch_match's multi-view consistency
check never agreed -- dark / textureless surfaces such as the roof-edge slab. The flat mosaic used to
fill them places every photo on the fitted wall plane, so a surface that is not exactly on that plane
lands at a photo-dependent canvas position (parallax): neighbouring source photos put the same slab
at different heights and the hole shows up as a staircase. Here every hole pixel is instead filled
from the photos' depth maps (raw pixel -> 3D -> Sim3d -> wall plane (u, v), the same projection the
dense render and DepthCanvasMapper use), so all photos agree on where the surface is.

Never invents wall: a hole pixel no photo has a (filled-within-`fill_max_px`) depth for stays a hole,
i.e. the caller keeps the flat pixel there.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from src.geometry.depth_mapping import DepthCanvasMapper


def _project_photo_points(
    mapper: DepthCanvasMapper, iid: str, image_dir: Path, hole_flat: np.ndarray, canvas_w: int, canvas_h: int,
):
    """One photo's depth map -> (canvas_x, canvas_y, BGR color, distance-to-camera) for every valid
    depth pixel that lands inside `hole_flat` (raveled canvas mask). None if the photo contributes
    nothing (no depth map, no image file, or no point lands in the target area). Shared by
    `fill_holes_from_depth` and `fill_holes_from_depth_exposure_matched` so the two never diverge on
    the actual raw-pixel -> canvas projection math."""
    import cv2

    geom = mapper._geom.get(iid)
    depth = mapper._depth(iid) if geom is not None else None
    if geom is None or depth is None:
        return None
    paths = list(image_dir.glob(f"{iid}.*"))
    if not paths:
        return None
    # 2026-10-08: imread_unicode -- cv2.imread returns None for non-ASCII paths on Windows (remote archives
    # extract to e.g. ...\extracted\수목토_1100_1\...), which silently skipped EVERY photo here
    # (log: WALL_FINISH_APPLIED texture_px=0 on both remote runs of 2026-10-07).
    from src.common.imageio import imread_unicode
    img = imread_unicode(str(paths[0]), cv2.IMREAD_COLOR)
    if img is None:
        return None
    dh, dw = depth.shape[:2]
    if img.shape[:2] != (dh, dw):
        img = cv2.resize(img, (dw, dh), interpolation=cv2.INTER_AREA)

    vv, uu = np.nonzero(depth > 0)
    z = depth[vv, uu].astype(np.float64)
    sx = geom.und_width / dw
    sy = geom.und_height / dh
    xn = ((uu + 0.5) * sx - geom.und_cx) / geom.und_fx
    yn = ((vv + 0.5) * sy - geom.und_cy) / geom.und_fy
    cam = np.column_stack([xn * z, yn * z, z])
    world = (cam - geom.t) @ geom.R
    utm = mapper.s * (world @ mapper.Rs.T) + mapper.Ts
    rel = utm - mapper.origin
    near = np.abs(rel @ mapper.normal) <= mapper.wall_band_m
    cx = np.rint(rel @ mapper.e_u * mapper.px_per_m).astype(np.int64)
    cy = np.rint(rel @ mapper.e_v * mapper.px_per_m).astype(np.int64)
    inside = near & (cx >= 0) & (cx < canvas_w) & (cy >= 0) & (cy < canvas_h)
    pix = cy * canvas_w + cx
    inside &= hole_flat[np.where(inside, pix, 0)]
    if not inside.any():
        return None
    center = mapper.s * (mapper.Rs @ (-geom.R.T @ geom.t)) + mapper.Ts
    key = np.linalg.norm(utm[inside] - center, axis=1).astype(np.float32)
    col = img[vv[inside], uu[inside]]
    return cx[inside].astype(np.int64), cy[inside].astype(np.int64), col, key


def fill_holes_from_depth(
    mapper: DepthCanvasMapper,
    hole_mask: np.ndarray,
    canvas_size: tuple[int, int],
    image_dir: str | Path,
    image_ids: list[str] | None = None,
    logger=None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """Returns (colors (H,W,3) uint8 BGR, filled (H,W) bool, owner (H,W) int32 index into the
    returned name list or -1, names). Per canvas pixel the photo whose camera is nearest to the
    3D point wins (best-viewed, and consistent with how dense owners are assigned)."""
    canvas_w, canvas_h = canvas_size
    image_dir = Path(image_dir)
    names = sorted(image_ids if image_ids is not None else mapper._geom.keys())
    colors = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
    best_key = np.full(canvas_h * canvas_w, np.inf, dtype=np.float32)
    owner = np.full(canvas_h * canvas_w, -1, dtype=np.int32)
    flat_colors = colors.reshape(-1, 3)
    hole_flat = hole_mask.reshape(-1)

    for n, iid in enumerate(names):
        got = _project_photo_points(mapper, iid, image_dir, hole_flat, canvas_w, canvas_h)
        if got is None:
            continue
        cx, cy, col, key = got
        pix = cy * canvas_w + cx
        better = key < best_key[pix]
        pix, key, col = pix[better], key[better], col[better]
        order = np.argsort(-key, kind="stable")  # ascending write order -> smallest key written last
        pix, key, col = pix[order], key[order], col[order]
        best_key[pix] = key
        flat_colors[pix] = col
        owner[pix] = n
        if logger is not None and n % 50 == 0:
            logger.info("DEPTH_FILL_PROGRESS %d/%d", n, len(names))

    filled = np.isfinite(best_key).reshape(canvas_h, canvas_w)
    return colors, filled, owner.reshape(canvas_h, canvas_w), names


MIN_SOURCE_BRIGHTNESS = 25.0  # see fill_holes_from_depth_exposure_matched's docstring


def fill_holes_from_depth_exposure_matched(
    mapper: DepthCanvasMapper,
    hole_mask: np.ndarray,
    canvas_size: tuple[int, int],
    image_dir: str | Path,
    image_ids: list[str] | None = None,
    logger=None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """Same per-pixel depth-correct placement as `fill_holes_from_depth`, but first matches each
    contributing photo's overall exposure/white-balance to its neighbours before compositing.

    Why: several photos routinely land in the same small canvas area (2026-09-27 실사용 발견, BACK
    facade -- one ~470x400px wall panel had 13 different contributing photos, 5 of them 10%+ each).
    `fill_holes_from_depth` picks whichever photo is nearest-camera PER PIXEL with no colour
    correction, so wherever the winner switches between two photos shot under slightly different
    light/exposure, a visible seam ("scratch"/blotch, real pixels but mismatched brightness) appears
    -- confirmed by colour-coding the owner map for that exact panel and finding it matches the
    "scratch" shape exactly. This reuses the SAME exposure compensator the flat mosaic's own
    `blend_visual` already uses (`cv2.detail.ExposureCompensator_GAIN`, whole-image gain -- not
    GAIN_BLOCKS, for the same reason `blend_visual` avoids it: a per-photo ROI here can be large and
    GAIN_BLOCKS' linear system size is quadratic in image count): each photo's own local ROI (just its
    own contributing pixels' bounding box, not the full canvas -- cheap regardless of canvas size)
    is fed as one "warped image" and gets a single per-photo gain, then that correction is applied to
    the sparse colours before the same nearest-camera z-buffer decides the final owner.

    A separate defect found on the same panel (2026-09-27): an open window's interior is genuinely
    near-black in every contributing photo (a dark room, no real "wall" behind it at all) -- exposure
    gain multiplies that near-zero signal by a large factor to match the bright wall around it,
    turning ordinary 8-bit quantization/JPEG noise into a visible false-colour triangle pattern
    (confirmed: raw pre-gain pixels there were < MIN_SOURCE_BRIGHTNESS, the no-exposure-match render
    was flat near-black, only the exposure-matched one showed the artifact). Any point whose RAW
    (pre-gain) brightness is below `MIN_SOURCE_BRIGHTNESS` is dropped before compositing -- the
    caller's existing dense-stereo/flat pixel is kept there instead of a confident-looking but
    fabricated-by-gain colour."""
    import cv2

    canvas_w, canvas_h = canvas_size
    image_dir = Path(image_dir)
    names = sorted(image_ids if image_ids is not None else mapper._geom.keys())
    hole_flat = hole_mask.reshape(-1)

    contributions = []
    for iid in names:
        got = _project_photo_points(mapper, iid, image_dir, hole_flat, canvas_w, canvas_h)
        if got is not None:
            cx, cy, col, key = got
            bright = col.mean(axis=1) >= MIN_SOURCE_BRIGHTNESS  # drop near-black (window-interior) points
            if bright.any():
                contributions.append((iid, cx[bright], cy[bright], col[bright], key[bright]))
        if logger is not None and len(contributions) % 50 == 0:
            logger.info("DEPTH_FILL_PROJECT_PROGRESS %d/%d photos scanned", names.index(iid) + 1, len(names))

    colors = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
    filled = np.zeros((canvas_h, canvas_w), dtype=bool)
    owner = np.full((canvas_h, canvas_w), -1, dtype=np.int32)
    if not contributions:
        return colors, filled, owner, []

    corners: list[tuple[int, int]] = []
    local_imgs: list[np.ndarray] = []
    local_masks: list[np.ndarray] = []
    for iid, cx, cy, col, key in contributions:
        x0, x1 = int(cx.min()), int(cx.max()) + 1
        y0, y1 = int(cy.min()), int(cy.max()) + 1
        limg = np.zeros((y1 - y0, x1 - x0, 3), dtype=np.uint8)
        lmask = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
        limg[cy - y0, cx - x0] = col
        lmask[cy - y0, cx - x0] = 255
        corners.append((x0, y0))
        local_imgs.append(limg)
        local_masks.append(lmask)

    compensator = cv2.detail.ExposureCompensator_createDefault(cv2.detail.ExposureCompensator_GAIN)
    compensator.feed(corners, local_imgs, local_masks)

    best_key = np.full(canvas_h * canvas_w, np.inf, dtype=np.float32)
    flat_colors = colors.reshape(-1, 3)
    owner_flat = owner.reshape(-1)
    names_used = []
    for idx, (iid, cx, cy, col, key) in enumerate(contributions):
        corner = corners[idx]
        x0, y0 = corner
        compensated = compensator.apply(idx, corner, local_imgs[idx], local_masks[idx])
        if isinstance(compensated, cv2.UMat):
            compensated = compensated.get()
        col_corrected = compensated[cy - y0, cx - x0]

        pix = cy * canvas_w + cx
        better = key < best_key[pix]
        pix, key_b, col_b = pix[better], key[better], col_corrected[better]
        order = np.argsort(-key_b, kind="stable")
        pix, col_b = pix[order], col_b[order]
        best_key[pix] = key_b[order]
        flat_colors[pix] = col_b
        owner_flat[pix] = idx
        names_used.append(iid)
        if logger is not None and idx % 50 == 0:
            logger.info("DEPTH_FILL_EXPOSURE_PROGRESS %d/%d", idx, len(contributions))

    filled = np.isfinite(best_key).reshape(canvas_h, canvas_w)
    return colors, filled, owner.reshape(canvas_h, canvas_w), names_used


ROOF_ZONE_M = 1.0       # roof-edge zone = above the dense wall's own top edge (robust p5 of its points) plus this overlap
EDGE_MARGIN_M = 2.0     # same corner exclusion as dense_stereo.EDGE_MARGIN_M (chamfered corner panels are out of plane)
DARK_GRAY_MAX = 70.0    # accept only dark source pixels (see apply_depth_fill for why)


def roof_zone_window(mapper: DepthCanvasMapper, fused_ply_path: str | Path, canvas_size: tuple[int, int]):
    """(x_lo, x_hi, y_hi) canvas px of the region depth-fill is allowed in."""
    from src.geometry.dense_stereo import _read_gaussian_or_plain_ply

    pos, _ = _read_gaussian_or_plain_ply(Path(fused_ply_path))
    utm = mapper.s * (pos @ mapper.Rs.T) + mapper.Ts
    rel = utm - mapper.origin
    near = np.abs(rel @ mapper.normal) <= mapper.wall_band_m
    u = rel[near] @ mapper.e_u
    v = rel[near] @ mapper.e_v
    canvas_w, canvas_h = canvas_size
    x_lo = max(0, int((u.min() + EDGE_MARGIN_M) * mapper.px_per_m))
    x_hi = min(canvas_w, int((u.max() - EDGE_MARGIN_M) * mapper.px_per_m))
    # p5, not min: the dense wall has almost no points ON the dark roof slab (that is the hole), and
    # stray roof structures / chimneys make min() land far above the real wall top.
    y_hi = min(canvas_h, int((np.percentile(v, 5) + ROOF_ZONE_M) * mapper.px_per_m))
    return x_lo, x_hi, y_hi


def apply_depth_fill(
    mapper: DepthCanvasMapper,
    fused_ply_path: str | Path,
    image_dir: str | Path,
    visual: np.ndarray,
    flat_only_mask: np.ndarray,
    logger=None,
):
    """Replaces flat-mosaic hole pixels (`flat_only_mask`: dense had no data there) in the roof-edge
    zone with depth-placed pixels. Returns (new_visual, replaced_mask, owner (H,W) int32, names).

    Scope is deliberately narrow (the roof-edge dark slab, FRONT V010 staircase): (a) only within
    the roof-edge zone (ROOF_ZONE_M below the dense wall's top edge) -- wall-interior holes and balconies are left exactly as they
    were; (b) not within EDGE_MARGIN_M of the wall's left/right ends (chamfered corner panels are off
    the plane); (c) only DARK source pixels -- the slab's neighbours in the depth maps are sky, and
    the nearest-valid-depth completion (fill_max_px) hands parapet depth to sky pixels a few cm away,
    which would otherwise paint sky-coloured fringe onto the wall plane."""
    h, w = visual.shape[:2]
    x_lo, x_hi, y_hi = roof_zone_window(mapper, fused_ply_path, (w, h))
    allowed = np.zeros((h, w), dtype=bool)
    allowed[:y_hi, x_lo:x_hi] = True
    hole = flat_only_mask & allowed
    colors, filled, owner, names = fill_holes_from_depth(mapper, hole, (w, h), image_dir, logger=logger)
    replaced = filled & (colors.mean(axis=2) < DARK_GRAY_MAX)
    out = visual.copy()
    out[replaced] = colors[replaced]
    return out, replaced, owner, names


MIN_WALL_TEXTURE_BRIGHTNESS = 55.0  # below this the ORIGINAL (pre-fix) pixel is a window/shadow opening,
                                     # not paintable wall -- see finish_hybrid_mosaic's docstring


def _sample_bilinear(img: np.ndarray, xy: np.ndarray) -> np.ndarray:
    """Bilinear colour at (N,2) float pixel positions. cv2.remap maps are limited to 32767 columns, so the points are
    laid out in rows of ROW_LEN (padded)."""
    import cv2

    row_len = 16384
    n = len(xy)
    rows = max(1, -(-n // row_len))
    pad = rows * row_len - n
    mx = np.pad(xy[:, 0].astype(np.float32), (0, pad)).reshape(rows, row_len)
    my = np.pad(xy[:, 1].astype(np.float32), (0, pad)).reshape(rows, row_len)
    out = cv2.remap(img, mx, my, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return out.reshape(-1, 3)[:n]


def fill_from_dense_points(
    mapper: DepthCanvasMapper,
    target_mask: np.ndarray,
    depth_canvas: np.ndarray,
    normal: np.ndarray,
    image_dir: str | Path,
    logger=None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """2026-10-12: photo colour for dense-rendered pixels the depth-map resampling could not reach.

    `fill_holes_from_depth` goes photo -> canvas through each photo's depth map, so it can only colour pixels some
    depth map has a value for. On a plain textureless wall (archive #2 RIGHT: white panels) the depth maps are mostly
    empty, and those pixels kept the fused point cloud's own flat grey colour -- visible blotches the flat mosaic does
    not have. Here the direction is reversed: every such canvas pixel already HAS its real 3D position (the dense
    render's winning point: plane point + depth x normal), which is projected straight into the photos; the photo whose
    camera is nearest and that sees the point (not hidden behind its own depth surface where known) gives the colour,
    sampled from the full-resolution original photo. Same safety property as fill_holes_from_depth -- every pixel comes
    from a real measured 3D point; the flat mosaic is never used, so no ghosting.

    Returns (colors BGR (H,W,3), filled (H,W) bool, owner (H,W) int32 index into names or -1, names)."""
    import cv2

    from src.common.imageio import imread_unicode

    canvas_h, canvas_w = target_mask.shape
    image_dir = Path(image_dir)
    names = sorted(mapper._geom.keys())
    colors = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
    owner = np.full((canvas_h, canvas_w), -1, dtype=np.int32)
    ys, xs = np.nonzero(target_mask & np.isfinite(depth_canvas))
    if len(ys) == 0:
        return colors, np.zeros((canvas_h, canvas_w), dtype=bool), owner, names
    u = (xs + 0.5) / mapper.px_per_m
    v = (ys + 0.5) / mapper.px_per_m
    d = depth_canvas[ys, xs].astype(np.float64)
    pts = mapper.origin + np.outer(u, mapper.e_u) + np.outer(v, mapper.e_v) + np.outer(d, np.asarray(normal, float))
    best = np.full(len(ys), np.inf)
    best_col = np.zeros((len(ys), 3), dtype=np.uint8)
    best_owner = np.full(len(ys), -1, dtype=np.int32)

    for n, iid in enumerate(names):
        geom = mapper._geom[iid]
        raw_w, raw_h = 2.0 * geom.raw_params[1], 2.0 * geom.raw_params[2]  # principal point ~ image centre
        corners = np.array([[0, 0], [raw_w, 0], [raw_w, raw_h], [0, raw_h]], dtype=np.float64)
        foot = mapper.map_points_on_plane(iid, corners)
        if not np.isfinite(foot).all():
            continue
        margin = 2.0 * mapper.px_per_m  # depth can shift a point up to ~WALL_BAND off the plane footprint
        sel = np.flatnonzero((xs >= foot[:, 0].min() - margin) & (xs <= foot[:, 0].max() + margin)
                             & (ys >= foot[:, 1].min() - margin) & (ys <= foot[:, 1].max() + margin))
        if len(sel) == 0:
            continue
        center = mapper.camera_center_utm(iid)
        dist = np.linalg.norm(pts[sel] - center, axis=1)
        closer = dist < best[sel]
        sel, dist = sel[closer], dist[closer]
        if len(sel) == 0:
            continue
        xy, _, visible = mapper.project_utm_points(iid, pts[sel])
        inside = visible & (xy[:, 0] >= 0) & (xy[:, 0] <= raw_w - 1) & (xy[:, 1] >= 0) & (xy[:, 1] <= raw_h - 1)
        if not inside.any():
            continue
        paths = list(image_dir.glob(f"{iid}.*"))
        img = imread_unicode(str(paths[0]), cv2.IMREAD_COLOR) if paths else None
        if img is None:
            continue
        sel, dist, xy = sel[inside], dist[inside], xy[inside]
        sampled = _sample_bilinear(img, xy)
        best[sel] = dist
        best_col[sel] = sampled
        best_owner[sel] = n
        if logger is not None and n % 50 == 0:
            logger.info("DENSE_POINT_FILL_PROGRESS %d/%d", n, len(names))

    got = np.isfinite(best)
    colors[ys[got], xs[got]] = best_col[got]
    owner[ys[got], xs[got]] = best_owner[got]
    filled = np.zeros((canvas_h, canvas_w), dtype=bool)
    filled[ys[got], xs[got]] = True
    return colors, filled, owner, names


def finish_hybrid_mosaic(hybrid, flat_result, mapper, image_dir, logger=None):
    """Wall-texture fix applied to an already-built dense/flat hybrid `MosaicResult` (BACK facade,
    2026-09-27~28 session; three approaches tried, this is the one that survived):

    `build_hybrid_mosaic`'s dense pixels are individual 3D points splatted as small flat-colour blocks
    with no blending -- fine where one photo dominates, but visibly blotchy wherever several photos
    each contribute a few points nearby (confirmed: a ~470x400px sample panel had 13 different
    contributing photos, 5 of them 10%+ each; colour-coding the owner map matched the visible
    "scratch" pattern pixel-for-pixel).

    Rejected #1 -- exposure-matched resampling (`fill_holes_from_depth_exposure_matched`, still used
    for FRONT's roof-edge fill via `apply_depth_fill`): resamples every dense pixel from its own best
    photo again with photos' exposure gain-matched first. Measurably reduced the blotch score (11.06
    -> 6.63 on that panel) -- but compared against `flat_result` itself only afterward, which was
    already far cleaner there UNMODIFIED (score 3.44) on both panels tested. Not wrong, just weaker
    than simply using flat.

    Rejected #2 -- flat-primary (use `flat_result` wherever it has data): scored as well as flat
    itself (no surprise) and was instant (no per-photo scan) -- but a user-supplied screenshot with
    exact canvas locations then caught REAL window-row ghosting in `flat_result` at several spots
    (confirmed: same location, dense clean/blotchy, flat visibly doubled). Tried gating flat-primary by
    local 3D relief (a real corner/balcony bulge forces flat's per-photo homography to disagree) --
    measured the relief at both a ghosted spot and a confirmed-clean spot and got THE SAME value
    (0.02-0.03m either way): local point-cloud relief does not predict where flat ghosts. No other
    reliable per-pixel signal was found this session, and ghosting (ambiguous double geometry) is a
    worse defect for a crack-inspection tool than blotchy-but-single-valued texture, so flat-primary is
    not used.

    Accepted -- plain depth resampling, NO exposure matching (`fill_holes_from_depth`): same
    per-pixel real-3D-position resampling as #1, just without the extra exposure-gain step. Weaker
    blotch reduction (11.06 -> ~9.1 on the tested panel) than #1, and #1's extra step is itself
    harmless -- kept out here only because the user asked for exactly this simpler version, having
    compared it directly against dense and found the softer, more natural-looking result preferable.
    Same safety property as #1: every pixel comes from a real measured 3D point's own photo, the same
    guarantee `build_hybrid_mosaic`'s own dense render already relies on, so this carries NONE of
    flat-primary's ghosting risk -- it never touches `flat_result` at all (flat is still used, as
    before, only to fill dense's own true gaps in `build_hybrid_mosaic`). Pixels whose ORIGINAL
    (pre-fix) brightness is below `MIN_WALL_TEXTURE_BRIGHTNESS` are left untouched -- resampling a
    genuinely dark window-interior pixel risks the same false-colour-from-noise failure documented for
    #1 (an open window turned into a swirl of triangles before that guard existed there).

    Returns a new `MosaicResult` (never mutates the inputs)."""
    import cv2

    visual = hybrid.visual_image
    canvas_size = (visual.shape[1], visual.shape[0])

    dense_only = ~(visual == flat_result.visual_image).all(axis=2)
    col, filled, owner_new, names_new = fill_holes_from_depth(
        mapper, dense_only, canvas_size, image_dir, logger=logger,
    )
    gray_orig = cv2.cvtColor(visual, cv2.COLOR_BGR2GRAY)
    texture_gate = filled & (gray_orig > MIN_WALL_TEXTURE_BRIGHTNESS)

    new_visual = visual.copy()
    new_visual[texture_gate] = col[texture_gate]

    # 2026-10-12: dense pixels no depth map reached (textureless wall) -> project their own 3D point into the photos.
    # Only set by build_hybrid_mosaic for a plain wall (no windows/balconies); FRONT/BACK-type facades skip this block.
    relief = getattr(hybrid, "wall_relief", None)
    if logger is not None and relief is not None:
        logger.info("WALL_RELIEF plain_wall=%s relief_fraction_0_3m=%s", relief.get("plain_wall"), relief.get("relief_fraction_0_3m"))
    dense_depth = getattr(hybrid, "dense_depth_canvas", None)
    dense_normal = getattr(hybrid, "dense_normal", None)
    point_gate = np.zeros_like(texture_gate)
    if dense_depth is not None and dense_normal is not None:
        # Plain wall only (build_hybrid_mosaic sets dense_depth just then): every observed pixel the depth-map
        # resampling did not reach -- dense pixels with no depth map AND the dense holes build_hybrid_mosaic filled
        # from the blended flat mosaic. Archive #2 RIGHT: those holes were the visible soft grey blocks between sharp
        # photo-resampled patches. On a plain wall (relief 0 %) the wall plane IS the measured surface, so a hole
        # pixel takes depth 0 (on the plane) and is coloured from the best photo like every other pixel -- one
        # consistent photo texture over the whole wall.
        observed = (hybrid.observed_mask > 0) if hybrid.observed_mask.ndim == 2 else np.any(hybrid.observed_mask > 0, axis=-1)
        remaining = observed & ~texture_gate & (gray_orig > MIN_WALL_TEXTURE_BRIGHTNESS)
        plain_depth = np.where(np.isfinite(dense_depth), dense_depth, 0.0).astype(np.float32)
        if remaining.any():
            col2, filled2, owner2, names2 = fill_from_dense_points(
                mapper, remaining, plain_depth, dense_normal, image_dir, logger=logger,
            )
            point_gate = filled2
            new_visual[point_gate] = col2[point_gate]
            # merge into the owner bookkeeping below as if it came from the depth-map fill
            names_index = {name: i for i, name in enumerate(names_new)}
            for i, name in enumerate(names2):
                if name not in names_index:
                    names_index[name] = len(names_new)
                    names_new = list(names_new) + [name]
            remap = np.array([names_index[nm] for nm in names2], dtype=np.int32) if names2 else np.zeros(0, np.int32)
            owner_new = owner_new.copy()
            owner_new[point_gate] = remap[owner2[point_gate]]
            texture_gate = texture_gate | point_gate
            if logger is not None:
                logger.info("DENSE_POINT_FILL %d px (depth-map fill reached %d px)",
                            int(point_gate.sum()), int(filled.sum()))

    owner_map = hybrid.seam_owner_map.copy() if hybrid.seam_owner_map is not None else None
    combined_index = list(hybrid.seam_owner_index or [])
    if owner_map is not None and texture_gate.any():
        lookup = {name: i + 1 for i, name in enumerate(combined_index)}  # 1-based, matches convention
        for name in names_new:
            if name not in lookup:
                combined_index.append(name)
                lookup[name] = len(combined_index)
        remap = np.zeros(len(names_new) + 1, dtype=np.uint16)
        for k, name in enumerate(names_new):
            remap[k] = lookup[name]
        owner_map[texture_gate] = remap[owner_new[texture_gate]]

    if logger is not None:
        logger.info("WALL_FINISH_APPLIED texture_px=%d", int(texture_gate.sum()))

    return type(hybrid)(
        analysis_image=new_visual,  # hybrid convention: analysis == visual (see build_hybrid_mosaic)
        visual_image=new_visual,
        observed_mask=hybrid.observed_mask,
        quality=hybrid.quality,
        source_transforms=hybrid.source_transforms,
        seam_owner_map=owner_map,
        seam_owner_index=combined_index,
    )
