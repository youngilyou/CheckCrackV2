"""Feed hloc(SuperPoint+LightGlue) matches into a COLMAP database — the hloc
counterpart of `src/matching/loftr_colmap_bridge.py`, offered as a selectable
matcher backend per explicit user request (2026-09-24): "LoFTR 적용, hloc
(SuperPoint) 적용 설정 창에 선택 콤보 박스 추가 하삼 : 라이선스 문제 고려하지 않음" — the
Magic Leap non-commercial license on SuperPoint/SuperGlue/LightGlue that made
this project standardize on LoFTR (see loftr_colmap_bridge.py's own docstring)
is a real constraint for a commercial deployment, but the user has explicitly
said to disregard it for this toggle so both backends can be compared
side-by-side, same as the earlier gsplat3d exploration
(`gsplat3d/run_hloc_reconstruction_full.py`) already did once, manually.

Runs hloc as a SEPARATE SUBPROCESS under its own dedicated environment
(`D:/ClaudePr/CheckCrack3D/nerfstudio/hloc_env/python.exe` by default,
overridable via `colmap.hloc_env_python` in the pipeline config) rather than
importing it in-process — hloc/SuperPoint/LightGlue were never installed into
this project's main venv, and merging environments risks exactly the kind of
pycolmap version mismatch this project already hit and fixed once this
session for the native-COLMAP-binary dense-stereo path. `tools/hloc_worker.py`
(run under that separate env) does ONLY feature extraction + matching and
exports plain `.npy` files; incremental SfM mapping still happens back here,
in the main process, on whatever pycolmap version the main env has — same
division of labor as the LoFTR bridge (in-process there only because LoFTR
itself already lives in the main env's torch install).

Unlike LoFTR (detector-free — the same image gets different point sets
depending which partner it was paired against, requiring the LoFTR bridge's
radius-merge keypoint consolidation), SuperPoint is a fixed per-image
detector: hloc's `extract_features` produces exactly ONE keypoint array per
image regardless of how many pairs it appears in, and `match_features`
matches are already indices into those two fixed arrays. So this bridge does
not need any consolidation step at all — it is structurally simpler than the
LoFTR one, just split across a process boundary.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from src.common.config import Config
from src.common.types import ImageMetadata

DEFAULT_HLOC_ENV_PYTHON = r"D:\ClaudePr\CheckCrack3D\nerfstudio\hloc_env\python.exe"
_WORKER_SCRIPT = Path(__file__).resolve().parent.parent.parent / "tools" / "hloc_worker.py"


def _resolve_hloc_env_python(cfg: Config | None) -> Path:
    if cfg is not None and "colmap" in cfg and "hloc_env_python" in cfg.colmap:
        return Path(str(cfg.colmap.hloc_env_python))
    return Path(DEFAULT_HLOC_ENV_PYTHON)


def match_database_with_hloc(
    database_path: str | Path,
    images_dir: str | Path,
    image_filenames: list[str],
    catalog: list[ImageMetadata],
    cfg: Config,
    workspace_dir: str | Path,
    logger=None,
    hloc_env_python: str | Path | None = None,
    feature_conf: str = "superpoint_max",
    matcher_conf: str = "superpoint+lightglue",
) -> dict:
    """Replace whatever keypoints/matches a prior `pycolmap.extract_features`
    call wrote into `database_path` with hloc(SuperPoint+LightGlue)-derived
    ones, then run COLMAP's own geometric verification so the database is
    ready for `pycolmap.incremental_mapping` — same contract as
    `loftr_colmap_bridge.match_database_with_loftr` (same required db state
    going in and coming out), so `src/sfm/colmap_runner.py` can pick either
    at the same call site.

    Pair selection reuses `pair_selector.select_pairs` (the project's own
    general temporal+GPS scheme, CLAUDE.local.md #7) — exhaustive N^2
    matching is explicitly banned by #43.4, so this never asks hloc to match
    every possible pair either, only the same candidate set LoFTR/SIFT would
    get for this facade.

    Raises `RuntimeError` if the hloc_env subprocess itself fails (missing
    env, crashed worker) — this never silently falls back to a different
    backend or fabricates a result; the caller (`colmap_runner.run_colmap`)
    decides what to do about that.
    """
    import pycolmap

    from src.common.logging import log_event
    from src.matching.pair_selector import select_pairs

    workspace_dir = Path(workspace_dir)
    hloc_out_dir = workspace_dir / "hloc"
    hloc_out_dir.mkdir(parents=True, exist_ok=True)

    resolved_python = Path(hloc_env_python) if hloc_env_python is not None else _resolve_hloc_env_python(cfg)
    if not resolved_python.exists():
        raise RuntimeError(
            f"hloc_env python.exe not found at {resolved_python} -- "
            "set colmap.hloc_env_python in the pipeline config if it moved"
        )

    name_set = set(image_filenames)
    sub_catalog = [m for m in catalog if Path(m.file_path).name in name_set]
    by_id_meta = {m.image_id: m for m in sub_catalog}
    pairs = select_pairs(sub_catalog, cfg)

    db = pycolmap.Database.open(database_path)
    name_to_image_id = {img.name: img.image_id for img in db.read_all_images()}

    pair_lines: list[str] = []
    for pc in pairs:
        meta_a = by_id_meta.get(pc.image_a)
        meta_b = by_id_meta.get(pc.image_b)
        if meta_a is None or meta_b is None:
            continue
        name_a, name_b = Path(meta_a.file_path).name, Path(meta_b.file_path).name
        if name_a not in name_to_image_id or name_b not in name_to_image_id:
            continue
        pair_lines.append(f"{name_a} {name_b}")

    stats = {"pairs_attempted": len(pairs), "pairs_candidate": len(pair_lines)}
    if not pair_lines:
        db.close()
        if logger:
            log_event(logger, "warning", "hloc: 매칭 후보 페어 없음", stage="COLMAP_HLOC_MATCH", **stats)
        return stats

    image_list_path = hloc_out_dir / "image_list.txt"
    image_list_path.write_text("\n".join(sorted(name_set)), encoding="utf-8")
    pairs_in_path = hloc_out_dir / "pairs_in.txt"
    pairs_in_path.write_text("\n".join(pair_lines), encoding="utf-8")

    if logger:
        log_event(
            logger, "info", "hloc(SuperPoint) 매칭 시작 (별도 env subprocess)",
            stage="COLMAP_HLOC_MATCH", image_count=len(name_set), pair_count=len(pair_lines),
        )

    cmd = [
        str(resolved_python), str(_WORKER_SCRIPT),
        "--images-dir", str(images_dir),
        "--image-list-file", str(image_list_path),
        "--pairs-file", str(pairs_in_path),
        "--out-dir", str(hloc_out_dir),
        "--feature-conf", feature_conf,
        "--matcher-conf", matcher_conf,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        db.close()
        raise RuntimeError(
            f"hloc worker failed (exit {proc.returncode})\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr[-4000:]}"
        )

    export_dir = hloc_out_dir / "export"
    kp_dir = export_dir / "keypoints"
    match_dir = export_dir / "matches"

    db.clear_keypoints()
    db.clear_descriptors()
    db.clear_matches()
    db.clear_two_view_geometries()

    written_kp_images = 0
    for name in name_set:
        kp_path = kp_dir / f"{name}.npy"
        if not kp_path.exists():
            continue
        kps = np.load(kp_path)
        if len(kps) == 0:
            continue
        db.write_keypoints(name_to_image_id[name], kps.astype(np.float32))
        written_kp_images += 1

    written_pairs = 0
    verify_pair_lines: list[str] = []
    for line in pair_lines:
        name_a, name_b = line.split()
        match_path = match_dir / f"{name_a}__{name_b}.npy".replace("/", "_")
        if not match_path.exists():
            continue
        arr = np.load(match_path)
        if len(arr) == 0:
            continue
        id_a, id_b = name_to_image_id[name_a], name_to_image_id[name_b]
        if id_a < id_b:
            db.write_matches(id_a, id_b, arr.astype(np.uint32))
        else:
            db.write_matches(id_b, id_a, arr[:, ::-1].astype(np.uint32))
        verify_pair_lines.append(f"{name_a} {name_b}")
        written_pairs += 1
    db.close()

    stats["written_keypoint_images"] = written_kp_images
    stats["written_pairs"] = written_pairs
    if written_pairs == 0:
        if logger:
            log_event(logger, "warning", "hloc 매칭 결과 없음 -- CM 재구성이 등록할 이미지를 찾지 못할 수 있음", stage="COLMAP_HLOC_MATCH", **stats)
        return stats

    verify_pairs_path = hloc_out_dir / "verify_pairs.txt"
    verify_pairs_path.write_text("\n".join(verify_pair_lines), encoding="utf-8")
    pycolmap.verify_matches(database_path, verify_pairs_path, pycolmap.TwoViewGeometryOptions())

    if logger:
        log_event(logger, "info", "hloc 기반 CM 매칭 완료", stage="COLMAP_HLOC_MATCH", **stats)

    return stats
