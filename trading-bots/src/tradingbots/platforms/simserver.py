"""The simulated market: dhanbot's mock Dhan server (real Dhan wire protocols, random-walk prices,
matching engine) run as a member of the team so everything works without a Dhan account.

It publishes its endpoints and access token under ``sim:server`` on the bus; the config bot hands them
out to the other bots exactly as it would a real Dhan token.
"""

from __future__ import annotations

import dataclasses
import os

from dhanbot.dhan.rest import DhanRest
from dhanbot.sim.mock_dhan import MockDhan

from ..core import topics
from ..core.bot import Bot


class SimMarketBot(Bot):
    name = "sim_market"

    def configure(self) -> None:
        self.mock: MockDhan | None = None
        self.every(5.0, self._publish, name="advertise")

    async def setup(self) -> None:
        self.mock = MockDhan(clock=self.clock, seed=7)
        self.mock.ips["primaryIP"] = "127.0.0.1"
        self.mock.add_holding("INFY", 10, 1480.0)
        self.mock.add_holding("HDFCBANK", 5, 1890.0)
        await self.mock.start(port=int((self.ctx.sim or {}).get("port", 0)))
        await self._publish()
        if os.environ.get("TRADINGBOTS_DEMO_MANUAL"):  # `bots demo`: you also trade by hand, in the "Dhan app"
            self.spawn(self._manual_trade(), name="demo-manual")

    async def _manual_trade(self) -> None:
        """One BUY placed directly with the exchange, not by the trading engine, so the budget bot counts it
        as your own trade."""
        await self.clock.sleep(20 * 60)
        assert self.mock is not None
        rest = DhanRest(self.mock.endpoints(), self.mock.client_id, self.mock.token)
        await rest.start()
        try:
            body = rest.dumps({"transactionType": "BUY", "exchangeSegment": "NSE_EQ", "productType": "CNC",
                               "orderType": "MARKET", "validity": "DAY", "securityId": "1333", "quantity": 4,
                               "price": 0, "correlationId": "MANUAL-DEMO"})
            res = await rest.place_order_raw(body)
            self.log.info("demo manual trade placed: %s", "ok" if res.ok else res.error)
        finally:
            await rest.close()

    async def _publish(self) -> None:
        if self.mock is None:
            return
        info = {"endpoints": dataclasses.asdict(self.mock.endpoints()), "client_id": self.mock.client_id,
                "token": self.mock.token, "port": self.mock.port}
        await self.bus.kv_set(topics.KV_SIM, info, ttl=30)

    async def teardown(self) -> None:
        if self.mock is not None:
            await self.mock.stop()
            await self.bus.kv_delete(topics.KV_SIM)

    def details(self) -> dict:
        m = self.mock
        return {"port": m.port if m else None, "orders": len(m.orders) if m else 0, "trades": len(m.trades) if m else 0}
