"""Dhan error envelope parsing and classification.

Dhan returns ``{"errorType": ..., "errorCode": "DH-9xx", "errorMessage": ...}`` on failures.
Codes as documented for DhanHQ v2 (verify against live behaviour; the classifier also looks at
message text so an unexpected code still lands in a sensible category):

    DH-901 invalid / expired access token      DH-906 order error (RMS / exchange)
    DH-902 no access (API/data not subscribed)  DH-907 data error
    DH-903 account issue (segment inactive)     DH-908 internal server error
    DH-904 rate limit exceeded                  DH-909 network error
    DH-905 input / validation error             DH-910 other
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Category(str, enum.Enum):
    AUTH = "AUTH"  # token invalid/expired -> refresh token, block entries
    ACCESS = "ACCESS"  # API / data plan not enabled
    ACCOUNT = "ACCOUNT"  # segment not active, account blocked
    IP_NOT_ALLOWED = "IP_NOT_ALLOWED"  # order from a non-registered static IP
    RATE_LIMIT = "RATE_LIMIT"
    INPUT = "INPUT"  # bad request fields
    MARGIN = "MARGIN"  # insufficient funds / RMS
    CIRCUIT = "CIRCUIT"  # price outside circuit / price band
    ORDER = "ORDER"  # other order rejection
    DATA = "DATA"
    SERVER = "SERVER"  # 5xx - may be retried for idempotent calls
    NETWORK = "NETWORK"  # timeout / connection error (outcome unknown for orders!)
    OTHER = "OTHER"


_CODE_MAP = {
    "DH-901": Category.AUTH,
    "DH-902": Category.ACCESS,
    "DH-903": Category.ACCOUNT,
    "DH-904": Category.RATE_LIMIT,
    "DH-905": Category.INPUT,
    "DH-906": Category.ORDER,
    "DH-907": Category.DATA,
    "DH-908": Category.SERVER,
    "DH-909": Category.NETWORK,
    "DH-910": Category.OTHER,
}

# message fragments (lower-case) that refine the category regardless of code
_TEXT_RULES: tuple[tuple[tuple[str, ...], Category], ...] = (
    (("static ip", "ip not", "whitelist", "white list", "unregistered ip", "invalid ip", "ip address"), Category.IP_NOT_ALLOWED),
    (("rate limit", "too many requests", "throttl"), Category.RATE_LIMIT),
    (("insufficient", "margin", "rms", "fund"), Category.MARGIN),
    (("ckt", "circuit", "price band", "price range", "dpr"), Category.CIRCUIT),
    (("token", "expired", "unauthor", "authentication"), Category.AUTH),
)


@dataclass(slots=True)
class DhanError:
    code: str = ""
    type: str = ""
    message: str = ""
    http_status: int = 0
    category: Category = Category.OTHER
    timeout: bool = False  # True when the request outcome is unknown (orders: must be resolved!)

    @property
    def retryable(self) -> bool:
        """Safe to retry *idempotent* (GET-like) calls. Never auto-retry order placement on this."""
        return self.category in (Category.SERVER, Category.NETWORK, Category.RATE_LIMIT)

    def __str__(self) -> str:
        bits = [self.category.value]
        if self.code:
            bits.append(self.code)
        if self.http_status:
            bits.append(f"http={self.http_status}")
        if self.message:
            bits.append(self.message)
        return " ".join(bits)


def classify(code: str, message: str, http_status: int = 0) -> Category:
    text = (message or "").lower()
    for fragments, cat in _TEXT_RULES:
        if any(f in text for f in fragments):
            # a token message under an order code is still AUTH; but don't let "fund" in a
            # rate-limit message etc. override an explicit RATE_LIMIT code
            if code == "DH-904" and cat is not Category.RATE_LIMIT:
                break
            return cat
    if code in _CODE_MAP:
        return _CODE_MAP[code]
    if http_status == 429:
        return Category.RATE_LIMIT
    if http_status in (401, 403):
        return Category.AUTH
    if http_status >= 500:
        return Category.SERVER
    if 400 <= http_status < 500:
        return Category.INPUT
    return Category.OTHER


def from_response(http_status: int, body: object) -> DhanError:
    code = typ = msg = ""
    if isinstance(body, dict):
        code = str(body.get("errorCode") or body.get("error_code") or "")
        typ = str(body.get("errorType") or body.get("error_type") or "")
        msg = str(body.get("errorMessage") or body.get("error_message") or body.get("message") or "")
    elif body:
        msg = str(body)[:300]
    return DhanError(code=code, type=typ, message=msg, http_status=http_status,
                     category=classify(code, msg, http_status))


def network_error(message: str, timeout: bool = True) -> DhanError:
    return DhanError(message=message, category=Category.NETWORK, timeout=timeout)


class ConfigError(Exception):
    """Raised with a list of human-readable problems found in the configuration."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid configuration:\n  - " + "\n  - ".join(problems))


class PreflightError(Exception):
    pass
