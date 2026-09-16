"""Feed LoFTR (not SIFT) matches into a COLMAP database (CLAUDE.local.md #8/#10/#12).

Root-cause fix for the repetitive-pattern mismatch defect confirmed 2026-09-16
(BACK facade, DJI_0117/0118 area and later DJI_0117/0158/0159/0160/0165/0119):
COLMAP's default matcher is SIFT, which confuses visually-identical repeated
features (round vents/holes on a high-rise facade) and triangulates an
isolated, wrong 3D point near the real one -- visible as duplicated/ghosted
holes in the rectified mosaic. This project already has a modern,
repetition-robust matcher (LoFTR, `src/matching/loftr_matcher.py`, used for
the H-chain path) -- this module is the bridge that lets COLMAP's incremental
mapper consume LoFTR's correspondences instead of SIFT's, via pycolmap's
documented low-level match-injection API (`Database.write_keypoints`/
`write_matches`, `pycolmap.verify_matches`).

General by construction, not tied to any one facade/image: pair selection
reuses `pair_selector.select_pairs` (the project's own general temporal+GPS
scheme, CLAUDE.local.md #7 -- exhaustive N^2 matching is explicitly banned by
#43.4, so this does not attempt to do LoFTR on every possible pair either),
and the keypoint-consolidation step is a generic radius-merge, not anything
keyed to a specific image ID or region. It runs identically for every facade
that has `colmap.use_loftr_matching: true` in its pipeline config.

Keypoint-consolidation problem this module solves: LoFTR is detector-free, so
matching the same image A against two different partners B and C yields two
*different* point sets for A (not indices into one shared per-image keypoint
list, the way SIFT's fixed keypoint detection does) -- but COLMAP's database
schema requires exactly that (`write_matches` takes indices into each image's
own `write_keypoints` array). This module collects every pair's raw points
per image, then merges points that are almost certainly the same underlying
pixel location (same source image, so its own LoFTR coarse-grid geometry is
identical regardless of which partner the pair was matched against -- points
from two pairs of the SAME image do land within a few px of each other when
they represent the same real point) via a radius-based union-find
(`scipy.spatial.cKDTree.query_pairs`), producing one deduplicated per-image
keypoint array plus a raw->canonical index map used to remap every pair's
matches.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from src.common.config import Config
from src.common.types import ImageMetadata


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _consolidate_image_keypoints(
    raw_points: np.ndarray, merge_radius_px: float
) -> tuple[np.ndarray, np.ndarray]:
    """raw_points: (M,2) pixel coords, possibly containing near-duplicates
    from different pairs. Returns (canonical_keypoints (K,2) float32,
    raw_to_canonical (M,) int64 -- raw_to_canonical[i] is the row in
    canonical_keypoints that raw_points[i] was merged into)."""
    from scipy.spatial import cKDTree

    n = len(raw_points)
    if n == 0:
        return np.zeros((0, 2), dtype=np.float32), np.zeros((0,), dtype=np.int64)

    uf = _UnionFind(n)
    tree = cKDTree(raw_points)
    for i, j in tree.query_pairs(r=merge_radius_px):
        uf.union(i, j)

    roots = np.array([uf.find(i) for i in range(n)])
    unique_roots, inverse = np.unique(roots, return_inverse=True)

    canonical = np.zeros((len(unique_roots), 2), dtype=np.float64)
    counts = np.zeros(len(unique_roots), dtype=np.int64)
    np.add.at(canonical, inverse, raw_points)
    np.add.at(counts, inverse, 1)
    canonical /= counts[:, None]
    return canonical.astype(np.float32), inverse.astype(np.int64)


def match_database_with_loftr(
    database_path: str | Path,
    images_dir: str | Path,
    image_filenames: list[str],
    catalog: list[ImageMetadata],
    cfg: Config,
    matcher,
    workspace_dir: str | Path,
    logger=None,
    merge_radius_px: float = 3.0,
) -> dict:
    """Replace whatever keypoints/matches a prior `pycolmap.extract_features`
    call wrote into `database_path` with LoFTR-derived ones, then run COLMAP's
    own geometric verification (RANSAC) so the database is ready for
    `pycolmap.incremental_mapping`/`IncrementalMapper` exactly like a
    SIFT-matched database would be. `image_filenames` must already have
    camera/image rows in the database (i.e. `extract_features` was run first
    -- this function does not create cameras, only keypoints/matches).

    `matcher` is a `TimeoutLoFTRMatcher` (caller-owned, so a facade that
    already has one running for its H-chain path reuses the same GPU worker
    instead of spawning a second one).

    Returns a small stats dict (`pairs_attempted`, `pairs_matched`,
    `pairs_timed_out`, `pairs_failed`, `total_raw_points`,
    `total_canonical_points`) for logging/diagnostics -- never fabricates
    success if LoFTR matching produced nothing usable (mapper.begin_reconstruction
    just won't find any two-view geometry, same as it would for a database with
    no matches at all)."""
    import pycolmap

    from src.common.logging import log_event
    from src.matching.loftr_matcher import MatchTimeoutError
    from src.matching.pair_selector import select_pairs

    workspace_dir = Path(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    name_set = set(image_filenames)
    sub_catalog = [m for m in catalog if Path(m.file_path).name in name_set]
    by_id_meta = {m.image_id: m for m in sub_catalog}
    pairs = select_pairs(sub_catalog, cfg)

    if logger:
        log_event(
            logger, "info", "LoFTR 기반 CM 매칭 시작 (SIFT 대체)",
            stage="COLMAP_LOFTR_MATCH", image_count=len(image_filenames), pair_count=len(pairs),
        )

    db = pycolmap.Database.open(database_path)
    name_to_image_id = {img.name: img.image_id for img in db.read_all_images()}

    raw_points: dict[str, list[np.ndarray]] = {name: [] for name in name_set}
    raw_counts: dict[str, int] = {name: 0 for name in name_set}
    # (name_a, name_b, raw_idx_into_a, raw_idx_into_b) -- indices are into the
    # eventual np.concatenate(raw_points[name]) array, assigned as each pair's
    # points are appended below.
    pair_records: list[tuple[str, str, np.ndarray, np.ndarray]] = []

    stats = {
        "pairs_attempted": len(pairs), "pairs_matched": 0,
        "pairs_timed_out": 0, "pairs_failed": 0,
    }

    for pc in pairs:
        meta_a = by_id_meta.get(pc.image_a)
        meta_b = by_id_meta.get(pc.image_b)
        if meta_a is None or meta_b is None:
            continue
        name_a = Path(meta_a.file_path).name
        name_b = Path(meta_b.file_path).name
        if name_a not in name_to_image_id or name_b not in name_to_image_id:
            continue
        try:
            result = matcher.match(meta_a.file_path, meta_b.file_path)
        except MatchTimeoutError:
            stats["pairs_timed_out"] += 1
            continue
        except Exception as exc:
            stats["pairs_failed"] += 1
            if logger:
                log_event(
                    logger, "warning", "LoFTR 매칭 실패 (해당 쌍 스킵)",
                    stage="COLMAP_LOFTR_MATCH", image_a=name_a, image_b=name_b, error=str(exc),
                )
            continue

        pts_a = np.asarray(result.keypoints_a, dtype=np.float64) if result.keypoints_a is not None else np.zeros((0, 2))
        pts_b = np.asarray(result.keypoints_b, dtype=np.float64) if result.keypoints_b is not None else np.zeros((0, 2))
        n = min(len(pts_a), len(pts_b))
        if n == 0:
            continue
        pts_a, pts_b = pts_a[:n], pts_b[:n]

        start_a, start_b = raw_counts[name_a], raw_counts[name_b]
        idx_a = np.arange(start_a, start_a + n)
        idx_b = np.arange(start_b, start_b + n)
        raw_points[name_a].append(pts_a)
        raw_points[name_b].append(pts_b)
        raw_counts[name_a] += n
        raw_counts[name_b] += n
        pair_records.append((name_a, name_b, idx_a, idx_b))
        stats["pairs_matched"] += 1

    # --- consolidate per-image keypoints ---
    canonical_kps: dict[str, np.ndarray] = {}
    raw_to_canon: dict[str, np.ndarray] = {}
    total_raw = total_canon = 0
    for name in name_set:
        raw = np.concatenate(raw_points[name], axis=0) if raw_points[name] else np.zeros((0, 2))
        canon, mapping = _consolidate_image_keypoints(raw, merge_radius_px)
        canonical_kps[name] = canon
        raw_to_canon[name] = mapping
        total_raw += len(raw)
        total_canon += len(canon)
    stats["total_raw_points"] = total_raw
    stats["total_canonical_points"] = total_canon

    # --- overwrite the SIFT keypoints/descriptors/matches this database had ---
    db.clear_keypoints()
    db.clear_descriptors()
    db.clear_matches()
    db.clear_two_view_geometries()

    for name, kps in canonical_kps.items():
        if len(kps) == 0:
            continue
        db.write_keypoints(name_to_image_id[name], kps.astype(np.float32))

    pair_lines: list[str] = []
    written_pairs = 0
    for name_a, name_b, idx_a, idx_b in pair_records:
        canon_a = raw_to_canon[name_a][idx_a]
        canon_b = raw_to_canon[name_b][idx_b]
        match_arr = np.stack([canon_a, canon_b], axis=1)
        match_arr = np.unique(match_arr, axis=0)  # de-dupe pairs merged onto the same canonical points
        if len(match_arr) == 0:
            continue
        id_a, id_b = name_to_image_id[name_a], name_to_image_id[name_b]
        # COLMAP's database schema keys the matches table by (min_id, max_id);
        # write_matches does not reorder columns for you, so this must be done
        # here or the stored correspondences would silently point at the wrong
        # keypoint in the swapped-order case.
        if id_a < id_b:
            db.write_matches(id_a, id_b, match_arr.astype(np.uint32))
        else:
            db.write_matches(id_b, id_a, match_arr[:, ::-1].astype(np.uint32))
        pair_lines.append(f"{name_a} {name_b}")
        written_pairs += 1
    db.close()

    if written_pairs == 0:
        if logger:
            log_event(
                logger, "warning", "LoFTR 매칭 결과 없음 -- CM 재구성이 등록할 이미지를 찾지 못할 수 있음",
                stage="COLMAP_LOFTR_MATCH", **stats,
            )
        return stats

    pairs_path = workspace_dir / "loftr_verify_pairs.txt"
    pairs_path.write_text("\n".join(pair_lines), encoding="utf-8")
    pycolmap.verify_matches(database_path, pairs_path, pycolmap.TwoViewGeometryOptions())

    if logger:
        log_event(logger, "info", "LoFTR 기반 CM 매칭 완료", stage="COLMAP_LOFTR_MATCH", **stats)

    return stats
