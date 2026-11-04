"""Position book: fills -> positions, average price, realised / unrealised P&L, estimated charges.

All money is tracked in paise (floats for averages). The book is driven by fills from the OMS and
periodically checked against the broker's ``GET /positions`` (see ``reconcile.py``).

Note: Dhan reports positions per (instrument, product) for the whole account. If you trade the
same instrument manually, the bot cannot tell the quantities apart - don't do that, or set
``reconcile.adopt_broker_qty = false``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import ChargesCfg
from .models import Fill, Instrument, Product, Side

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ClosedTrade:
    inst: Instrument
    product: Product
    pnl_paise: float  # net of estimated charges
    qty: int


class Position:
    __slots__ = ("inst", "product", "qty", "avg_paise", "realised_paise", "charges_paise", "last_paise",
                 "_trade_realised0", "_trade_charges0", "_trade_qty", "buy_qty", "sell_qty")

    def __init__(self, inst: Instrument, product: Product) -> None:
        self.inst = inst
        self.product = product
        self.qty = 0  # signed: + long, - short
        self.avg_paise = 0.0
        self.realised_paise = 0.0
        self.charges_paise = 0.0
        self.last_paise = 0
        self._trade_realised0 = 0.0
        self._trade_charges0 = 0.0
        self._trade_qty = 0
        self.buy_qty = 0
        self.sell_qty = 0

    @property
    def unrealised_paise(self) -> float:
        if not self.qty or not self.last_paise:
            return 0.0
        return self.qty * (self.last_paise - self.avg_paise) * self.inst.multiplier

    @property
    def net_paise(self) -> float:
        return self.realised_paise + self.unrealised_paise - self.charges_paise

    @property
    def exposure_paise(self) -> float:
        px = self.last_paise or self.avg_paise
        return abs(self.qty) * px * self.inst.multiplier

    @property
    def is_open(self) -> bool:
        return self.qty != 0

    def apply(self, side: Side, qty: int, price_paise: float, charges_paise: float) -> ClosedTrade | None:
        signed = qty * side.sign
        self.charges_paise += charges_paise
        if side is Side.BUY:
            self.buy_qty += qty
        else:
            self.sell_qty += qty
        closed: ClosedTrade | None = None
        if self.qty == 0 or (self.qty > 0) == (signed > 0):  # open / increase
            if self.qty == 0:
                self._trade_realised0 = self.realised_paise
                self._trade_charges0 = self.charges_paise - charges_paise
                self._trade_qty = 0
            total = abs(self.qty) + qty
            self.avg_paise = (self.avg_paise * abs(self.qty) + price_paise * qty) / total
            self.qty += signed
            self._trade_qty += qty
        else:  # reduce / close / flip
            closing = min(abs(self.qty), qty)
            direction = 1 if self.qty > 0 else -1
            self.realised_paise += closing * (price_paise - self.avg_paise) * direction * self.inst.multiplier
            self.qty += direction * -closing
            rest = qty - closing
            if self.qty == 0:
                pnl = (self.realised_paise - self._trade_realised0) - (self.charges_paise - self._trade_charges0)
                closed = ClosedTrade(self.inst, self.product, pnl, self._trade_qty)
                self.avg_paise = 0.0
                if rest:  # flip: the remainder opens a new trade at this price
                    self._trade_realised0 = self.realised_paise
                    self._trade_charges0 = self.charges_paise
                    self._trade_qty = rest
                    self.qty = rest * side.sign
                    self.avg_paise = price_paise
        self.last_paise = int(round(price_paise))  # a fill is the freshest trade price we know
        return closed

    def to_dict(self) -> dict:
        return {"symbol": self.inst.trading_symbol, "segment": self.inst.segment, "security_id": self.inst.security_id,
                "product": self.product.value, "qty": self.qty, "avg": round(self.avg_paise / 100, 2),
                "ltp": round(self.last_paise / 100, 2), "realised": round(self.realised_paise / 100, 2),
                "unrealised": round(self.unrealised_paise / 100, 2), "charges": round(self.charges_paise / 100, 2),
                "net": round(self.net_paise / 100, 2)}


class PositionBook:
    def __init__(self, charges: ChargesCfg) -> None:
        self.charges = charges
        self.positions: dict[tuple[int, int, str], Position] = {}
        self._by_key: dict[tuple[int, int], list[Position]] = {}
        self.fills = 0
        self.turnover_paise = 0.0

    def get(self, inst: Instrument, product: Product, create: bool = False) -> Position | None:
        k = (inst.feed_code, inst.security_id, product.value)
        p = self.positions.get(k)
        if p is None and create:
            p = self.positions[k] = Position(inst, product)
            self._by_key.setdefault(inst.key, []).append(p)
        return p

    def qty(self, inst: Instrument, product: Product | None = None) -> int:
        if product is not None:
            p = self.get(inst, product)
            return p.qty if p else 0
        return sum(p.qty for p in self._by_key.get(inst.key, ()))

    def estimate_charges(self, qty: int, price_paise: float, inst: Instrument, first_fill_of_order: bool) -> float:
        c = qty * price_paise * inst.multiplier * self.charges.statutory_pct / 100.0
        if first_fill_of_order:
            c += self.charges.brokerage_per_order_inr * 100.0
        return c

    def apply_fill(self, fill: Fill, first_fill_of_order: bool) -> ClosedTrade | None:
        pos = self.get(fill.instrument, fill.product, create=True)
        charges = self.estimate_charges(fill.qty, fill.price_paise, fill.instrument, first_fill_of_order)
        self.fills += 1
        self.turnover_paise += fill.qty * fill.price_paise * fill.instrument.multiplier
        return pos.apply(fill.side, fill.qty, fill.price_paise, charges)  # type: ignore[union-attr]

    def mark(self, key: tuple[int, int], ltp_paise: int) -> bool:
        """Update mark price for every product of this instrument. Returns True if a position is open."""
        lst = self._by_key.get(key)
        if not lst:
            return False
        any_open = False
        for p in lst:
            p.last_paise = ltp_paise
            any_open = any_open or p.qty != 0
        return any_open

    # ---- aggregates ------------------------------------------------------------------------
    def realised_paise(self) -> float:
        return sum(p.realised_paise for p in self.positions.values())

    def unrealised_paise(self) -> float:
        return sum(p.unrealised_paise for p in self.positions.values())

    def charges_paise(self) -> float:
        return sum(p.charges_paise for p in self.positions.values())

    def net_paise(self) -> float:
        return sum(p.net_paise for p in self.positions.values())

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.qty != 0]

    def gross_exposure_paise(self) -> float:
        return sum(p.exposure_paise for p in self.positions.values() if p.qty)

    def snapshot(self) -> list[dict]:
        return [p.to_dict() for p in self.positions.values()]

    # ---- broker reconciliation ----------------------------------------------------------------
    def diff(self, rows: list[dict], resolve, only_keys: set | None = None) -> list["Drift"]:
        """Compare with ``GET /positions`` rows. ``resolve(segment, security_id) -> Instrument | None``.

        ``only_keys`` limits the check to (feed_code, security_id, product) keys the bot trades.
        """
        out: list[Drift] = []
        seen: set[tuple[int, int, str]] = set()
        for row in rows or []:
            try:
                seg = str(row.get("exchangeSegment"))
                sid = int(row.get("securityId"))
                product = Product(str(row.get("productType", "INTRADAY")))
                net = int(row.get("netQty") or 0)
            except (TypeError, ValueError):
                continue
            inst = resolve(seg, sid)
            if inst is None:
                continue
            key = (inst.feed_code, inst.security_id, product.value)
            if only_keys is not None and key not in only_keys:
                continue
            seen.add(key)
            pos = self.get(inst, product)
            local = pos.qty if pos else 0
            if local != net:
                cost = float(row.get("costPrice") or row.get("buyAvg" if net > 0 else "sellAvg") or 0)
                out.append(Drift(key, inst, product, local, net, cost))
        for key, pos in self.positions.items():
            if pos.qty and key not in seen and (only_keys is None or key in only_keys):
                out.append(Drift(key, pos.inst, pos.product, pos.qty, 0, 0.0))
        return out

    def adopt(self, d: "Drift") -> None:
        pos = self.get(d.inst, d.product, create=True)
        pos.qty = d.broker  # type: ignore[union-attr]
        pos.avg_paise = d.cost * 100 if d.broker else 0.0  # type: ignore[union-attr]

    def reconcile(self, rows: list[dict], resolve, adopt: bool = True, only_keys: set | None = None) -> list[str]:
        """Diff and (optionally) adopt immediately. Returns human-readable drift messages."""
        drifts = self.diff(rows, resolve, only_keys)
        msgs = [f"{d.inst.trading_symbol} {d.product.value}: bot={d.local} broker={d.broker}" for d in drifts]
        if adopt:
            for d in drifts:
                self.adopt(d)
        if msgs:
            log.warning("position drift vs broker: %s", "; ".join(msgs))
        return msgs


@dataclass(slots=True)
class Drift:
    key: tuple[int, int, str]
    inst: Instrument
    product: Product
    local: int
    broker: int
    cost: float


def broker_pnl_inr(rows: list[dict]) -> float:
    """Whole-account P&L from ``GET /positions`` (used when risk.include_manual_trades = true)."""
    total = 0.0
    for r in rows or []:
        try:
            total += float(r.get("realizedProfit") or 0) + float(r.get("unrealizedProfit") or 0)
        except (TypeError, ValueError):
            pass
    return total
