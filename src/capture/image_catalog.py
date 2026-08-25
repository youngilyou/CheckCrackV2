"""Build an image catalog (metadata table) for a set of DJI still images.

Output mirrors CLAUDE.local.md #28: ``metadata/images.parquet``.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pandas as pd
import yaml

from src.capture.dji_metadata import parse_dji_image
from src.common.atomic_io import atomic_write_parquet
from src.common.logging import get_logger, log_event
from src.common.types import ImageMetadata

_IMAGE_EXTS = {".jpg", ".jpeg"}

# Fields an operator can set in <images_dir>/session_metadata.yaml -- DJI
# EXIF/XMP carries none of these (capture distance, battery, weather, etc.
# aren't camera telemetry), so they're supplied by the operator, never
# guessed (CLAUDE.local.md #5). Field set mirrors
# CheckCrack_Operator_Camera_Capture_Settings_Guide's "운용자 필수 Metadata" slide.
_MISSION_KEYS = {
    "capture_session", "capture_distance_m", "distance_measurement_method",
    "battery_sequence", "precision_zone_of",
}
_OPERATOR_SESSION_KEYS = {
    "capture_date", "building_block", "building_side",
    "image_original_preserved", "reference_scale_available",
    "weather", "lighting_condition", "notes",
}


def scan_images(images_dir: str | Path) -> list[Path]:
    images_dir = Path(images_dir)
    # The Viewer's "--in-place" flow (tools/stitch_folder.py) writes this
    # facade's own results, including numbered *.jpg live-preview snapshots,
    # to <images_dir>/output/ — a subfolder of the very directory this rglob
    # scans. Without this exclusion, re-running (or a crash leaving stale
    # preview files behind) feeds the pipeline's own output back in as if it
    # were source photos on the next run, corrupting image_count/matching.
    output_dir = images_dir / "output"
    return sorted(
        p for p in images_dir.rglob("*")
        if p.suffix.lower() in _IMAGE_EXTS and output_dir not in p.parents
    )


def _load_session_metadata(images_dir: Path) -> dict:
    """<images_dir>/session_metadata.yaml, if present. Missing/empty file ->
    {} (every field just stays None, per CLAUDE.local.md #5 -- never invented)."""
    path = images_dir / "session_metadata.yaml"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _apply_session_metadata(meta: ImageMetadata, session: dict) -> None:
    for key in _MISSION_KEYS:
        if key in session:
            setattr(meta.mission, key, session[key])
    for key in _OPERATOR_SESSION_KEYS:
        if key in session:
            setattr(meta.operator_session, key, session[key])


def _check_no_zoom(catalog: list[ImageMetadata]) -> None:
    """QA warning (not a hard fail): the capture guide forbids digital/optical
    zoom mid-flight (같은 세션 내 초점거리 고정). If a capture session's
    equivalent_focal_length_mm varies, zoom may have been used -- flag it so
    a human can check, rather than silently stitching mismatched-scale photos."""
    groups: dict[str, list[ImageMetadata]] = {}
    for meta in catalog:
        key = meta.mission.capture_session or meta.mission.flight_id
        if not key:
            continue
        groups.setdefault(key, []).append(meta)

    logger = None
    for session_key, metas in groups.items():
        focal_lengths = {
            m.camera.equivalent_focal_length_mm for m in metas
            if m.camera.equivalent_focal_length_mm is not None
        }
        if len(focal_lengths) > 1:
            if logger is None:
                logger = get_logger("pipeline", log_dir="logs")
            log_event(
                logger, "warning",
                "capture session에서 equivalent_focal_length_mm이 일정하지 않음 -- 비행 중 줌 사용 의심",
                stage="CAPTURE_QA", capture_session=session_key,
                equivalent_focal_lengths_mm=sorted(focal_lengths),
                image_count=len(metas),
            )


def build_catalog(images_dir: str | Path) -> list[ImageMetadata]:
    images_dir = Path(images_dir)
    session = _load_session_metadata(images_dir)
    catalog = [parse_dji_image(p) for p in scan_images(images_dir)]
    if session:
        for meta in catalog:
            _apply_session_metadata(meta, session)
    _check_no_zoom(catalog)
    return catalog


def catalog_to_dataframe(catalog: list[ImageMetadata]) -> pd.DataFrame:
    rows = [_flatten(meta) for meta in catalog]
    return pd.DataFrame(rows)


def _flatten(meta: ImageMetadata) -> dict:
    row = {
        "image_id": meta.image_id,
        "file_path": meta.file_path,
        "timestamp_utc": meta.timestamp_utc,
        "drone_model": meta.drone_model,
        "camera_model": meta.camera_model,
        "width": meta.width,
        "height": meta.height,
    }
    for prefix, obj in (
        ("gps", meta.gps),
        ("drone_pose", meta.drone_pose),
        ("gimbal_pose", meta.gimbal_pose),
        ("camera", meta.camera),
        ("mission", meta.mission),
        ("operator_session", meta.operator_session),
    ):
        for k, v in asdict(obj).items():
            row[f"{prefix}.{k}"] = v
    return row


def save_catalog(catalog: list[ImageMetadata], out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df = catalog_to_dataframe(catalog)
    atomic_write_parquet(df, out_path)
    return out_path


def load_catalog(images_dir: str | Path, cache_path: str | Path | None = None) -> list[ImageMetadata]:
    """Build (and optionally cache) an image catalog for `images_dir`.

    Re-parses from source images every call unless a valid parquet cache is
    given via `cache_path` — kept simple since Phase 1 catalogs are small
    (tens-hundreds of images).
    """
    catalog = build_catalog(images_dir)
    if cache_path is not None:
        save_catalog(catalog, cache_path)
    return catalog
