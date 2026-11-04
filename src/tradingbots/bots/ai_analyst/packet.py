"""Build the analyst's data packet from the tables: compact, rounded and deterministic.

Each run produces one JSON packet:
* per stock: price, returns, averages, momentum, volatility, 52-week position and the history bot's
  trend label, plus the position when the stock is held;
* the benchmark;
* portfolio money figures: summary, allocation, charges by month and today's P&L;
* the daily budget status.

Raw ticks never go in. Every number comes from a table another bot wrote, so the model sees exactly
what the dashboard shows.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from dhanbot.clock import ist_datetime

from ...core.settings import Settings
from ...core.tables_io import TableReader

METRIC_FIELDS = ("close", "ret_1d", "ret_1w", "ret_1m", "ret_3m", "ret_6m", "ret_1y", "ret_ytd", "cagr", "vol_ann",
                 "max_dd_1y", "sharpe_1y", "sma20", "sma50", "sma200", "dist_sma50", "dist_sma200", "rsi14", "macd",
                 "macd_signal", "atr14_pct", "adx14", "high_52w", "low_52w", "from_high_52w", "from_low_52w",
                 "vol_ratio20", "beta_1y", "rs_3m", "support", "resistance", "golden_cross_on", "death_cross_on",
                 "trend", "trend_score")


def _clean(v: Any, nd: int = 2) -> Any:
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else round(v, nd)
    if isinstance(v, date):
        return v.isoformat()
    return v


def _rows(reader: TableReader, sql: str, params: list | None = None) -> list[dict]:
    try:
        return reader.query(sql, params).to_dicts()
    except Exception:
        return []


def build_packet(reader: TableReader, settings: Settings, kind: str, now: float, budget_usage: dict | None = None,
                 symbols: list[str] | None = None) -> tuple[dict, list[str]]:
    """Returns (packet, data_notes). ``symbols`` restricts the stock list (e.g. a deep dive)."""
    today = ist_datetime(now).date()
    notes: list[str] = []
    metrics = {r["symbol"]: r for r in _rows(reader, "SELECT * FROM history_metrics")}
    quotes = {r["symbol"]: r for r in _rows(reader, "SELECT * FROM quotes_latest")}
    alloc = _rows(reader, "SELECT platform, symbol, kind, SUM(qty) AS qty, SUM(invested) AS invested, "
                          "SUM(current_value) AS current_value, SUM(pnl) AS pnl, SUM(weight_pct) AS weight_pct "
                          "FROM money_allocation GROUP BY platform, symbol, kind ORDER BY current_value DESC")
    held = {r["symbol"]: r for r in alloc if r.get("symbol") and (r.get("qty") or 0) != 0}
    wanted = symbols or [w.symbol for w in settings.stocks()]
    for s in held:  # anything you hold is analysed too, even if it is not on the watchlist
        if s not in wanted and symbols is None:
            wanted.append(s)
    if len(wanted) > settings.ai.max_stocks:
        notes.append(f"{len(wanted) - settings.ai.max_stocks} stocks left out (ai.max_stocks = {settings.ai.max_stocks})")
        wanted = wanted[: settings.ai.max_stocks]
    stocks = []
    for sym in wanted:
        m, q, h = metrics.get(sym), quotes.get(sym), held.get(sym)
        item: dict[str, Any] = {"symbol": sym}
        if q:
            item.update(ltp=_clean(q.get("ltp")), change_pct=_clean(q.get("change_pct")),
                        quote_age_s=_clean(now - float(q["updated_ts"]), 0) if q.get("updated_ts") else None)
        if m:
            item.update({k: _clean(m.get(k)) for k in METRIC_FIELDS})
            item["history_days"] = m.get("days")
            item["metrics_as_of"] = _clean(m.get("as_of"))
        else:
            notes.append(f"{sym}: no daily history yet")
        if h:
            item["position"] = {k: _clean(h.get(k)) for k in ("kind", "qty", "invested", "current_value", "pnl",
                                                               "weight_pct")}
            inv = float(h.get("invested") or 0)
            item["position"]["pnl_pct"] = _clean(float(h.get("pnl") or 0) / inv * 100) if inv else None
        stocks.append(item)
    bench = settings.benchmark()
    market: dict[str, Any] = {}
    if bench is not None:
        bm, bq = metrics.get(bench.symbol), quotes.get(bench.symbol)
        market = {"benchmark": bench.symbol, "ltp": _clean((bq or {}).get("ltp")),
                  "change_pct": _clean((bq or {}).get("change_pct")),
                  **({k: _clean(bm.get(k)) for k in ("trend", "trend_score", "ret_1w", "ret_1m", "ret_3m", "rsi14",
                                                      "dist_sma50", "dist_sma200")} if bm else {})}
        if not bm:
            notes.append(f"{bench.symbol} (benchmark): no daily history yet")
    summary = {r["metric"]: {"value": _clean(r.get("value")), "unit": r.get("unit")}
               for r in _rows(reader, "SELECT metric, value, unit FROM money_summary")}
    charges = [{k: _clean(v) for k, v in r.items()} for r in _rows(
        reader, "SELECT month, SUM(total) AS charges, SUM(turnover) AS turnover, "
                "CASE WHEN SUM(turnover) > 0 THEN SUM(total) / SUM(turnover) * 100 END AS pct_of_turnover "
                "FROM money_charges GROUP BY month ORDER BY month DESC LIMIT 3")]
    pnl = _rows(reader, "SELECT SUM(realized) AS realized, SUM(unrealized) AS unrealized, SUM(charges) AS charges, "
                        "SUM(net) AS net FROM money_pnl WHERE day = ?", [today])
    portfolio = {"summary": summary,
                 "allocation": [{k: _clean(r.get(k)) for k in ("platform", "symbol", "kind", "current_value",
                                                               "weight_pct")} for r in alloc[:10]],
                 "charges_by_month": charges,
                 "pnl_today": {k: _clean(v) for k, v in (pnl[0] if pnl else {}).items()}}
    budget: dict[str, Any] = {}
    if budget_usage:
        budget = {"limits": [{k: it.get(k) for k in ("label", "used", "limit", "pct", "remaining", "unit")}
                             for it in budget_usage.get("limits", [])[:8]],
                  "manual_trades_today": (budget_usage.get("external") or {}).get("trades"),
                  "entries_blocked_today": budget_usage.get("budget_rejects"),
                  "engine": budget_usage.get("engine")}
    else:
        notes.append("no daily budget status (budget bot not running)")
    packet = {"kind": kind, "as_of": f"{ist_datetime(now):%Y-%m-%d %H:%M} IST", "platform": settings.platform.name,
              "market": market, "stocks": stocks, "portfolio": portfolio, "budget": budget, "data_notes": notes}
    return packet, notes
