"""A local, in-process fake of the Dhan HQ v2 platform.

It speaks the same wire protocols as the real service so the *real* client code is exercised:

* REST  ``/v2/...``  (orders, super orders, positions, funds, quotes, charts, kill switch, P&L exit,
  profile, static IP, token renew) and ``/app/generateAccessToken`` (PIN+TOTP auth);
* binary market feed WebSocket ``/feed`` (ticker / quote / full packets, disconnect codes);
* order-update WebSocket ``/orderws`` (``order_alert`` JSON messages);
* instrument master CSV ``/api-data/api-scrip-master.csv``.

Prices follow a seeded random walk; a simple matching engine fills marketable orders against the
simulated best bid/ask and handles SL / SL-M triggers and super-order (entry + target + stop) legs.
Fault injection (timeouts, lost orders, rejects, 429s, token expiry, IP rejection, feed drops) lets
tests prove the client's recovery paths.

Message formats follow DhanHQ v2 documentation as closely as known; see README "Verify on first
live run" for the fields that could not be confirmed from the sandbox this was built in.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import itertools
import logging
import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import orjson
from aiohttp import WSMsgType, web

from ..clock import IST_OFFSET_S, RealClock, ist_datetime
from ..dhan import packets as P
from ..dhan.endpoints import SEGMENT_TO_CODE, Endpoints

log = logging.getLogger(__name__)

JSON_KEY: web.RequestKey[Any] = web.RequestKey("json", object)


def make_fake_jwt(client_id: str, exp: float, nonce: int = 0) -> str:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(orjson.dumps(obj)).decode().rstrip("=")

    return f"{b64({'typ': 'JWT', 'alg': 'HS512'})}.{b64({'dhanClientId': client_id, 'exp': int(exp), 'n': nonce})}.sig{nonce}"


# ---------------------------------------------------------------------------------------------
# instruments + price simulation
# ---------------------------------------------------------------------------------------------


@dataclass
class MockInstrument:
    security_id: int
    segment: str
    trading_symbol: str
    name: str
    instrument_type: str
    price: float
    lot: int = 1
    tick_paise: int = 5
    expiry: date | None = None
    strike: float = 0.0
    option_type: str = ""
    underlying: str = ""
    sigma_daily: float = 0.012
    # runtime
    bid: float = 0.0
    ask: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0
    volume: int = 0
    oi: int = 0
    depth_bids: list = field(default_factory=list)
    depth_asks: list = field(default_factory=list)

    @property
    def tick(self) -> float:
        return self.tick_paise / 100.0

    @property
    def feed_code(self) -> int:
        return SEGMENT_TO_CODE[self.segment]


def _next_weekday(d: date, weekday: int) -> date:
    days = (weekday - d.weekday()) % 7
    return d + timedelta(days=days or 7)


def default_instruments(today: date) -> list[MockInstrument]:
    insts = [
        MockInstrument(2885, "NSE_EQ", "RELIANCE", "RELIANCE INDUSTRIES LTD", "EQUITY", 1450.0, tick_paise=10),
        MockInstrument(1333, "NSE_EQ", "HDFCBANK", "HDFC BANK LTD", "EQUITY", 1980.0, tick_paise=10),
        MockInstrument(1594, "NSE_EQ", "INFY", "INFOSYS LIMITED", "EQUITY", 1520.0, tick_paise=10),
        MockInstrument(13, "IDX_I", "NIFTY", "Nifty 50", "INDEX", 24000.0, tick_paise=5, sigma_daily=0.009),
        MockInstrument(25, "IDX_I", "BANKNIFTY", "Nifty Bank", "INDEX", 52000.0, tick_paise=5, sigma_daily=0.011),
    ]
    exp1 = _next_weekday(today, 1)  # Tuesday expiries
    exp2 = exp1 + timedelta(days=7)
    insts.append(MockInstrument(35001, "NSE_FNO", f"NIFTY-{exp1:%b%Y}-FUT", "NIFTY FUT", "FUTIDX", 24050.0, lot=75,
                                expiry=exp1, underlying="NIFTY", sigma_daily=0.009))
    sid = itertools.count(40001)
    for exp in (exp1, exp2):
        for k in range(23500, 24550, 50):
            for ot in ("CE", "PE"):
                insts.append(MockInstrument(next(sid), "NSE_FNO", f"NIFTY-{exp:%b%Y}-{k}-{ot}", f"NIFTY {k} {ot}",
                                            "OPTIDX", 0.0, lot=75, expiry=exp, strike=float(k), option_type=ot,
                                            underlying="NIFTY"))
    return insts


class PriceSim:
    def __init__(self, instruments: list[MockInstrument], seed: int = 1) -> None:
        self.rng = random.Random(seed)
        self.by_key: dict[tuple[str, int], MockInstrument] = {(i.segment, i.security_id): i for i in instruments}
        self.instruments = instruments
        self.underlyings = {i.trading_symbol: i for i in instruments if i.instrument_type == "INDEX"}
        for i in instruments:
            if i.instrument_type.startswith("OPT"):
                i.price = self._option_price(i)
            i.price = self._round(i, i.price)
            i.open = i.high = i.low = i.price
            i.prev_close = self._round(i, i.price * (1 + self.rng.uniform(-0.01, 0.01)))
            self._book(i)

    @staticmethod
    def _round(i: MockInstrument, px: float) -> float:
        t = i.tick
        return round(max(t, round(px / t) * t), 2)

    def _option_price(self, i: MockInstrument) -> float:
        u = self.underlyings.get(i.underlying)
        spot = u.price if u else i.strike
        intrinsic = max(0.0, spot - i.strike) if i.option_type == "CE" else max(0.0, i.strike - spot)
        tv = 120.0 * math.exp(-abs(spot - i.strike) / 250.0)
        return max(0.05, intrinsic + tv)

    def _book(self, i: MockInstrument) -> None:
        if i.instrument_type == "INDEX":  # indices have no order book
            i.bid = i.ask = i.price
            i.depth_bids, i.depth_asks = [], []
            return
        i.bid = round(max(i.tick, i.price - i.tick), 2)
        i.ask = round(i.price + i.tick, 2)
        i.depth_bids = [(round(i.bid - n * i.tick, 2), self.rng.randint(1, 50) * i.lot, self.rng.randint(1, 20)) for n in range(5)]
        i.depth_asks = [(round(i.ask + n * i.tick, 2), self.rng.randint(1, 50) * i.lot, self.rng.randint(1, 20)) for n in range(5)]

    def step(self, dt_s: float, keys: set[tuple[str, int]] | None = None) -> list[MockInstrument]:
        """Advance prices by dt seconds. Underlyings first, then derived options."""
        changed = []
        frac = math.sqrt(max(dt_s, 1e-6) / 22500.0)  # ~6.25h session
        for i in self.instruments:
            if i.instrument_type.startswith("OPT"):
                continue
            if keys is not None and (i.segment, i.security_id) not in keys and i.instrument_type != "INDEX":
                continue
            i.price = self._round(i, i.price * math.exp(i.sigma_daily * frac * self.rng.gauss(0, 1)))
            changed.append(i)
        for i in self.instruments:
            if not i.instrument_type.startswith("OPT"):
                continue
            if keys is not None and (i.segment, i.security_id) not in keys:
                continue
            i.price = self._round(i, self._option_price(i))
            changed.append(i)
        for i in changed:
            i.high, i.low = max(i.high, i.price), min(i.low, i.price)
            if i.instrument_type != "INDEX":
                i.volume += self.rng.randint(1, 20) * i.lot
            self._book(i)
        return changed


# ---------------------------------------------------------------------------------------------
# orders / positions
# ---------------------------------------------------------------------------------------------


@dataclass
class MockOrder:
    order_id: str
    cid: str
    inst: MockInstrument
    side: str
    qty: int
    otype: str
    price: float
    trigger: float
    product: str
    validity: str
    leg: str = ""  # "" regular | ENTRY_LEG | TARGET_LEG | STOP_LOSS_LEG
    status: str = "TRANSIT"
    filled: int = 0
    avg: float = 0.0
    last_fill_price: float = 0.0
    reason: str = ""
    triggered: bool = False
    target: float = 0.0
    stop: float = 0.0
    trail: float = 0.0
    created: float = 0.0
    updated: float = 0.0

    @property
    def working(self) -> bool:
        return self.status in ("TRANSIT", "PENDING", "PART_TRADED", "TRIGGERED")


@dataclass
class MockPosition:
    inst: MockInstrument
    product: str
    buy_qty: int = 0
    buy_val: float = 0.0
    sell_qty: int = 0
    sell_val: float = 0.0

    @property
    def net(self) -> int:
        return self.buy_qty - self.sell_qty

    @property
    def buy_avg(self) -> float:
        return self.buy_val / self.buy_qty if self.buy_qty else 0.0

    @property
    def sell_avg(self) -> float:
        return self.sell_val / self.sell_qty if self.sell_qty else 0.0

    @property
    def realized(self) -> float:
        closed = min(self.buy_qty, self.sell_qty)
        return (self.sell_avg - self.buy_avg) * closed if closed else 0.0

    @property
    def unrealized(self) -> float:
        n = self.net
        if n > 0:
            return n * (self.inst.price - self.buy_avg)
        if n < 0:
            return -n * (self.sell_avg - self.inst.price)
        return 0.0


@dataclass
class Faults:
    latency_ms: float = 0.0  # added to every REST response
    place_timeout_once: bool = False  # order IS created but the response is delayed past the client timeout
    place_lost_once: bool = False  # order is NOT created and the response is delayed (client must mark FAILED)
    timeout_delay_s: float = 3.0
    reject_next: str = ""  # broker rejects next placement with this message (HTTP 400, DH-906)
    rate_limit_next: int = 0  # next N order calls get HTTP 429 / DH-904
    ip_rejected: bool = False  # order APIs fail with a static-IP error
    exchange_reject_next: str = ""  # order accepted then REJECTED by "exchange" via order update
    freeze_feed: bool = False  # stop sending ticks (stale-feed tests)


_LEG_NO = {"": 1, "ENTRY_LEG": 1, "STOP_LOSS_LEG": 2, "TARGET_LEG": 3}
_PRODUCT_CODE = {"INTRADAY": "I", "CNC": "C", "MARGIN": "M", "MTF": "F"}
_EXCH = {"NSE_EQ": ("NSE", "E"), "NSE_FNO": ("NSE", "D"), "BSE_EQ": ("BSE", "E"), "BSE_FNO": ("BSE", "D"),
         "MCX_COMM": ("MCX", "M"), "IDX_I": ("NSE", "I"), "NSE_CURRENCY": ("NSE", "C"), "BSE_CURRENCY": ("BSE", "C")}


class MockDhan:
    def __init__(self, clock: RealClock | None = None, client_id: str = "1000000001", seed: int = 7,
                 tick_interval_s: float = 1.0, ltt_mode: str = "ist_shifted", pin: str = "1234",
                 totp_secret: str = "JBSWY3DPEHPK3PXP", starting_balance: float = 500000.0,
                 instruments: list[MockInstrument] | None = None, auto_ticks: bool = True) -> None:
        self.clock = clock or RealClock()
        self.client_id = client_id
        self.pin = pin
        self.totp_secret = totp_secret
        self._token_n = itertools.count(1)
        self.token = make_fake_jwt(client_id, self.clock.time() + 86400, next(self._token_n))
        self.instruments = instruments or default_instruments(ist_datetime(self.clock.time()).date())
        self.sim = PriceSim(self.instruments, seed)
        self.tick_interval_s = tick_interval_s
        self.ltt_offset = IST_OFFSET_S if ltt_mode == "ist_shifted" else 0
        self.auto_ticks = auto_ticks
        self.faults = Faults()
        self.orders: dict[str, list[MockOrder]] = {}  # order id -> [entry/regular, (target leg), (stop leg)]
        self.by_cid: dict[str, str] = {}
        self.positions: dict[tuple[str, int, str], MockPosition] = {}
        self.balance = starting_balance
        self.killswitch = False
        self.pnl_exit: dict[str, Any] | None = None
        self.ips: dict[str, str] = {"primaryIP": "", "secondaryIP": ""}
        self.requests: list[tuple[str, str, Any]] = []  # (method, path, body) for assertions
        self.order_api_calls = 0
        self._ids = itertools.count(52100000001)
        self._feed_clients: dict[web.WebSocketResponse, dict[tuple[str, int], int]] = {}
        self._order_clients: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None
        self._ticker_task: asyncio.Task | None = None
        self.port = 0

    # ---- lifecycle -----------------------------------------------------------------------
    async def start(self, host: str = "127.0.0.1", port: int = 0) -> Endpoints:
        app = web.Application(middlewares=[self._middleware])
        r = app.router
        r.add_post("/v2/orders", self.h_place)
        r.add_post("/v2/orders/slicing", self.h_place)
        r.add_get("/v2/orders", self.h_list_orders)
        r.add_get("/v2/orders/external/{cid}", self.h_order_by_cid)
        r.add_get("/v2/orders/{oid}", self.h_get_order)
        r.add_put("/v2/orders/{oid}", self.h_modify)
        r.add_delete("/v2/orders/{oid}", self.h_cancel)
        r.add_post("/v2/super/orders", self.h_place_super)
        r.add_get("/v2/super/orders", self.h_list_orders)
        r.add_put("/v2/super/orders/{oid}", self.h_modify)
        r.add_delete("/v2/super/orders/{oid}/{leg}", self.h_cancel_super)
        r.add_get("/v2/positions", self.h_positions)
        r.add_delete("/v2/positions", self.h_exit_all)
        r.add_get("/v2/holdings", self._const([]))
        r.add_get("/v2/fundlimit", self.h_funds)
        r.add_post("/v2/margincalculator", self.h_margin)
        r.add_get("/v2/trades/", self._const([]))
        r.add_post("/v2/marketfeed/quote", self.h_quote)
        r.add_post("/v2/marketfeed/ltp", self.h_quote)
        r.add_post("/v2/marketfeed/ohlc", self.h_quote)
        r.add_post("/v2/charts/intraday", self.h_intraday)
        r.add_post("/v2/killswitch", self.h_killswitch)
        r.add_get("/v2/killswitch", self._dyn(lambda: {"dhanClientId": self.client_id,
                                                       "killSwitchStatus": "ACTIVATE" if self.killswitch else "DEACTIVATE"}))
        r.add_post("/v2/pnlExit", self.h_pnl_exit_set)
        r.add_get("/v2/pnlExit", self._dyn(lambda: self.pnl_exit or {"pnlExitStatus": "INACTIVE"}))
        r.add_delete("/v2/pnlExit", self.h_pnl_exit_stop)
        r.add_get("/v2/profile", self.h_profile)
        r.add_get("/v2/RenewToken", self.h_renew)
        r.add_get("/v2/ip/getIP", self._dyn(lambda: {"dhanClientId": self.client_id, **self.ips}))
        r.add_post("/v2/ip/setIP", self.h_set_ip)
        r.add_put("/v2/ip/modifyIP", self.h_set_ip)
        r.add_post("/app/generateAccessToken", self.h_generate_token)
        r.add_get("/api-data/api-scrip-master.csv", self.h_csv)
        r.add_get("/feed", self.h_feed_ws)
        r.add_get("/orderws", self.h_order_ws)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        if self.auto_ticks:
            self._ticker_task = asyncio.create_task(self._tick_loop())
        return self.endpoints()

    def endpoints(self) -> Endpoints:
        base = f"127.0.0.1:{self.port}"
        return Endpoints(rest_base=f"http://{base}/v2", auth_base=f"http://{base}", feed_url=f"ws://{base}/feed",
                         order_ws_url=f"ws://{base}/orderws",
                         instruments_csv_url=f"http://{base}/api-data/api-scrip-master.csv")

    async def stop(self) -> None:
        if self._ticker_task:
            self._ticker_task.cancel()
            try:
                await self._ticker_task
            except (asyncio.CancelledError, Exception):
                pass
        for ws in list(self._feed_clients) + list(self._order_clients):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    # ---- helpers -------------------------------------------------------------------------
    def _const(self, obj: Any):
        async def handler(request: web.Request) -> web.Response:
            return self._json(obj)
        return handler

    def _dyn(self, fn):
        async def handler(request: web.Request) -> web.Response:
            return self._json(fn())
        return handler

    @staticmethod
    def _json(obj: Any, status: int = 200) -> web.Response:
        return web.Response(body=orjson.dumps(obj), status=status, content_type="application/json")

    def _err(self, status: int, code: str, msg: str, typ: str = "Order_Error") -> web.Response:
        return self._json({"errorType": typ, "errorCode": code, "errorMessage": msg}, status)

    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        path = request.path
        if path.startswith("/v2/"):
            body = None
            if request.can_read_body:
                raw = await request.read()
                try:
                    body = orjson.loads(raw) if raw else None
                except orjson.JSONDecodeError:
                    return self._err(400, "DH-905", "invalid JSON", "Input_Exception")
            request[JSON_KEY] = body
            self.requests.append((request.method, path, body))
            token = request.headers.get("access-token", "")
            if token != self.token:
                return self._err(401, "DH-901", "Invalid or expired access token", "Invalid_Authentication")
            if self.faults.latency_ms:
                await asyncio.sleep(self.faults.latency_ms / 1000.0)
        return await handler(request)

    def now(self) -> float:
        return self.clock.time()

    def _ts(self) -> str:
        return ist_datetime(self.now()).strftime("%Y-%m-%d %H:%M:%S")

    def find(self, segment: str, security_id: int) -> MockInstrument | None:
        return self.sim.by_key.get((segment, int(security_id)))

    def instrument(self, symbol: str) -> MockInstrument:
        for i in self.instruments:
            if i.trading_symbol == symbol:
                return i
        raise KeyError(symbol)

    def set_price(self, symbol: str, price: float) -> None:
        """Test helper: jump a price and run matching + publish ticks immediately."""
        i = self.instrument(symbol)
        i.price = PriceSim._round(i, price)
        i.high, i.low = max(i.high, i.price), min(i.low, i.price)
        self.sim._book(i)
        self._match_instrument(i)
        self._publish([i])
        self._check_pnl_exit()

    def expire_token(self) -> None:
        """Invalidate the current token: REST -> 401 DH-901, feed -> disconnect 807."""
        self.token = make_fake_jwt(self.client_id, self.now() + 86400, next(self._token_n))
        for ws in list(self._feed_clients):
            asyncio.ensure_future(self._send_and_close(ws, P.build_disconnect(807)))

    async def _send_and_close(self, ws: web.WebSocketResponse, data: bytes) -> None:
        try:
            await ws.send_bytes(data)
            await ws.close()
        except Exception:
            pass

    async def drop_feeds(self) -> None:
        for ws in list(self._feed_clients):
            await ws.close()

    async def drop_order_ws(self) -> None:
        for ws in list(self._order_clients):
            await ws.close()

    # ---- REST: orders --------------------------------------------------------------------
    def _order_guard(self) -> web.Response | None:
        self.order_api_calls += 1
        f = self.faults
        if f.ip_rejected:
            return self._err(403, "DH-905", "Order request from unregistered static IP address", "Input_Exception")
        if f.rate_limit_next > 0:
            f.rate_limit_next -= 1
            return self._err(429, "DH-904", "Too many requests", "Rate_Limit")
        return None

    def _new_order(self, body: dict, leg: str = "") -> MockOrder | web.Response:
        try:
            inst = self.find(body["exchangeSegment"], int(body["securityId"]))
            qty = int(body["quantity"])
            side = body["transactionType"]
            otype = body["orderType"]
        except (KeyError, TypeError, ValueError):
            return self._err(400, "DH-905", "Missing or invalid order fields", "Input_Exception")
        if inst is None or inst.segment == "IDX_I":
            return self._err(400, "DH-905", "Invalid securityId / exchangeSegment", "Input_Exception")
        if body.get("dhanClientId") != self.client_id:
            return self._err(400, "DH-905", "dhanClientId mismatch", "Input_Exception")
        if qty <= 0 or qty % inst.lot:
            return self._err(400, "DH-905", f"quantity must be a multiple of lot size {inst.lot}", "Input_Exception")
        if side not in ("BUY", "SELL") or otype not in ("LIMIT", "MARKET", "STOP_LOSS", "STOP_LOSS_MARKET"):
            return self._err(400, "DH-905", "invalid transactionType/orderType", "Input_Exception")
        price = float(body.get("price") or 0)
        if otype in ("LIMIT", "STOP_LOSS") and price <= 0:
            return self._err(400, "DH-905", "price required", "Input_Exception")
        if price and abs(round(price * 100) % inst.tick_paise) > 1e-6:
            return self._err(400, "DH-905", f"price {price} not a multiple of tick {inst.tick}", "Input_Exception")
        if self.killswitch:
            return self._err(400, "DH-906", "Kill switch is active for the day", "Order_Error")
        if self.faults.reject_next:
            msg, self.faults.reject_next = self.faults.reject_next, ""
            return self._err(400, "DH-906", msg, "Order_Error")
        cid = str(body.get("correlationId") or "")
        o = MockOrder(str(next(self._ids)), cid, inst, side, qty, otype, price, float(body.get("triggerPrice") or 0),
                      body.get("productType", "INTRADAY"), body.get("validity", "DAY"), leg=leg,
                      created=self.now(), updated=self.now())
        return o

    async def h_place(self, req: web.Request) -> web.Response:
        if (g := self._order_guard()) is not None:
            return g
        lost = self.faults.place_lost_once
        if lost:
            self.faults.place_lost_once = False
            await asyncio.sleep(self.faults.timeout_delay_s)
            return self._json({"orderId": "", "orderStatus": "TRANSIT"})
        o = self._new_order(req[JSON_KEY] or {})
        if isinstance(o, web.Response):
            return o
        self._register(o)
        if self.faults.place_timeout_once:
            self.faults.place_timeout_once = False
            self._after_place(o)
            await asyncio.sleep(self.faults.timeout_delay_s)
        else:
            asyncio.get_running_loop().call_soon(self._after_place, o)
        return self._json({"orderId": o.order_id, "orderStatus": "TRANSIT"})

    async def h_place_super(self, req: web.Request) -> web.Response:
        if (g := self._order_guard()) is not None:
            return g
        body = req[JSON_KEY] or {}
        o = self._new_order(body, leg="ENTRY_LEG")
        if isinstance(o, web.Response):
            return o
        o.target = float(body.get("targetPrice") or 0)
        o.stop = float(body.get("stopLossPrice") or 0)
        o.trail = float(body.get("trailingJump") or 0)
        if o.target <= 0 and o.stop <= 0:
            return self._err(400, "DH-905", "targetPrice or stopLossPrice required", "Input_Exception")
        self._register(o)
        asyncio.get_running_loop().call_soon(self._after_place, o)
        return self._json({"orderId": o.order_id, "orderStatus": "TRANSIT"})

    def _register(self, o: MockOrder) -> None:
        self.orders[o.order_id] = [o]
        if o.cid:
            self.by_cid[o.cid] = o.order_id

    def _after_place(self, o: MockOrder) -> None:
        if o.status != "TRANSIT":
            return
        if self.faults.exchange_reject_next:
            o.status, o.reason = "REJECTED", self.faults.exchange_reject_next
            self.faults.exchange_reject_next = ""
            self._emit(o)
            return
        o.status = "PENDING"
        self._emit(o)
        self._match_order(o)

    def _all_legs(self):
        for legs in self.orders.values():
            yield from legs

    def _order_json(self, o: MockOrder) -> dict:
        return {
            "dhanClientId": self.client_id, "orderId": o.order_id, "correlationId": o.cid, "orderStatus": o.status,
            "transactionType": o.side, "exchangeSegment": o.inst.segment, "productType": o.product,
            "orderType": o.otype, "validity": o.validity, "tradingSymbol": o.inst.trading_symbol,
            "securityId": str(o.inst.security_id), "quantity": o.qty, "disclosedQuantity": 0, "price": o.price,
            "triggerPrice": o.trigger, "afterMarketOrder": False, "legName": o.leg or "NA",
            "createTime": self._ts(), "updateTime": self._ts(), "exchangeTime": self._ts(),
            "remainingQuantity": o.qty - o.filled, "averageTradedPrice": round(o.avg, 2), "filledQty": o.filled,
            "omsErrorCode": "", "omsErrorDescription": o.reason,
        }

    async def h_list_orders(self, req: web.Request) -> web.Response:
        return self._json([self._order_json(o) for o in self._all_legs()])

    async def h_get_order(self, req: web.Request) -> web.Response:
        legs = self.orders.get(req.match_info["oid"])
        if not legs:
            return self._err(404, "DH-905", "order not found", "Input_Exception")
        return self._json(self._order_json(legs[0]))

    async def h_order_by_cid(self, req: web.Request) -> web.Response:
        oid = self.by_cid.get(req.match_info["cid"])
        if not oid:
            return self._err(404, "DH-905", "No order found for correlation id", "Input_Exception")
        return self._json(self._order_json(self.orders[oid][0]))

    async def h_modify(self, req: web.Request) -> web.Response:
        if (g := self._order_guard()) is not None:
            return g
        body = req[JSON_KEY] or {}
        legs = self.orders.get(req.match_info["oid"])
        if not legs:
            return self._err(404, "DH-905", "order not found", "Input_Exception")
        leg_name = body.get("legName") or ""
        o = next((x for x in legs if (x.leg or "") == leg_name), legs[0]) if leg_name else legs[0]
        if not o.working:
            return self._err(400, "DH-906", f"order not modifiable in status {o.status}", "Order_Error")
        if "price" in body and body["price"] is not None:
            o.price = float(body["price"])
        if "stopLossPrice" in body and o.leg == "STOP_LOSS_LEG":
            o.trigger = float(body["stopLossPrice"])
        if "targetPrice" in body and o.leg == "TARGET_LEG":
            o.price = float(body["targetPrice"])
        if body.get("quantity"):
            o.qty = int(body["quantity"])
        if body.get("triggerPrice"):
            o.trigger = float(body["triggerPrice"])
        if body.get("orderType"):
            o.otype = body["orderType"]
        o.updated = self.now()
        self._emit(o)
        self._match_order(o)
        return self._json({"orderId": o.order_id, "orderStatus": o.status})

    async def h_cancel(self, req: web.Request) -> web.Response:
        if (g := self._order_guard()) is not None:
            return g
        legs = self.orders.get(req.match_info["oid"])
        if not legs:
            return self._err(404, "DH-905", "order not found", "Input_Exception")
        changed = False
        for o in legs:
            if o.working:
                o.status, o.updated, changed = "CANCELLED", self.now(), True
                self._emit(o)
        if not changed:
            return self._err(400, "DH-906", f"order not cancellable in status {legs[0].status}", "Order_Error")
        return self._json({"orderId": legs[0].order_id, "orderStatus": "CANCELLED"})

    async def h_cancel_super(self, req: web.Request) -> web.Response:
        if (g := self._order_guard()) is not None:
            return g
        legs = self.orders.get(req.match_info["oid"])
        leg = req.match_info["leg"]
        if not legs:
            return self._err(404, "DH-905", "order not found", "Input_Exception")
        targets = legs if leg == "ENTRY_LEG" else [x for x in legs if x.leg == leg]
        changed = False
        for o in targets:
            if o.working:
                o.status, o.updated, changed = "CANCELLED", self.now(), True
                self._emit(o)
        if leg != "ENTRY_LEG":  # remember so it is not created later
            legs[0].__dict__.setdefault("_cancelled_legs", set()).add(leg)
        if not changed and leg == "ENTRY_LEG":
            return self._err(400, "DH-906", "nothing to cancel", "Order_Error")
        return self._json({"orderId": legs[0].order_id, "orderStatus": "CANCELLED"})

    # ---- matching --------------------------------------------------------------------------
    def _match_instrument(self, inst: MockInstrument) -> None:
        for o in list(self._all_legs()):
            if o.inst is inst and o.working and o.status != "TRANSIT":
                self._match_order(o)

    def _match_order(self, o: MockOrder) -> None:
        if not o.working or o.status == "TRANSIT":
            return
        i = o.inst
        otype = o.otype
        if otype in ("STOP_LOSS", "STOP_LOSS_MARKET") and not o.triggered:
            hit = i.price >= o.trigger if o.side == "BUY" else i.price <= o.trigger
            if not hit:
                return
            o.triggered = True
            o.status = "TRIGGERED"
            otype = "MARKET" if otype == "STOP_LOSS_MARKET" else "LIMIT"
        elif otype == "STOP_LOSS_MARKET":
            otype = "MARKET"
        elif otype == "STOP_LOSS":
            otype = "LIMIT"
        if otype == "MARKET":
            px = i.ask if o.side == "BUY" else i.bid
        else:
            if o.side == "BUY" and i.ask <= o.price:
                px = i.ask
            elif o.side == "SELL" and i.bid >= o.price:
                px = i.bid
            else:
                return
        self._fill(o, o.qty - o.filled, px)

    def _fill(self, o: MockOrder, qty: int, px: float) -> None:
        if qty <= 0:
            return
        o.avg = (o.avg * o.filled + px * qty) / (o.filled + qty)
        o.filled += qty
        o.last_fill_price = px
        o.status = "TRADED" if o.filled >= o.qty else "PART_TRADED"
        o.updated = self.now()
        key = (o.inst.segment, o.inst.security_id, o.product)
        pos = self.positions.get(key) or self.positions.setdefault(key, MockPosition(o.inst, o.product))
        if o.side == "BUY":
            pos.buy_qty += qty
            pos.buy_val += px * qty
        else:
            pos.sell_qty += qty
            pos.sell_val += px * qty
        self._emit(o)
        legs = self.orders.get(o.order_id, [])
        if o.leg == "ENTRY_LEG" and o.status == "TRADED":
            self._spawn_super_legs(legs[0])
        elif o.leg in ("TARGET_LEG", "STOP_LOSS_LEG") and o.status == "TRADED":
            for other in legs[1:]:
                if other is not o and other.working:
                    other.status, other.updated = "CANCELLED", self.now()
                    self._emit(other)

    def _spawn_super_legs(self, entry: MockOrder) -> None:
        legs = self.orders[entry.order_id]
        if len(legs) > 1:
            return
        cancelled = entry.__dict__.get("_cancelled_legs", set())
        exit_side = "SELL" if entry.side == "BUY" else "BUY"
        if entry.target > 0 and "TARGET_LEG" not in cancelled:
            legs.append(MockOrder(entry.order_id, entry.cid, entry.inst, exit_side, entry.qty, "LIMIT", entry.target, 0.0,
                                  entry.product, entry.validity, leg="TARGET_LEG", status="PENDING",
                                  created=self.now(), updated=self.now()))
        if entry.stop > 0 and "STOP_LOSS_LEG" not in cancelled:
            legs.append(MockOrder(entry.order_id, entry.cid, entry.inst, exit_side, entry.qty, "STOP_LOSS_MARKET", 0.0,
                                  entry.stop, entry.product, entry.validity, leg="STOP_LOSS_LEG", status="PENDING",
                                  trail=entry.trail, created=self.now(), updated=self.now()))
        for leg in legs[1:]:
            self._emit(leg)
            self._match_order(leg)

    def _trail(self, inst: MockInstrument) -> None:
        for legs in self.orders.values():
            entry = legs[0]
            if entry.inst is not inst or entry.trail <= 0 or entry.leg != "ENTRY_LEG":
                continue
            for leg in legs[1:]:
                if leg.leg != "STOP_LOSS_LEG" or not leg.working:
                    continue
                if entry.side == "BUY":
                    best = max(leg.__dict__.get("_best", entry.avg), inst.price)
                    leg.__dict__["_best"] = best
                    new_trig = round(best - (entry.avg - entry.stop), 2)
                    if new_trig >= leg.trigger + entry.trail:
                        leg.trigger = new_trig
                else:
                    best = min(leg.__dict__.get("_best", entry.avg), inst.price)
                    leg.__dict__["_best"] = best
                    new_trig = round(best + (entry.stop - entry.avg), 2)
                    if new_trig <= leg.trigger - entry.trail:
                        leg.trigger = new_trig

    # ---- order update websocket ----------------------------------------------------------
    def _update_msg(self, o: MockOrder) -> bytes:
        exch, seg = _EXCH.get(o.inst.segment, ("NSE", "E"))
        status_text = {"TRANSIT": "Transit", "PENDING": "Pending", "TRADED": "Traded", "PART_TRADED": "Part_Traded",
                       "CANCELLED": "Cancelled", "REJECTED": "Rejected", "EXPIRED": "Expired",
                       "TRIGGERED": "Triggered"}.get(o.status, o.status)
        data = {
            "Exchange": exch, "Segment": seg, "Source": "P", "SecurityId": str(o.inst.security_id),
            "ClientId": self.client_id, "ExchOrderNo": f"1100000{o.order_id[-8:]}", "OrderNo": o.order_id,
            "Product": _PRODUCT_CODE.get(o.product, "I"), "TxnType": "B" if o.side == "BUY" else "S",
            "OrderType": {"LIMIT": "LMT", "MARKET": "MKT", "STOP_LOSS": "SL", "STOP_LOSS_MARKET": "SLM"}[o.otype],
            "Validity": o.validity, "Quantity": o.qty, "TradedQty": o.filled, "RemainingQuantity": o.qty - o.filled,
            "Price": o.price, "TriggerPrice": o.trigger, "TradedPrice": o.last_fill_price,
            "AvgTradedPrice": round(o.avg, 2), "Status": status_text, "LegNo": _LEG_NO.get(o.leg, 1),
            "CorrelationId": o.cid, "ReasonDescription": o.reason, "Symbol": o.inst.trading_symbol,
            "LastUpdatedTime": self._ts(), "Instrument": o.inst.instrument_type, "LotSize": o.inst.lot,
        }
        return orjson.dumps({"Type": "order_alert", "Data": data})

    def _emit(self, o: MockOrder) -> None:
        if not self._order_clients:
            return
        msg = self._update_msg(o).decode()
        for ws in list(self._order_clients):
            if not ws.closed:
                asyncio.ensure_future(self._safe_send_str(ws, msg))

    @staticmethod
    async def _safe_send_str(ws: web.WebSocketResponse, msg: str) -> None:
        try:
            await ws.send_str(msg)
        except Exception:
            pass

    async def h_order_ws(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(req)
        authed = False
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = orjson.loads(msg.data)
                login = data.get("LoginReq") or {}
                if not authed:
                    if login.get("MsgCode") == 42 and login.get("ClientId") == self.client_id and login.get("Token") == self.token:
                        authed = True
                        self._order_clients.add(ws)
                    else:
                        await ws.close(code=4001, message=b"auth failed")
                        break
        finally:
            self._order_clients.discard(ws)
        return ws

    # ---- market feed websocket -----------------------------------------------------------
    async def h_feed_ws(self, req: web.Request) -> web.WebSocketResponse:
        q = req.query
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(req)
        if q.get("version") != "2" or q.get("authType") != "2" or q.get("clientId") != self.client_id:
            await ws.send_bytes(P.build_disconnect(808))
            await ws.close()
            return ws
        if q.get("token") != self.token:
            await ws.send_bytes(P.build_disconnect(807))
            await ws.close()
            return ws
        subs: dict[tuple[str, int], int] = {}
        self._feed_clients[ws] = subs
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = orjson.loads(msg.data)
                code = int(data.get("RequestCode", 0))
                if code == 12:
                    break
                items = data.get("InstrumentList") or []
                if len(items) > 100 or data.get("InstrumentCount") != len(items):
                    log.warning("mock feed: bad subscription message %s", data)
                    continue
                for it in items:
                    key = (it["ExchangeSegment"], int(it["SecurityId"]))
                    if code in (15, 17, 21):
                        subs[key] = max(code, subs.get(key, 0))
                        inst = self.sim.by_key.get(key)
                        if inst:
                            await ws.send_bytes(P.build_prev_close(inst.feed_code, inst.security_id, inst.prev_close)
                                                + self._packet(inst, subs[key]))
                    elif code in (16, 18, 22):
                        subs.pop(key, None)
        finally:
            self._feed_clients.pop(ws, None)
        return ws

    def _packet(self, i: MockInstrument, mode: int) -> bytes:
        ltt = int(self.now()) + self.ltt_offset
        if mode == 15:
            return P.build_ticker(i.feed_code, i.security_id, i.price, ltt)
        common = dict(ltq=i.lot, atp=round((i.high + i.low) / 2, 2), volume=i.volume, tot_sell=1000, tot_buy=1200,
                      open=i.open, close=i.prev_close, high=i.high, low=i.low)
        if mode == 17:
            return P.build_quote(i.feed_code, i.security_id, i.price, ltt, **common)
        return P.build_full(i.feed_code, i.security_id, i.price, ltt, oi=i.oi, bids=i.depth_bids, asks=i.depth_asks,
                            **common)

    def subscribed_keys(self) -> set[tuple[str, int]]:
        keys: set[tuple[str, int]] = set()
        for subs in self._feed_clients.values():
            keys.update(subs)
        for o in self._all_legs():
            if o.working:
                keys.add((o.inst.segment, o.inst.security_id))
        return keys

    def _publish(self, changed: list[MockInstrument]) -> None:
        if self.faults.freeze_feed:
            return
        changed_keys = {(i.segment, i.security_id): i for i in changed}
        for ws, subs in list(self._feed_clients.items()):
            if ws.closed:
                continue
            frame = b"".join(self._packet(changed_keys[k], mode) for k, mode in subs.items() if k in changed_keys)
            if frame:
                asyncio.ensure_future(self._safe_send_bytes(ws, frame))

    @staticmethod
    async def _safe_send_bytes(ws: web.WebSocketResponse, data: bytes) -> None:
        try:
            await ws.send_bytes(data)
        except Exception:
            pass

    def step(self, dt_s: float | None = None) -> None:
        """Advance prices one step, match orders, publish ticks (also callable from tests)."""
        dt = self.tick_interval_s if dt_s is None else dt_s
        changed = self.sim.step(dt, self.subscribed_keys())
        for i in changed:
            self._trail(i)
            self._match_instrument(i)
        self._publish(changed)
        self._check_pnl_exit()

    async def _tick_loop(self) -> None:
        while True:
            await self.clock.sleep(self.tick_interval_s)
            try:
                self.step()
            except Exception:
                log.exception("mock tick step failed")

    # ---- positions / funds / misc ----------------------------------------------------------
    def _pos_json(self, p: MockPosition) -> dict:
        n = p.net
        return {
            "dhanClientId": self.client_id, "tradingSymbol": p.inst.trading_symbol,
            "securityId": str(p.inst.security_id), "positionType": "LONG" if n > 0 else "SHORT" if n < 0 else "CLOSED",
            "exchangeSegment": p.inst.segment, "productType": p.product, "buyAvg": round(p.buy_avg, 2),
            "buyQty": p.buy_qty, "costPrice": round(p.buy_avg if n >= 0 else p.sell_avg, 2),
            "sellAvg": round(p.sell_avg, 2), "sellQty": p.sell_qty, "netQty": n,
            "realizedProfit": round(p.realized, 2), "unrealizedProfit": round(p.unrealized, 2),
            "multiplier": 1, "dayBuyQty": p.buy_qty, "daySellQty": p.sell_qty,
            "dayBuyValue": round(p.buy_val, 2), "daySellValue": round(p.sell_val, 2),
        }

    def total_pnl(self) -> float:
        return sum(p.realized + p.unrealized for p in self.positions.values())

    async def h_positions(self, req: web.Request) -> web.Response:
        return self._json([self._pos_json(p) for p in self.positions.values()])

    def exit_all(self) -> None:
        for p in list(self.positions.values()):
            n = p.net
            if n == 0:
                continue
            o = MockOrder(str(next(self._ids)), "", p.inst, "SELL" if n > 0 else "BUY", abs(n), "MARKET", 0.0, 0.0,
                          p.product, "DAY", status="PENDING", created=self.now(), updated=self.now())
            self._register(o)
            self._fill(o, o.qty, p.inst.bid if n > 0 else p.inst.ask)
        for o in list(self._all_legs()):
            if o.working:
                o.status = "CANCELLED"
                self._emit(o)

    async def h_exit_all(self, req: web.Request) -> web.Response:
        if (g := self._order_guard()) is not None:
            return g
        self.exit_all()
        return self._json({"status": "SUCCESS", "message": "All positions exited"})

    async def h_funds(self, req: web.Request) -> web.Response:
        realized = sum(p.realized for p in self.positions.values())
        used = sum(abs(p.net) * p.inst.price * (0.2 if p.inst.segment.endswith("EQ") else 1.0)
                   for p in self.positions.values())
        avail = self.balance + realized - used
        return self._json({"dhanClientId": self.client_id, "availabelBalance": round(avail, 2),
                           "sodLimit": self.balance, "collateralAmount": 0.0, "receiveableAmount": 0.0,
                           "utilizedAmount": round(used, 2), "blockedPayoutAmount": 0.0,
                           "withdrawableBalance": round(avail, 2)})

    async def h_margin(self, req: web.Request) -> web.Response:
        b = req[JSON_KEY] or {}
        value = float(b.get("price") or 0) * int(b.get("quantity") or 0)
        factor = 0.2 if b.get("productType") == "INTRADAY" and str(b.get("exchangeSegment", "")).endswith("EQ") else 1.0
        return self._json({"totalMargin": round(value * factor, 2), "availableBalance": self.balance,
                           "insufficientBalance": 0.0, "leverage": f"{1 / factor:.2f}"})

    async def h_quote(self, req: web.Request) -> web.Response:
        body = req[JSON_KEY] or {}
        out: dict[str, dict[str, Any]] = {}
        for seg, ids in body.items():
            if not isinstance(ids, list):
                continue
            for sid in ids:
                i = self.find(seg, int(sid))
                if not i:
                    continue
                out.setdefault(seg, {})[str(sid)] = {
                    "last_price": i.price, "average_price": i.price, "volume": i.volume,
                    "upper_circuit_limit": round(i.prev_close * 1.2, 2), "lower_circuit_limit": round(i.prev_close * 0.8, 2),
                    "ohlc": {"open": i.open, "high": i.high, "low": i.low, "close": i.prev_close},
                    "depth": {"buy": [{"price": p, "quantity": q, "orders": n} for p, q, n in i.depth_bids],
                              "sell": [{"price": p, "quantity": q, "orders": n} for p, q, n in i.depth_asks]},
                    "oi": i.oi, "net_change": round(i.price - i.prev_close, 2),
                }
        return self._json({"data": out, "status": "success"})

    async def h_intraday(self, req: web.Request) -> web.Response:
        b = req[JSON_KEY] or {}
        i = self.find(b.get("exchangeSegment", ""), int(b.get("securityId") or 0))
        if i is None:
            return self._err(400, "DH-905", "invalid instrument", "Input_Exception")
        interval = int(b.get("interval") or 1) * 60
        n = 60
        rng = random.Random(i.security_id)
        px = i.prev_close
        ts0 = int(self.now()) // interval * interval - n * interval
        o = []
        h = []
        lo = []
        c = []
        v = []
        t = []
        for k in range(n):
            op = px
            px = round(px * math.exp(0.002 * rng.gauss(0, 1)), 2)
            o.append(op)
            c.append(px)
            h.append(round(max(op, px) * 1.0005, 2))
            lo.append(round(min(op, px) * 0.9995, 2))
            v.append(rng.randint(1000, 5000))
            t.append(ts0 + k * interval)
        return self._json({"open": o, "high": h, "low": lo, "close": c, "volume": v, "timestamp": t})

    async def h_killswitch(self, req: web.Request) -> web.Response:
        status = req.query.get("killSwitchStatus", "").upper()
        if status not in ("ACTIVATE", "DEACTIVATE"):
            return self._err(400, "DH-905", "killSwitchStatus must be ACTIVATE or DEACTIVATE", "Input_Exception")
        self.killswitch = status == "ACTIVATE"
        return self._json({"dhanClientId": self.client_id,
                           "killSwitchStatus": f"Kill Switch has been successfully {status.lower()}d"})

    async def h_pnl_exit_set(self, req: web.Request) -> web.Response:
        b = req[JSON_KEY] or {}
        self.pnl_exit = {"pnlExitStatus": "ACTIVE", "profit": float(b.get("profitValue") or 0),
                         "loss": float(b.get("lossValue") or 0), "productType": b.get("productType") or [],
                         "enableKillSwitch": bool(b.get("enableKillSwitch"))}
        return self._json({"pnlExitStatus": "ACTIVE", "message": "P&L based exit configured"})

    async def h_pnl_exit_stop(self, req: web.Request) -> web.Response:
        self.pnl_exit = None
        return self._json({"pnlExitStatus": "DISABLED"})

    def _check_pnl_exit(self) -> None:
        cfg = self.pnl_exit
        if not cfg or cfg.get("pnlExitStatus") != "ACTIVE":
            return
        pnl = self.total_pnl()
        if (cfg["loss"] and pnl <= -cfg["loss"]) or (cfg["profit"] and pnl >= cfg["profit"]):
            log.warning("mock: server-side P&L exit triggered at %.2f", pnl)
            self.exit_all()
            cfg["pnlExitStatus"] = "TRIGGERED"
            if cfg["enableKillSwitch"]:
                self.killswitch = True

    async def h_profile(self, req: web.Request) -> web.Response:
        exp = ist_datetime(self.now() + 86400).strftime("%d/%m/%Y %H:%M")
        return self._json({"dhanClientId": self.client_id, "tokenValidity": exp,
                           "activeSegment": "Equity, Derivative, Currency, Commodity", "ddpi": "Active",
                           "mtf": "Active", "dataPlan": "Active",
                           "dataValidity": ist_datetime(self.now() + 30 * 86400).strftime("%Y-%m-%d %H:%M:%S.0")})

    async def h_renew(self, req: web.Request) -> web.Response:
        self.token = make_fake_jwt(self.client_id, self.now() + 86400, next(self._token_n))
        return self._json({"accessToken": self.token, "expiryTime": ist_datetime(self.now() + 86400).isoformat()})

    async def h_generate_token(self, req: web.Request) -> web.Response:
        import pyotp

        q = req.query
        if q.get("dhanClientId") != self.client_id or q.get("pin") != self.pin:
            return self._json({"status": "error", "message": "Invalid client id or pin"}, 400)
        if not pyotp.TOTP(self.totp_secret).verify(q.get("totp", ""), for_time=self.now(), valid_window=1):
            return self._json({"status": "error", "message": "Invalid TOTP"}, 400)
        self.token = make_fake_jwt(self.client_id, self.now() + 86400, next(self._token_n))
        return self._json({"dhanClientId": self.client_id, "dhanClientName": "MOCK USER", "accessToken": self.token,
                           "expiryTime": ist_datetime(self.now() + 86400).isoformat()})

    async def h_set_ip(self, req: web.Request) -> web.Response:
        b = req[JSON_KEY] or {}
        flag = "primaryIP" if b.get("ipFlag", "PRIMARY") == "PRIMARY" else "secondaryIP"
        self.ips[flag] = str(b.get("ip", ""))
        return self._json({"status": "SUCCESS", "message": "IP saved"})

    async def h_csv(self, req: web.Request) -> web.Response:
        return web.Response(text=self.instrument_csv(), content_type="text/csv")

    def instrument_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["SEM_EXM_EXCH_ID", "SEM_SEGMENT", "SEM_SMST_SECURITY_ID", "SEM_INSTRUMENT_NAME", "SEM_EXPIRY_CODE",
                    "SEM_TRADING_SYMBOL", "SEM_LOT_UNITS", "SEM_CUSTOM_SYMBOL", "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE",
                    "SEM_OPTION_TYPE", "SEM_TICK_SIZE", "SEM_EXPIRY_FLAG", "SEM_EXCH_INSTRUMENT_TYPE", "SEM_SERIES",
                    "SM_SYMBOL_NAME"])
        for i in self.instruments:
            exch, seg = _EXCH[i.segment]
            w.writerow([exch, seg, i.security_id, i.instrument_type, 0, i.trading_symbol, f"{float(i.lot):.1f}",
                        i.name, f"{i.expiry} 14:30:00" if i.expiry else "", f"{i.strike:.5f}" if i.strike else "-0.01000",
                        i.option_type or "XX", f"{float(i.tick_paise):.4f}", "W" if i.expiry else "NA",
                        "OP" if i.option_type else "ES", "EQ" if i.segment.endswith("EQ") else "NA",
                        i.name.upper()])
        return buf.getvalue()

