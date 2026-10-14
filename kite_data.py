"""Kite Connect access: authentication, instrument universe, quotes and history.

All network access lives here so the screening logic can be tested offline.
Read-only market-data endpoints only — this module never places or modifies
orders and imports no order functionality.

Documented limits honoured here (kite.trade/docs/connect/v3):
* /quote: max 500 instruments per call, 1 request/second
* historical candles: 3 requests/second
* other endpoints: 10 requests/second
* access tokens expire at 06:00 IST the next day (regulatory)
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, TypeVar

import pandas as pd

from calendar_utils import IST, now_ist, to_ist
from config import (
    HISTORICAL_REQUESTS_PER_SECOND,
    OTHER_REQUESTS_PER_SECOND,
    QUOTE_BATCH_SIZE,
    QUOTE_REQUESTS_PER_SECOND,
    Settings,
)
from screener import Quote

log = logging.getLogger(__name__)
T = TypeVar("T")

HISTORY_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


class AuthError(RuntimeError):
    """Access token missing, invalid or expired. User must log in again."""


class DataPermissionError(RuntimeError):
    """The API key's subscription does not permit this data (e.g. historical)."""


class DataUnavailableError(RuntimeError):
    """A request failed after retries."""


# --------------------------------------------------------------------------
# Rate limiting and retries
# --------------------------------------------------------------------------
class RateLimiter:
    """Thread-safe minimum-interval limiter (slightly under the documented cap)."""

    def __init__(self, per_second: float, safety: float = 0.85):
        self.interval = 1.0 / (per_second * safety)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.interval


QUOTE_LIMITER = RateLimiter(QUOTE_REQUESTS_PER_SECOND)
HIST_LIMITER = RateLimiter(HISTORICAL_REQUESTS_PER_SECOND)
OTHER_LIMITER = RateLimiter(OTHER_REQUESTS_PER_SECOND)


def _classify(exc: Exception) -> str:
    """Return 'auth', 'permission', 'retry' or 'fatal' for an exception."""
    try:
        from kiteconnect import exceptions as kex
    except ImportError:  # pragma: no cover
        kex = None
    if kex is not None:
        if isinstance(exc, kex.TokenException):
            return "auth"
        if isinstance(exc, kex.PermissionException):
            return "permission"
        if isinstance(exc, (kex.NetworkException, kex.DataException, kex.GeneralException)):
            return "retry"
        if isinstance(exc, kex.InputException):
            return "fatal"
    try:
        import requests

        if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return "retry"
    except ImportError:  # pragma: no cover
        pass
    return "fatal"


def sanitize_error(msg: str | Exception) -> str:
    """Mask credentials and access tokens in error messages."""
    if not msg:
        return ""
    import re

    s = str(msg)
    s = re.sub(
        r"(api[_-]?key|access[_-]?token|api[_-]?secret|request[_-]?token|token|secret)\s*[:=]\s*['\"]?[a-zA-Z0-9_\-]+['\"]?",
        r"\1=***",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"(token|bearer)\s+[a-zA-Z0-9_\-]+", r"\1 ***", s, flags=re.IGNORECASE)
    return s


def call_with_retry(
    fn: Callable[[], T],
    limiter: RateLimiter,
    attempts: int = 4,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    last: Exception | None = None
    for i in range(attempts):
        limiter.wait()
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classified below
            kind = _classify(exc)
            if kind == "auth":
                raise AuthError(
                    "Kite access token is invalid or expired. Run `python auth_login.py` to log in again."
                ) from None
            if kind == "permission":
                raise DataPermissionError(
                    f"Kite denied access ({type(exc).__name__}). Check that your app has the "
                    "Connect (market data) subscription."
                ) from None
            if kind == "fatal":
                raise DataUnavailableError(f"{type(exc).__name__}: {sanitize_error(str(exc))}") from None
            last = exc
            delay = base_delay * (2**i)
            clean_exc = sanitize_error(str(exc))
            log.warning("Kite request failed (%s: %s); retry %d/%d in %.1fs", type(exc).__name__, clean_exc, i + 1, attempts, delay)
            sleep(delay)
    clean_last = sanitize_error(str(last))
    raise DataUnavailableError(f"Request failed after {attempts} attempts: {type(last).__name__}: {clean_last}")


# --------------------------------------------------------------------------
# Client / auth
# --------------------------------------------------------------------------
def token_probably_expired(created_at_iso: str | None, now: datetime | None = None) -> bool:
    """Kite tokens expire at 06:00 IST the day after login."""
    if not created_at_iso:
        return False  # unknown: let the API validate it
    try:
        created = to_ist(datetime.fromisoformat(created_at_iso))
    except ValueError:
        return False
    now = to_ist(now or now_ist())
    expiry = datetime.combine(created.date() + timedelta(days=1), datetime.min.time(), IST).replace(hour=6)
    if created.hour < 6:
        expiry = created.replace(hour=6, minute=0, second=0, microsecond=0)
    return now >= expiry


def make_client(settings: Settings):
    if not settings.api_key:
        raise AuthError("KITE_API_KEY is not set. Copy .env.example to .env and fill it in.")
    if not settings.access_token:
        raise AuthError("KITE_ACCESS_TOKEN is not set. Run `python auth_login.py` to log in.")
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=settings.api_key, timeout=15)
    kite.set_access_token(settings.access_token)
    return kite


