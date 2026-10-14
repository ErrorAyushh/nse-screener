"""Calendar handling, previous-week volume and SMA exclusion of today's candle."""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from calendar_utils import (
    IST,
    derive_trading_calendar,
    is_market_open,
    previous_completed_week,
    previous_trading_day,
    session_date,
)
from conftest import HOLIDAYS, make_candles, trading_days_before
from indicators import (
    calculate_linear_session_pace,
    calculate_time_adjusted_volume,
    last_completed_close,
    prepare_chart_data,
    previous_week_volume,
    sma_completed,
)


def ist(*a):
    return datetime(*a, tzinfo=IST)


# 8. Weekends, holidays, incomplete weeks -----------------------------------
def test_weekend_uses_last_friday_session():
    assert session_date(ist(2026, 10, 11, 15, 0), HOLIDAYS) == date(2026, 10, 9)  # Sunday
    assert session_date(ist(2026, 10, 10, 12, 0), HOLIDAYS) == date(2026, 10, 9)  # Saturday
    assert not is_market_open(ist(2026, 10, 11, 11, 0), HOLIDAYS)


def test_holiday_uses_previous_trading_day():
    assert session_date(ist(2026, 10, 2, 11, 0), HOLIDAYS) == date(2026, 10, 1)  # Gandhi Jayanti
    assert not is_market_open(ist(2026, 10, 2, 11, 0), HOLIDAYS)
    assert previous_trading_day(date(2026, 10, 5), HOLIDAYS) == date(2026, 10, 1)  # skips Fri holiday + weekend


def test_before_open_uses_previous_session_and_open_uses_today():
    assert session_date(ist(2026, 10, 12, 9, 0), HOLIDAYS) == date(2026, 10, 9)
    assert session_date(ist(2026, 10, 12, 9, 15), HOLIDAYS) == date(2026, 10, 12)
    assert is_market_open(ist(2026, 10, 12, 9, 15), HOLIDAYS)
    assert is_market_open(ist(2026, 10, 12, 15, 30), HOLIDAYS)
    assert not is_market_open(ist(2026, 10, 12, 15, 31), HOLIDAYS)


def test_previous_week_is_calendar_week_not_rolling_window():
    session = date(2026, 10, 14)  # Wednesday
    days = trading_days_before(session, 40)
    # Volume per day = day-of-month so the sum identifies exactly which days were used.
    candles = make_candles(days, 100.0, [d.day for d in days])
    r = previous_week_volume(candles, session, days)
    assert r.value == 5 + 6 + 7 + 8 + 9  # Mon 5 Oct – Fri 9 Oct only
    # A rolling 7-day window would have included Mon 12 and Tue 13 of the current week.
    assert r.value != sum(d.day for d in days if (session - d).days <= 7)


def test_previous_week_with_exchange_holiday_counts_only_sessions():
    session = date(2026, 10, 7)  # Wednesday; previous week 28 Sep – 2 Oct, 2 Oct is a holiday
    days = trading_days_before(session, 40)
    assert date(2026, 10, 2) not in days
    candles = make_candles(days, 100.0, 1000)
    r = previous_week_volume(candles, session, days)
    assert r.value == 4000
    assert "4 sessions" in r.detail


def test_current_incomplete_week_excluded_on_monday_and_friday():
    # Monday 12 Oct and Friday 16 Oct both screen against Mon 5 – Fri 9 Oct;
    # Friday's own week (12–15 Oct, already in the cache) must not be counted.
    for session in (date(2026, 10, 12), date(2026, 10, 16)):
        days = trading_days_before(session, 40)
        candles = make_candles(days, 100.0, [d.day for d in days])
        assert previous_week_volume(candles, session, days).value == 5 + 6 + 7 + 8 + 9


def test_fully_closed_week_falls_back_to_last_week_with_sessions():
    session = date(2026, 10, 14)
    cal = [d for d in trading_days_before(session, 40) if not (date(2026, 10, 5) <= d <= date(2026, 10, 9))]
    monday, friday, sessions = previous_completed_week(session, cal)
    assert monday == date(2026, 9, 28) and len(sessions) == 4


