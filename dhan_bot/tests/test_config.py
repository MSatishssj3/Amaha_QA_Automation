from __future__ import annotations

from pathlib import Path

import pytest

from dhanbot.config import AppConfig, load_config, parse_env_file
from dhanbot.errors import ConfigError
from dhanbot.logging_setup import redact, register_secrets

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


def test_defaults_are_valid():
    cfg = load_config(data={}, env={})
    assert isinstance(cfg, AppConfig)
    assert cfg.mode.mode == "paper"
    assert cfg.session.entry_start == "09:15" and cfg.session.squareoff_time == "15:10"


def test_example_config_loads():
    cfg = load_config(EXAMPLE, env={})
    assert cfg.instruments, "example config should define instruments"
    assert any(i.segment == "IDX_I" and not i.tradable for i in cfg.instruments)


def test_unknown_keys_are_errors():
    with pytest.raises(ConfigError) as e:
        load_config(data={"risk": {"max_daly_loss_inr": 100}}, env={})
    assert "max_daly_loss_inr" in str(e.value)
    with pytest.raises(ConfigError):
        load_config(data={"riskk": {}}, env={})


def test_type_errors_and_all_problems_reported():
    with pytest.raises(ConfigError) as e:
        load_config(data={"risk": {"max_open_positions": "three", "max_daily_loss_inr": -5}}, env={})
    assert "max_open_positions" in str(e.value)


def test_validation_rules():
    with pytest.raises(ConfigError) as e:
        load_config(data={"rate_limits": {"orders_per_sec": 25}}, env={})
    assert "ceiling" in str(e.value)
    with pytest.raises(ConfigError) as e:
        load_config(data={"risk": {"max_daily_loss_inr": 0}}, env={})
    assert "mandatory" in str(e.value)
    with pytest.raises(ConfigError) as e:
        load_config(data={"session": {"entry_start": "15:20"}}, env={})
    assert "entry_start" in str(e.value)


def test_live_requires_both_flag_and_config():
    with pytest.raises(ConfigError) as e:
        load_config(data={}, env={}, live_requested=True)
    assert "both must say live" in str(e.value)
    with pytest.raises(ConfigError) as e:
        load_config(data={"mode": {"mode": "live"}, "dhan": {"auth": "totp"}}, env={}, live_requested=True)
    assert "DHAN_CLIENT_ID" in str(e.value) and "DHAN_PIN" in str(e.value)
    cfg = load_config(data={"mode": {"mode": "live"}}, env={"DHAN_CLIENT_ID": "1000000001"}, live_requested=True)
    assert cfg.is_live


def test_mcx_profile():
    cfg = load_config(data={"session": {"profile": "mcx"}}, env={})
    assert cfg.session.session_end == "23:30" and cfg.session.market_open == "09:00"


def test_env_file_and_secrets_redaction(tmp_path):
    ef = tmp_path / ".env"
    ef.write_text('# c\nDHAN_CLIENT_ID=123\nexport DHAN_ACCESS_TOKEN="eyJabc.def.ghi"\nDHAN_PIN=\'4321\'\n')
    assert parse_env_file(ef) == {"DHAN_CLIENT_ID": "123", "DHAN_ACCESS_TOKEN": "eyJabc.def.ghi", "DHAN_PIN": "4321"}
    cfg = load_config(data={}, env={}, env_file=ef)
    assert cfg.secrets.client_id == "123" and cfg.secrets.pin == "4321"
    assert "4321" not in repr(cfg.secrets) and "eyJabc" not in repr(cfg.secrets)
    register_secrets(cfg.secrets.values())
    assert "4321" not in redact("login pin 4321 ok")
    assert "eyJabc.def.ghi" not in redact("wss://x?version=2&token=eyJabc.def.ghi&clientId=1")
    assert "SECRET" not in redact("url?token=SECRET&x=1")


def test_index_instrument_forced_non_tradable():
    cfg = load_config(data={"instruments": [{"alias": "NIFTY", "segment": "IDX_I", "security_id": 13}]}, env={})
    assert cfg.instruments[0].tradable is False
