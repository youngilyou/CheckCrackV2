"""SUPERSEDED (2026-09-13) -- this is "attempt 2" (400 sampled positives, kept
for the record). It ALSO still regressed real-crack performance (box mAP50
0.639, mask mAP50 0.482 vs baseline 0.692/0.521) -- EarlyStopping picked
epoch 1 as best, meaning 854 images was too thin a slice to re-anchor the
model. See finetune_hard_negatives_full_positive.py ("attempt 3", ALL 4916
crack512 positives, lower lr) for the version that actually beat baseline
(box mAP50 0.704, mask mAP50 0.540) -- that is the one whose output is
registered as crack.model_v2 in config/pipeline.yaml. Do not re-run this
script expecting a usable model; it is here only so the attempt-2 numbers
above are reproducible/inspectable.

Re-run of the 2026-09-13 raw_crops hard-negative fine-tune, this time MIXED
with real positive crack examples -- the first attempt (background-only, 358
crops) measurably hurt real-crack recall/precision on the CUBIT-Seg crack512
validation set (box mAP50 0.692->0.612, mask mAP50 0.521->0.450), classic
catastrophic forgetting from an all-negative fine-tune with no positive
gradient signal to counterbalance it.

This script:
  1. Rebuilds training/crack_seg/dataset_raw_crops/ from
     training_data_raw_crops/*.json (build_dataset_from_annotations, unchanged
     -- the 358 confirmed-FP background_regions + the 2 pre-existing real
     crack regions).
  2. Mixes in a random sample of real positive crack512 TRAIN images+labels
     (sibling CheckCrack repo's training/crack_seg/dataset/, 4916 train
     images) -- copied in ADDITION to (never replacing) what step 1 wrote.
     Deliberately samples ONLY from crack512's train split, never its val
     split (the 546-image held-out set used for the before/after recall
     comparison) -- copying from val would leak evaluation data into training
     and invalidate that comparison.
  3. Fine-tunes from the SAME seed checkpoint as the failed attempt
     (raw_crops_seed, itself a copy of the currently-deployed baseline2
     checkpoint) -- so this run is comparable apples-to-apples.

Real crack512 val set stays completely untouched by this script -- run
evaluate against it separately afterwards, same as the background-only
attempt's comparison.
"""
from __future__ import annotations

import random
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from training.crack_seg.train_from_viewer import (  # noqa: E402
    RUNS_DIR, SOURCES, build_dataset_from_annotations,
)

# Explicitly the SEED checkpoint (a copy of the currently-deployed, undamaged
# baseline2 weights) -- NOT find_latest_checkpoint("raw_crops"), which would
# now resolve to the FAILED background-only fine-tune run (more recent mtime
# than the seed) and compound its regression instead of starting fresh from
# the known-good baseline.
SEED_CHECKPOINT = RUNS_DIR / "raw_crops_seed" / "weights" / "best.pt"

CRACK512_ROOT = Path(r"D:\ClaudePr\CheckCrack\training\crack_seg\dataset")
N_TRAIN_POSITIVES = 400
N_VAL_POSITIVES = 80  # dataset_raw_crops' OWN internal val split (ultralytics live monitoring only)
SEED = 0
EPOCHS = 20


def copy_positive_sample(split_name: str, count: int, dataset_dir: Path, used: set[str]) -> int:
    images_dir = CRACK512_ROOT / "images" / "train"  # always sample from crack512's TRAIN pool
    labels_dir = CRACK512_ROOT / "labels" / "train"
    all_stems = sorted(p.stem for p in images_dir.glob("*.jpg") if p.stem not in used)
    random.Random(SEED + hash(split_name) % 1000).shuffle(all_stems)
    chosen = all_stems[:count]
    used.update(chosen)

    dst_img_dir = dataset_dir / "images" / split_name
    dst_lbl_dir = dataset_dir / "labels" / split_name
    written = 0
    for stem in chosen:
        img_src = images_dir / f"{stem}.jpg"
        lbl_src = labels_dir / f"{stem}.txt"
        if not img_src.exists() or not lbl_src.exists():
            continue
        shutil.copyfile(img_src, dst_img_dir / f"crack512_{stem}.jpg")
        shutil.copyfile(lbl_src, dst_lbl_dir / f"crack512_{stem}.txt")
        written += 1
    return written


def main() -> None:
    dataset_dir = SOURCES["structural_fp_v2"]["dataset_dir"]
    training_data_dir = SOURCES["structural_fp_v2"]["training_data_dir"]

    print("rebuilding dataset_raw_crops/ from training_data_raw_crops/*.json (hard negatives)...")
    summary = build_dataset_from_annotations(training_data_dir, dataset_dir)
    print(f"  background/existing-annotation samples: {summary}")

    used: set[str] = set()
    n_train = copy_positive_sample("train", N_TRAIN_POSITIVES, dataset_dir, used)
    n_val = copy_positive_sample("val", N_VAL_POSITIVES, dataset_dir, used)
    print(f"mixed in {n_train} real crack512 positives into train, {n_val} into val "
          f"(sampled only from crack512's own TRAIN split -- its val split was never touched)")

    checkpoint = SEED_CHECKPOINT
    if not checkpoint.exists():
        raise SystemExit(f"seed checkpoint not found: {checkpoint} -- run the seeding step first")
    print(f"fine-tuning from {checkpoint} (explicit seed, not find_latest_checkpoint)")

    from ultralytics import YOLO

    model = YOLO(str(checkpoint))
    run_name = f"raw_crops_finetune_mixed_{time.strftime('%Y%m%d_%H%M%S')}"
    model.train(
        data=str(dataset_dir / "dataset.yaml"),
        epochs=EPOCHS,
        imgsz=512,
        batch=32,
        project=str(RUNS_DIR),
        name=run_name,
        patience=10,
        device=0,
        workers=0,
    )
    print(f"done: {RUNS_DIR / run_name}")


if __name__ == "__main__":
    main()