def test_derived_calendar_ignores_single_instrument_anomalies():
    a = [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)]
    b = [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)]
    c = [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 3)]  # stray Saturday
    assert derive_trading_calendar([a, b, c]) == a


def test_zero_volume_candle_is_real_data_not_missing():
    session = date(2026, 10, 12)
    days = trading_days_before(session, 20)
    candles = make_candles(days, 100.0, 0)
    assert previous_week_volume(candles, session, days).value == 0.0


def test_nan_volume_is_missing_not_zero():
    session = date(2026, 10, 12)
    days = trading_days_before(session, 20)
    vols = [1000.0] * len(days)
    vols[-1] = float("nan")
    r = previous_week_volume(make_candles(days, 100.0, vols), session, days)
    assert r.value is None and r.reason == "incomplete_week_history"


# 9. Today's incomplete candle excluded from SMA -----------------------------
def test_sma_excludes_todays_partial_candle():
    session = date(2026, 10, 12)
    days = trading_days_before(session, 60)
    candles = make_candles(days + [session], [100.0] * 60 + [10_000.0], [1000] * 60 + [99_999_999])
    r = sma_completed(candles, session, 50)
    assert r.value == pytest.approx(100.0)
    assert last_completed_close(candles, session).value == 100.0
    assert previous_week_volume(candles, session, days).value == 5000


def test_sma_uses_exactly_last_50_completed_closes():
    session = date(2026, 10, 12)
    days = trading_days_before(session, 60)
    closes = list(range(1, 61))  # 1..60 ; last 50 are 11..60
    r = sma_completed(make_candles(days, [float(c) for c in closes], 1), session, 50)
    assert r.value == pytest.approx(sum(range(11, 61)) / 50)


def test_sma_needs_50_closes():
    session = date(2026, 10, 12)
    days = trading_days_before(session, 49)
    r = sma_completed(make_candles(days, 100.0, 1), session, 50)
    assert r.value is None and r.reason == "insufficient_history"


def test_duplicate_candle_dates_are_deduplicated():
    session = date(2026, 10, 12)
    days = trading_days_before(session, 50)
    df = make_candles(days, 100.0, 1000)
    df = pd.concat([df, df.tail(1).assign(close=100.0)])
    assert sma_completed(df, session, 50).value == pytest.approx(100.0)


def test_special_saturday_trading_session_included_in_previous_week():
    # Previous week has Mon 5 Oct - Fri 9 Oct PLUS a special Saturday session on 10 Oct
    session = date(2026, 10, 14)
    normal_days = trading_days_before(session, 40)
    special_sat = date(2026, 10, 10)
    cal_with_sat = sorted(normal_days + [special_sat])
    monday, end_date, sessions = previous_completed_week(session, cal_with_sat)
    assert monday == date(2026, 10, 5)
    assert end_date == special_sat
    assert len(sessions) == 6
    assert special_sat in sessions

    # Complete history including Saturday sums all 6 sessions
    candles = make_candles(cal_with_sat, 100.0, 1000)
    r = previous_week_volume(candles, session, cal_with_sat)
    assert r.value == 6000
    assert "6 sessions" in r.detail

    # Missing Saturday candle fails gracefully
    candles_no_sat = make_candles(normal_days, 100.0, 1000)
    r2 = previous_week_volume(candles_no_sat, session, cal_with_sat)
    assert r2.value is None
    assert "incomplete_week_history" == r2.reason
    assert "2026-10-10" in r2.detail


def test_last_completed_close_rejects_non_positive_value():
    session = date(2026, 10, 12)
    days = trading_days_before(session, 5)
    # Candle with zero close
    candles_zero = make_candles(days, [100.0, 100.0, 100.0, 100.0, 0.0], 1000)
    # Candle normalized will drop <=0 close or last_completed_close flags it
    r = last_completed_close(candles_zero, session)
    # Either dropped so last close is previous positive one, or flagged invalid
    assert r.ok and r.value == 100.0  # normalized dropped the 0.0, using last valid close 100.0


