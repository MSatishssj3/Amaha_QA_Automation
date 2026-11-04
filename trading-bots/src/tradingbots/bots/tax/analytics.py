"""Indian tax estimate for your trades (pure functions). An estimate to discuss with your CA, not tax advice.

Each matched quantity of a sale is one row of ``tax_lots``, in one of these categories:
* **intraday**: equity bought and sold on the same day. That is product INTRADAY, or delivery buys and sells of
  one stock netted within a day. Speculative business income, at your slab rate.
* **fno**: futures and options, plus commodity and currency derivatives. Non-speculative business income, at your
  slab rate.
* **stcg** / **ltcg**: delivery equity held up to / more than 12 months. Short- / long-term capital gains.
* **unmatched**: a sale with no purchase in the trade history or in your manual lots. It is listed so you can add
  the purchase on the Tax page. It is never guessed.

Rules applied (tax year 2026-27; check them with your CA):
* **Matching.** FIFO, as for demat holdings. Same-day delivery buys and sells of a stock are netted first
  (speculative).
* **Charges.** For capital gains, brokerage, exchange fees, SEBI fee, stamp duty and GST are cost of acquisition or
  sale expenses. STT is not deductible. For business income, every charge (STT included) is an expense.
* **Grandfathering.** Shares bought before 1 Feb 2018 (manual lots with the 31 Jan 2018 price) cost the higher of
  the actual cost and that price, capped at the sale price.
* **Set-off within the year.**
  - A short-term capital loss goes against long-term gains.
  - An F&O loss goes against speculative, short- and long-term gains, highest rate first.
  - A speculative loss stays within speculative income; a long-term loss within long-term gains.
  - The long-term exemption applies after set-off.
* **Not covered.** Losses carried in from earlier years and your other income (salary and so on). Options that
  expired without a closing trade are not in the trade book. Splits and bonus issues are not applied: add such
  shares as manual lots.
"""

from __future__ import annotations

import calendar
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Callable, Iterable

from dhanbot.clock import ist_datetime

EQUITY_SEGMENTS = {"NSE_EQ", "BSE_EQ"}
INTRADAY_PRODUCTS = {"INTRADAY", "CO", "BO"}
GRANDFATHER_BEFORE = date(2018, 2, 1)
LABELS = {"intraday": "Intraday equity (speculative)", "fno": "F&O and other derivatives (business)",
          "stcg": "Short-term capital gains", "ltcg": "Long-term capital gains", "unmatched": "Needs a purchase",
          "total": "Total"}
CATEGORIES = ("intraday", "fno", "stcg", "ltcg")


@dataclass(slots=True)
class TFill:
    platform: str
    symbol: str
    segment: str
    product: str
    id: str
    ts: float
    side: int  # +1 buy / -1 sell
    qty: int
    price: float
    stt: float = 0.0
    other: float = 0.0  # every other charge
    source: str = "manual"  # bot / manual

    @property
    def day(self) -> date:
        return ist_datetime(self.ts).date()


@dataclass(slots=True)
class Lot:
    qty: int
    price: float
    day: date | None
    stt_unit: float = 0.0
    other_unit: float = 0.0
    origin: str = "history"  # history / manual / same day
    fmv_2018: float | None = None
    src: TFill | None = None  # the fill that opened it


def fy_of(d: date) -> str:
    """Financial (tax) year label: 1 Apr 2026 - 31 Mar 2027 -> "2026-27"."""
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


def fy_range(fy: str) -> tuple[date, date]:
    y = int(fy[:4])
    return date(y, 4, 1), date(y + 1, 3, 31)


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def is_long_term(buy: date, sell: date, months: int = 12) -> bool:
    """Held for *more than* ``months``: bought 5 Nov 2025 -> long-term from 6 Nov 2026."""
    return sell > add_months(buy, months)


def long_term_from(buy: date, months: int = 12) -> date:
    return date.fromordinal(add_months(buy, months).toordinal() + 1)


def category_of(f: TFill) -> str:
    if f.segment and f.segment not in EQUITY_SEGMENTS:
        return "fno"
    return "intraday" if f.product in INTRADAY_PRODUCTS else "delivery"


