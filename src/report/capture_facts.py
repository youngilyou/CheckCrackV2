"""Shooting facts for the report cover / section 01 (2026-10-09, 사용자 요청: "작업중" 칸을 실제 값으로).

Everything here is measured from the run's own data -- never a planned/assumed value:
  * equipment / camera: EXIF/XMP Make + Model of a source photo, resolution, EXIF focal length (mm) when present. The drone name is only given for camera models whose drone is unambiguous
    (CAMERA_TO_DRONE); otherwise just the maker + camera model.
  * capture date(s): EXIF time of the first and last source photo (by file name = shooting order).
  * photo count: registered source photos of the mosaic (`{facade}_source_images.json`).
  * shooting distance: perpendicular distance of every registered camera to the facade plane, from the COLMAP
    reconstruction + the Sim3d/plane in `{facade}_depth_mapping.json` (same geometry the Dense mosaic used);
    original resolution on the wall = distance / focal length (mm per photo pixel). Missing reconstruction (e.g.
    deleted at contract end) -> those facts are simply absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

# Camera model (EXIF Model) -> drone, only where the camera is built into exactly one drone line.
CAMERA_TO_DRONE = {
    "M3E": "DJI Mavic 3 Enterprise",
    "M3T": "DJI Mavic 3 Thermal",
    "M4E": "DJI Matrice 4E",
    "M4T": "DJI Matrice 4T",
    "L2D-20c": "DJI Mavic 3 계열",
}


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def _photo_facts(output_dir: Path, source_images: list[dict]) -> dict:
    from src.capture.dji_metadata import parse_dji_image
    from src.common.paths import resolve_source_image

    facts: dict = {"photo_count": len(source_images) or None}
    ordered = sorted((e for e in source_images if isinstance(e, dict)), key=lambda e: str(e.get("image_id", "")))
    parsed = []
    for entry in ordered[:5] + ordered[-5:][::-1]:  # first readable from the start and from the end
        path = resolve_source_image(entry.get("file_path", ""), output_dir)
        if not path:
            continue
        try:
            parsed.append(parse_dji_image(path))
        except Exception:  # noqa: BLE001 -- a broken photo only means a missing fact
            continue
        if len(parsed) >= 2 and parsed[0].image_id != parsed[-1].image_id:
            break
    if not parsed:
        return facts
    first = parsed[0]
    make, model = first.drone_model, first.camera_model
    if make or model:
        drone = CAMERA_TO_DRONE.get(model or "")
        raw = " ".join(p for p in (make, model) if p)
        facts["equipment"] = f"{drone} (EXIF {raw})" if drone else raw
    camera = [model] if model else []
    if first.width and first.height:
        camera.append(f"{first.width}×{first.height}")
    if first.camera.focal_length_mm:
        camera.append(f"초점거리 {first.camera.focal_length_mm:g} mm")
    if camera:
        facts["camera"] = " · ".join(camera)
    dates = sorted({m.timestamp_utc[:10] for m in parsed if m.timestamp_utc})
    if dates:
        facts["capture_date"] = dates[0] if len(dates) == 1 else f"{dates[0]} ~ {dates[-1]}"
    return facts


def _distance_facts(output_dir: Path, facade_id: str) -> dict:
    sidecar = _read_json(output_dir / f"{facade_id}_depth_mapping.json")
    if not isinstance(sidecar, dict):
        return {}
    sparse_dir = output_dir / str(sidecar.get("native_sparse_dir", ""))
    if not sparse_dir.exists():
        return {}
    try:
        import pycolmap

        rec = pycolmap.Reconstruction(str(sparse_dir))
    except Exception:  # noqa: BLE001
        return {}
    sim = sidecar["sim3d"]
    scale, rot, trans = float(sim["scale"]), np.asarray(sim["rotation"], float), np.asarray(sim["translation"], float)
    plane = sidecar["plane"]
    origin = np.asarray(plane["origin"], float)
    normal = np.cross(np.asarray(plane["e_u"], float), np.asarray(plane["e_v"], float))
    normal /= np.linalg.norm(normal)
    distances, mm_per_px = [], []
    for image in rec.images.values():
        if not image.has_pose:
            continue
        center = scale * rot @ np.asarray(image.projection_center(), float) + trans
        d = abs(float(np.dot(center - origin, normal)))
        distances.append(d)
        focal_px = float(rec.cameras[image.camera_id].mean_focal_length())
        if focal_px > 0:
            mm_per_px.append(d / focal_px * 1000.0)
    if not distances:
        return {}
    d = np.asarray(distances)
    facts = {
        "distance_median_m": round(float(np.median(d)), 1),
        "distance_min_m": round(float(np.percentile(d, 5)), 1),
        "distance_max_m": round(float(np.percentile(d, 95)), 1),
    }
    if mm_per_px:
        facts["mm_per_px_median"] = round(float(np.median(mm_per_px)), 2)
    return facts


def compute_capture_facts(output_dir: str | Path, facade_id: str, source_images: list[dict]) -> dict:
    output_dir = Path(output_dir)
    facts = _photo_facts(output_dir, source_images)
    facts.update(_distance_facts(output_dir, facade_id))
    return facts


def distance_text(facts: dict) -> str | None:
    """"실측 9.8 m (8.9~11.2 m) · 원본 약 2.9 mm/px" -- None when nothing was measured."""
    if facts.get("distance_median_m") is None:
        return None
    text = f"실측 {facts['distance_median_m']:.1f} m ({facts['distance_min_m']:.1f}~{facts['distance_max_m']:.1f} m)"
    if facts.get("mm_per_px_median") is not None:
        text += f" · 원본 약 {facts['mm_per_px_median']:.1f} mm/px"
    return text
