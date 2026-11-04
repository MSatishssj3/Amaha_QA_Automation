"""Calling Claude, in one place.

This module owns the request shape, refusal fallbacks, structured output, token counting, cost
accounting and error classification. The bot itself never touches the SDK.

* Each request has a frozen system prompt marked for prompt caching, the per-run data packet as the
  user message, adaptive thinking, an explicit effort level, and a JSON-schema output format built
  from the pydantic report model.
* Server-side refusal fallbacks (``fallbacks: "default"``) are on by default for the models that
  support them. When the requested model declines, the API re-runs the request on the model
  Anthropic recommends for that refusal category, inside the same call. ``ai.refusal_fallbacks``
  turns this off. The Message Batches API does not accept fallbacks, so a refused batch item is
  re-run as a normal request.
* ``stop_reason`` is checked before anything is parsed. A refusal or a truncated answer is reported
  as such and is never parsed as a report.
* Cost is computed per attempt (``usage.iterations``), each at its own model's price, and every
  call is recorded, including declined and truncated ones, because they can be billed too.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import Any, Iterator, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from ...core.settings import AICfg
from .prompt import SYSTEM_PROMPT

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"  # gates the fallbacks: "default" form
# Models that accept server-side fallbacks. Claude Haiku 5.5 has none, so its requests never send the field.
FALLBACK_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"})
# USD per million tokens: (input, output, cache read, cache write at the 5-minute TTL). The configured model
# is priced from settings (ai.prices); this table covers models a fallback may be served by.
LIST_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.5),
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
BATCH_DISCOUNT = 0.5  # the Batches API bills all tokens at half price
_BILLED_ITERATIONS = ("message", "fallback_message", "advisor_message", "compaction")


class ClaudeError(Exception):
    """kind: auth | permission | not_found | bad_request | rate_limit | server | api | network | refusal |
    truncated | invalid_output. ``usd`` is what the failed call still cost."""

    def __init__(self, kind: str, message: str, *, retryable: bool = False, usd: float = 0.0,
                 details: dict | None = None) -> None:
        super().__init__(message)
        self.kind, self.retryable, self.usd, self.details = kind, retryable, usd, details or {}


@dataclass
class CallResult:
    parsed: BaseModel
    model: str  # requested
    served_by: str  # the model that produced the answer (differs after a fallback)
    fallback_used: bool
    stop_reason: str
    usage: dict
    usd: float
    request_id: str
    seconds: float


def prices_for(model: str, cfg: AICfg) -> tuple[float, float, float, float]:
    if model == cfg.model:
        p = cfg.prices
        return p.input, p.output, p.cache_read, p.cache_write
    return LIST_PRICES.get(model) or (cfg.prices.input, cfg.prices.output, cfg.prices.cache_read, cfg.prices.cache_write)


def usage_cost(usage: Any, requested: str, cfg: AICfg, discount: float = 1.0) -> tuple[float, dict]:
    """USD for one response. With fallbacks, ``usage.iterations`` lists every attempt (the top-level
    usage covers only the attempt that produced the message), each priced at its own model's rates."""
    its = [i for i in (getattr(usage, "iterations", None) or []) if getattr(i, "type", "") in _BILLED_ITERATIONS]
    entries = its or [usage]
    tot = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    usd = 0.0
    for e in entries:
        pi, po, pr, pw = prices_for(str(getattr(e, "model", None) or requested), cfg)
        inp, out = int(getattr(e, "input_tokens", 0) or 0), int(getattr(e, "output_tokens", 0) or 0)
        cr = int(getattr(e, "cache_read_input_tokens", 0) or 0)
        cw = int(getattr(e, "cache_creation_input_tokens", 0) or 0)
        usd += (inp * pi + out * po + cr * pr + cw * pw) / 1e6
        tot["input_tokens"] += inp
        tot["output_tokens"] += out
        tot["cache_read_tokens"] += cr
        tot["cache_write_tokens"] += cw
    return usd * discount, tot


def final_text(msg: Any) -> str:
    """The answer's text. After a mid-answer fallback the content holds the declined model's partial text,
    a ``fallback`` block, then the fallback model's answer, so only text after the last boundary counts."""
    blocks = list(getattr(msg, "content", None) or [])
    last = max((i for i, b in enumerate(blocks) if getattr(b, "type", "") == "fallback"), default=-1)
    return "".join(b.text for b in blocks[last + 1:] if getattr(b, "type", "") == "text")


def build_request(cfg: AICfg, user_text: str, schema: type[BaseModel], max_tokens: int | None = None) -> dict:
    """Request parameters shared by normal calls, token counting and batch items (no fallbacks here)."""
    return {
        "model": cfg.model,
        "max_tokens": int(max_tokens or cfg.max_output_tokens),
        # frozen instructions first, marked for caching; the per-run packet goes after the breakpoint
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_text}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": cfg.effort,
                          "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}},
    }


class Pong(BaseModel):
    reply: str


def ping_request(cfg: AICfg) -> dict:
    """The smallest useful call (a fraction of a cent): checks the key, the model name and structured output."""
    return {"model": cfg.model, "max_tokens": 1024,
            "system": [{"type": "text", "text": "You check that an API connection works."}],
            "messages": [{"role": "user", "content": "Reply with the single word OK."}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "low", "format": {"type": "json_schema",
                                                          "schema": anthropic.transform_schema(Pong)}}}


