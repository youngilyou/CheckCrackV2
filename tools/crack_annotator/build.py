"""Build a standalone crack-box-annotation HTML page for one facade mosaic.

Embeds a downscaled JPEG of the mosaic (base64) into template.html so the
page is self-contained (no external file references, matches Artifact
constraints). Boxes drawn in the page export pixel coordinates in the
*original* mosaic's resolution, not the downscaled display resolution.

Usage:
    python tools/crack_annotator/build.py F004 facades/F004/output/F004_analysis_colmap.tif
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import cv2

TEMPLATE_PATH = Path(__file__).parent / "template.html"
OUTPUT_DIR = Path(__file__).parent / "output"
MAX_DISPLAY_DIM = 2000
JPEG_QUALITY = 85


def build(facade_id: str, mosaic_path: str | Path) -> Path:
    mosaic_path = Path(mosaic_path)
    img = cv2.imread(str(mosaic_path))
    if img is None:
        raise FileNotFoundError(f"could not read mosaic: {mosaic_path}")
    orig_h, orig_w = img.shape[:2]

    scale = min(1.0, MAX_DISPLAY_DIM / max(orig_w, orig_h))
    disp_w, disp_h = int(round(orig_w * scale)), int(round(orig_h * scale))
    small = cv2.resize(img, (disp_w, disp_h), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    b64 = base64.b64encode(buf.tobytes()).decode("ascii")

    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = html.replace("F004_analysis_colmap.tif", mosaic_path.name)
    html = html.replace('>F004<', f'>{facade_id}<')
    html = html.replace('"F004"', f'"{facade_id}"')
    html = html.replace('"F004_analysis_colmap.tif"', f'"{mosaic_path.name}"')
    html = html.replace("Crack Box Annotator — F004", f"Crack Box Annotator — {facade_id}")
    html = html.replace("__ORIG_W__", str(orig_w))
    html = html.replace("__ORIG_H__", str(orig_h))
    html = html.replace("__DISP_W__", str(disp_w))
    html = html.replace("__DISP_H__", str(disp_h))
    html = html.replace("__DISP_SCALE_PCT__", str(round(100 * scale)))
    html = html.replace("__IMAGE_B64__", b64)

    if "__" in html:
        leftover = [line for line in html.splitlines() if "__" in line and "font" not in line]
        raise RuntimeError(f"unsubstituted placeholder(s) remain: {leftover[:3]}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{facade_id}_annotator.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python tools/crack_annotator/build.py <facade_id> <mosaic_path>")
        sys.exit(1)
    out = build(sys.argv[1], sys.argv[2])
    print(f"wrote {out}")
