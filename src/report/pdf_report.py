"""HTML/CSS -> PDF inspection report generator (CLAUDE.local.md #40 Phase 8).

Replaces the previous C#/PDFsharp-MigraDoc generator: MigraDoc's Word-style
flow/table layout model cannot produce the print-quality, designed-cover-page
look the reference template needs, whereas HTML/CSS gets there directly with
ordinary web layout (grid, typography, precise image placement). Rendered via
WeasyPrint, which turns real CSS into a paginated PDF.

Windows note: WeasyPrint needs Pango/cairo/gobject native libraries that a
plain "pip install weasyprint" does NOT provide on Windows (verified empirically:
it imports as a Python package but crashes on first use with "cannot load
library 'libgobject-2.0-0'"). The conda-forge build bundles those libraries
under <env>/Library/bin, so Windows installs must go through conda, not pip --
see scripts/setup_dev_machine.ps1. `_ensure_native_libs()` below makes that
work from a plain subprocess launch (this script is always invoked as a
subprocess, never through an activated shell) by prepending the conda env's
Library/bin to PATH before weasyprint is imported.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import jinja2
import numpy as np


def _ensure_native_libs() -> None:
    if sys.platform != "win32":
        return
    conda_prefix = Path(sys.prefix)
    lib_bin = conda_prefix / "Library" / "bin"
    fonts_dir = conda_prefix / "Library" / "etc" / "fonts"
    if lib_bin.is_dir():
        os.environ["PATH"] = str(lib_bin) + os.pathsep + os.environ.get("PATH", "")
    if fonts_dir.is_dir():
        os.environ.setdefault("FONTCONFIG_PATH", str(fonts_dir))


_ensure_native_libs()

try:
    from weasyprint import HTML
except OSError as exc:  # native libs still missing -- fail with an actionable Korean message
    raise ImportError(
        "WeasyPrint의 네이티브 라이브러리(Pango/cairo/gobject)를 찾을 수 없습니다. "
        "Windows에서는 'pip install weasyprint'만으로는 동작하지 않고 "
        "'conda install -c conda-forge weasyprint'로 설치해야 합니다 "
        "(scripts/setup_dev_machine.ps1 참고). "
        f"원본 오류: {exc}"
    ) from exc

from src.common.atomic_io import atomic_write_json  # noqa: E402 (import order: native-lib bootstrap must run first)
from src.common.imageio import imread_unicode  # noqa: E402
from src.crack.review import apply_review  # noqa: E402
from src.report.crack_crops import generate_crack_crops, generate_crack_map  # noqa: E402
from src.report.svg_charts import ChartSlice, donut_or_pie_svg, legend_rows  # noqa: E402

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_JINJA_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=jinja2.select_autoescape(["html"]),
)

CHART_PALETTE = ["#4c8bf5", "#f5a623", "#e04f4f", "#8e44ad", "#1abc9c", "#7f8c8d"]
MALGUN_PATH = Path(r"C:\Windows\Fonts\malgun.ttf")
MALGUN_BOLD_PATH = Path(r"C:\Windows\Fonts\malgunbd.ttf")
COVER_ILLUSTRATION_PATH = Path(__file__).parent / "assets" / "cover_illustration.png"
FOOTER_BRAND = "AI SmartCrack | Drone AI Building Inspection"


def _font_data_uri(path: Path) -> str | None:
    if not path.exists():
        return None
    return "data:font/ttf;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _cover_illustration_uri() -> str | None:
    """The same fixed drone+building illustration on every report's cover
    (user's explicit request: one consistent graphic, not a per-facade photo).
    Re-encoded to a downscaled JPEG rather than embedding the ~2MB source PNG
    as-is -- purely a file-size optimization for a decorative image, the
    source file on disk is left untouched."""
    if not COVER_ILLUSTRATION_PATH.exists():
        return None
    img = imread_unicode(str(COVER_ILLUSTRATION_PATH), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    scale = min(1.0, 1400 / w)
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii") if ok else None


def _pick(output_dir: Path, facade_id: str, *suffixes: str) -> Path | None:
    for suffix in suffixes:
        candidate = output_dir / f"{facade_id}{suffix}"
        if candidate.exists():
            return candidate
    return None


def _read_json(path: Path | None) -> dict | list | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


@dataclass
class FacadeSnapshot:
    facade_id: str
    output_dir: Path
    quality: dict | None
    quality_colmap: dict | None
    colmap: dict | None
    cracks: list[dict]
    raw_crack_count: int
    reviewed_by: str | None
    reviewed_at: str | None
    source_images: list[dict]
    analysis_path: Path | None
    used_colmap: bool
    crack_mask_path: Path | None


def load_facade_snapshot(output_dir: str | Path, facade_id: str) -> FacadeSnapshot:
    output_dir = Path(output_dir)
    analysis_colmap = _pick(output_dir, facade_id, "_analysis_colmap.tif")
    analysis_plain = _pick(output_dir, facade_id, "_analysis.tif")
    used_colmap = analysis_colmap is not None
    analysis_path = analysis_colmap or analysis_plain

    quality = _read_json(output_dir / f"{facade_id}_quality_report.json")
    quality_colmap = _read_json(output_dir / f"{facade_id}_quality_report_colmap.json")
    colmap = _read_json(output_dir / f"{facade_id}_colmap_report.json")
    raw_cracks = _read_json(output_dir / f"{facade_id}_cracks.json") or []
    raw_cracks = raw_cracks if isinstance(raw_cracks, list) else []
    # {facade_id}_crack_review.json (written by the viewer's review UI) is
    # entirely optional -- a facade nobody has reviewed yet just renders every
    # AI detection unchanged, exactly like before this feature existed.
    review = _read_json(output_dir / f"{facade_id}_crack_review.json")
    review = review if isinstance(review, dict) else None
    cracks = apply_review(raw_cracks, review)
    source_images = _read_json(output_dir / f"{facade_id}_source_images.json") or []
    crack_mask_path = _pick(output_dir, facade_id, "_crack_mask.tif")

    return FacadeSnapshot(
        facade_id=facade_id,
        output_dir=output_dir,
        quality=quality if isinstance(quality, dict) else None,
        quality_colmap=quality_colmap if isinstance(quality_colmap, dict) else None,
        colmap=colmap if isinstance(colmap, dict) else None,
        cracks=cracks if isinstance(cracks, list) else [],
        raw_crack_count=len(raw_cracks),
        reviewed_by=review.get("reviewed_by") if review else None,
        reviewed_at=review.get("reviewed_at") if review else None,
        source_images=source_images if isinstance(source_images, list) else [],
        analysis_path=analysis_path,
        used_colmap=used_colmap,
        crack_mask_path=crack_mask_path,
    )


def build_width_distribution(cracks: list[dict], has_calibration: bool) -> list[ChartSlice]:
    """Same rule the previous C# generator used and the user already
    confirmed: calibration present -> fixed, meaningful 0.3mm-based mm
    buckets; no calibration (the common case) -> buckets computed from this
    dataset's own actual min/max px width, never a mm threshold copy-pasted
    onto px values (that collapsed real data into one bucket, confirmed by
    direct testing earlier in this project)."""
    if has_calibration:
        edges = [0.2, 0.3, 0.5, 1.0]
        labels = ["<0.2mm", "0.2-0.3mm", "0.3-0.5mm", "0.5-1.0mm", "1.0mm+"]
        values = [0.0] * len(labels)
        for c in cracks:
            w = c.get("max_width_mm") or 0.0
            idx = len(edges)
            for i, e in enumerate(edges):
                if w < e:
                    idx = i
                    break
            values[idx] += 1
        return [ChartSlice(l, v, CHART_PALETTE[i % len(CHART_PALETTE)]) for i, (l, v) in enumerate(zip(labels, values))]

    widths = [c.get("max_width_px", 0.0) for c in cracks]
    if not widths:
        return []
    lo, hi = min(widths), max(widths)
    if hi - lo < 0.01:
        return [ChartSlice(f"{lo:.1f}px", float(len(cracks)), CHART_PALETTE[0])]

    bucket_count = 4
    step = (hi - lo) / bucket_count
    values = [0.0] * bucket_count
    labels = []
    for i in range(bucket_count):
        b_lo = lo + step * i
        b_hi = hi if i == bucket_count - 1 else lo + step * (i + 1)
        labels.append(f"{b_lo:.1f}-{b_hi:.1f}px")
    for w in widths:
        idx = min(bucket_count - 1, int((w - lo) / step))
        values[idx] += 1
    return [ChartSlice(l, v, CHART_PALETTE[i % len(CHART_PALETTE)]) for i, (l, v) in enumerate(zip(labels, values))]


def build_confidence_tiers(cracks: list[dict]) -> list[ChartSlice]:
    high = sum(1 for c in cracks if c.get("confidence", 0) >= 0.7)
    mid = sum(1 for c in cracks if 0.4 <= c.get("confidence", 0) < 0.7)
    low = sum(1 for c in cracks if c.get("confidence", 0) < 0.4)
    return [
        ChartSlice("높음(\u22650.7)", float(high), "#2ecc71"),
        ChartSlice("보통(0.4-0.7)", float(mid), "#f1c40f"),
        ChartSlice("낮음(<0.4)", float(low), "#e74c3c"),
    ]


def _mosaic_thumbnail_only(snapshot: FacadeSnapshot) -> str | None:
    """Downscaled mosaic thumbnail with no crack-crop work -- used by the
    building report's side-group grid, which only ever shows one small
    representative image per side and never a single crack, so running the
    full per-crack crop+mask pass here (as `_mosaic_section_data` does for
    the facade report) would decode the whole mosaic just to throw the
    result away."""
    if snapshot.analysis_path is None:
        return None
    img = imread_unicode(str(snapshot.analysis_path), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    scale = min(1.0, 1200 / max(h, w))
    thumb = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA) if scale < 1.0 else img
    ok, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii") if ok else None


def _mosaic_section_data(snapshot: FacadeSnapshot, numbered_cracks: list[dict]) -> tuple[str | None, str | None, dict]:
    """Loads the (possibly huge, tens-of-thousands-of-px) analysis mosaic
    EXACTLY ONCE and reuses that single in-memory array for the downscaled
    section-02 thumbnail, the whole-facade numbered crack map, and every
    per-crack crop -- re-opening the full TIFF per use is the exact
    memory-explosion pattern already fixed elsewhere in this project for
    large-image handling. `numbered_cracks` must already carry `_no` (see
    generate_facade_report) so the map's markers match the detail cards."""
    if snapshot.analysis_path is None:
        return None, None, {}
    img = imread_unicode(str(snapshot.analysis_path), cv2.IMREAD_COLOR)
    if img is None:
        return None, None, {}

    h, w = img.shape[:2]
    scale = min(1.0, 1600 / max(h, w))
    thumb = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA) if scale < 1.0 else img
    ok, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 88])
    thumb_uri = "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii") if ok else None

    crack_map_uri = generate_crack_map(img, numbered_cracks) if numbered_cracks else None
    crops = generate_crack_crops(img, snapshot.cracks)
    return thumb_uri, crack_map_uri, crops


