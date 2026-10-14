"""Trading-calendar and session-date logic (pure functions, no API calls).

Two sources of calendar truth are used:

* ``data/nse_holidays.csv`` + weekends: decides whether *today* is a trading
  day and which session the dashboard is screening (the "session date").
* The dates that actually appear in cached daily candles: the authoritative
  record of which *past* days were trading sessions. This is what the
  previous-week volume uses, so unannounced closures and special sessions are
  reflected exactly as the exchange recorded them.
"""
from __future__ import annotations

import csv
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from config import MARKET_CLOSE, MARKET_OPEN

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    """Kite returns naive datetimes in IST for quotes; treat naive as IST."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def load_holidays(path: Path) -> set[date]:
    """Load exchange holidays from a CSV with a ``date`` column (YYYY-MM-DD)."""
    holidays: set[date] = set()
    if not path.exists():
        return holidays
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("date") or "").strip()
            if raw and not raw.startswith("#"):
                holidays.add(date.fromisoformat(raw))
    return holidays


def is_trading_day(d: date, holidays: set[date]) -> bool:
    return d.weekday() < 5 and d not in holidays


def previous_trading_day(d: date, holidays: set[date]) -> date:
    cur = d - timedelta(days=1)
    for _ in range(30):
        if is_trading_day(cur, holidays):
            return cur
        cur -= timedelta(days=1)
    raise ValueError(f"No trading day found in the 30 days before {d}")


def is_market_open(now: datetime, holidays: set[date]) -> bool:
    now = to_ist(now)
    if not is_trading_day(now.date(), holidays):
        return False
    return time(*MARKET_OPEN) <= now.time() <= time(*MARKET_CLOSE)


def session_date(now: datetime, holidays: set[date]) -> date:
    """The trading session being screened.

    Today if today is a trading day and the normal market has opened,
    otherwise the most recent previous trading day (a "last-session snapshot").
    """
    now = to_ist(now)
    today = now.date()
    if is_trading_day(today, holidays) and now.time() >= time(*MARKET_OPEN):
        return today
    return previous_trading_day(today, holidays)


def last_completed_trading_day(now: datetime, holidays: set[date]) -> date:
    """The most recent completed trading session on the exchange.

    If today is a trading day and normal market trading has completed (now >= MARKET_CLOSE,
    i.e. 15:30 IST), today's session has completed and today is returned.
    Otherwise (before market close on a trading day, on weekends, or on holidays),
    the previous trading day is returned. This strictly excludes in-progress daily candles.
    """
    now = to_ist(now)
    today = now.date()
    if is_trading_day(today, holidays) and now.time() >= time(*MARKET_CLOSE):
        return today
    return previous_trading_day(today, holidays)


def week_start(d: date) -> date:
    """Monday of the week containing ``d``."""
    return d - timedelta(days=d.weekday())


def derive_trading_calendar(
    dates_per_instrument: Iterable[Iterable[date]], min_fraction: float = 0.5
) -> list[date]:
    """Exchange trading days observed in cached candles.

    A date counts as a session if at least ``min_fraction`` of instruments that
    have any history have a candle on it. This filters out single-instrument
    anomalies while still capturing every real session.
    """
    counter: Counter[date] = Counter()
    n = 0
    for ds in dates_per_instrument:
        uniq = set(ds)
        if not uniq:
            continue
        n += 1
        counter.update(uniq)
    if n == 0:
        return []
    threshold = max(1, int(n * min_fraction + 0.9999))
    return sorted(d for d, c in counter.items() if c >= threshold)


def previous_completed_week(
    session: date, trading_calendar: Iterable[date], max_weeks_back: int = 4
) -> tuple[date, date, list[date]] | None:
    """Range and trading sessions of the last completed trading week.

    The week containing ``session`` is the current (incomplete) week and is
    excluded. Walks back week by week until a week with at least one recorded
    session is found (handles a fully-closed holiday week).
    Includes special trading sessions on Saturdays/weekends if present in ``trading_calendar``.
    Returns ``(monday, end_date, sessions)`` or ``None`` if nothing is found.
    """
    cal = sorted(set(trading_calendar))
    monday = week_start(session) - timedelta(days=7)
    for _ in range(max_weeks_back):
        friday = monday + timedelta(days=4)
        week_end = monday + timedelta(days=6)
        sessions = [d for d in cal if monday <= d <= week_end]
        if sessions:
            end_date = max(friday, sessions[-1])
            return monday, end_date, sessions
        monday -= timedelta(days=7)
    return None

