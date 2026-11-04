"""REST client, token manager, market feed and order-update socket against the local mock Dhan."""

from __future__ import annotations

import asyncio

import orjson
import pyotp
import pytest

from dhanbot.config import load_config
from dhanbot.dhan import packets as P
from dhanbot.dhan.auth import AuthError, TokenManager, jwt_expiry
from dhanbot.dhan.feed import FeedConnection
from dhanbot.dhan.normalize import available_balance, normalise_order
from dhanbot.dhan.order_ws import OrderUpdateConnection
from dhanbot.dhan.rest import DhanRest
from dhanbot.errors import Category
from dhanbot.models import OrderStatus
from dhanbot.ratelimit import AsyncLimiter
from dhanbot.sim.mock_dhan import MockDhan, make_fake_jwt

from .helpers import wait_for

CID = "1000000001"


@pytest.fixture
async def mock():
    m = MockDhan(client_id=CID, auto_ticks=False)
    await m.start()
    yield m
    await m.stop()


@pytest.fixture
async def rest(mock):
    r = DhanRest(mock.endpoints(), CID, mock.token, order_timeout_s=0.5,
                 limiters={"non_trading": AsyncLimiter(18), "data": AsyncLimiter(4), "quote": AsyncLimiter(1)})
    await r.start()
    yield r
    await r.close()


def order_body(rest: DhanRest, **kw) -> bytes:
    payload = {"transactionType": "BUY", "exchangeSegment": "NSE_EQ", "productType": "INTRADAY", "orderType": "LIMIT",
               "validity": "DAY", "securityId": "2885", "quantity": 1, "disclosedQuantity": 0, "price": 0.0,
               "triggerPrice": 0.0, "afterMarketOrder": False, "correlationId": "T1"}
    payload.update(kw)
    return rest.dumps(payload)


async def test_rest_headers_body_and_place(mock, rest):
    inst = mock.instrument("RELIANCE")
    res = await rest.place_order_raw(order_body(rest, price=round(inst.ask + 1, 1), correlationId="T1"))
    assert res.ok, res
    assert res.data["orderId"] and res.data["orderStatus"] == "TRANSIT"
    method, path, body = mock.requests[-1]
    assert (method, path) == ("POST", "/v2/orders") and body["dhanClientId"] == CID
    await wait_for(lambda: mock.orders[res.data["orderId"]][0].status == "TRADED")
    by_cid = await rest.get_order_by_cid("T1")
    upd = normalise_order(by_cid.data)
    assert upd.status is OrderStatus.FILLED and upd.filled_qty == 1 and upd.cid == "T1"
    pos = await rest.get_positions()
    assert pos.ok and pos.data[0]["netQty"] == 1
    funds = await rest.get_funds()
    assert available_balance(funds.data) is not None


async def test_rest_errors_are_classified(mock, rest):
    mock.faults.reject_next = "Insufficient margin for order"
    r = await rest.place_order_raw(order_body(rest, price=1460.0))
    assert not r.ok and r.error.category is Category.MARGIN and r.error.code == "DH-906"
    mock.faults.rate_limit_next = 1
    r = await rest.place_order_raw(order_body(rest, price=1460.0))
    assert r.error.category is Category.RATE_LIMIT and r.status == 429
    mock.faults.ip_rejected = True
    r = await rest.place_order_raw(order_body(rest, price=1460.0))
    assert r.error.category is Category.IP_NOT_ALLOWED
    mock.faults.ip_rejected = False
    r = await rest.place_order_raw(order_body(rest, price=1460.03))
    assert r.error.category is Category.INPUT
    rest.set_token("bad")
    r = await rest.get_positions()
    assert r.error.category is Category.AUTH


async def test_rest_timeout_marks_outcome_unknown(mock, rest):
    mock.faults.place_timeout_once = True
    mock.faults.timeout_delay_s = 1.0
    r = await rest.place_order_raw(order_body(rest, price=1460.0, correlationId="TO1"))
    assert not r.ok and r.error.category is Category.NETWORK and r.error.timeout
    # ... but the order exists at the broker and can be found by correlationId
    found = await rest.get_order_by_cid("TO1")
    assert found.ok and normalise_order(found.data).cid == "TO1"


