"""SMA and weekly-volume calculations on daily candles (pure functions).

Candles are a DataFrame with at least ``date`` (datetime.date), ``close`` and
``volume`` columns. Every function takes the *session date* being screened
and only ever uses candles strictly before it, so today's in-progress candle
can never leak into a "completed" calculation.

Each function returns an ``IndicatorResult``: a value, or ``None`` together
with a human-readable reason. Missing data is never replaced by zero.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Iterable

import pandas as pd

from calendar_utils import previous_completed_week


@dataclass(frozen=True)
class IndicatorResult:
    value: float | None
    reason: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None


def normalize_candles(candles: pd.DataFrame | None) -> pd.DataFrame:
    """Return candles sorted by date, de-duplicated, with valid closes only."""
    if candles is None or candles.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = candles.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    df = df[df["close"] > 0]
    df = df.sort_values("date").drop_duplicates(subset="date", keep="last")
    return df.reset_index(drop=True)


def completed_candles(candles: pd.DataFrame | None, session: date) -> pd.DataFrame:
    """Candles strictly before the session date (excludes today's partial candle)."""
    df = normalize_candles(candles)
    return df[df["date"] < session].reset_index(drop=True)


def sma_completed(candles: pd.DataFrame | None, session: date, window: int = 50) -> IndicatorResult:
    """Simple moving average of the last ``window`` completed daily closes."""
    df = completed_candles(candles, session)
    if len(df) < window:
        return IndicatorResult(None, "insufficient_history", f"{len(df)} of {window} completed closes")
    closes = df["close"].iloc[-window:].astype(float)
    if (closes <= 0).any():
        return IndicatorResult(None, "invalid_close", "non-positive close in SMA window")
    return IndicatorResult(
        float(closes.mean()),
        detail=f"{df['date'].iloc[-window]} to {df['date'].iloc[-1]}",
    )


def last_completed_close(candles: pd.DataFrame | None, session: date) -> IndicatorResult:
    df = completed_candles(candles, session)
    if df.empty:
        return IndicatorResult(None, "no_history")
    val = float(df["close"].iloc[-1])
    if val <= 0 or not math.isfinite(val):
        return IndicatorResult(None, "invalid_close", f"non-positive close {val}")
    return IndicatorResult(val, detail=str(df["date"].iloc[-1]))


def previous_week_volume(
    candles: pd.DataFrame | None, session: date, trading_calendar: Iterable[date]
) -> IndicatorResult:
    """Total traded volume over the last completed Mon–Fri trading week.

    The exchange's sessions in that week come from ``trading_calendar``. If the
    instrument is missing a candle (or has a missing volume) for any of those
    sessions, the result is ``None``: summing a partial week would understate
    the base and could falsely pass the volume-expansion condition.
    """
    week = previous_completed_week(session, trading_calendar)
    if week is None:
        return IndicatorResult(None, "no_completed_week", "no trading week found in calendar")
    monday, end_date, sessions = week
    df = completed_candles(candles, session)
    in_week = df[(df["date"] >= monday) & (df["date"] <= end_date)]
    by_date = dict(zip(in_week["date"], in_week["volume"]))
    missing = [d for d in sessions if d not in by_date or pd.isna(by_date[d]) or float(by_date[d]) < 0]
    label = f"{monday:%d %b} to {end_date:%d %b %Y} ({len(sessions)} sessions)"
    if missing:
        return IndicatorResult(
            None,
            "incomplete_week_history",
            f"{label}; missing {', '.join(d.isoformat() for d in missing)}",
        )
    total = float(sum(float(by_date[d]) for d in sessions))
    return IndicatorResult(total, detail=label)


def calculate_time_adjusted_volume(
    today_volume: float | None,
    comparable_hist_volume: float | None = None,
) -> tuple[float | None, str]:
    """Calculate time-adjusted volume against comparable historical intraday data.

    Meaning:
        Compares today's cumulative traded volume up to time `t` against the historical
        cumulative volume traded up to the exact same time of day `t` (e.g. 5-minute
        or 1-minute historical intraday bars accumulated over the previous week).
        Formula: today_volume / comparable_hist_volume

    Limitations of Linear Prorating:
        Assuming uniform volume arrival (elapsed_minutes / 375) produces severe distortion
        because equity markets exhibit an empirical U-shaped volume curve (surges at
        market open and market-on-close, lull mid-day).
        Furthermore, the current local database stores only daily candles, so true
        minute-by-minute intraday profile baselines are not stored locally.

    Rule:
        If comparable historical intraday data is provided, calculate the true ratio.
        If comparable historical intraday data is unavailable (None or <= 0), return
        None and "N/A (Intraday history unavailable)" rather than inventing an artificial value.
    """
    if today_volume is None or today_volume < 0:
        return None, "N/A (Missing today volume)"
    if comparable_hist_volume is None or comparable_hist_volume <= 0:
        return None, "N/A (Intraday history unavailable)"

    multiple = round(today_volume / comparable_hist_volume, 2)
    formula = f"{today_volume:,.0f} / {comparable_hist_volume:,.0f} (Historical Same-Time Baseline)"
    return multiple, formula


def calculate_linear_session_pace(
    today_volume: float | None,
    prev_week_volume: float | None,
    now: datetime,
    market_open: bool,
) -> tuple[float | None, str, float]:
    """Calculate linear session pace heuristic (Unadjusted for intraday U-shape volume curve).

    Meaning:
        Prorates the previous completed week's volume linearly by elapsed session fraction
        f = elapsed_minutes / 375 (from 09:15 to 15:30 IST).

    Limitations:
        - Severe morning distortion: volume is concentrated in the opening 45 minutes;
          linear scaling makes normal morning volume look like an extreme outlier.
        - Ignores afternoon/closing surge and mid-day lull.
        - Uses a 5-day aggregate volume rather than a time-matched historical intraday curve.
    """
    if today_volume is None or prev_week_volume is None or prev_week_volume <= 0 or today_volume < 0:
        return None, "N/A (insufficient volume data)", 1.0

    if not market_open:
        ratio = round(today_volume / prev_week_volume, 2)
        desc = f"Full Session: {today_volume:,.0f} / {prev_week_volume:,.0f}"
        return ratio, desc, 1.0

    from calendar_utils import to_ist
    ist_now = to_ist(now)
    open_minutes = 9 * 60 + 15   # 09:15 IST
    close_minutes = 15 * 60 + 30  # 15:30 IST
    total_minutes = close_minutes - open_minutes  # 375 minutes

    cur_minutes = ist_now.hour * 60 + ist_now.minute
    elapsed = max(1, min(total_minutes, cur_minutes - open_minutes))
    frac = max(0.05, min(1.0, elapsed / float(total_minutes)))

    expected_prorata = prev_week_volume * frac
    if expected_prorata <= 0:
        return None, "N/A (zero baseline)", frac

    pace = round(today_volume / expected_prorata, 2)
    desc = (
        f"Linear Pace (Heuristic): {today_volume:,.0f} / "
        f"[{prev_week_volume:,.0f} * {frac:.1%} elapsed ({elapsed}/{total_minutes}m)]"
    )
    return pace, desc, frac


def prepare_chart_data(
    candles: pd.DataFrame | None,
    sma_window: int = 50,
    display_tail: int = 90,
    session: date | None = None,
) -> pd.DataFrame:
    """Prepare candlestick and rolling SMA series for charting.

    Ensures:
    1. Daily candles are sorted by date ascending and deduplicated.
    2. Rolling 50-session SMA is computed strictly on completed daily closes.
       When `session` is supplied, only candles strictly before `session`
       are included (preventing any partial intraday bar from polluting the series).
    3. `sma50` is computed on the full completed history prior to tail-slicing,
       producing a mathematically distinct series from `close`.
    4. Slices to `display_tail` sessions for clean rendering.
    """
    if candles is None or candles.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "sma50"])

    df = completed_candles(candles, session) if session is not None else normalize_candles(candles)
    if df.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "sma50"])

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["sma50"] = df["close"].rolling(window=sma_window, min_periods=sma_window).mean()

    if display_tail and len(df) > display_tail:
        df = df.tail(display_tail).reset_index(drop=True)

    return df



