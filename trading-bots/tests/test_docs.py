"""The generated reference docs match the code, and the shipped templates are valid."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from dhanbot.config import load_config

from tradingbots.core.settings import Settings, load_settings_file

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "tradingbots" / "templates"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_docs", ROOT / "scripts" / "gen_docs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_reference_docs_are_up_to_date():
    gen = _gen()
    assert (ROOT / "docs" / "tables.md").read_text(encoding="utf-8") == gen.tables_md(), "run scripts/gen_docs.py"
    assert (ROOT / "docs" / "messages.md").read_text(encoding="utf-8") == gen.messages_md(), "run scripts/gen_docs.py"


def test_templates_are_valid_and_match_the_defaults():
    assert load_settings_file(TEMPLATES / "settings.example.toml") == Settings()
    assert load_settings_file(ROOT / "config" / "settings.example.toml") == Settings()
    cfg = load_config(TEMPLATES / "dhanbot.example.toml", env={"DHAN_CLIENT_ID": "1", "DHAN_ACCESS_TOKEN": "x"})
    assert cfg.mode.mode == "paper" and cfg.strategy.name == "ema_cross"  # never live by default
    env = (TEMPLATES / "env.example").read_text(encoding="utf-8")
    for key in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "ANTHROPIC_API_KEY", "DASHBOARD_PASSWORD_HASH"):
        assert f"\n{key}=\n" in env  # present and empty: no secret ever ships
    for f in ("settings.example.toml", "dhanbot.example.toml", "holidays.txt"):
        assert (TEMPLATES / f).read_text(encoding="utf-8") == (ROOT / "config" / f).read_text(encoding="utf-8")
