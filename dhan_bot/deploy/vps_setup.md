# Deploying dhanbot on a Mumbai VPS (AWS `ap-south-1`)

Why a VPS in Mumbai:

* **Static IP is mandatory.** Since SEBI's retail algo framework (2026), Dhan only accepts order API
  calls from a static IP registered on your account. Market data works from anywhere.
* **Latency.** A server in Mumbai reaches Dhan's API in about 1–5 ms, against roughly 20–80 ms from
  home broadband. That is the single biggest speed gain available. Measure it with `dhanbot ping`.
* **Reliability.** It runs every trading day without your PC being on.

Approximate cost: `t4g.small` plus an Elastic IP and 20 GB of disk comes to about ₹1,500/month.
Check current AWS pricing; this figure was not verified. Any provider with a Mumbai region and a
static IP works the same way (Google Cloud `asia-south1`, Azure Central India, ...).

---

## 1. Create the server

1. In the AWS console, choose the **Asia Pacific (Mumbai) ap-south-1** region.
2. EC2 → Launch instance:
   * Image: **Ubuntu Server 24.04 LTS (arm64)**
   * Type: **t4g.small** (2 vCPU / 2 GB). `t4g.micro` also works for one strategy on a few instruments.
   * Key pair: create one and download the `.pem` file.
   * Network / security group: allow inbound **SSH (22) from your IP only**. Nothing else is needed:
     Telegram uses outbound polling.
   * Storage: 20 GB gp3.
3. EC2 → **Elastic IPs** → Allocate → Associate with the instance. **This address is your static IP.**
4. SSH in: `ssh -i key.pem ubuntu@<ELASTIC_IP>`

## 2. Prepare the OS

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3 python3-venv python3-pip git chrony ufw unattended-upgrades
sudo timedatectl set-timezone Asia/Kolkata
sudo systemctl enable --now chrony          # accurate clock (orders, logs, LTT detection)
sudo ufw default deny incoming && sudo ufw allow OpenSSH && sudo ufw --force enable
sudo dpkg-reconfigure -plow unattended-upgrades
sudo adduser --disabled-password --gecos "" dhanbot
```

## 3. Install the bot

```bash
sudo -iu dhanbot
git clone <your repo url> src && mkdir -p bot && cd bot
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ../src/dhan_bot        # installs `dhanbot`; `git pull` in ../src updates it
cp -r ../src/dhan_bot/strategies .              # your strategy files live here
.venv/bin/dhanbot init .                        # creates config.toml, .env, holidays.txt
chmod 600 .env
```

Edit `.env` (client id, plus a token or PIN+TOTP) and `config.toml` (instruments, risk limits,
strategy). Keep `mode = "paper"` for now.

## 4. Register the static IP with Dhan

The IP is locked for 7 days after it is set or changed, so double-check it first.

```bash
curl -s https://api.ipify.org                   # must equal your Elastic IP
.venv/bin/dhanbot ip set <ELASTIC_IP>           # or: web.dhan.co -> DhanHQ Trading APIs -> Static IP
.venv/bin/dhanbot ip show
```

Optionally register your home or office static IP as `--secondary`.

## 5. Check everything (read-only)

```bash
.venv/bin/dhanbot ping                          # expect a keep-alive p50 of a few ms
.venv/bin/dhanbot check                         # token, data plan, static IP, instruments, feed, LTT mode
```

Fix every `FAIL` line before going further.

## 6. Install the systemd units

```bash
exit   # back to the ubuntu user
sudo cp /home/dhanbot/src/dhan_bot/deploy/dhanbot*.service /home/dhanbot/src/dhan_bot/deploy/dhanbot*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dhanbot.timer dhanbot-stop.timer
systemctl list-timers | grep dhanbot           # next start: weekday 08:30 IST
```

Run it once by hand to confirm it starts: `sudo systemctl start dhanbot && journalctl -u dhanbot -f`.
Outside market hours it exits ("session already over" / holiday), which is expected.

## 7. Daily operation

* **Token.** With `dhan.auth = "totp"` the bot logs in by itself every morning. With
  `dhan.auth = "token"`, paste a fresh token each day before 08:30:
  `sudo -iu dhanbot bot/.venv/bin/dhanbot login --token <TOKEN> -c bot/config.toml`
* **Watch it:** Telegram alerts, or `dhanbot status -c config.toml`, or
  `journalctl -u dhanbot -f`, or `logs/dhanbot.jsonl`.
* **Intervene:** `dhanbot pause` / `resume` / `flatten` / `killswitch` (or the same commands on
  Telegram). `touch data/KILL` also halts within a second.
* **After the close:** `dhanbot report -c config.toml`. Recorded ticks are in `data/ticks/` for
  `dhanbot replay`.

## 8. Going live (only after several clean paper days)

1. In `config.toml`, set `mode = "live"` and tight limits (`max_daily_loss_inr`, `max_qty` = 1 lot or
   1 share).
2. Edit `/etc/systemd/system/dhanbot.service`: switch to the `ExecStart` line with `--live`, then
   run `sudo systemctl daemon-reload`.
3. The first live day: watch it. Confirm the server-side P&L exit shows in `dhanbot check`
   (`pnl_exit` line) and that the first super order is accepted.

## Hardening checklist

* SSH key login only (the Ubuntu cloud image default), security group restricted to your IP.
* `.env`, `data/token.json` and `data/control.json` are mode 600. Never commit them.
* Storing `DHAN_PIN` + `DHAN_TOTP_SECRET` on the server is what makes auto-login possible. If the
  server is compromised, so is your Dhan login. The alternative is `auth = "token"` and pasting a
  token daily.
* Keep the OS patched (unattended-upgrades) and the clock synced (chrony).
