"""Instrument master: download / cache / index Dhan's scrip-master CSV.

* Downloaded once per day to ``data/instruments/YYYY-MM-DD.csv`` (falls back to the newest older
  file if the download fails), parsed with the stdlib ``csv`` module (no pandas), filtered to the
  segments the bot actually needs, and pickled for fast same-day restarts.
* Column names are validated up front - if Dhan changes the file the bot refuses to start with a
  clear message instead of trading the wrong security.
* ``SEM_TICK_SIZE`` units could not be confirmed offline (paise vs rupees); ``tick_unit = "auto"``
  treats values >= 1 as paise and values < 1 as rupees (so 5.0 and 0.05 both mean 5 paise).
  ``dhanbot check`` prints the resolved tick for every configured instrument - eyeball it once.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import pickle
import re
from bisect import bisect_left
from datetime import date
from pathlib import Path

import aiohttp

from .config import AppConfig, InstrumentCfg
from .dhan.endpoints import CSV_SEGMENT_MAP, SEGMENT_TO_CODE
from .models import Instrument, rupees_to_paise

log = logging.getLogger(__name__)

COL_EXCH = "SEM_EXM_EXCH_ID"
COL_SEG = "SEM_SEGMENT"
COL_ID = "SEM_SMST_SECURITY_ID"
COL_ITYPE = "SEM_INSTRUMENT_NAME"
COL_SYMBOL = "SEM_TRADING_SYMBOL"
COL_LOT = "SEM_LOT_UNITS"
COL_EXPIRY = "SEM_EXPIRY_DATE"
COL_STRIKE = "SEM_STRIKE_PRICE"
COL_OPT = "SEM_OPTION_TYPE"
COL_TICK = "SEM_TICK_SIZE"
COL_NAME = "SM_SYMBOL_NAME"
COL_CUSTOM = "SEM_CUSTOM_SYMBOL"
REQUIRED_COLUMNS = (COL_EXCH, COL_SEG, COL_ID, COL_ITYPE, COL_SYMBOL, COL_LOT, COL_EXPIRY, COL_STRIKE, COL_OPT,
                    COL_TICK)
_PICKLE_VERSION = 2


class InstrumentError(Exception):
    pass


def _tick_paise(raw: str, unit: str) -> int:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 5
    if v <= 0:
        return 5
    if unit == "paise" or (unit == "auto" and v >= 1):
        return max(1, int(round(v)))
    return max(1, int(round(v * 100)))


def _expiry(raw: str) -> date | None:
    raw = (raw or "").strip()
    if len(raw) < 10 or raw[:2] in ("NA", "-1"):
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _underlying(trading_symbol: str, name: str) -> str:
    if "-" in trading_symbol:
        return trading_symbol.split("-", 1)[0].upper()
    return (name or trading_symbol).split(" ", 1)[0].upper()


class InstrumentStore:
    def __init__(self) -> None:
        self.by_key: dict[tuple[int, int], Instrument] = {}
        self.by_symbol: dict[tuple[str, str], Instrument] = {}
        self._options: dict[tuple[str, str], list[tuple[date, int, Instrument]]] = {}
        self._futures: dict[str, list[tuple[date, Instrument]]] = {}
        self.source = ""

    def __len__(self) -> int:
        return len(self.by_key)

    # ---- building ------------------------------------------------------------------------
    def add(self, inst: Instrument) -> None:
        self.by_key[inst.key] = inst
        self.by_symbol.setdefault((inst.segment, inst.trading_symbol.upper()), inst)
        if inst.is_option and inst.expiry:
            self._options.setdefault((inst.underlying, inst.option_type), []).append(
                (inst.expiry, inst.strike_paise, inst))
        elif inst.instrument_type.startswith("FUT") and inst.expiry:
            self._futures.setdefault(inst.underlying, []).append((inst.expiry, inst))

    def _finish(self) -> None:
        for lst in self._options.values():
            lst.sort(key=lambda t: (t[0], t[1]))
        for flist in self._futures.values():
            flist.sort(key=lambda t: t[0])

    @classmethod
    def from_csv_text(cls, text: str, segments: set[str] | None = None, tick_unit: str = "auto",
                      freeze_qty: dict[str, int] | None = None) -> "InstrumentStore":
        reader = csv.DictReader(io.StringIO(text))
        cols = set(reader.fieldnames or [])
        missing = [c for c in REQUIRED_COLUMNS if c not in cols]
        if missing:
            raise InstrumentError(
                f"instrument CSV format changed: missing columns {missing}. Found: {sorted(cols)[:30]}. "
                "Update the column mapping in dhanbot/instruments.py before trading.")
        freeze_qty = {k.upper(): v for k, v in (freeze_qty or {}).items()}
        store = cls()
        bad = 0
        for row in reader:
            seg = CSV_SEGMENT_MAP.get((row[COL_EXCH].strip().upper(), row[COL_SEG].strip().upper()))
            if seg is None or (segments is not None and seg not in segments):
                continue
            try:
                sid = int(float(row[COL_ID]))
                lot = max(1, int(round(float(row[COL_LOT] or 1))))
            except (TypeError, ValueError):
                bad += 1
                continue
            sym = row[COL_SYMBOL].strip()
            name = (row.get(COL_NAME) or row.get(COL_CUSTOM) or "").strip()
            itype = row[COL_ITYPE].strip().upper()
            opt = row[COL_OPT].strip().upper()
            opt = opt if opt in ("CE", "PE") else ""
            try:
                strike = float(row[COL_STRIKE] or 0)
            except ValueError:
                strike = 0.0
            und = _underlying(sym, name) if (opt or itype.startswith("FUT")) else ""
            store.add(Instrument(
                security_id=sid, segment=seg, feed_code=SEGMENT_TO_CODE[seg], trading_symbol=sym, name=name,
                instrument_type=itype, lot_size=lot, tick_paise=_tick_paise(row[COL_TICK], tick_unit),
                expiry=_expiry(row[COL_EXPIRY]), strike_paise=rupees_to_paise(strike) if strike > 0 else 0,
                option_type=opt, underlying=und,
                freeze_qty=freeze_qty.get(sym.upper(), freeze_qty.get(und, 0)) if und or sym else 0,
            ))
        if bad:
            log.warning("instrument CSV: skipped %d malformed rows", bad)
        store._finish()
        return store

    # ---- loading with daily cache ----------------------------------------------------------
    @classmethod
    async def load(cls, cfg: AppConfig, csv_url: str, segments: set[str], today: date,
                   session: aiohttp.ClientSession | None = None) -> "InstrumentStore":
        folder = cfg.data_dir / "instruments"
        folder.mkdir(parents=True, exist_ok=True)
        tick_unit = cfg.instrument_master.tick_unit
        key = hashlib.sha1(repr((sorted(segments), tick_unit, sorted(cfg.freeze_qty.items()), csv_url,
                                 _PICKLE_VERSION)).encode()).hexdigest()[:10]
        pkl = folder / f"{today.isoformat()}.{key}.pkl"
        if pkl.exists():
            try:
                with pkl.open("rb") as fh:
                    store = pickle.load(fh)
                store.source = str(pkl)
                log.info("instruments: %d loaded from cache %s", len(store), pkl.name)
                return store
            except Exception:
                log.warning("instrument cache %s unreadable, rebuilding", pkl)
        csv_path = folder / f"{today.isoformat()}.csv"
        if not csv_path.exists():
            try:
                own = session is None
                s = session or aiohttp.ClientSession()
                try:
                    async with s.get(csv_url, timeout=aiohttp.ClientTimeout(total=120)) as r:
                        r.raise_for_status()
                        data = await r.read()
                finally:
                    if own:
                        await s.close()
                tmp = csv_path.with_suffix(".part")
                tmp.write_bytes(data)
                tmp.replace(csv_path)
                log.info("instruments: downloaded %.1f MB", len(data) / 1e6)
            except Exception as e:
                older = sorted(folder.glob("????-??-??.csv"))
                if not older:
                    raise InstrumentError(f"could not download instrument master ({e}) and no cached copy exists") from e
                csv_path = older[-1]
                log.warning("instruments: download failed (%s); using cached %s", e, csv_path.name)
        text = csv_path.read_text(encoding="utf-8", errors="replace")
        store = cls.from_csv_text(text, segments, tick_unit, cfg.freeze_qty)
        store.source = str(csv_path)
        try:
            with pkl.open("wb") as fh:
                pickle.dump(store, fh, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            log.warning("could not write instrument cache %s", pkl)
        cls._cleanup(folder, cfg.instrument_master.keep_days)
        log.info("instruments: %d indexed for segments %s", len(store), sorted(segments))
        return store

    @staticmethod
    def _cleanup(folder: Path, keep_days: int) -> None:
        days = sorted({p.name[:10] for p in folder.iterdir() if re.match(r"\d{4}-\d{2}-\d{2}", p.name)})
        for old in days[:-keep_days] if keep_days > 0 else []:
            for p in folder.glob(f"{old}*"):
                try:
                    p.unlink()
                except OSError:
                    pass

    # ---- lookups ---------------------------------------------------------------------------
    def get(self, segment: str, security_id: int) -> Instrument | None:
        return self.by_key.get((SEGMENT_TO_CODE[segment], int(security_id)))

    def by_trading_symbol(self, segment: str, symbol: str) -> Instrument | None:
        return self.by_symbol.get((segment, symbol.upper()))

    def resolve(self, icfg: InstrumentCfg) -> Instrument:
        """Resolve an [[instruments]] config entry to a concrete Instrument."""
        inst = None
        if icfg.security_id:
            inst = self.get(icfg.segment, icfg.security_id)
            if inst is None and icfg.segment == "IDX_I":
                # Indices are sometimes missing from filtered masters; they only need an id to stream.
                inst = Instrument(icfg.security_id, "IDX_I", 0, icfg.trading_symbol or icfg.alias,
                                  name=icfg.alias, instrument_type="INDEX", tick_paise=5)
                self.add(inst)
        elif icfg.trading_symbol:
            inst = self.by_trading_symbol(icfg.segment, icfg.trading_symbol)
        if inst is None:
            hint = self.search(icfg.trading_symbol or str(icfg.security_id), limit=5)
            raise InstrumentError(
                f"instrument {icfg.alias!r} ({icfg.segment} {icfg.trading_symbol or icfg.security_id}) not found. "
                f"Did you mean: {[f'{i.segment}:{i.trading_symbol}' for i in hint] or 'nothing similar'}")
        return inst

    def search(self, text: str, limit: int = 20, segment: str | None = None) -> list[Instrument]:
        t = text.upper().strip()
        if not t:
            return []
        exact: list[Instrument] = []
        prefix: list[Instrument] = []
        contains: list[Instrument] = []
        for (seg, sym), inst in self.by_symbol.items():
            if segment and seg != segment:
                continue
            if sym == t:
                exact.append(inst)
            elif sym.startswith(t):
                prefix.append(inst)
            elif t in sym or t in inst.name.upper():
                contains.append(inst)
            if len(exact) + len(prefix) >= limit * 4:
                break
        prefix.sort(key=lambda i: (len(i.trading_symbol), i.expiry or date.min, i.strike_paise))
        return (exact + prefix + contains)[:limit]

    def expiries(self, underlying: str, kind: str = "CE") -> list[date]:
        return sorted({e for e, _, _ in self._options.get((underlying.upper(), kind.upper()), [])})

    def future(self, underlying: str, expiry: str | date = "nearest", today: date | None = None) -> Instrument:
        lst = self._futures.get(underlying.upper(), [])
        today = today or date.today()
        live = [(e, i) for e, i in lst if e >= today]
        if not live:
            raise InstrumentError(f"no futures for {underlying}")
        if isinstance(expiry, date):
            for e, i in live:
                if e == expiry:
                    return i
            raise InstrumentError(f"no {underlying} future expiring {expiry}")
        idx = {"nearest": 0, "next": 1, "far": 2}.get(expiry, 0)
        return live[min(idx, len(live) - 1)][1]

    def option(self, underlying: str, kind: str, spot: float, expiry: str | date = "nearest",
               strike: str | int | float = "ATM", today: date | None = None) -> Instrument:
        """Resolve an option contract.

        ``expiry``: "nearest" | "next" | "far" | a ``date``.
        ``strike``: "ATM", "ATM+2" / "ATM-1" (N strikes above / below ATM, regardless of CE/PE), or an
        absolute strike in rupees.
        """
        kind = kind.upper()
        und = underlying.upper()
        lst = self._options.get((und, kind))
        if not lst:
            raise InstrumentError(f"no {kind} options for {underlying}")
        today = today or date.today()
        exps = sorted({e for e, _, _ in lst if e >= today})
        if not exps:
            raise InstrumentError(f"no unexpired {kind} options for {underlying}")
        if isinstance(expiry, date):
            if expiry not in exps:
                raise InstrumentError(f"{underlying} has no {kind} expiry on {expiry}; available: {exps[:5]}")
            exp = expiry
        else:
            idx = {"nearest": 0, "next": 1, "far": 2}.get(str(expiry), None)
            if idx is None:
                raise InstrumentError("expiry must be nearest | next | far | a date")
            exp = exps[min(idx, len(exps) - 1)]
        chain = [(k, inst) for e, k, inst in lst if e == exp]
        strikes = [k for k, _ in chain]
        if isinstance(strike, (int, float)):
            want = rupees_to_paise(float(strike))
            for k, inst in chain:
                if k == want:
                    return inst
            raise InstrumentError(f"{underlying} {exp} {kind}: strike {strike} not listed")
        m = re.fullmatch(r"ATM(?:([+-])(\d+))?", str(strike).upper().replace(" ", ""))
        if not m:
            raise InstrumentError("strike must be ATM, ATM+N, ATM-N or a number")
        spot_p = rupees_to_paise(spot)
        pos = bisect_left(strikes, spot_p)
        cands = [i for i in (pos - 1, pos) if 0 <= i < len(strikes)]
        atm = min(cands, key=lambda i: (abs(strikes[i] - spot_p), i))
        off = int(m.group(2) or 0) * (1 if m.group(1) != "-" else -1)
        target = atm + off
        if not 0 <= target < len(chain):
            raise InstrumentError(f"{strike} is outside the listed strike range for {underlying} {exp}")
        return chain[target][1]
