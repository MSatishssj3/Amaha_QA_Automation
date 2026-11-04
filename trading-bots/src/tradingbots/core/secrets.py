"""Secrets come only from ``.env`` (next to ``config/``) and the process environment - never from the
settings store or the bus (except the short-lived Dhan access token, which the config bot shares
through the bus key-value store so other bots can connect)."""

from __future__ import annotations

import os
from pathlib import Path

from dhanbot.config import parse_env_file
from dhanbot.logging_setup import register_secrets

SECRET_KEYS = ("DHAN_ACCESS_TOKEN", "DHAN_PIN", "DHAN_TOTP_SECRET", "DHAN_APP_SECRET", "ANTHROPIC_API_KEY",
               "DASHBOARD_PASSWORD_HASH")


def load_env(base_dir: Path) -> dict[str, str]:
    env = parse_env_file(Path(base_dir) / ".env")
    env.update({k: v for k, v in os.environ.items() if k.startswith(("DHAN_", "ANTHROPIC_", "DASHBOARD_"))})
    register_secrets([env[k] for k in SECRET_KEYS if env.get(k)])
    return env
