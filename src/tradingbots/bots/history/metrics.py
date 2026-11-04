"""Per-stock trend metrics from daily candles (pure functions - easy to test and to reuse).

Conventions: percentages are in %, a "month" is 21 trading days, a "year" is 252. Indicators follow
the standard definitions (Wilder smoothing for RSI / ATR / ADX; MACD 12-26-9 on EMAs).
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import numpy as np

PERIODS = {"ret_1d": 1, "ret_1w": 5, "ret_1m": 21, "ret_3m": 63, "ret_6m": 126, "ret_1y": 252}
RISK_FREE = 0.065  # annual, roughly Indian T-bill yield; only used for the Sharpe-style ratio


def _ret(close: np.ndarray, n: int) -> float | None:
    if len(close) <= n or close[-1 - n] <= 0:
        return None
    return float((close[-1] / close[-1 - n] - 1) * 100)


def sma(x: np.ndarray, n: int) -> float | None:
    return float(x[-n:].mean()) if len(x) >= n else None


def sma_series(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def ema_series(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    a = 2.0 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
    return out


def wilder(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder's smoothing (RMA): seed with the simple mean of the first n values."""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def rsi(close: np.ndarray, n: int = 14) -> float | None:
    if len(close) <= n:
        return None
    d = np.diff(close)
    up, dn = wilder(np.clip(d, 0, None), n)[-1], wilder(np.clip(-d, 0, None), n)[-1]
    if dn == 0:
        return 100.0 if up > 0 else 50.0
    return float(100 - 100 / (1 + up / dn))


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev = np.concatenate(([close[0]], close[:-1]))
    return np.maximum(high - low, np.maximum(abs(high - prev), abs(low - prev)))