# 10. Date boundaries and session info synchronization targets ----------------
def test_last_completed_trading_day_boundaries():
    from calendar_utils import last_completed_trading_day

    # Monday during trading hours (incomplete candle) -> Friday
    assert last_completed_trading_day(ist(2026, 10, 12, 10, 0), HOLIDAYS) == date(2026, 10, 9)
    # Monday right before market close (15:29:59) -> Friday
    assert last_completed_trading_day(ist(2026, 10, 12, 15, 29, 59), HOLIDAYS) == date(2026, 10, 9)
    # Monday at market close (15:30:00) -> Monday completed
    assert last_completed_trading_day(ist(2026, 10, 12, 15, 30, 0), HOLIDAYS) == date(2026, 10, 12)
    # Monday evening / night after market close -> Monday completed
    assert last_completed_trading_day(ist(2026, 10, 12, 18, 0), HOLIDAYS) == date(2026, 10, 12)
    assert last_completed_trading_day(ist(2026, 10, 12, 22, 17), HOLIDAYS) == date(2026, 10, 12)

    # Tuesday morning before market open -> Monday completed
    assert last_completed_trading_day(ist(2026, 10, 13, 8, 30), HOLIDAYS) == date(2026, 10, 12)
    # Tuesday during market hours (09:59) -> Monday completed (excludes today's partial candle)
    assert last_completed_trading_day(ist(2026, 10, 13, 9, 59), HOLIDAYS) == date(2026, 10, 12)
    # Tuesday after market close -> Tuesday completed
    assert last_completed_trading_day(ist(2026, 10, 13, 16, 0), HOLIDAYS) == date(2026, 10, 13)

    # Weekend -> previous Friday
    assert last_completed_trading_day(ist(2026, 10, 17, 12, 0), HOLIDAYS) == date(2026, 10, 16)
    assert last_completed_trading_day(ist(2026, 10, 18, 12, 0), HOLIDAYS) == date(2026, 10, 16)

    # Gandhi Jayanti holiday (Friday 2026-10-02) -> Thursday 2026-10-01
    assert last_completed_trading_day(ist(2026, 10, 2, 12, 0), HOLIDAYS) == date(2026, 10, 1)


def test_session_info_and_build_context_consistency(tmp_path):
    from config import Settings
    from pipeline import build_context, session_info

    holidays_file = tmp_path / "holidays.csv"
    holidays_file.write_text("date,description\n2026-10-02,Gandhi Jayanti\n", encoding="utf-8")
    settings = Settings(holidays_csv=holidays_file)

    # Case 1: Monday night at 22:17 (after close)
    info_night = session_info(settings, ist(2026, 10, 12, 22, 17))
    assert info_night.session == date(2026, 10, 12)
    assert info_night.last_completed == date(2026, 10, 12)  # Monday completed, cache should have Monday
    assert info_night.prior_session == date(2026, 10, 9)
    assert not info_night.market_open

    # Case 2: Tuesday morning at 08:30 (before open)
    info_pre = session_info(settings, ist(2026, 10, 13, 8, 30))
    assert info_pre.session == date(2026, 10, 12)  # snapshot is last traded session
    assert info_pre.last_completed == date(2026, 10, 12)  # cache should have Monday
    assert info_pre.prior_session == date(2026, 10, 9)
    assert not info_pre.market_open

    # Case 3: Tuesday at 09:59 (LIVE market open)
    info_live = session_info(settings, ist(2026, 10, 13, 9, 59))
    assert info_live.session == date(2026, 10, 13)
    assert info_live.last_completed == date(2026, 10, 12)  # completed candles required through Monday
    assert info_live.prior_session == date(2026, 10, 12)
    assert info_live.market_open

    # Context build in live mode
    ctx_live = build_context(info_live, {})
    assert ctx_live.session == date(2026, 10, 13)
    assert ctx_live.expected_last_completed == date(2026, 10, 12)
    assert ctx_live.market_open

    # Context build in snapshot mode
    ctx_pre = build_context(info_pre, {})
    assert ctx_pre.session == date(2026, 10, 12)
    assert ctx_pre.expected_last_completed == date(2026, 10, 9)
    assert not ctx_pre.market_open


