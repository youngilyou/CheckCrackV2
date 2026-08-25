"""Convert a user-selected image/same-stem-mask folder into YOLO-seg polygon
labels -- the "기존 마스크 데이터셋" training source.

Deliberately a separate dataset dir from prepare_dataset.py's crack512
output (training/crack_seg/dataset/, train.py's untouched baseline) and from
the viewer's box-annotated sources (dataset_raw_crops/, dataset_stitched/) --
CLAUDE.local.md's "1 Facade = 1 Flight" spirit of never silently merging
independent sources applies here too: keeping each training source's data
and resulting model fully separate makes it possible to tell which source is
responsible for a given result while debugging.

Unlike crack512 (two batches under <root>/<batch>/outputs_RGB|outputs_Mask),
crack_org/crack_org is flat: <stem>.jpg + <stem>.png (mask) side by side.
"""

from __future__ import annotations

import random
import shutil
import sys
import argparse
from pathlib import Path

import cv2

# training/ has no __init__.py (these are standalone scripts, not a package)
# -- add the repo root to sys.path so the sibling prepare_dataset.py module
# can be imported regardless of this script's own working directory, same
# pattern as tools/stitch_for_ai_training.py uses for src.*.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from training.crack_seg.prepare_dataset import mask_to_yolo_polygons  # noqa: E402

OUT_ROOT = Path("training/crack_seg/dataset_labeled")
VAL_FRACTION = 0.1
SEED = 0


def main(input_dir: str | Path | None = None) -> None:
    if input_dir is None:
        raise SystemExit("--input is required for the existing-mask dataset source")
    dataset_root = Path(input_dir)
    if not dataset_root.is_dir():
        raise SystemExit(f"input folder does not exist: {dataset_root}")

    pairs: list[tuple[Path, Path]] = []
    image_paths: list[Path] = []
    for pattern in ("*.jpg", "*.jpeg", "*.tif", "*.tiff", "*.bmp"):
        image_paths.extend(dataset_root.glob(pattern))

    missing_masks: list[Path] = []
    for jpg_path in sorted(image_paths):
        mask_path = jpg_path.with_suffix(".png")
        if mask_path.exists():
            pairs.append((jpg_path, mask_path))
        else:
            missing_masks.append(jpg_path)

    if not image_paths:
        raise SystemExit(f"no supported images found in {dataset_root}")
    if missing_masks:
        preview = "\n".join(p.name for p in missing_masks[:10])
        raise SystemExit(f"{len(missing_masks)} image(s) are missing same-stem .png masks:\n{preview}")

    print(f"found {len(pairs)} image/mask pairs in {dataset_root}")

    random.Random(SEED).shuffle(pairs)
    n_val = max(1, int(len(pairs) * VAL_FRACTION))
    splits = {"val": pairs[:n_val], "train": pairs[n_val:]}

    for split, split_pairs in splits.items():
        img_dir = OUT_ROOT / "images" / split
        lbl_dir = OUT_ROOT / "labels" / split
        img_dir.mkdir(parents=True, exist_ok=True)
        lbl_dir.mkdir(parents=True, exist_ok=True)

        empty_count = 0
        for jpg_path, mask_path in split_pairs:
            img = cv2.imread(str(jpg_path))
            h, w = img.shape[:2]
            lines = mask_to_yolo_polygons(mask_path, w, h)
            if not lines:
                empty_count += 1

            shutil.copyfile(jpg_path, img_dir / jpg_path.name)
            (lbl_dir / f"{jpg_path.stem}.txt").write_text("\n".join(lines), encoding="utf-8")

        print(f"{split}: {len(split_pairs)} images, {empty_count} with no polygon (background-only)")

    yaml_content = f"""path: {OUT_ROOT.resolve().as_posix()}
train: images/train
val: images/val
names:
  0: crack
"""
    (OUT_ROOT / "dataset.yaml").write_text(yaml_content, encoding="utf-8")
    print(f"wrote {OUT_ROOT / 'dataset.yaml'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True, help="folder containing images and same-stem .png masks")
    args = parser.parse_args()
    main(args.input)
