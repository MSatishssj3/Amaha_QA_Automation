"""AI analyst bot: Claude reads the team's tables and writes plain-language reports.

* Scheduled runs at ``schedule.ai_runs`` (default 08:45 pre-open, 12:30 midday, 15:45 post-close,
  trading days), plus on demand (``cmd.ai_analyst.run``, the dashboard's "run analysis now").
* Input is a compact data packet built from the tables (``packet.py``). Output is a structured
  report (``schema.py``) stored in SQLite and as Markdown under ``data/ai_analyst/reports/``, then
  announced with ``ai.report``.
* **Advisory only.** Reports are never turned into orders: no bot acts on them.
* Cost control:
  - the frozen system prompt is cached;
  - tokens are counted before sending;
  - a daily USD cap (``ai.daily_usd_budget``) skips any run that would exceed it;
  - every call's tokens and cost are logged to ``ai_costs`` (``ai.cost`` events). The money bot
    books them as an expense.
* Refusals: server-side fallbacks are on by default (see ``claude.py``). A declined request is
  reported as such and never parsed.
* Nightly deep dives (``ai.nightly_batch``): one request per stock through the Message Batches API,
  at half price. Batches reject fallbacks, so a refused item is re-run as a normal request.
* Without ``ANTHROPIC_API_KEY`` the bot runs in **disabled** mode and says how to enable it.
  ``ai.provider = "stub"`` writes rule-based demo reports, clearly marked, at no cost.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

import anthropic
from dhanbot.clock import ist_datetime
from pydantic import BaseModel

from ...core import topics
from ...core.bot import Bot
from ...core.secrets import load_env
from ...core.settings import Settings
from ...core.tables_io import TableReader, TableStore
from .claude import BATCH_DISCOUNT, Claude, ClaudeError, CallResult, build_request, prices_for
from .packet import build_packet
from .prompt import user_message
from .schema import DeepDive, Report
from .store import AIStore
from .stub import stub_report

DEEP_DIVE_MAX_TOKENS = 16000


class _Pong(BaseModel):
    reply: str


def _has_cli_profile() -> bool:
    """An ``ant auth login`` profile also authenticates the SDK, without an API key in .env."""
    return (Path.home() / ".config" / "anthropic").is_dir()


def report_markdown(kind: str, r: Report, when: str, served_by: str, usd: float, provider: str) -> str:
    who = "rule-based demo stub (no AI)" if provider == "stub" else f"{served_by}, cost ${usd:.4f}"
    lines = [f"# {r.headline}", "", f"*{kind.replace('_', ' ')} report - {when} - {who}*", "", r.narrative_markdown, "",
             "## Market", "", r.market_context, "", "## Stocks", "",
             "| Stock | Trend | Watch | Confidence | Outlook |", "|---|---|---|---|---|"]
    for s in r.stocks:
        lines.append(f"| {s.symbol} | {s.trend.replace('_', ' ')} | {s.watch_action.replace('_', ' ')} | {s.confidence} | "
                     f"{s.outlook.replace('|', '/')} |")
    for s in r.stocks:
        lines += ["", f"### {s.symbol}", "",
                  f"- Support: {', '.join(f'{x:,.2f}' for x in s.key_levels.support) or '-'}; "
                  f"resistance: {', '.join(f'{x:,.2f}' for x in s.key_levels.resistance) or '-'}"]
        lines += [f"- {o}" for o in s.observations] + [f"- Risk: {x}" for x in s.risks]
        lines += [f"- Data gap: {g}" for g in s.data_gaps]
    p = r.portfolio
    lines += ["", "## Portfolio", "", p.summary, "", f"- Concentration: {p.concentration}",
              f"- Charges: {p.charges_drag}", f"- Daily budget: {p.budget_use}"] + [f"- {o}" for o in p.observations]
    lines += ["", "## Caveats", ""] + [f"- {c}" for c in r.caveats]
    lines += ["", "*Decision support only - not financial advice. Nothing in this report places an order.*", ""]
    return "\n".join(lines)


def deep_dive_markdown(d: DeepDive, when: str, served_by: str, usd: float) -> str:
    lines = [f"# {d.symbol}: deep dive", "", f"*{when} - {served_by}, cost ${usd:.4f}*", "", d.summary, "",
             f"**Trend:** {d.trend.replace('_', ' ')} - **watch:** {d.watch_action.replace('_', ' ')} - "
             f"**confidence:** {d.confidence}", "", "## Trend", "", d.trend_analysis, "", "## Momentum", "", d.momentum,
             "", "## Volatility and risk", "", d.volatility_and_risk, "", "## Levels", "",
             f"- Support: {', '.join(f'{x:,.2f}' for x in d.key_levels.support) or '-'}",
             f"- Resistance: {', '.join(f'{x:,.2f}' for x in d.key_levels.resistance) or '-'}", "", "## Scenarios", ""]
    for sc in d.scenarios:
        lines += [f"**{sc.name}** - {sc.description}", f"- Confirms: {sc.trigger}", f"- Ruled out: {sc.invalidation}", ""]
    lines += ["## Caveats", ""] + [f"- {c}" for c in d.caveats]
    lines += ["", "*Decision support only - not financial advice.*", ""]
    return "\n".join(lines)


class AIAnalystBot(Bot):
    name = "ai_analyst"
    client_factory: Callable[[], Any] | None = None  # tests inject a fake Anthropic client here

    def configure(self) -> None:
        self.command("run", self.cmd_run)
        self.command("status", self.cmd_status)
        self.command("reports", self.cmd_reports)
        self.command("report", self.cmd_report)
        self.command("test", self.cmd_test)
        self.command("deep_dive", self.cmd_deep_dive)
        self.every(300.0, self.poll_batches, name="batches")
        self.daily(lambda: self.settings.ai.deep_dive_at, self.nightly, name="deep-dive")
        self.mode = "disabled"
        self.reason = ""
        self.claude: Claude | None = None
        self.running = ""
        self.last: dict = {}
        self._lock = asyncio.Lock()

    def details(self) -> dict:
        ai = self.settings.ai
        return {"mode": self.mode, "reason": self.reason, "running": self.running, "last": self.last,
                "model": ai.model, "effort": ai.effort, "budget_usd": ai.daily_usd_budget,
                "spent_today_usd": round(self.store.spent(self._day()), 4) if hasattr(self, "store") else None}

    async def setup(self) -> None:
        home = self.ctx.paths.bot("ai_analyst")
        self.store = AIStore(home / "ai.db")
        self.reports_dir = home / "reports"
        self.reader = TableReader(self.ctx.paths.tables)
        self.tables = TableStore(self.ctx.paths.tables)
        self._configure()
        self.spawn(self._schedule(), name="schedule")

    async def teardown(self) -> None:
        self.store.close()

    async def on_settings(self, old: Settings, new: Settings) -> None:
        if old.ai != new.ai:
            self._configure()

    def _configure(self) -> None:
        ai = self.settings.ai
        self.claude = None
        if not ai.enabled:
            self.mode, self.reason = "disabled", "AI analysis is switched off (ai.enabled = false)"
            return
        if ai.provider == "stub":
            self.mode, self.reason = "stub", "demo stub: rule-based reports, no AI and no cost"
            return
        factory = type(self).client_factory
        if factory is not None:
            client = factory()
        else:
            env = load_env(self.ctx.paths.base)
            kw = {k: v for k, v in (("api_key", env.get("ANTHROPIC_API_KEY")),
                                    ("auth_token", env.get("ANTHROPIC_AUTH_TOKEN"))) if v}
            if not kw and not _has_cli_profile():
                self.mode = "disabled"
                self.reason = ("no Anthropic credentials - add ANTHROPIC_API_KEY=... to .env and restart this bot, or "
                               "set ai.provider = \"stub\" for demo reports")
                return
            client = anthropic.AsyncAnthropic(max_retries=2, **kw)
        self.claude = Claude(client, ai)
        fb = "on" if ai.refusal_fallbacks else "off"
        self.mode, self.reason = "anthropic", f"{ai.model}, effort {ai.effort}, refusal fallbacks {fb}"

    # ---- helpers ---------------------------------------------------------------------------------------------
    def _day(self, ts: float | None = None) -> str:
        return ist_datetime(self.clock.time() if ts is None else ts).date().isoformat()

    @staticmethod
    def kind_for(ts: float) -> str:
        hm = ist_datetime(ts).strftime("%H:%M")
        return "pre_open" if hm < "09:15" else "midday" if hm < "15:30" else "post_close"

    def _estimate(self, input_tokens: int, purpose: str, max_tokens: int, discount: float = 1.0) -> float:
        pi, po, _, _ = prices_for(self.settings.ai.model, self.settings.ai)
        out = self.store.avg_output_tokens(purpose) or max_tokens * 0.5
        return (input_tokens * pi + min(out, max_tokens) * po) / 1e6 * discount

    def _budget_problem(self, estimate: float) -> str:
        cap = self.settings.ai.daily_usd_budget
        spent = self.store.spent(self._day())
        if cap <= 0:
            return "the daily AI budget is $0 (ai.daily_usd_budget)"
        if spent + estimate > cap:
            return f"today's AI budget is ${cap:.2f}: ${spent:.2f} spent, this run would cost about ${estimate:.2f}"
        return ""

    async def _record_call(self, purpose: str, model: str, served_by: str, stop_reason: str, usage: dict, usd: float,
                           request_id: str = "", error: str = "") -> None:
        now = self.clock.time()
        day = ist_datetime(now).date()
        self.store.add_call(ts=now, day=day.isoformat(), purpose=purpose, model=model, served_by=served_by,
                            stop_reason=stop_reason, usage=usage, usd=usd, request_id=request_id, error=error)
        row = {"ts": now, "model": served_by or model, "purpose": purpose,
               "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
               "cache_read_tokens": usage.get("cache_read_tokens", 0),
               "cache_write_tokens": usage.get("cache_write_tokens", 0), "usd": round(usd, 6),
               "inr": round(usd * self.settings.money.usd_inr, 4)}
        parts = await asyncio.to_thread(self.tables.append, "ai_costs", [row], day)
        await self.publish(topics.AI_COST, row)
        await self.publish(topics.TABLES_UPDATED, {"table": "ai_costs", "rows": 1, "partitions": parts})

    async def _record_result(self, purpose: str, res: CallResult) -> None:
        await self._record_call(purpose, res.model, res.served_by, res.stop_reason, res.usage, res.usd, res.request_id)

    async def _record_error(self, purpose: str, e: ClaudeError) -> None:
        if e.usd or e.details.get("usage"):
            await self._record_call(purpose, self.settings.ai.model, e.details.get("served_by", ""), e.kind,
                                    e.details.get("usage") or {}, e.usd, e.details.get("request_id", ""), e.kind)

    async def _publish_report(self, rid: str, kind: str, status: str, headline: str, usd: float, served_by: str,
                              provider: str, fallback_used: bool = False, error: str = "") -> None:
        now = self.clock.time()
        self.last = {"id": rid, "kind": kind, "status": status, "headline": headline, "ts": now, "usd": round(usd, 4),
                     "served_by": served_by, "provider": provider, "error": error}
        await self.publish(topics.AI_REPORT, {"report_id": rid, "kind": kind, "status": status, "headline": headline,
                                              "cost_usd": round(usd, 4), "ts": now, "served_by": served_by,
                                              "provider": provider, "fallback_used": fallback_used, "error": error})

    def _write_md(self, day: str, now: float, kind: str, md: str, suffix: str = "") -> None:
        d = self.reports_dir / day
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{ist_datetime(now):%H%M}-{kind}{suffix}.md").write_text(md, encoding="utf-8")

    # ---- the analysis ---------------------------------------------------------------------------------------
    async def _schedule(self) -> None:
        while not self._stop.is_set():
            runs = self.settings.schedule.ai_runs
            now = self.clock.time()
            if not runs:
                await self.clock.sleep(300.0)
                continue
            nxt = min(self.ctx.calendar.next_time(now, t) for t in runs)
            if nxt - now > 300:
                await self.clock.sleep(300.0)  # re-read the schedule every 5 minutes
                continue
            await self.clock.sleep(max(0.0, nxt - now))
            if self.mode != "disabled":
                await self.analyse(self.kind_for(nxt), source="schedule")

    async def analyse(self, kind: str, symbols: list[str] | None = None, source: str = "schedule") -> dict:
        if self.mode == "disabled":
            return {"ran": False, "reason": self.reason}
        async with self._lock:
            self.running = kind
            try:
                return await self._analyse(kind, symbols, source)
            finally:
                self.running = ""

    async def _analyse(self, kind: str, symbols: list[str] | None, source: str) -> dict:
        s, now = self.settings, self.clock.time()
        day = self._day(now)
        usage = await self.bus.kv_get(topics.KV_BUDGET_USAGE)
        packet, _ = await asyncio.to_thread(build_packet, self.reader, s, kind, now, usage, symbols)
        if not packet["stocks"]:
            return {"ran": False, "reason": "nothing to analyse: the watchlist has no stocks"}
        when = f"{ist_datetime(now):%Y-%m-%d %H:%M} IST"
        if self.mode == "stub":
            r = stub_report(packet)
            md = report_markdown(kind, r, when, "stub", 0.0, "stub")
            rid = self.store.add_report(ts=now, day=day, kind=kind, status="ok", model="stub", served_by="stub",
                                        headline=r.headline, report=r.model_dump(), packet=packet, usd=0.0,
                                        markdown=md, provider="stub")
            self._write_md(day, now, kind, md)
            await self._publish_report(rid, kind, "ok", r.headline, 0.0, "stub", "stub")
            return {"ran": True, "report_id": rid, "headline": r.headline, "cost_usd": 0.0, "provider": "stub"}
        assert self.claude is not None
        self.claude.cfg = s.ai
        req = build_request(s.ai, user_message(kind, packet), Report)
        purpose = f"report:{kind}"
        try:
            n_in = await self.claude.count_tokens(req)
        except ClaudeError as e:
            return await self._failed(kind, purpose, e, packet, when)
        problem = self._budget_problem(self._estimate(n_in, "report:%", req["max_tokens"]))
        if problem:
            await self.alert(f"AI {kind.replace('_', ' ')} analysis skipped: {problem}", "WARNING", key="ai-budget")
            return {"ran": False, "reason": problem, "input_tokens": n_in}
        try:
            res = await self.claude.run(req, Report)
        except ClaudeError as e:
            return await self._failed(kind, purpose, e, packet, when)
        await self._record_result(purpose, res)
        r: Report = res.parsed  # type: ignore[assignment]
        got = {v.symbol for v in r.stocks}
        missing = [x["symbol"] for x in packet["stocks"] if x["symbol"] not in got]
        if missing:  # say so rather than silently showing fewer stocks
            r.caveats.append(f"The analysis left out: {', '.join(missing)}")
        md = report_markdown(kind, r, when, res.served_by, res.usd, "anthropic")
        rid = self.store.add_report(ts=now, day=day, kind=kind, status="ok", model=res.model, served_by=res.served_by,
                                    headline=r.headline, report=r.model_dump(), markdown=md, packet=packet, usd=res.usd)
        self._write_md(day, now, kind, md)
        await self._publish_report(rid, kind, "ok", r.headline, res.usd, res.served_by, "anthropic", res.fallback_used)
        if source == "schedule":
            await self.alert(f"AI {kind.replace('_', ' ')} report: {r.headline}", "INFO", key="ai-report")
        return {"ran": True, "report_id": rid, "headline": r.headline, "cost_usd": round(res.usd, 4),
                "served_by": res.served_by, "fallback_used": res.fallback_used, "input_tokens": n_in,
                "cache_read_tokens": res.usage.get("cache_read_tokens", 0)}

    async def _failed(self, kind: str, purpose: str, e: ClaudeError, packet: dict, when: str) -> dict:
        await self._record_error(purpose, e)
        now = self.clock.time()
        rid = self.store.add_report(ts=now, day=self._day(now), kind=kind, status=e.kind, model=self.settings.ai.model,
                                    served_by=e.details.get("served_by", ""), headline="", report=None,
                                    markdown="", packet=packet, usd=e.usd, error=str(e))
        await self._publish_report(rid, kind, e.kind, "", e.usd, e.details.get("served_by", ""), "anthropic",
                                   bool(e.details.get("fallback_used")), str(e))
        level = "WARNING" if e.retryable or e.kind == "refusal" else "ERROR"
        await self.alert(f"AI {kind.replace('_', ' ')} analysis failed ({e.kind}): {e}", level, key=f"ai-{e.kind}")
        return {"ran": False, "error": str(e), "error_kind": e.kind, "report_id": rid, "cost_usd": round(e.usd, 4)}

    # ---- nightly deep dives (Message Batches) ------------------------------------------------------------------
    async def nightly(self) -> None:
        if self.settings.ai.nightly_batch and self.mode == "anthropic":
            await self.submit_deep_dives()

    async def submit_deep_dives(self, symbols: list[str] | None = None) -> dict:
        if self.mode != "anthropic" or self.claude is None:
            return {"submitted": False, "reason": self.reason if self.mode != "stub" else "not available in stub mode"}
        s, now = self.settings, self.clock.time()
        self.claude.cfg = s.ai
        usage = await self.bus.kv_get(topics.KV_BUDGET_USAGE)
        wanted = symbols or [w.symbol for w in s.stocks()][: s.ai.max_stocks]
        packets: dict[str, dict] = {}
        for sym in wanted:
            pkt, _ = await asyncio.to_thread(build_packet, self.reader, s, "deep_dive", now, usage, [sym])
            packets[sym] = pkt
        if not packets:
            return {"submitted": False, "reason": "no stocks to analyse"}
        items = [(f"dd-{sym}", build_request(s.ai, user_message("deep_dive", p), DeepDive, DEEP_DIVE_MAX_TOKENS))
                 for sym, p in packets.items()]
        try:
            n_in = await self.claude.count_tokens(items[0][1])
        except ClaudeError as e:
            return {"submitted": False, "reason": str(e)}
        est = self._estimate(n_in, "deep_dive", DEEP_DIVE_MAX_TOKENS, BATCH_DISCOUNT) * len(items)
        problem = self._budget_problem(est)
        if problem:
            await self.alert(f"AI deep dives skipped: {problem}", "WARNING", key="ai-budget")
            return {"submitted": False, "reason": problem}
        try:
            bid = await self.claude.submit_batch(items)
        except ClaudeError as e:
            await self.alert(f"AI deep-dive batch not submitted: {e}", "ERROR", key=f"ai-{e.kind}")
            return {"submitted": False, "reason": str(e)}
        self.store.add_batch(bid, now, self._day(now), list(packets))
        self._batch_packets = getattr(self, "_batch_packets", {})
        self._batch_packets[bid] = packets
        return {"submitted": True, "batch_id": bid, "requests": len(items), "estimate_usd": round(est, 4)}

    async def poll_batches(self) -> None:
        if self.claude is None:
            return
        for b in self.store.open_batches():
            try:
                status, counts = await self.claude.batch_state(b["id"])
            except ClaudeError as e:
                self._error("batch status", e)
                continue
            if status != "ended":
                continue
            results = await self.claude.batch_results(b["id"])
            done, failed = await self._collect(b, results)
            self.store.close_batch(b["id"], "ended", counts)
            await self.alert(f"AI nightly deep dives ready: {done} stocks" + (f", {failed} failed" if failed else ""),
                             "INFO", key="ai-deep-dive")

    async def _collect(self, batch: dict, results: list) -> tuple[int, int]:
        s, done, failed = self.settings, 0, 0
        packets = getattr(self, "_batch_packets", {}).get(batch["id"], {})
        assert self.claude is not None
        for r in sorted(results, key=lambda x: x.custom_id):  # results arrive in any order: key by custom_id
            sym = r.custom_id.removeprefix("dd-")
            now = self.clock.time()
            pkt = packets.get(sym)
            if pkt is None:
                pkt, _ = await asyncio.to_thread(build_packet, self.reader, s, "deep_dive", now, None, [sym])
            if r.result.type != "succeeded":
                failed += 1
                self.store.add_report(ts=now, day=self._day(now), kind="deep_dive", status=r.result.type,
                                      model=s.ai.model, served_by="", headline=f"{sym}: deep dive {r.result.type}",
                                      report=None, markdown="", packet=pkt, usd=0.0, error=r.result.type)
                continue
            try:
                res = self.claude.finish(r.result.message, s.ai.model, DeepDive, discount=BATCH_DISCOUNT)
            except ClaudeError as e:
                await self._record_error("deep_dive", e)
                if e.kind != "refusal":
                    failed += 1
                    continue
                try:  # batches cannot fall back server-side: re-run the refused item as a normal request
                    res = await self.claude.run(build_request(s.ai, user_message("deep_dive", pkt), DeepDive,
                                                              DEEP_DIVE_MAX_TOKENS), DeepDive)
                except ClaudeError as e2:
                    await self._record_error("deep_dive", e2)
                    failed += 1
                    continue
            await self._record_result("deep_dive", res)
            d: DeepDive = res.parsed  # type: ignore[assignment]
            when = f"{ist_datetime(now):%Y-%m-%d %H:%M} IST"
            md = deep_dive_markdown(d, when, res.served_by, res.usd)
            headline = f"{d.symbol}: {d.trend.replace('_', ' ')} - {d.watch_action.replace('_', ' ')}"
            rid = self.store.add_report(ts=now, day=self._day(now), kind="deep_dive", status="ok", model=res.model,
                                        served_by=res.served_by, headline=headline, report=d.model_dump(),
                                        markdown=md, packet=pkt, usd=res.usd)
            self._write_md(self._day(now), now, "deep_dive", md, f"-{sym}")
            await self._publish_report(rid, "deep_dive", "ok", headline, res.usd, res.served_by, "anthropic",
                                       res.fallback_used)
            done += 1
        return done, failed

    # ---- commands ------------------------------------------------------------------------------------------------
    async def cmd_run(self, data: dict) -> dict:
        kind = str(data.get("kind") or "on_demand")
        if kind not in ("pre_open", "midday", "post_close", "on_demand"):
            return {"ran": False, "reason": f"unknown kind {kind!r}"}
        symbols = list(data["symbols"]) if data.get("symbols") else None
        if self.mode == "disabled":
            return {"ran": False, "reason": self.reason}
        if data.get("wait"):
            return await self.analyse(kind, symbols, source="command")
        if self.running:
            return {"started": False, "reason": f"an analysis is already running ({self.running})"}
        self.spawn(self.analyse(kind, symbols, source="command"), name="run")
        return {"started": True, "kind": kind}

    async def cmd_status(self, data: dict) -> dict:
        ai = self.settings.ai
        day = self._day()
        return {"mode": self.mode, "reason": self.reason, "model": ai.model, "effort": ai.effort,
                "refusal_fallbacks": ai.refusal_fallbacks, "budget_usd": ai.daily_usd_budget,
                "spent_today_usd": round(self.store.spent(day), 4), "running": self.running, "last": self.last,
                "open_batches": len(self.store.open_batches()), "schedule": self.settings.schedule.ai_runs,
                "calls_today": len(self.store.calls(day))}

    async def cmd_reports(self, data: dict) -> dict:
        return {"reports": self.store.reports(int(data.get("limit") or 20), data.get("kind"))}

    async def cmd_report(self, data: dict) -> dict:
        r = self.store.report(str(data.get("id") or ""))
        return {"found": r is not None, "report": r}

    async def cmd_deep_dive(self, data: dict) -> dict:
        return await self.submit_deep_dives(list(data["symbols"]) if data.get("symbols") else None)

    async def cmd_test(self, data: dict) -> dict:
        """One tiny real call to check the key (a fraction of a cent; runs even when the daily cap is used up)."""
        if self.mode != "anthropic" or self.claude is None:
            return {"ok_key": False, "reason": self.reason}
        ai = self.settings.ai
        req = {"model": ai.model, "max_tokens": 1024,
               "system": [{"type": "text", "text": "You check that an API connection works."}],
               "messages": [{"role": "user", "content": "Reply with the single word OK."}],
               "thinking": {"type": "adaptive"},
               "output_config": {"effort": "low", "format": {"type": "json_schema",
                                                             "schema": anthropic.transform_schema(_Pong)}}}
        try:
            res = await self.claude.run(req, _Pong)
        except ClaudeError as e:
            await self._record_error("test", e)
            return {"ok_key": e.kind not in ("auth", "permission"), "error": str(e), "error_kind": e.kind}
        await self._record_result("test", res)
        return {"ok_key": True, "reply": res.parsed.reply, "served_by": res.served_by,  # type: ignore[attr-defined]
                "cost_usd": round(res.usd, 6), "seconds": round(res.seconds, 2)}
