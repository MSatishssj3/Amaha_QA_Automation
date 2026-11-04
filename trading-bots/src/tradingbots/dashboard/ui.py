"""Small shared pieces for the pages: the team's clock, bot health, command results, tables."""

from __future__ import annotations

import time
from typing import Any

import polars as pl
import streamlit as st
from dhanbot.clock import ist_datetime

from ..supervisor import KV_SUPERVISOR
from . import theme as T
from .data import TeamClient


def team_now(cli: TeamClient) -> float:
    """The team's clock (accelerated in a demo), so ages and 'today' match what the bots see."""
    sup = cli.kv(KV_SUPERVISOR) or {}
    sim = sup.get("sim")
    if sim:
        return float(sim["start"]) + (time.time() - float(sim["wall_ref"])) * float(sim["speed"])
    return time.time()


def team_today(cli: TeamClient):
    return ist_datetime(team_now(cli)).date()


def hhmm(ts: float | None) -> str:
    return f"{ist_datetime(float(ts)):%d %b %H:%M:%S}" if ts else "-"


def bot_rows(cli: TeamClient) -> list[dict]:
    """One row per bot: from the supervisor when it runs, else from the bots' own heartbeats."""
    now = time.time()
    sup = cli.kv(KV_SUPERVISOR) or {}
    hbs = cli.heartbeats()
    rows = []
    if sup.get("bots") and now - float(sup.get("ts") or 0) < 30:
        for b in sup["bots"]:
            hb = hbs.get(b["name"], {})
            rows.append({"Bot": b["name"], "State": b["state"], "Heartbeat": _age(hb.get("ts"), now),
                         "Restarts": b.get("restarts", 0), "Errors": (hb.get("counters") or {}).get("errors", 0),
                         "Note": b.get("note") or hb.get("last_error") or ""})
    else:
        for name, hb in sorted(hbs.items()):
            fresh = now - float(hb.get("ts") or 0) < 30
            rows.append({"Bot": name, "State": hb.get("status") if fresh else "silent", "Heartbeat": _age(hb.get("ts"), now),
                         "Restarts": "-", "Errors": (hb.get("counters") or {}).get("errors", 0),
                         "Note": hb.get("last_error") or ""})
    return rows


def _age(ts: Any, now: float) -> str:
    if not ts:
        return "never"
    s = now - float(ts)
    return f"{s:.0f}s ago" if s < 120 else f"{s / 60:.0f} min ago"


def bot_running(cli: TeamClient, name: str) -> bool:
    hb = cli.heartbeats().get(name)
    return bool(hb) and time.time() - float(hb.get("ts") or 0) < 30 and hb.get("status") == "ok"


def result(res: dict, ok_text: str = "Done.") -> None:
    """Show a command's reply: its error, the problems it lists, or success."""
    if not res.get("ok", False):
        st.error(res.get("error") or "The bot reported an error.")
        return
    errs = res.get("errors") or []
    if errs:
        st.error("Not saved:\n\n" + "\n".join(f"- {e}" for e in errs))
        return
    for flag in ("applied", "added", "saved", "done", "started", "restarted", "submitted", "ran"):
        if flag in res and not res[flag]:
            st.warning(res.get("reason") or res.get("error") or "Nothing changed.")
            return
    changes = res.get("changes") or []
    st.success(ok_text + ("\n\n" + "\n".join(f"- {c}" for c in changes) if changes else ""))
    if res.get("restart_required"):
        st.info("Some of these changes take effect after the bots restart (`bots run --all`).")


def table(df: pl.DataFrame | list[dict], height: int | None = None, **kw) -> None:
    if isinstance(df, list):
        df = pl.DataFrame(df) if df else pl.DataFrame()
    if df.is_empty():
        st.caption("Nothing here yet.")
        return
    st.dataframe(df, hide_index=True, width="stretch", height=height or "auto", **kw)


def chart(fig) -> None:
    st.plotly_chart(fig, theme=None, config={"displayModeBar": False, "responsive": True})


def tokens() -> dict:
    return T.tokens()
