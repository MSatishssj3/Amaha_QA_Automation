"""Rule-based stand-in for the demo (``ai.provider = "stub"``): no AI, no API key, no cost.

It fills the same report schema from the history bot's metrics with fixed rules, so the dashboard and
the rest of the team can be tried out end to end. Every report it writes says plainly that it was not
produced by Claude.
"""

from __future__ import annotations

from .schema import KeyLevels, PortfolioView, Report, StockView

STUB_NOTE = "Produced by simple fixed rules for the demo - not by Claude. Set ai.provider = \"anthropic\" for AI analysis."


def _levels(s: dict) -> KeyLevels:
    px = s.get("ltp") or s.get("close") or 0.0
    cands = [v for v in (s.get("support"), s.get("resistance"), s.get("sma50"), s.get("sma200"), s.get("high_52w"),
                         s.get("low_52w")) if isinstance(v, (int, float)) and v > 0]
    sup = sorted((v for v in cands if v < px), key=lambda v: px - v)[:3]
    res = sorted((v for v in cands if v > px), key=lambda v: v - px)[:3]
    return KeyLevels(support=[round(v, 2) for v in sup], resistance=[round(v, 2) for v in res])


def stub_report(packet: dict) -> Report:
    views = []
    for s in packet.get("stocks", []):
        trend = s.get("trend") if s.get("trend") in ("strong_up", "up", "sideways", "down", "strong_down") else "unclear"
        rsi, held = s.get("rsi14"), bool(s.get("position"))
        if held:
            action = "consider_exit" if trend in ("down", "strong_down") else "hold"
        elif trend == "strong_up" and rsi is not None and rsi < 70:
            action = "consider_entry"
        elif trend in ("down", "strong_down"):
            action = "avoid"
        else:
            action = "watch"
        obs = []
        if s.get("ret_1m") is not None:
            obs.append(f"1-month return {s['ret_1m']:+.1f}%")
        if rsi is not None:
            obs.append(f"RSI {rsi:.0f}" + (" (stretched)" if rsi > 70 else " (oversold)" if rsi < 30 else ""))
        if s.get("dist_sma50") is not None:
            obs.append(f"{s['dist_sma50']:+.1f}% vs its 50-day average")
        risks = []
        if s.get("atr14_pct") is not None:
            risks.append(f"typical daily range {s['atr14_pct']:.1f}% of price")
        if s.get("max_dd_1y") is not None:
            risks.append(f"worst fall in a year {s['max_dd_1y']:.0f}%")
        gaps = [] if s.get("history_days") else ["no daily history yet"]
        days = s.get("history_days") or 0
        conf = "medium" if days >= 250 and trend != "unclear" else "low"
        views.append(StockView(symbol=s["symbol"], trend=trend,
                               outlook=f"Rule-based view: {trend.replace('_', ' ')} trend"
                                       + (f" (score {s['trend_score']:+.0f})." if s.get("trend_score") is not None else "."),
                               key_levels=_levels(s), observations=obs[:3], risks=risks[:3], confidence=conf,
                               watch_action=action, data_gaps=gaps))
    m = packet.get("market") or {}
    budget = packet.get("budget") or {}
    first = (budget.get("limits") or [{}])[0]
    budget_text = (f"{first.get('label')}: {first.get('pct', 0):.0f}% used" if first.get("label")
                   else "no daily budget status")
    ups = sum(v.trend in ("up", "strong_up") for v in views)
    headline = f"[Demo stub - no AI] {ups} of {len(views)} stocks in an uptrend"
    md = "\n".join([f"**{headline}**", ""] + [f"- **{v.symbol}**: {v.trend.replace('_', ' ')} - {v.watch_action.replace('_', ' ')}"
                                             for v in views] + ["", f"*{STUB_NOTE} Not financial advice.*"])
    return Report(headline=headline,
                  market_context=(f"{m.get('benchmark')}: {m.get('change_pct', 0) or 0:+.2f}% today, trend "
                                  f"{m.get('trend', 'unknown')}" if m.get("benchmark") else "no benchmark data"),
                  stocks=views,
                  portfolio=PortfolioView(summary="Rule-based summary of the money tables.",
                                          concentration="See the Money page for weights.",
                                          charges_drag="See the Money page for charges by month.",
                                          budget_use=budget_text, observations=[]),
                  caveats=[STUB_NOTE], narrative_markdown=md)
