"""Delete the Dense Stereo intermediates nothing reads any more from output folders that finished
BEFORE the pipeline started doing it itself (colmap.dense_cleanup_intermediate, 2026-10-06).

What is deleted / kept: src/geometry/dense_stereo.py::dense_intermediate_paths. Only folders with a
finished dense result (`*_analysis_colmap_dense.tif` next to `colmap_dense/`) and the files later steps
need (geometric depth maps, sparse, fused.ply) are touched.

Default is a dry run (prints what would be freed). Add --apply to actually delete.

Usage:
    python tools/cleanup_dense_intermediates.py <folder> [<folder> ...] [--apply]
    (each folder may be a run folder like .../output/V010 or any parent of several; searched recursively)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.geometry.dense_stereo import cleanup_dense_intermediates  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    args = ap.parse_args()

    total = 0
    for root in args.folders:
        for dense_dir in sorted(Path(root).rglob("colmap_dense/dense")):
            run_dir = dense_dir.parent.parent
            if not any(run_dir.glob("*_analysis_colmap_dense.tif")):
                print(f"[건너뜀] {run_dir}: Dense 결과(*_analysis_colmap_dense.tif) 없음 -- 미완료 실행일 수 있어 그대로 둠")
                continue
            info = cleanup_dense_intermediates(dense_dir, dry_run=not args.apply)
            if info["skipped_reason"]:
                print(f"[건너뜀] {run_dir}: {info['skipped_reason']}")
                continue
            total += info["bytes_freed"]
            verb = "삭제" if args.apply else "삭제 예정"
            print(f"[{verb}] {run_dir}: 항목 {info['deleted']}개, {info['bytes_freed'] / 1e9:.1f} GB")
    print(f"합계 {total / 1e9:.1f} GB {'삭제함' if args.apply else '삭제 예정 (--apply로 실제 삭제)'}")


if __name__ == "__main__":
    main()
