"""First real (non-synthetic-placeholder) baseline for the "known structural
confounder" slice of CLAUDE.local.md's AI-accuracy question (2026-09-15 ask:
"TTA가 요구하는 90% 정확도에 대응하는 실측치는 현재 없으니 지금 시험기준으로
먼저 만들어 놓으세요").

Converts training_data_structural_fp_v2/*.json (358 operator-confirmed
NOT-a-crack regions -- window frames/panel joints/building corners the v1
model false-positived on, across 66 real BACK-facade photos) into
src/crack/validation.py's GT schema (non_cracks only -- there is no reviewed
POSITIVE crack ground truth yet, so `cracks` stays empty) and writes one
{image_stem}.json per source file into --out-dir, ready for
tools/validate_crack_detection.py --gt-dir.

IMPORTANT SCOPE LIMIT (read before trusting any number this produces):
  1. This measures ONLY false-positive avoidance on KNOWN confounders --
     never recall/precision on true positive cracks, since no positive GT
     exists for real facade photos yet (that's still gated on Phase B).
     "TP"/"FN" from the harness will always be 0/0 here (no GT cracks to
     match) -- the only real signal is CONFOUNDER_FP vs plain FP counts,
     print them directly rather than trusting aggregate()'s recall/precision
     (those come back None/0.0 here as an artifact of no positive GT, not a
     real 0% score).
  2. These exact 358 regions were used to FINE-TUNE the v2 model
     (training/crack_seg/finetune_hard_negatives_full_positive.py calls
     build_dataset_from_annotations on this same training_data_structural_fp_v2/
     directory, with no train/val split recorded for it). Evaluating v2
     against this data is testing on its own training set -- NOT a valid
     held-out measurement, and this tool must never be used to report a v2
     number as "validated". v1 (which never saw this data) is the only
     legitimate model to test here; it establishes the "before" baseline
     this problem actually has at, and a real measurement of v2 needs either
     freshly-annotated confounders v2 has never trained on, or a proper
     held-out split carved out before any future fine-tune.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def convert(src_dir: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for src in sorted(src_dir.glob("*.json")):
        data = json.loads(src.read_text(encoding="utf-8"))
        image_path = data.get("image_path")
        if not image_path or not Path(image_path).exists():
            print(f"skip {src.name}: image_path missing or not found ({image_path})")
            continue
        regions = data.get("background_regions", [])
        if not regions:
            continue
        non_cracks = []
        for r in regions:
            points = r.get("points", [])
            if len(points) < 3:
                continue
            polygon_px = [[p["x"], p["y"]] for p in points]
            non_cracks.append({
                "nc_id": f"{src.stem}_{r.get('region_id')}",
                "polygon_px": polygon_px,
                "confounder_type": "structural_fp",  # window frame / panel joint / building corner (unlabeled sub-type)
            })
        gt = {"image_path": image_path, "cracks": [], "non_cracks": non_cracks}
        out_path = out_dir / f"{src.stem}.json"
        out_path.write_text(json.dumps(gt, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src-dir", default="training_data_structural_fp_v2")
    parser.add_argument("--out-dir", default="training_data_structural_fp_v2_gt")
    args = parser.parse_args()

    n = convert(Path(args.src_dir), Path(args.out_dir))
    print(f"wrote {n} GT files to {args.out_dir} (non_cracks only -- see module docstring for scope limits)")
    print(f"run: python tools/validate_crack_detection.py --gt-dir {args.out_dir} "
          f"--model training/crack_seg/models/v1_all_cracks/best.pt")
    print("do NOT run --model v2 against this same GT dir and report it as validated (see docstring: data leakage)")


if __name__ == "__main__":
    main()
