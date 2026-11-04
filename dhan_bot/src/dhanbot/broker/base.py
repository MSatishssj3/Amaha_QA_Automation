"""Broker interface shared by the live (Dhan REST) and paper (simulated) implementations.

The OMS cannot tell them apart: both acknowledge placements and then push ``BrokerUpdate`` objects
(acks, fills, cancels, rejects) through ``on_update``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from ..errors import DhanError
from ..models import BrokerUpdate, Order, OrderStatus


@dataclass(slots=True)
class PlaceResult:
    ok: bool
    broker_id: str = ""
    status: OrderStatus | None = None
    error: DhanError | None = None
    rtt_ns: int = 0


@dataclass(slots=True)
class BrokerResult:
    ok: bool
    error: DhanError | None = None
    rtt_ns: int = 0


class LookupFailed(Exception):
    """The broker could not be asked (network/server); the order state is still unknown."""


class Broker(ABC):
    name = "broker"
    supports_super = True

    def __init__(self) -> None:
        self.on_update: Callable[[BrokerUpdate], None] = lambda u: None

    async def start(self) -> None:  # pragma: no cover - default no-op
        pass

    async def stop(self) -> None:  # pragma: no cover - default no-op
        pass

    @abstractmethod
    async def place(self, order: Order) -> PlaceResult: ...

    @abstractmethod
    async def modify(self, order: Order, *, price_paise: int | None = None, qty: int | None = None,
                     trigger_paise: int | None = None, leg: str | None = None) -> BrokerResult: ...

    @abstractmethod
    async def cancel(self, order: Order, leg: str | None = None) -> BrokerResult: ...

    @abstractmethod
    async def lookup(self, cid: str) -> BrokerUpdate | None:
        """Find an order by correlationId. ``None`` = definitely not at the broker. Raises LookupFailed."""

    async def positions(self) -> list[dict] | None:
        return None

    async def exit_all(self) -> bool:
        return False