class Claude:
    def __init__(self, client: Any, cfg: AICfg) -> None:
        self.client = client  # anthropic.AsyncAnthropic (tests pass a fake with the same surface)
        self.cfg = cfg

    @contextlib.contextmanager
    def _errors(self, model: str) -> Iterator[None]:
        try:
            yield
        except anthropic.AuthenticationError:
            raise ClaudeError("auth", "the Anthropic API key was rejected - check ANTHROPIC_API_KEY in .env") from None
        except anthropic.PermissionDeniedError as e:
            raise ClaudeError("permission", f"this API key may not use {model}: {e.message}") from None
        except anthropic.NotFoundError as e:
            raise ClaudeError("not_found", f"unknown model or endpoint ({model}): {e.message}") from None
        except anthropic.BadRequestError as e:
            raise ClaudeError("bad_request", f"the request was rejected: {e.message}") from None
        except anthropic.RateLimitError:
            raise ClaudeError("rate_limit", "rate limited by the Anthropic API (after automatic retries)",
                              retryable=True) from None
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise ClaudeError("server", f"Anthropic API error {e.status_code} (after automatic retries)",
                                  retryable=True) from None
            raise ClaudeError("api", f"Anthropic API error {e.status_code}: {e.message}") from None
        except anthropic.APIConnectionError as e:  # includes timeouts
            raise ClaudeError("network", f"could not reach the Anthropic API: {e}", retryable=True) from None

    def _with_fallbacks(self, req: dict) -> dict:
        kw = dict(req)
        if self.cfg.refusal_fallbacks and req["model"] in FALLBACK_MODELS:
            kw["betas"] = [FALLBACK_BETA]
            kw["fallbacks"] = "default"
        return kw

    async def count_tokens(self, req: dict) -> int:
        """Input tokens of a request (system + packet + output schema), before sending it."""
        with self._errors(req["model"]):
            r = await self.client.beta.messages.count_tokens(model=req["model"], system=req["system"],
                                                             messages=req["messages"], thinking=req["thinking"],
                                                             output_config=req["output_config"])
        return int(r.input_tokens)

    async def run(self, req: dict, schema: type[T]) -> CallResult:
        """One streamed request (long thinking + JSON output would risk HTTP timeouts otherwise)."""
        t0 = time.monotonic()
        with self._errors(req["model"]):
            async with self.client.beta.messages.stream(**self._with_fallbacks(req),
                                                        timeout=self.cfg.timeout_s) as stream:
                msg = await stream.get_final_message()
        return self.finish(msg, req["model"], schema, time.monotonic() - t0)

    def finish(self, msg: Any, requested: str, schema: type[T], seconds: float = 0.0,
               discount: float = 1.0) -> CallResult:
        usd, usage = usage_cost(getattr(msg, "usage", None), requested, self.cfg, discount)
        iterations = getattr(getattr(msg, "usage", None), "iterations", None) or []
        fallback_used = any(getattr(i, "type", "") == "fallback_message" for i in iterations)
        served = str(getattr(msg, "model", "") or requested)
        info = {"served_by": served, "fallback_used": fallback_used, "usage": usage,
                "request_id": str(getattr(msg, "_request_id", "") or getattr(msg, "id", "") or "")}
        if msg.stop_reason == "refusal":  # branch on stop_reason; stop_details is informational and may be null
            sd = getattr(msg, "stop_details", None)
            cat = getattr(sd, "category", None) if sd is not None else None
            tail = " (the fallback model declined too)" if fallback_used else ""
            raise ClaudeError("refusal", f"Claude declined this request (category: {cat or 'unspecified'}){tail}",
                              usd=usd, details={**info, "category": cat})
        if msg.stop_reason == "max_tokens":
            raise ClaudeError("truncated", f"the answer hit ai.max_output_tokens ({self.cfg.max_output_tokens}) - "
                                           "raise it, or lower ai.effort / ai.max_stocks", usd=usd, details=info)
        text = final_text(msg)
        try:
            parsed = schema.model_validate_json(text)
        except ValidationError as e:
            raise ClaudeError("invalid_output", f"the answer did not match the report schema ({e.error_count()} "
                                                "problems)", usd=usd, details={**info, "text": text[:2000]}) from None
        return CallResult(parsed, requested, served, fallback_used, str(msg.stop_reason), usage, usd,
                          info["request_id"], seconds)

    # ---- Message Batches (nightly deep dives at half price) -------------------------------------------
    async def submit_batch(self, items: list[tuple[str, dict]]) -> str:
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        requests = [Request(custom_id=cid, params=MessageCreateParamsNonStreaming(**req)) for cid, req in items]
        with self._errors(self.cfg.model):
            batch = await self.client.messages.batches.create(requests=requests)
        return str(batch.id)

    async def batch_state(self, batch_id: str) -> tuple[str, dict]:
        with self._errors(self.cfg.model):
            b = await self.client.messages.batches.retrieve(batch_id)
        counts = {k: int(getattr(b.request_counts, k, 0) or 0)
                  for k in ("processing", "succeeded", "errored", "canceled", "expired")}
        return str(b.processing_status), counts

    async def batch_results(self, batch_id: str) -> list[Any]:
        """Each item has ``custom_id`` and ``result.type`` (succeeded / errored / canceled / expired).
        Results come in any order: key them by custom_id."""
        out = []
        with self._errors(self.cfg.model):
            async for r in await self.client.messages.batches.results(batch_id):
                out.append(r)
        return out
