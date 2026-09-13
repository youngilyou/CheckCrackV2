"""Turn a manually-reviewed set of confirmed-false-positive crack detections
(e.g. from a raw-photo-pipeline run and a subsequent visual audit) into
training/crack_seg/train_from_viewer.py's "raw_crops" background_regions JSON
format -- see that script's iter_background_regions()/build_dataset_from_
annotations() (2026-09-13) for how these get turned into empty-label YOLO-seg
training crops.

Background: {facade_id}_cracks.json already carries, per crack,
source_observations[] with each raw source photo's own bbox_px_in_source/
polygon_px_in_source (src/crack/raw_pipeline.py). A manual review pass over
all detections (contact-sheet montages, one crack per cell) produces a
review-labels JSON classifying each crack_id as "ambiguous" (could be a real
hairline crack OR a vertical panel control joint -- deliberately NOT used as
a hard negative, to avoid teaching the model to ignore an actual crack),
"background_noise" (landed off the building entirely -- also not a useful
architecture-specific hard negative), or (everything else, the default)
confirmed structural false positive -- ONLY this last category is written out
here. This script only reads each crack's FIRST source_observation (index 0)
-- the exact crop a human actually looked at when classifying it -- rather
than every source photo the crack happened to also appear in, so the training
crop matches what was actually visually confirmed as a false positive.

Usage:
    python tools/build_hard_negatives_from_review.py \\
        <facade_id>_cracks.json <review_labels.json> <facade_id>_source_images.json \\
        [--training-data-dir training_data_structural_fp_v2]

review_labels.json shape (see the manual-review montage tooling):
    {"ambiguous_ids": [<int suffix>, ...], "background_noise_ids": [<int suffix>, ...]}
crack_id "BACK_C000060" -> suffix 60, matched against those two int lists.

Writes one {image_id}.json per raw source photo that has at least one
confirmed-FP region, under --training-data-dir (default matches
train_from_viewer.py's SOURCES["structural_fp_v2"]["training_data_dir"]).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def crack_id_suffix(crack_id: str) -> int | None:
    m = re.search(r"_C(\d+)$", crack_id)
    return int(m.group(1)) if m else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cracks_json", type=Path)
    parser.add_argument("review_labels_json", type=Path)
    parser.add_argument("source_images_json", type=Path)
    parser.add_argument("--training-data-dir", type=Path, default=Path("training_data_structural_fp_v2"))
    args = parser.parse_args()

    cracks = json.loads(args.cracks_json.read_text(encoding="utf-8"))
    review = json.loads(args.review_labels_json.read_text(encoding="utf-8"))
    source_images = json.loads(args.source_images_json.read_text(encoding="utf-8"))
    image_paths = {entry["image_id"]: entry["file_path"] for entry in source_images}

    excluded = set(review.get("ambiguous_ids", [])) | set(review.get("background_noise_ids", []))

    # image_id -> list of (crack_id, polygon points) confirmed-FP regions,
    # using each crack's first source_observation only (see module docstring).
    regions_by_image: dict[str, list[tuple[str, list[dict]]]] = {}
    skipped_no_obs = 0
    skipped_no_path = 0
    for crack in cracks:
        suffix = crack_id_suffix(crack["crack_id"])
        if suffix is None or suffix in excluded:
            continue
        obs = crack.get("source_observations") or []
        if not obs:
            skipped_no_obs += 1
            continue
        primary = obs[0]
        image_id = primary["image_id"]
        if image_id not in image_paths:
            skipped_no_path += 1
            continue
        polygon = primary.get("polygon_px_in_source") or []
        if len(polygon) < 3:
            continue
        points = [{"x": round(p[0]), "y": round(p[1])} for p in polygon]
        regions_by_image.setdefault(image_id, []).append((crack["crack_id"], points))

    args.training_data_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    total_regions = 0
    for image_id, entries in regions_by_image.items():
        payload = {
            "facade_id": image_id,
            "image_path": image_paths[image_id],
            "background_regions": [
                {"region_id": crack_id_suffix(crack_id), "points": points, "source_crack_id": crack_id}
                for crack_id, points in entries
            ],
        }
        out_path = args.training_data_dir / f"{image_id}_hardneg.json"
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1
        total_regions += len(entries)

    print(f"wrote {written} annotation file(s), {total_regions} background region(s), "
          f"into {args.training_data_dir}")
    print(f"excluded (ambiguous/background_noise): {len(excluded)}")
    if skipped_no_obs:
        print(f"skipped (no source_observations): {skipped_no_obs}")
    if skipped_no_path:
        print(f"skipped (source image_id not found in source_images.json): {skipped_no_path}")


if __name__ == "__main__":
    main()
