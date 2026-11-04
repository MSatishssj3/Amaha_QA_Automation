"""Colors, number formats and chart chrome for the dashboard, in light and dark.

Every color is a documented palette token, never eyeballed. Color does one job per chart:
* **polarity** (up/down, gain/loss): the diverging blue <-> red pair, with a gray midpoint;
* **magnitude**: one blue hue;
* **status** (budget meters): good / warning / critical, always with an icon and a label.
Moving averages are neutral context lines (secondary and muted ink) with direct labels, so color
stays free for polarity. Text always uses ink tokens, never a data color.
"""

from __future__ import annotations

import html
import math
from typing import Any

LIGHT: dict[str, Any] = {
    "mode": "light", "surface": "#fcfcfb", "page": "#f9f9f7", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
    "grid": "#e1e0d9", "axis": "#c3c2b7", "series1": "#2a78d6", "up": "#2a78d6", "down": "#e34948",
    "mid": "#f0efec", "track": "#cde2fb", "good": "#0ca30c", "warning": "#fab219", "critical": "#d03b3b",
    "success_text": "#006300", "border": "rgba(11,11,11,0.10)", "volume": "#c3c2b7",
}
DARK: dict[str, Any] = {
    "mode": "dark", "surface": "#1a1a19", "page": "#0d0d0d", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781",
    "grid": "#2c2c2a", "axis": "#383835", "series1": "#3987e5", "up": "#3987e5", "down": "#e66767",
    "mid": "#383835", "track": "#104281", "good": "#0ca30c", "warning": "#fab219", "critical": "#d03b3b",
    "success_text": "#0ca30c", "border": "rgba(255,255,255,0.10)", "volume": "#383835",
}
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def tokens() -> dict[str, Any]:
    """The palette for the viewer's current Streamlit theme (light unless the session says dark)."""
    try:
        import streamlit as st

        kind = getattr(st.context.theme, "type", None)
    except Exception:
        kind = None
    return DARK if kind == "dark" else LIGHT


# ---- numbers --------------------------------------------------------------------------------------------------
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
    return "-" if x is None else (f"${x:,.4f}" if abs(x) < 1 else f"${x:,.2f}")


def arrow(x: float | None) -> str:
    """Direction as a glyph, so up/down never rests on color alone."""
    if x is None or x == 0:
        return "•"
    return "▲" if x > 0 else "▼"


# ---- chart chrome -----------------------------------------------------------------------------------------------
def style(fig, t: dict[str, Any], *, height: int = 320, legend: bool = False, hover: str = "closest",
          margin: dict | None = None):
    """Surface, ink, recessive solid hairline grid, system sans, tooltips with values first."""
    fig.update_layout(
        height=height, paper_bgcolor=t["surface"], plot_bgcolor=t["surface"],
        font=dict(family=FONT, color=t["ink2"], size=12), hovermode=hover,
        margin=margin or dict(l=8, r=8, t=8, b=8), showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, font=dict(color=t["ink2"]), bgcolor="rgba(0,0,0,0)"),
        hoverlabel=dict(bgcolor=t["surface"], bordercolor=t["axis"], font=dict(family=FONT, color=t["ink"], size=12)),
    )
    fig.update_xaxes(showgrid=False, linecolor=t["axis"], linewidth=1, tickfont=dict(color=t["muted"]), zeroline=False,
                     ticks="", showspikes=hover.startswith("x"), spikecolor=t["muted"], spikethickness=1,
                     spikedash="solid", spikemode="across", spikesnap="cursor")
    fig.update_yaxes(showgrid=True, gridcolor=t["grid"], gridwidth=1, griddash="solid", linecolor=t["axis"],
                     tickfont=dict(color=t["muted"]), zeroline=False, ticks="")
    return fig


def thin_bargap(n_bars: int, plot_px: float, max_px: float = 22.0) -> float:
    """bargap that keeps bars at most ``max_px`` thick (the band's leftover stays air)."""
    if n_bars <= 0:
        return 0.3
    band = plot_px / n_bars
    return min(0.85, max(0.15, 1 - max_px / band))


def diverging_scale(t: dict[str, Any]) -> list:
    """Loss (red) <- gray midpoint -> gain (blue), equal arms; use with zmid=0 and a symmetric range."""
    return [[0.0, t["down"]], [0.5, t["mid"]], [1.0, t["up"]]]


# ---- meters (a ratio against a limit) ------------------------------------------------------------------------------
def meter_html(label: str, used_text: str, pct_used: float, t: dict[str, Any], *, warn_at: float = 80.0,
               note: str = "") -> str:
    """Track = lighter step of the fill's ramp; fill carries severity; status adds an icon and words."""
    p = max(0.0, float(pct_used or 0.0))
    if p >= 100:
        color, icon, status = t["critical"], "⛔", "limit reached"
    elif p >= warn_at:
        color, icon, status = t["warning"], "⚠", "near the limit"
    else:
        color, icon, status = t["series1"], "", ""
    width = min(100.0, p)
    track = t["track"]
    status_html = (f'<span style="color:{t["ink"]};font-weight:600">{icon} {html.escape(status)}</span> · '
                   if status else "")
    note_html = f' <span style="color:{t["muted"]}">{html.escape(note)}</span>' if note else ""
    return (
        f'<div style="margin:2px 0 14px 0;font-family:{FONT}">'
        f'<div style="display:flex;justify-content:space-between;gap:12px;color:{t["ink"]};font-size:14px">'
        f'<span>{html.escape(label)}</span><span style="color:{t["ink2"]}">{status_html}'
        f'{html.escape(used_text)} · <b style="color:{t["ink"]}">{p:.0f}%</b></span></div>'
        f'<div role="meter" aria-label="{html.escape(label)}" aria-valuenow="{p:.0f}" aria-valuemin="0" '
        f'aria-valuemax="100" style="margin-top:6px;height:8px;border-radius:4px;background:{track};overflow:hidden">'
        f'<div style="width:{width:.1f}%;height:8px;border-radius:4px;background:{color}"></div></div>{note_html}</div>'
    )
