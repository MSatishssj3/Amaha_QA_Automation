"""Turn Dhan REST payloads into table rows. Field lookups are case-insensitive and accept the aliases
seen in Dhan's docs/SDK, because some response fields could not be verified against the live API."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from dhanbot.clock import IST, ist_datetime
from dhanbot.dhan.normalize import available_balance, normalise_order


def _low(row: dict) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in row.items()}


def _pick(low: dict, *names: str, default: Any = None) -> Any:
    for n in names:
        v = low.get(n.lower())
        if v not in (None, "", "NA"):
            return v
    return default


def num(v: Any, default: float = 0.0) -> float:
    try:
        return float(str(v).replace(",", "")) if v not in (None, "", "NA") else default
    except (TypeError, ValueError):
        return default


def inum(v: Any, default: int = 0) -> int:
    return int(round(num(v, default)))


_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%d-%m-%Y %H:%M:%S",
               "%Y-%m-%d")
_DATE_FORMATS = ("%b %d, %Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y")


def ist_ts(text: Any) -> float | None:
    """Dhan timestamps are IST wall-clock strings."""
    if text in (None, "", "NA"):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(s[:26], fmt).replace(tzinfo=IST).timestamp()
        except ValueError:
            continue
    return None


def ist_date(text: Any) -> date | None:
    if text in (None, "", "NA"):
        return None
    s = str(text).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    ts = ist_ts(s)
    return ist_datetime(ts).date() if ts else None


def position_row(raw: dict, platform: str, as_of: float) -> dict:
    low = _low(raw)
    return {"platform": platform, "symbol": _pick(low, "tradingsymbol", default=""),
            "segment": _pick(low, "exchangesegment", default=""), "security_id": inum(_pick(low, "securityid")),
            "product": _pick(low, "producttype", default=""), "net_qty": inum(_pick(low, "netqty")),
            "buy_qty": inum(_pick(low, "buyqty", "daybuyqty")), "sell_qty": inum(_pick(low, "sellqty", "daysellqty")),
            "buy_avg": num(_pick(low, "buyavg")), "sell_avg": num(_pick(low, "sellavg")),
            "cost_price": num(_pick(low, "costprice")), "realized": num(_pick(low, "realizedprofit")),
            "unrealized": num(_pick(low, "unrealizedprofit")), "as_of_ts": as_of}


def holding_row(raw: dict, platform: str, as_of: float) -> dict:
    low = _low(raw)
    return {"platform": platform, "symbol": _pick(low, "tradingsymbol", default=""),
            "security_id": inum(_pick(low, "securityid")), "isin": _pick(low, "isin", default=""),
            "qty": inum(_pick(low, "totalqty", "dpqty", "availableqty")), "avg_cost": num(_pick(low, "avgcostprice")),
            "as_of_ts": as_of}


def funds_row(raw: dict, platform: str, as_of: float) -> dict:
    low = _low(raw)
    return {"platform": platform, "available": available_balance(raw) or 0.0,
            "utilized": num(_pick(low, "utilizedamount")), "sod_limit": num(_pick(low, "sodlimit")),
            "withdrawable": num(_pick(low, "withdrawablebalance")), "as_of_ts": as_of}


def order_row(raw: dict, platform: str) -> dict:
    u = normalise_order(raw)
    low = _low(raw)
    return {"platform": platform, "order_id": u.broker_id if u else str(_pick(low, "orderid", default="")),
            "correlation_id": u.cid if u else "", "symbol": _pick(low, "tradingsymbol", "symbol", default=""),
            "segment": _pick(low, "exchangesegment", default=""),
            "side": (u.side.value if u and u.side else str(_pick(low, "transactiontype", default=""))),
            "qty": inum(_pick(low, "quantity")), "filled_qty": inum(u.filled_qty if u else _pick(low, "filledqty")),
            "price": num(_pick(low, "price")), "avg_price": num(u.avg_price if u else 0),
            "status": (u.status.value if u and u.status else str(_pick(low, "orderstatus", default=""))),
            "product": _pick(low, "producttype", default=""), "order_type": _pick(low, "ordertype", default=""),
            "leg": _pick(low, "legname", default=""), "reason": u.reason if u else "",
            "source": "", "updated_ts": ist_ts(_pick(low, "updatetime", "createtime")) or 0.0}


CHARGE_FIELDS = {"brokerage": ("brokeragecharges", "brokerage"), "stt": ("stt",),
                 "exchange_txn": ("exchangetransactioncharges", "exchangecharges"), "sebi": ("sebitax", "sebicharges"),
                 "stamp_duty": ("stampduty",), "gst": ("servicetax", "gst", "igst")}


def trade_row(raw: dict, platform: str) -> dict:
    low = _low(raw)
    qty = inum(_pick(low, "tradedquantity", "quantity", "tradedqty"))
    price = num(_pick(low, "tradedprice", "price"))
    charges = {k: num(_pick(low, *aliases)) for k, aliases in CHARGE_FIELDS.items()}
    ts = ist_ts(_pick(low, "exchangetime", "createtime", "updatetime")) or 0.0
    row = {"platform": platform,
           "trade_id": str(_pick(low, "exchangetradeid", "tradeid", default="") or
                           f"{_pick(low, 'orderid', default='')}-{ts}-{qty}"),
           "order_id": str(_pick(low, "orderid", default="")), "symbol": _pick(low, "tradingsymbol", default=""),
           "segment": _pick(low, "exchangesegment", default=""), "security_id": inum(_pick(low, "securityid")),
           "side": str(_pick(low, "transactiontype", default="")).upper(), "qty": qty, "price": price,
           "value": round(qty * price, 2), "product": _pick(low, "producttype", default=""),
           "instrument": _pick(low, "instrument", default=""), **charges,
           "total_charges": round(sum(charges.values()), 2), "source": "", "trade_ts": ts}
    row["date"] = ist_datetime(ts).date() if ts else None
    return row


_LEDGER_RULES = (
    (re.compile(r"fund|upi|neft|imps|rtgs|pay ?in|deposit|added", re.I), "deposit"),
    (re.compile(r"withdraw|pay ?out|refund to bank", re.I), "withdrawal"),
    (re.compile(r"charge|brokerage|stt|gst|stamp|dp |dp$|amc|sebi|turnover", re.I), "charges"),
    (re.compile(r"settlement|mtm|p&l|pnl", re.I), "settlement"),
    (re.compile(r"bought|sold|buy|sell|trade", re.I), "trade"),
)


def ledger_category(narration: str, debit: float, credit: float) -> str:
    for rx, cat in _LEDGER_RULES:
        if rx.search(narration or ""):
            if cat == "deposit" and debit > 0 and credit == 0:
                return "withdrawal"
            return cat
    return "other"


def ledger_row(raw: dict, platform: str) -> dict:
    low = _low(raw)
    debit, credit = num(_pick(low, "debit")), num(_pick(low, "credit"))
    narr = str(_pick(low, "narration", "description", default=""))
    return {"platform": platform, "narration": narr, "voucher_desc": str(_pick(low, "voucherdesc", default="")),
            "voucher_no": str(_pick(low, "vouchernumber", "voucherno", default="")), "debit": debit, "credit": credit,
            "running_balance": num(_pick(low, "runbal", "runningbalance")),
            "category": ledger_category(narr, debit, credit), "date": ist_date(_pick(low, "voucherdate", "date"))}


def candle_rows(arrays: dict) -> list[dict]:
    ts = arrays.get("timestamp") or arrays.get("start_Time") or []
    out = []
    vols = arrays.get("volume") or [0] * len(ts)
    for t, o, h, lo, c, v in zip(ts, arrays.get("open", []), arrays.get("high", []), arrays.get("low", []),
                                 arrays.get("close", []), vols, strict=False):
        out.append({"day": ist_datetime(float(t)).date(), "open": float(o), "high": float(h), "low": float(lo),
                    "close": float(c), "volume": int(v or 0)})
    return out
