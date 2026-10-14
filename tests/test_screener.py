"""Screening rules: pass, individual failures, strict boundaries, missing data."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from conftest import NOW, SESSION, make_candles, make_quote, mcap_table
from indicators import sma_completed
from screener import (
    DATA_ISSUE,
    FAIL,
    FILTER_FAIL,
    MATCH,
    NO_DATA,
    PASS,
    StockInput,
    evaluate_stock,
    screen_universe,
)


def run(stock, ctx, params, mcap):
    return evaluate_stock(stock, ctx, params, mcap)


# 1. A stock passing all four conditions -------------------------------------
def test_all_four_pass(base_stock, ctx, params, mcap):
    r = run(base_stock, ctx, params, mcap)
    assert r["status"] == MATCH
    assert [r["c1_volume"], r["c2_mcap"], r["c3_prev_close"], r["c4_sma"]] == [PASS] * 4
    assert r["prev_week_volume"] == 5000
    assert r["volume_multiple"] == pytest.approx(5.0)
    assert r["sma50"] == pytest.approx(100.0)
    assert r["pct_change"] == pytest.approx(10.0)
    assert r["market_cap_cr"] == 5000


# 2. Failing each condition individually ------------------------------------
def test_fails_volume_only(base_stock, ctx, params, mcap):
    base_stock.quote = make_quote(volume=15_000)
    r = run(base_stock, ctx, params, mcap)
    assert r["status"] == FILTER_FAIL
    assert (r["c1_volume"], r["c2_mcap"], r["c3_prev_close"], r["c4_sma"]) == (FAIL, PASS, PASS, PASS)


def test_fails_market_cap_only(base_stock, ctx, params, mcap):
    base_stock.symbol = "SMALL"
    r = run(base_stock, ctx, params, mcap)
    assert (r["c1_volume"], r["c2_mcap"], r["c3_prev_close"], r["c4_sma"]) == (PASS, FAIL, PASS, PASS)
    assert r["status"] == FILTER_FAIL


def test_fails_prev_close_only(days, ctx, params, mcap):
    closes = [80.0] * (len(days) - 1) + [100.0]  # SMA ~80.4, previous close 100
    s = StockInput(1, "GOOD", "Good", make_quote(ltp=99.5), make_candles(days, closes, 1000))
    r = run(s, ctx, params, mcap)
    assert (r["c1_volume"], r["c2_mcap"], r["c3_prev_close"], r["c4_sma"]) == (PASS, PASS, FAIL, PASS)
    assert r["status"] == FILTER_FAIL


def test_fails_sma_only(days, ctx, params, mcap):
    closes = [120.0] * (len(days) - 1) + [100.0]  # SMA 119.6, previous close 100
    s = StockInput(1, "GOOD", "Good", make_quote(ltp=110.0), make_candles(days, closes, 1000))
    r = run(s, ctx, params, mcap)
    assert (r["c1_volume"], r["c2_mcap"], r["c3_prev_close"], r["c4_sma"]) == (PASS, PASS, PASS, FAIL)
    assert r["status"] == FILTER_FAIL


# 3-6. Strict boundaries -----------------------------------------------------
def test_volume_exactly_four_times_fails(base_stock, ctx, params, mcap):
    base_stock.quote = make_quote(volume=20_000)  # 4 * 5,000
    r = run(base_stock, ctx, params, mcap)
    assert r["c1_volume"] == FAIL and r["status"] == FILTER_FAIL


def test_volume_just_above_four_times_passes(base_stock, ctx, params, mcap):
    base_stock.quote = make_quote(volume=20_001)
    assert run(base_stock, ctx, params, mcap)["c1_volume"] == PASS


def test_market_cap_exactly_1000_crore_fails(base_stock, ctx, params, mcap):
    base_stock.symbol = "EDGE"
    r = run(base_stock, ctx, params, mcap)
    assert r["c2_mcap"] == FAIL and r["status"] == FILTER_FAIL


def test_market_cap_boundary_999_9_1000_0_1000_1(base_stock, ctx, params):
    """Rule is strictly `market_cap_cr > 1000.0`:
    - 999.9 Cr must FAIL
    - 1000.0 Cr must FAIL
    - 1000.1 Cr must PASS
    """
    table_999_9 = mcap_table([{"symbol": "GOOD", "market_cap_cr": "999.9", "as_of_date": "2026-09-30", "source": "t"}])
    r_999 = run(base_stock, ctx, params, table_999_9)
    assert r_999["c2_mcap"] == FAIL
    assert r_999["status"] == FILTER_FAIL

    table_1000_0 = mcap_table([{"symbol": "GOOD", "market_cap_cr": "1000.0", "as_of_date": "2026-09-30", "source": "t"}])
    r_1000 = run(base_stock, ctx, params, table_1000_0)
    assert r_1000["c2_mcap"] == FAIL
    assert r_1000["status"] == FILTER_FAIL

    table_1000_1 = mcap_table([{"symbol": "GOOD", "market_cap_cr": "1000.1", "as_of_date": "2026-09-30", "source": "t"}])
    r_1001 = run(base_stock, ctx, params, table_1000_1)
    assert r_1001["c2_mcap"] == PASS
    assert r_1001["status"] == MATCH


def test_market_cap_exactly_1000_crore_from_shares_fails(base_stock, ctx, params):
    # 1,000 crore = 1e10 rupees; at LTP 100 that is 1e8 shares.
    table = mcap_table([{"symbol": "GOOD", "shares_outstanding": "100000000", "as_of_date": "2026-09-30", "source": "t"}])
    base_stock.quote = make_quote(ltp=100.0, prev_close=99.0)
    base_stock.candles = make_candles(base_stock.candles["date"].tolist(), [90.0] * 69 + [99.0], 1000)
    r = run(base_stock, ctx, params, table)
    assert r["market_cap_cr"] == pytest.approx(1000.0)
    assert r["c2_mcap"] == FAIL


def test_price_equal_prev_close_fails(base_stock, ctx, params, mcap):
    base_stock.quote = make_quote(ltp=100.0, prev_close=100.0)
    base_stock.candles = make_candles(base_stock.candles["date"].tolist(), [90.0] * 69 + [100.0], 1000)
    r = run(base_stock, ctx, params, mcap)
    assert r["c3_prev_close"] == FAIL
    assert r["c4_sma"] == PASS


def test_price_equal_sma_fails(days, ctx, params, mcap):
    closes = [104.0] * (len(days) - 1) + [100.0]
    candles = make_candles(days, closes, 1000)
    sma = sma_completed(candles, SESSION).value
    s = StockInput(1, "GOOD", "Good", make_quote(ltp=sma), candles)
    r = run(s, ctx, params, mcap)
    assert r["c3_prev_close"] == PASS
    assert r["c4_sma"] == FAIL


def test_custom_thresholds_are_respected(base_stock, ctx, mcap, params):
    p = replace(params, volume_multiplier=6.0, min_market_cap_cr=6000)
    r = run(base_stock, ctx, p, mcap)
    assert r["c1_volume"] == FAIL and r["c2_mcap"] == FAIL


# 7. Missing / stale data ----------------------------------------------------
def test_missing_market_cap_is_data_issue_not_pass(base_stock, ctx, params, mcap):
    base_stock.symbol = "UNKNOWN"
    r = run(base_stock, ctx, params, mcap)
    assert r["c2_mcap"] == NO_DATA
    assert r["status"] == DATA_ISSUE
    assert r["market_cap_cr"] is None
    assert "MISSING" in r["data_issues"]


def test_stale_market_cap_is_data_issue(base_stock, ctx, params, mcap):
    base_stock.symbol = "OLD"
    r = run(base_stock, ctx, params, mcap)
    assert r["c2_mcap"] == NO_DATA and "STALE" in r["data_issues"]


def test_missing_historical_candles(base_stock, ctx, params, mcap):
    base_stock.candles = None
    r = run(base_stock, ctx, params, mcap)
    assert r["c1_volume"] == NO_DATA and r["c4_sma"] == NO_DATA
    assert r["prev_week_volume"] is None and r["sma50"] is None
    assert r["status"] == DATA_ISSUE


def test_insufficient_history_for_sma(days, ctx, params, mcap):
    s = StockInput(1, "GOOD", "Good", make_quote(), make_candles(days[-30:], 100.0, 1000))
    r = run(s, ctx, params, mcap)
    assert r["c4_sma"] == NO_DATA and "insufficient_history" in r["data_issues"]
    assert r["c1_volume"] == PASS  # previous week is still fully covered


def test_missing_day_in_previous_week_is_not_summed(days, ctx, params, mcap):
    partial = [d for d in days if d.isoformat() != "2026-10-07"]
    s = StockInput(1, "GOOD", "Good", make_quote(volume=10**9), make_candles(partial, 100.0, 1000))
    r = run(s, ctx, params, mcap)
    assert r["c1_volume"] == NO_DATA
    assert r["prev_week_volume"] is None
    assert "2026-10-07" in r["data_issues"]


def test_stale_quote_during_market_hours(base_stock, ctx, params, mcap):
    base_stock.quote = make_quote(ts=datetime(2026, 10, 12, 10, 40, 0))  # 20 min old at 11:00
    r = run(base_stock, ctx, params, mcap)
    assert r["status"] == DATA_ISSUE
    assert r["ltp"] is None
    assert "stale" in r["data_issues"]


def test_quote_from_previous_session_is_rejected(base_stock, ctx, params, mcap):
    base_stock.quote = make_quote(ts=datetime(2026, 10, 9, 15, 59), ltt=datetime(2026, 10, 9, 15, 29))
    r = run(base_stock, ctx, params, mcap)
    assert r["status"] == DATA_ISSUE and "not session" in r["data_issues"]


def test_missing_quote(base_stock, ctx, params, mcap):
    base_stock.quote = None
    r = run(base_stock, ctx, params, mcap)
    assert r["status"] == DATA_ISSUE
    assert {r["c1_volume"], r["c3_prev_close"], r["c4_sma"]} == {NO_DATA}


def test_no_trade_this_session(base_stock, ctx, params, mcap):
    base_stock.quote = make_quote(volume=0, ltt=datetime(2026, 10, 9, 15, 20))
    r = run(base_stock, ctx, params, mcap)
    assert r["c1_volume"] == FAIL  # zero volume is real data and cannot exceed 4x
    assert r["c3_prev_close"] == NO_DATA and r["c4_sma"] == NO_DATA
    assert r["status"] == DATA_ISSUE
    assert r["primary_exception"] == "NO_TRADES"


def test_prev_close_mismatch_with_history_is_data_issue(base_stock, ctx, params, mcap):
    base_stock.quote = make_quote(prev_close=120.0, ltp=125.0)  # history says 100
    r = run(base_stock, ctx, params, mcap)
    assert r["c3_prev_close"] == NO_DATA and "differs" in r["data_issues"]
    assert r["status"] == DATA_ISSUE
    assert r["primary_exception"] == "PRICE_DISCREPANCY"


def test_missing_data_always_classified_as_data_issue_never_legitimate_fail(base_stock, ctx, params, mcap):
    """Incomplete data must NEVER be labeled as a legitimate filter failure.
    Even if volume < 4x, missing market cap makes the evaluation incomplete.
    """
    base_stock.symbol = "UNKNOWN"
    base_stock.quote = make_quote(volume=100)
    r = run(base_stock, ctx, params, mcap)
    assert r["c2_mcap"] == NO_DATA and r["c1_volume"] == FAIL
    assert r["status"] == DATA_ISSUE
    assert r["primary_exception"] == "MISSING_MARKET_CAP"


def test_closed_market_snapshot_does_not_check_quote_age(base_stock, ctx, params, mcap):
    closed = replace(ctx, market_open=False, now=NOW.replace(hour=20))
    assert run(base_stock, closed, params, mcap)["status"] == MATCH


def test_screen_universe_sorts_by_volume_multiple(base_stock, days, ctx, params, mcap):
    big = StockInput(2, "GOOD", "Good 2", make_quote(volume=50_000), make_candles(days, 100.0, 1000))
    missing = StockInput(3, "UNKNOWN", "X", None, None)
    df = screen_universe([base_stock, missing, big], ctx, params, mcap)
    assert df["instrument_token"].tolist()[:2] == [2, 1]
    assert df["instrument_token"].tolist()[-1] == 3


def test_zero_previous_week_volume_is_data_issue_not_match(days, ctx, params, mcap):
    candles = make_candles(days, 100.0, 0)  # zero volume on all historical days
    s = StockInput(1, "GOOD", "Good Ltd", make_quote(volume=5000), candles)
    r = run(s, ctx, params, mcap)
    assert r["c1_volume"] == NO_DATA
    assert "previous week volume is zero" in r["data_issues"]
    assert r["status"] == DATA_ISSUE  # Must never be MATCH
    assert r["volume_multiple"] is None  # Must never be inf


def test_market_cap_method_and_source_returned(base_stock, ctx, params):
    table = mcap_table([
        {"symbol": "GOOD", "shares_outstanding": "200000000", "as_of_date": "2026-09-30", "source": "filing_q2"},
    ])
    r = run(base_stock, ctx, params, table)
    assert r["market_cap_method"] == "shares_x_ltp"
    assert r["market_cap_source"] == "filing_q2"


def test_overall_precedence_rules(base_stock, ctx, params, mcap):
    # Base stock is MATCH
    assert run(base_stock, ctx, params, mcap)["status"] == MATCH

    # 1 NO_DATA + 3 PASS -> DATA_ISSUE
    s_issue = StockInput(1, "OLD", "Old", make_quote(), base_stock.candles)
    assert run(s_issue, ctx, params, mcap)["status"] == DATA_ISSUE

    # Missing data must strictly be DATA_ISSUE, never legitimate filter failure
    s_fail_and_issue = StockInput(1, "OLD", "Old", make_quote(ltp=90.0, prev_close=100.0), base_stock.candles)
    assert run(s_fail_and_issue, ctx, params, mcap)["status"] == DATA_ISSUE
    assert run(s_fail_and_issue, ctx, params, mcap)["primary_exception"] == "MISSING_MARKET_CAP"


def test_candidate_verification_audit_fields(base_stock, ctx, params, mcap):
    r = run(base_stock, ctx, params, mcap)
    required_audit_keys = [
        "instrument_token", "symbol", "company_name", "status", "primary_exception",
        "underlying_issues", "underlying_issues_str",
        "ltp", "prev_close", "pct_change", "market_cap_cr", "market_cap_status",
        "market_cap_as_of", "market_cap_method", "market_cap_source",
        "today_volume", "prev_week_volume", "prev_week_range", "volume_multiple",
        "volume_multiple_formula", "time_adjusted_volume_multiplier", "time_adjusted_formula",
        "linear_session_pace", "linear_session_pace_formula", "session_fraction",
        "sma50", "sma50_distance_pct", "candle_count", "quote_time",
        "quote_age_seconds", "last_trade_time", "history_last_date", "history_status",
        "c1_volume", "c1_reason", "c2_mcap", "c2_reason", "c3_prev_close", "c3_reason",
        "c4_sma", "c4_reason", "data_issues", "active_session", "expected_last_completed",
    ]
    for k in required_audit_keys:
        assert k in r, f"Missing audit key: {k}"


def test_missing_market_cap_and_history_shows_both_underlying_issues(base_stock, ctx, params, mcap):
    """Requirement 2: Keep mutually exclusive primary exception category,
    but record every underlying data issue independently.
    A stock missing both market cap and history must show both issues,
    even if its primary category is MISSING_MARKET_CAP.
    """
    base_stock.symbol = "UNKNOWN"
    base_stock.candles = None
    r = run(base_stock, ctx, params, mcap)

    assert r["status"] == DATA_ISSUE
    assert r["primary_exception"] == "MISSING_MARKET_CAP"
    assert "MISSING_MARKET_CAP" in r["underlying_issues"]
    assert "INCOMPLETE_HISTORY" in r["underlying_issues"]
    assert "MISSING_MARKET_CAP" in r["underlying_issues_str"]
    assert "INCOMPLETE_HISTORY" in r["underlying_issues_str"]
    # Check that both reasons are captured in detailed data issues
    assert "market cap" in r["data_issues"].lower()
    assert "historical candles" in r["data_issues"].lower()


def test_top_level_status_reconciliation_partitions_universe(base_stock, days, ctx, params, mcap):
    """Requirement 6: Reconcile all eligible instruments into exactly one
    top-level status: MATCH, FILTER_FAIL, or DATA_ISSUE.
    """
    # 1. Matching stock
    s_match = base_stock

    # 2. Filter fail stock (clean data, volume < 4x)
    s_fail = StockInput(2, "GOOD", "Good 2", make_quote(volume=1000), make_candles(days, 100.0, 1000))

    # 3. Data issue stock (missing market cap)
    s_issue = StockInput(3, "UNKNOWN", "Unknown", make_quote(volume=50_000), make_candles(days, 100.0, 1000))

    df = screen_universe([s_match, s_fail, s_issue], ctx, params, mcap)
    assert len(df) == 3

    statuses = set(df["status"].tolist())
    assert statuses == {MATCH, FILTER_FAIL, DATA_ISSUE}

    # Verify each stock has exactly one top-level status
    assert (df["status"] == MATCH).sum() == 1
    assert (df["status"] == FILTER_FAIL).sum() == 1
    assert (df["status"] == DATA_ISSUE).sum() == 1
    assert (df["status"] == MATCH).sum() + (df["status"] == FILTER_FAIL).sum() + (df["status"] == DATA_ISSUE).sum() == 3


