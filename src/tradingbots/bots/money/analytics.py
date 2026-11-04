"""Money analytics (pure functions): where the money is, what it earned, and what it cost.

* Realised P&L uses FIFO lot matching per (platform, symbol) over the available trade history - the
  same method for intraday and delivery. A sell with no matching buy in the history (bought before the
  lookback window) is matched against the holding's average cost when known, otherwise counted as 0
  and reported in ``notes``.
* Charges come from the per-trade charge fields of the broker's trade history.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime
from typing import Any

from dhanbot.clock import ist_datetime


def fifo_realized(trades: list[dict], avg_cost: dict[tuple[str, str], float] | None = None
                  ) -> tuple[list[dict], dict[tuple[str, str], tuple[int, float]], list[str]]:
    """Return (realised rows per platform/day/symbol, open lots per key as (qty, avg), notes)."""
    avg_cost = avg_cost or {}
    lots: dict[tuple[str, str], deque] = defaultdict(deque)  # +qty long lots / -qty short lots: (qty, price)
    realized: dict[tuple[str, date, str], float] = defaultdict(float)
    notes: list[str] = []
    for t in sorted(trades, key=lambda r: (r.get("trade_ts") or 0, r.get("trade_id") or "")):
        key = (t.get("platform") or "", t.get("symbol") or "")
        qty = int(t.get("qty") or 0)
        px = float(t.get("price") or 0)
        if qty <= 0:
            continue
        sign = 1 if str(t.get("side")).upper() == "BUY" else -1
        day = t.get("day") or (ist_datetime(t["trade_ts"]).date() if t.get("trade_ts") else date.today())
        q = lots[key]
        remaining = qty
        while remaining and q and (q[0][0] > 0) != (sign > 0):  # closes existing opposite lots
            lot_qty, lot_px = q[0]
            m = min(remaining, abs(lot_qty))
            realized[(key[0], day, key[1])] += m * (px - lot_px) * (1 if lot_qty > 0 else -1)
            remaining -= m
            if m == abs(lot_qty):
                q.popleft()
            else:
                q[0] = (lot_qty - m * (1 if lot_qty > 0 else -1), lot_px)
        if remaining:
            if sign < 0 and key in avg_cost and not q:
                # selling shares bought before the history window: use the holding's average cost
                realized[(key[0], day, key[1])] += remaining * (px - avg_cost[key])
            else:
                if sign < 0 and not q:
                    notes.append(f"{key[1]}: sell of {remaining} without a matching buy in the history "
                                 f"(treated as a short / unknown cost)")
                q.append((sign * remaining, px))
    rows = [{"platform": k[0], "day": k[1], "symbol": k[2], "realized": round(v, 2)} for k, v in sorted(realized.items())]
    open_lots = {}
    for key, q in lots.items():
        qty = sum(x[0] for x in q)
        if qty:
            open_lots[key] = (qty, sum(abs(x[0]) * x[1] for x in q) / sum(abs(x[0]) for x in q))
    return rows, open_lots, notes


def allocation(holdings: list[dict], positions: list[dict], ltp: dict[str, float],
               sectors: dict[str, str] | None = None) -> list[dict]:
    sectors = sectors or {}
    rows = []
    for h in holdings:
        qty = int(h.get("qty") or 0)
        if not qty:
            continue
        px = ltp.get(h["symbol"]) or float(h.get("avg_cost") or 0)
        inv = qty * float(h.get("avg_cost") or 0)
        cur = qty * px
        rows.append({"platform": h.get("platform", ""), "symbol": h["symbol"], "sector": sectors.get(h["symbol"], ""),
                     "kind": "holding", "qty": qty, "invested": round(inv, 2), "current_value": round(cur, 2),
                     "pnl": round(cur - inv, 2)})
    for p in positions:
        qty = int(p.get("net_qty") or 0)
        if not qty or str(p.get("product")) == "CNC":  # CNC positions show up in holdings after settlement
            continue
        cost = float(p.get("cost_price") or p.get("buy_avg") or p.get("sell_avg") or 0)
        px = ltp.get(p["symbol"]) or cost
        inv = abs(qty) * cost
        pnl = float(p.get("unrealized") or 0) or (px - cost) * qty
        rows.append({"platform": p.get("platform", ""), "symbol": p["symbol"], "sector": sectors.get(p["symbol"], ""),
                     "kind": "position", "qty": qty, "invested": round(inv, 2), "current_value": round(inv + pnl, 2),
                     "pnl": round(pnl, 2)})
    total = sum(r["current_value"] for r in rows) or 1.0
    for r in rows:
        r["pnl_pct"] = round(r["pnl"] / r["invested"] * 100, 2) if r["invested"] else 0.0
        r["weight_pct"] = round(r["current_value"] / total * 100, 2)
    return sorted(rows, key=lambda r: -r["current_value"])


CHARGE_COLS = ("brokerage", "stt", "exchange_txn", "sebi", "stamp_duty", "gst")


def charges_by_month(trades: list[dict]) -> list[dict]:
    agg: dict[tuple[str, str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in trades:
        day = t.get("day") or (ist_datetime(t["trade_ts"]).date() if t.get("trade_ts") else None)
        if day is None:
            continue
        a = agg[(t.get("platform") or "", f"{day:%Y-%m}", t.get("symbol") or "")]
        for c in CHARGE_COLS:
            a[c] += float(t.get(c) or 0)
        a["total"] += float(t.get("total_charges") or sum(float(t.get(c) or 0) for c in CHARGE_COLS))
        a["turnover"] += float(t.get("value") or 0)
    out = []
    for (platform, month, sym), a in sorted(agg.items()):
        row = {"platform": platform, "month": month, "symbol": sym, **{k: round(v, 2) for k, v in a.items()}}
        row["pct_of_turnover"] = round(a["total"] / a["turnover"] * 100, 4) if a["turnover"] else 0.0
        out.append(row)
    return out


def cashflows_by_month(ledger: list[dict]) -> list[dict]:
    agg: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
    for e in ledger:
        d = e.get("day") or e.get("date")
        if not d or e.get("category") not in ("deposit", "withdrawal"):
            continue
        key = (e.get("platform") or "", f"{d:%Y-%m}" if isinstance(d, date) else str(d)[:7])
        if e["category"] == "deposit":
            agg[key][0] += float(e.get("credit") or 0)
        else:
            agg[key][1] += float(e.get("debit") or 0)
    return [{"platform": p, "month": m, "deposits": round(v[0], 2), "withdrawals": round(v[1], 2),
             "net": round(v[0] - v[1], 2)} for (p, m), v in sorted(agg.items())]


def xirr(flows: list[tuple[date, float]], guess: float = 0.1) -> float | None:
    """Annualised internal rate of return of dated cash flows (investor view: money in < 0)."""
    flows = [(d, a) for d, a in flows if a]
    if len(flows) < 2 or all(a > 0 for _, a in flows) or all(a < 0 for _, a in flows):
        return None
    d0 = min(d for d, _ in flows)

    def npv(r: float) -> float:
        return sum(a / (1 + r) ** ((d - d0).days / 365.0) for d, a in flows)

    lo, hi = -0.9999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):  # bisection: robust for any sign pattern with one root in range
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            break
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return round(mid * 100, 2)


def summary(alloc: list[dict], pnl_rows: list[dict], charges: list[dict], cash: list[dict], expenses: list[dict],
            funds_available: float | None, today: date, ledger: list[dict]) -> list[dict]:
    month, year = f"{today:%Y-%m}", today.year
    invested = sum(r["invested"] for r in alloc)
    value = sum(r["current_value"] for r in alloc)

    def pnl_since(pred) -> float:
        return round(sum(r["realized"] for r in pnl_rows if pred(r["day"])), 2)

    ch_mtd = sum(r["total"] for r in charges if r["month"] == month)
    ch_ytd = sum(r["total"] for r in charges if r["month"].startswith(str(year)))
    turnover_ytd = sum(r["turnover"] for r in charges if r["month"].startswith(str(year)))
    exp_mtd = sum(e["amount"] for e in expenses if f"{e['day']:%Y-%m}" == month)
    ai_mtd = sum(e["amount"] for e in expenses if e.get("auto") and f"{e['day']:%Y-%m}" == month)
    net_dep = sum(r["net"] for r in cash)
    worth = value + (funds_available or 0.0)
    flows: list[tuple[date, float]] = []
    for e in ledger:
        d = e.get("day") or e.get("date")
        if isinstance(d, date) and e.get("category") == "deposit":
            flows.append((d, -float(e.get("credit") or 0)))
        elif isinstance(d, date) and e.get("category") == "withdrawal":
            flows.append((d, float(e.get("debit") or 0)))
    if flows:
        flows.append((today, worth))
    rows = [
        ("net_worth", worth, "INR", "holdings + open positions at market + available cash"),
        ("invested", invested, "INR", "cost of holdings and open positions"),
        ("current_value", value, "INR", ""),
        ("unrealized_pnl", value - invested, "INR", ""),
        ("realized_pnl_today", pnl_since(lambda d: d == today), "INR", "FIFO-matched trades"),
        ("realized_pnl_mtd", pnl_since(lambda d: f"{d:%Y-%m}" == month), "INR", ""),
        ("realized_pnl_ytd", pnl_since(lambda d: d.year == year), "INR", ""),
        ("charges_mtd", ch_mtd, "INR", "brokerage + taxes + exchange fees"),
        ("charges_ytd", ch_ytd, "INR", ""),
        ("charges_pct_of_turnover_ytd", (ch_ytd / turnover_ytd * 100) if turnover_ytd else 0.0, "%", ""),
        ("expenses_mtd", exp_mtd, "INR", "subscriptions, servers, AI and manual entries"),
        ("ai_cost_mtd", ai_mtd, "INR", "Claude API usage"),
        ("net_deposits", net_dep, "INR", "funds added minus withdrawn (ledger window)"),
        ("cash_available", funds_available or 0.0, "INR", "broker fund limit"),
        ("xirr", xirr(flows) if flows else None, "%", "annualised return on deposits (ledger window)"),
        ("positions_count", float(len(alloc)), "count", ""),
    ]
    return [{"metric": m, "value": (round(v, 2) if isinstance(v, (int, float)) else None), "unit": u, "note": n}
            for m, v, u, n in rows]


def to_date(v: Any) -> date | None:
    if isinstance(v, date):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None
