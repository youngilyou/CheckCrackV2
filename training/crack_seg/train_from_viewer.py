"""CheckCrackViewer's "AI 학습" tab pipeline call -- builds YOLO-seg training samples
for whichever training source the viewer selected and runs Ultralytics training.

Usage:
    python training/crack_seg/train_from_viewer.py --source labeled --mode new
    python training/crack_seg/train_from_viewer.py --source raw_crops --mode finetune
    python training/crack_seg/train_from_viewer.py --source stitched --mode new

Three fully independent training sources (see CLAUDE.local.md-style "never
silently merge independent sources" spirit -- keeping each source's data and
resulting model separate makes it possible to tell which source produced a
given result while debugging):
  - "labeled":    datasets/CUBIT-Seg/crack_org (already has ground-truth
                  masks) -> prepare_labeled_dataset.py -> dataset_labeled/.
                  No viewer polygon annotation involved at all.
  - "raw_crops":  a folder of plain crack photos with no labels (e.g.
                  datasets/CUBIT-Seg/CrackTextRGB) -> regions drawn in the
                  viewer, saved to training_data_raw_crops/*.json ->
                  dataset_raw_crops/.
  - "stitched":   the existing facade-mosaic flow -> regions drawn in the
                  viewer, saved to training_data_stitched/*.json ->
                  dataset_stitched/.

Deliberately a SEPARATE script from train.py, not a modified version of it --
train.py is the manually-run CUBIT-Seg crack512 baseline experiment
(hardcoded params, COCO-pretrained start, its own training/crack_seg/dataset/)
and should keep working exactly as-is; this script is the one CheckCrackViewer
actually invokes, free to grow its own CLI/behavior without disturbing that
baseline run or mixing any of these three sources' data with it or each other.

Region -> crop conversion (raw_crops/stitched only): each annotated polygon
becomes its own small training image, padded around the polygon bounds and
clamped to the source image. The polygon points are re-expressed directly in
the crop's normalized YOLO-seg coordinates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
import traceback
from pathlib import Path

import cv2
import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

TRAINING_LOG_ROOT = ROOT / "logs" / "training_runs"
CROP_PADDING_FRAC = 0.25  # extra context around each region, each side
VAL_FRACTION = 0.2
BASE_CHECKPOINT = "yolov8s-seg.pt"
CRACK_SEG_DIR = Path(__file__).resolve().parent
RUNS_DIR = CRACK_SEG_DIR / "runs"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")

# Per-source config -- "labeled" has no training_data_dir because it never
# goes through the viewer's polygon-annotation step at all.
SOURCES: dict[str, dict] = {
    "labeled": {
        "dataset_dir": CRACK_SEG_DIR / "dataset_labeled",
    },
    "raw_crops": {
        "dataset_dir": CRACK_SEG_DIR / "dataset_raw_crops",
        "training_data_dir": ROOT / "training_data_raw_crops",
    },
    "stitched": {
        "dataset_dir": CRACK_SEG_DIR / "dataset_stitched",
        "training_data_dir": ROOT / "training_data_stitched",
    },
}


_status_path: Path | None = None
_events_path: Path | None = None
_summary_path: Path | None = None


def configure_run_logs(run_name: str) -> Path:
    global _status_path, _events_path, _summary_path
    run_log_dir = TRAINING_LOG_ROOT / run_name
    run_log_dir.mkdir(parents=True, exist_ok=True)
    _status_path = run_log_dir / "status.json"
    _events_path = run_log_dir / "events.jsonl"
    _summary_path = run_log_dir / "summary.json"
    return run_log_dir


def write_status(**fields) -> None:
    if _status_path is None or _events_path is None:
        raise RuntimeError("run logs are not configured")

    payload = {"observed_at": time.time(), **fields}
    encoded = json.dumps(payload, ensure_ascii=False)
    _status_path.write_text(encoded, encoding="utf-8")
    with _events_path.open("a", encoding="utf-8") as f:
        f.write(encoded + "\n")


def write_summary(**fields) -> None:
    if _summary_path is None:
        raise RuntimeError("run logs are not configured")
    _summary_path.write_text(json.dumps(fields, ensure_ascii=False, indent=2), encoding="utf-8")


def write_dataset_yaml(dataset_dir: Path) -> None:
    yaml_content = f"""path: {dataset_dir.resolve().as_posix()}
train: images/train
val: images/val
names:
  0: crack
