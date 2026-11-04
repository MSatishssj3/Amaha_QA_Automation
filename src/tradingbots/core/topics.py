"""Every topic on the bus, in one place. Payload schemas live in ``messages.py``.

Events (publish/subscribe, at-least-once, every subscribing bot gets every message):
"""

CONFIG_CHANGED = "config.changed"  # {version, changed: [str], author}
AUTH_TOKEN = "auth.token"  # {platform, version, expires_at} - the token itself is in KV secret:token

MARKET_QUOTES = "market.quotes"  # QuotesMsg (coalesced, several instruments per message)
MARKET_BARS = "market.bars"  # BarsMsg (closed candles built from every tick)
MARKET_STATUS = "market.status"  # {platform, connected, instruments, frames, last_tick_age_s}
MARKET_HISTORY = "market.history"  # HistoryMsg (daily candles fetched on request)

ACCOUNT_POSITIONS = "account.positions"  # SnapshotMsg
ACCOUNT_HOLDINGS = "account.holdings"
ACCOUNT_FUNDS = "account.funds"
ACCOUNT_ORDERS = "account.orders"
ACCOUNT_TRADES = "account.trades"  # SnapshotMsg (rows = Dhan trade book / history rows)
ACCOUNT_LEDGER = "account.ledger"
ACCOUNT_TOPICS = (ACCOUNT_POSITIONS, ACCOUNT_HOLDINGS, ACCOUNT_FUNDS, ACCOUNT_ORDERS, ACCOUNT_TRADES, ACCOUNT_LEDGER)

TABLES_UPDATED = "tables.updated"  # {table, rows, partitions}
HISTORY_UPDATED = "history.updated"  # {symbols, as_of}
MONEY_UPDATED = "money.updated"  # {as_of, summary}

BUDGET_POLICY = "budget.policy"  # BudgetPolicyMsg
BUDGET_USAGE = "budget.usage"  # BudgetUsageMsg
BUDGET_EXTERNAL = "budget.external"  # {date, turnover_inr, trades, per_stock} - trades not placed by the bot

ALERT = "alert"  # AlertMsg

AI_REPORT = "ai.report"  # {report_id, kind, headline, cost_usd}
AI_COST = "ai.cost"  # {ts, model, input_tokens, output_tokens, cache_read, cache_write, usd, purpose}

EXEC_ORDER = "exec.order"  # ExecOrderMsg (every order state change from the trading engine)
EXEC_FILL = "exec.fill"  # ExecFillMsg
EXEC_STATE = "exec.state"  # {state, reason, mode}
EXEC_PNL = "exec.pnl"  # {realised, unrealised, charges, net, open_positions}

BOT_HEARTBEAT = "bot.heartbeat"  # HeartbeatMsg


def cmd(bot: str, action: str) -> str:
    """Command topic (request/reply), delivered to the named bot: ``cmd.<bot>.<action>``."""
    return f"cmd.{bot}.{action}"


# key-value store keys
KV_CONFIG = "config:current"  # {version, settings}
KV_TOKEN = "secret:token"  # {platform, client_id, token, expires_at, version, endpoints?}
KV_QUOTES = "market:quotes"  # {symbol: quote}
KV_BUDGET_POLICY = "budget:policy"
KV_BUDGET_USAGE = "budget:usage"
KV_EXEC_STATUS = "exec:status"
KV_SIM = "sim:server"  # {endpoints..., client_id, token} published by the simulated-market server


def kv_heartbeat(bot: str) -> str:
    return f"hb:{bot}"