async def test_connect_refused_is_known_not_placed():
    from dhanbot.dhan.endpoints import Endpoints

    r = DhanRest(Endpoints(rest_base="http://127.0.0.1:1/v2"), CID, "t", order_timeout_s=0.5)
    await r.start()
    try:
        res = await r.place_order_raw(b"{}")
        assert not res.ok and res.error.category is Category.NETWORK and res.error.timeout is False
    finally:
        await r.close()


async def test_misc_endpoints(mock, rest):
    assert (await rest.profile()).data["dataPlan"] == "Active"
    assert (await rest.set_ip("1.2.3.4")).ok
    assert (await rest.get_ip()).data["primaryIP"] == "1.2.3.4"
    assert (await rest.set_pnl_exit(2500, 2500, ["INTRADAY"], True)).ok
    assert mock.pnl_exit["loss"] == 2500
    assert (await rest.killswitch(True)).ok and mock.killswitch
    q = await rest.quote({"NSE_EQ": [2885]})
    assert q.data["data"]["NSE_EQ"]["2885"]["upper_circuit_limit"] > 0
    h = await rest.intraday(2885, "NSE_EQ", "EQUITY", "2026-01-01 09:15:00", "2026-01-01 15:30:00", 1)
    assert len(h.data["close"]) == 60
    renewed = await rest.renew_token()
    assert renewed.ok and renewed.data["accessToken"] == mock.token


# ---- auth ---------------------------------------------------------------------------------


def test_jwt_expiry():
    assert jwt_expiry(make_fake_jwt("1", 1900000000)) == 1900000000.0
    assert jwt_expiry("garbage") is None


async def test_token_manager_sources_and_totp(mock, tmp_path):
    cfg = load_config(data={"dhan": {"auth": "totp"}, "paths": {"data_dir": str(tmp_path)}},
                      env={"DHAN_CLIENT_ID": CID, "DHAN_PIN": mock.pin, "DHAN_TOTP_SECRET": mock.totp_secret})
    tm = TokenManager(cfg, mock.endpoints(), cache_path=tmp_path / "token.json")
    seen = []
    tm.on_change(seen.append)
    tok = await tm.ensure()
    assert tok == mock.token and tm.source == "totp" and seen == [tok]
    assert (tmp_path / "token.json").exists() and tm.seconds_left() > 80000
    # second manager picks up the cached token without logging in again
    tm2 = TokenManager(cfg, mock.endpoints(), cache_path=tmp_path / "token.json")
    assert await tm2.ensure() == tok and tm2.source == "cache"
    # needs validity beyond expiry -> renew
    assert await tm2.ensure(valid_until=tm2.expires_at + 10) == mock.token


async def test_token_manager_without_credentials_raises(mock, tmp_path):
    cfg = load_config(data={"paths": {"data_dir": str(tmp_path)}}, env={"DHAN_CLIENT_ID": CID})
    tm = TokenManager(cfg, mock.endpoints(), cache_path=tmp_path / "t.json")
    with pytest.raises(AuthError):
        await tm.ensure()


async def test_totp_wrong_pin_rejected(mock, tmp_path):
    cfg = load_config(data={"dhan": {"auth": "totp"}}, env={"DHAN_CLIENT_ID": CID, "DHAN_PIN": "0000",
                                                            "DHAN_TOTP_SECRET": pyotp.random_base32()})
    tm = TokenManager(cfg, mock.endpoints(), cache_path=tmp_path / "t.json")
    with pytest.raises(AuthError):
        await tm.login_totp()


# ---- market feed ----------------------------------------------------------------------------


def test_subscription_batching():
    subs = {("NSE_EQ", i): 17 for i in range(250)}
    subs[("IDX_I", 13)] = 15
    msgs = [orjson.loads(m) for m in FeedConnection.build_messages(subs)]
    assert [m["RequestCode"] for m in msgs] == [15, 17, 17, 17]
    assert [m["InstrumentCount"] for m in msgs] == [1, 100, 100, 50]
    assert all(m["InstrumentCount"] == len(m["InstrumentList"]) for m in msgs)
    un = [orjson.loads(m) for m in FeedConnection.build_messages({("NSE_EQ", 1): 21}, unsubscribe=True)]
    assert un[0]["RequestCode"] == 22


