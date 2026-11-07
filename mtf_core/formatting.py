"""Indian-style number formatting helpers for bots that display results."""

from __future__ import annotations


def inr(x: float, decimals: int = 2) -> str:
    """Group digits the Indian way: 1234567.8 -> '12,34,567.80'."""
    neg = x < 0
    text = f"{abs(x):.{decimals}f}"
    whole, _, frac = text.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + whole + ("." + frac if frac else "")


def rupees(x: float, decimals: int = 2) -> str:
    """'Rs' amount with the rupee sign: 100000 -> '₹1,00,000.00'."""
    return "₹" + inr(x, decimals)


def _trim(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def amount_in_words(x: float) -> str:
    """150000 -> '1.5 lakh', 25000000 -> '2.5 crore', 4000 -> '4 thousand'."""
    if x >= 1e7:
        return f"{_trim(x / 1e7)} crore"
    if x >= 1e5:
        return f"{_trim(x / 1e5)} lakh"
    if x >= 1e3:
        return f"{_trim(x / 1e3)} thousand"
    return f"{_trim(x)} rupees"


def short_amount(x: float) -> str:
    """150000 -> '1.5 L', 25000000 -> '2.5 Cr', 4000 -> '4 K'."""
    if x >= 1e7:
        return f"{_trim(x / 1e7)} Cr"
    if x >= 1e5:
        return f"{_trim(x / 1e5)} L"
    if x >= 1e3:
        return f"{_trim(x / 1e3)} K"
    return _trim(x)