def _crack_metrics(cracks: list[dict]) -> dict:
    if not cracks:
        return {}
    lengths = [c.get("length_px", 0.0) for c in cracks]
    widths = [c.get("max_width_px", 0.0) for c in cracks]
    areas = [c.get("area_px", 0.0) for c in cracks]
    confidences = [c.get("confidence", 0.0) for c in cracks]
    has_calibration = any(c.get("max_width_mm") is not None for c in cracks)
    precision_count = sum(1 for c in cracks if c.get("severity") == "정밀점검대상")
    minor_count = sum(1 for c in cracks if c.get("severity") == "경미")
    return {
        "count": len(cracks),
        "avg_confidence": sum(confidences) / len(confidences),
        "avg_length_px": sum(lengths) / len(lengths),
        "max_width_px": max(widths),
        "avg_area_px": sum(areas) / len(areas),
        "has_calibration": has_calibration,
        "precision_count": precision_count,
        "minor_count": minor_count,
        "observed": sum(1 for c in cracks if c.get("observation_state") == "OBSERVED"),
        "occluded": sum(1 for c in cracks if c.get("observation_state") == "OCCLUDED"),
        "high_confidence": sum(1 for c in confidences if c >= 0.7),
        "low_confidence": sum(1 for c in confidences if c < 0.4),
    }