"""
    (dataset_dir / "dataset.yaml").write_text(yaml_content, encoding="utf-8")


def file_sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def polygon_area_px(points: list[tuple[int, int]]) -> float:
    if len(points) < 3:
        return 0.0
    area = 0.0
    for idx, (x0, y0) in enumerate(points):
        x1, y1 = points[(idx + 1) % len(points)]
        area += x0 * y1 - x1 * y0
    return abs(area) / 2.0


def mask_skeleton(mask: np.ndarray) -> np.ndarray:
    skeleton = np.zeros(mask.shape, dtype=np.uint8)
    work = (mask > 0).astype(np.uint8) * 255
    element = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    while cv2.countNonZero(work) > 0:
        eroded = cv2.erode(work, element)
        opened = cv2.dilate(eroded, element)
        temp = cv2.subtract(work, opened)
        skeleton = cv2.bitwise_or(skeleton, temp)
        work = eroded
    return skeleton


def compute_polygon_measurements(local_points: list[tuple[int, int]], width: int, height: int) -> dict:
    mask = np.zeros((height, width), dtype=np.uint8)
    contour = np.array(local_points, dtype=np.int32).reshape((-1, 1, 2))
    cv2.fillPoly(mask, [contour], 255)
    area_px = int(cv2.countNonZero(mask))
    skeleton = mask_skeleton(mask)
    skeleton_px = int(cv2.countNonZero(skeleton))
    distance = cv2.distanceTransform(mask, cv2.DIST_L2, 5) if area_px else np.zeros_like(mask, dtype=np.float32)
    max_width_px = float(distance.max() * 2.0) if area_px else 0.0
    mean_width_px = float(area_px / skeleton_px) if skeleton_px > 0 else 0.0
    return {
        "measurement_basis": "pixel_only_original_image_coordinates",
        "measurement_warning": (
            "Physical mm/cm conversion is not performed here. It requires preserved original image, "
            "camera/capture metadata, capture distance/pose, and a visible reference scale."
        ),
        "area_px": area_px,
        "polygon_area_px": round(polygon_area_px(local_points), 3),
        "skeleton_length_px": skeleton_px,
        "mean_width_px": round(mean_width_px, 3),
        "max_width_px": round(max_width_px, 3),
    }


def choose_split(source_id: str, source_count: int) -> str:
    if source_count < 2:
        return "train"
    digest = hashlib.sha1(source_id.encode("utf-8", errors="ignore")).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return "val" if bucket < VAL_FRACTION else "train"


def ensure_non_empty_train_val(samples: list[dict]) -> str:
    if not samples:
        return "empty"
    splits = {sample["split"] for sample in samples}
    if len(samples) == 1:
        samples[0]["split"] = "train"
        samples.append({**samples[0], "split": "val", "split_note": "single_sample_dev_fallback_duplicate"})
        return "single_sample_dev_fallback_duplicate"
    if "val" not in splits:
        samples[-1]["split"] = "val"
        return "hash_split_adjusted_for_non_empty_val"
    if "train" not in splits:
        samples[0]["split"] = "train"
        return "hash_split_adjusted_for_non_empty_train"
    return "hash_by_source_image"


def iter_regions(data: dict) -> list[tuple[int, list[tuple[int, int]]]]:
    """Read the current polygon format, with compatibility for older box JSON."""
    regions: list[tuple[int, list[tuple[int, int]]]] = []
    if isinstance(data.get("regions"), list):
        for idx, region in enumerate(data["regions"], start=1):
            points = region.get("points") or []
            polygon = [(int(p["x"]), int(p["y"])) for p in points if "x" in p and "y" in p]
            if len(polygon) >= 3:
                regions.append((int(region.get("region_id") or idx), polygon))
        return regions

    for idx, box in enumerate(data.get("boxes") or [], start=1):
        try:
            x0, y0, x1, y1 = int(box["x0"]), int(box["y0"]), int(box["x1"]), int(box["y1"])
        except KeyError:
            continue
        regions.append((
            int(box.get("box_id") or idx),
            [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
        ))
    return regions


def build_dataset_from_annotations(training_data_dir: Path, dataset_dir: Path) -> dict:
    """Rebuild raw/stitch YOLO-seg crops with a train/val split and metadata."""
    for rel in ("images/train", "images/val", "labels/train", "labels/val", "metadata"):
        shutil.rmtree(dataset_dir / rel, ignore_errors=True)
    for split in ("train", "val"):
        (dataset_dir / "images" / split).mkdir(parents=True, exist_ok=True)
        (dataset_dir / "labels" / split).mkdir(parents=True, exist_ok=True)
    metadata_dir = dataset_dir / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    write_dataset_yaml(dataset_dir)

    if not training_data_dir.is_dir():
        return {"written": 0, "train": 0, "val": 0, "split_strategy": "missing_training_data_dir"}

    json_paths = sorted(training_data_dir.glob("*.json"))
    source_count = len(json_paths)
    samples: list[dict] = []
    for json_path in json_paths:
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"skip {json_path.name}: invalid JSON ({exc})")
            continue

        source_id = data.get("facade_id") or json_path.stem
        image_path = data.get("image_path")
        regions = iter_regions(data)
        if not image_path or not Path(image_path).exists():
            print(f"skip {source_id}: image_path missing or not found ({image_path})")
            continue
        if not regions:
            continue

        img = cv2.imread(image_path)
        if img is None:
            print(f"skip {source_id}: could not read {image_path}")
            continue
        img_h, img_w = img.shape[:2]

        source_split = choose_split(str(source_id), source_count)
        source_hash = file_sha256(Path(image_path))

        for region_id, polygon_points in regions:
            x_values = [p[0] for p in polygon_points]
            y_values = [p[1] for p in polygon_points]
            x0, y0, x1, y1 = min(x_values), min(y_values), max(x_values), max(y_values)
            bw, bh = x1 - x0, y1 - y0
            pad_x, pad_y = int(bw * CROP_PADDING_FRAC), int(bh * CROP_PADDING_FRAC)
            cx0 = max(0, x0 - pad_x)
            cy0 = max(0, y0 - pad_y)
            cx1 = min(img_w, x1 + pad_x)
            cy1 = min(img_h, y1 + pad_y)
            if cx1 <= cx0 or cy1 <= cy0:
                continue

            crop = img[cy0:cy1, cx0:cx1]
            ch, cw = crop.shape[:2]
            if ch < 8 or cw < 8:
                continue

            label_points: list[str] = []
            local_points: list[tuple[int, int]] = []
            for px, py in polygon_points:
                local_x = int(np.clip(px - cx0, 0, cw - 1))
                local_y = int(np.clip(py - cy0, 0, ch - 1))
                local_points.append((local_x, local_y))
                label_points.append(f"{local_x / cw:.6f}")
                label_points.append(f"{local_y / ch:.6f}")
            polygon = "0 " + " ".join(label_points)

            stem = f"viewer_{source_id}_{region_id}"
            samples.append({
                "sample_id": stem,
                "split": source_split,
                "source_annotation_json": str(json_path),
                "source_id": source_id,
                "source_image": image_path,
                "source_image_sha256": source_hash,
                "source_image_width_px": img_w,
                "source_image_height_px": img_h,
                "region_id": region_id,
                "crop_origin_x": cx0,
                "crop_origin_y": cy0,
                "crop_width": cw,
                "crop_height": ch,
                "global_to_local_mapping": {
                    "local_x": "global_x - crop_origin_x",
                    "local_y": "global_y - crop_origin_y",
                },
                "original_polygon_points": [{"x": int(x), "y": int(y)} for x, y in polygon_points],
                "local_polygon_points": [{"x": int(x), "y": int(y)} for x, y in local_points],
                "yolo_polygon": polygon,
                "crop_image": f"images/{{split}}/{stem}.jpg",
                "label_file": f"labels/{{split}}/{stem}.txt",
                "pixel_measurements": compute_polygon_measurements(local_points, cw, ch),
                "_crop": crop,
            })

    split_strategy = ensure_non_empty_train_val(samples)
    counts = {"train": 0, "val": 0}
    with (metadata_dir / "samples.jsonl").open("w", encoding="utf-8") as meta:
        for sample in samples:
            split = sample["split"]
            stem = sample["sample_id"]
            crop = sample.pop("_crop")
            img_path = dataset_dir / "images" / split / f"{stem}.jpg"
            lbl_path = dataset_dir / "labels" / split / f"{stem}.txt"
            cv2.imwrite(str(img_path), crop)
            lbl_path.write_text(sample["yolo_polygon"], encoding="utf-8")
            sample["crop_image"] = f"images/{split}/{stem}.jpg"
            sample["label_file"] = f"labels/{split}/{stem}.txt"
            meta.write(json.dumps(sample, ensure_ascii=False) + "\n")
            counts[split] += 1

    summary = {
        "written": counts["train"] + counts["val"],
        "train": counts["train"],
        "val": counts["val"],
        "split_strategy": split_strategy,
        "metadata": str(metadata_dir / "samples.jsonl"),
    }
    (metadata_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"prepared {summary['written']} viewer-annotated crop(s): train={counts['train']}, val={counts['val']}")
    return summary


def find_latest_checkpoint(source: str) -> Path | None:
    """Only looks at this SOURCE's own prior runs (runs/<source>_*/weights/best.pt)
    -- fine-tuning must never silently pick up a checkpoint another source's
    data actually trained."""
    candidates = sorted(
        RUNS_DIR.glob(f"{source}_*/weights/best.pt"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


def has_split_images(path: Path) -> bool:
    return path.is_dir() and any(p.is_file() and p.suffix.lower() in IMAGE_EXTS for p in path.iterdir())


def detect_training_device() -> tuple[int | str, dict]:
    try:
        import torch

        cuda_available = bool(torch.cuda.is_available())
        if cuda_available:
            index = int(torch.cuda.current_device())
            return index, {
                "device": index,
                "device_type": "cuda",
                "cuda_available": True,
                "cuda_device_count": int(torch.cuda.device_count()),
                "cuda_device_name": torch.cuda.get_device_name(index),
                "torch_version": torch.__version__,
                "cuda_version": torch.version.cuda,
            }

        return "cpu", {
            "device": "cpu",
            "device_type": "cpu",
            "cuda_available": False,
            "cuda_device_count": int(torch.cuda.device_count()),
            "torch_version": torch.__version__,
            "cuda_version": torch.version.cuda,
        }
    except Exception as exc:
        return "cpu", {
            "device": "cpu",
            "device_type": "cpu",
            "cuda_available": False,
            "device_detection_error": str(exc),
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=list(SOURCES), required=True)
    parser.add_argument("--mode", choices=["new", "finetune"], required=True)
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--labeled-input", type=Path, default=None)
    parser.add_argument("--run-id", default=None, help="stable run id supplied by the viewer for per-run logs")
    args = parser.parse_args()
    source_cfg = SOURCES[args.source]
    dataset_dir = source_cfg["dataset_dir"]
    run_name = args.run_id or f"{args.source}_{args.mode}_{time.strftime('%Y%m%d_%H%M%S')}"
    run_log_dir = configure_run_logs(run_name)

    write_status(
        status="preparing",
        source=args.source,
        mode=args.mode,
        run_name=run_name,
        log_dir=str(run_log_dir),
        dataset_dir=str(dataset_dir),
    )

    dataset_summary: dict = {}
    if args.source == "labeled":
        from training.crack_seg.prepare_labeled_dataset import main as prepare_labeled_dataset

        if args.labeled_input is None:
            write_status(
                status="error", source=args.source, mode=args.mode, run_name=run_name, log_dir=str(run_log_dir),
                error="기존 마스크 데이터셋 폴더를 먼저 선택하세요.",
            )
            write_summary(
                status="error", source=args.source, mode=args.mode, run_name=run_name,
                log_dir=str(run_log_dir), error="기존 마스크 데이터셋 폴더를 먼저 선택하세요.",
            )
            print("missing --labeled-input for source=labeled")
            sys.exit(1)
        prepare_labeled_dataset(args.labeled_input)
        dataset_summary = {
            "source": "labeled",
            "split_strategy": "prepare_labeled_dataset",
        }
    else:
        dataset_summary = build_dataset_from_annotations(source_cfg["training_data_dir"], dataset_dir)

    # dataset.yaml itself now always gets (re)written by build_dataset_from_
    # annotations, so its mere existence no longer proves there's anything to
    # train on -- check the actual image folder has content instead.
    dataset_yaml = dataset_dir / "dataset.yaml"
    train_has_images = has_split_images(dataset_dir / "images" / "train")
    val_has_images = has_split_images(dataset_dir / "images" / "val")
    if not dataset_yaml.exists() or not train_has_images or not val_has_images:
        write_status(
            status="error", source=args.source, mode=args.mode, run_name=run_name, log_dir=str(run_log_dir),
            error=f"학습 데이터가 없습니다: {dataset_dir} -- 먼저 데이터를 준비/저장하세요.",
        )
        write_summary(
            status="error", source=args.source, mode=args.mode, run_name=run_name,
            log_dir=str(run_log_dir), dataset_yaml=str(dataset_yaml),
            error=f"학습 데이터가 없습니다: {dataset_dir} -- 먼저 데이터를 준비/저장하세요.",
        )
        print(f"no dataset at {dataset_yaml} -- nothing to train on")
        sys.exit(1)

    if args.mode == "finetune":
        checkpoint = find_latest_checkpoint(args.source)
        if checkpoint is None:
            write_status(
                status="error", source=args.source, mode=args.mode, run_name=run_name, log_dir=str(run_log_dir),
                error="파인튜닝할 기존 가중치(best.pt)가 없습니다 -- 먼저 이 소스로 새 학습을 실행하세요.",
            )
            write_summary(
                status="error", source=args.source, mode=args.mode, run_name=run_name,
                log_dir=str(run_log_dir),
                error="파인튜닝할 기존 가중치(best.pt)가 없습니다 -- 먼저 이 소스로 새 학습을 실행하세요.",
            )
            print(f"no existing runs/{args.source}_*/weights/best.pt found -- run --mode new first")
            sys.exit(1)
        start_from = str(checkpoint)
        print(f"fine-tuning from {checkpoint}")
    else:
        start_from = BASE_CHECKPOINT
        print(f"training from scratch (base checkpoint {BASE_CHECKPOINT})")

    start_checkpoint_path = Path(start_from)
    training_parameters = {
        "epochs": args.epochs,
        "imgsz": 512,
        "batch": 32,
        "workers": 0,
        "patience": 10,
        "lr0": "ultralytics_default",
        "augmentation": "ultralytics_default",
        "base_checkpoint_sha256": file_sha256(start_checkpoint_path) if start_checkpoint_path.exists() else None,
    }

    # Imported after argparse/dataset-prep so `--help` and dataset errors
    # don't pay ultralytics' own import cost first.
    from ultralytics import YOLO  # noqa: E402

    device, device_info = detect_training_device()
    model = YOLO(start_from)

    def on_epoch_end(trainer) -> None:
        write_status(
            status="training",
            source=args.source,
            mode=args.mode,
            epoch=trainer.epoch + 1,
            epochs=trainer.epochs,
            run_name=run_name,
            log_dir=str(run_log_dir),
            base_checkpoint=start_from,
            device_info=device_info,
            dataset_summary=dataset_summary,
            training_parameters=training_parameters,
        )

    model.add_callback("on_train_epoch_end", on_epoch_end)

    write_status(
        status="training",
        source=args.source,
        mode=args.mode,
        epoch=0,
        epochs=args.epochs,
        run_name=run_name,
        log_dir=str(run_log_dir),
        dataset_yaml=str(dataset_yaml),
        base_checkpoint=start_from,
        device_info=device_info,
        dataset_summary=dataset_summary,
        training_parameters=training_parameters,
    )
    results = model.train(
        data=str(dataset_yaml),
        epochs=args.epochs,
        imgsz=512,
        batch=32,
        project=str(RUNS_DIR),
        name=run_name,
        patience=10,
        device=device,
        workers=0,  # Windows multiprocessing DataLoader workers hung silently (no progress, no error) for 20+ min
    )
    run_dir = str(results.save_dir) if results is not None else str(RUNS_DIR / run_name)

    # Explicit val() after training (rather than trusting train()'s own return
    # value's shape, which varies across ultralytics versions) -- .results_dict
    # is the stable public surface for "give me the accuracy numbers as a
    # plain dict" on both DetMetrics and SegmentMetrics.
    write_status(
        status="validating",
        source=args.source,
        mode=args.mode,
        run_name=run_name,
        log_dir=str(run_log_dir),
        run_dir=run_dir,
        base_checkpoint=start_from,
        device_info=device_info,
        dataset_summary=dataset_summary,
        training_parameters=training_parameters,
    )
    val_metrics = model.val(data=str(dataset_yaml), device=device)
    metrics = {k: float(v) for k, v in val_metrics.results_dict.items()}

    result_best = str(Path(run_dir) / "weights" / "best.pt")
    result_last = str(Path(run_dir) / "weights" / "last.pt")
    write_status(
        status="done",
        source=args.source,
        mode=args.mode,
        run_name=run_name,
        log_dir=str(run_log_dir),
        run_dir=run_dir,
        dataset_yaml=str(dataset_yaml),
        base_checkpoint=start_from,
        result_best_checkpoint=result_best,
        result_last_checkpoint=result_last,
        device_info=device_info,
        dataset_summary=dataset_summary,
        training_parameters=training_parameters,
        metrics=metrics,
    )
    write_summary(
        status="done",
        source=args.source,
        mode=args.mode,
        run_name=run_name,
        log_dir=str(run_log_dir),
        run_dir=run_dir,
        dataset_yaml=str(dataset_yaml),
        base_checkpoint=start_from,
        result_best_checkpoint=result_best,
        result_last_checkpoint=result_last,
        epochs=args.epochs,
        imgsz=512,
        batch=32,
        device=device,
        device_info=device_info,
        dataset_summary=dataset_summary,
        training_parameters=training_parameters,
        metrics=metrics,
    )
    print(f"done: {run_dir}")
    print(f"metrics: {metrics}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        if _status_path is not None:
            error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            detail = traceback.format_exc()
            write_status(status="error", error=error, traceback=detail)
            write_summary(status="error", error=error, traceback=detail)
        raise
