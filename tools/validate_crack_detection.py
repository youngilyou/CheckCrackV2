"""0.3mm validation protocol harness (SmartCrack V2 design section 17,
2026-08-26). See src/crack/validation.py's module docstring first -- this
tool has ZERO real Ground Truth behind it yet; it exists so the real
validation can start the moment real annotated data does (Phase B).

Two modes:

  --self-test
      No images, no model, no GT files -- synthetic Crack/GT objects built
      directly in this script, run straight through
      src/crack/validation.py's matching+aggregation logic. This proves the
      HARNESS'S OWN CODE is correct (IoU matching, TP/FN/FP/CONFOUNDER_FP
      classification, width-error math, group-by aggregation) -- it is
      NOT a measurement of real detector performance and must never be
      quoted as one. Run this any time the matching/metric code changes.

  --gt-dir <folder>
      Real mode: one {image_stem}.json GT file (see validation.py's schema)
      next to each real photo in the folder, runs the actual detector
      (via crack/shadow_recovery.py's detect_cracks_with_shadow_recovery)
      on each photo, matches against that photo's GT, aggregates by
      --group-by, prints a report table. Does nothing useful until real GT
      files exist (Phase B, gated on the drone + reference-crack
      acquisition already in progress separately).

Usage:
    python tools/validate_crack_detection.py --self-test
    python tools/validate_crack_detection.py --gt-dir <folder> --model PATH [--group-by width_bin]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.common.config import load_config  # noqa: E402
from src.common.imageio import imread_unicode  # noqa: E402
from src.crack.validation import (  # noqa: E402
    GtCrack,
    GtNonCrack,
    aggregate,
    match_detections,
)


@dataclass
class _FakeCrack:
    """Minimal duck-typed stand-in for src.common.types.Crack -- the
    self-test only needs polygon_px/max_width_px/area_px/crack_id, not a
    real Crack's full field set."""

    crack_id: str
    polygon_px: np.ndarray
    max_width_px: float
    area_px: float


def _rect(x0, y0, x1, y1) -> np.ndarray:
    return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float64)


def run_self_test() -> None:
    print("=== SELF-TEST: harness logic only, NOT a real performance measurement ===")
    print()

    gt_cracks = [
        GtCrack(gt_id="GT001", polygon_px=_rect(0, 0, 100, 10), width_px=10.0, width_bin="0.3-0.5",
                lighting_category="bright"),
        GtCrack(gt_id="GT002", polygon_px=_rect(200, 200, 260, 206), width_px=6.0, width_bin="0.2-0.3",
                lighting_category="shadow"),
        GtCrack(gt_id="GT003", polygon_px=_rect(400, 400, 500, 404), width_px=4.0, width_bin="0.2-0.3",
                lighting_category="shadow"),  # deliberately left undetected below -> expect FN
    ]
    gt_non_cracks = [
        GtNonCrack(nc_id="NC001", polygon_px=_rect(600, 600, 700, 610), confounder_type="joint"),
    ]
    detected = [
        # Matches GT001 well (high IoU) -- width off by 2px.
        _FakeCrack(crack_id="C0", polygon_px=_rect(2, 1, 98, 11), max_width_px=12.0, area_px=960.0),
        # Matches GT002 well -- width off by 1px.
        _FakeCrack(crack_id="C1", polygon_px=_rect(201, 199, 259, 207), max_width_px=5.0, area_px=348.0),
        # GT003 gets NO matching detection -> should register as FN.
        # A detection with no GT match at all -> plain FP.
        _FakeCrack(crack_id="C2", polygon_px=_rect(1000, 1000, 1050, 1010), max_width_px=8.0, area_px=500.0),
        # A detection landing on the labeled joint (NC001) -> CONFOUNDER_FP.
        _FakeCrack(crack_id="C3", polygon_px=_rect(605, 601, 695, 609), max_width_px=7.0, area_px=720.0),
    ]

    results = match_detections(gt_cracks, gt_non_cracks, detected)

    kinds = sorted(r.kind for r in results)
    expected = sorted(["TP", "TP", "FN", "FP", "CONFOUNDER_FP"])
    print("result kinds:", kinds)
    assert kinds == expected, f"expected {expected}, got {kinds}"
    print("PASS: TP/FN/FP/CONFOUNDER_FP classification correct")
    print()

    by_lighting = aggregate(results, group_by="lighting_category")
    for key, m in by_lighting.items():
        print(f"  [{key}] tp={m.tp} fn={m.fn} fp={m.fp} confounder_fp={m.confounder_fp} "
              f"recall={m.recall} precision={m.precision} width_mae_px={m.width_mae_px}")

    shadow = by_lighting.get("shadow")
    assert shadow is not None and shadow.tp == 1 and shadow.fn == 1, "shadow-group TP/FN counts wrong"
    assert shadow.width_mae_px == 1.0, f"expected width MAE 1.0px for the shadow group, got {shadow.width_mae_px}"
    print("PASS: group-by aggregation and width MAE correct")
    print()
    print("=== SELF-TEST PASSED (harness logic verified; still no real validation data) ===")


