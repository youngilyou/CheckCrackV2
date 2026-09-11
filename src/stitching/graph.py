"""Global stitch graph (CLAUDE.local.md #10).

Homographies are composed along a *weighted* shortest-path tree from a chosen
reference image (Dijkstra over each edge's 1/inlier_ratio cost), not
accumulated as a naive linear chain (#10: "단순 chain 누적 ... 만 사용하지
않는다") and not a plain hop-count BFS either (confirmed real, 2026-09-11: a
hop-count BFS can route a node through several low-confidence edges just
because that happens to be fewest hops, even when a slightly-longer,
higher-confidence path to the same node already exists in the graph -- and
since every edge's own small error compounds as more of them get chained,
which edges get used matters as much as how many). Images in a different
connected component than the reference, or reachable only through edges
whose combined cost exceeds `max_cumulative_weight`, are reported as
unreachable rather than force-merged or composed through untrustworthy
matches -- the COLMAP fallback (#12) exists precisely to rectify facades
this path can't confidently cover.
"""

from __future__ import annotations

import networkx as nx
import numpy as np

from src.common.types import GeometryResult


def build_stitch_graph(geometry_results: list[GeometryResult]) -> nx.Graph:
    g = nx.Graph()
    for r in geometry_results:
        if r.status != "OK":
            continue
        weight = 1.0 / max(r.inlier_ratio, 1e-6)
        g.add_edge(r.image_a, r.image_b, geom=r, weight=weight)
    return g


def _edge_homography_u_to_v(g: nx.Graph, u: str, v: str) -> np.ndarray:
    """Direct u->v homography for graph edge (u, v), regardless of which way
    GeometryResult.image_a/image_b happened to be assigned when this edge was
    matched (image_a always maps -> image_b, never guaranteed to be u)."""
    geom = g.edges[u, v]["geom"]
    if geom.image_a == u and geom.image_b == v:
        return geom.homography
    if geom.image_a == v and geom.image_b == u:
        return np.linalg.inv(geom.homography)
    raise AssertionError(f"edge/geom id mismatch for ({u}, {v})")


