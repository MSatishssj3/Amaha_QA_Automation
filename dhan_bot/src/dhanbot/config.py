"""Configuration: ``config.toml`` + ``.env`` + environment -> validated, frozen dataclasses.

Design rules
* Unknown keys are **errors** (a typo such as ``max_daly_loss_inr`` must never silently fall
  back to a default when real money is involved).
* All problems are reported at once.
* Secrets only come from the environment / ``.env`` and are redacted in ``repr``.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_type_hints

from .clock import parse_hhmm
from .dhan.endpoints import (
    LIVE_AUTH_BASE,
    LIVE_FEED_URL,
    LIVE_INSTRUMENTS_CSV,
    LIVE_ORDER_WS_URL,
    LIVE_REST_BASE,
    MODE_CODES,
    SEGMENT_TO_CODE,
    Endpoints,
)
from .errors import ConfigError

# Hard ceilings imposed by Dhan / SEBI. Config may go lower, never higher.
HARD_MAX_ORDERS_PER_SEC = 10
HARD_MAX_ORDERS_PER_MIN = 250


@dataclass(frozen=True)
class ModeCfg:
    mode: str = "paper"  # paper | live


@dataclass(frozen=True)
class DhanCfg:
    auth: str = "token"  # token | totp | oauth
    expected_ip: str = ""  # optional: assert the host's public IP equals this
    public_ip_url: str = "https://api.ipify.org"  # used by `check` to discover this host's public IP
    cid_prefix: str = "DB"  # correlationId prefix that marks orders as placed by this bot


@dataclass(frozen=True)
class EndpointsCfg:
    rest_base: str = LIVE_REST_BASE
    auth_base: str = LIVE_AUTH_BASE
    feed_url: str = LIVE_FEED_URL
    order_ws_url: str = LIVE_ORDER_WS_URL
    instruments_csv_url: str = LIVE_INSTRUMENTS_CSV

    def build(self) -> Endpoints:
        return Endpoints(self.rest_base, self.auth_base, self.feed_url, self.order_ws_url, self.instruments_csv_url)


_SESSION_PROFILES: dict[str, dict[str, str]] = {
    "nse": dict(preflight="08:30", feed_connect="09:00", market_open="09:15", entry_start="09:15",
                entry_end="15:00", squareoff_time="15:10", session_end="15:30"),
    "mcx": dict(preflight="08:30", feed_connect="08:55", market_open="09:00", entry_start="09:00",
                entry_end="23:00", squareoff_time="23:15", session_end="23:30"),
}


@dataclass(frozen=True)
class SessionCfg:
    profile: str = "nse"
    preflight: str = ""
    feed_connect: str = ""
    market_open: str = ""
    entry_start: str = ""
    entry_end: str = ""
    squareoff_time: str = ""
    session_end: str = ""
    squareoff_enabled: bool = True
    holidays_file: str = "holidays.txt"
    exit_on_holiday: bool = True


@dataclass(frozen=True)
class FeedCfg:
    stale_after_s: float = 5.0
    ltt_mode: str = "auto"  # auto | utc | ist_shifted
    record_ticks: bool = False
    heartbeat_s: float = 10.0


@dataclass(frozen=True)
class ExecutionCfg:
    default_order_type: str = "LIMIT"  # LIMIT (marketable limit) | MARKET
    validity: str = "DAY"  # DAY | IOC
    entry_buffer_ticks: int = 2
    exit_buffer_ticks: int = 3
    entry_ttl_ms: int = 3000
    reprice_once: bool = True
    exit_mode: str = "super_order"  # super_order | bot_managed | none
    sl_limit_buffer_ticks: int = 5
    allow_slicing: bool = False
    flatten_scope: str = "bot"  # bot | all
    order_timeout_ms: int = 1500
    squareoff_reprice_s: float = 2.0
    squareoff_max_reprices: int = 5
    squareoff_market_fallback: bool = True
    flatten_on_shutdown: bool = True  # exit INTRADAY bot positions if the bot is stopped mid-session
    keep_warm_s: float = 20.0  # ping the REST API this often so order connections stay hot


@dataclass(frozen=True)
class RiskCfg:
    max_daily_loss_inr: float = 2000.0
    daily_profit_lock_inr: float = 0.0  # 0 = off
    max_order_value_inr: float = 100000.0
    max_gross_exposure_inr: float = 300000.0
    max_open_positions: int = 3
    max_orders_per_day: int = 1000  # new order placements by the bot
    max_trades_per_instrument: int = 20
    max_consecutive_losses: int = 5
    price_band_pct: float = 2.0
    price_band_options_pct: float = 10.0
    duplicate_window_ms: int = 500
    reject_storm_count: int = 5
    reject_storm_window_s: float = 60.0
    token_min_validity_s: float = 300.0
    margin_check: bool = True
    margin_buffer: float = 1.1
    include_manual_trades: bool = False
    broker_killswitch_on_halt: bool = False
    pnl_exit_backstop: bool = True
    pnl_exit_backstop_factor: float = 1.25


@dataclass(frozen=True)
class RateLimitCfg:
    orders_per_sec: int = 10
    orders_per_min: int = 250
    order_api_calls_per_day: int = 4500  # place + modify + cancel (Dhan's daily cap is higher)
    entry_max_wait_ms: int = 0  # 0 = reject entries immediately when no token is available
    non_trading_per_sec: int = 18
    data_per_sec: int = 4
    quote_per_sec: int = 1


@dataclass(frozen=True)
class ChargesCfg:
    brokerage_per_order_inr: float = 20.0
    statutory_pct: float = 0.03  # % of turnover per executed side (rough STT+exchange+GST+stamp)


@dataclass(frozen=True)
class PaperCfg:
    ack_ms: float = 30.0
    slippage_ticks: int = 1
    partial_fills: bool = False
    starting_balance_inr: float = 100000.0


@dataclass(frozen=True)
class ReconcileCfg:
    orders_interval_s: float = 3.0
    positions_interval_s: float = 10.0
    funds_interval_s: float = 60.0
    fast_poll_s: float = 1.0
    adopt_broker_qty: bool = True


@dataclass(frozen=True)
class InstrumentMasterCfg:
    tick_unit: str = "auto"  # auto | paise | rupees  (unit of SEM_TICK_SIZE in Dhan's CSV)
    extra_segments: tuple[str, ...] = ()
    keep_days: int = 3


@dataclass(frozen=True)
class InstrumentCfg:
    alias: str
    segment: str
    trading_symbol: str = ""
    security_id: int = 0
    mode: str = "quote"  # ticker | quote | full
    product: str = "INTRADAY"
    max_qty: int = 1  # max units per order (for F&O: units, i.e. lots * lot size)
    tradable: bool = True


@dataclass(frozen=True)
class StrategyCfg:
    name: str = ""  # module name under dhanbot.strategy / dhanbot.strategy.examples, or "path/file.py:Class"
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TelegramCfg:
    enabled: bool = False
    commands: bool = True
    max_messages_per_min: int = 20


@dataclass(frozen=True)
class PerfCfg:
    uvloop: bool = True
    gc_freeze: bool = True
    gc_gen0_threshold: int = 50000
    strategy_tick_budget_us: float = 50.0
    latency_report_s: float = 60.0


@dataclass(frozen=True)
class PathsCfg:
    data_dir: str = "data"
    log_dir: str = "logs"


@dataclass(frozen=True)
class Secrets:
    client_id: str = ""
    access_token: str = ""
    pin: str = ""
    totp_secret: str = ""
    app_id: str = ""
    app_secret: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""

    def values(self) -> list[str]:
        return [v for v in (self.access_token, self.pin, self.totp_secret, self.app_secret, self.telegram_token) if v]

    def __repr__(self) -> str:
        shown = {f.name: ("***" if getattr(self, f.name) and f.name != "client_id" else getattr(self, f.name))
                 for f in dataclasses.fields(self)}
        return f"Secrets({shown})"


@dataclass(frozen=True)
class AppConfig:
    mode: ModeCfg = field(default_factory=ModeCfg)
    dhan: DhanCfg = field(default_factory=DhanCfg)
    endpoints: EndpointsCfg = field(default_factory=EndpointsCfg)
    session: SessionCfg = field(default_factory=SessionCfg)
    feed: FeedCfg = field(default_factory=FeedCfg)
    execution: ExecutionCfg = field(default_factory=ExecutionCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    rate_limits: RateLimitCfg = field(default_factory=RateLimitCfg)
    charges: ChargesCfg = field(default_factory=ChargesCfg)
    paper: PaperCfg = field(default_factory=PaperCfg)
    reconcile: ReconcileCfg = field(default_factory=ReconcileCfg)
    instrument_master: InstrumentMasterCfg = field(default_factory=InstrumentMasterCfg)
    instruments: tuple[InstrumentCfg, ...] = ()
    freeze_qty: dict[str, int] = field(default_factory=dict)
    margin_factors: dict[str, float] = field(default_factory=lambda: {
        "NSE_EQ:INTRADAY": 0.2, "BSE_EQ:INTRADAY": 0.2,
        "NSE_EQ:CNC": 1.0, "BSE_EQ:CNC": 1.0,
        "OPTION:BUY": 1.0,
    })
    strategy: StrategyCfg = field(default_factory=StrategyCfg)
    telegram: TelegramCfg = field(default_factory=TelegramCfg)
    perf: PerfCfg = field(default_factory=PerfCfg)
    paths: PathsCfg = field(default_factory=PathsCfg)
    secrets: Secrets = field(default_factory=Secrets)
    base_dir: str = "."

    # ---- derived helpers ----
    @property
    def is_live(self) -> bool:
        return self.mode.mode == "live"

    @property
    def data_dir(self) -> Path:
        p = Path(self.paths.data_dir)
        return p if p.is_absolute() else Path(self.base_dir) / p

    @property
    def log_dir(self) -> Path:
        p = Path(self.paths.log_dir)
        return p if p.is_absolute() else Path(self.base_dir) / p

    def resolve_path(self, rel: str) -> Path:
        p = Path(rel)
        if p.is_absolute():
            return p
        candidate = Path(self.base_dir) / p
        if candidate.exists():
            return candidate
        return self.data_dir / p

    def session_times(self) -> dict[str, str]:
        return {k: getattr(self.session, k) for k in
                ("preflight", "feed_connect", "market_open", "entry_start", "entry_end", "squareoff_time", "session_end")}

    def with_overrides(self, **sections: Any) -> "AppConfig":
        return dataclasses.replace(self, **sections)


# ---------------------------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------------------------

_SECTION_TYPES = {
    "mode": ModeCfg, "dhan": DhanCfg, "endpoints": EndpointsCfg, "session": SessionCfg, "feed": FeedCfg,
    "execution": ExecutionCfg, "risk": RiskCfg, "rate_limits": RateLimitCfg, "charges": ChargesCfg,
    "paper": PaperCfg, "reconcile": ReconcileCfg, "instrument_master": InstrumentMasterCfg,
    "strategy": StrategyCfg, "telegram": TelegramCfg, "perf": PerfCfg, "paths": PathsCfg,
}


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal ``.env`` parser: KEY=VALUE lines, ``#`` comments, optional surrounding quotes."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


def _coerce(name: str, typ: Any, value: Any, problems: list[str]) -> Any:
    origin = getattr(typ, "__origin__", None)
    try:
        if typ is bool:
            if isinstance(value, bool):
                return value
            raise TypeError
        if typ is int:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
                raise TypeError
            return int(value)
        if typ is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError
            return float(value)
        if typ is str:
            if not isinstance(value, str):
                raise TypeError
            return value
        if origin is tuple:
            if not isinstance(value, (list, tuple)):
                raise TypeError
            return tuple(str(v) for v in value)
        if origin is dict or typ is dict:
            if not isinstance(value, dict):
                raise TypeError
            return dict(value)
    except (TypeError, ValueError):
        problems.append(f"{name}: expected {getattr(typ, '__name__', typ)}, got {value!r}")
        return None
    return value


def _build(cls: type, data: dict[str, Any], prefix: str, problems: list[str]) -> Any:
    hints = get_type_hints(cls)
    known = {f.name for f in dataclasses.fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in known:
            problems.append(f"{prefix}.{key}: unknown setting (typo?)")
            continue
        v = _coerce(f"{prefix}.{key}", hints[key], value, problems)
        if v is not None:
            kwargs[key] = v
    try:
        return cls(**kwargs)
    except TypeError as e:  # missing required fields
        problems.append(f"{prefix}: {e}")
        return None


def load_config(path: str | Path | None = None, env: dict[str, str] | None = None,
                env_file: str | Path | None = None, data: dict[str, Any] | None = None,
                live_requested: bool = False) -> AppConfig:
    """Load + validate. ``data`` lets tests pass a dict instead of a file."""
    problems: list[str] = []
    base_dir = Path(".")
    if path is not None:
        p = Path(path)
        if not p.exists():
            raise ConfigError([f"config file not found: {p}"])
        with p.open("rb") as fh:
            try:
                data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as e:
                raise ConfigError([f"{p}: TOML syntax error: {e}"]) from e
        base_dir = p.resolve().parent
    data = dict(data or {})

    # environment: process env overrides .env file
    env_values: dict[str, str] = {}
    ef = Path(env_file) if env_file else base_dir / ".env"
    env_values.update(parse_env_file(ef))
    env_values.update(env if env is not None else os.environ)

    sections: dict[str, Any] = {}
    for name, cls in _SECTION_TYPES.items():
        raw = data.pop(name, {})
        if not isinstance(raw, dict):
            problems.append(f"[{name}] must be a table")
            raw = {}
        if name == "session":
            raw = _apply_session_profile(raw, problems)
        obj = _build(cls, raw, name, problems)
        if obj is not None:
            sections[name] = obj

    instruments: list[InstrumentCfg] = []
    for i, raw in enumerate(data.pop("instruments", []) or []):
        if not isinstance(raw, dict):
            problems.append(f"instruments[{i}] must be a table")
            continue
        obj = _build(InstrumentCfg, raw, f"instruments[{i}]", problems)
        if obj is not None:
            if obj.segment == "IDX_I" and obj.tradable:
                obj = dataclasses.replace(obj, tradable=False)
            instruments.append(obj)

    freeze = data.pop("freeze_qty", {}) or {}
    margin_factors = data.pop("margin_factors", None)
    for k in data:
        problems.append(f"{k}: unknown top-level section (typo?)")

    secrets = Secrets(
        client_id=env_values.get("DHAN_CLIENT_ID", "").strip(),
        access_token=env_values.get("DHAN_ACCESS_TOKEN", "").strip(),
        pin=env_values.get("DHAN_PIN", "").strip(),
        totp_secret=env_values.get("DHAN_TOTP_SECRET", "").strip().replace(" ", ""),
        app_id=env_values.get("DHAN_APP_ID", "").strip(),
        app_secret=env_values.get("DHAN_APP_SECRET", "").strip(),
        telegram_token=env_values.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=env_values.get("TELEGRAM_CHAT_ID", "").strip(),
    )

    kwargs: dict[str, Any] = dict(sections)
    kwargs.update(instruments=tuple(instruments), freeze_qty={str(k).upper(): int(v) for k, v in freeze.items()},
                  secrets=secrets, base_dir=str(base_dir))
    if margin_factors is not None:
        kwargs["margin_factors"] = {str(k): float(v) for k, v in margin_factors.items()}
    cfg = AppConfig(**kwargs) if not problems else None
    if cfg is not None:
        problems.extend(validate(cfg, live_requested=live_requested))
    if problems:
        raise ConfigError(problems)
    return cfg


def _apply_session_profile(raw: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    profile = raw.get("profile", "nse")
    if profile not in _SESSION_PROFILES:
        problems.append(f"session.profile: must be one of {sorted(_SESSION_PROFILES)}, got {profile!r}")
        profile = "nse"
    merged = dict(_SESSION_PROFILES[profile])
    merged.update({k: v for k, v in raw.items() if v != ""})
    merged["profile"] = profile
    return merged


def validate(cfg: AppConfig, live_requested: bool = False) -> list[str]:
    p: list[str] = []
    if cfg.mode.mode not in ("paper", "live"):
        p.append("mode.mode: must be 'paper' or 'live'")
    if cfg.dhan.auth not in ("token", "totp", "oauth"):
        p.append("dhan.auth: must be token | totp | oauth")
    if not (1 <= len(cfg.dhan.cid_prefix) <= 4) or not cfg.dhan.cid_prefix.isalnum():
        p.append("dhan.cid_prefix: 1-4 alphanumeric characters")

    times = cfg.session_times()
    parsed: dict[str, tuple[int, int, int]] = {}
    for k, v in times.items():
        try:
            parsed[k] = parse_hhmm(v)
        except ValueError as e:
            p.append(f"session.{k}: {e}")
    if len(parsed) == len(times):
        order = ["preflight", "feed_connect", "market_open", "entry_start", "entry_end", "squareoff_time", "session_end"]
        for a, b in zip(order, order[1:]):
            if parsed[a] > parsed[b]:
                p.append(f"session.{a} ({times[a]}) must not be after session.{b} ({times[b]})")

    if cfg.feed.ltt_mode not in ("auto", "utc", "ist_shifted"):
        p.append("feed.ltt_mode: auto | utc | ist_shifted")
    if cfg.feed.stale_after_s <= 0:
        p.append("feed.stale_after_s must be > 0")

    ex = cfg.execution
    if ex.default_order_type not in ("LIMIT", "MARKET"):
        p.append("execution.default_order_type: LIMIT | MARKET")
    if ex.validity not in ("DAY", "IOC"):
        p.append("execution.validity: DAY | IOC")
    if ex.exit_mode not in ("super_order", "bot_managed", "none"):
        p.append("execution.exit_mode: super_order | bot_managed | none")
    if ex.flatten_scope not in ("bot", "all"):
        p.append("execution.flatten_scope: bot | all")
    for k in ("entry_buffer_ticks", "exit_buffer_ticks", "sl_limit_buffer_ticks", "squareoff_max_reprices"):
        if getattr(ex, k) < 0:
            p.append(f"execution.{k} must be >= 0")
    if ex.order_timeout_ms < 100:
        p.append("execution.order_timeout_ms must be >= 100")

    r = cfg.risk
    if r.max_daily_loss_inr <= 0:
        p.append("risk.max_daily_loss_inr must be > 0 (a daily loss limit is mandatory)")
    for k in ("max_order_value_inr", "max_gross_exposure_inr"):
        if getattr(r, k) <= 0:
            p.append(f"risk.{k} must be > 0")
    for k in ("max_open_positions", "max_orders_per_day", "max_trades_per_instrument", "max_consecutive_losses",
              "reject_storm_count"):
        if getattr(r, k) < 1:
            p.append(f"risk.{k} must be >= 1")
    if not 0 < r.price_band_pct <= 20 or not 0 < r.price_band_options_pct <= 50:
        p.append("risk.price_band_pct must be in (0, 20] and price_band_options_pct in (0, 50]")
    if r.pnl_exit_backstop_factor < 1.0:
        p.append("risk.pnl_exit_backstop_factor must be >= 1.0 (backstop must be wider than the bot's own limit)")

    rl = cfg.rate_limits
    if not 1 <= rl.orders_per_sec <= HARD_MAX_ORDERS_PER_SEC:
        p.append(f"rate_limits.orders_per_sec must be 1..{HARD_MAX_ORDERS_PER_SEC} (SEBI/Dhan ceiling)")
    if not 1 <= rl.orders_per_min <= HARD_MAX_ORDERS_PER_MIN:
        p.append(f"rate_limits.orders_per_min must be 1..{HARD_MAX_ORDERS_PER_MIN} (Dhan ceiling)")
    if rl.order_api_calls_per_day < 1:
        p.append("rate_limits.order_api_calls_per_day must be >= 1")
    if not 1 <= rl.non_trading_per_sec <= 20 or not 1 <= rl.data_per_sec <= 5 or rl.quote_per_sec != 1:
        p.append("rate_limits: non_trading_per_sec 1..20, data_per_sec 1..5, quote_per_sec must be 1")

    if cfg.instrument_master.tick_unit not in ("auto", "paise", "rupees"):
        p.append("instrument_master.tick_unit: auto | paise | rupees")
    for seg in cfg.instrument_master.extra_segments:
        if seg not in SEGMENT_TO_CODE:
            p.append(f"instrument_master.extra_segments: unknown segment {seg!r}")

    seen: set[str] = set()
    for inst in cfg.instruments:
        if inst.alias in seen:
            p.append(f"instruments: duplicate alias {inst.alias!r}")
        seen.add(inst.alias)
        if inst.segment not in SEGMENT_TO_CODE:
            p.append(f"instruments[{inst.alias}].segment: unknown {inst.segment!r} (one of {sorted(SEGMENT_TO_CODE)})")
        if not inst.trading_symbol and not inst.security_id:
            p.append(f"instruments[{inst.alias}]: set trading_symbol or security_id")
        if inst.mode not in MODE_CODES:
            p.append(f"instruments[{inst.alias}].mode: ticker | quote | full")
        if inst.product not in ("CNC", "INTRADAY", "MARGIN", "MTF"):
            p.append(f"instruments[{inst.alias}].product: CNC | INTRADAY | MARGIN | MTF")
        if inst.max_qty < 0:
            p.append(f"instruments[{inst.alias}].max_qty must be >= 0")

    if cfg.is_live or live_requested:
        s = cfg.secrets
        if not s.client_id:
            p.append("live mode: DHAN_CLIENT_ID missing (.env)")
        # dhan.auth='token' may also be satisfied by the cached token written by `dhanbot login`,
        # so a missing DHAN_ACCESS_TOKEN is checked at preflight, not here.
        if cfg.dhan.auth == "totp" and not (s.pin and s.totp_secret):
            p.append("dhan.auth='totp' needs DHAN_PIN and DHAN_TOTP_SECRET in .env")
        if cfg.dhan.auth == "oauth" and not (s.app_id and s.app_secret):
            p.append("dhan.auth='oauth' needs DHAN_APP_ID and DHAN_APP_SECRET in .env")
    if live_requested and not cfg.is_live:
        p.append("--live given but mode.mode is 'paper' in config: both must say live")
    if cfg.telegram.enabled and not (cfg.secrets.telegram_token and cfg.secrets.telegram_chat_id):
        p.append("telegram.enabled needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    return p