def _lot(f: TFill, q: int, origin: str = "history") -> Lot:
    return Lot(q, f.price, f.day, f.stt / f.qty, f.other / f.qty, origin, None, f)


def _row(cat: str, f: TFill, lot: Lot | None, q: int, side: str, remark: str = "") -> dict[str, Any]:
    """One matched quantity. ``f`` is the closing fill; ``lot`` is what it closes (None: unmatched)."""
    close_value = q * f.price
    close_stt, close_other = f.stt * q / f.qty, f.other * q / f.qty
    r: dict[str, Any] = {"fy": fy_of(f.day), "category": cat, "platform": f.platform, "symbol": f.symbol,
                         "segment": f.segment, "product": f.product, "side": side, "qty": q,
                         "open_date": None, "close_date": f.day, "open_price": None, "close_price": f.price,
                         "holding_days": None, "open_value": None, "close_value": round(close_value, 2), "gross": None,
                         "deductible_charges": None, "stt": None, "net": None, "grandfathered": False,
                         "lot_source": lot.origin if lot else "", "source": f.source, "remark": remark}
    if lot is None:
        return r
    cost_unit, gf = lot.price, False
    if cat == "ltcg" and lot.day is not None and lot.day < GRANDFATHER_BEFORE and lot.fmv_2018:
        cost_unit = max(lot.price, min(lot.fmv_2018, f.price))
        gf = cost_unit != lot.price
    open_value = q * cost_unit
    stt = lot.stt_unit * q + close_stt
    other = lot.other_unit * q + close_other
    # long: bought (open) then sold (close); short: sold (open) then bought back (close)
    gross = close_value - open_value if side == "long" else open_value - close_value
    deductible = other + stt if cat in ("intraday", "fno") else other
    days = (f.day - lot.day).days if lot.day else None
    notes = [remark] if remark else []
    if gf:
        notes.append("cost = 31 Jan 2018 price (grandfathered)")
    if days is not None and days < 0:
        notes.append("purchase date is after the sale: check the manual lot")
    r.update(open_date=lot.day, open_price=round(cost_unit, 4), open_value=round(open_value, 2), holding_days=days,
             gross=round(gross, 2), deductible_charges=round(deductible, 2), stt=round(stt, 2),
             net=round(gross - deductible, 2), grandfathered=gf, remark="; ".join(notes))
    return r


def _match_fifo(q: deque[Lot], f: TFill, left: int, cat_fn: Callable[[Lot], str], side: str, out: list[dict],
                remark: str = "") -> int:
    """Close ``left`` units of fill ``f`` against the open lots in ``q`` (FIFO). Returns the units not matched."""
    while left > 0 and q:
        lot = q[0]
        take = min(left, lot.qty)
        out.append(_row(cat_fn(lot), f, lot, take, side, remark))
        left -= take
        if take == lot.qty:
            q.popleft()
        else:
            q[0] = replace(lot, qty=lot.qty - take)
    return left


def tax_lots(fills: Iterable[TFill], manual: Iterable[dict] = (), months: int = 12
             ) -> tuple[list[dict], dict[tuple[str, str], list[Lot]]]:
    """Every matched sale (and every unmatched one), plus the delivery lots still held per (platform, symbol).
    ``manual`` lots (shares bought before the trade history) come first in their stock's FIFO queue."""
    groups: dict[tuple[str, str, str], list[TFill]] = defaultdict(list)
    for f in fills:
        if f.qty > 0 and f.price > 0 and f.side in (1, -1):
            c = category_of(f)
            groups[(c if c == "fno" else "equity", f.platform, f.symbol)].append(f)
    manual_by: dict[tuple[str, str], list[Lot]] = defaultdict(list)
    for m in manual:
        manual_by[(m.get("platform") or "", str(m["symbol"]).upper())].append(
            Lot(int(m["qty"]), float(m["price"]), date.fromisoformat(str(m["buy_date"])), 0.0, 0.0, "manual",
                float(m["fmv_2018"]) if m.get("fmv_2018") else None))
    out: list[dict] = []
    held: dict[tuple[str, str], list[Lot]] = {}
    for (kind, platform, symbol), fs in groups.items():
        fs.sort(key=lambda x: (x.ts, x.id))
        if kind == "fno":
            _fno(fs, out)
            continue
        queue: deque[Lot] = deque(sorted(manual_by.pop((platform, symbol), []), key=lambda lt: lt.day or date.min))
        _equity(fs, queue, out, months)
        if queue:
            held[(platform, symbol)] = list(queue)
    for key, lots in manual_by.items():  # manual lots of stocks with no trades in the history: still held
        held[key] = sorted(lots, key=lambda lt: lt.day or date.min)
    out.sort(key=lambda r: (r["close_date"], r["symbol"], r["category"]))
    return out, held


