"""The bots. ``BOTS`` maps each name to its class (used by the supervisor and ``bots run <name>``)."""

from __future__ import annotations

import importlib

_REGISTRY = {
    "config": "tradingbots.bots.config.bot:ConfigBot",
    "sim_market": "tradingbots.platforms.simserver:SimMarketBot",
    "market_data": "tradingbots.bots.market_data.bot:MarketDataBot",
    "tables": "tradingbots.bots.tables.bot:TablesBot",
    "history": "tradingbots.bots.history.bot:HistoryBot",
    "money": "tradingbots.bots.money.bot:MoneyBot",
    "journal": "tradingbots.bots.journal.bot:JournalBot",
    "tax": "tradingbots.bots.tax.bot:TaxBot",
    "budget": "tradingbots.bots.budget.bot:BudgetBot",
    "ai_analyst": "tradingbots.bots.ai_analyst.bot:AIAnalystBot",
    "execution": "tradingbots.bots.execution.bot:ExecutionBot",
    "notifier": "tradingbots.bots.notifier.bot:NotifierBot",
}
BOT_NAMES = tuple(_REGISTRY)


def bot_class(name: str):
    if name not in _REGISTRY:
        raise KeyError(f"unknown bot {name!r}; one of {', '.join(BOT_NAMES)}")
    mod, cls = _REGISTRY[name].split(":")
    return getattr(importlib.import_module(mod), cls)
