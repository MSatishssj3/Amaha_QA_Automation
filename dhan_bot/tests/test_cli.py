"""CLI commands end to end (each runs its own event loop, so these tests are synchronous)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from dhanbot.cli import main
from dhanbot.sim.mock_dhan import MockDhan

ROOT = Path(__file__).resolve().parents[1]


def write_config(d: Path, extra: str = "", env: str = "") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(f"""
[mode]
mode = "paper"
[paths]
data_dir = "data"
log_dir = "logs"
[perf]
gc_freeze = false
[[instruments]]
alias = "RELIANCE"
segment = "NSE_EQ"
trading_symbol = "RELIANCE"
mode = "full"
max_qty = 10
[[instruments]]
alias = "NIFTY"
segment = "IDX_I"
security_id = 13
mode = "ticker"
[strategy]
name = "ema_cross"
[strategy.params]
symbol = "RELIANCE"
fast = 5
slow = 13
{extra}
""")
    (d / ".env").write_text(env)
    return d / "config.toml"


@pytest.fixture
def thread_mock():
    loop = asyncio.new_event_loop()
    mock = MockDhan()
    mock.ips["primaryIP"] = "203.0.113.10"
    started = threading.Event()

    def runner():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(mock.start())
        started.set()
        loop.run_forever()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    started.wait(5)
    yield mock
    asyncio.run_coroutine_threadsafe(mock.stop(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)


def endpoints_toml(mock: MockDhan) -> str:
    ep = mock.endpoints()
    return (f'[endpoints]\nrest_base = "{ep.rest_base}"\nauth_base = "{ep.auth_base}"\nfeed_url = "{ep.feed_url}"\n'
            f'order_ws_url = "{ep.order_ws_url}"\ninstruments_csv_url = "{ep.instruments_csv_url}"\n')


def test_init_creates_files(tmp_path, capsys):
    assert main(["init", str(tmp_path)]) == 0
    assert (tmp_path / "config.toml").exists() and (tmp_path / ".env").exists() and (tmp_path / "holidays.txt").exists()
    assert main(["init", str(tmp_path)]) == 0
    assert "exists - left unchanged" in capsys.readouterr().out
    # the generated config is valid
    assert main(["status", "--config", str(tmp_path / "config.toml")]) == 3  # no bot running


def test_bad_config_exit_code(tmp_path, capsys):
    cfg = write_config(tmp_path, extra="[risk]\nmax_daly_loss_inr = 5\n")
    assert main(["report", "--config", str(cfg)]) == 1
    assert "max_daly_loss_inr" in capsys.readouterr().err


def test_synth_replay_and_report(tmp_path, capsys):
    cfg = write_config(tmp_path)
    out = tmp_path / "day.bin"
    assert main(["synth", str(out), "--symbols", "RELIANCE,NIFTY", "--minutes", "120", "--date", "2026-11-04"]) == 0
    assert out.stat().st_size > 10000
    from dhanbot.replay import synth_instrument_store  # replaying a file needs a matching instrument master
    from datetime import date

    csv = tmp_path / "master.csv"
    m = MockDhan.__new__(MockDhan)
    from dhanbot.sim.mock_dhan import default_instruments

    m.instruments = default_instruments(date(2026, 11, 4))
    csv.write_text(MockDhan.instrument_csv(m))
    assert main(["replay", str(out), "--config", str(cfg), "--csv", str(csv)]) == 0
    text = capsys.readouterr().out
    assert "Day report" in text and "trades" in text
    assert main(["replay", "--synthetic", "--config", str(cfg), "--minutes", "60", "--date", "2026-11-04"]) == 0
    rcfg = write_config(tmp_path / "r", extra="", env="")
    # point report at the replay journal
    (tmp_path / "r" / "config.toml").write_text((tmp_path / "config.toml").read_text().replace(
        'data_dir = "data"', f'data_dir = "{tmp_path / "data" / "replay"}"'))
    assert main(["report", "--config", str(rcfg), "--date", "2026-11-04"]) == 0
    assert "orders" in capsys.readouterr().out


def test_instruments_search_and_option_with_local_csv(tmp_path, capsys):
    cfg = write_config(tmp_path)
    m = MockDhan.__new__(MockDhan)
    from datetime import date

    from dhanbot.sim.mock_dhan import default_instruments

    m.instruments = default_instruments(date.today())
    csv = tmp_path / "m.csv"
    csv.write_text(MockDhan.instrument_csv(m))
    assert main(["instruments", "search", "INFY", "--config", str(cfg), "--csv", str(csv)]) == 0
    assert "1594" in capsys.readouterr().out
    assert main(["instruments", "option", "NIFTY", "--kind", "PE", "--spot", "24010", "--strike", "ATM+1",
                 "--config", str(cfg), "--csv", str(csv)]) == 0
    out = capsys.readouterr().out
    assert "24050" in out and "PE" in out


def test_killswitch_fallback_creates_kill_file(tmp_path, capsys):
    cfg = write_config(tmp_path)
    assert main(["killswitch", "--config", str(cfg)]) == 0
    assert (tmp_path / "data" / "KILL").exists()


def test_simulate_runs_the_live_code_path(tmp_path, capsys):
    cfg = write_config(tmp_path, extra='[session]\nentry_start = "09:15"\n')
    assert main(["simulate", "--config", str(cfg), "--speed", "300", "--minutes", "25", "--log-level", "WARNING"]) == 0
    out = capsys.readouterr().out
    assert "Day report" in out and "[live]" in out
    assert (tmp_path / "data" / "simulate" / "journal.db").exists()


def test_check_and_ip_and_ping_against_mock(tmp_path, capsys, thread_mock):
    cfg = write_config(tmp_path, extra=endpoints_toml(thread_mock),
                       env=f"DHAN_CLIENT_ID={thread_mock.client_id}\nDHAN_ACCESS_TOKEN={thread_mock.token}\n")
    rc = main(["check", "--config", str(cfg), "--feed-seconds", "2.5"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "READY" in out and "market_feed" in out and "IST-shifted" in out and "RELIANCE" in out
    assert main(["ip", "show", "--config", str(cfg)]) == 0
    assert "203.0.113.10" in capsys.readouterr().out
    assert main(["ip", "set", "198.51.100.7", "--secondary", "--config", str(cfg)]) == 0
    assert thread_mock.ips["secondaryIP"] == "198.51.100.7"
    assert main(["ping", "--config", str(cfg), "-n", "5"]) == 0
    assert "HTTPS keep-alive" in capsys.readouterr().out
    assert main(["login", "--token", thread_mock.token, "--config", str(cfg)]) == 0
    assert (tmp_path / "data" / "token.json").exists()
