"""What the AI analyst must return: validated with pydantic and enforced by structured outputs
(``output_config.format``), so a report is always complete, well-typed JSON."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Trend = Literal["strong_up", "up", "sideways", "down", "strong_down", "unclear"]
Confidence = Literal["low", "medium", "high"]
# Advisory only: a hint for the investor's attention. Nothing ever turns it into an order.
WatchAction = Literal["watch", "consider_entry", "consider_exit", "hold", "avoid"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KeyLevels(_M):
    support: list[float] = Field(description="Nearest support prices in rupees, closest to the price first")
    resistance: list[float] = Field(description="Nearest resistance prices in rupees, closest to the price first")


class StockView(_M):
    symbol: str
    trend: Trend
    outlook: str = Field(description="One or two plain sentences")
    key_levels: KeyLevels
    observations: list[str] = Field(description="At most three short points, each grounded in the packet")
    risks: list[str] = Field(description="At most three short points")
    confidence: Confidence
    watch_action: WatchAction
    data_gaps: list[str] = Field(description="What the packet lacks for a firmer view; empty when nothing")


class PortfolioView(_M):
    summary: str
    concentration: str = Field(description="How concentrated holdings and positions are, by stock and platform")
    charges_drag: str = Field(description="What brokerage, taxes and fees are costing, relative to turnover or P&L")
    budget_use: str = Field(description="How much of today's daily budget is used and what that implies")
    observations: list[str] = Field(description="At most four short points")


class Report(_M):
    headline: str = Field(description="One line of at most 15 words")
    market_context: str = Field(description="The benchmark's move and trend in one or two sentences")
    stocks: list[StockView]
    portfolio: PortfolioView
    caveats: list[str]
    narrative_markdown: str = Field(description="Dashboard summary in Markdown, at most 250 words")


class Scenario(_M):
    name: Literal["bull", "base", "bear"]
    description: str
    trigger: str = Field(description="The price or signal that would confirm this scenario")
    invalidation: str = Field(description="The price or signal that would rule it out")


class DeepDive(_M):
    symbol: str
    summary: str = Field(description="Three or four sentences")
    trend: Trend
    trend_analysis: str
    momentum: str
    volatility_and_risk: str
    key_levels: KeyLevels
    scenarios: list[Scenario] = Field(description="Exactly one bull, one base and one bear scenario")
    watch_action: WatchAction
    confidence: Confidence
    caveats: list[str]
