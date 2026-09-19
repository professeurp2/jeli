"""Questions per day, stacked by outcome: an SVG drawn on the server, with a tooltip per day.

Thin columns with a rounded top and a square base, a 2 px gap between stacked segments, the day's
total on the cap, recessive grid lines. Identity never relies on colour alone: the legend names
each series and a table view sits under the chart.
"""

import html
import math
from datetime import date

from app.control.words import OUTCOMES


def nice_ceiling(value: int) -> tuple[int, int]:
    """A round axis maximum and its step, with at most 5 intervals: 0 / 5 / 10 / 15 …"""
    if value <= 4:
        return 4, 1
    magnitude = 10 ** max(0, math.floor(math.log10(value)) - 1)
    for step in (m * magnitude for m in (1, 2, 5, 10, 20, 25, 50, 100)):
        if math.ceil(value / step) <= 5:
            return math.ceil(value / step) * step, step
    return value, value


def questions_chart(days: list[date], counts: dict[tuple[date, str], int]) -> str:
    width, height, left, right, top, bottom = 640, 230, 34, 8, 24, 30
    plot_h = height - top - bottom
    band = (width - left - right) / len(days)
    bar = min(28.0, band * 0.5)
    totals = {d: sum(counts.get((d, o), 0) for o, _ in OUTCOMES) for d in days}
    y_max, step = nice_ceiling(max(totals.values(), default=0))
    baseline = top + plot_h
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-labelledby="chart-title">']
    for tick in range(0, y_max + 1, step):
        y = baseline - tick / y_max * plot_h
        if tick:
            parts.append(f'<line class="grid-line" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{tick}</text>')
    for i, day in enumerate(days):
        x = left + band * i + (band - bar) / 2
        detail = ", ".join(f"{counts.get((day, o), 0)} {label.lower()}" for o, label in OUTCOMES)
        parts.append(f'<g class="col"><title>{day:%a %d %b}: {totals[day]} questions — {html.escape(detail)}</title>')
        parts.append(f'<rect class="hit" x="{left + band * i:.1f}" y="{top}" width="{band:.1f}" height="{plot_h}" rx="6"/>')
        segments = [(o, counts.get((day, o), 0)) for o, _ in OUTCOMES if counts.get((day, o), 0)]
        cursor = baseline
        for n, (outcome, value) in enumerate(segments):
            seg_top = cursor - value / y_max * plot_h
            seg_bottom = cursor - (2 if n else 0)
            h = seg_bottom - seg_top
            if h > 0.5:
                series = [o for o, _ in OUTCOMES].index(outcome) + 1
                if n == len(segments) - 1:
                    r = min(4.0, h, bar / 2)
                    parts.append(
                        f'<path class="s{series}" d="M{x:.1f},{seg_bottom:.1f} L{x:.1f},{seg_top + r:.1f} '
                        f'Q{x:.1f},{seg_top:.1f} {x + r:.1f},{seg_top:.1f} L{x + bar - r:.1f},{seg_top:.1f} '
                        f'Q{x + bar:.1f},{seg_top:.1f} {x + bar:.1f},{seg_top + r:.1f} L{x + bar:.1f},{seg_bottom:.1f} Z"/>'
                    )
                else:
                    parts.append(f'<rect class="s{series}" x="{x:.1f}" y="{seg_top:.1f}" width="{bar:.1f}" height="{h:.1f}"/>')
            cursor = seg_top
        if totals[day]:
            parts.append(f'<text class="total" x="{x + bar / 2:.1f}" y="{cursor - 7:.1f}" text-anchor="middle">{totals[day]}</text>')
        parts.append(f'<text class="tick" x="{x + bar / 2:.1f}" y="{height - 9}" text-anchor="middle">{day:%a %d}</text></g>')
    parts.append(f'<line class="base" x1="{left}" x2="{width - right}" y1="{baseline}" y2="{baseline}"/>')
    if not any(totals.values()):
        parts.append(
            f'<text class="empty-note" x="{(left + width - right) / 2}" y="{top + plot_h / 2}" text-anchor="middle">'
            "No questions yet: they appear here as members ask Jeli</text>"
        )
    parts.append("</svg>")
    return "".join(parts)
