"""Plotly figures for the dashboard. Every chart applies the same rules:

* thin marks;
* one job per color (``theme.py``);
* solid hairline grids;
* values in tooltips and in a table view, never by color alone;
* a crosshair on time series.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Iterable

import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from . import theme as T

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return []
    if hi == lo:
        hi = lo + (abs(lo) or 1.0)
    raw = (hi - lo) / max(1, n)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    out, v = [], start
    while v <= hi + step * 0.5 and len(out) < 50:
        out.append(round(v, 10))
        v += step
    return out


def inr_axis(fig, values: Iterable[float], axis: str = "y", include_zero: bool = True, **kw) -> None:
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not vals:
        return
    lo, hi = min(vals), max(vals)
    if include_zero:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    ticks = nice_ticks(lo, hi)
    upd = fig.update_yaxes if axis == "y" else fig.update_xaxes
    upd(tickvals=ticks, ticktext=[T.inr(v, compact=True) for v in ticks], **kw)


def empty(t: dict[str, Any], text: str, height: int = 240):
    fig = go.Figure()
    fig.add_annotation(text=text, showarrow=False, font=dict(color=t["muted"], size=13), x=0.5, y=0.5,
                       xref="paper", yref="paper")
    T.style(fig, t, height=height)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


def price_chart(c: pl.DataFrame, symbol: str, t: dict[str, Any], holidays: Iterable[date] = (), height: int = 480):
    """Daily candles (blue up / red down), SMA 50 / 200 as labelled context lines, volume below."""
    if c.is_empty():
        return empty(t, f"No daily history for {symbol} yet", height)
    c = c.sort("day")
    closes = c["close"]
    sma50 = closes.rolling_mean(50) if c.height >= 50 else None
    sma200 = closes.rolling_mean(200) if c.height >= 200 else None
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.78, 0.22], vertical_spacing=0.03)
    x = c["day"].to_list()
    fig.add_trace(go.Candlestick(
        x=x, open=c["open"], high=c["high"], low=c["low"], close=closes, name=symbol,
        increasing=dict(line=dict(color=t["up"], width=1), fillcolor=t["up"]),
        decreasing=dict(line=dict(color=t["down"], width=1), fillcolor=t["down"]), whiskerwidth=0), row=1, col=1)
    for name, s, color in (("SMA 50", sma50, t["ink2"]), ("SMA 200", sma200, t["muted"])):
        if s is None:
            continue
        fig.add_trace(go.Scatter(x=x, y=s, mode="lines", name=name, line=dict(color=color, width=2),
                                 hovertemplate="%{y:,.2f}"), row=1, col=1)
        last = s.drop_nulls()
        if last.len():
            fig.add_annotation(x=x[-1], y=float(last[-1]), text=f"  {name}", showarrow=False, xanchor="left",
                               font=dict(color=t["ink2"], size=11), row=1, col=1)
    fig.add_trace(go.Bar(x=x, y=c["volume"], name="Volume", marker=dict(color=t["volume"]),
                         hovertemplate="%{y:,.0f}"), row=2, col=1)
    T.style(fig, t, height=height, legend=True, hover="x unified", margin=dict(l=8, r=64, t=28, b=8))
    # weekends are hidden; holidays are not (a hidden date that has data would break the average lines)
    fig.update_xaxes(rangeslider_visible=False, rangebreaks=[dict(bounds=["sat", "mon"])])
    fig.update_yaxes(tickformat=",.0f", row=1, col=1)
    fig.update_yaxes(showgrid=False, tickformat="~s", row=2, col=1)
    return fig


def intraday_line(b: pl.DataFrame, symbol: str, t: dict[str, Any], height: int = 260):
    if b.is_empty():
        return empty(t, f"No 1-minute bars for {symbol} today yet", height)
    b = b.sort("start_ts")
    x = [T_from_ts(v) for v in b["start_ts"].to_list()]
    y = b["close"].to_list()
    fig = go.Figure(go.Scatter(x=x, y=y, mode="lines", name=symbol, line=dict(color=t["series1"], width=2),
                               fill="tozeroy", fillcolor=_alpha(t["series1"], 0.10), hovertemplate="%{y:,.2f}"))
    lo, hi = min(y), max(y)
    pad = (hi - lo) * 0.1 or hi * 0.002
    fig.add_trace(go.Scatter(x=[x[-1]], y=[y[-1]], mode="markers", marker=dict(size=8, color=t["series1"],
                                                                              line=dict(color=t["surface"], width=2)),
                             hoverinfo="skip", showlegend=False))
    fig.add_annotation(x=x[-1], y=y[-1], text=f"  {y[-1]:,.2f}", showarrow=False, xanchor="left",
                       font=dict(color=t["ink"], size=12))
    T.style(fig, t, height=height, hover="x unified", margin=dict(l=8, r=64, t=8, b=8))
    fig.update_yaxes(range=[lo - pad, hi + pad], tickformat=",.0f")
    return fig


def T_from_ts(ts: float):
    from dhanbot.clock import ist_datetime

    return ist_datetime(float(ts)).replace(tzinfo=None)


def _alpha(hex_color: str, a: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{a})"


def monthly_heatmap(m: pl.DataFrame, t: dict[str, Any], height: int = 260):
    """Seasonality: months across, years down; loss red <- gray -> gain blue; values in the tooltip and table."""
    if m.is_empty():
        return empty(t, "No monthly returns yet", height)
    years = sorted({int(y) for y in m["year"].to_list()}, reverse=True)
    grid = [[None] * 12 for _ in years]
    for r in m.iter_rows(named=True):
        grid[years.index(int(r["year"]))][int(r["month"]) - 1] = r["ret"]
    flat = [abs(v) for row in grid for v in row if v is not None]
    lim = max(flat) if flat else 1.0
    fig = go.Figure(go.Heatmap(z=grid, x=MONTHS, y=[str(y) for y in years], colorscale=T.diverging_scale(t), zmid=0,
                               zmin=-lim, zmax=lim, xgap=2, ygap=2, hoverongaps=False,
                               hovertemplate="%{y} %{x}: %{z:+.1f}%<extra></extra>",
                               colorbar=dict(thickness=8, outlinewidth=0, ticksuffix="%",
                                             tickfont=dict(color=t["muted"]))))
    T.style(fig, t, height=max(140, 60 + 34 * len(years)))
    fig.update_yaxes(showgrid=False, type="category")
    return fig


def corr_heatmap(cdf: pl.DataFrame, t: dict[str, Any], height: int = 360):
    if cdf.is_empty():
        return empty(t, "No correlations yet", height)
    syms = sorted(set(cdf["symbol_a"].to_list()) | set(cdf["symbol_b"].to_list()))
    idx = {s: i for i, s in enumerate(syms)}
    z = [[None] * len(syms) for _ in syms]
    for r in cdf.iter_rows(named=True):
        i, j = idx[r["symbol_a"]], idx[r["symbol_b"]]
        z[i][j] = z[j][i] = r["corr"]
    for i in range(len(syms)):
        z[i][i] = 1.0
    fig = go.Figure(go.Heatmap(z=z, x=syms, y=syms, colorscale=T.diverging_scale(t), zmid=0, zmin=-1, zmax=1, xgap=2,
                               ygap=2, hovertemplate="%{y} vs %{x}: %{z:.2f}<extra></extra>",
                               colorbar=dict(thickness=8, outlinewidth=0, tickfont=dict(color=t["muted"]))))
    T.style(fig, t, height=height)
    fig.update_yaxes(showgrid=False, autorange="reversed")
    return fig


def ranked_bars(labels: list[str], values: list[float], t: dict[str, Any], *, money: bool = True,
                height: int | None = None, suffix: str = ""):
    """One whole's parts compared: largest first, one hue, value at the bar tip."""
    if not labels:
        return empty(t, "Nothing to show yet", 200)
    pairs = sorted(zip(labels, values, strict=True), key=lambda p: p[1] or 0, reverse=True)
    labels, values = [p[0] for p in pairs], [float(p[1] or 0) for p in pairs]
    h = height or max(160, 36 * len(labels) + 40)
    text = [T.inr(v) if money else f"{v:,.1f}{suffix}" for v in values]
    fig = go.Figure(go.Bar(x=values, y=labels, orientation="h", marker=dict(color=t["series1"], cornerradius=4),
                           text=text, textposition="outside", cliponaxis=False, textfont=dict(color=t["ink2"]),
                           hovertemplate="%{y}: %{text}<extra></extra>"))
    T.style(fig, t, height=h, margin=dict(l=8, r=72, t=8, b=8))
    fig.update_layout(bargap=T.thin_bargap(len(labels), h - 16))
    fig.update_yaxes(autorange="reversed", showgrid=False)
    fig.update_xaxes(showgrid=True, gridcolor=t["grid"], showticklabels=False)
    return fig


