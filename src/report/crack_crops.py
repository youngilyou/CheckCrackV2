"""Per-crack crop thumbnails for the PDF report's crack-detail section
(CLAUDE.local.md #24/#27 — the report must show the crack itself, not just a
row of numbers).

Two crops per crack, mirroring the operator's own reference UI (WIDE/ZOOM
side-by-side panels): a wider "context" crop with a box marking where the
crack sits, and a tight "zoom" crop with the mask filled in so the crack
itself is legible. Both are cut from the SAME stitched facade mosaic, not
from a separately re-captured/zoomed camera frame — labeled accordingly in
the report template ("위치"/"확대", never "카메라 원본") so a derived crop is
never presented as something it isn't.

`analysis_image` must be loaded by the caller exactly ONCE per facade and
reused across every crack — these mosaics can legitimately run tens of
thousands of pixels wide (#21), so re-opening the full TIFF once per crack
would repeat the memory-explosion class of bug this project already hit and
fixed elsewhere in the pipeline (large-image handling). Cropping (twice, at
two window sizes) out of an already-in-memory array is cheap; loading the
array is not.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import cv2
import numpy as np

MASK_COLOR_BGR = (40, 40, 235)  # crack red — matches the color used elsewhere in this report
MASK_ALPHA = 0.45
BOX_COLOR_BGR = (40, 220, 60)  # green ROI box, matches the reference UI's location marker
MANUAL_MARKER_COLOR_BGR = (196, 86, 26)  # blue — distinguishes reviewer-added cracks from AI red on the crack map


@dataclass
class CrackCrop:
    crack_id: str
    context_uri: str | None  # wider crop with a box marking the crack's location -- "where is it"
    zoom_uri: str | None  # tight crop with the mask filled in -- "what does it look like"


def _to_data_uri(bgr_image: np.ndarray, quality: int = 88) -> str:
    ok, buf = cv2.imencode(".jpg", bgr_image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("failed to encode crack crop as JPEG")
    b64 = base64.b64encode(buf.tobytes()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _crop_region(analysis_image: np.ndarray, bbox: list[float], pad_px: int) -> tuple[np.ndarray, int, int] | None:
    h, w = analysis_image.shape[:2]
    x0, y0, x1, y1 = bbox
    cx0 = max(0, int(x0) - pad_px)
    cy0 = max(0, int(y0) - pad_px)
    cx1 = min(w, int(x1) + pad_px)
    cy1 = min(h, int(y1) + pad_px)
    if cx1 <= cx0 or cy1 <= cy0:
        return None
    crop = analysis_image[cy0:cy1, cx0:cx1].copy()
    if crop.size == 0:
        return None
    return crop, cx0, cy0


def _downscale(image: np.ndarray, max_dim_px: int) -> np.ndarray:
    h, w = image.shape[:2]
    scale = min(1.0, max_dim_px / max(w, h))
    if scale >= 1.0:
        return image
    return cv2.resize(image, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)


def generate_crack_crops(
    analysis_image: np.ndarray,
    cracks: list[dict],
    zoom_pad_px: int = 60,
    zoom_max_dim_px: int = 420,
    context_pad_px: int = 260,
    context_max_dim_px: int = 420,
) -> dict[str, CrackCrop]:
    """Returns {crack_id: CrackCrop}. Cracks whose bbox/polygon can't be read
    (missing fields, out-of-bounds after clipping to an empty region) are
    silently skipped — the report template falls back to a plain "이미지
    없음" placeholder for those rather than showing a broken image.

    Long cracks (a >500px-long diagonal seam crack is common in this
    dataset) get a proportionally wider context window than the fixed
    `context_pad_px` alone would give — otherwise the box marking a long
    crack would run off the edge of its own "where is it" crop, defeating
    the point of showing context at all."""
    out: dict[str, CrackCrop] = {}

    for crack in cracks:
        bbox = crack.get("bbox_px")
        polygon = crack.get("polygon_px")
        crack_id = crack.get("crack_id")
        if not bbox or not polygon or not crack_id:
            continue

        bbox_w = bbox[2] - bbox[0]
        bbox_h = bbox[3] - bbox[1]
        adaptive_context_pad = max(context_pad_px, int(0.35 * max(bbox_w, bbox_h)))

        zoom_uri = None
        zoomed = _crop_region(analysis_image, bbox, zoom_pad_px)
        if zoomed is not None:
            crop, ox, oy = zoomed
            pts = np.array(polygon, dtype=np.float64)
            pts[:, 0] -= ox
            pts[:, 1] -= oy
            pts_i = np.round(pts).astype(np.int32).reshape(-1, 1, 2)

            overlay = crop.copy()
            cv2.fillPoly(overlay, [pts_i], MASK_COLOR_BGR)
            cv2.addWeighted(overlay, MASK_ALPHA, crop, 1 - MASK_ALPHA, 0, dst=crop)
            cv2.polylines(crop, [pts_i], isClosed=True, color=MASK_COLOR_BGR, thickness=2)
            zoom_uri = _to_data_uri(_downscale(crop, zoom_max_dim_px))

        context_uri = None
        contexted = _crop_region(analysis_image, bbox, adaptive_context_pad)
        if contexted is not None:
            crop, ox, oy = contexted
            bx0, by0 = int(bbox[0] - ox), int(bbox[1] - oy)
            bx1, by1 = int(bbox[2] - ox), int(bbox[3] - oy)
            thickness = max(2, round(min(crop.shape[0], crop.shape[1]) / 150))
            cv2.rectangle(crop, (bx0, by0), (bx1, by1), BOX_COLOR_BGR, thickness)
            context_uri = _to_data_uri(_downscale(crop, context_max_dim_px))

        if zoom_uri is None and context_uri is None:
            continue
        out[crack_id] = CrackCrop(crack_id=crack_id, context_uri=context_uri, zoom_uri=zoom_uri)

    return out


def generate_crack_map(analysis_image: np.ndarray, numbered_cracks: list[dict], max_dim_px: int = 2800) -> str | None:
    """Whole-facade mosaic with every crack outlined and numbered (matching
    the operator's own reference screen: a full elevation photo with numbered
    markers, cross-referenced against a detail list below it) — the "확대"
    per-crack crops alone answer "what does this crack look like" but not
    "where does it sit on the whole wall", which this answers instead.

    `numbered_cracks` entries need `_no` (the same number shown on that
    crack's detail card, assigned by the caller so the map and the cards use
    one consistent numbering) plus the usual `polygon_px`/`bbox_px`. Markers
    are drawn AFTER downscaling to the final canvas size, not on the
    full-resolution mosaic first — a 1-2px outline drawn at full res on a
    tens-of-thousands-of-px-wide image would just get smoothed away by the
    resize's area-averaging, and drawing directly on the shrunk canvas is
    far cheaper anyway."""
    h, w = analysis_image.shape[:2]
    scale = min(1.0, max_dim_px / max(w, h))
    canvas = cv2.resize(analysis_image, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA) if scale < 1.0 else analysis_image.copy()
    canvas_h, canvas_w = canvas.shape[:2]

    # Sized relative to the canvas, not a fixed pixel count -- a fixed small
    # radius/font (the first version of this function used ~9px/0.42) reads
    # fine on a debug screenshot but turns into unreadable red specks once
    # embedded in a printed report page (confirmed: user reported exactly
    # this after the first version shipped). Big enough to read at normal
    # report zoom, at the cost of nearby markers sometimes overlapping on a
    # facade with many close-together cracks -- legibility of any one number
    # matters more than avoiding all overlap.
    base_dim = max(canvas_w, canvas_h)
    radius = max(8, round(0.0073 * base_dim))  # 1/3 of the first version's size (user: too big)
    font_scale = radius / 26.0
    text_thickness = max(1, round(radius / 16))
    outline_thickness = max(2, round(base_dim / 1400))

    for crack in numbered_cracks:
        no = crack.get("_no")
        polygon = crack.get("polygon_px")
        bbox = crack.get("bbox_px")
        if no is None or not polygon or not bbox:
            continue
        # Manually-added cracks get their own marker color -- a reviewer
        # scanning the map shouldn't have to open every card to tell which
        # markers are AI detections vs their own additions.
        marker_color = MANUAL_MARKER_COLOR_BGR if crack.get("source") == "manual" else MASK_COLOR_BGR

        pts = (np.array(polygon, dtype=np.float64) * scale).round().astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(canvas, [pts], isClosed=True, color=marker_color, thickness=outline_thickness)

        label = str(no)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, text_thickness)
        marker_r = max(radius, int(max(tw, th) / 2) + 4)

        # Clamp to keep the whole marker on-canvas -- a crack near the
        # mosaic's edge (common: this facade type's stitched canvas is full
        # of irregular black cutout margins, see stitching/warp.py) would
        # otherwise get its number circle sliced off by the image border.
        cx = min(max((bbox[0] + bbox[2]) / 2 * scale, marker_r), canvas_w - marker_r)
        cy = min(max((bbox[1] + bbox[3]) / 2 * scale, marker_r), canvas_h - marker_r)

        cv2.circle(canvas, (int(cx), int(cy)), marker_r, marker_color, -1)
        cv2.circle(canvas, (int(cx), int(cy)), marker_r, (255, 255, 255), max(2, text_thickness - 1), cv2.LINE_AA)
        cv2.putText(
            canvas, label, (int(cx - tw / 2), int(cy + th / 2)),
            cv2.FONT_HERSHEY_SIMPLEX, font_scale, (255, 255, 255), text_thickness, cv2.LINE_AA,
        )

    return _to_data_uri(canvas, quality=90)
