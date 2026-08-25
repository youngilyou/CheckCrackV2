"""Generate a PDF inspection report (src/report/pdf_report.py, HTML/CSS ->
WeasyPrint) from an already-stitched (and optionally crack-detected) facade's
output/ folder.

Usage:
    python tools/generate_report.py facade <output_dir> <facade_id> [--building-id ID]
    python tools/generate_report.py building <manifest.json> <reports_dir>

`building` mode's manifest.json:
    {
      "complex_name": "...",
      "building_name": "..." | null,
      "facades": [{"facade_id": "...", "side": "...", "output_dir": "..."}, ...]
    }
The caller (CheckCrackViewer) resolves each facade's CURRENT version dir
before writing the manifest -- this script only ever reads a version already
picked for it, same division of responsibility as tools/stitch_folder.py /
tools/detect_cracks_folder.py already have with the C# side.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.report.pdf_report import generate_building_report, generate_facade_report  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    p_facade = sub.add_parser("facade")
    p_facade.add_argument("output_dir")
    p_facade.add_argument("facade_id")
    p_facade.add_argument("--building-id", default="B000")

    p_building = sub.add_parser("building")
    p_building.add_argument("manifest_path")
    p_building.add_argument("reports_dir")

    args = parser.parse_args()

    if args.mode == "facade":
        output_dir = Path(args.output_dir)
        if not output_dir.is_dir():
            print(f"not a folder: {output_dir}")
            sys.exit(1)
        report_path = generate_facade_report(output_dir, args.facade_id, args.building_id)
    else:
        report_path = generate_building_report(args.manifest_path, args.reports_dir)

    print(f"report written: {report_path}")


if __name__ == "__main__":
    main()