def detect_inconsistent_edges(
    g: nx.Graph,
    sizes: dict[str, tuple[int, int]],
    max_discrepancy_px: float = 500.0,
    min_disagreeing_witnesses: int = 2,
) -> set[frozenset]:
    """Flags edges that are very likely a repeated-pattern false match (or any
    other geometrically-wrong-but-inlier-passing pairwise match) rather than
    a merely imprecise one, using *independent triangle* cycle-consistency --
    unlike `compute_drift_score` (which only checks non-tree edges against one
    fixed spanning tree, purely as a facade-level diagnostic), this checks
    every edge against every common-neighbor 2-hop alternative, before any
    tree is even picked, specifically so `compute_global_homographies` can be
    kept from ever routing through a flagged edge in the first place.

    Confirmed real, 2026-09-11 (FRONT facade, a repetitive-window apartment
    block): `compute_global_homographies`'s weighted-shortest-path + cutoff
    (both keyed on average edge confidence) left max_drift_score_px at
    ~259862px with zero images excluded -- because the offending edge had a
    perfectly ordinary inlier_ratio (LoFTR found plenty of matches, they just
    matched the wrong-but-identical-looking window/floor to the wrong one),
    so nothing about its *weight* looked unusual. A single wrong-but-plausible
    edge inherited by everything downstream of it in the tree is a different
    failure mode than "many mediocre edges chained together", and needs a
    different signal: does this edge's own claimed transform actually agree
    with what the *rest of the graph* independently implies, not just how
    many LoFTR matches it found.

    `max_discrepancy_px=500.0` was tuned (not guessed) against that same real
    FRONT facade's actual LoFTR matching output (cached geometry_results, 1856
    OK edges over 150 images): the first attempt at 40px flagged 1510/1856
    edges (81%!) -- far too aggressive, fragmenting the graph into 36
    disconnected components with 55 images unreachable, trading catastrophic
    drift for catastrophic coverage loss instead. A sweep across thresholds
    found 500px keeps the graph fully connected (0 unreachable, 1 component)
    while still cutting mean_drift_px 998->163 and max_drift_px
    1,000,219->4,761 -- both dramatically better without giving up any
    coverage. Below full-coverage thresholds, mean/max drift keep falling as
    the threshold tightens further, but only by sacrificing reachability --
    500px is the point past which tightening starts costing coverage instead
    of buying trust. Note the facade still easily exceeds this project's own
    `max_seam_drift_px` (20px) either way -- the point of this function was
    never to make the H-chain path trustworthy on its own for a facade this
    repetitive, only to stop it from being *needlessly* catastrophic (a
    15729x10036 canvas that's 95% wasted background is a worse diagnostic
    artifact, and a slower blend, than a merely-bad one) on the way to the
    COLMAP fallback that's the actual answer here.

    For edge (u, v), for every other node w with its own edge to both u and v,
    compare the direct H(u->v) against H(w->v) @ H(u->w) (u->w->v composed).
    A single disagreeing witness could itself be the wrong one (repeated
    patterns can fool more than one pairing), so an edge is only flagged when
    at least `min_disagreeing_witnesses` *independent* triangles disagree --
    multiple unrelated witnesses converging on "this direct edge is the odd
    one out" is far stronger evidence than any single comparison.

    Returns a set of frozenset({u, v}) edge keys to drop from the graph
    entirely before path-finding (see stitching/mosaic.py's stitch_facade).
    """
    flagged: set[frozenset] = set()
    for u, v in g.edges:
        if u not in sizes:
            continue
        try:
            h_uv_direct = _edge_homography_u_to_v(g, u, v)
        except AssertionError:
            continue
        common = (set(g.neighbors(u)) & set(g.neighbors(v))) - {u, v}
        if len(common) < min_disagreeing_witnesses:
            continue

        w_px, h_px = sizes[u]
        corners = np.array([[0, 0], [w_px, 0], [w_px, h_px], [0, h_px]], dtype=np.float64)
        direct_corners = _apply_homography(h_uv_direct, corners)

        disagreeing = 0
        for w in common:
            try:
                h_uw = _edge_homography_u_to_v(g, u, w)
                h_wv = _edge_homography_u_to_v(g, w, v)
            except AssertionError:
                continue
            h_uv_via_w = h_wv @ h_uw
            predicted_corners = _apply_homography(h_uv_via_w, corners)
            discrepancy_px = float(np.max(np.linalg.norm(predicted_corners - direct_corners, axis=1)))
            if discrepancy_px > max_discrepancy_px:
                disagreeing += 1

        if disagreeing >= min_disagreeing_witnesses:
            flagged.add(frozenset((u, v)))
    return flagged


def pick_reference(g: nx.Graph) -> str | None:
    """Pick the best-connected node as the stitch reference frame (deterministic tie-break)."""
    if g.number_of_nodes() == 0:
        return None
    degrees = dict(g.degree())
    return max(degrees.items(), key=lambda kv: (kv[1], kv[0]))[0]


def compute_global_homographies(
    g: nx.Graph, reference: str, max_cumulative_weight: float | None = None
) -> tuple[dict[str, np.ndarray], list[str]]:
    """Compose per-edge homographies along the highest-confidence path from
    `reference` to each node (weighted shortest path / Dijkstra over the
    per-edge `weight` build_stitch_graph already computes as 1/inlier_ratio —
    previously computed but never actually read anywhere, confirmed by
    searching the codebase 2026-09-11).

    `max_cumulative_weight`, if given, additionally excludes any node whose
    *best available* path still costs more than this — every path to it runs
    through enough low-confidence edges that composing through them isn't
    trustworthy, so it's treated exactly like a different-connected-component
    node rather than composed anyway just to fill coverage.

    Returns (homographies mapping each reachable, trusted node -> reference
    frame, list of node ids that are unreachable or excluded by the cutoff).
    """
    distances, paths = nx.single_source_dijkstra(g, reference, weight="weight")

    homographies: dict[str, np.ndarray] = {reference: np.eye(3)}
    for node in sorted(distances, key=lambda n: distances[n]):
        if node == reference:
            continue
        if max_cumulative_weight is not None and distances[node] > max_cumulative_weight:
            continue
        u, v = paths[node][-2], paths[node][-1]
        if u not in homographies:
            # u's own distance was smaller than v's (Dijkstra processes nodes
            # in non-decreasing distance order), so this only happens if u
            # itself was cut off above — every path through it is equally
            # untrustworthy, so v is excluded too rather than composed anyway.
            continue
        geom = g.edges[u, v]["geom"]
        if geom.image_a == u and geom.image_b == v:
            h_v_to_u = np.linalg.inv(geom.homography)
        elif geom.image_a == v and geom.image_b == u:
            h_v_to_u = geom.homography
        else:
            raise AssertionError(f"edge/geom id mismatch for ({u}, {v})")
        homographies[v] = homographies[u] @ h_v_to_u

    unreachable = [n for n in g.nodes if n not in homographies]
    return homographies, unreachable


