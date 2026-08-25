"""Finetune YOLOv8-seg on CUBIT-Seg crack polygons (CLAUDE.local.md #2.7).

Starts from Ultralytics' COCO-pretrained yolov8s-seg backbone (standard
transfer-learning initialization, not a crack-specific checkpoint) and
finetunes on our own local crack512 data — this is the finetuning path the
doc calls for instead of trusting an unrelated pretrained crack model as-is.
"""

from ultralytics import YOLO

if __name__ == "__main__":
    model = YOLO("yolov8s-seg.pt")
    model.train(
        data="training/crack_seg/dataset/dataset.yaml",
        epochs=40,
        imgsz=512,
        batch=32,
        project="training/crack_seg/runs",
        name="baseline",
        patience=10,
        device=0,
        workers=0,  # Windows multiprocessing DataLoader workers hung silently (no progress, no error) for 20+ min
    )
