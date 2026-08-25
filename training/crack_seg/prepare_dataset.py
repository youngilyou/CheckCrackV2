"""Convert CUBIT-Seg (binary mask) crack images into YOLO-seg polygon labels.

CLAUDE.local.md #2.7 says not to assume a public crack model is optimized
for this project's walls and to plan finetuning on real data instead; this
prepares a from-scratch baseline on the one real, local dataset available
(datasets/CUBIT-Seg) rather than depending on downloading an external
checkpoint of unknown license/fit.

CUBIT-Seg's crack512 has two independently-annotated batches (V7, labelme)
of 512x512 RGB/binary-mask pairs — both folded into one "crack" class here.
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

import cv2
import numpy as np

DATASET_ROOT = Path("datasets/CUBIT-Seg/crack512/crack512")
BATCHES = ["V7", "labelme"]
OUT_ROOT = Path("training/crack_seg/dataset")
VAL_FRACTION = 0.1
MIN_CONTOUR_AREA_PX = 8  # drop speck-noise contours from mask antialiasing
SEED = 0


def mask_to_yolo_polygons(mask_path: Path, img_w: int, img_h: int) -> list[str]:
    mask = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)
    _, binary = cv2.threshold(mask, 1, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    lines = []
    for contour in contours:
        if cv2.contourArea(contour) < MIN_CONTOUR_AREA_PX:
            continue
        contour = contour.reshape(-1, 2)
        if len(contour) < 3:
            continue
        coords = []
        for x, y in contour:
            coords.append(f"{x / img_w:.6f}")
            coords.append(f"{y / img_h:.6f}")
        lines.append("0 " + " ".join(coords))
    return lines


def main() -> None:
    pairs: list[tuple[Path, Path]] = []
    for batch in BATCHES:
        rgb_dir = DATASET_ROOT / batch / "outputs_RGB"
        mask_dir = DATASET_ROOT / batch / "outputs_Mask"
        for rgb_path in sorted(rgb_dir.glob("*.jpg")):
            mask_path = mask_dir / f"{rgb_path.stem}.png"
            if mask_path.exists():
                pairs.append((rgb_path, mask_path))
    print(f"found {len(pairs)} image/mask pairs across {BATCHES}")

    random.Random(SEED).shuffle(pairs)
    n_val = max(1, int(len(pairs) * VAL_FRACTION))
    splits = {"val": pairs[:n_val], "train": pairs[n_val:]}

    for split, split_pairs in splits.items():
        img_dir = OUT_ROOT / "images" / split
        lbl_dir = OUT_ROOT / "labels" / split
        img_dir.mkdir(parents=True, exist_ok=True)
        lbl_dir.mkdir(parents=True, exist_ok=True)

        empty_count = 0
        for rgb_path, mask_path in split_pairs:
            img = cv2.imread(str(rgb_path))
            h, w = img.shape[:2]
            lines = mask_to_yolo_polygons(mask_path, w, h)
            if not lines:
                empty_count += 1

            dest_name = f"{rgb_path.parent.parent.name}_{rgb_path.stem}"
            shutil.copyfile(rgb_path, img_dir / f"{dest_name}.jpg")
            (lbl_dir / f"{dest_name}.txt").write_text("\n".join(lines), encoding="utf-8")

        print(f"{split}: {len(split_pairs)} images, {empty_count} with no polygon (background-only tile)")

    yaml_content = f"""path: {OUT_ROOT.resolve().as_posix()}
train: images/train
val: images/val
names:
  0: crack
"""
    (OUT_ROOT / "dataset.yaml").write_text(yaml_content, encoding="utf-8")
    print(f"wrote {OUT_ROOT / 'dataset.yaml'}")


if __name__ == "__main__":
    main()
