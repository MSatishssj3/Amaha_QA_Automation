"""Async DhanHQ v2 REST client.

* One ``aiohttp`` session with a small keep-alive pool; connections are opened at startup
  (``warm``) and kept hot (``keep_warm``) so an order never pays for DNS + TCP + TLS.
* Order calls take *pre-serialised bytes* (built from per-instrument templates by the broker).
* Calls never raise for HTTP/network failures - they return ``Result`` with a classified
  ``DhanError``. ``error.timeout`` means "outcome unknown": for order placement the OMS resolves it
  by correlationId and **never blindly retries**.
* Non-order calls pass through per-category async rate limiters.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
import orjson

from ..errors import Category, DhanError, from_response, network_error
from ..ratelimit import AsyncLimiter
from .endpoints import Endpoints

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Result:
    ok: bool
    status: int = 0
    data: Any = None
    error: DhanError | None = None
    rtt_ns: int = 0

    def __repr__(self) -> str:
        return f"Result(ok={self.ok}, status={self.status}, error={self.error}, rtt_ms={self.rtt_ns / 1e6:.2f})"


class DhanRest:
    def __init__(self, endpoints: Endpoints, client_id: str, token: str = "", *, order_timeout_s: float = 1.5,
                 timeout_s: float = 5.0, limiters: dict[str, AsyncLimiter] | None = None, pool_size: int = 8) -> None:
        self.ep = endpoints
        self.client_id = client_id
        self.order_timeout = aiohttp.ClientTimeout(total=order_timeout_s, connect=min(0.5, order_timeout_s))
        self.timeout = aiohttp.ClientTimeout(total=timeout_s, connect=min(2.0, timeout_s))
        self.limiters = limiters or {}
        self.pool_size = pool_size
        self._session: aiohttp.ClientSession | None = None
        self._headers: dict[str, str] = {}
        self._profile_headers: dict[str, str] = {}
        self.set_token(token)
        self.last_rtt_ns = 0
        self.calls = 0

    # ---- lifecycle -----------------------------------------------------------------------
    async def start(self) -> None:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=self.pool_size, ttl_dns_cache=600, keepalive_timeout=75,
                                             force_close=False)
            self._session = aiohttp.ClientSession(connector=connector, json_serialize=lambda o: orjson.dumps(o).decode())

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("DhanRest.start() not called")
        return self._session

    def set_token(self, token: str) -> None:
        self.token = token
        self._headers = {"access-token": token, "client-id": self.client_id,
                         "Content-Type": "application/json", "Accept": "application/json"}
        # /profile and /RenewToken use a `dhanClientId` header instead of `client-id` (per SDK)
        self._profile_headers = dict(self._headers, dhanClientId=self.client_id)

    async def warm(self, connections: int = 2) -> None:
        """Open N pooled connections now so the first order skips the TLS handshake."""
        await asyncio.gather(*(self.profile() for _ in range(connections)), return_exceptions=True)

    # ---- core ----------------------------------------------------------------------------
    def dumps(self, payload: dict[str, Any]) -> bytes:
        if "dhanClientId" not in payload:
            payload = {"dhanClientId": self.client_id, **payload}
        return orjson.dumps(payload)

    async def request(self, method: str, path: str, *, body: bytes | None = None, params: dict | None = None,
                      bucket: str | None = "non_trading", order: bool = False,
                      headers: dict[str, str] | None = None) -> Result:
        lim = self.limiters.get(bucket) if bucket else None
        if lim is not None:
            await lim.acquire()
        url = self.ep.rest_base + path
        timeout = self.order_timeout if order else self.timeout
        t0 = time.perf_counter_ns()
        self.calls += 1
        try:
            async with self.session.request(method, url, data=body, params=params, headers=headers or self._headers,
                                            timeout=timeout) as resp:
                raw = await resp.read()
                status = resp.status
        except asyncio.TimeoutError:
            return Result(False, 0, None, network_error(f"{method} {path} timed out", timeout=True),
                          time.perf_counter_ns() - t0)
        except aiohttp.ClientConnectorError as e:  # never reached the server: outcome is known (not placed)
            return Result(False, 0, None, network_error(f"connect failed: {e}", timeout=False),
                          time.perf_counter_ns() - t0)
        except (aiohttp.ClientError, OSError) as e:  # dropped mid-request: outcome unknown
            return Result(False, 0, None, network_error(f"{type(e).__name__}: {e}", timeout=True),
                          time.perf_counter_ns() - t0)
        rtt = time.perf_counter_ns() - t0
        self.last_rtt_ns = rtt
        data: Any = None
        if raw:
            try:
                data = orjson.loads(raw)
            except orjson.JSONDecodeError:
                data = raw.decode("utf-8", "replace")
        if 200 <= status < 300 and not (isinstance(data, dict) and data.get("errorCode")):
            return Result(True, status, data, None, rtt)
        err = from_response(status, data)
        if err.category is Category.RATE_LIMIT and lim is not None:
            lim.cooldown(1.0)
        return Result(False, status, data, err, rtt)

    def _json(self, method: str, path: str, payload: dict | None = None, **kw: Any):
        body = self.dumps(payload) if payload is not None else None
        return self.request(method, path, body=body, **kw)

    # ---- orders (hot path: caller supplies bytes; rate limiting done by the OMS) -----------
    async def place_order_raw(self, body: bytes, slicing: bool = False) -> Result:
        return await self.request("POST", "/orders/slicing" if slicing else "/orders", body=body, bucket=None, order=True)

    async def place_super_raw(self, body: bytes) -> Result:
        return await self.request("POST", "/super/orders", body=body, bucket=None, order=True)

    async def modify_order(self, order_id: str, payload: dict) -> Result:
        return await self._json("PUT", f"/orders/{order_id}", {"orderId": order_id, **payload}, bucket=None, order=True)

    async def modify_super(self, order_id: str, payload: dict) -> Result:
        return await self._json("PUT", f"/super/orders/{order_id}", {"orderId": order_id, **payload}, bucket=None,
                                order=True)

    async def cancel_order(self, order_id: str) -> Result:
        return await self.request("DELETE", f"/orders/{order_id}", bucket=None, order=True)

    async def cancel_super(self, order_id: str, leg: str) -> Result:
        return await self.request("DELETE", f"/super/orders/{order_id}/{leg}", bucket=None, order=True)

    async def get_orders(self) -> Result:
        return await self.request("GET", "/orders")

    async def get_order(self, order_id: str) -> Result:
        return await self.request("GET", f"/orders/{order_id}")

    async def get_order_by_cid(self, cid: str) -> Result:
        return await self.request("GET", f"/orders/external/{cid}")

    async def get_super_orders(self) -> Result:
        return await self.request("GET", "/super/orders")

    async def get_trades(self) -> Result:
        """Today's trade book."""
        return await self.request("GET", "/trades/")

    async def get_trade_history(self, from_date: str, to_date: str, page: int = 0) -> Result:
        """Executed trades between two dates (``YYYY-MM-DD``), with per-trade charges. Pages start at 0."""
        return await self.request("GET", f"/trades/{from_date}/{to_date}/{page}")

    async def get_ledger(self, from_date: str, to_date: str) -> Result:
        """Account ledger (funds added / withdrawn, settlements, charges) between two dates (``YYYY-MM-DD``)."""
        return await self.request("GET", "/ledger", params={"from-date": from_date, "to-date": to_date})

    # ---- portfolio / funds ---------------------------------------------------------------
    async def get_positions(self) -> Result:
        return await self.request("GET", "/positions")

    async def exit_all_positions(self) -> Result:
        return await self.request("DELETE", "/positions", bucket=None, order=True)

    async def get_holdings(self) -> Result:
        return await self.request("GET", "/holdings")

    async def get_funds(self) -> Result:
        return await self.request("GET", "/fundlimit")

    async def margin(self, payload: dict) -> Result:
        return await self._json("POST", "/margincalculator", payload)

    # ---- market data ---------------------------------------------------------------------
    async def quote(self, securities: dict[str, list[int]], kind: str = "quote") -> Result:
        """kind: ltp | ohlc | quote. Body e.g. {"NSE_EQ": [1333]} (Quote APIs: 1 request/second)."""
        return await self.request("POST", f"/marketfeed/{kind}", body=orjson.dumps(securities), bucket="quote")

    async def intraday(self, security_id: int, segment: str, instrument: str, from_dt: str, to_dt: str,
                       interval: int = 1, oi: bool = False) -> Result:
        payload = {"securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
                   "interval": interval, "oi": oi, "fromDate": from_dt, "toDate": to_dt}
        return await self._json("POST", "/charts/intraday", payload, bucket="data")

    async def historical_daily(self, security_id: int, segment: str, instrument: str, from_date: str, to_date: str,
                               expiry_code: int = 0, oi: bool = False) -> Result:
        """Daily candles (``YYYY-MM-DD`` dates). Response: arrays open/high/low/close/volume/timestamp."""
        payload = {"securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
                   "expiryCode": expiry_code, "oi": oi, "fromDate": from_date, "toDate": to_date}
        return await self._json("POST", "/charts/historical", payload, bucket="data")

    # ---- trader controls -----------------------------------------------------------------
    async def killswitch(self, activate: bool) -> Result:
        status = "ACTIVATE" if activate else "DEACTIVATE"
        return await self.request("POST", f"/killswitch?killSwitchStatus={status}", body=self.dumps({}))

    async def killswitch_status(self) -> Result:
        return await self.request("GET", "/killswitch")

    async def set_pnl_exit(self, profit_value: float, loss_value: float, product_types: list[str],
                           enable_kill_switch: bool = True) -> Result:
        return await self._json("POST", "/pnlExit", {"profitValue": float(profit_value), "lossValue": float(loss_value),
                                                     "productType": product_types,
                                                     "enableKillSwitch": enable_kill_switch})

    async def get_pnl_exit(self) -> Result:
        return await self.request("GET", "/pnlExit")

    async def stop_pnl_exit(self) -> Result:
        return await self.request("DELETE", "/pnlExit")

    # ---- account -------------------------------------------------------------------------
    async def profile(self) -> Result:
        return await self.request("GET", "/profile", headers=self._profile_headers)

    async def renew_token(self) -> Result:
        return await self.request("GET", "/RenewToken", headers=self._profile_headers)

    async def get_ip(self) -> Result:
        return await self.request("GET", "/ip/getIP")

    async def set_ip(self, ip: str, flag: str = "PRIMARY") -> Result:
        return await self._json("POST", "/ip/setIP", {"ip": ip, "ipFlag": flag})

    async def modify_ip(self, ip: str, flag: str = "PRIMARY") -> Result:
        return await self._json("PUT", "/ip/modifyIP", {"ip": ip, "ipFlag": flag})
