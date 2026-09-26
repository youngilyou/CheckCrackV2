"""hloc(SuperPoint+LightGlue) feature extraction + matching worker — runs under
the SEPARATE hloc_env (D:/ClaudePr/CheckCrack3D/nerfstudio/hloc_env/python.exe),
never imported directly by the main pipeline process.

Why a separate process at all: hloc/SuperPoint/LightGlue live in their own venv
(installed for the gsplat3d exploration work, see
`gsplat3d/run_hloc_reconstruction_full.py`) with its own torch/pycolmap
versions, not the main project's env. Rather than merging environments (real
version-conflict risk — this session already hit one pycolmap version
mismatch elsewhere), this worker is invoked as a subprocess by
`src/matching/hloc_bridge.py` (main env), does ONLY feature extraction +
matching, and hands results back as plain .npy files — no pycolmap import
here at all, so there is nothing to version-mismatch. Incremental SfM mapping
still happens in the main process exactly like the LoFTR/SIFT paths
(`src/sfm/colmap_runner.py`), so COLMAP's own version is whatever the main
env has, always.

2026-09-24: added alongside `src/matching/loftr_colmap_bridge.py` per explicit
user request to offer hloc(SuperPoint) as a selectable matcher backend
(license conflict — Magic Leap non-commercial — explicitly disregarded by the
user for this toggle; CLAUDE.local.md's LoFTR-over-hloc rationale was about
license + this project's own measured quality, not a hard technical block).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images-dir", required=True)
    ap.add_argument("--image-list-file", required=True, help="newline-separated filenames within --images-dir")
    ap.add_argument("--pairs-file", required=True, help="newline-separated 'name_a name_b' pairs to match")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--feature-conf", default="superpoint_max")
    ap.add_argument("--matcher-conf", default="superpoint+lightglue")
    args = ap.parse_args()

    import h5py
    import numpy as np
    from hloc import extract_features, match_features

    images_dir = Path(args.images_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    references = [
        line.strip() for line in Path(args.image_list_file).read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    pair_lines = [line.strip() for line in Path(args.pairs_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not references:
        print("NO_IMAGES", flush=True)
        return
    if not pair_lines:
        print("NO_PAIRS", flush=True)
        return

    feature_conf = extract_features.confs[args.feature_conf]
    matcher_conf = match_features.confs[args.matcher_conf]

    features_h5 = out_dir / "features.h5"
    matches_h5 = out_dir / "matches.h5"
    pairs_path = out_dir / "pairs.txt"
    pairs_path.write_text("\n".join(pair_lines), encoding="utf-8")

    print(f"EXTRACT {len(references)} images", flush=True)
    extract_features.main(feature_conf, images_dir, image_list=references, feature_path=features_h5)
    print(f"MATCH {len(pair_lines)} pairs", flush=True)
    match_features.main(matcher_conf, pairs_path, features=features_h5, matches=matches_h5)

    # --- export to plain .npy so the caller (main env) never needs h5py ---
    export_dir = out_dir / "export"
    kp_dir = export_dir / "keypoints"
    match_dir = export_dir / "matches"
    kp_dir.mkdir(parents=True, exist_ok=True)
    match_dir.mkdir(parents=True, exist_ok=True)

    def _key(name: str) -> str:
        # hloc's h5 keys use forward slashes verbatim; only "/" inside a name
        # (never present here — flat filenames) would need escaping.
        return name

    exported_kp = 0
    with h5py.File(features_h5, "r") as f:
        for name in references:
            key = _key(name)
            if key not in f:
                continue
            kps = f[key]["keypoints"].__array__().astype(np.float32)
            np.save(kp_dir / f"{name}.npy", kps)
            exported_kp += 1

    exported_matches = 0
    with h5py.File(matches_h5, "r") as f:
        for line in pair_lines:
            name_a, name_b = line.split()
            key_a, key_b = _key(name_a), _key(name_b)
            if key_a not in f or key_b not in f[key_a]:
                continue
            grp = f[key_a][key_b]
            matches0 = grp["matches0"].__array__()  # (Ka,) -> index into b's kps, or -1
            valid = matches0 > -1
            if not valid.any():
                continue
            idx_a = np.nonzero(valid)[0]
            idx_b = matches0[valid]
            arr = np.stack([idx_a, idx_b], axis=1).astype(np.int64)
            out_name = f"{name_a}__{name_b}.npy".replace("/", "_")
            np.save(match_dir / out_name, arr)
            exported_matches += 1

    print(f"DONE keypoints={exported_kp} matches={exported_matches}", flush=True)


if __name__ == "__main__":
    sys.exit(main() or 0)