def atr(high, low, close, n: int = 14) -> float | None:
    if len(close) < n + 1:
        return None
    return float(wilder(true_range(high, low, close)[1:], n)[-1])


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> float | None:
    if len(close) < 2 * n + 1:
        return None
    up = high[1:] - high[:-1]
    dn = low[:-1] - low[1:]
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = true_range(high, low, close)[1:]
    atr_s = wilder(tr, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * wilder(plus_dm, n) / atr_s
        mdi = 100 * wilder(minus_dm, n) / atr_s
        dx = 100 * abs(pdi - mdi) / (pdi + mdi)
    dx = np.nan_to_num(dx[n - 1:], nan=0.0)
    if len(dx) < n:
        return None
    return float(wilder(dx, n)[-1])


def max_drawdown(close: np.ndarray) -> float | None:
    if len(close) < 2:
        return None
    peak = np.maximum.accumulate(close)
    return float(((close / peak) - 1).min() * 100)


def last_cross(fast: np.ndarray, slow: np.ndarray, days: list[date]) -> tuple[date | None, date | None]:
    """Most recent golden (fast crosses above slow) and death (below) cross dates."""
    golden = death = None
    diff = fast - slow
    for i in range(1, len(diff)):
        a, b = diff[i - 1], diff[i]
        if np.isnan(a) or np.isnan(b):
            continue
        if a <= 0 < b:
            golden = days[i]
        elif a >= 0 > b:
            death = days[i]
    return golden, death


def swing_levels(high: np.ndarray, low: np.ndarray, price: float, lookback: int = 60, w: int = 3
                 ) -> tuple[float | None, float | None]:
    """Nearest swing low below and swing high above the price (fractal pivots, ``w`` bars each side)."""
    h, lo = high[-lookback:], low[-lookback:]
    lows = [lo[i] for i in range(w, len(lo) - w) if lo[i] == lo[i - w:i + w + 1].min()]
    highs = [h[i] for i in range(w, len(h) - w) if h[i] == h[i - w:i + w + 1].max()]
    support = max((x for x in lows if x < price), default=None)
    resistance = min((x for x in highs if x > price), default=None)
    if support is None and len(lo):
        support = float(lo[-20:].min()) if lo[-20:].min() < price else None
    if resistance is None and len(h):
        resistance = float(h[-20:].max()) if h[-20:].max() > price else None
    return (float(support) if support is not None else None, float(resistance) if resistance is not None else None)


def trend(close: float, sma50: float | None, sma200: float | None, sma50_10d_ago: float | None,
          ret_3m: float | None, adx14: float | None) -> tuple[str, float]:
    signals: list[float] = []
    if sma50:
        signals.append(1.0 if close > sma50 else -1.0)
    if sma50 and sma200:
        signals.append(1.0 if sma50 > sma200 else -1.0)
    if sma50 and sma50_10d_ago:
        signals.append(float(np.sign(sma50 - sma50_10d_ago)))
    if ret_3m is not None:
        signals.append(max(-1.0, min(1.0, ret_3m / 10.0)))
    if not signals:
        return "sideways", 0.0
    score = 100.0 * sum(signals) / len(signals)
    strong = (adx14 or 0) >= 25
    if score >= 60 and strong:
        label = "strong_up"
    elif score >= 25:
        label = "up"
    elif score <= -60 and strong:
        label = "strong_down"
    elif score <= -25:
        label = "down"
    else:
        label = "sideways"
    return label, round(score, 1)


def stock_metrics(symbol: str, days: list[date], o, h, lo, c, v, bench: dict[date, float] | None = None
                  ) -> dict[str, Any]:
    close = np.asarray(c, dtype=float)
    high = np.asarray(h, dtype=float)
    low = np.asarray(lo, dtype=float)
    vol = np.asarray(v, dtype=float)
    n = len(close)
    out: dict[str, Any] = {"symbol": symbol, "as_of": days[-1], "close": float(close[-1]), "days": n}
    for k, p in PERIODS.items():
        out[k] = _ret(close, p)
    year_start = [i for i, d in enumerate(days) if d.year == days[-1].year]
    out["ret_ytd"] = (float((close[-1] / close[year_start[0] - 1] - 1) * 100)
                      if year_start and year_start[0] > 0 else None)
    out["cagr"] = (float(((close[-1] / close[0]) ** (252.0 / (n - 1)) - 1) * 100) if n >= 253 and close[0] > 0
                   else None)
    lr = np.diff(np.log(close[-253:])) if n > 2 else np.array([])
    if len(lr) >= 20:
        sd = lr.std(ddof=1)
        out["vol_ann"] = float(sd * math.sqrt(252) * 100)
        out["sharpe_1y"] = (float((lr.mean() * 252 - RISK_FREE) / (sd * math.sqrt(252))) if sd > 0 else None)
    else:
        out["vol_ann"] = out["sharpe_1y"] = None
    out["max_dd_1y"] = max_drawdown(close[-252:])
    out["sma20"], out["sma50"], out["sma200"] = sma(close, 20), sma(close, 50), sma(close, 200)
    out["dist_sma50"] = (float((close[-1] / out["sma50"] - 1) * 100) if out["sma50"] else None)
    out["dist_sma200"] = (float((close[-1] / out["sma200"] - 1) * 100) if out["sma200"] else None)
    out["rsi14"] = rsi(close, 14)
    macd_line = ema_series(close, 12) - ema_series(close, 26)
    valid = macd_line[~np.isnan(macd_line)]
    out["macd"] = float(macd_line[-1]) if not np.isnan(macd_line[-1]) else None
    sig = ema_series(valid, 9) if len(valid) >= 9 else np.array([np.nan])
    out["macd_signal"] = float(sig[-1]) if not np.isnan(sig[-1]) else None
    a = atr(high, low, close, 14)
    out["atr14_pct"] = float(a / close[-1] * 100) if a else None
    out["adx14"] = adx(high, low, close, 14)
    out["high_52w"] = float(high[-252:].max())
    out["low_52w"] = float(low[-252:].min())
    out["from_high_52w"] = float((close[-1] / out["high_52w"] - 1) * 100)
    out["from_low_52w"] = float((close[-1] / out["low_52w"] - 1) * 100)
    avg20 = vol[-21:-1].mean() if n >= 21 else 0.0
    out["vol_ratio20"] = float(vol[-1] / avg20) if avg20 > 0 else None
    out["support"], out["resistance"] = swing_levels(high, low, float(close[-1]))
    s50, s200 = sma_series(close, 50), sma_series(close, 200)
    out["golden_cross_on"], out["death_cross_on"] = last_cross(s50, s200, days)
    s50_ago = float(s50[-11]) if n >= 61 and not np.isnan(s50[-11]) else None
    out["trend"], out["trend_score"] = trend(float(close[-1]), out["sma50"], out["sma200"], s50_ago,
                                             out["ret_3m"], out["adx14"])
    out["beta_1y"], out["rs_3m"] = None, None
    if bench:
        pairs = [(close[i], bench[d]) for i, d in enumerate(days) if d in bench][-253:]
        if len(pairs) >= 30:
            s_ret = np.diff(np.log([p[0] for p in pairs]))
            b_ret = np.diff(np.log([p[1] for p in pairs]))
            var_b = b_ret.var(ddof=1)
            out["beta_1y"] = float(np.cov(s_ret, b_ret, ddof=1)[0, 1] / var_b) if var_b > 0 else None
        bdays = sorted(bench)
        if len(bdays) > 63 and out["ret_3m"] is not None:
            b3 = (bench[bdays[-1]] / bench[bdays[-64]] - 1) * 100
            out["rs_3m"] = float(out["ret_3m"] - b3)
    return out


def monthly_returns(symbol: str, days: list[date], close: list[float]) -> list[dict]:
    month_end: dict[tuple[int, int], float] = {}
    for d, c in zip(days, close, strict=True):
        month_end[(d.year, d.month)] = c
    keys = sorted(month_end)
    out = []
    for prev, cur in zip(keys, keys[1:], strict=False):
        out.append({"symbol": symbol, "year": cur[0], "month": cur[1],
                    "ret": (month_end[cur] / month_end[prev] - 1) * 100})
    return out


def correlations(series: dict[str, dict[date, float]], lookback: int = 252) -> list[dict]:
    syms = sorted(series)
    if len(syms) < 2:
        return []
    common = sorted(set.intersection(*(set(series[s]) for s in syms)))[-(lookback + 1):]
    if len(common) < 30:
        return []
    rets = {s: np.diff(np.log([series[s][d] for d in common])) for s in syms}
    out = []
    for a in syms:
        for b in syms:
            out.append({"symbol_a": a, "symbol_b": b, "corr": 1.0 if a == b else float(np.corrcoef(rets[a], rets[b])[0, 1])})
    return out
