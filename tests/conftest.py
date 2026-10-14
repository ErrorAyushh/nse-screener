"""Shared fixtures: synthetic candles, quotes and market-cap tables (no network)."""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_utils import IST  # noqa: E402
from market_cap import parse_market_cap_frame  # noqa: E402
from screener import Quote, ScreenContext, ScreenParams, StockInput  # noqa: E402

HOLIDAYS = {date(2026, 10, 2), date(2026, 10, 20)}
SESSION = date(2026, 10, 12)  # Monday
LAST_COMPLETED = date(2026, 10, 9)  # Friday
NOW = datetime(2026, 10, 12, 11, 0, tzinfo=IST)


def trading_days_before(end: date, n: int, holidays=HOLIDAYS) -> list[date]:
    out, d = [], end - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5 and d not in holidays:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def make_candles(days: list[date], closes, volumes) -> pd.DataFrame:
    if not isinstance(closes, list):
        closes = [closes] * len(days)
    if not isinstance(volumes, list):
        volumes = [volumes] * len(days)
    return pd.DataFrame(
        {"date": days, "open": closes, "high": closes, "low": closes, "close": closes, "volume": volumes}
    )


def make_quote(ltp=110.0, prev_close=100.0, volume=25_000, ts=None, ltt=None) -> Quote:
    ts = ts or datetime(2026, 10, 12, 10, 59, 30)  # naive = IST, like Kite
    ltt = ltt or datetime(2026, 10, 12, 10, 59, 0)
    return Quote(last_price=ltp, prev_close=prev_close, volume=volume, timestamp=ts, last_trade_time=ltt)


def mcap_table(rows: list[dict]):
    return parse_market_cap_frame(pd.DataFrame(rows))


@pytest.fixture
def days():
    return trading_days_before(SESSION, 70)


@pytest.fixture
def ctx(days):
    return ScreenContext(
        now=NOW, session=SESSION, expected_last_completed=LAST_COMPLETED, market_open=True, trading_calendar=days
    )


@pytest.fixture
def params():
    return ScreenParams()


@pytest.fixture
def mcap():
    return mcap_table(
        [
            {"symbol": "GOOD", "market_cap_cr": "5000", "as_of_date": "2026-09-30", "source": "test"},
            {"symbol": "SMALL", "market_cap_cr": "500", "as_of_date": "2026-09-30", "source": "test"},
            {"symbol": "EDGE", "market_cap_cr": "1000", "as_of_date": "2026-09-30", "source": "test"},
            {"symbol": "OLD", "market_cap_cr": "5000", "as_of_date": "2025-01-31", "source": "test"},
        ]
    )


@pytest.fixture
def base_stock(days):
    """Close 100 and volume 1,000 every day: SMA 100, previous week 5,000."""
    return StockInput(1, "GOOD", "Good Ltd", make_quote(), make_candles(days, 100.0, 1000))
