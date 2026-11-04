"""Performance journal analytics (pure functions): fills -> round trips -> statistics.

* **Round trip** = one position from flat back to flat, per book (real or paper), platform, symbol and product.
  Scaling in and out stays one trade. A fill that reverses the position closes the trade and opens the next
  one; its quantity and charges are split between the two.
* **Gross** P&L comes from prices only. **Net** is after charges: the broker's actual per-trade charges, or the
  engine's estimate for paper trades.
* **Slippage** (engine trades): each fill against its order's reference price (stop trigger, limit price, or the
  last traded price when a market order was placed). It is a cost in rupees: positive means you paid more or
  received less.
* A delivery (CNC) sale with nothing bought in the history sells shares bought earlier. Its cost comes from the
  holdings' average price when known. Otherwise it is listed as ``unmatched`` and left out of the statistics.
* Statistics use closed trades only. Each trade counts on the day it closed.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from dhanbot.clock import ist_datetime

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
HOLD_BUCKETS = [(300.0, "< 5 min"), (1800.0, "5-30 min"), (7200.0, "30 min - 2 h"), (86400.0, "2 h - 1 day"),
                (7 * 86400.0, "1-7 days"), (math.inf, "> 7 days")]
EXIT_REASONS = {"exit": "strategy exit", "sl": "stop-loss", "target": "target", "squareoff": "square-off",
                "flatten": "flatten / halt", "entry": "reversal", "adopted": "engine (adopted)", "manual": "manual",
                "bracket": "bracket exit"}
SCOPES = ("today", "month", "fy", "all")
SOURCES = ("real", "bot", "manual", "paper")
DIMENSIONS = ("hour", "weekday", "symbol", "holding", "direction", "source", "exit", "tag")


@dataclass(slots=True)
class Fill:
    book: str  # "real" (broker trade book) or "paper" (engine paper fills)
    platform: str
    symbol: str
    product: str
    id: str  # broker trade id, or a stable id for a paper fill
    ts: float
    side: int  # +1 buy / -1 sell
    qty: int
    price: float
    charges: float = 0.0
    source: str = "manual"  # bot / manual / paper
    segment: str = ""
    role: str = ""  # engine order role (entry / exit / sl / target / squareoff / flatten / bracket); "" if manual
    ref_price: float | None = None  # slippage reference; set for engine fills on the order's own side only

    @property
    def key(self) -> tuple[str, str, str, str]:
        return self.book, self.platform, self.symbol, self.product


@dataclass
class _Trip:
    first: Fill
    direction: int
    entry_ts: float
    entry_qty: int = 0
    entry_value: float = 0.0
    exit_qty: int = 0
    exit_value: float = 0.0
    exit_ts: float | None = None
    charges: float = 0.0
    slippage: float | None = None
    pos: int = 0
    max_qty: int = 0
    last_role: str = ""
    sources: set[str] = field(default_factory=set)
    ids: set[str] = field(default_factory=set)

    def add(self, f: Fill, q: int, entry: bool) -> None:
        self.charges += f.charges * q / f.qty
        self.ids.add(f.id)
        self.sources.add(f.source)
        if entry:
            self.entry_qty += q
            self.entry_value += q * f.price
            self.pos += q
            self.max_qty = max(self.max_qty, self.pos)
        else:
            self.exit_qty += q
            self.exit_value += q * f.price
            self.pos -= q
            self.exit_ts = f.ts
            self.last_role = f.role or ("manual" if f.source == "manual" else "")
        if f.ref_price:
            self.slippage = (self.slippage or 0.0) + (f.price - f.ref_price) * q * f.side

    def row(self, closed: bool) -> dict[str, Any]:
        f = self.first
        avg_entry = self.entry_value / self.entry_qty
        avg_exit = self.exit_value / self.exit_qty if self.exit_qty else None
        src = next(iter(self.sources)) if len(self.sources) == 1 else "mixed"
        r = _base(f, self.direction, "closed" if closed else "open", src, self.entry_ts)
        r.update(qty=self.entry_qty, max_qty=self.max_qty, avg_entry=round(avg_entry, 4), fills=len(self.ids),
                 slippage=round(self.slippage, 2) if self.slippage is not None else None)
        if closed:
            gross = self.direction * (self.exit_value - self.entry_value)
            net = gross - self.charges
            r.update(exit_ts=self.exit_ts, exit_day=_day(self.exit_ts), avg_exit=round(avg_exit or 0.0, 4),
                     gross=round(gross, 2), charges=round(self.charges, 2), net=round(net, 2),
                     return_pct=round(net / self.entry_value * 100, 3) if self.entry_value else None,
                     holding_s=round((self.exit_ts or self.entry_ts) - self.entry_ts, 1),
                     exit_reason=EXIT_REASONS.get(self.last_role, self.last_role or None))
        else:  # no P&L until it is closed; the Money and Trading pages show open positions at market
            r.update(open_qty=self.pos, charges=round(self.charges, 2))
        return r


def _day(ts: float | None) -> date | None:
    return ist_datetime(float(ts)).date() if ts else None


def _base(f: Fill, direction: int, status: str, source: str, entry_ts: float | None) -> dict[str, Any]:
    return {"trade_key": f"{f.book}:{f.platform}:{f.symbol}:{f.product}:{f.id}", "book": f.book, "platform": f.platform,
            "symbol": f.symbol, "segment": f.segment, "product": f.product,
            "direction": "long" if direction > 0 else "short", "status": status, "source": source,
            "entry_ts": entry_ts, "entry_day": _day(entry_ts), "exit_ts": None, "exit_day": None, "qty": 0,
            "max_qty": 0, "open_qty": 0, "avg_entry": None, "avg_exit": None, "gross": None, "charges": None,
            "net": None, "return_pct": None, "holding_s": None, "slippage": None, "exit_reason": None, "fills": 1,
            "remark": "", "tags": "", "note": ""}


def _earlier_shares(f: Fill, q: int, cost: float | None) -> dict[str, Any]:
    """A delivery sale of shares bought before the trade history starts."""
    r = _base(f, 1, "closed" if cost else "unmatched", f.source, None)
    charges = f.charges * q / f.qty
    r.update(trade_key=r["trade_key"] + ":earlier", qty=q, max_qty=q, exit_ts=f.ts, exit_day=_day(f.ts),
             avg_exit=f.price, charges=round(charges, 2),
             exit_reason=EXIT_REASONS.get(f.role or ("manual" if f.source == "manual" else ""), f.role or None))
    if cost:
        gross = q * (f.price - cost)
        r.update(avg_entry=round(cost, 4), gross=round(gross, 2), net=round(gross - charges, 2),
                 return_pct=round((gross - charges) / (q * cost) * 100, 3),
                 remark="bought before the trade history: cost is your holdings' average price")
    else:
        r["remark"] = "bought before the trade history and no average cost is known: left out of the statistics"
    return r


def round_trips(fills: Iterable[Fill], holding_cost: dict[tuple[str, str], float] | None = None) -> list[dict]:
    """Every round trip (closed, open or unmatched), oldest first."""
    holding_cost = holding_cost or {}
    by_key: dict[tuple, list[Fill]] = defaultdict(list)
    for f in fills:
        if f.qty > 0 and f.price > 0 and f.side in (1, -1):
            by_key[f.key].append(f)
    out: list[dict] = []
    for fs in by_key.values():
        fs.sort(key=lambda x: (x.ts, x.id))
        trip: _Trip | None = None
        for f in fs:
            left = f.qty
            while left > 0:
                if trip is None:
                    if f.side < 0 and f.product == "CNC" and f.book == "real":
                        out.append(_earlier_shares(f, left, holding_cost.get((f.platform, f.symbol))))
                        break
                    trip = _Trip(f, f.side, f.ts)
                if f.side == trip.direction:
                    trip.add(f, left, entry=True)
                    left = 0
                else:
                    take = min(left, trip.pos)
                    trip.add(f, take, entry=False)
                    left -= take
                    if trip.pos == 0:
                        out.append(trip.row(closed=True))
                        trip = None
        if trip is not None:
            out.append(trip.row(closed=False))
    return sorted(out, key=lambda r: (r["exit_ts"] or r["entry_ts"] or 0.0, r["trade_key"]))


# ---- statistics ---------------------------------------------------------------------------------------------
STAT_KEYS = ("trades", "wins", "losses", "win_rate", "net_pnl", "gross_pnl", "charges", "avg_win", "avg_loss", "payoff",
             "expectancy", "profit_factor", "max_drawdown", "losing_streak", "winning_streak", "avg_hold_s",
             "slippage", "charges_pct_of_gross", "best", "worst")


def closed(trips: Iterable[dict]) -> list[dict]:
    return sorted((t for t in trips if t["status"] == "closed"), key=lambda t: (t["exit_ts"] or 0.0, t["trade_key"]))


def stats(trips: Iterable[dict]) -> dict[str, Any]:
    """Headline numbers over the closed trades (win = net > 0 after charges)."""
    cs = closed(trips)
    out: dict[str, Any] = dict.fromkeys(STAT_KEYS)
    out.update(trades=len(cs), wins=0, losses=0, net_pnl=0.0, gross_pnl=0.0, charges=0.0, max_drawdown=0.0,
               losing_streak=0, winning_streak=0)
    if not cs:
        return out
    nets = [float(t["net"]) for t in cs]
    wins, losses = [x for x in nets if x > 0], [x for x in nets if x < 0]
    gp, gl = sum(wins), -sum(losses)
    gross, charges = sum(float(t["gross"] or 0) for t in cs), sum(float(t["charges"] or 0) for t in cs)
    eq = peak = mdd = 0.0
    run_w = run_l = best_w = best_l = 0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        run_w, run_l = (run_w + 1, 0) if x > 0 else (0, run_l + 1) if x < 0 else (0, 0)
        best_w, best_l = max(best_w, run_w), max(best_l, run_l)
    holds = [float(t["holding_s"]) for t in cs if t.get("holding_s") is not None]
    slips = [float(t["slippage"]) for t in cs if t.get("slippage") is not None]
    avg_win = gp / len(wins) if wins else None
    avg_loss = -gl / len(losses) if losses else None
    out.update(wins=len(wins), losses=len(losses), win_rate=round(len(wins) / len(cs) * 100, 2),
               net_pnl=round(sum(nets), 2), gross_pnl=round(gross, 2), charges=round(charges, 2),
               avg_win=round(avg_win, 2) if avg_win is not None else None,
               avg_loss=round(avg_loss, 2) if avg_loss is not None else None,
               payoff=round(avg_win / -avg_loss, 3) if avg_win is not None and avg_loss else None,
               expectancy=round(sum(nets) / len(cs), 2),
               profit_factor=round(gp / gl, 3) if gl > 0 else None,
               max_drawdown=round(mdd, 2), losing_streak=best_l, winning_streak=best_w,
               avg_hold_s=round(sum(holds) / len(holds), 1) if holds else None,
               slippage=round(sum(slips), 2) if slips else None,
               charges_pct_of_gross=round(charges / gross * 100, 2) if gross > 0 else None,
               best=round(max(nets), 2), worst=round(min(nets), 2))
    return out


def equity_curve(trips: Iterable[dict]) -> list[tuple[float, float, str]]:
    """(exit time, cumulative net P&L, symbol) after each closed trade."""
    eq, out = 0.0, []
    for t in closed(trips):
        eq += float(t["net"])
        out.append((float(t["exit_ts"]), round(eq, 2), t.get("symbol") or ""))
    return out


def fy_start(d: date) -> date:
    """Indian financial year: 1 April to 31 March."""
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def in_scope(t: dict, scope: str, today: date) -> bool:
    d = t.get("exit_day")
    if scope == "all":
        return True
    if d is None:
        return False
    if scope == "today":
        return d == today
    if scope == "month":
        return (d.year, d.month) == (today.year, today.month)
    if scope == "fy":
        return d >= fy_start(today)
    raise ValueError(f"unknown scope {scope!r}")


def of_source(trips: Iterable[dict], source: str) -> list[dict]:
    """``real`` = engine + manual (+ mixed); ``paper`` = engine paper trades; ``bot`` / ``manual`` = real only."""
    if source == "real":
        return [t for t in trips if t["book"] == "real"]
    if source == "paper":
        return [t for t in trips if t["book"] == "paper"]
    return [t for t in trips if t["book"] == "real" and t["source"] == source]


def summary_rows(trips: list[dict], today: date) -> list[dict]:
    rows = []
    for scope in SCOPES:
        for source in SOURCES:
            ts = [t for t in of_source(trips, source) if in_scope(t, scope, today)]
            rows.append({"scope": scope, "source": source, **stats(ts)})
    return rows


def _buckets(t: dict, dim: str) -> list[tuple[int, str]]:
    ets = t.get("entry_ts")
    if dim == "hour":
        return [(ist_datetime(ets).hour, f"{ist_datetime(ets).hour:02d}:00")] if ets else []
    if dim == "weekday":
        wd = ist_datetime(ets or t["exit_ts"]).weekday()
        return [(wd, WEEKDAYS[wd])]
    if dim == "holding":
        h = t.get("holding_s")
        if h is None:
            return []
        i = next(i for i, (lim, _) in enumerate(HOLD_BUCKETS) if h < lim)
        return [(i, HOLD_BUCKETS[i][1])]
    if dim == "direction":
        return [(0 if t["direction"] == "long" else 1, t["direction"])]
    if dim == "tag":
        tags = [x for x in (t.get("tags") or "").split(",") if x]
        return [(0, x) for x in tags] or [(0, "untagged")]
    if dim == "exit":
        return [(0, t.get("exit_reason") or "-")]
    return [(0, str(t.get(dim) or "-"))]  # symbol, source


def breakdown(trips: Iterable[dict], dim: str) -> list[dict]:
    """Closed trades grouped by one dimension. ``sort`` orders the buckets: by time for hour / weekday /
    holding, else by net P&L, best first."""
    groups: dict[tuple[int, str], list[float]] = defaultdict(list)
    charges: dict[tuple[int, str], float] = defaultdict(float)
    for t in closed(trips):
        for b in _buckets(t, dim):
            groups[b].append(float(t["net"]))
            charges[b] += float(t["charges"] or 0)
    rows = []
    for (order, label), nets in groups.items():
        w = sum(x > 0 for x in nets)
        rows.append({"dimension": dim, "bucket": label, "sort": order, "trades": len(nets), "wins": w,
                     "win_rate": round(w / len(nets) * 100, 2), "net_pnl": round(sum(nets), 2),
                     "avg_net": round(sum(nets) / len(nets), 2), "charges": round(charges[(order, label)], 2)})
    if dim in ("hour", "weekday", "holding", "direction"):
        rows.sort(key=lambda r: r["sort"])
    else:
        rows.sort(key=lambda r: (-r["net_pnl"], r["bucket"]))
        for i, r in enumerate(rows):
            r["sort"] = i
    return rows


def breakdown_rows(trips: list[dict], today: date) -> list[dict]:
    rows = []
    for scope in ("month", "fy", "all"):
        for source in ("real", "paper"):
            ts = [t for t in of_source(trips, source) if in_scope(t, scope, today)]
            for dim in DIMENSIONS:
                rows += [{"scope": scope, "source": source, **r} for r in breakdown(ts, dim)]
    return rows


def parse_tags(raw: Any) -> list[str]:
    """Tags are short lower-case words: "Breakout, news" -> ["breakout", "news"]."""
    items = raw if isinstance(raw, (list, tuple)) else str(raw or "").split(",")
    out: list[str] = []
    for x in items:
        tag = " ".join(str(x).strip().lower().split())[:32]
        if tag and tag not in out:
            out.append(tag)
    return out[:12]