def refine_homographies_globally(
    g: nx.Graph,
    reference: str,
    homographies: dict[str, np.ndarray],
    sizes: dict[str, tuple[int, int]],
) -> dict[str, np.ndarray]:
    """Jointly refines every node's homography using *every* surviving edge in
    `g` as a constraint (motion averaging / bundle-adjustment-style least
    squares over the 2D homography graph), not just the single shortest-path
    tree `compute_global_homographies` picked -- so the many redundant edges
    that tree had to discard (this facade: ~1187 surviving edges but only
    ~149 needed for a spanning tree) can correct accumulated chain error
    instead of only ever being usable as a read-only diagnostic
    (`compute_drift_score`).

    Confirmed real, 2026-09-11 (same FRONT facade, after detect_inconsistent_edges
    already removed the single-catastrophic-edge failure mode): a handful of
    images (e.g. DJI_0171) still landed thousands of px off canvas-position
    even though *every* edge on their own shortest path individually looked
    fine (passed both the inlier-ratio weight check and the triangle
    consistency check) -- 3 individually-small errors still compounded into a
    large one. Tightening the path-length cutoff to squeeze this out trades
    it for coverage at a terrible rate (confirmed: dropping the cutoff enough
    to fix it made 57% of the facade's images unreachable), so the only way
    to actually fix the accumulation itself (not just refuse to serve it) is
    to let every image's position be corrected by the graph's redundancy,
    the same role bundle adjustment plays for camera poses in 3D.

    Uses `compute_global_homographies`'s own tree-composed result as the
    initial guess (already a decent starting point) and refines with
    `scipy.optimize.least_squares` (Trust Region Reflective, sparse Jacobian
    -- each edge's residual only touches its own two endpoints' 8 parameters
    out of ~150*8, so a dense Jacobian would be wasteful and slow for no
    benefit). Each homography is parameterized as its own 8 free values with
    h[2,2] fixed to 1 (standard projective normalization); the reference
    image's own homography (identity) is never a free parameter -- fixing
    one node's frame is what keeps the whole system from just sliding around
    together with zero cost (a "gauge" constraint, same idea as fixing one
    camera in bundle adjustment).
    """
    from scipy.optimize import least_squares
    from scipy.sparse import lil_matrix

    nodes = [n for n in homographies if n != reference and n in sizes]
    if not nodes:
        return dict(homographies)
    index = {n: i for i, n in enumerate(nodes)}
    n_params = len(nodes) * 8

    def pack(hmap: dict[str, np.ndarray]) -> np.ndarray:
        x = np.zeros(n_params)
        for n, i in index.items():
            h = hmap[n]
            x[i * 8 : (i + 1) * 8] = (h / h[2, 2]).flatten()[:8]
        return x

    def unpack(x: np.ndarray) -> dict[str, np.ndarray]:
        hmap = {reference: np.eye(3)}
        for n, i in index.items():
            flat = x[i * 8 : (i + 1) * 8]
            hmap[n] = np.append(flat, 1.0).reshape(3, 3)
        return hmap

    edges = [
        (u, v) for u, v in g.edges
        if u in sizes and (u == reference or u in index) and (v == reference or v in index)
    ]
    corners = {
        iid: np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float64) for iid, (w, h) in sizes.items()
    }

    def residuals(x: np.ndarray) -> np.ndarray:
        hmap = unpack(x)
        out = np.empty(len(edges) * 8)
        for k, (u, v) in enumerate(edges):
            h_direct = _edge_homography_u_to_v(g, u, v)
            h_predicted = np.linalg.inv(hmap[v]) @ hmap[u]
            direct_pts = _apply_homography(h_direct, corners[u])
            predicted_pts = _apply_homography(h_predicted, corners[u])
            out[k * 8 : (k + 1) * 8] = (direct_pts - predicted_pts).flatten()
        return out

    sparsity = lil_matrix((len(edges) * 8, n_params), dtype=np.int8)
    for k, (u, v) in enumerate(edges):
        rows = slice(k * 8, (k + 1) * 8)
        if u in index:
            sparsity[rows, index[u] * 8 : (index[u] + 1) * 8] = 1
        if v in index:
            sparsity[rows, index[v] * 8 : (index[v] + 1) * 8] = 1

    x0 = pack(homographies)
    # x_scale="jac" + tr_solver="lsmr" confirmed real, 2026-09-11 (same FRONT
    # facade): default trf settings hit its xtol stopping condition after
    # only 6 function evaluations with first-order optimality still ~3.57e9
    # (nowhere near converged) -- a homography's 8 parameters span wildly
    # different natural scales (rotation/shear terms near 1 vs translation
    # terms in the thousands of px), which badly conditions the unscaled
    # trust-region step; automatic Jacobian-based scaling roughly halved
    # both mean and max drift versus the unscaled default.
    result = least_squares(
        residuals, x0, jac_sparsity=sparsity, method="trf", x_scale="jac", tr_solver="lsmr", max_nfev=500
    )
    return unpack(result.x)


