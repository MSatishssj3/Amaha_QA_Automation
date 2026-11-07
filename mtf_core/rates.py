"""Broker MTF interest-rate slabs.

The bundled rates live in rates.json next to this file. Other bots can:
    - read them:        PLATFORMS["Dhan"], interest_rate(5_00_000, "Dhan")
    - add a broker:     register_platform("Angel One", [(None, 14.99)])
    - load newer rates: load_rates("my_rates.json")
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .models import Slab

SlabLike = Union[Slab, Tuple[Optional[float], float], dict]

PLATFORMS: Dict[str, List[Slab]] = {}
DEFAULT_PLATFORM = "Pocketful"
RATES_CHECKED = ""
RATE_SOURCES: Dict[str, str] = {}


def _to_slab(s: SlabLike) -> Slab:
    if isinstance(s, Slab):
        return s
    if isinstance(s, dict):
        upto, rate = s.get("upto"), s["rate"]
    else:
        upto, rate = s
    if upto is not None and upto == float("inf"):
        upto = None
    return Slab(None if upto is None else float(upto), float(rate))


def _validate(name: str, slabs: Sequence[Slab]) -> None:
    if not slabs:
        raise ValueError(f"{name}: needs at least one slab")
    if slabs[-1].upto is not None:
        raise ValueError(f"{name}: the last slab must have no upper limit (upto=None)")
    bounds = [s.upto for s in slabs[:-1]]
    if any(b is None for b in bounds) or bounds != sorted(bounds):
        raise ValueError(f"{name}: slab limits must increase and only the last may be open")


def register_platform(name: str, slabs: Iterable[SlabLike]) -> None:
    """Add or replace a platform. Slabs: [(upto, rate), ..., (None, rate)]."""
    parsed = [_to_slab(s) for s in slabs]
    _validate(name, parsed)
    PLATFORMS[name] = parsed


def load_rates(path: Optional[str] = None) -> None:
    """(Re)load all platforms from a rates JSON file (default: the bundled one)."""
    global DEFAULT_PLATFORM, RATES_CHECKED
    if path is None:
        text = resources.files(__package__).joinpath("rates.json").read_text(encoding="utf-8")
    else:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    data = json.loads(text)
    PLATFORMS.clear()
    for name, slabs in data["platforms"].items():
        register_platform(name, slabs)
    DEFAULT_PLATFORM = data.get("default_platform", next(iter(PLATFORMS)))
    RATES_CHECKED = data.get("checked", "")
    RATE_SOURCES.clear()
    RATE_SOURCES.update(data.get("sources", {}))


def _slabs(platform: str) -> List[Slab]:
    try:
        return PLATFORMS[platform]
    except KeyError:
        raise KeyError(f"Unknown platform {platform!r}. Known: {', '.join(PLATFORMS)}") from None


def interest_rate(borrowed: float, platform: Optional[str] = None) -> float:
    """% p.a. a platform charges when the total borrowed is `borrowed`."""
    for slab in _slabs(platform or DEFAULT_PLATFORM):
        if slab.covers(borrowed):
            return slab.rate
    raise AssertionError("unreachable: last slab is open-ended")


def next_slab(borrowed: float, platform: Optional[str] = None) -> Optional[Slab]:
    """The slab that starts right after the one `borrowed` falls in, if any."""
    slabs = _slabs(platform or DEFAULT_PLATFORM)
    for i, slab in enumerate(slabs):
        if slab.covers(borrowed):
            return slabs[i + 1] if i + 1 < len(slabs) else None
    return None


def daily_interest(borrowed: float, rate: float) -> float:
    """Interest for one day on `borrowed` at `rate` % p.a."""
    return borrowed * rate / 100 / 365


load_rates()
