"""Tiny server-side SVG charts (no JavaScript): smooth sparklines and donuts."""

from __future__ import annotations

import math
from html import escape
from itertools import count

from markupsafe import Markup

_uid = count(1)

TIER_COLORS = {"fast_path": "#22d3ee", "gpu": "#a78bfa", "cpu": "#f472b6", "cloud": "#fbbf24"}
TIER_NAMES = {"fast_path": "chemin rapide", "gpu": "rapide", "cpu": "lourd", "cloud": "cloud"}


def _smooth_path(points: list[tuple[float, float]]) -> str:
    """Catmull-Rom spline through the points, as cubic Bézier segments (the 'liquid' line)."""
    if len(points) < 2:
        x, y = points[0] if points else (0.0, 0.0)
        return f"M{x:.1f},{y:.1f}"
    d = [f"M{points[0][0]:.1f},{points[0][1]:.1f}"]
    for i in range(len(points) - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1, p2 = points[i], points[i + 1]
        p3 = points[i + 2] if i + 2 < len(points) else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d.append(f"C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}")
    return " ".join(d)


def sparkline(
    values: list[float], color: str = "#a78bfa", width: int = 240, height: int = 44
) -> Markup:
    """Area + line sparkline; a flat line when there is no data."""
    vals = [max(0.0, float(v)) for v in values] or [0.0, 0.0]
    if len(vals) == 1:
        vals = vals * 2
    top = max(vals) or 1.0
    pad = 3.0
    step = (width - 2 * pad) / (len(vals) - 1)
    pts = [
        (pad + i * step, height - pad - (v / top) * (height - 2 * pad)) for i, v in enumerate(vals)
    ]
    line = _smooth_path(pts)
    area = f"{line} L{pts[-1][0]:.1f},{height} L{pts[0][0]:.1f},{height} Z"
    gid = f"sg{next(_uid)}"
    c = escape(color)
    last = pts[-1]
    # every interpolated value is a number or escaped: safe to mark as HTML
    return Markup(  # noqa: S704
        f'<svg class="spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        'aria-hidden="true">'
        f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{c}" stop-opacity=".45"/>'
        f'<stop offset="1" stop-color="{c}" stop-opacity="0"/></linearGradient></defs>'
        f'<path d="{area}" fill="url(#{gid})"/>'
        f'<path d="{line}" fill="none" stroke="{c}" stroke-width="2.2" stroke-linecap="round" '
        f'vector-effect="non-scaling-stroke"/>'
        f'<circle cx="{last[0]:.1f}" cy="{last[1]:.1f}" r="3.2" fill="{c}"/>'
        "</svg>"
    )


def donut(segments: list[tuple[str, float, str]], size: int = 150, thickness: int = 18,
          center_label: str = "") -> Markup:  # fmt: skip
    """Donut chart; segments are (label, value, color)."""
    total = sum(max(0.0, v) for _, v, _ in segments)
    r = (size - thickness) / 2
    circ = 2 * math.pi * r
    parts = [
        f'<svg class="donut" viewBox="0 0 {size} {size}" role="img" '
        f'aria-label="{escape(center_label or "répartition")}">',
        f'<circle cx="{size / 2}" cy="{size / 2}" r="{r:.2f}" fill="none" '
        f'stroke="currentColor" stroke-opacity=".08" stroke-width="{thickness}"/>',
    ]
    offset = 0.0
    gap = 2.5 if len([s for s in segments if s[1] > 0]) > 1 else 0.0
    for label, value, color in segments:
        if total <= 0 or value <= 0:
            continue
        length = circ * value / total
        dash = max(0.0, length - gap)
        parts.append(
            f'<circle cx="{size / 2}" cy="{size / 2}" r="{r:.2f}" fill="none" '
            f'stroke="{escape(color)}" '
            f'stroke-width="{thickness}" stroke-linecap="round" '
            f'stroke-dasharray="{dash:.2f} {circ - dash:.2f}" stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {size / 2} {size / 2})"><title>{escape(label)}</title></circle>'
        )
        offset += length
    parts.append(
        f'<text x="50%" y="48%" text-anchor="middle" dominant-baseline="middle" '
        f'font-size="26" font-weight="760" fill="currentColor">{int(total)}</text>'
        f'<text x="50%" y="66%" text-anchor="middle" font-size="11" fill="currentColor" '
        f'fill-opacity=".6">{escape(center_label)}</text>'
    )
    parts.append("</svg>")
    return Markup("".join(parts))  # noqa: S704 (numbers and escaped labels only)


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)
