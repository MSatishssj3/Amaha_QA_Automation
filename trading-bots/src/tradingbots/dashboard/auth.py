"""Dashboard login: a scrypt password hash in ``.env`` (``DASHBOARD_PASSWORD_HASH``), set with
``bots set-password``. The password itself is never stored.

* Wrong attempts slow down for everyone (1, 2, 4 ... 30 s), whichever browser they come from.
* A signed-in session lasts ``dashboard.session_minutes``.
* The dashboard binds to 127.0.0.1 by default. Reach it on a server through an SSH tunnel.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time

_N, _R, _P = 2**15, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32, maxmem=128 * 1024 * 1024)
    b64 = base64.urlsafe_b64encode
    return f"scrypt${_N}${_R}${_P}${b64(salt).decode()}${b64(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.strip().split("$")
        if algo != "scrypt":
            return False
        want = base64.urlsafe_b64decode(dk)
        got = hashlib.scrypt(password.encode(), salt=base64.urlsafe_b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(want), maxmem=128 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


class Throttle:
    """Process-wide failed-attempt counter: the delay doubles per failure, resets on success."""

    def __init__(self) -> None:
        self.failures = 0
        self.last = 0.0
        self._lock = threading.Lock()

    def delay(self) -> float:
        with self._lock:
            if time.monotonic() - self.last > 900:
                self.failures = 0  # quiet for 15 minutes: start over
            return 0.0 if self.failures == 0 else min(30.0, 2.0 ** (self.failures - 1))

    def failed(self) -> None:
        with self._lock:
            self.failures += 1
            self.last = time.monotonic()

    def ok(self) -> None:
        with self._lock:
            self.failures = 0


def gate(stored_hash: str | None, session_minutes: int) -> bool:
    """Render the sign-in form unless this session is signed in. Returns True when the page may render."""
    import streamlit as st

    @st.cache_resource(show_spinner=False)
    def _throttle() -> Throttle:
        return Throttle()

    ss = st.session_state
    if ss.get("auth_at") and time.time() - ss["auth_at"] < session_minutes * 60:
        return True
    ss.pop("auth_at", None)
    st.title("Trading bots")
    if not stored_hash:
        st.error("No dashboard password is set yet. On the server run `bots set-password`, then restart the "
                 "dashboard.")
        return False
    with st.form("login", clear_on_submit=True):
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        th = _throttle()
        wait = th.delay()
        if wait:
            with st.spinner(f"Too many wrong passwords - waiting {wait:.0f}s"):
                time.sleep(wait)
        if verify_password(pw or "", stored_hash):
            th.ok()
            ss["auth_at"] = time.time()
            st.rerun()
        th.failed()
        st.error("Wrong password.")
    return False
