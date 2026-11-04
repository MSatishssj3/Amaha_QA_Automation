# Running trading-bots on a server (VPS)

This runs the whole team around the clock on a small Linux server in Mumbai. It is the same server
`dhan_bot` uses, and the bots replace dhan_bot's own service.

## 1. The server

- **Region:** Mumbai (AWS `ap-south-1`, or any Mumbai provider). The closer to the exchange, the
  lower the latency.
- **Size:** 2 vCPU / **4 GB RAM** (for example AWS `t4g.medium`). The team runs about 10 Python
  processes plus the dashboard. A 2 GB machine is tight.
- **Static public IP:** Dhan only accepts orders from the IP registered on your account. Attach an
  Elastic IP (AWS) or the provider's equivalent, then register it in Dhan (web.dhan.co → API
  settings, or `dhanbot ip set <ip>` from dhan_bot).
- **OS:** Ubuntu 24.04 LTS (any Linux with Python 3.11 or newer works).
- **Clock:** keep NTP on (the default). The bots schedule everything in IST.

## 2. Install

```bash
sudo adduser --disabled-password --gecos "" trader
sudo apt update && sudo apt install -y python3-venv python3-pip git
sudo mkdir -p /opt/trading-bots && sudo chown trader:trader /opt/trading-bots
sudo -iu trader
git clone https://github.com/MSatishssj3/trading-bots /opt/trading-bots
cd /opt/trading-bots
python3 -m venv .venv
.venv/bin/pip install -e ".[redis]"
.venv/bin/bots init            # config/settings.toml, config/dhanbot.toml, config/holidays.txt, .env
.venv/bin/bots set-password    # the dashboard password (stored as a scrypt hash in .env)
chmod 600 .env
```

Then fill in:

- **`.env`:** `DHAN_CLIENT_ID`, plus either a daily `DHAN_ACCESS_TOKEN` (or paste one on the
  dashboard) or `DHAN_PIN` and `DHAN_TOTP_SECRET` for automatic login. Add `ANTHROPIC_API_KEY` to
  enable the AI analyst.
- **`config/settings.toml`:**
  - `platform.name = "dhan"`
  - your watchlist
  - the `[budget]` limits
- **`config/holidays.txt`:** this year's NSE holidays, from nseindia.com.
- **`config/dhanbot.toml`** (only to trade with your strategy): your instruments and strategy. Then
  set `execution.enabled = true`. It stays in **paper** mode until you also set `execution.live =
  true` **and** `mode = "live"` in `dhanbot.toml`.

## 3. Check before running

```bash
.venv/bin/bots check            # read-only: settings, bus, Dhan token + data plan, AI key, password
.venv/bin/bots check --login    # with dhan_auth = "totp": also performs the daily login
.venv/bin/bots ai test          # one tiny Claude call (a fraction of a cent)
```

## 4. Run as a service

```bash
# if dhan_bot ran as its own service before, stop it: two engines on one account double-trade
sudo systemctl disable --now dhanbot 2>/dev/null || true
sudo cp deploy/trading-bots.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bots
journalctl -u trading-bots -f       # alerts and restarts
.venv/bin/bots status               # every bot's health
```

Each bot logs to `logs/<bot>.jsonl`, with tokens and keys redacted. Crash tracebacks from before a
bot's logging started go to `logs/<bot>.stderr.log`.

**Optional: Redis bus.** The default SQLite bus needs nothing extra. For large watchlists:

```bash
sudo apt install -y redis-server
sudo cp deploy/redis.conf /etc/redis/redis.conf
sudo systemctl restart redis-server
```

Then set `system.bus_backend = "redis"` and restart the service.

## 5. The dashboard

The dashboard listens on `127.0.0.1:8501` only. Reach it through an SSH tunnel from your computer:

```bash
ssh -N -L 8501:127.0.0.1:8501 trader@<server-ip>
```

Then open http://127.0.0.1:8501 and sign in. Don't open port 8501 in the firewall. If you must
expose it, put it behind a reverse proxy with HTTPS and keep the password long.

## 6. Day to day

- **Token:** with `dhan_auth = "token"`, paste the new token each day on the dashboard (Settings →
  Dhan token). With `"totp"` the config bot logs in by itself.
- **Updates:** `git pull && .venv/bin/pip install -e ".[redis]" && sudo systemctl restart
  trading-bots`. The trading engine flattens intraday positions on a graceful stop, so restart
  outside market hours.
- **Backups:** back up `config/`, `.env` and `data/`. `data/` holds the tables, the settings
  history, budget history, expenses and AI reports.
- **Disk:** the tables grow by a few MB a day for a 25-stock watchlist. The bus keeps 48 hours
  (`system.bus_retention_hours`).

## Docker instead

`docker compose up -d --build` in the project folder runs the team with a Redis bus. The dashboard
is published on `127.0.0.1:8501`; use the same SSH tunnel. Prepare `config/` and `.env` first
(`bots init` / `bots set-password` on any machine, or copy the templates). The container user is
uid 1000, so the project folder must be writable by it.