def columns(x: list, y: list[float], t: dict[str, Any], *, money: bool = True, height: int = 260, label_last: bool = True,
            hover_fmt: str | None = None):
    """Magnitude over time, one hue (e.g. charges or AI spend per month)."""
    if not x:
        return empty(t, "Nothing to show yet", height)
    ys = [float(v or 0) for v in y]
    fig = go.Figure(go.Bar(x=x, y=ys, marker=dict(color=t["series1"], cornerradius=4),
                           customdata=[T.inr(v) if money else f"{v:,.4f}" for v in ys],
                           hovertemplate=hover_fmt or "%{x}: %{customdata}<extra></extra>"))
    if label_last:
        fig.add_annotation(x=x[-1], y=ys[-1], text=T.inr(ys[-1]) if money else f"{ys[-1]:,.2f}", showarrow=False,
                           yanchor="bottom", font=dict(color=t["ink2"], size=11))
    T.style(fig, t, height=height, margin=dict(l=8, r=8, t=16, b=8))
    fig.update_layout(bargap=T.thin_bargap(len(x), 600))
    fig.update_xaxes(type="category")  # one bar per label; never a stretched date axis
    if money:
        inr_axis(fig, ys)
    return fig


def diverging_bars(x: list, y: list[float], t: dict[str, Any], height: int = 260):
    """Gain above / loss below the zero baseline: blue up, red down, value in the tooltip."""
    if not x:
        return empty(t, "No profit or loss yet", height)
    ys = [float(v or 0) for v in y]
    fig = go.Figure(go.Bar(x=x, y=ys, marker=dict(color=[t["up"] if v >= 0 else t["down"] for v in ys], cornerradius=4),
                           customdata=[f"{T.arrow(v)} {T.inr(v, signed=True)}" for v in ys],
                           hovertemplate="%{x}: %{customdata}<extra></extra>"))
    fig.add_hline(y=0, line=dict(color=t["axis"], width=1))
    T.style(fig, t, height=height, margin=dict(l=8, r=8, t=16, b=8))
    fig.update_layout(bargap=T.thin_bargap(len(x), 600))
    fig.update_xaxes(type="category")
    inr_axis(fig, ys)
    return fig