def _fno(fs: list[TFill], out: list[dict]) -> None:
    longs: deque[Lot] = deque()
    shorts: deque[Lot] = deque()
    for f in fs:
        opp, same, side = (shorts, longs, "short") if f.side > 0 else (longs, shorts, "long")
        left = _match_fifo(opp, f, f.qty, lambda lot: "fno", side, out)
        if left:
            same.append(_lot(f, left))


def _equity(fs: list[TFill], queue: deque[Lot], out: list[dict], months: int) -> None:
    by_day: dict[date, list[TFill]] = defaultdict(list)
    for f in fs:
        by_day[f.day].append(f)
    for day in sorted(by_day):
        day_fills = by_day[day]
        # 1. intraday product: FIFO within the day, long or short
        longs: deque[Lot] = deque()
        shorts: deque[Lot] = deque()
        for f in (x for x in day_fills if x.product in INTRADAY_PRODUCTS):
            opp, same, side = (shorts, longs, "short") if f.side > 0 else (longs, shorts, "long")
            left = _match_fifo(opp, f, f.qty, lambda lot: "intraday", side, out)
            if left:
                same.append(_lot(f, left, "same day"))
        for lot in longs:  # an intraday buy kept overnight became a delivery holding
            queue.append(replace(lot, origin="history"))
        for lot in shorts:
            assert lot.src is not None
            out.append(_row("unmatched", replace(lot.src, qty=lot.qty), None, lot.qty, "short",
                            "intraday sale not bought back in the trade history"))
        # 2. delivery: same-day buys and sells of the stock net first (nothing is delivered), the rest is FIFO
        dl = [x for x in day_fills if x.product not in INTRADAY_PRODUCTS]
        buys = deque(_lot(x, x.qty, "same day") for x in dl if x.side > 0)
        sells = [x for x in dl if x.side < 0]
        netted = min(sum(lt.qty for lt in buys), sum(x.qty for x in sells))
        for f in sells:
            same_day = min(f.qty, netted)
            if same_day:
                _match_fifo(buys, f, same_day, lambda lot: "intraday", "long", out,
                            "delivery bought and sold the same day: netted, so speculative")
                netted -= same_day
            left = f.qty - same_day
            if left:
                left = _match_fifo(queue, f, left, lambda lot, d=f.day: _cg(lot, d, months), "long", out)
            if left:
                out.append(_row("unmatched", f, None, left, "long",
                                "sold without a purchase in the trade history: add the purchase on the Tax page"))
        for lot in buys:  # bought and not sold the same day: joins the holdings, oldest first
            queue.append(replace(lot, origin="history"))


def _cg(lot: Lot, sell_day: date, months: int) -> str:
    return "ltcg" if lot.day is not None and is_long_term(lot.day, sell_day, months) else "stcg"


# ---- the estimate --------------------------------------------------------------------------------------------
def summarize(rows: list[dict], cfg: Any) -> list[dict]:
    """One row per financial year and category, plus a total, after set-off; ``cfg`` is the [tax] settings."""
    out: list[dict] = []
    for fy in sorted({r["fy"] for r in rows}):
        out += _summarize_fy([r for r in rows if r["fy"] == fy], fy, cfg)
    return out


