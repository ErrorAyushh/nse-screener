"""Configuration loaded from environment variables (.env) with safe defaults.

Secrets (API key/secret/access token) are only ever read from the environment.
They are never logged, printed, or rendered in the dashboard.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"

# --- Documented Kite Connect limits (verified against kite.trade/docs/connect/v3) ---
# Rate limits: quote 1 req/s, historical 3 req/s, other endpoints 10 req/s.
# /quote accepts up to 500 instruments per call; /quote/ohlc and /quote/ltp up to 1000.
# WebSocket: 3000 instruments per connection, 3 connections per API key.
QUOTE_BATCH_SIZE = 500
QUOTE_REQUESTS_PER_SECOND = 1.0
HISTORICAL_REQUESTS_PER_SECOND = 3.0
OTHER_REQUESTS_PER_SECOND = 10.0

# NSE normal-market session (IST).
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)

# Default screening thresholds (all comparisons are STRICTLY greater-than).
DEFAULT_MIN_MARKET_CAP_CR = 1000.0
DEFAULT_VOLUME_MULTIPLIER = 4.0
SMA_WINDOW = 50


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = float(raw)
        if val != val or abs(val) == float("inf"):
            raise ValueError(f"Environment variable {name} must be finite, got {raw!r}")
        return val
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))



def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return list(default)
    return [x.strip().upper() for x in raw.split(",") if x.strip()]


@dataclass
class Settings:
    api_key: str = ""
    api_secret: str = ""
    access_token: str = ""
    access_token_created_at: str = ""

    data_dir: Path = PROJECT_ROOT / "data"
    cache_dir: Path = PROJECT_ROOT / "data" / "cache"
    market_cap_csv: Path = PROJECT_ROOT / "data" / "market_cap.csv"
    holidays_csv: Path = PROJECT_ROOT / "data" / "nse_holidays.csv"

    min_market_cap_cr: float = DEFAULT_MIN_MARKET_CAP_CR
    volume_multiplier: float = DEFAULT_VOLUME_MULTIPLIER
    refresh_interval_seconds: int = 60
    min_refresh_interval_seconds: int = 15

    # A market-cap record older than this many days is STALE and cannot qualify.
    market_cap_max_age_days: int = 190
    # During market hours, a quote whose exchange timestamp is older than this is STALE.
    quote_stale_seconds: int = 300
    # Allowed relative gap between quote prev-close and the historical close of the
    # previous session before Condition 3 is marked as a data issue.
    prev_close_tolerance_pct: float = 1.0
    # Calendar days of daily history to request on a first (cold) cache build.
    history_lookback_days: int = 140
    # Only download history for instruments that have a market-cap record
    # (instruments without one can never qualify).
    history_only_with_market_cap: bool = True

    # Instrument-universe filter. Kite NSE dump uses tradingsymbol suffixes for
    # non-EQ series (e.g. "-BE", "-SM", "-GB"). Plain symbols are EQ series.
    allowed_series_suffixes: list[str] = field(default_factory=list)

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.access_token)


def load_settings(env_path: Path | None = None, override: bool = False) -> Settings:
    """Load settings. ``override=True`` re-reads .env (e.g. after a fresh login)."""
    load_dotenv(env_path or ENV_PATH, override=override)
    s = Settings(
        api_key=os.getenv("KITE_API_KEY", "").strip(),
        api_secret=os.getenv("KITE_API_SECRET", "").strip(),
        access_token=os.getenv("KITE_ACCESS_TOKEN", "").strip(),
        access_token_created_at=os.getenv("KITE_ACCESS_TOKEN_CREATED_AT", "").strip(),
        min_market_cap_cr=_env_float("MIN_MARKET_CAP_CR", DEFAULT_MIN_MARKET_CAP_CR),
        volume_multiplier=_env_float("VOLUME_MULTIPLIER", DEFAULT_VOLUME_MULTIPLIER),
        refresh_interval_seconds=_env_int("REFRESH_INTERVAL_SECONDS", 60),
        market_cap_max_age_days=_env_int("MARKET_CAP_MAX_AGE_DAYS", 190),
        quote_stale_seconds=_env_int("QUOTE_STALE_SECONDS", 300),
        prev_close_tolerance_pct=_env_float("PREV_CLOSE_TOLERANCE_PCT", 1.0),
        history_lookback_days=_env_int("HISTORY_LOOKBACK_DAYS", 140),
        history_only_with_market_cap=_env_bool("HISTORY_ONLY_WITH_MARKET_CAP", True),
        allowed_series_suffixes=_env_list("ALLOWED_SERIES_SUFFIXES", []),
    )
    mc = os.getenv("MARKET_CAP_CSV")
    if mc:
        p = Path(mc)
        s.market_cap_csv = p if p.is_absolute() else PROJECT_ROOT / p
    s.min_market_cap_cr = max(0.0, s.min_market_cap_cr)
    s.volume_multiplier = max(0.0, s.volume_multiplier)
    s.refresh_interval_seconds = max(s.refresh_interval_seconds, s.min_refresh_interval_seconds)
    s.market_cap_max_age_days = max(1, s.market_cap_max_age_days)
    s.quote_stale_seconds = max(10, s.quote_stale_seconds)
    s.history_lookback_days = max(50, s.history_lookback_days)
    s.cache_dir.mkdir(parents=True, exist_ok=True)
    return s
