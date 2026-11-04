"""Number formatting shared by the bots and the dashboard: rupees with Indian grouping (1,00,000), lakh /
crore compaction, percentages and direction glyphs, so up/down never rests on colour alone."""

from __future__ import annotations

import math


def _indian_group(n: int) -> str:
    s = str(abs(n))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def inr(x: float | None, compact: bool = False, signed: bool = False) -> str:
    """Rupees with Indian digit grouping (1,00,000); compact uses lakh / crore (12.35 L, 1.24 Cr)."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    sign = "-" if x < 0 else ("+" if signed and x > 0 else "")
    a = abs(float(x))
    if compact and a >= 1e7:
        return f"{sign}₹{a / 1e7:,.2f} Cr"
    if compact and a >= 1e5:
        return f"{sign}₹{a / 1e5:,.2f} L"
    if a < 100 and a != int(a):
        return f"{sign}₹{a:,.2f}"
    return f"{sign}₹{_indian_group(round(a))}"


def pct(x: float | None, signed: bool = True, nd: int = 1) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{x:+.{nd}f}%" if signed else f"{x:.{nd}f}%"


def usd(x: float | None) -> str:
    if x is None:
        return "-"
    return f"${x:,.2f}" if x == 0 or abs(x) >= 0.01 else f"${x:,.4f}"  # tiny per-call costs keep 4 places


def plural(n: float, word: str, many: str | None = None) -> str:
    return f"{n:,.0f} {word if n == 1 else (many or word + 's')}"


def arrow(x: float | None) -> str:
    """Direction as a glyph, so up/down never rests on color alone."""
    if x is None or x == 0:
        return "•"
    return "▲" if x > 0 else "▼"
