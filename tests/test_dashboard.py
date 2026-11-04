"""Dashboard: login, and every page rendering against seeded team data (Streamlit's headless AppTest)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from tradingbots.core import topics
from tradingbots.dashboard import theme as T
from tradingbots.dashboard.auth import Throttle, hash_password, verify_password
from tradingbots.dashboard.charts import nice_ticks

from .dash_seed import PASSWORD, FakeBots, replies, seed

APP = str(Path(__file__).resolve().parents[1] / "src" / "tradingbots" / "dashboard" / "app.py")


def test_password_hash_and_throttle():
    h = hash_password("s3cret pass")
    assert h.startswith("scrypt$") and "s3cret" not in h
    assert verify_password("s3cret pass", h) and not verify_password("s3cret pas", h)
    assert not verify_password("x", "garbage") and not verify_password("x", "bcrypt$1$2$3$4$5")
    th = Throttle()
    assert th.delay() == 0
    for _ in range(3):
        th.failed()
    assert th.delay() == 4.0
    th.ok()
    assert th.delay() == 0


def test_formatting_helpers():
    assert T.inr(100000) == "₹1,00,000" and T.inr(12345678) == "₹1,23,45,678"
    assert T.inr(1234567, compact=True) == "₹12.35 L" and T.inr(123456789, compact=True) == "₹12.35 Cr"
    assert T.inr(-320.5, signed=True) == "-₹320" and T.inr(42.5) == "₹42.50" and T.inr(None) == "-"
    assert T.pct(1.234) == "+1.2%" and T.arrow(-1) == "▼" and T.arrow(0) == "•"
    assert nice_ticks(0, 60000) == [0, 20000, 40000, 60000] and nice_ticks(-450, 900) == [-500, 0, 500, 1000]
    assert T.thin_bargap(3, 300) > 0.5  # 100px bands -> bars stay ~22px thick


@pytest.fixture
def app(tmp_path, monkeypatch):
    today = time.localtime()
    from datetime import date

    d = date(today.tm_year, today.tm_mon, today.tm_mday)
    seed(tmp_path, d)
    monkeypatch.setenv("TRADINGBOTS_BASE", str(tmp_path))
    monkeypatch.delenv("DASHBOARD_PASSWORD_HASH", raising=False)
    with FakeBots(tmp_path, replies(d.isoformat())) as bots:
        yield tmp_path, bots


def signed_in() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["auth_at"] = time.time()
    return at


def test_login_required_and_wrong_password_refused(app):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert at.title[0].value == "Trading bots" and len(at.text_input) == 1  # only the sign-in form
    at.text_input[0].input("wrong")
    at.button[0].click().run()
    assert any("Wrong password" in e.value for e in at.error)
    at.text_input[0].input(PASSWORD)
    at.button[0].click().run(timeout=90)  # the throttle waits 1 s after one failure
    assert not at.exception and at.title[0].value == "Overview"


PAGES = [
    ("pages/overview.py", "Overview", ["Net worth", "Daily budget used"]),
    ("pages/market.py", "Live market", ["Feed"]),
    ("pages/history.py", "Stock history", ["Trend", "RSI 14"]),
    ("pages/money.py", "Money", ["Net worth", "XIRR"]),
    ("pages/budget.py", "Daily budget", ["New entries today", "Entries blocked by the budget"]),
    ("pages/trading.py", "Trading", ["Mode", "Net P&L today"]),
    ("pages/ai.py", "AI insights", ["Model", "Spent today"]),
    ("pages/tables.py", "Tables", []),
    ("pages/settings.py", "Settings", []),
]


@pytest.mark.parametrize("page,title,metrics", PAGES, ids=[p[1] for p in PAGES])
def test_every_page_renders(app, page, title, metrics):
    at = signed_in().run()
    at.switch_page(page).run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title[0].value == title
    labels = [m.label for m in at.metric]
    for want in metrics:
        assert any(want in lbl for lbl in labels), (want, labels)


def test_overview_shows_budget_meters_and_alerts(app):
    at = signed_in().run()
    md = "\n".join(m.value for m in at.markdown)
    assert 'role="meter"' in md and "near the limit" in md and "limit reached" in md  # 85% and 100% meters
    assert "New entries today at 85%" in md
    vals = {m.label: m.value for m in at.metric}
    assert vals["Net worth"] == "₹2.63 L" and vals["Daily budget used"] == "85%"


def test_budget_form_sends_a_config_update(app):
    _, bots = app
    at = signed_in().run()
    at.switch_page("pages/budget.py").run()
    box = next(n for n in at.number_input if n.label.startswith("New entries per day"))
    box.set_value(75000.0)
    next(b for b in at.button if b.label == "Save limits").click().run()
    assert not at.exception
    sent = [d for t, d in bots.seen if t == topics.cmd("config", "update")]
    assert sent and sent[-1]["patch"]["budget"]["daily_capital_limit_inr"] == 75000.0
    assert sent[-1]["author"] == "dashboard"
    assert any("Limits saved" in s.value for s in at.success)


def test_ai_page_renders_the_report(app):
    at = signed_in().run()
    at.switch_page("pages/ai.py").run()
    assert not at.exception
    assert any("Large caps steady" in m.value for m in at.markdown)


def test_notifications_tab_status_test_message_and_save(app):
    _, bots = app
    at = signed_in().run()
    at.switch_page("pages/settings.py").run()
    assert not at.exception, [e.value for e in at.exception]
    vals = {m.label: m.value for m in at.metric}
    assert vals["Telegram"] == "on" and vals["Email"] == "off"
    assert vals["Sent since start"] == "5 Telegram · 0 email"
    next(b for b in at.button if b.label == "Send a test message").click().run()
    assert not at.exception
    assert any(s.value == "telegram: sent" for s in at.success)
    assert any(i.value.startswith("email: off - email: set SMTP_HOST") for i in at.info)
    assert [t for t, _ in bots.seen].count(topics.cmd("notifier", "test")) == 1
    next(t for t in at.text_input if t.label.startswith("Quiet hours")).input("23:00-06:30")
    next(b for b in at.button if b.label == "Save notifications").click().run()
    assert not at.exception
    sent = [d for t, d in bots.seen if t == topics.cmd("config", "update")]
    assert sent and sent[-1]["patch"]["notify"]["quiet_hours"] == ["23:00", "06:30"]
    assert sent[-1]["patch"]["notify"]["info_keys"] == ["exec-started", "exec-day-report", "budget-summary",
                                                        "ai-report", "ai-deep-dive"]