def _fmt(value, pattern="{:.1f}", empty="-"):
    return pattern.format(value) if value is not None else empty


def _quality_rows(quality: dict | None, colmap: dict | None, used_colmap: bool) -> list[tuple[str, str]]:
    if quality is None:
        return []
    return [
        ("소스 이미지 수", str(quality.get("image_count", "-"))),
        ("매칭된 이미지 쌍", f"{quality.get('matched_pair_count', '-')} (실패 {quality.get('failed_pair_count', '-')})"),
        ("커버리지 비율", _fmt(quality.get("coverage_ratio", None) and quality["coverage_ratio"] * 100, "{:.1f}%")),
        ("CM 폴백 실행 여부", "예" if colmap is not None else "아니오"),
        ("CM 정밀 보정 이미지 반영", "적용됨 (위 이미지에 반영)" if used_colmap else "미적용 (등록 이미지 부족 등으로 기본 스티칭 사용)"),
    ]


def _render(template_name: str, context: dict) -> bytes:
    template = _JINJA_ENV.get_template(template_name)
    html = template.render(**context)
    return HTML(string=html, base_url=str(_TEMPLATE_DIR)).write_pdf()


def _atomic_write_pdf(pdf_bytes: bytes, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_bytes(pdf_bytes)
    os.replace(tmp_path, out_path)


def generate_facade_report(output_dir: str | Path, facade_id: str, building_id: str = "B000") -> Path:
    snapshot = load_facade_snapshot(output_dir, facade_id)
    quality = snapshot.quality_colmap or snapshot.quality
    metrics = _crack_metrics(snapshot.cracks)

    # Numbered by the same \uae38\uc774(px) descending order the detail cards use,
    # assigned BEFORE the map is drawn so a marker's number on the whole-facade
    # map always points at the matching card below it.
    cracks_sorted = sorted(snapshot.cracks, key=lambda c: c.get("length_px", 0.0), reverse=True)
    for i, c in enumerate(cracks_sorted, start=1):
        c["_no"] = i

    mosaic_uri, crack_map_uri, crops = _mosaic_section_data(snapshot, cracks_sorted)

    width_slices = build_width_distribution(snapshot.cracks, metrics.get("has_calibration", False)) if snapshot.cracks else []
    confidence_slices = build_confidence_tiers(snapshot.cracks) if snapshot.cracks else []

    for c in cracks_sorted:
        crop = crops.get(c.get("crack_id"))
        c["_context_uri"] = crop.context_uri if crop else None
        c["_zoom_uri"] = crop.zoom_uri if crop else None
        c["_source_preview"] = ", ".join(c.get("source_image_ids", [])[:2]) + (
            " \uc678" if len(c.get("source_image_ids", [])) > 2 else ""
        )

    context = {
        "malgun_regular_uri": _font_data_uri(MALGUN_PATH),
        "malgun_bold_uri": _font_data_uri(MALGUN_BOLD_PATH),
        "report_kind": "facade",
        "report_no": f"CC-{datetime.now():%Y%m%d}-{facade_id}",
        "title_label": facade_id,
        "cover_illustration_uri": _cover_illustration_uri(),
        "issue_date": f"{datetime.now():%Y. %m. %d.}",
        "footer_brand": FOOTER_BRAND,
        "building_name_value": "작업중",
        "quality_rows": _quality_rows(quality, snapshot.colmap, snapshot.used_colmap),
        "mosaic_uri": mosaic_uri,
        "used_colmap": snapshot.used_colmap,
        "crack_map_uri": crack_map_uri,
        "cracks": cracks_sorted,
        "raw_crack_count": snapshot.raw_crack_count,
        "reviewed_by": snapshot.reviewed_by,
        "reviewed_at": snapshot.reviewed_at,
        "metrics": metrics,
        "width_chart_svg": donut_or_pie_svg(width_slices, inner_ratio=0.0) if width_slices else None,
        "width_legend": legend_rows(width_slices),
        "width_title": "균열 폭 분포 (mm)" if metrics.get("has_calibration") else "균열 폭 분포 (px, calibration 없음)",
        "confidence_chart_svg": donut_or_pie_svg(confidence_slices, inner_ratio=0.6) if confidence_slices else None,
        "confidence_legend": legend_rows(confidence_slices),
        "deliverables": _facade_deliverables(snapshot),
    }

    pdf_bytes = _render("report.html", context)
    out_path = snapshot.output_dir / f"{facade_id}_report.pdf"
    _atomic_write_pdf(pdf_bytes, out_path)
    return out_path


def _facade_deliverables(snapshot: FacadeSnapshot) -> list[tuple[str, bool, str]]:
    analysis_name = snapshot.analysis_path.name if snapshot.analysis_path else ""
    visual_path = _pick(snapshot.output_dir, snapshot.facade_id, "_visual_colmap.tif", "_visual.tif")
    return [
        ("외벽 스티칭 결과 (분석용)", snapshot.analysis_path is not None, analysis_name),
        ("외벽 스티칭 결과 (열람용)", visual_path is not None, visual_path.name if visual_path else ""),
        ("크랙 위치도", snapshot.crack_mask_path is not None, f"{snapshot.facade_id}_crack_mask.tif"),
        ("크랙 데이터 (JSON)", bool(snapshot.cracks), f"{snapshot.facade_id}_cracks.json"),
        ("스티칭 품질 리포트 (JSON)", snapshot.quality is not None, f"{snapshot.facade_id}_quality_report.json"),
        ("정밀 보정 리포트 (JSON)", snapshot.colmap is not None, f"{snapshot.facade_id}_colmap_report.json"),
        ("본 PDF 보고서", True, f"{snapshot.facade_id}_report.pdf"),
    ]


# ---------------------------------------------------------------------------
# Building / complex aggregate report
# ---------------------------------------------------------------------------


def generate_building_report(manifest_path: str | Path, reports_dir: str | Path) -> Path:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    complex_name: str = manifest["complex_name"]
    building_name: str | None = manifest.get("building_name")
    facade_inputs: list[dict] = manifest["facades"]
    if not facade_inputs:
        raise ValueError("포함할 facade가 없습니다.")

    entries = []
    for f in facade_inputs:
        snap = load_facade_snapshot(f["output_dir"], f["facade_id"])
        entries.append({"side": f.get("side") or "(미지정)", "snapshot": snap})

    label = f"{complex_name} {building_name}" if building_name else complex_name

    all_cracks: list[dict] = []
    for e in entries:
        for c in e["snapshot"].cracks:
            c = dict(c)
            c["_facade_id"] = e["snapshot"].facade_id
            all_cracks.append(c)
    metrics = _crack_metrics(all_cracks)

    by_side: dict[str, list[dict]] = {}
    for e in entries:
        by_side.setdefault(e["side"], []).append(e)
    side_groups = []
    for side, group in sorted(by_side.items()):
        thumb_uri = None
        for e in group:
            snap = e["snapshot"]
            if snap.analysis_path is not None:
                thumb_uri = _mosaic_thumbnail_only(snap)
                break
        side_groups.append({
            "side": side,
            "count": len(group),
            "thumb_uri": thumb_uri,
            "image_count": sum(e["snapshot"].quality.get("image_count", 0) if e["snapshot"].quality else 0 for e in group),
        })

    width_slices = build_width_distribution(all_cracks, metrics.get("has_calibration", False)) if all_cracks else []
    confidence_slices = build_confidence_tiers(all_cracks) if all_cracks else []
    side_crack_counts = []
    for side, group in sorted(by_side.items()):
        facade_ids = {e["snapshot"].facade_id for e in group}
        side_crack_counts.append({"label": side, "value": sum(1 for c in all_cracks if c["_facade_id"] in facade_ids)})

    top_cracks = sorted(all_cracks, key=lambda c: c.get("length_px", 0.0), reverse=True)[:30]
    for c in top_cracks:
        c["_source_preview"] = ", ".join(c.get("source_image_ids", [])[:2]) + (
            " \uc678" if len(c.get("source_image_ids", [])) > 2 else ""
        )

    deliverable_rows = [
        (
            e["snapshot"].facade_id,
            e["side"],
            e["snapshot"].analysis_path is not None,
            len(e["snapshot"].cracks),
        )
        for e in entries
    ]

    context = {
        "malgun_regular_uri": _font_data_uri(MALGUN_PATH),
        "malgun_bold_uri": _font_data_uri(MALGUN_BOLD_PATH),
        "report_kind": "building",
        "report_no": f"CC-BLD-{datetime.now():%Y%m%d}-{_sanitize(label)}",
        "title_label": label,
        "cover_illustration_uri": _cover_illustration_uri(),
        "issue_date": f"{datetime.now():%Y. %m. %d.}",
        "footer_brand": FOOTER_BRAND,
        "building_name_value": label,
        "facade_count": len(entries),
        "side_count": len(by_side),
        "stitched_count": sum(1 for e in entries if e["snapshot"].analysis_path is not None),
        "total_images": sum(e["snapshot"].quality.get("image_count", 0) if e["snapshot"].quality else 0 for e in entries),
        "side_groups": side_groups,
        "cracks": top_cracks,
        "cracks_truncated": len(all_cracks) > 30,
        "metrics": metrics,
        "width_chart_svg": donut_or_pie_svg(width_slices, inner_ratio=0.0) if width_slices else None,
        "width_legend": legend_rows(width_slices),
        "width_title": "균열 폭 분포 (mm, 전체)" if metrics.get("has_calibration") else "균열 폭 분포 (px, 전체, calibration 없음)",
        "confidence_chart_svg": donut_or_pie_svg(confidence_slices, inner_ratio=0.6) if confidence_slices else None,
        "confidence_legend": legend_rows(confidence_slices),
        "side_crack_counts": side_crack_counts,
        "deliverable_rows": deliverable_rows,
        "raw_crack_count": sum(e["snapshot"].raw_crack_count for e in entries),
        "reviewed_facade_count": sum(1 for e in entries if e["snapshot"].reviewed_by),
        "total_facade_count_for_review": len(entries),
    }

    pdf_bytes = _render("report.html", context)
    safe_label = _sanitize(f"{complex_name}_{building_name}" if building_name else complex_name)
    out_path = Path(reports_dir) / f"{safe_label}_종합보고서.pdf"
    _atomic_write_pdf(pdf_bytes, out_path)
    return out_path


def _sanitize(name: str) -> str:
    invalid = '<>:"/\\|?*'
    for ch in invalid:
        name = name.replace(ch, "_")
    return name