def _summarize_fy(rows: list[dict], fy: str, cfg: Any) -> list[dict]:
    agg = {c: {"matches": 0, "gains": 0.0, "losses": 0.0, "turnover": 0.0, "deductible_charges": 0.0, "stt": 0.0}
           for c in CATEGORIES}
    unmatched = 0
    for r in rows:
        a = agg.get(r["category"])
        if a is None:
            unmatched += 1
            continue
        n = float(r["net"] or 0)
        a["matches"] += 1
        a["gains" if n > 0 else "losses"] += n
        a["deductible_charges"] += float(r["deductible_charges"] or 0)
        a["stt"] += float(r["stt"] or 0)
        if r["category"] in ("intraday", "fno"):
            a["turnover"] += abs(float(r["gross"] or 0))  # sum of favourable and unfavourable differences
    net = {c: agg[c]["gains"] + agg[c]["losses"] for c in CATEGORIES}
    after, moved = dict(net), dict.fromkeys(CATEGORIES, 0.0)

    def set_off(loss: str, gain: str) -> None:
        if after[loss] < 0 < after[gain]:
            x = min(-after[loss], after[gain])
            after[loss] += x
            after[gain] -= x
            moved[loss] += x
            moved[gain] -= x

    set_off("stcg", "ltcg")  # a short-term loss also goes against long-term gains
    rates = {"intraday": cfg.slab_rate_pct, "stcg": cfg.stcg_rate_pct, "ltcg": cfg.ltcg_rate_pct}
    for gain in sorted(rates, key=lambda c: -rates[c]):  # an F&O loss goes against the highest-taxed income first
        set_off("fno", gain)
    cess = 1 + cfg.cess_pct / 100
    biz_sur = 1 + cfg.surcharge_pct / 100
    cg_sur = 1 + min(cfg.surcharge_pct, 15.0) / 100  # the surcharge on these capital gains is capped at 15%
    out = []
    for c in CATEGORIES:
        a = agg[c]
        positive = max(after[c], 0.0)
        exemption = min(positive, cfg.ltcg_exemption_inr) if c == "ltcg" else 0.0
        taxable = positive - exemption
        rate = {"intraday": cfg.slab_rate_pct, "fno": cfg.slab_rate_pct, "stcg": cfg.stcg_rate_pct,
                "ltcg": cfg.ltcg_rate_pct}[c]
        tax = taxable * rate / 100 * (biz_sur if c in ("intraday", "fno") else cg_sur) * cess
        out.append({"fy": fy, "category": c, "label": LABELS[c], "matches": a["matches"], "gains": round(a["gains"], 2),
                    "losses": round(a["losses"], 2), "net": round(net[c], 2), "set_off": round(moved[c], 2),
                    "after_set_off": round(after[c], 2), "exemption": round(exemption, 2), "taxable": round(taxable, 2),
                    "rate_pct": rate, "est_tax": round(tax, 2), "carry_forward": round(max(-after[c], 0.0), 2),
                    "turnover": round(a["turnover"], 2), "deductible_charges": round(a["deductible_charges"], 2),
                    "stt": round(a["stt"], 2), "unmatched": 0})
    tot = {k: round(sum(r[k] for r in out), 2) for k in ("matches", "gains", "losses", "net", "exemption", "taxable",
                                                       "est_tax", "carry_forward", "turnover", "deductible_charges",
                                                       "stt")}
    out.append({"fy": fy, "category": "total", "label": LABELS["total"], **tot, "set_off": 0.0,
                "after_set_off": round(sum(r["after_set_off"] for r in out), 2), "rate_pct": None,
                "unmatched": unmatched})
    return out


def open_lots(held: dict[tuple[str, str], list[Lot]], holdings: dict[tuple[str, str], int], today: date,
              months: int = 12) -> list[dict]:
    """Delivery lots still held, with the day each becomes long-term, plus holdings the history cannot explain."""
    rows = []
    for (platform, symbol), lots in sorted(held.items()):
        for lot in lots:
            ltf = long_term_from(lot.day, months) if lot.day else None
            rows.append({"platform": platform, "symbol": symbol, "qty": lot.qty, "buy_date": lot.day,
                         "buy_price": round(lot.price, 4), "long_term_from": ltf,
                         "days_to_long_term": max(0, (ltf - today).days) if ltf else None, "lot_source": lot.origin,
                         "remark": ""})
    for (platform, symbol), qty in sorted(holdings.items()):
        have = sum(lt.qty for lt in held.get((platform, symbol), []))
        if qty > have:
            rows.append({"platform": platform, "symbol": symbol, "qty": qty - have, "buy_date": None, "buy_price": None,
                         "long_term_from": None, "days_to_long_term": None, "lot_source": "",
                         "remark": "needs a purchase date and price: bought before the trade history"})
    return rows
