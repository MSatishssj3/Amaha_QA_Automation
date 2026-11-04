"""Endpoint URLs and segment code maps (source: official dhanhq 2.3.0 SDK).

Every URL is overridable from config so tests and ``dhanbot simulate`` can point the exact
same code at the local mock server.
"""

from __future__ import annotations

from dataclasses import dataclass

LIVE_REST_BASE = "https://api.dhan.co/v2"
LIVE_AUTH_BASE = "https://auth.dhan.co"
LIVE_FEED_URL = "wss://api-feed.dhan.co"
LIVE_ORDER_WS_URL = "wss://api-order-update.dhan.co"
LIVE_INSTRUMENTS_CSV = "https://images.dhan.co/api-data/api-scrip-master.csv"


@dataclass(frozen=True)
class Endpoints:
    rest_base: str = LIVE_REST_BASE
    auth_base: str = LIVE_AUTH_BASE
    feed_url: str = LIVE_FEED_URL
    order_ws_url: str = LIVE_ORDER_WS_URL
    instruments_csv_url: str = LIVE_INSTRUMENTS_CSV

    @property
    def is_live(self) -> bool:
        return self.rest_base == LIVE_REST_BASE

    def feed_ws_url(self, client_id: str, token: str) -> str:
        return f"{self.feed_url}?version=2&token={token}&clientId={client_id}&authType=2"


# API exchangeSegment string <-> numeric code used inside binary feed packets
SEGMENT_TO_CODE: dict[str, int] = {
    "IDX_I": 0,
    "NSE_EQ": 1,
    "NSE_FNO": 2,
    "NSE_CURRENCY": 3,
    "BSE_EQ": 4,
    "MCX_COMM": 5,
    "BSE_CURRENCY": 7,
    "BSE_FNO": 8,
}
CODE_TO_SEGMENT: dict[int, str] = {v: k for k, v in SEGMENT_TO_CODE.items()}

# feed subscription request codes
REQ_TICKER = 15
REQ_QUOTE = 17
REQ_FULL = 21
REQ_DISCONNECT = 12
MODE_CODES = {"ticker": REQ_TICKER, "quote": REQ_QUOTE, "full": REQ_FULL}
MODE_RANK = {REQ_TICKER: 0, REQ_QUOTE: 1, REQ_FULL: 2}

# feed disconnect reason codes (packet code 50)
DISCONNECT_REASONS = {
    805: "too many websocket connections",
    806: "data API subscription required",
    807: "access token expired",
    808: "invalid client id",
    809: "authentication failed",
}
FATAL_DISCONNECTS = frozenset({806, 808, 809})

# instrument CSV (SEM_EXM_EXCH_ID, SEM_SEGMENT) -> API segment
CSV_SEGMENT_MAP: dict[tuple[str, str], str] = {
    ("NSE", "E"): "NSE_EQ",
    ("NSE", "D"): "NSE_FNO",
    ("NSE", "C"): "NSE_CURRENCY",
    ("NSE", "I"): "IDX_I",
    ("BSE", "E"): "BSE_EQ",
    ("BSE", "D"): "BSE_FNO",
    ("BSE", "C"): "BSE_CURRENCY",
    ("BSE", "I"): "IDX_I",
    ("MCX", "M"): "MCX_COMM",
    ("MCX", "I"): "IDX_I",
}
