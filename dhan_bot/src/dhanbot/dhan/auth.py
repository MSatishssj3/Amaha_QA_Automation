"""Access-token lifecycle.

Sources, in priority order: ``DHAN_ACCESS_TOKEN`` (env/.env) -> cached ``data/token.json``
(written by ``dhanbot login``) -> automatic PIN+TOTP login -> OAuth helper.

Dhan access tokens are JWTs; ``exp`` is read from the payload (no signature check - we only need the
expiry) so the bot knows ahead of time when to renew (``GET /v2/RenewToken``) or log in again.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import stat
from pathlib import Path
from typing import Callable

import aiohttp
import orjson

from ..clock import RealClock
from ..config import AppConfig
from ..logging_setup import register_secrets
from .endpoints import Endpoints

log = logging.getLogger(__name__)


class AuthError(Exception):
    pass


def jwt_expiry(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = orjson.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if exp else None
    except Exception:
        return None


def _extract_token(data: object) -> str:
    if isinstance(data, dict):
        for k in ("accessToken", "access_token", "token"):
            v = data.get(k)
            if isinstance(v, str) and v:
                return v
        inner = data.get("data")
        if isinstance(inner, dict):
            return _extract_token(inner)
    return ""


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # Windows / exotic filesystems
        pass
    os.replace(tmp, path)


class TokenManager:
    def __init__(self, cfg: AppConfig, endpoints: Endpoints, clock: RealClock | None = None,
                 cache_path: Path | None = None) -> None:
        self.cfg = cfg
        self.ep = endpoints
        self.clock = clock or RealClock()
        self.client_id = cfg.secrets.client_id
        self.cache_path = cache_path or (cfg.data_dir / "token.json")
        self.token = ""
        self.expires_at: float | None = None
        self.source = ""
        self._listeners: list[Callable[[str], None]] = []
        self._lock = asyncio.Lock()

    def on_change(self, fn: Callable[[str], None]) -> None:
        self._listeners.append(fn)

    def seconds_left(self) -> float:
        if not self.token:
            return 0.0
        if self.expires_at is None:
            return float("inf")
        return self.expires_at - self.clock.time()

    def _set(self, token: str, source: str, persist: bool = True) -> None:
        changed = token != self.token
        self.token, self.source, self.expires_at = token, source, jwt_expiry(token)
        register_secrets([token])
        if persist and source != "env":
            write_private(self.cache_path, orjson.dumps({"client_id": self.client_id, "token": token,
                                                         "expires_at": self.expires_at, "source": source}))
        if changed:
            log.info("access token loaded from %s (valid for %.1f h)", source,
                     (self.seconds_left() / 3600) if self.expires_at else float("nan"))
            for fn in self._listeners:
                try:
                    fn(token)
                except Exception:
                    log.exception("token listener failed")

    def _candidates(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if self.cfg.secrets.access_token:
            out.append((self.cfg.secrets.access_token, "env"))
        if self.cache_path.exists():
            try:
                d = orjson.loads(self.cache_path.read_bytes())
                if d.get("token") and d.get("client_id") in ("", self.client_id):
                    out.append((d["token"], "cache"))
            except Exception:
                log.warning("ignoring unreadable token cache %s", self.cache_path)
        return out

    async def ensure(self, valid_until: float | None = None, allow_login: bool = True) -> str:
        """Return a token valid until ``valid_until`` (epoch), renewing / logging in if needed."""
        async with self._lock:
            now = self.clock.time()
            need = valid_until if valid_until is not None else now + 60
            best: tuple[str, str] | None = None
            best_exp = -1.0
            for tok, src in self._candidates():
                exp = jwt_expiry(tok)
                e = exp if exp is not None else float("inf")
                if e > best_exp:
                    best, best_exp = (tok, src), e
            if best and best_exp >= need:
                self._set(best[0], best[1], persist=False)
                return self.token
            if best and best_exp > now:  # still valid but not long enough: try renew
                self._set(best[0], best[1], persist=False)
                if await self._renew_locked() and (self.expires_at or float("inf")) >= need:
                    return self.token
            if allow_login and self.cfg.dhan.auth == "totp":
                await self._login_totp_locked()
                return self.token
            if best and best_exp > now:
                log.warning("token expires before the session ends (%.0f min left) and could not be renewed",
                            (best_exp - now) / 60)
                return self.token
            raise AuthError("no valid Dhan access token: paste a fresh DHAN_ACCESS_TOKEN into .env, "
                            "run `dhanbot login`, or set dhan.auth = \"totp\" with DHAN_PIN + DHAN_TOTP_SECRET")

    async def renew(self) -> bool:
        async with self._lock:
            return await self._renew_locked()

    async def _renew_locked(self) -> bool:
        if not self.token:
            return False
        url = f"{self.ep.rest_base}/RenewToken"
        headers = {"access-token": self.token, "dhanClientId": self.client_id, "client-id": self.client_id}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = orjson.loads(await r.read() or b"{}")
                    ok = r.status == 200
        except Exception as e:
            log.warning("token renew failed: %s", e)
            return False
        tok = _extract_token(data) if ok else ""
        if not tok:
            log.warning("token renew rejected: %s", data)
            return False
        self._set(tok, "renew")
        return True

    async def login_totp(self) -> str:
        async with self._lock:
            await self._login_totp_locked()
            return self.token

    async def _login_totp_locked(self) -> None:
        import pyotp

        s = self.cfg.secrets
        if not (self.client_id and s.pin and s.totp_secret):
            raise AuthError("TOTP login needs DHAN_CLIENT_ID, DHAN_PIN and DHAN_TOTP_SECRET")
        totp = pyotp.TOTP(s.totp_secret)
        remaining = 30 - (self.clock.time() % 30)
        if remaining < 3:  # don't submit a code that is about to roll over
            await self.clock.sleep(remaining + 0.5)
        code = totp.at(self.clock.time())
        url = f"{self.ep.auth_base}/app/generateAccessToken"
        params = {"dhanClientId": self.client_id, "pin": s.pin, "totp": code}
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    raw = await r.read()
                    status = r.status
        except Exception as e:
            raise AuthError(f"TOTP login request failed: {type(e).__name__}") from e
        try:
            data = orjson.loads(raw or b"{}")
        except orjson.JSONDecodeError:
            data = {}
        tok = _extract_token(data) if status == 200 else ""
        if not tok:
            msg = data.get("message") or data.get("errorMessage") if isinstance(data, dict) else ""
            raise AuthError(f"TOTP login rejected (HTTP {status}): {msg or 'no token in response'}")
        self._set(tok, "totp")

    # ---- OAuth (interactive helper for `dhanbot login --oauth`) -----------------------------
    async def oauth_consent(self) -> tuple[str, str]:
        s = self.cfg.secrets
        url = f"{self.ep.auth_base}/app/generate-consent"
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, params={"client_id": self.client_id},
                                 headers={"app_id": s.app_id, "app_secret": s.app_secret}) as r:
                data = orjson.loads(await r.read() or b"{}")
        consent = data.get("consentAppId") if isinstance(data, dict) else None
        if not consent:
            raise AuthError(f"generate-consent failed: {data}")
        return consent, f"{self.ep.auth_base}/login/consentApp-login?consentAppId={consent}"

    async def oauth_consume(self, token_id: str) -> str:
        s = self.cfg.secrets
        url = f"{self.ep.auth_base}/app/consumeApp-consent"
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, params={"tokenId": token_id},
                                headers={"app_id": s.app_id, "app_secret": s.app_secret}) as r:
                data = orjson.loads(await r.read() or b"{}")
        tok = _extract_token(data)
        if not tok:
            raise AuthError(f"consumeApp-consent failed: {data}")
        self._set(tok, "oauth")
        return tok

    def save_manual(self, token: str) -> None:
        self._set(token.strip(), "manual")