async def test_feed_receives_ticks_and_reconnects(mock):
    frames: list[bytes] = []
    events: list[str] = []
    ep = mock.endpoints()
    feed = FeedConnection("t", lambda: ep.feed_ws_url(CID, mock.token), frames.append,
                          on_event=lambda k, i: events.append(k), stale_after_s=0.5)
    feed.subscribe([("NSE_EQ", 2885)], 21)
    feed.subscribe([("NSE_EQ", 2885)], 15)  # lower mode must not downgrade
    assert feed.desired[("NSE_EQ", 2885)] == 21
    task = asyncio.create_task(feed.run())
    assert await wait_for(lambda: feed.connected and len(frames) >= 1)
    first = P.parse_frame(frames[0])
    assert any(isinstance(p, P.PrevClose) for p in first) and any(getattr(p, "code", 0) == P.CODE_FULL for p in first)
    mock.step()
    assert await wait_for(lambda: len(frames) >= 2)
    await mock.drop_feeds()
    assert await wait_for(lambda: "disconnected" in events)
    assert await wait_for(lambda: feed.connected and feed.reconnects >= 1, timeout=5)
    n = len(frames)
    mock.step()
    assert await wait_for(lambda: len(frames) > n), "resubscribed after reconnect"
    await feed.stop()
    await asyncio.wait_for(task, 3)


async def test_feed_token_expiry_and_fatal(mock):
    ep = mock.endpoints()
    events: list[tuple[str, dict]] = []
    tokens = {"t": mock.token}
    feed = FeedConnection("t", lambda: ep.feed_ws_url(CID, tokens["t"]), lambda b: None,
                          on_event=lambda k, i: events.append((k, i)))
    feed.subscribe([("NSE_EQ", 2885)], 15)
    task = asyncio.create_task(feed.run())
    assert await wait_for(lambda: feed.connected)
    mock.expire_token()
    assert await wait_for(lambda: any(k == "token_expired" for k, _ in events))
    tokens["t"] = mock.token  # engine refreshed the token -> reconnect succeeds
    assert await wait_for(lambda: feed.connected, timeout=5)
    await feed.stop()
    await asyncio.wait_for(task, 3)

    bad = FeedConnection("bad", lambda: ep.feed_ws_url("999", mock.token), lambda b: None,
                         on_event=lambda k, i: events.append((k, i)))
    t2 = asyncio.create_task(bad.run())
    await asyncio.wait_for(t2, 5)  # fatal 808 -> run() returns, no retry loop
    assert bad.fatal_reason == 808


async def test_feed_watchdog_recycles_silent_socket(mock):
    ep = mock.endpoints()
    feed = FeedConnection("w", lambda: ep.feed_ws_url(CID, mock.token), lambda b: None, stale_after_s=0.3)
    feed.subscribe([("NSE_EQ", 2885)], 15)
    task = asyncio.create_task(feed.run())
    assert await wait_for(lambda: feed.connected)
    mock.faults.freeze_feed = True
    assert await wait_for(lambda: feed.reconnects >= 1, timeout=5)
    await feed.stop()
    await asyncio.wait_for(task, 3)


# ---- order updates ----------------------------------------------------------------------------


async def test_order_update_socket(mock, rest):
    ep = mock.endpoints()
    updates = []
    ou = OrderUpdateConnection(ep.order_ws_url, CID, lambda: mock.token, updates.append)
    task = asyncio.create_task(ou.run())
    assert await wait_for(lambda: ou.connected and mock._order_clients)
    inst = mock.instrument("RELIANCE")
    r = await rest.place_order_raw(order_body(rest, price=round(inst.ask + 1, 1), correlationId="OU1"))
    assert r.ok
    assert await wait_for(lambda: any(u.status is OrderStatus.FILLED for u in updates))
    filled = [u for u in updates if u.status is OrderStatus.FILLED][0]
    assert filled.cid == "OU1" and filled.filled_qty == 1 and filled.avg_price > 0 and filled.side.value == "BUY"
    assert filled.broker_id == r.data["orderId"]
    await ou.stop()
    await asyncio.wait_for(task, 3)