def verify_session(kite) -> str:
    """Cheap authenticated call; returns the user's id (never the token)."""
    profile = call_with_retry(kite.profile, OTHER_LIMITER, attempts=2)
    return str(profile.get("user_id", ""))


# --------------------------------------------------------------------------
# Instrument universe
# --------------------------------------------------------------------------
def filter_equity_universe(instruments: pd.DataFrame, allowed_suffixes: Iterable[str] = ()) -> tuple[pd.DataFrame, dict]:
    """Keep NSE cash-equity instruments.

    * exchange NSE, segment NSE, instrument_type EQ (drops indices, F&O, CDS)
    * tradingsymbols with a series suffix ("-BE", "-SM", "-GB", ...) are dropped
      unless the suffix is listed in ``allowed_suffixes``
    * duplicate tradingsymbols are dropped entirely (ambiguous)
    """
    allowed = {s.strip().upper().lstrip("-") for s in allowed_suffixes}
    df = instruments.copy()
    stats = {"raw": len(df)}
    df = df[(df["exchange"] == "NSE") & (df["segment"] == "NSE") & (df["instrument_type"] == "EQ")]
    stats["eq_type"] = len(df)
    sym = df["tradingsymbol"].astype(str)
    suffix = sym.str.extract(r"-([A-Z0-9]+)$", expand=False)
    keep = suffix.isna() | suffix.isin(allowed)
    df = df[keep]
    stats["after_series_filter"] = len(df)
    dups = df["tradingsymbol"].duplicated(keep=False)
    stats["duplicates_dropped"] = int(dups.sum())
    df = df[~dups]
    df = df[["instrument_token", "tradingsymbol", "name", "exchange"]].copy()
    df["instrument_token"] = df["instrument_token"].astype(int)
    df["name"] = df["name"].fillna("").astype(str)
    stats["eligible"] = len(df)
    return df.sort_values("tradingsymbol").reset_index(drop=True), stats


def load_instruments(kite, cache_dir: Path, today: date, force: bool = False) -> pd.DataFrame:
    """NSE instrument dump, cached once per day on disk."""
    path = cache_dir / f"instruments_NSE_{today.isoformat()}.csv"
    if path.exists() and not force:
        return pd.read_csv(path)
    rows = call_with_retry(lambda: kite.instruments("NSE"), OTHER_LIMITER)
    df = pd.DataFrame(rows)
    if df.empty:
        raise DataUnavailableError("Kite returned an empty NSE instrument list")
    df.to_csv(path, index=False)
    for old in cache_dir.glob("instruments_NSE_*.csv"):
        if old != path:
            old.unlink(missing_ok=True)
    return df


# --------------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------------
def parse_quote(raw: dict | None) -> Quote | None:
    if not raw:
        return None
    ohlc = raw.get("ohlc") or {}

    def num(x):
        try:
            v = float(x)
            return v if v == v else None  # NaN guard
        except (TypeError, ValueError):
            return None

    def dt(x):
        if isinstance(x, datetime):
            return x
        if isinstance(x, str) and x:
            try:
                return datetime.fromisoformat(x)
            except ValueError:
                return None
        return None

    return Quote(
        last_price=num(raw.get("last_price")),
        prev_close=num(ohlc.get("close")),
        volume=num(raw.get("volume")),
        timestamp=dt(raw.get("timestamp")),
        last_trade_time=dt(raw.get("last_trade_time")),
    )


def fetch_quotes(kite, symbols: list[str], progress: Callable[[float], None] | None = None) -> dict[str, Quote | None]:
    """Full quotes for NSE symbols in batches of 500 at <=1 request/second.

    Returns symbol -> Quote (or None if Kite returned nothing for it).
    """
    out: dict[str, Quote | None] = {}
    batches = [symbols[i : i + QUOTE_BATCH_SIZE] for i in range(0, len(symbols), QUOTE_BATCH_SIZE)]
    for n, batch in enumerate(batches, start=1):
        keys = [f"NSE:{s}" for s in batch]
        data = call_with_retry(lambda keys=keys: kite.quote(keys), QUOTE_LIMITER)
        for s, k in zip(batch, keys):
            out[s] = parse_quote(data.get(k))
        if progress:
            progress(n / len(batches))
    return out


# --------------------------------------------------------------------------
# Historical candle cache
# --------------------------------------------------------------------------
@dataclass
class HistoryRefreshReport:
    requested: int = 0
    fetched: int = 0
    skipped_fresh: int = 0
    full_refetch: int = 0
    errors: dict[int, str] = field(default_factory=dict)


