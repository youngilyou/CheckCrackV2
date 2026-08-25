"""Applies a human reviewer's decisions on top of raw AI crack detections
before report generation (CLAUDE.local.md #37: "Human Verification" — AI
Crack Candidate -> Human Verification -> Final Record).

The review overlay (`{facade_id}_crack_review.json`, written by the
CheckCrackViewer UI) never touches `{facade_id}_cracks.json` itself -- that
file is owned and rewritten by the Python detection pipeline on every
re-run (tools/detect_cracks_folder.py), so writing review state into it
would just get silently discarded the next time detection runs. Keeping
review state in its own sibling file also means the raw AI output stays
intact for audit (CLAUDE.local.md #39 provenance) even after a reviewer
rejects a detection.

A manually-added crack is measured through the exact same
`skeleton.measure_polygon` the AI pipeline uses for its own detections --
same algorithm, only the polygon's source differs (human-drawn vs
AI-inferred) -- so its length/width/area numbers are real, not guessed.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from src.crack.skeleton import measure_polygon


def apply_review(cracks: list[dict[str, Any]], review: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Returns the crack list a report should actually render: AI detections
    minus anything a reviewer rejected, plus anything a reviewer manually
    added. Every returned entry carries `source: "ai"` or `"manual"` so the
    report can visibly label human-added cracks rather than presenting them
    as if the AI had found them (CLAUDE.local.md #5/#39 -- never blur the
    line between a detection and a human judgment call).

    `review` is None (or has no matching data) exactly when no
    `_crack_review.json` exists yet for this version -- in that case every
    AI crack passes through unchanged, tagged "ai"."""
    rejected_ids = set()
    manual_additions: list[dict[str, Any]] = []
    if review:
        rejected_ids = {r["crack_id"] for r in review.get("rejected", [])}
        manual_additions = review.get("manual_additions", [])

    result: list[dict[str, Any]] = []
    for crack in cracks:
        if crack.get("crack_id") in rejected_ids:
            continue
        c = dict(crack)
        c.setdefault("source", "ai")
        result.append(c)

    facade_id = cracks[0]["facade_id"] if cracks else (review or {}).get("facade_id", "")
    existing_manual_nums = [
        int(c["crack_id"].rsplit("_M", 1)[1])
        for c in result
        if "_M" in c.get("crack_id", "") and c["crack_id"].rsplit("_M", 1)[1].isdigit()
    ]
    next_manual_num = max(existing_manual_nums, default=0) + 1

    for addition in manual_additions:
        polygon = np.asarray(addition["polygon_px"], dtype=np.float64)
        measurement = measure_polygon(polygon)
        if measurement is None:
            continue  # degenerate polygon (e.g. a single click with no area) -- skip, don't fabricate a crack from nothing

        x0, y0 = polygon.min(axis=0)
        x1, y1 = polygon.max(axis=0)
        crack_id = addition.get("crack_id") or f"{facade_id}_M{next_manual_num:06d}"
        next_manual_num += 1

        result.append({
            "crack_id": crack_id,
            "facade_id": facade_id,
            "length_px": round(measurement.length_px, 2),
            "max_width_px": round(measurement.max_width_px, 2),
            "mean_width_px": round(measurement.mean_width_px, 2),
            "area_px": round(float(cv2_polygon_area(polygon)), 1),
            "length_mm": None,
            "max_width_mm": None,
            "area_mm2": None,
            "confidence": 1.0,  # a human-confirmed crack, not a model score -- see "source" for what that means
            "observation_state": "OBSERVED",
            "source_image_ids": addition.get("source_image_ids", []),
            "severity": None,
            "building_id": addition.get("building_id", "B000"),
            "position": {
                "pixel_x": round(float((x0 + x1) / 2), 1),
                "pixel_y": round(float((y0 + y1) / 2), 1),
                "u_m": None,
                "v_m": None,
            },
            "bbox_px": [round(float(x0), 1), round(float(y0), 1), round(float(x1), 1), round(float(y1), 1)],
            "polygon_px": polygon.round(1).tolist(),
            "skeleton_px": measurement.skeleton_px.round(1).tolist(),
            "source_tile_ids": [],
            "source": "manual",
            "added_by": addition.get("added_by"),
            "added_at": addition.get("added_at"),
        })

    return result


def cv2_polygon_area(polygon_px: np.ndarray) -> float:
    """Shoelace formula -- avoids an extra cv2 import just for this."""
    x = polygon_px[:, 0]
    y = polygon_px[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))
