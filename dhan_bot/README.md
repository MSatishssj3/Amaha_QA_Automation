# dhanbot: a low-latency trading framework for Dhan

`dhanbot` trades on your [Dhan](https://dhan.co) account through the official DhanHQ v2 API.
It ships as a **foundation without a strategy**: you write your trading rules later, as one small
Python file. The engine handles the rest:

* **Market data:** live binary feed, auto-reconnect, candles, indicators.
* **Execution:** marketable-limit orders, super orders with the stop-loss and target held at Dhan,
  slicing.
* **Order safety:** a state machine that never re-sends an order whose outcome is unknown.
  Correlation IDs, recovery after a crash or restart.
* **Risk:** daily loss limit, position and exposure caps, fat-finger guard, kill switch,
  automatic square-off, and Dhan's server-side P&L exit as a backstop.
* **Modes:** paper trading on the live feed, a full offline **simulator**, **replay/backtest** of
  recorded days, then live.
* **Operations:** journal, day reports, latency statistics, Telegram alerts and control, systemd
  deployment.

> ⚠️ **Risk warning.** Automated trading can lose money quickly. This software comes with no
> guarantee of profit or correctness. Run it in paper mode first, start live with the minimum size,
> and keep the loss limits tight. The example strategy exists only to show the API. It is **not**
> a recommendation.

---

## Contents

1. [How it works and why it is fast](#1-how-it-works-and-why-it-is-fast)
2. [Requirements](#2-requirements)
3. [Quick start: no Dhan account needed](#3-quick-start-no-dhan-account-needed)
4. [Connect your Dhan account (paper mode)](#4-connect-your-dhan-account-paper-mode)
5. [Going live checklist](#5-going-live-checklist)
6. [Writing a strategy](#6-writing-a-strategy)
7. [Risk controls](#7-risk-controls)
8. [Order handling details](#8-order-handling-details)
9. [Daily routine and monitoring](#9-daily-routine-and-monitoring)
10. [Command reference](#10-command-reference)
11. [Configuration reference](#11-configuration-reference)
12. [Latency](#12-latency)
13. [Troubleshooting](#13-troubleshooting)
14. [Verify on your first live day](#14-verify-on-your-first-live-day)
15. [Development and tests](#15-development-and-tests)

---

## 1. How it works and why it is fast

```
 Dhan feed (binary WS) ─► parse ─► market state ─► P&L marks ─► candles ─► strategy.on_tick/on_bar
                                                                                    │ ctx.buy()
                                     risk checks (in-memory, µs) ◄──────────────────┘
                                                    │
 Dhan REST (keep-alive, pre-warmed) ◄── order manager (rate gate, correlation id) ◄─┘
 Dhan order-update WS ─► fills ─► positions ─► P&L ─► risk (loss limit → HALT → flatten)
```

* The whole path from a WebSocket frame to an order on the wire is **synchronous Python**: one
  event loop, no threads and no queues in between. Measured in-process: parsing a full-depth packet
  takes about 5–9 µs, and the risk check plus order submit about 30–40 µs.
* Orders go out over **already-open TLS connections**, with payloads built from per-instrument
  templates. A keep-alive call every 20 s keeps the connections hot.
* **Network distance matters most.** Your order spends about 1–5 ms on the network from a Mumbai
  server, against roughly 20–80 ms from home broadband. Dhan caps orders at 10/second anyway, so
  a faster language would gain microseconds, not milliseconds. See
  [deploy/vps_setup.md](deploy/vps_setup.md).
* Extras: `uvloop`, `orjson`, the garbage collector frozen after startup, and an interpreter
  warm-up of the order path before the first trade.

## 2. Requirements

* A Dhan account with **DhanHQ API access** and an active **Data API** subscription. The live market
  feed refuses to connect without it (disconnect code 806).
* Python **3.11+** on Linux, macOS or Windows (Windows is fine for paper trading and development).
* For **live** orders: a **static public IP registered with Dhan**, required by SEBI's retail algo
  rules since 2026. A Mumbai VPS gives you one; see [deploy/vps_setup.md](deploy/vps_setup.md).
* Dhan's limits, which the bot enforces itself: 10 orders/s, 250/min, with modifications and
  cancels counted; at most 25 modifications per order.

## 3. Quick start: no Dhan account needed

```bash
cd dhan_bot
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
dhanbot init .                     # creates config.toml, .env, holidays.txt
dhanbot simulate --speed 120       # a whole trading day against a local fake Dhan in ~3 minutes
dhanbot replay --synthetic         # instant backtest on a synthetic day
```

`simulate` runs the **real live code path** (REST, both WebSockets, order manager, risk) against a
local mock of Dhan, so you can watch orders, fills, square-off and the day report without any risk.

## 4. Connect your Dhan account (paper mode)

1. On web.dhan.co, open **DhanHQ Trading APIs**. Generate an access token, or enable TOTP for
   automatic login.
2. Fill in `.env`:
   * `DHAN_CLIENT_ID`
   * either `DHAN_ACCESS_TOKEN` (valid about 24 h; `dhan.auth = "token"`)
   * or `DHAN_PIN` + `DHAN_TOTP_SECRET` (the bot logs in every morning; `dhan.auth = "totp"`)
3. Pick the instruments in `config.toml`. `dhanbot instruments search HDFC` finds IDs, and
   `dhanbot instruments option NIFTY --kind CE --spot 24000 --strike ATM` resolves options.
4. Run `dhanbot check`. This is **read-only**: it checks the token, data plan, static IP, funds,
   instruments, tick sizes, the live feed and the order-update socket.
5. Run `dhanbot run`. **Paper mode:** real market data, simulated fills, the same risk rules.
   Ticks are recorded to `data/ticks/` so you can replay the day later.

## 5. Going live checklist

* [ ] Several full days in paper mode with the exact strategy and config you will run live.
      `dhanbot report` looks right.
* [ ] Running on the server whose **static IP is registered** (`dhanbot ip show`; `dhanbot check`
      shows the static IP line `OK`).
* [ ] `dhanbot ping`: keep-alive p50 in single-digit milliseconds.
* [ ] Limits are small: `max_qty` of 1 share or 1 lot, a tight `max_daily_loss_inr`,
      `max_open_positions`.
* [ ] `execution.exit_mode = "super_order"`, so stops live at Dhan even if the server dies.
* [ ] In `config.toml` set `mode = "live"`, **and** start with `dhanbot run --live`. Both are
      required.
* [ ] On the first live day: watch it, and confirm the items in
      [section 14](#14-verify-on-your-first-live-day).

## 6. Writing a strategy

Copy [strategies/my_strategy.py](strategies/my_strategy.py), then point the config at it:

```toml
[strategy]
name = "strategies/my_strategy.py:MyStrategy"
[strategy.params]
symbol = "RELIANCE"
qty = 1
```

```python
from dhanbot.strategy import Strategy, EMA

class MyStrategy(Strategy):
    name = "my_strategy"
    params = {"symbol": "RELIANCE", "qty": 1}           # defaults; [strategy.params] overrides

    def subscriptions(self):                            # (symbols to stream, candles to build)
        return [self.p.symbol], [(self.p.symbol, "5m")]

    def on_start(self, ctx):                            # history is pre-loaded: indicators are warm
        self.ema = ctx.indicator(EMA, 20, on=(self.p.symbol, "5m"))

    def on_bar(self, ctx, bar):                         # a candle closed
        flat = ctx.position_qty(self.p.symbol) == 0
        if flat and ctx.can_enter() and bar.close > self.ema.value:
            ctx.buy(self.p.symbol, self.p.qty, sl=bar.close * 0.99, target=bar.close * 1.02)
        elif not flat and bar.close < self.ema.value:
            ctx.exit(self.p.symbol)
```

**Callbacks:**

| Callback | When it runs |
|---|---|
| `on_start` | Once, after startup checks and the history warm-up |
| `on_recover` | After a restart that left open positions |
| `on_tick` | Every price update (hot path) |
| `on_bar` | When a candle closes |
| `on_order` | On every order event (see below) |
| `on_timer` | At times registered with `ctx.schedule` |
| `on_stop` | Once, at shutdown |

`on_order` event kinds: `ack`, `fill`, `filled`, `cancelled`, `rejected`, `expired`, `failed`,
`unknown`, `modified` and `leg_update`.

**`ctx` methods:**

| Group | Methods |
|---|---|
| Orders | `buy` / `sell(symbol, qty, price=None, order_type=, sl=, target=, trail=, trigger=, product=, validity=, tag=)` · `exit(symbol)` · `exit_all()` · `cancel(order)` · `modify(order, price=, qty=, trigger=)` |
| Positions & P&L | `position(symbol)` · `position_qty(symbol)` · `positions()` · `pnl()` · `open_orders(symbol)` · `can_enter()` |
| Market data | `ltp` · `quote` (bid/ask/depth/volume/OI) · `bars(symbol, tf)` · `indicator(cls, ..., on=(symbol, tf))` · `history()` |
| Options | `option(underlying, "CE"/"PE", expiry="nearest", strike="ATM+1")` · `subscribe(instrument, mode, max_qty)` (stream and trade it at runtime) |
| Utilities | `schedule("14:30", name)` · `state` (a dict that survives same-day restarts) · `log` · `notify(text)` · `now()` · `run_in_executor(fn)` |

**Indicators** (O(1) per update): `EMA`, `SMA`, `RSI`, `ATR`, `VWAP`, `RollingMax`, `RollingMin`.

**Rules:**
* Never block in a callback.
* Keep `on_tick` tiny.
* If a callback raises an exception, the strategy is **disabled** for the day. Risk, stops and
  square-off keep running, and you get an alert.

**Test loop:**
1. `dhanbot replay --synthetic`
2. `dhanbot simulate`
3. `dhanbot run` (paper)
4. `dhanbot replay data/ticks/<day>.bin` (backtest on real recorded days)
5. Live.

## 7. Risk controls

These checks run on every order, identically in paper and live mode. **Exits are never blocked by
limits.** They are only *reduce-only*: an exit can never flip or increase a position.

| Rule | Default | Effect |
|---|---|---|
| Daily loss (net of estimated charges) | ₹2,000 | **HALT**: cancel everything, flatten, stop for the day |
| Daily profit lock | off | PAUSE new entries |
| Consecutive losing trades | 5 | PAUSE |
| Broker reject storm | 5 in 60 s | HALT |
| Max qty / order value / open positions / gross exposure | per config | reject the entry |
| Fat-finger price band | ±2% (options ±10%), within circuits | reject |
| Margin estimate vs available funds | on | reject the entry |
| Stale feed (no tick for 5 s) | on | reject the entry |
| One working (unfilled) entry per instrument | 1 | reject the entry (stops a buggy strategy firing on every tick) |
| Entry window, token about to expire, duplicate within 500 ms, daily order caps | on | reject the entry |

**Kill switch levels.**
* **PAUSED**: no new entries. Exits and stops still work. Lift it with `dhanbot resume`.
* **HALTED**: latched for the day. Triggered by a risk rule, `dhanbot killswitch`, Telegram `/kill`,
  or creating the file `data/KILL`. Optionally it also activates Dhan's own kill switch
  (`risk.broker_killswitch_on_halt`).

**Backstop.** In live mode the bot registers Dhan's server-side **P&L exit** at 1.25× your limits,
so Dhan squares you off even if the bot or the server dies.

**Plug-in checks.** Other programs that embed the engine can add their own entry rules with
`engine.risk.extra_checks.append(fn)`, where `fn(order, record)` returns a deny `Decision` or `None`.
The [`trading-bots`](https://github.com/MSatishssj3/trading-bots) daily budget gatekeeper uses this.
A check that raises an exception blocks the entry. Exits never consult plug-ins.

**One engine per data directory.** The engine takes an exclusive lock on `data/engine.lock`, so a
second copy (for example a stray `dhanbot run` next to the systemd service) refuses to start
instead of double-trading.

**Square-off.** At 15:10 (configurable) every INTRADAY position the bot opened is closed. This
happens before Dhan's own auto square-off, which charges a fee. If the bot is restarted after
15:10, it squares off immediately.

## 8. Order handling details

* **Entries** are **marketable limits**: best ask (for a buy) plus 2 ticks, clamped to the circuit
  limits. They fill like a market order but with capped slippage. An entry not filled within 3 s is
  re-priced once, then cancelled.
* **Stops and targets:**
  * `exit_mode = "super_order"` (default): one request creates the entry, target and stop at Dhan,
    optionally with a trailing stop.
  * `bot_managed`: an exchange stop-limit order plus a target watched by the bot. If the price gaps
    through the stop-limit, the bot chases it with a marketable exit.
* **Exits** first cancel the instrument's working orders and protective legs, and **wait for the
  cancellations to be confirmed**. Only then is the exit sent, so a stop filling at the same moment
  cannot double-exit. The exit is re-priced every 2 s, with a market order as a last resort.
* **Unknown outcome** (a timeout or a dropped connection): the order is looked up by its
  correlation ID. It is adopted if found, and marked FAILED if not. **Entries are never re-sent
  blindly.** Exits are re-sent once.
* **Crash recovery:** on startup, today's orders whose correlation IDs carry the bot's prefix
  (`dhan.cid_prefix`) are adopted, and positions in your configured instruments are taken from
  Dhan. Positions in other instruments are left alone. Don't trade the bot's instruments by hand.

## 9. Daily routine and monitoring

* **Automatic:** systemd starts the bot at 08:30 IST on weekdays. It runs the startup checks,
  trades, squares off, writes the day report and exits after 15:30. Holidays come from
  `holidays.txt`. **Keep that file updated from the NSE holiday list.**
* **Token:** with `auth = "totp"` there's nothing to do. Otherwise run `dhanbot login --token <TOKEN>`
  every morning.
* **Watch:** Telegram alerts and commands (`/status /pnl /positions /orders /pause /resume
  /flatten /kill`), `dhanbot status`, `logs/dhanbot.jsonl`.
* **After the close:** `dhanbot report`. Reports are in `data/reports/`, the journal in
  `data/journal.db` (SQLite), and recordings in `data/ticks/`.

## 10. Command reference

| Command | Purpose |
|---|---|
| `dhanbot init [dir]` | create `config.toml`, `.env`, `holidays.txt` |
| `dhanbot check` | read-only readiness check (fix every FAIL first) |
| `dhanbot login [--token T \| --oauth]` | create/cache today's access token |
| `dhanbot ip show \| set IP [--secondary] \| modify IP` | manage the static IP registered at Dhan (locked for 7 days after a change) |
| `dhanbot ping` | network latency to Dhan's API from this machine |
| `dhanbot instruments search TEXT` / `instruments option NIFTY --kind CE --spot 24000 --strike ATM` | look up instruments |
| `dhanbot run [--live]` | trade the session (paper unless both config and flag say live) |
| `dhanbot simulate [--speed 60] [--minutes N]` | whole bot against a local mock Dhan |
| `dhanbot replay FILE \| --synthetic` | deterministic backtest through the real engine |
| `dhanbot synth OUT.bin` | write a synthetic recording |
| `dhanbot status \| pause \| resume \| flatten \| killswitch \| stop` | control the running bot |
| `dhanbot report [--date]` | orders, fills, P&L and latency for a day |

Exit codes: `0` OK, `1` config error, `2` startup checks failed, `3` runtime error.

## 11. Configuration reference

All settings, with comments, are in [config.example.toml](config.example.toml). Unknown or
misspelled keys are **errors**, and every problem is reported at once. The ones you'll use most:

| Setting | Meaning |
|---|---|
| `mode.mode` | `paper` / `live` |
| `dhan.auth` | `token` / `totp` / `oauth` |
| `[[instruments]]` | alias, segment, trading_symbol or security_id, mode (`ticker`/`quote`/`full`), product, `max_qty` |
| `risk.*` | loss limit, exposure, positions, price band, reject storm, P&L-exit backstop |
| `execution.*` | order type, buffers, entry TTL, `exit_mode`, square-off re-pricing, `flatten_on_shutdown` |
| `session.*` | `profile = "nse" \| "mcx"`, entry window, square-off time |
| `feed.record_ticks` | record raw frames for replay |
| `telegram.enabled` | alerts and remote control |

## 12. Latency

The bot measures itself and logs p50/p95/p99 every minute. The numbers also appear in the day
report and `dhanbot report`.

| Stage | What it measures | Typical |
|---|---|---|
| `frame_parse` | WebSocket frame → packets | 5–25 µs |
| `strategy_on_tick` | your `on_tick` | your code |
| `tick_to_submit` / `tick_to_send` | frame received → order handed to the HTTP client | tens to a few hundred µs |
| `order_send_to_ack` | REST round trip incl. Dhan's OMS | network + Dhan (ms) |
| `order_send_to_fill` | to the first fill report | + exchange |

## 13. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `DH-901` / feed disconnect `807` | Access token expired. Use `dhanbot login`, or `auth = "totp"` |
| Feed disconnect `806` | No active Data API subscription on the account |
| Feed disconnect `805` | Too many WebSocket connections (another bot or app is using your quota) |
| `808` / `809` | Wrong client id / authentication failed |
| Orders rejected, mentioning the IP | This host is not the registered static IP. Check `dhanbot ip show` and `dhanbot check`. The bot HALTs on this |
| `DH-904` / HTTP 429 | Rate limit. The bot backs off; exits are retried, entries are rejected |
| "rate not within ckt limit" | Price outside the circuit. Limits are fetched at startup; restart if circuits changed intraday |
| `stale_feed` rejections | No ticks for the instrument (illiquid, feed down, or market closed) |
| `strategy disabled after an exception` | A bug in your strategy. See the traceback in the log; reproduce it with `dhanbot replay` |
| `instrument CSV format changed` | Dhan changed its scrip-master columns. Update `instruments.py` before trading |

## 14. Verify on your first live day

The bot was built without network access to Dhan, so its protocol details come from the official
`dhanhq` 2.3.0 SDK source. The binary feed parser is tested byte-for-byte against the SDK. A few
things could not be confirmed offline. Each is handled defensively, but check them once:

1. **Last-trade-time (LTT) encoding:** the `ltt_mode` line in `dhanbot check` (auto-detected).
2. **Tick sizes:** `dhanbot check` prints `tick=₹…` for each instrument. Confirm they look right
   (for example ₹0.05 or ₹0.01 for equities). If not, set `instrument_master.tick_unit`.
3. **Order-update field names:** the first raw order-update message of the day is logged and stored
   in the journal (`events`, kind `order_update_sample`). Fills must appear in `dhanbot report`.
4. **Super orders** are accepted for your segment/product. If Dhan rejects them, set
   `exit_mode = "bot_managed"`.
5. **Error codes** for IP and margin rejects: check the rejection reason text in the log.
6. **Freeze quantities:** set `[freeze_qty]` for the F&O underlyings you trade.
7. **Holidays:** `holidays.txt` matches this year's NSE list.

## 15. Development and tests

```bash
pip install -e ".[dev]"
pytest                 # ~150 tests, fully offline (mock Dhan server, SDK parity, E2E live/paper/replay)
```

Layout:

```
src/dhanbot/
  dhan/        REST client, auth/token, binary feed + parser, order-update socket, normaliser
  broker/      live (Dhan REST) and paper (simulated) brokers
  oms.py       order state machine          risk.py     pre-trade checks + kill switch
  engine.py    composition root / hot path  positions.py P&L     instruments.py scrip master
  bars.py indicators.py market_state.py execution.py scheduler.py reconcile.py
  journal.py recorder.py replay.py latency.py control.py checks.py cli.py notify/telegram.py
  strategy/    Strategy base, Context API, loader, examples
  sim/         mock Dhan server (tests + `dhanbot simulate`)
strategies/    your strategies (template included)
deploy/        systemd units + VPS guide
```

**Security:**
* Secrets live only in `.env` and `data/token.json` (mode 600), and are redacted from every log line.
* The control channel listens on localhost only and is protected by a token.
* Telegram obeys only your chat ID, and `/flatten` and `/kill` need a `/confirm`.
