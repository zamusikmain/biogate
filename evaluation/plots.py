"""Dependency-free SVG charts: DET (log FAR vs log FRR) and ROC (FAR vs 1-FRR)."""

from __future__ import annotations

import math
from html import escape

WIDTH, HEIGHT, PAD = 640, 480, 64
COLORS = ["#2563eb", "#dc2626", "#059669", "#7c3aed"]


def _frame(title: str, x_label: str, y_label: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" font-family="sans-serif" '
        f'font-size="12">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="white"/>',
        f'<text x="{WIDTH / 2}" y="24" text-anchor="middle" font-size="15">{escape(title)}</text>',
        f'<text x="{WIDTH / 2}" y="{HEIGHT - 12}" text-anchor="middle">{escape(x_label)}</text>',
        f'<text x="16" y="{HEIGHT / 2}" text-anchor="middle" transform="rotate(-90 16 {HEIGHT / 2})">'
        f"{escape(y_label)}</text>",
        f'<rect x="{PAD}" y="{PAD}" width="{WIDTH - 2 * PAD}" height="{HEIGHT - 2 * PAD}" fill="none" stroke="#888"/>',
    ]


def _legend(names: list[str]) -> list[str]:
    parts = []
    for index, name in enumerate(names):
        y = PAD + 18 + index * 18
        color = COLORS[index % len(COLORS)]
        parts.append(
            f'<line x1="{PAD + 12}" y1="{y - 4}" x2="{PAD + 36}" y2="{y - 4}" stroke="{color}" stroke-width="2"/>'
        )
        parts.append(f'<text x="{PAD + 42}" y="{y}">{escape(name)}</text>')
    return parts


def det_svg(curves: dict[str, list[tuple[float, float]]], title: str) -> str:
    """curves: name -> [(far, frr)]. Log-log axes from 1e-4 to 1."""
    low = 1e-4
    span = WIDTH - 2 * PAD, HEIGHT - 2 * PAD

    def x(value: float) -> float:
        return PAD + (math.log10(max(value, low)) - math.log10(low)) / -math.log10(low) * span[0]

    def y(value: float) -> float:
        return HEIGHT - PAD - (math.log10(max(value, low)) - math.log10(low)) / -math.log10(low) * span[1]

    parts = _frame(title, "FAR (log)", "FRR (log)")
    for exponent in range(-4, 1):
        tick = 10.0**exponent
        parts.append(f'<line x1="{x(tick):.1f}" y1="{PAD}" x2="{x(tick):.1f}" y2="{HEIGHT - PAD}" stroke="#eee"/>')
        parts.append(f'<line x1="{PAD}" y1="{y(tick):.1f}" x2="{WIDTH - PAD}" y2="{y(tick):.1f}" stroke="#eee"/>')
        parts.append(f'<text x="{x(tick):.1f}" y="{HEIGHT - PAD + 16}" text-anchor="middle">{tick:g}</text>')
        parts.append(f'<text x="{PAD - 6}" y="{y(tick) + 4:.1f}" text-anchor="end">{tick:g}</text>')
    for index, points in enumerate(curves.values()):
        # Log axes: zero error rates cannot be drawn, so only points with both rates above zero are plotted.
        path = " ".join(f"{x(far):.1f},{y(frr):.1f}" for far, frr in points if far > 0 and frr > 0)
        parts.append(f'<polyline points="{path}" fill="none" stroke="{COLORS[index % len(COLORS)]}" stroke-width="2"/>')
    parts.extend(_legend(list(curves)))
    parts.append("</svg>")
    return "\n".join(parts)


def roc_svg(curves: dict[str, list[tuple[float, float]]], title: str) -> str:
    """curves: name -> [(far, frr)]. Linear axes: FAR on x, true accept rate (1-FRR) on y."""
    span = WIDTH - 2 * PAD, HEIGHT - 2 * PAD

    def x(value: float) -> float:
        return PAD + value * span[0]

    def y(value: float) -> float:
        return HEIGHT - PAD - value * span[1]

    parts = _frame(title, "FAR", "TAR = 1 - FRR")
    for step in range(0, 11, 2):
        tick = step / 10
        parts.append(f'<text x="{x(tick):.1f}" y="{HEIGHT - PAD + 16}" text-anchor="middle">{tick:g}</text>')
        parts.append(f'<text x="{PAD - 6}" y="{y(tick) + 4:.1f}" text-anchor="end">{tick:g}</text>')
    for index, points in enumerate(curves.values()):
        path = " ".join(f"{x(far):.1f},{y(1 - frr):.1f}" for far, frr in points)
        parts.append(f'<polyline points="{path}" fill="none" stroke="{COLORS[index % len(COLORS)]}" stroke-width="2"/>')
    parts.extend(_legend(list(curves)))
    parts.append("</svg>")
    return "\n".join(parts)
