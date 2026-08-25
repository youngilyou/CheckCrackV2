"""Stitch every image in a folder into one facade mosaic — no flags to remember.

Usage:
    python tools/stitch_folder.py <images_folder> [facade_name]

Wraps src.pipeline.runner.run_facade_poc with sensible defaults:
  - facade_name defaults to the folder's own name
  - output goes to facades/<facade_name>/output/
  - uses config/pipeline.yaml (production thresholds — NOT the relaxed
    pipeline.dev_test.yaml used earlier for the non-planar orbit test set)
  - COLMAP fallback (#12) runs automatically if drift is detected. Its
    rectified output needs a real facade plane, and this script has no
    building footprint to take one from (#4.2) — but it still produces a
    _colmap-corrected mosaic by fitting the plane straight from COLMAP's own
    triangulated points instead (src.geometry.rectification.
    facade_plane_from_reconstruction), just with no operator-supplied
    footprint to validate the fit against.

This covers CLAUDE.local.md #3.1's basic case: one folder of DJI photos of
one wall, no building footprint needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

# When launched with stdout/stderr redirected to a pipe (e.g. the Viewer's
# "▶ 실행" button spawning this as a subprocess), Python falls back to the
# Windows console's codepage (cp949 on a Korean system) instead of UTF-8 —
# any em dash or other non-cp949 character in a print() then crashes with
# UnicodeEncodeError, masking whatever the actual message was.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline.runner import run_facade_poc  # noqa: E402


def main() -> None:
    argv = sys.argv[1:]
    in_place = "--in-place" in argv

    output_dir_arg: str | None = None
    if "--output-dir" in argv:
        idx = argv.index("--output-dir")
        if idx + 1 >= len(argv):
            print("--output-dir requires a path argument")
            sys.exit(1)
        output_dir_arg = argv[idx + 1]
        argv = argv[:idx] + argv[idx + 2 :]

    args = [a for a in argv if a != "--in-place"]

    if len(args) < 1:
        print(
            "usage: python tools/stitch_folder.py <images_folder> [facade_name] "
            "[--in-place] [--output-dir PATH]"
        )
        sys.exit(1)

    images_dir = Path(args[0])
    if not images_dir.is_dir():
        print(f"not a folder: {images_dir}")
        sys.exit(1)

    facade_name = args[1] if len(args) > 1 else images_dir.name

    # --output-dir wins over --in-place when both are given — the Viewer now
    # always passes an explicit version subfolder it allocated itself (see
    # FacadeVersionStore.AllocateNextVersionDir on the C# side) so re-running
    # never overwrites a prior successful run's output.
    if output_dir_arg is not None:
        output_dir = Path(output_dir_arg)
    elif in_place:
        # --in-place: write straight to <images_dir>/output/ instead of the
        # usual facades/<name>/output/ (#28) — for someone who just wants the
        # result sitting right next to the photos they picked, not
        # centralized in the repo.
        output_dir = images_dir / "output"
    else:
        output_dir = None

    print(f"stitching '{images_dir}' as facade '{facade_name}' ...")
    out = run_facade_poc(
        facade_id=facade_name,
        images_dir=images_dir,
        output_root="facades",
        config_path="config/pipeline.yaml",
        output_dir=output_dir,
    )
    if out is None:
        print("failed: no image pair passed the geometry quality gate — check logs/pipeline.log")
        sys.exit(1)

    print(f"done: {out}")
    print(f"  - {facade_name}_analysis.tif / {facade_name}_visual.tif")
    print(f"  - {facade_name}_quality_report.json (check needs_colmap_fallback)")
    print(f"  - if COLMAP ran: {facade_name}_colmap_report.json")


if __name__ == "__main__":
    main()