def count_connected_components(g: nx.Graph) -> int:
    if g.number_of_nodes() == 0:
        return 0
    return nx.number_connected_components(g)


def _apply_homography(H: np.ndarray, pts: np.ndarray) -> np.ndarray:
    homog = np.hstack([pts, np.ones((pts.shape[0], 1))])
    proj = homog @ H.T
    return proj[:, :2] / proj[:, 2:3]


def compute_drift_score(
    g: nx.Graph,
    reference: str,
    homographies: dict[str, np.ndarray],
    sizes: dict[str, tuple[int, int]],
) -> tuple[float | None, float | None, int]:
    """Cycle-consistency drift (CLAUDE.local.md #10/#11/#12).

    For every graph edge that is NOT part of the BFS spanning tree used to
    compose `homographies`, compare the directly measured pair homography
    against the one predicted by chaining through the reference frame.
    Disagreement here is accumulated chain error a naive tree traversal
    can't self-correct — the signal #12 uses to decide a COLMAP fallback is
    warranted.

    Returns (mean_drift_px, max_drift_px, cycle_edge_count). The first two
    are None when the graph has no cycle-closing edge to check against
    (nothing to disagree with, not evidence of zero drift).
    """
    tree_edges = {frozenset(e) for e in nx.bfs_edges(g, reference)}
    cycle_edges = [e for e in g.edges if frozenset(e) not in tree_edges]

    errors: list[float] = []
    for u, v in cycle_edges:
        if u not in homographies or v not in homographies or u not in sizes:
            continue
        geom = g.edges[u, v]["geom"]
        if geom.image_a == u and geom.image_b == v:
            h_u_to_v_direct = geom.homography
        elif geom.image_a == v and geom.image_b == u:
            h_u_to_v_direct = np.linalg.inv(geom.homography)
        else:
            continue
        h_u_to_v_predicted = np.linalg.inv(homographies[v]) @ homographies[u]

        w, h = sizes[u]
        corners = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float64)
        predicted = _apply_homography(h_u_to_v_predicted, corners)
        direct = _apply_homography(h_u_to_v_direct, corners)
        errors.extend(np.linalg.norm(predicted - direct, axis=1).tolist())

    if not errors:
        return None, None, len(cycle_edges)
    return float(np.mean(errors)), float(np.max(errors)), len(cycle_edges)
