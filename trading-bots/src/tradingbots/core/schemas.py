"""Every table the team writes: columns, types, partitioning, owner and meaning.

Timestamps are epoch seconds (``*_ts``, UTC); dates are IST calendar dates. Money is in rupees.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

F = pl.Float64
I = pl.Int64
S = pl.String
B = pl.Boolean


@dataclass(frozen=True)
class TableDef:
    name: str
    owner: str
    description: str
    columns: dict[str, tuple[pl.DataType, str]]
    partition: str | None = None  # hive partition key ("date" / "symbol") or None
    mode: str = "replace"  # "append" (parts + nightly compaction) or "replace" (rewrite partition)

    @property
    def schema(self) -> dict[str, pl.DataType]:
        return {k: v[0] for k, v in self.columns.items()}

    def empty(self) -> pl.DataFrame:
        cols = dict(self.schema)
        if self.partition and self.partition not in cols:
            cols[self.partition] = pl.Date if self.partition == "date" else S
        return pl.DataFrame(schema=cols)


def _t(name, owner, description, partition=None, mode="replace", **cols) -> TableDef:
    return TableDef(name, owner, description, cols, partition, mode)


TABLES: dict[str, TableDef] = {t.name: t for t in [
    # ---- written by the tables bot from market-data / execution events ----------------------------
    _t("bars_1m", "tables", "One-minute OHLCV candles built from every tick", "date", "append",
       symbol=(S, "trading symbol"), segment=(S, "exchange segment"), security_id=(I, "Dhan security id"),
       tf=(I, "timeframe in seconds"), start_ts=(F, "candle open time"), open=(F, ""), high=(F, ""), low=(F, ""),
       close=(F, ""), volume=(I, "traded volume in the candle (0 for indices)")),
    _t("quotes_latest", "tables", "Latest quote per instrument (rewritten every few seconds)",
       symbol=(S, ""), segment=(S, ""), security_id=(I, ""), ltp=(F, "last traded price"), bid=(F, ""), ask=(F, ""),
       volume=(I, "day volume"), oi=(I, "open interest"), prev_close=(F, ""), change_pct=(F, "% change vs prev close"),
       ltt_ts=(F, "last trade time"), updated_ts=(F, "")),
    _t("positions", "tables", "Latest broker positions snapshot of the day", "date", "replace",
       platform=(S, ""), symbol=(S, ""), segment=(S, ""), security_id=(I, ""), product=(S, "INTRADAY / CNC / MARGIN"),
       net_qty=(I, ""), buy_qty=(I, ""), sell_qty=(I, ""), buy_avg=(F, ""), sell_avg=(F, ""), cost_price=(F, ""),
       realized=(F, "realised P&L"), unrealized=(F, "unrealised P&L"), as_of_ts=(F, "")),
    _t("holdings", "tables", "Latest demat holdings snapshot of the day", "date", "replace",
       platform=(S, ""), symbol=(S, ""), security_id=(I, ""), isin=(S, ""), qty=(I, "total quantity"),
       avg_cost=(F, "average cost price"), as_of_ts=(F, "")),
    _t("funds", "tables", "Fund-limit snapshots", "date", "append",
       platform=(S, ""), available=(F, "available balance"), utilized=(F, ""), sod_limit=(F, "start-of-day limit"),
       withdrawable=(F, ""), as_of_ts=(F, "")),
    _t("orders", "tables", "Latest broker order book of the day", "date", "replace",
       platform=(S, ""), order_id=(S, ""), correlation_id=(S, ""), symbol=(S, ""), segment=(S, ""), side=(S, ""),
       qty=(I, ""), filled_qty=(I, ""), price=(F, ""), avg_price=(F, ""), status=(S, ""), product=(S, ""),
       order_type=(S, ""), leg=(S, ""), reason=(S, ""), source=(S, "bot / manual"), updated_ts=(F, "")),
    _t("trades", "tables", "Executed trades with per-trade charges", "date", "replace",
       platform=(S, ""), trade_id=(S, ""), order_id=(S, ""), symbol=(S, ""), segment=(S, ""), security_id=(I, ""),
       side=(S, "BUY / SELL"), qty=(I, ""), price=(F, ""), value=(F, "qty x price"), product=(S, ""),
       instrument=(S, ""), brokerage=(F, ""), stt=(F, ""), exchange_txn=(F, ""), sebi=(F, ""), stamp_duty=(F, ""),
       gst=(F, ""), total_charges=(F, ""), source=(S, "bot / manual"), trade_ts=(F, "")),
    _t("ledger", "tables", "Account ledger vouchers (funds in/out, charges, settlements)", "date", "replace",
       platform=(S, ""), narration=(S, ""), voucher_desc=(S, ""), voucher_no=(S, ""), debit=(F, ""), credit=(F, ""),
       running_balance=(F, ""), category=(S, "deposit / withdrawal / charges / settlement / trade / other")),
    _t("exec_orders", "tables", "Orders placed by the execution bot (dhan_bot engine)", "date", "replace",
       cid=(S, "correlation id"), broker_id=(S, ""), symbol=(S, ""), segment=(S, ""), side=(S, ""), qty=(I, ""),
       price=(F, ""), status=(S, ""), intent=(S, "ENTRY / EXIT"), role=(S, ""), filled_qty=(I, ""),
       avg_price=(F, ""), reason=(S, ""), updated_ts=(F, "last change"), placed_ts=(F, "when it was placed")),
    _t("exec_fills", "tables", "Fills of execution-bot orders", "date", "append",
       cid=(S, ""), broker_id=(S, ""), symbol=(S, ""), segment=(S, ""), side=(S, ""), qty=(I, ""), price=(F, ""),
       intent=(S, ""), ts=(F, "")),
    _t("candles_daily", "tables", "Daily OHLCV history per symbol", "symbol", "replace",
       segment=(S, ""), security_id=(I, ""), day=(pl.Date, "IST trading date"), open=(F, ""), high=(F, ""),
       low=(F, ""), close=(F, ""), volume=(I, "")),
    # ---- history bot ------------------------------------------------------------------------------------
    _t("history_metrics", "history", "Per-stock trend metrics as of the latest daily close",
       symbol=(S, ""), as_of=(pl.Date, "last candle date"), close=(F, ""), ret_1d=(F, "%"), ret_1w=(F, "%"),
       ret_1m=(F, "%"), ret_3m=(F, "%"), ret_6m=(F, "%"), ret_1y=(F, "%"), ret_ytd=(F, "%"), cagr=(F, "% per year"),
       vol_ann=(F, "annualised volatility %"), max_dd_1y=(F, "max drawdown % (1y)"), sharpe_1y=(F, ""),
       sma20=(F, ""), sma50=(F, ""), sma200=(F, ""), dist_sma50=(F, "% above SMA50"), dist_sma200=(F, "%"),
       rsi14=(F, ""), macd=(F, ""), macd_signal=(F, ""), atr14_pct=(F, "% of price"), adx14=(F, ""),
       high_52w=(F, ""), low_52w=(F, ""), from_high_52w=(F, "% below 52w high"), from_low_52w=(F, "% above 52w low"),
       vol_ratio20=(F, "today's volume / 20d avg"), beta_1y=(F, "vs benchmark"), rs_3m=(F, "3m return minus benchmark"),
       support=(F, "recent swing low"), resistance=(F, "recent swing high"),
       golden_cross_on=(pl.Date, "last SMA50>SMA200 cross"), death_cross_on=(pl.Date, "last SMA50<SMA200 cross"),
       trend=(S, "strong_up / up / sideways / down / strong_down"), trend_score=(F, "-100..100"), days=(I, "history days")),
    _t("monthly_returns", "history", "Month-by-month returns per symbol (seasonality)",
       symbol=(S, ""), year=(I, ""), month=(I, ""), ret=(F, "%")),
    _t("correlations", "history", "Correlation of daily returns between watchlist symbols (1y)",
       symbol_a=(S, ""), symbol_b=(S, ""), corr=(F, "")),
    # ---- money bot --------------------------------------------------------------------------------------
    _t("money_allocation", "money", "Where the money is: invested vs current value by platform and stock",
       platform=(S, ""), symbol=(S, ""), sector=(S, ""), kind=(S, "holding / position"), qty=(I, ""),
       invested=(F, ""), current_value=(F, ""), pnl=(F, ""), pnl_pct=(F, ""), weight_pct=(F, "% of total value")),
    _t("money_pnl", "money", "Realised P&L by day and stock (from trades) plus unrealised by stock",
       platform=(S, ""), day=(pl.Date, ""), symbol=(S, ""), realized=(F, ""), unrealized=(F, ""), charges=(F, ""),
       net=(F, "")),
    _t("money_charges", "money", "Charges by type, platform, stock and month",
       platform=(S, ""), month=(S, "YYYY-MM"), symbol=(S, ""), brokerage=(F, ""), stt=(F, ""), exchange_txn=(F, ""),
       sebi=(F, ""), stamp_duty=(F, ""), gst=(F, ""), total=(F, ""), turnover=(F, ""), pct_of_turnover=(F, "")),
    _t("money_cashflows", "money", "Funds added / withdrawn by month",
       platform=(S, ""), month=(S, ""), deposits=(F, ""), withdrawals=(F, ""), net=(F, "")),
    _t("money_expenses", "money", "Spending outside trades: subscriptions, servers, AI API, manual entries",
       id=(I, ""), day=(pl.Date, ""), category=(S, ""), platform=(S, ""), amount=(F, "rupees"), note=(S, ""),
       auto=(B, "added automatically (e.g. AI API cost)")),
    _t("money_summary", "money", "Headline money numbers",
       metric=(S, ""), value=(F, ""), unit=(S, "INR / % / count"), note=(S, "")),
    # ---- ai-analyst --------------------------------------------------------------------------------------
    _t("ai_costs", "ai_analyst", "Every Claude API call: tokens and cost", "date", "append",
       ts=(F, ""), model=(S, ""), purpose=(S, ""), input_tokens=(I, ""), output_tokens=(I, ""),
       cache_read_tokens=(I, ""), cache_write_tokens=(I, ""), usd=(F, ""), inr=(F, "at the configured rate")),
]}


def coerce(table: str, rows: list[dict] | pl.DataFrame) -> pl.DataFrame:
    """Build a frame with exactly the declared columns and types (missing columns become null)."""
    td = TABLES[table]
    schema = td.schema
    if isinstance(rows, pl.DataFrame):
        df = rows
    else:
        df = pl.from_dicts(rows, schema={k: v for k, v in schema.items()}, strict=False) if rows else td.empty()
    exprs = []
    for col, dtype in schema.items():
        if col in df.columns:
            exprs.append(pl.col(col).cast(dtype, strict=False))
        else:
            exprs.append(pl.lit(None, dtype=dtype).alias(col))
    return df.select(exprs)