def run_real_validation(gt_dir: Path, model_path: str, config_path: str, group_by: str | None, device: str) -> None:
    from src.crack.detector import CrackDetector
    from src.crack.shadow_recovery import detect_cracks_with_shadow_recovery

    cfg = load_config(config_path)
    detector = CrackDetector(model_path, cfg, device=None if device == "cpu" else device)

    gt_files = sorted(gt_dir.glob("*.json"))
    if not gt_files:
        print(f"no GT .json files found in {gt_dir} -- nothing to validate yet (Phase B item, see module docstring)")
        return

    all_results = []
    for gt_file in gt_files:
        gt_data = json.loads(gt_file.read_text(encoding="utf-8"))
        image = imread_unicode(Path(gt_data["image_path"]), cv2.IMREAD_COLOR)
        if image is None:
            print(f"  skip {gt_file.name}: could not read {gt_data['image_path']}")
            continue

        gt_cracks = [GtCrack(gt_id=c["gt_id"], polygon_px=np.array(c["polygon_px"]),
                              width_mm=c.get("width_mm"), width_px=c.get("width_px"),
                              width_bin=c.get("width_bin"), distance_category=c.get("distance_category"),
                              lighting_category=c.get("lighting_category"), wall_material=c.get("wall_material"))
                     for c in gt_data.get("cracks", [])]
        gt_non_cracks = [GtNonCrack(nc_id=nc["nc_id"], polygon_px=np.array(nc["polygon_px"]),
                                     confounder_type=nc.get("confounder_type"))
                         for nc in gt_data.get("non_cracks", [])]

        detected = detect_cracks_with_shadow_recovery(gt_file.stem, image, cfg, detector, illumination_device=device)
        results = match_detections(gt_cracks, gt_non_cracks, detected)
        all_results.extend(results)
        print(f"  {gt_file.name}: {len(gt_cracks)} GT crack(s), {len(detected)} detection(s), "
              f"{sum(1 for r in results if r.kind == 'TP')} matched")

    print()
    groups = aggregate(all_results, group_by=group_by)
    for key, m in groups.items():
        print(f"[{key}] recall={m.recall} precision={m.precision} "
              f"tp={m.tp} fn={m.fn} fp={m.fp} confounder_fp={m.confounder_fp} width_mae_px={m.width_mae_px}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--gt-dir", type=Path, default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--config", default="config/pipeline.yaml")
    parser.add_argument("--group-by", default=None,
                         choices=["width_bin", "distance_category", "lighting_category", "wall_material"])
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        return

    if args.gt_dir is None:
        print("nothing to do -- pass --self-test or --gt-dir <folder>")
        sys.exit(1)

    cfg = load_config(args.config)
    model_path = args.model or str(cfg.crack.model)
    run_real_validation(args.gt_dir, model_path, args.config, args.group_by, args.device)


if __name__ == "__main__":
    main()
