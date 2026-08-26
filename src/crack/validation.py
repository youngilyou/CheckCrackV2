"""0.3mm validation protocol (SmartCrack V2 design review section 17,
2026-08-26): matches detected cracks against human-annotated Ground Truth,
computes Recall/Precision/False-Positive/False-Negative + Width/Length/Area
error, and aggregates by whatever stratification tags the GT provides
(width bin, capture distance, angle, lighting, wall material, confounder
presence) -- CLAUDE.local.md #41's "Minimum PoC Success Criteria" made
concrete and width-focused per the design doc.

THIS FILE HAS ZERO REAL VALIDATION DATA BEHIND IT YET. There is no drone,
no Ground Truth crack specimens, no real annotated facade photos -- per the
Phase A/B/C plan already agreed (project_checkcrack_status.md), that's
explicitly Phase B, gated on the drone + reference-crack acquisition the
user is doing separately. This module is the HARNESS: the matching/metric
math, ready to run the instant real GT exists. Running it against synthetic
or placeholder data (see tools/validate_crack_detection.py's --self-test)
proves the CODE is correct -- it is NOT a substitute for real validation and
must never be reported or treated as one (CLAUDE.local.md #9/#26's
never-fabricate principle extended to "never fabricate validation
evidence" too).

Ground Truth schema (one JSON file per test image, see
docs/crack_validation_gt_schema.md for the full spec once real annotation
starts):
    {
      "image_path": "...",
      "cracks": [
        {"gt_id": "GT001", "polygon_px": [[x,y],...],
         "width_mm": 0.35 (or null), "width_px": 4.2 (or null),
         "width_bin": "0.3-0.5" (or null), "distance_category": "10m",
         "lighting_category": "shadow", "wall_material": "concrete"}
      ],
      "non_cracks": [
        {"nc_id": "NC001", "polygon_px": [[x,y],...], "confounder_type": "joint"}
      ]
    }
"non_cracks" are confounders (control joints, shadows, stains) explicitly
labeled NOT a crack, per the shoot-prep checklist -- letting this harness
separately report "confounder false positives" (a detection that landed on
a known joint/shadow) from generic false positives (landed on nothing
annotated at all), which is exactly the signal the 3-stage filter design
needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import Polygon

IOU_MATCH_THRESHOLD = 0.2  # same threshold merge_tiles.py/multiview.py already use for "is this the same object"


def _to_shapely(poly_px) -> Polygon | None:
    poly_px = np.asarray(poly_px, dtype=np.float64)
    if len(poly_px) < 3:
        return None
    poly = Polygon(poly_px)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly if (not poly.is_empty and poly.is_valid and poly.geom_type == "Polygon") else None


@dataclass
class GtCrack:
    gt_id: str
    polygon_px: np.ndarray
    width_mm: float | None = None
    width_px: float | None = None
    width_bin: str | None = None
    distance_category: str | None = None
    lighting_category: str | None = None
    wall_material: str | None = None


@dataclass
class GtNonCrack:
    nc_id: str
    polygon_px: np.ndarray
    confounder_type: str | None = None  # "joint" | "shadow" | "stain" | ...


@dataclass
class MatchResult:
    """One row of the confusion-matrix-with-error-magnitudes this harness
    produces. `kind` is one of TP/FN/FP/CONFOUNDER_FP."""

    kind: str
    gt_id: str | None = None
    detected_crack_id: str | None = None
    iou: float | None = None
    width_error_px: float | None = None
    length_error_px: float | None = None
    area_error_px2: float | None = None
    width_bin: str | None = None
    distance_category: str | None = None
    lighting_category: str | None = None
    wall_material: str | None = None
    confounder_type: str | None = None


def match_detections(
    gt_cracks: list[GtCrack],
    gt_non_cracks: list[GtNonCrack],
    detected,  # list[Crack] (src/common/types.py) -- duck-typed to avoid a hard import cycle
    iou_threshold: float = IOU_MATCH_THRESHOLD,
) -> list[MatchResult]:
    """Greedy IoU matching (same pattern as merge_tiles.py's
    match_crack_ids): each GT crack claims its best-IoU detection above
    threshold (TP), unclaimed GT cracks are FN, unclaimed detections that
    land on a GT non-crack are CONFOUNDER_FP (the joint/shadow/stain
    false-positive signal the 3-stage filter design cares about
    specifically), and unclaimed detections matching nothing at all are
    plain FP."""
    gt_polys = [_to_shapely(g.polygon_px) for g in gt_cracks]
    nc_polys = [_to_shapely(g.polygon_px) for g in gt_non_cracks]
    det_polys = [_to_shapely(d.polygon_px) for d in detected]

    used_gt: set[int] = set()
    used_det: set[int] = set()
    results: list[MatchResult] = []

    # TP: match each GT crack to its best-IoU detection.
    for gi, gp in enumerate(gt_polys):
        if gp is None:
            continue
        best_di, best_iou = -1, 0.0
        for di, dp in enumerate(det_polys):
            if di in used_det or dp is None:
                continue
            inter = gp.intersection(dp).area
            if inter == 0:
                continue
            iou = inter / gp.union(dp).area
            if iou > best_iou:
                best_iou, best_di = iou, di
        gt = gt_cracks[gi]
        if best_di >= 0 and best_iou >= iou_threshold:
            used_gt.add(gi)
            used_det.add(best_di)
            det = detected[best_di]
            width_err = None if gt.width_px is None else float(det.max_width_px - gt.width_px)
            results.append(MatchResult(
                kind="TP", gt_id=gt.gt_id, detected_crack_id=det.crack_id, iou=round(best_iou, 3),
                width_error_px=width_err,
                length_error_px=None,  # filled in by caller if GT carries a length reference; not in base schema
                area_error_px2=float(det.area_px) if gt.width_px is None else None,
                width_bin=gt.width_bin, distance_category=gt.distance_category,
                lighting_category=gt.lighting_category, wall_material=gt.wall_material,
            ))
        else:
            results.append(MatchResult(
                kind="FN", gt_id=gt.gt_id, width_bin=gt.width_bin,
                distance_category=gt.distance_category, lighting_category=gt.lighting_category,
                wall_material=gt.wall_material,
            ))

    # Remaining unclaimed detections: FP, or CONFOUNDER_FP if they land on a
    # known non-crack.
    for di, dp in enumerate(det_polys):
        if di in used_det or dp is None:
            continue
        det = detected[di]
        confounder_type = None
        for nc, ncp in zip(gt_non_cracks, nc_polys):
            if ncp is None:
                continue
            inter = dp.intersection(ncp).area
            if inter > 0 and inter / dp.union(ncp).area >= iou_threshold:
                confounder_type = nc.confounder_type
                break
        results.append(MatchResult(
            kind="CONFOUNDER_FP" if confounder_type else "FP",
            detected_crack_id=det.crack_id, confounder_type=confounder_type,
        ))

    return results


@dataclass
class Metrics:
    group: str
    tp: int = 0
    fn: int = 0
    fp: int = 0
    confounder_fp: int = 0
    width_errors_px: list = field(default_factory=list)

    @property
    def recall(self) -> float | None:
        return None if (self.tp + self.fn) == 0 else self.tp / (self.tp + self.fn)

    @property
    def precision(self) -> float | None:
        denom = self.tp + self.fp + self.confounder_fp
        return None if denom == 0 else self.tp / denom

    @property
    def width_mae_px(self) -> float | None:
        return None if not self.width_errors_px else float(np.mean(np.abs(self.width_errors_px)))


def aggregate(results: list[MatchResult], group_by: str | None = None) -> dict[str, Metrics]:
    """group_by: one of "width_bin"/"distance_category"/"lighting_category"/
    "wall_material", or None for a single overall total. Missing tags fall
    into an "unlabeled" bucket rather than being silently dropped."""
    groups: dict[str, Metrics] = {}

    def group_key(r: MatchResult) -> str:
        if group_by is None:
            return "ALL"
        value = getattr(r, group_by, None)
        return value if value is not None else "unlabeled"

    for r in results:
        key = group_key(r)
        m = groups.setdefault(key, Metrics(group=key))
        if r.kind == "TP":
            m.tp += 1
            if r.width_error_px is not None:
                m.width_errors_px.append(r.width_error_px)
        elif r.kind == "FN":
            m.fn += 1
        elif r.kind == "FP":
            m.fp += 1
        elif r.kind == "CONFOUNDER_FP":
            m.confounder_fp += 1
    return groups