class HistoryCache:
    """Daily candles per instrument, stored as CSV under data/cache/history.

    ``_meta.json`` records, per token, the last session date the cache was
    fetched through, so instruments that did not trade (suspended) are not
    re-requested on every refresh.
    """

    def __init__(self, cache_dir: Path):
        self.dir = cache_dir / "history"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.dir / "_meta.json"
        self.meta: dict[str, dict] = {}
        if self.meta_path.exists():
            try:
                self.meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.meta = {}

    def _path(self, token: int) -> Path:
        return self.dir / f"{int(token)}.csv"

    def load(self, token: int) -> pd.DataFrame | None:
        p = self._path(token)
        if not p.exists():
            return None
        df = pd.read_csv(p)
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df

    def fetched_through(self, token: int) -> date | None:
        m = self.meta.get(str(int(token)))
        return date.fromisoformat(m["through"]) if m and m.get("through") else None

    def error(self, token: int) -> str:
        m = self.meta.get(str(int(token)))
        return (m or {}).get("error", "")

    def save_meta(self) -> None:
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.meta, indent=0), encoding="utf-8")
        tmp.replace(self.meta_path)

    @staticmethod
    def _to_frame(rows: list[dict]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"]).map(lambda x: to_ist(x.to_pydatetime()).date())
        return df[HISTORY_COLUMNS]

    def refresh(
        self,
        kite,
        tokens: list[int],
        through: date,
        lookback_days: int,
        force: bool = False,
        progress: Callable[[float, str], None] | None = None,
    ) -> HistoryRefreshReport:
        """Ensure each token has completed daily candles through ``through``.

        ``through`` must be the last *completed* session, so today's partial
        candle is never requested. Incremental fetches overlap the last 10 days;
        if overlapping closes changed (split/bonus adjustment) the full lookback
        is re-downloaded.
        """
        rep = HistoryRefreshReport(requested=len(tokens))
        log.info(
            "Starting history refresh for %d instruments through %s (lookback: %d days, force=%s)",
            len(tokens),
            through,
            lookback_days,
            force,
        )
        for i, token in enumerate(tokens, start=1):
            key = str(int(token))
            ft = self.fetched_through(token)
            if not force and ft is not None and ft >= through and not self.error(token):
                rep.skipped_fresh += 1
                log.debug("Token %d: skipped (already fresh through %s)", token, ft)
                if progress:
                    progress(i / len(tokens), f"{i}/{len(tokens)}")
                continue
            existing = None if force else self.load(token)
            full_start = through - timedelta(days=lookback_days)
            start = full_start
            if existing is not None and not existing.empty:
                start = max(full_start, existing["date"].max() - timedelta(days=10))
            log.info("Token %d: attempting history fetch (start=%s, through=%s)", token, start, through)
            try:
                rows = self._fetch(kite, token, start, through)
                new = self._to_frame(rows)
                if existing is not None and not existing.empty and start != full_start:
                    if self._adjusted(existing, new):
                        log.info("Token %d: corporate action adjustment detected, refetching full lookback", token)
                        rows = self._fetch(kite, token, full_start, through)
                        new = self._to_frame(rows)
                        existing = None
                        rep.full_refetch += 1
                merged = new if existing is None or existing.empty else pd.concat([existing, new])
                merged = merged.drop_duplicates(subset="date", keep="last").sort_values("date")
                merged = merged[merged["date"] >= full_start - timedelta(days=30)]
                target_path = self._path(token)
                tmp_path = target_path.with_suffix(".tmp")
                merged.to_csv(tmp_path, index=False)
                tmp_path.replace(target_path)
                self.meta[key] = {"through": through.isoformat(), "fetched_at": now_ist().isoformat()}
                rep.fetched += 1
                log.info("Token %d: successfully updated history through %s (%d candles)", token, through, len(merged))
            except (AuthError, DataPermissionError):
                log.error("Token %d: fatal auth or permission error during history refresh", token)
                self.save_meta()
                raise
            except DataUnavailableError as exc:
                clean_err = sanitize_error(str(exc))
                rep.errors[int(token)] = clean_err
                self.meta[key] = {**self.meta.get(key, {}), "error": clean_err[:200]}
                log.warning("Token %d: history fetch failed: %s", token, clean_err)
            if progress:
                progress(i / len(tokens), f"{i}/{len(tokens)}")
            if i % 100 == 0:
                self.save_meta()
        self.save_meta()
        log.info(
            "History refresh complete: requested=%d, fetched=%d, skipped=%d, full_refetch=%d, errors=%d",
            rep.requested,
            rep.fetched,
            rep.skipped_fresh,
            rep.full_refetch,
            len(rep.errors),
        )
        return rep

    @staticmethod
    def _adjusted(existing: pd.DataFrame, new: pd.DataFrame, tol: float = 0.005) -> bool:
        m = existing.merge(new, on="date", suffixes=("_old", "_new"))
        if m.empty:
            return False
        rel = (m["close_old"] - m["close_new"]).abs() / m["close_new"].abs()
        return bool((rel > tol).any())

    @staticmethod
    def _fetch(kite, token: int, start: date, end: date) -> list[dict]:
        from_dt = datetime.combine(start, datetime.min.time())
        to_dt = datetime.combine(end, datetime.max.time()).replace(microsecond=0)
        return call_with_retry(lambda: kite.historical_data(int(token), from_dt, to_dt, "day"), HIST_LIMITER)
