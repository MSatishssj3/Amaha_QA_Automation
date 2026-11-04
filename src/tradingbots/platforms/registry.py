"""Pick the adapter for the configured platform. To add a broker: implement ``Platform`` in a new
module and register its name here (and in ``PlatformCfg.name``)."""

from __future__ import annotations

from pathlib import Path

from dhanbot.clock import RealClock
from dhanbot.dhan.endpoints import Endpoints

from .base import Platform, PlatformError


def endpoints_from(info: dict | None) -> Endpoints:
    """``info`` is the token record (``secret:token``); simulated sessions carry their own endpoints."""
    ep = (info or {}).get("endpoints")
    return Endpoints(**ep) if ep else Endpoints()


def make_platform(name: str, token_info: dict, data_dir: Path, clock: RealClock | None = None,
                  segments: set[str] | None = None) -> Platform:
    if not token_info or not token_info.get("token"):
        raise PlatformError(f"no access token for platform {name!r} yet")
    if name in ("dhan", "simulated"):
        from .dhan import DhanPlatform

        return DhanPlatform(name, endpoints_from(token_info), token_info["client_id"], token_info["token"],
                            data_dir, clock, segments=segments)
    raise PlatformError(f"unknown platform {name!r}")