# 11. Time-adjusted volume metrics and charting data preparation ---------------
def test_time_adjusted_volume_with_comparable_historical_data():
    # If historical intraday baseline is 50,000 and today's volume is 250,000 -> 5.0x
    mult, desc = calculate_time_adjusted_volume(250_000, 50_000)
    assert mult == 5.0
    assert "Historical Same-Time Baseline" in desc

    # When comparable data is unavailable (None or <= 0) -> Returns None and N/A
    mult_na, desc_na = calculate_time_adjusted_volume(250_000, None)
    assert mult_na is None
    assert "Intraday history unavailable" in desc_na

    mult_zero, desc_zero = calculate_time_adjusted_volume(250_000, 0)
    assert mult_zero is None
    assert "Intraday history unavailable" in desc_zero

    mult_no_today, _ = calculate_time_adjusted_volume(None, 50_000)
    assert mult_no_today is None


def test_linear_session_pace_heuristic_bounds_and_fractions():
    # Session open 09:15, close 15:30 (375 minutes)
    # At 09:30 IST (15 min elapsed): frac = 15/375 = 0.04 -> clamped to 0.05
    pace, desc, frac = calculate_linear_session_pace(
        today_volume=10_000,
        prev_week_volume=100_000,
        now=ist(2026, 10, 13, 9, 30),
        market_open=True,
    )
    assert frac == pytest.approx(0.05)
    assert pace == pytest.approx(10_000 / (100_000 * 0.05))  # 2.0x

    # At 12:22:30 IST (~187.5 min elapsed): frac ~ 0.50
    pace_mid, desc_mid, frac_mid = calculate_linear_session_pace(
        today_volume=50_000,
        prev_week_volume=100_000,
        now=ist(2026, 10, 13, 12, 22),
        market_open=True,
    )
    assert 0.49 <= frac_mid <= 0.51
    assert "Linear Pace (Heuristic)" in desc_mid

    # Market closed mode uses full session (frac = 1.0)
    pace_closed, desc_closed, frac_closed = calculate_linear_session_pace(
        today_volume=150_000,
        prev_week_volume=100_000,
        now=ist(2026, 10, 13, 18, 0),
        market_open=False,
    )
    assert frac_closed == 1.0
    assert pace_closed == 1.50
    assert "Full Session" in desc_closed


def test_prepare_chart_data_sma50_distinct_from_close():
    # 70 days of candles
    days = trading_days_before(date(2026, 10, 12), 70)
    closes = [float(100 + i) for i in range(70)]
    raw_df = make_candles(days, closes, 1000)

    chart_df = prepare_chart_data(raw_df, sma_window=50, display_tail=60)
    assert len(chart_df) == 60
    assert "sma50" in chart_df.columns
    assert "close" in chart_df.columns

    # Verify sma50 is populated and mathematically distinct from close
    last_row = chart_df.iloc[-1]
    expected_sma50 = sum(closes[-50:]) / 50.0
    assert last_row["sma50"] == pytest.approx(expected_sma50)
    assert last_row["close"] == closes[-1]
    assert last_row["sma50"] != last_row["close"]  # Confirms SMA line does not mirror close!

    # Verify handling of empty or None candles
    empty_df = prepare_chart_data(None)
    assert empty_df.empty
    assert "sma50" in empty_df.columns


def test_prepare_chart_data_strictly_completed_candles_when_session_provided():
    # 60 days before session, plus one candle on session day (partial/in-progress)
    sess = date(2026, 10, 13)
    days = trading_days_before(sess, 60) + [sess]
    closes = [100.0] * 60 + [999.0]
    raw_df = make_candles(days, closes, 1000)

    # When session is supplied, candle on session day must be excluded from chart and SMA
    chart_df = prepare_chart_data(raw_df, sma_window=50, display_tail=60, session=sess)
    assert chart_df["date"].max().date() < sess
    assert chart_df["close"].max() == 100.0
    assert chart_df.iloc[-1]["sma50"] == pytest.approx(100.0)


