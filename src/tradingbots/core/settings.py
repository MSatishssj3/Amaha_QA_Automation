"""The single settings schema shared by every bot.

The **config bot** owns the live, versioned copy; every other bot reads it from the bus key-value
store and reacts to ``config.changed``. ``config/settings.toml`` seeds it and is kept in sync, so you
can edit settings either on the dashboard or in the file.

Unknown keys are rejected (a typo must never silently fall back to a default where money is involved).
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from dhanbot.clock import parse_hhmm
from dhanbot.dhan.endpoints import SEGMENT_TO_CODE


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _hhmm(v: str) -> str:
    parse_hhmm(v)
    return v


class SystemCfg(_Strict):
    """Read once at process start (changing these needs a restart)."""

    data_dir: str = "data"
    log_dir: str = "logs"
    bus_backend: Literal["sqlite", "redis"] = "sqlite"
    redis_url: str = "redis://127.0.0.1:6379/0"
    bus_retention_hours: float = 48.0


class PlatformCfg(_Strict):
    name: Literal["simulated", "dhan"] = "simulated"
    dhan_auth: Literal["token", "totp"] = "token"
    simulated_speed: float = 1.0  # clock speed of the built-in simulated market (demo only)
    # The config bot keeps the Dhan token valid at least until this IST time each trading day (it is the
    # only bot that logs in or renews, so other bots never invalidate each other's tokens).
    # Use "23:59" if your dhan_bot strategy trades the MCX evening session.
    token_valid_until: str = "16:00"

    @field_validator("token_valid_until")
    @classmethod
    def _valid_until(cls, v: str) -> str:
        return _hhmm(v)


class WatchItem(_Strict):
    symbol: str  # trading symbol as in Dhan's instrument master (e.g. RELIANCE), or an index name
    segment: str = "NSE_EQ"
    security_id: int = 0  # optional; required for indices (e.g. NIFTY = 13 in IDX_I)
    mode: Literal["ticker", "quote", "full"] = "quote"

    @field_validator("segment")
    @classmethod
    def _seg(cls, v: str) -> str:
        if v not in SEGMENT_TO_CODE:
            raise ValueError(f"unknown segment {v!r}; one of {sorted(SEGMENT_TO_CODE)}")
        return v

    @property
    def key(self) -> str:
        return f"{self.segment}:{self.symbol}"


class MarketDataCfg(_Strict):
    quote_interval_ms: int = Field(250, ge=50, le=10_000)
    bar_timeframes: list[str] = ["1m"]
    snapshot_interval_s: float = Field(60.0, ge=10)
    ledger_lookback_days: int = Field(30, ge=1, le=365)
    record_raw: bool = False


class ScheduleCfg(_Strict):
    history_refresh: str = "16:00"
    history_years: float = Field(2.0, gt=0, le=10)
    ai_runs: list[str] = ["08:45", "12:30", "15:45"]
    money_interval_s: float = Field(300.0, ge=30)
    compaction: str = "23:30"
    budget_summary: str = "15:45"  # end-of-day budget summary (alert + history)

    @field_validator("history_refresh", "compaction", "budget_summary")
    @classmethod
    def _t(cls, v: str) -> str:
        return _hhmm(v)

    @field_validator("ai_runs")
    @classmethod
    def _runs(cls, v: list[str]) -> list[str]:
        return [_hhmm(x) for x in v]


class AIPrices(_Strict):
    """USD per million tokens (defaults: Claude Opus 5.5 list prices)."""

    input: float = 4.0
    output: float = 20.0
    cache_read: float = 0.20
    cache_write: float = 5.0


class AICfg(_Strict):
    """The AI analyst. Its output is advisory only: nothing it writes is ever turned into an order."""

    enabled: bool = True
    # anthropic: Claude (needs ANTHROPIC_API_KEY in .env). stub: rule-based demo reports, no AI, no cost.
    provider: Literal["anthropic", "stub"] = "anthropic"
    model: str = "claude-opus-5-5"
    # Claude Opus 5.5 defaults to "medium" when unset; analysis is intelligence-sensitive, so "high".
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    daily_usd_budget: float = Field(1.0, ge=0)  # runs that would exceed it are skipped (0 = no AI spend)
    max_stocks: int = Field(25, ge=1, le=200)
    max_output_tokens: int = Field(24000, ge=1024, le=128000)  # includes thinking; responses are streamed
    refusal_fallbacks: bool = True  # server-side fallback ("default" routing) when a request is declined
    nightly_batch: bool = False  # per-stock deep dives through the Message Batches API (half price)
    deep_dive_at: str = "19:30"  # when the nightly batch is submitted (trading days)
    timeout_s: float = Field(900.0, ge=30)
    prices: AIPrices = AIPrices()

    @field_validator("deep_dive_at")
    @classmethod
    def _dd(cls, v: str) -> str:
        return _hhmm(v)


class BudgetCfg(_Strict):
    """Daily budget gatekeeper limits. 0 means 'no limit' for that rule."""

    enabled: bool = True
    daily_capital_limit_inr: float = Field(100_000.0, ge=0)  # total value of new entries today
    max_capital_in_use_inr: float = Field(150_000.0, ge=0)  # open exposure + working entries, any moment
    per_stock_daily_limit_inr: float = Field(50_000.0, ge=0)
    per_stock_overrides: dict[str, float] = {}  # symbol -> daily limit
    per_platform_daily_limit_inr: dict[str, float] = {}  # platform -> daily limit
    max_trades_per_day: int = Field(20, ge=0)
    max_trades_per_stock: int = Field(5, ge=0)
    max_daily_loss_inr: float = Field(2_000.0, ge=0)
    alert_at_pct: list[float] = [80.0, 100.0]
    count_manual_trades: bool = True  # trades you place yourself in the Dhan app use up the budget too
    # closed: block entries when no policy is known or the budget bot has been silent for policy_max_age_s
    # last_known: keep enforcing the last policy received, however old (still blocks if none ever arrived)
    fail_mode: Literal["closed", "last_known"] = "closed"
    policy_max_age_s: float = Field(600.0, ge=30)

    @field_validator("alert_at_pct")
    @classmethod
    def _pcts(cls, v: list[float]) -> list[float]:
        if any(not 0 < x <= 200 for x in v):
            raise ValueError("alert thresholds must be between 0 and 200 (%)")
        return sorted(v)


class MoneyCfg(_Strict):
    sector_map_file: str = ""  # optional CSV: symbol,sector
    usd_inr: float = Field(85.0, gt=0)  # conversion for USD costs (AI API); set it to the current rate
    expense_categories: list[str] = ["data subscription", "server / VPS", "AI API", "software", "advisory", "other"]


class ExecutionCfg(_Strict):
    enabled: bool = False  # opt-in: runs your dhan_bot strategy inside the team with the budget guard
    dhanbot_config: str = "config/dhanbot.toml"  # see config/dhanbot.example.toml
    live: bool = False  # real orders need BOTH this and mode = "live" in the dhan_bot config


NOTIFY_INFO_KEYS = {  # INFO alerts worth a message (notify.info_keys); prefixes of the alert key
    "exec-started": "trading engine started",
    "exec-day-report": "trading engine's day report",
    "budget-summary": "budget end-of-day summary",
    "budget-changed": "daily budget limits changed",
    "ai-report": "AI report ready",
    "ai-deep-dive": "nightly AI deep dives done",
}


class NotifyCfg(_Strict):
    """Alerts to your phone (Telegram) and by email. Credentials live in .env (see .env.example)."""

    enabled: bool = True
    telegram: bool = True  # instant alerts + commands (/status, /budget, /pause, /flatten ...)
    email: bool = True  # the daily summary, plus alerts at email_min_level and above
    min_level: Literal["INFO", "WARNING", "ERROR"] = "WARNING"
    # INFO alerts that are still worth a message (alert key prefixes)
    info_keys: list[str] = ["exec-started", "exec-day-report", "budget-summary", "ai-report", "ai-deep-dive"]
    dedupe_minutes: float = Field(10.0, ge=0)  # the same alert is sent at most once per this many minutes
    quiet_hours: list[str] = ["22:00", "07:00"]  # [from, to] IST: only ERROR alerts are sent; [] = never quiet
    daily_summary_at: str = "15:50"  # trading days
    email_min_level: Literal["WARNING", "ERROR"] = "ERROR"
    commands: bool = True  # accept commands from your Telegram chat (only that chat)

    @field_validator("daily_summary_at")
    @classmethod
    def _summary_time(cls, v: str) -> str:
        return _hhmm(v)

    @field_validator("quiet_hours")
    @classmethod
    def _quiet(cls, v: list[str]) -> list[str]:
        if v and len(v) != 2:
            raise ValueError("quiet_hours is [] or [from, to], e.g. [\"22:00\", \"07:00\"]")
        return [_hhmm(x) for x in v]


class DashboardCfg(_Strict):
    address: str = "127.0.0.1"
    port: int = Field(8501, ge=1024, le=65535)
    refresh_s: float = Field(2.0, ge=0.5)
    session_minutes: int = Field(720, ge=5)


class Settings(_Strict):
    system: SystemCfg = SystemCfg()
    platform: PlatformCfg = PlatformCfg()
    watchlist: list[WatchItem] = [
        WatchItem(symbol="NIFTY", segment="IDX_I", security_id=13, mode="ticker"),
        WatchItem(symbol="RELIANCE"), WatchItem(symbol="HDFCBANK"), WatchItem(symbol="INFY"),
    ]
    market_data: MarketDataCfg = MarketDataCfg()
    schedule: ScheduleCfg = ScheduleCfg()
    ai: AICfg = AICfg()
    budget: BudgetCfg = BudgetCfg()
    money: MoneyCfg = MoneyCfg()
    execution: ExecutionCfg = ExecutionCfg()
    notify: NotifyCfg = NotifyCfg()
    dashboard: DashboardCfg = DashboardCfg()
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        seen: set[str] = set()
        for w in self.watchlist:
            if w.key in seen:
                raise ValueError(f"watchlist: duplicate {w.key}")
            seen.add(w.key)
            if w.segment == "IDX_I" and not w.security_id:
                raise ValueError(f"watchlist: index {w.symbol} needs security_id (NIFTY 50 = 13, NIFTY BANK = 25)")
        return self

    # ---- helpers ------------------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def benchmark(self) -> WatchItem | None:
        for w in self.watchlist:
            if w.segment == "IDX_I":
                return w
        return None

    def stocks(self) -> list[WatchItem]:
        return [w for w in self.watchlist if w.segment != "IDX_I"]


class SettingsError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid settings:\n  - " + "\n  - ".join(problems))


def validation_problems(e: ValidationError) -> list[str]:
    out = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "(root)"
        msg = err["msg"]
        if err["type"] == "extra_forbidden":
            msg = "unknown setting (typo?)"
        out.append(f"{loc}: {msg}")
    return out


def parse_settings(data: dict[str, Any]) -> Settings:
    try:
        return Settings.model_validate(data)
    except ValidationError as e:
        raise SettingsError(validation_problems(e)) from None


def load_settings_file(path: Path) -> Settings:
    if not path.exists():
        return Settings()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise SettingsError([f"{path}: TOML syntax error: {e}"]) from None
    return parse_settings(data)


# For containers: where the bus and the dashboard listen, without editing settings.toml. Applied only where a
# process reads these values at start, never to the settings that are saved, so an override cannot leak into the file.
def bus_location(s: Settings) -> tuple[str, str]:
    return (os.environ.get("TRADINGBOTS_BUS_BACKEND") or s.system.bus_backend,
            os.environ.get("TRADINGBOTS_REDIS_URL") or s.system.redis_url)


def dashboard_address(s: Settings) -> str:
    return os.environ.get("TRADINGBOTS_DASHBOARD_ADDRESS") or s.dashboard.address


def write_settings_file(path: Path, s: Settings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("# Managed by the config bot - edits here are imported on its next start.\n"
                   + tomli_w.dumps(s.to_dict()), encoding="utf-8")
    tmp.replace(path)


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge; lists and scalars in ``patch`` replace those in ``base``."""
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def diff(old: dict[str, Any], new: dict[str, Any], prefix: str = "") -> list[str]:
    """Human-readable list of changed keys: ``ai.effort: high -> max``."""
    out: list[str] = []
    for k in sorted(set(old) | set(new)):
        a, b = old.get(k), new.get(k)
        path = f"{prefix}{k}"
        if isinstance(a, dict) and isinstance(b, dict):
            out.extend(diff(a, b, path + "."))
        elif a != b:
            out.append(f"{path}: {_short(a)} -> {_short(b)}")
    return out


def _short(v: Any) -> str:
    s = repr(v)
    return s if len(s) <= 60 else s[:57] + "..."
