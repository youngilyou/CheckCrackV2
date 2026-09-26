"""Long-lived helper for CheckCrackViewer's 결과 보기: "I clicked HERE on the stitched mosaic -- where
is that exact spot in the original photo, and how far off is the marker?"

Why (2026-09-26, 사용자 요구: "스티칭에서 선택한 곳과 원본이미지 위치 표시 오차"): the viewer used to put the
marker on the original photo at H^-1(click), the flat-plane homography inverse. Measured on V009
(60 wall clicks, marker vs the displayed dense mosaic, image-content NCC): median 7.2 px (=7 cm; the
original photo is ~3.8x finer than the 1 cm/px mosaic, so ~27 raw px), p90 14 px, max 24 px -- because
the displayed mosaic is depth-correct while a real wall is not one plane (src/geometry/depth_mapping.py).

This process inverts the depth mapping instead: it finds the raw pixel whose OWN depth puts it on the
canvas at the clicked spot, and it MEASURES the resulting error independently of that construction
(src/crack/position_check.py: place the photo's pixels around the marker onto the canvas and see how
far they sit from the displayed mosaic at the click). Both the old flat-homography marker and the
depth marker are measured so the improvement is visible.

Protocol (one JSON object per line on stdin -> one JSON object per line on stdout):
  ready line : {"ready": true, "depth": <bool>, "mosaic": "<file name>"}
  request    : {"id": 1, "x": 3086, "y": 2004}           (canvas / mosaic pixel)
  response   : {"id": 1, "image_id": "...", "raw_width": W, "raw_height": H,
                "flat":  {"x":..,"y":..},                (old marker, always)
                "depth": {"x":..,"y":..,"residual_px":..} | null,
                "flat_error":  {"offset_px","dx","dy","ncc"} | null,
                "depth_error": {...} | null,
                "px_per_m": <float|null>, "calibrated": <bool>}
              or {"id": 1, "error": "<reason>"}
An error value of null means "could not be verified" (textureless wall etc.), never 0.

Usage: python tools/click_locator.py <output_dir> <facade_id>
"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

import cv2
import numpy as np

warnings.filterwarnings("ignore")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crack.position_check import check_position  # noqa: E402
from src.geometry.depth_mapping import load_mapper_from_sidecar  # noqa: E402


def _pick(output_dir: Path, facade_id: str, *suffixes: str) -> Path | None:
    for suffix in suffixes:
        p = output_dir / f"{facade_id}{suffix}"
        if p.exists():
            return p
    return None


def _imread(path: Path, flags: int):
    return cv2.imdecode(np.fromfile(str(path), np.uint8), flags)


def _reply(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class Locator:
    def __init__(self, output_dir: Path, facade_id: str) -> None:
        self.output_dir = output_dir
        self.facade_id = facade_id
        mosaic_path = _pick(output_dir, facade_id, "_analysis_colmap_dense.tif", "_analysis_colmap.tif", "_analysis.tif")
        hom_path = _pick(output_dir, facade_id, "_homographies_colmap_dense.json", "_homographies_colmap.json", "_homographies.json")
        own_path = _pick(output_dir, facade_id, "_seam_owner_map_colmap_dense.png", "_seam_owner_map_colmap.png", "_seam_owner_map.png")
        idx_path = _pick(output_dir, facade_id, "_seam_owner_index_colmap_dense.json", "_seam_owner_index_colmap.json", "_seam_owner_index.json")
        if None in (mosaic_path, hom_path, own_path, idx_path):
            raise FileNotFoundError("mosaic / homographies / seam owner artifacts missing")
        self.mosaic_name = mosaic_path.name
        self.mosaic = _imread(mosaic_path, cv2.IMREAD_GRAYSCALE)
        self.hom = json.loads(hom_path.read_text(encoding="utf-8"))
        self.owner = _imread(own_path, cv2.IMREAD_UNCHANGED)
        self.index = json.loads(idx_path.read_text(encoding="utf-8"))
        src_path = output_dir / f"{facade_id}_source_images.json"
        from src.common.paths import resolve_source_image

        self.paths = {
            e["image_id"]: (resolve_source_image(e["file_path"], output_dir) or e["file_path"])
            for e in json.loads(src_path.read_text(encoding="utf-8"))
        }
        # The depth mapping only describes a DENSE mosaic (a flat mosaic is drawn by the homographies).
        self.mapper = load_mapper_from_sidecar(output_dir, facade_id) if "colmap_dense" in mosaic_path.name else None
        self.px_per_m = None
        self.calibrated = False
        scale_path = output_dir / f"{facade_id}_scale_colmap.json"
        if scale_path.exists():
            s = json.loads(scale_path.read_text(encoding="utf-8"))
            self.calibrated = bool(s.get("calibrated", False))
            self.px_per_m = float(s["px_per_m"]) if s.get("px_per_m") else None
        self._gray: dict[str, np.ndarray] = {}

    def _raw_gray(self, image_id: str) -> np.ndarray | None:
        if image_id not in self._gray:
            if len(self._gray) >= 6:
                self._gray.pop(next(iter(self._gray)))
            path = self.paths.get(image_id)
            self._gray[image_id] = _imread(Path(path), cv2.IMREAD_GRAYSCALE) if path and Path(path).exists() else None
        return self._gray[image_id]

    # ---- depth inversion: raw pixel q with depth-mapped canvas position == click -----------------------
    def _forward(self, image_id: str, q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return self.mapper.map_points(image_id, np.atleast_2d(q), depth_radius=1)

    def _newton(self, image_id: str, click: np.ndarray, q: np.ndarray, iters: int = 10) -> tuple[np.ndarray, float] | None:
        best = None
        for _ in range(iters):
            f, ok = self._forward(image_id, q)
            if not ok[0]:
                return best
            r = click - f[0]
            res = float(np.hypot(*r))
            if best is None or res < best[1]:
                best = (q.copy(), res)
            if res < 0.25:
                return q, res
            d = 3.0
            fx, okx = self._forward(image_id, q + [d, 0])
            fy, oky = self._forward(image_id, q + [0, d])
            if not (okx[0] and oky[0]):
                return best
            J = np.column_stack([(fx[0] - f[0]) / d, (fy[0] - f[0]) / d])
            if abs(np.linalg.det(J)) < 1e-9:
                return best
            step = np.linalg.solve(J, r)
            n = float(np.hypot(*step))
            if n > 80:
                step *= 80 / n
            q = q + step
        return best

    def _invert_depth(self, image_id: str, click: np.ndarray, q0: np.ndarray) -> tuple[np.ndarray, float] | None:
        found = self._newton(image_id, click, q0)
        if found is not None and found[1] < 1.0:
            return found
        # Newton failed (depth discontinuity / invalid start): coarse local search, then refine.
        offs = np.arange(-240, 241, 8.0)
        gx, gy = np.meshgrid(q0[0] + offs, q0[1] + offs)
        pts = np.column_stack([gx.ravel(), gy.ravel()])
        f, ok = self.mapper.map_points(image_id, pts, depth_radius=1)
        if not ok.any():
            return found
        d = np.where(ok, np.hypot(f[:, 0] - click[0], f[:, 1] - click[1]), np.inf)
        k = int(np.argmin(d))
        refined = self._newton(image_id, click, pts[k])
        cand = refined if refined is not None else (pts[k], float(d[k]))
        return cand if cand[1] < 1.0 else (found if found is not None and found[1] < cand[1] else cand)

    def locate(self, x: int, y: int) -> dict:
        h, w = self.owner.shape[:2]
        if not (0 <= x < w and 0 <= y < h):
            return {"error": "클릭 위치가 모자이크 범위 밖"}
        o = int(self.owner[y, x])
        if o == 0:
            return {"error": "관측되지 않은 영역"}
        image_id = self.index[o - 1]
        entry = self.hom.get(image_id)
        raw = self._raw_gray(image_id)
        if entry is None or raw is None:
            return {"error": f"{image_id} 원본 파일/호모그래피 없음"}

        click = np.array([float(x), float(y)])
        Hm = np.array(entry["H"], dtype=np.float64)
        p = np.linalg.inv(Hm) @ np.array([click[0], click[1], 1.0])
        q_flat = p[:2] / p[2]

        def flat_fn(pts: np.ndarray):
            return cv2.perspectiveTransform(pts.reshape(-1, 1, 2), Hm).reshape(-1, 2), np.ones(len(pts), dtype=bool)

        probe, _ = flat_fn(np.array([q_flat, q_flat + [20, 0], q_flat + [0, 20]]))
        ppr = float(np.sqrt(abs(np.cross(probe[1] - probe[0], probe[2] - probe[0]))) / 20.0)  # canvas px per raw px
        ppr = ppr if np.isfinite(ppr) and ppr > 1e-3 else 0.27

        def measure(map_fn, q: np.ndarray):
            c = check_position(map_fn, raw, (float(q[0]), float(q[1])), (float(click[0]), float(click[1])), self.mosaic, ppr)
            return None if c is None else {"offset_px": round(c.offset_px, 2), "dx": round(c.dx, 2), "dy": round(c.dy, 2),
                                            "ncc": round(c.ncc, 3)}

        out = {
            "image_id": image_id, "raw_width": int(entry["width"]), "raw_height": int(entry["height"]),
            "flat": {"x": round(float(q_flat[0]), 1), "y": round(float(q_flat[1]), 1)},
            "depth": None, "flat_error": measure(flat_fn, q_flat), "depth_error": None,
            "px_per_m": self.px_per_m, "calibrated": self.calibrated,
        }
        if self.mapper is not None and self.mapper.has_image(image_id):
            inv = self._invert_depth(image_id, click, q_flat)
            if inv is not None and inv[1] < 1.0:
                q_depth, residual = inv
                out["depth"] = {"x": round(float(q_depth[0]), 1), "y": round(float(q_depth[1]), 1),
                                "residual_px": round(residual, 2)}
                out["depth_error"] = measure(lambda pts: self.mapper.map_points(image_id, pts, depth_radius=1), q_depth)
        return out


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: click_locator.py <output_dir> <facade_id>", file=sys.stderr)
        sys.exit(2)
    try:
        locator = Locator(Path(sys.argv[1]), sys.argv[2])
    except Exception as exc:  # startup failure is reported, not crashed silently
        _reply({"ready": False, "error": str(exc)})
        sys.exit(1)
    _reply({"ready": True, "depth": locator.mapper is not None, "mosaic": locator.mosaic_name})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            result = locator.locate(int(req["x"]), int(req["y"]))
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
            req = {}
        result["id"] = req.get("id") if isinstance(req, dict) else None
        _reply(result)


if __name__ == "__main__":
    main()
