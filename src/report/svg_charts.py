"""Inline SVG pie/donut charts for the HTML report (src/report/pdf_report.py).

Rendered as real vector markup embedded straight in the page, not a
rasterized PNG (the previous C# generator's WPF-drawn-then-rasterized
approach) — WeasyPrint prints inline SVG natively, so this is both simpler
(no temp files, no PPI/DPI accounting for print quality) and sharper at any
zoom level.

Technique: a single <circle> with a dashed stroke, one dash segment per
slice (the standard SVG "donut chart" trick). Stroke width = radius makes
the ring fill all the way to the center, i.e. a pie chart — donut and pie
are the same function with a different `inner_ratio`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class ChartSlice:
    label: str
    value: float
    color: str  # any CSS color, e.g. "#1f6feb"


def donut_or_pie_svg(
    slices: list[ChartSlice],
    size: int = 200,
    inner_ratio: float = 0.6,
    stroke_bg: str = "#eef0f3",
) -> str:
    """`inner_ratio=0` collapses the hole to a point (pie chart). Returns a
    self-contained <svg> string with `size` as both width and height."""
    total = sum(s.value for s in slices)
    radius = size / 2 * 0.82
    center = size / 2
    stroke_width = radius * (1.0 - max(0.0, min(0.95, inner_ratio)))
    draw_radius = radius - stroke_width / 2
    circumference = 2 * math.pi * draw_radius

    parts = [
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
        f'xmlns="http://www.w3.org/2000/svg">',
        f'<circle cx="{center}" cy="{center}" r="{draw_radius:.2f}" fill="none" '
        f'stroke="{stroke_bg}" stroke-width="{stroke_width:.2f}" />',
    ]

    if total <= 0:
        parts.append("</svg>")
        return "\n".join(parts)

    offset = 0.0
    # start at 12 o'clock, go clockwise: rotate the whole circle -90deg
    for s in slices:
        if s.value <= 0:
            continue
        frac = s.value / total
        dash = frac * circumference
        gap = circumference - dash
        parts.append(
            f'<circle cx="{center}" cy="{center}" r="{draw_radius:.2f}" fill="none" '
            f'stroke="{s.color}" stroke-width="{stroke_width:.2f}" '
            f'stroke-dasharray="{dash:.2f} {gap:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {center} {center})" '
            f'stroke-linecap="butt" />'
        )
        offset += dash

    parts.append("</svg>")
    return "\n".join(parts)


def legend_rows(slices: list[ChartSlice]) -> list[dict]:
    """Precomputed {label, value, pct, color} rows for the template's legend
    list — keeps percentage math out of Jinja2."""
    total = sum(s.value for s in slices)
    rows = []
    for s in slices:
        pct = (s.value / total * 100.0) if total > 0 else 0.0
        rows.append({"label": s.label, "value": s.value, "pct": round(pct, 1), "color": s.color})
    return rows
