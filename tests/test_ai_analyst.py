"""AI analyst: request shape, fallbacks, refusals, cost accounting, budget cap, batches, disabled and stub
modes. Claude is replaced by a fake client that returns real SDK response objects, so no API call is
made and no credit is spent."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from anthropic.types.beta import BetaMessage
from anthropic.types.messages import MessageBatchIndividualResponse

from tradingbots.bots.ai_analyst.bot import AIAnalystBot
from tradingbots.bots.ai_analyst.claude import FALLBACK_BETA, Claude, ClaudeError, build_request, usage_cost
from tradingbots.bots.ai_analyst.prompt import SYSTEM_PROMPT
from tradingbots.bots.ai_analyst.schema import DeepDive, KeyLevels, PortfolioView, Report, Scenario, StockView
from tradingbots.core import topics
from tradingbots.core.settings import AICfg
from tradingbots.core.tables_io import TableReader, TableStore

from .helpers import make_settings, read_all, wait_for
from .team import Team

SYMS = ["RELIANCE", "HDFCBANK", "INFY"]


# ---- fakes ------------------------------------------------------------------------------------------------------
class _Stream:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        if isinstance(self.resp, BaseException):
            raise self.resp  # the SDK sends the request when the stream opens
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return self.resp


class _AsyncIter:
    def __init__(self, items):
        self.items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.items:
            raise StopAsyncIteration
        return self.items.pop(0)


class FakeAnthropic:
    """The surface the analyst uses: beta.messages.stream / count_tokens and messages.batches.*."""

    def __init__(self, responses=None, input_tokens: int = 9000):
        self.responses = list(responses or [])
        self.input_tokens = input_tokens
        self.calls: list[tuple[str, object]] = []
        self.batches: dict[str, list] = {}
        self.batch_results: dict[str, list] = {}
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream, count_tokens=self._count))
        self.messages = SimpleNamespace(batches=SimpleNamespace(create=self._bcreate, retrieve=self._bretrieve,
                                                                results=self._bresults))

    def _stream(self, **kw):
        self.calls.append(("stream", kw))
        return _Stream(self.responses.pop(0))

    async def _count(self, **kw):
        self.calls.append(("count_tokens", kw))
        return SimpleNamespace(input_tokens=self.input_tokens)

    async def _bcreate(self, requests):
        bid = f"msgbatch_{len(self.batches) + 1}"
        self.batches[bid] = list(requests)
        self.calls.append(("batch_create", requests))
        return SimpleNamespace(id=bid, processing_status="in_progress")

    async def _bretrieve(self, bid):
        n = len(self.batches[bid])
        return SimpleNamespace(processing_status="ended" if bid in self.batch_results else "in_progress",
                               request_counts=SimpleNamespace(processing=0, succeeded=n, errored=0, canceled=0, expired=0))

    async def _bresults(self, bid):
        return _AsyncIter(self.batch_results[bid])


def usage(inp=9000, out=4000, cr=2000, cw=0, iterations=None) -> dict:
    u = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}
    if iterations is not None:
        u["iterations"] = iterations
    return u


def beta_msg(text: str | None = None, *, stop="end_turn", model="claude-opus-5-5", u: dict | None = None,
             content: list | None = None, stop_details: dict | None = None) -> BetaMessage:
    content = content if content is not None else ([{"type": "text", "text": text}] if text is not None else [])
    return BetaMessage.model_validate({"id": "msg_test", "type": "message", "role": "assistant", "model": model,
                                       "content": content, "stop_reason": stop, "stop_sequence": None,
                                       "stop_details": stop_details, "usage": u or usage()})


def sample_report(symbols=SYMS) -> Report:
    return Report(
        headline="Large caps steady; RELIANCE leads", market_context="NIFTY flat, trend up.",
        stocks=[StockView(symbol=s, trend="up", outlook=f"{s} holds above its 50-day average.",
                          key_levels=KeyLevels(support=[1400.0], resistance=[1500.0]), observations=["RSI 58"],
                          risks=["wide daily range"], confidence="medium", watch_action="watch", data_gaps=[])
                for s in symbols],
        portfolio=PortfolioView(summary="Two holdings.", concentration="INFY 60%", charges_drag="0.1% of turnover",
                                budget_use="40% of today's budget used", observations=[]),
        caveats=["Based on daily data only."], narrative_markdown="**Steady.**\n\n- RELIANCE above SMA50")


def sample_dive(sym: str) -> DeepDive:
    sc = [Scenario(name=n, description=f"{n} case", trigger="close above 1500", invalidation="close below 1400")
          for n in ("bull", "base", "bear")]
    return DeepDive(symbol=sym, summary="Uptrend intact.", trend="up", trend_analysis="Above SMA50 and SMA200.",
                    momentum="MACD above signal.", volatility_and_risk="ATR 1.8%.",
                    key_levels=KeyLevels(support=[1400.0], resistance=[1500.0]), scenarios=sc, watch_action="watch",
                    confidence="medium", caveats=["No fundamentals."])


def http_error(cls, status: int):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(f"error {status}", response=httpx2.Response(status, request=req), body=None)


# ---- the Claude wrapper ---------------------------------------------------------------------------------------------
async def test_request_shape_fallbacks_and_cost():
    cfg = AICfg()
    fake = FakeAnthropic([beta_msg(sample_report().model_dump_json())])
    claude = Claude(fake, cfg)
    req = build_request(cfg, "Report kind: midday\n\n<packet>{}</packet>", Report)
    res = await claude.run(req, Report)
    kind, kw = fake.calls[0]
    assert kind == "stream" and kw["model"] == "claude-opus-5-5" and kw["max_tokens"] == 24000
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"  # refusal fallbacks on by default
    assert kw["thinking"] == {"type": "adaptive"} and kw["output_config"]["effort"] == "high"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}  # frozen prompt cached, data after it
    assert kw["system"][0]["text"] == SYSTEM_PROMPT and "<packet>{}</packet>" in kw["messages"][0]["content"]
    assert isinstance(res.parsed, Report) and res.parsed.stocks[1].symbol == "HDFCBANK"
    # 9000 in x $4 + 4000 out x $20 + 2000 cache read x $0.20 per MTok
    assert res.usd == pytest.approx((9000 * 4 + 4000 * 20 + 2000 * 0.2) / 1e6)
    assert res.usage == {"input_tokens": 9000, "output_tokens": 4000, "cache_read_tokens": 2000, "cache_write_tokens": 0}
    assert res.served_by == "claude-opus-5-5" and not res.fallback_used


async def test_no_fallbacks_where_unsupported_or_switched_off():
    for cfg in (AICfg(model="claude-haiku-5-5"), AICfg(refusal_fallbacks=False)):
        fake = FakeAnthropic([beta_msg(sample_report().model_dump_json(), model=cfg.model)])
        await Claude(fake, cfg).run(build_request(cfg, "x", Report), Report)
        kw = fake.calls[0][1]
        assert "fallbacks" not in kw and "betas" not in kw


async def test_fallback_answer_is_read_after_the_boundary_and_each_attempt_priced():
    cfg = AICfg()
    good = sample_report().model_dump_json()
    content = [{"type": "text", "text": '{"headline": "partial from the declined mod'},
               {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-4-8"},
                "trigger": {"type": "refusal", "category": "cyber"}},
               {"type": "text", "text": good}]
    its = [{"type": "message", "model": "claude-opus-5-5", "input_tokens": 9000, "output_tokens": 300,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
           {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 9000, "output_tokens": 4000,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}]
    fake = FakeAnthropic([beta_msg(content=content, model="claude-opus-4-8", u=usage(9000, 4000, 0, 0, its))])
    res = await Claude(fake, cfg).run(build_request(cfg, "x", Report), Report)
    assert res.fallback_used and res.served_by == "claude-opus-4-8" and res.parsed.headline.startswith("Large caps")
    assert res.usd == pytest.approx((9000 * 4 + 300 * 20 + 9000 * 5 + 4000 * 25) / 1e6)  # opus-4-8 at $5/$25


async def test_refusal_and_truncation_are_reported_not_parsed():
    cfg = AICfg()
    its = [{"type": "message", "model": "claude-opus-5-5", "input_tokens": 9000, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
           {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 9000, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}]
    refused = beta_msg(content=[], stop="refusal", stop_details={"type": "refusal", "category": "cyber"},
                       u=usage(9000, 0, 0, 0, its))
    with pytest.raises(ClaudeError) as ei:
        await Claude(FakeAnthropic([refused]), cfg).run(build_request(cfg, "x", Report), Report)
    assert ei.value.kind == "refusal" and "cyber" in str(ei.value) and "fallback model declined too" in str(ei.value)
    assert ei.value.usd == pytest.approx(9000 * 4 / 1e6 + 9000 * 5 / 1e6)
    cut = beta_msg('{"headline": "cut off', stop="max_tokens")
    with pytest.raises(ClaudeError) as ei:
        await Claude(FakeAnthropic([cut]), cfg).run(build_request(cfg, "x", Report), Report)
    assert ei.value.kind == "truncated" and ei.value.usd > 0
    bad = beta_msg('{"headline": 5}')
    with pytest.raises(ClaudeError) as ei:
        await Claude(FakeAnthropic([bad]), cfg).run(build_request(cfg, "x", Report), Report)
    assert ei.value.kind == "invalid_output"


async def test_api_errors_are_classified():
    cfg = AICfg()
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    cases = [(http_error(anthropic.AuthenticationError, 401), "auth", False),
             (http_error(anthropic.BadRequestError, 400), "bad_request", False),
             (http_error(anthropic.RateLimitError, 429), "rate_limit", True),
             (http_error(anthropic.InternalServerError, 500), "server", True),
             (anthropic.APIConnectionError(request=req), "network", True)]
    for exc, kind, retry in cases:
        with pytest.raises(ClaudeError) as ei:
            await Claude(FakeAnthropic([exc]), cfg).run(build_request(cfg, "x", Report), Report)
        assert (ei.value.kind, ei.value.retryable) == (kind, retry), exc


def test_usage_cost_uses_settings_prices_for_the_configured_model():
    cfg = AICfg(prices={"input": 1.0, "output": 2.0, "cache_read": 0.1, "cache_write": 1.25})
    u = SimpleNamespace(input_tokens=1_000_000, output_tokens=500_000, cache_read_input_tokens=1_000_000,
                        cache_creation_input_tokens=0, iterations=None)
    usd, tot = usage_cost(u, "claude-opus-5-5", cfg)
    assert usd == pytest.approx(1.0 + 1.0 + 0.1) and tot["cache_read_tokens"] == 1_000_000
    assert usage_cost(u, "claude-opus-5-5", cfg, discount=0.5)[0] == pytest.approx(1.05)


# ---- the bot ------------------------------------------------------------------------------------------------------
def seed_tables(tmp) -> None:
    st = TableStore(tmp / "data" / "tables")
    rows = []
    for i, s in enumerate(SYMS + ["NIFTY"]):
        rows.append({"symbol": s, "close": 1400.0 + i * 100, "ret_1d": 0.4, "ret_1m": 2.5 + i, "ret_3m": 6.0,
                     "sma50": 1380.0 + i * 100, "sma200": 1300.0 + i * 100, "dist_sma50": 1.4, "dist_sma200": 7.7,
                     "rsi14": 58.0 + i, "adx14": 27.0, "atr14_pct": 1.8, "vol_ann": 22.0, "max_dd_1y": -14.0,
                     "support": 1390.0 + i * 100, "resistance": 1460.0 + i * 100, "trend": "up",
                     "trend_score": 55.0, "days": 300})
    st.replace("history_metrics", rows)
    st.replace("quotes_latest", [{"symbol": s, "segment": "NSE_EQ", "ltp": 1405.0 + i * 100, "change_pct": 0.35,
                                  "updated_ts": 0.0} for i, s in enumerate(SYMS)])
    # holdings with integer quantities: DuckDB returns SUM() over them as Decimal (a real demo crash, now covered)
    st.replace("money_allocation", [{"platform": "simulated", "symbol": "INFY", "kind": "holding", "qty": 10,
                                     "invested": 14800.0, "current_value": 15160.0, "pnl": 360.0, "weight_pct": 60.0},
                                    {"platform": "simulated", "symbol": "TCS", "kind": "holding", "qty": 3,
                                     "invested": 9900.0, "current_value": 10110.0, "pnl": 210.0, "weight_pct": 40.0}])


def ai_settings(tmp, **ai):
    return make_settings(tmp, ai=ai, schedule={"ai_runs": []})


@pytest.fixture
def fake_client(monkeypatch):
    holder = {}

    def use(fake):
        holder["fake"] = fake
        monkeypatch.setattr(AIAnalystBot, "client_factory", staticmethod(lambda: fake))
        return fake

    return use


async def test_bot_runs_an_analysis_stores_it_and_logs_the_cost(tmp_path, fake_client):
    seed_tables(tmp_path)
    fake = fake_client(FakeAnthropic([beta_msg(sample_report(["RELIANCE", "HDFCBANK"]).model_dump_json())]))
    async with Team(tmp_path, ["ai_analyst"], settings=ai_settings(tmp_path)) as team:
        c = team.client
        st = await c.request(topics.cmd("ai_analyst", "status"), {})
        assert st["mode"] == "anthropic" and "refusal fallbacks on" in st["reason"]
        r = await c.request(topics.cmd("ai_analyst", "run"), {"kind": "post_close", "wait": True}, timeout=30)
        assert r["ran"], r
        expected = (9000 * 4 + 4000 * 20 + 2000 * 0.2) / 1e6
        assert r["cost_usd"] == pytest.approx(expected, abs=1e-4) and r["served_by"] == "claude-opus-5-5"
        assert [k for k, _ in fake.calls] == ["count_tokens", "stream"]
        packet_text = fake.calls[1][1]["messages"][0]["content"]
        assert "post_close" in packet_text and '"rsi14":58.0' in packet_text and '"benchmark":"NIFTY"' in packet_text
        full = await c.request(topics.cmd("ai_analyst", "report"), {"id": r["report_id"]})
        rep = full["report"]
        assert rep["status"] == "ok" and rep["report"]["headline"].startswith("Large caps")
        assert "The analysis left out: INFY, TCS" in rep["report"]["caveats"]  # the fake skipped these (TCS is held)
        assert "| RELIANCE | up | watch | medium |" in rep["markdown"]
        md_files = list((tmp_path / "data" / "ai_analyst" / "reports").rglob("*-post_close.md"))
        assert len(md_files) == 1 and "not financial advice" in md_files[0].read_text()
        costs = TableReader(tmp_path / "data" / "tables").table("ai_costs").to_dicts()
        assert len(costs) == 1 and costs[0]["usd"] == pytest.approx(expected, abs=1e-6)
        assert costs[0]["inr"] == pytest.approx(expected * 85.0, abs=1e-3) and costs[0]["purpose"] == "report:post_close"
        assert await wait_for(lambda: read_all(c, topics.AI_REPORT), 5)
        ev = [m.data for m in await read_all(c, topics.AI_REPORT)]
        assert ev[-1]["report_id"] == r["report_id"] and ev[-1]["status"] == "ok"
        assert [m.data["purpose"] for m in await read_all(c, topics.AI_COST)] == ["report:post_close"]
        st = await c.request(topics.cmd("ai_analyst", "status"), {})
        assert st["spent_today_usd"] == pytest.approx(expected, abs=1e-4)


async def test_daily_ai_budget_skips_runs_before_sending(tmp_path, fake_client):
    seed_tables(tmp_path)
    fake = fake_client(FakeAnthropic([]))
    async with Team(tmp_path, ["ai_analyst"], settings=ai_settings(tmp_path, daily_usd_budget=0.05)) as team:
        r = await team.client.request(topics.cmd("ai_analyst", "run"), {"wait": True}, timeout=30)
        assert not r["ran"] and "AI budget is $0.05" in r["reason"]
        assert [k for k, _ in fake.calls] == ["count_tokens"]  # counted, never sent
        alerts = [m.data for m in await read_all(team.client, topics.ALERT)]
        assert any(a["key"] == "ai-budget" for a in alerts)


async def test_refused_request_is_recorded_with_its_cost(tmp_path, fake_client):
    seed_tables(tmp_path)
    refused = beta_msg(content=[], stop="refusal", stop_details={"type": "refusal", "category": None},
                       u=usage(9000, 0, 0, 0))
    fake_client(FakeAnthropic([refused]))
    async with Team(tmp_path, ["ai_analyst"], settings=ai_settings(tmp_path)) as team:
        c = team.client
        r = await c.request(topics.cmd("ai_analyst", "run"), {"wait": True}, timeout=30)
        assert not r["ran"] and r["error_kind"] == "refusal" and "unspecified" in r["error"]
        reps = (await c.request(topics.cmd("ai_analyst", "reports"), {}))["reports"]
        assert reps[0]["status"] == "refusal" and reps[0]["usd"] == pytest.approx(0.036)
        costs = TableReader(tmp_path / "data" / "tables").table("ai_costs").to_dicts()
        assert len(costs) == 1 and costs[0]["usd"] == pytest.approx(0.036)


async def test_nightly_deep_dives_via_batches_rerun_refusals_with_fallbacks(tmp_path, fake_client):
    seed_tables(tmp_path)
    fake = fake_client(FakeAnthropic([beta_msg(sample_dive("HDFCBANK").model_dump_json(), model="claude-opus-4-8")]))
    async with Team(tmp_path, ["ai_analyst"], settings=ai_settings(tmp_path, nightly_batch=True)) as team:
        c = team.client
        r = await c.request(topics.cmd("ai_analyst", "deep_dive"), {"symbols": ["RELIANCE", "HDFCBANK"]}, timeout=30)
        assert r["submitted"] and r["requests"] == 2, r
        reqs = fake.batches[r["batch_id"]]
        assert [q["custom_id"] for q in reqs] == ["dd-RELIANCE", "dd-HDFCBANK"]
        p = reqs[0]["params"]
        assert "fallbacks" not in p and p["output_config"]["format"]["type"] == "json_schema"  # batches reject fallbacks
        assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
        ok = {"id": "m1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": "end_turn",
              "stop_sequence": None, "content": [{"type": "text", "text": sample_dive("RELIANCE").model_dump_json()}],
              "usage": {"input_tokens": 3000, "output_tokens": 2000}}
        refused = {**ok, "id": "m2", "stop_reason": "refusal", "content": [],
                   "usage": {"input_tokens": 3000, "output_tokens": 0}}
        fake.batch_results[r["batch_id"]] = [  # results arrive in any order
            MessageBatchIndividualResponse.model_validate({"custom_id": "dd-HDFCBANK",
                                                           "result": {"type": "succeeded", "message": refused}}),
            MessageBatchIndividualResponse.model_validate({"custom_id": "dd-RELIANCE",
                                                           "result": {"type": "succeeded", "message": ok}})]
        await team.bots["ai_analyst"].poll_batches()
        reps = (await c.request(topics.cmd("ai_analyst", "reports"), {"kind": "deep_dive"}))["reports"]
        by_head = {x["headline"].split(":")[0]: x for x in reps}
        assert set(by_head) == {"RELIANCE", "HDFCBANK"} and all(x["status"] == "ok" for x in reps)
        assert by_head["RELIANCE"]["usd"] == pytest.approx((3000 * 4 + 2000 * 20) / 1e6 * 0.5)  # batch half price
        assert by_head["HDFCBANK"]["served_by"] == "claude-opus-4-8"  # re-run as a normal request, fell back
        rerun = [kw for k, kw in fake.calls if k == "stream"]
        assert len(rerun) == 1 and rerun[0]["fallbacks"] == "default"
        assert (await c.request(topics.cmd("ai_analyst", "status"), {}))["open_batches"] == 0


async def test_disabled_without_credentials_and_stub_mode(tmp_path, monkeypatch):
    seed_tables(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no `ant auth login` profile either
    monkeypatch.setattr(AIAnalystBot, "client_factory", None)
    async with Team(tmp_path, ["ai_analyst"], settings=ai_settings(tmp_path)) as team:
        st = await team.client.request(topics.cmd("ai_analyst", "status"), {})
        assert st["mode"] == "disabled" and "ANTHROPIC_API_KEY" in st["reason"]
        r = await team.client.request(topics.cmd("ai_analyst", "run"), {"wait": True})
        assert not r["ran"] and "ANTHROPIC_API_KEY" in r["reason"]
        t = await team.client.request(topics.cmd("ai_analyst", "test"), {})
        assert t["ok_key"] is False
        up = await team.client.request(topics.cmd("ai_analyst", "status"), {})
        assert up["spent_today_usd"] == 0
    s2 = ai_settings(tmp_path, provider="stub")
    async with Team(tmp_path, ["ai_analyst"], settings=s2, namespace="stub") as team:
        r = await team.client.request(topics.cmd("ai_analyst", "run"), {"wait": True, "kind": "pre_open"})
        assert r["ran"] and r["headline"].startswith("[Demo stub - no AI]") and r["cost_usd"] == 0
        rep = (await team.client.request(topics.cmd("ai_analyst", "report"), {"id": r["report_id"]}))["report"]
        assert rep["provider"] == "stub" and "not by Claude" in rep["report"]["caveats"][0]
        stocks = rep["report"]["stocks"]
        assert len(stocks) == 4 and stocks[0]["key_levels"]["support"]
        assert stocks[-1]["symbol"] == "TCS" and stocks[-1]["watch_action"] == "hold"  # held, so it is analysed too
        assert TableReader(tmp_path / "data" / "tables").count("ai_costs") == 0


def test_packet_from_real_table_types_is_json_safe(tmp_path):
    import orjson

    from tradingbots.bots.ai_analyst.packet import build_packet
    from tradingbots.bots.ai_analyst.prompt import user_message

    seed_tables(tmp_path)
    s = make_settings(tmp_path)
    packet, notes = build_packet(TableReader(tmp_path / "data" / "tables"), s, "midday", 1_793_000_000.0, None)
    orjson.dumps(packet)  # no Decimal / numpy values left
    held = {x["symbol"]: x.get("position") for x in packet["stocks"]}
    assert held["INFY"]["qty"] == 10.0 and held["TCS"]["pnl_pct"] == pytest.approx(2.12, abs=0.01)  # held, not watched
    assert "TCS: no daily history yet" in notes and "<packet>" in user_message("midday", packet)
