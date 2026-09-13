"""Third attempt at the 2026-09-13 raw_crops hard-negative fine-tune.

Attempt 1 (358 background-only crops): regressed real-crack performance hard
(box mAP50 0.692->0.612, mask mAP50 0.521->0.450) -- catastrophic forgetting,
no positive signal at all.

Attempt 2 (358 negatives + 400 randomly-sampled crack512 positives, low
epochs): still regressed (box mAP50 0.639, mask mAP50 0.482) and, tellingly,
EarlyStopping picked epoch 1 as best -- every epoch after that made held-out
real-crack performance WORSE, meaning the small (854-image) mixed dataset was
too thin/narrow a slice of what the original baseline was trained on to
actually re-anchor the model; continued training just drifted away from a
well-optimized baseline with too little diverse signal to correct that.

Attempt 3 (this script): use ALL 4916 crack512 TRAIN positives (not a 400
sample) mixed with the same 358 confirmed-FP hard negatives, explicit lower
learning rate (AdamW, lr0=5e-4 vs the auto-selected 2e-3 both prior attempts
used) and wider patience, so the model has much more of its original positive
distribution to hold onto while learning the negative-region correction.
crack512's own 546-image VAL split is still never touched (same held-out
comparison set used for all three attempts).
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from training.crack_seg.train_from_viewer import (  # noqa: E402
    RUNS_DIR, SOURCES, build_dataset_from_annotations,
)

CRACK512_ROOT = Path(r"D:\ClaudePr\CheckCrack\training\crack_seg\dataset")
N_VAL_POSITIVES = 200  # held out from crack512's TRAIN pool for THIS run's own internal val monitoring
EPOCHS = 30
LR0 = 5e-4
SEED_CHECKPOINT = RUNS_DIR / "raw_crops_seed" / "weights" / "best.pt"


def copy_all_positive_train(dataset_dir: Path) -> tuple[int, int]:
    images_dir = CRACK512_ROOT / "images" / "train"
    labels_dir = CRACK512_ROOT / "labels" / "train"
    all_stems = sorted(p.stem for p in images_dir.glob("*.jpg"))

    # Deterministic split: every Nth stem (by sorted order) goes to this run's
    # internal val, the rest to train -- simpler and just as unbiased as a
    # random shuffle for a set this large, and avoids importing `random` only
    # to reseed it once.
    val_stride = max(1, len(all_stems) // N_VAL_POSITIVES)
    val_stems = set(all_stems[::val_stride][:N_VAL_POSITIVES])

    counts = {"train": 0, "val": 0}
    for stem in all_stems:
        img_src = images_dir / f"{stem}.jpg"
        lbl_src = labels_dir / f"{stem}.txt"
        if not img_src.exists() or not lbl_src.exists():
            continue
        split = "val" if stem in val_stems else "train"
        shutil.copyfile(img_src, dataset_dir / "images" / split / f"crack512_{stem}.jpg")
        shutil.copyfile(lbl_src, dataset_dir / "labels" / split / f"crack512_{stem}.txt")
        counts[split] += 1
    return counts["train"], counts["val"]


def main() -> None:
    dataset_dir = SOURCES["structural_fp_v2"]["dataset_dir"]
    training_data_dir = SOURCES["structural_fp_v2"]["training_data_dir"]

    print(f"rebuilding {dataset_dir}/ from {training_data_dir}/*.json (hard negatives)...")
    summary = build_dataset_from_annotations(training_data_dir, dataset_dir)
    print(f"  background/existing-annotation samples: {summary}")

    n_train, n_val = copy_all_positive_train(dataset_dir)
    print(f"mixed in {n_train} real crack512 positives into train, {n_val} into val "
          f"(all sampled from crack512's own TRAIN split -- its 546-image val split was never touched)")

    if not SEED_CHECKPOINT.exists():
        raise SystemExit(f"seed checkpoint not found: {SEED_CHECKPOINT} -- run the seeding step first")
    print(f"fine-tuning from {SEED_CHECKPOINT} (explicit seed, not find_latest_checkpoint)")

    from ultralytics import YOLO

    model = YOLO(str(SEED_CHECKPOINT))
    run_name = f"structural_fp_v2_finetune_{time.strftime('%Y%m%d_%H%M%S')}"
    model.train(
        data=str(dataset_dir / "dataset.yaml"),
        epochs=EPOCHS,
        imgsz=512,
        batch=32,
        project=str(RUNS_DIR),
        name=run_name,
        patience=15,
        device=0,
        workers=0,
        optimizer="AdamW",
        lr0=LR0,
    )
    print(f"done: {RUNS_DIR / run_name}")


if __name__ == "__main__":
    main()