def equity_curve(points: list[tuple[float, float, str]], t: dict[str, Any], height: int = 300):
    """Cumulative net P&L after each closed trade (x = trade number, so nights and weekends leave no gaps)."""
    if not points:
        return empty(t, "No closed trades in this period", height)
    n = list(range(1, len(points) + 1))
    y = [p[1] for p in points]
    hover = [f"trade {i} · {p[2]} · {T_from_ts(p[0]):%d %b %H:%M}<br>cumulative {T.arrow(p[1])} {T.inr(p[1], signed=True)}"
             for i, p in zip(n, points, strict=True)]
    fig = go.Figure(go.Scatter(x=n, y=y, mode="lines+markers" if len(n) <= 60 else "lines",
                               line=dict(color=t["series1"], width=2), marker=dict(size=5, color=t["series1"]),
                               customdata=hover, hovertemplate="%{customdata}<extra></extra>"))
    fig.add_hline(y=0, line=dict(color=t["axis"], width=1))
    fig.add_annotation(x=n[-1], y=y[-1], text=f"  {T.inr(y[-1], signed=True)}", showarrow=False, xanchor="left",
                       font=dict(color=t["ink"], size=12))
    T.style(fig, t, height=height, margin=dict(l=8, r=72, t=8, b=8))
    fig.update_xaxes(title=dict(text="closed trades, oldest first", font=dict(color=t["muted"], size=11)),
                     tickformat="d")
    inr_axis(fig, y)
    return fig
