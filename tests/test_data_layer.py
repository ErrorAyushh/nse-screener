"""Market-cap loading, Kite error handling, history-cache failures (offline fakes)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from kiteconnect import exceptions as kex

from auth_login import extract_request_token
from calendar_utils import IST
from config import Settings
from conftest import LAST_COMPLETED, make_quote, trading_days_before
from kite_data import (
    AuthError,
    DataPermissionError,
    DataUnavailableError,
    HistoryCache,
    RateLimiter,
    call_with_retry,
    filter_equity_universe,
    make_client,
    parse_quote,
    token_probably_expired,
)
from market_cap import DUPLICATE, INVALID, MISSING, NO_PRICE, OK, STALE, load_market_cap, parse_market_cap_frame
from screener import DATA_ISSUE, NO_DATA, StockInput, evaluate_stock

FAST = RateLimiter(1000.0)


def no_sleep(_):
    return None


# ---- Market cap --------------------------------------------------------------
def test_market_cap_validation_rules(tmp_path):
    p = tmp_path / "mc.csv"
    p.write_text(
        "# comment line\n"
        "symbol,company_name,market_cap_cr,shares_outstanding,as_of_date,source\n"
        "aaa,A,2500,,2026-09-30,x\n"
        "BBB,B,,50000000,2026-09-30,x\n"
        "DUP,D1,2000,,2026-09-30,x\n"
        "DUP,D2,3000,,2026-09-30,x\n"
        "NEG,N,-5,,2026-09-30,x\n"
        "BOTH,B,100,50000000,2026-09-30,x\n"
        "NONE,NO,,,2026-09-30,x\n"
        "ZERO,Z,0,0,2026-09-30,x\n"
        "NEGS,NS,100,-5000,2026-09-30,x\n"
        "NONNUM,NN,bad,invalid,2026-09-30,x\n"
        "NODATE,N,100,,,x\n",
        encoding="utf-8",
    )
    t = load_market_cap(p)
    today = date(2026, 10, 12)
    assert t.lookup("AAA", 10.0, today, 190).status == OK
    assert t.lookup("AAA", None, today, 190).value_cr == 2500  # reported figure does not need LTP
    b = t.lookup("BBB", 400.0, today, 190)
    assert b.status == OK and b.value_cr == pytest.approx(2000.0) and b.method == "shares_x_ltp"
    assert t.lookup("BBB", None, today, 190).status == NO_PRICE
    assert t.lookup("DUP", 1.0, today, 190).status == DUPLICATE
    assert t.lookup("NEG", 1.0, today, 190).status == INVALID
    # BOTH: When both fields are present:
    # 1. With LTP > 0: live calculation from shares_outstanding (50M shares * 400 = 2000 Cr)
    both_live = t.lookup("BOTH", 400.0, today, 190)
    assert both_live.status == OK and both_live.value_cr == pytest.approx(2000.0) and both_live.method == "shares_x_ltp"
    # 2. When LTP is missing/None: safe fallback to reported market_cap_cr (100 Cr)
    both_fallback = t.lookup("BOTH", None, today, 190)
    assert both_fallback.status == OK and both_fallback.value_cr == 100.0 and both_fallback.method == "reported"
    # Rejections for invalid/missing/negative/zero values
    assert t.lookup("NONE", 1.0, today, 190).status == INVALID
    assert t.lookup("ZERO", 1.0, today, 190).status == INVALID
    assert t.lookup("NEGS", 1.0, today, 190).status == INVALID
    assert t.lookup("NONNUM", 1.0, today, 190).status == INVALID
    assert t.lookup("NODATE", 1.0, today, 190).status == INVALID
    assert t.lookup("ZZZ", 1.0, today, 190).status == MISSING
    assert t.lookup("AAA", 1.0, date(2027, 6, 1), 190).status == STALE


def test_market_cap_missing_file_and_columns(tmp_path):
    assert "not found" in load_market_cap(tmp_path / "nope.csv").load_error
    t = parse_market_cap_frame(pd.DataFrame([{"symbol": "A", "price": 5}]))
    assert t.load_error and t.records.empty


def test_refresh_market_cap_processing(tmp_path):
    from refresh_market_cap import process_amfi_data

    xlsx_path = tmp_path / "sample_amfi.xlsx"
    raw_df = pd.DataFrame(
        [
            ["Title row", None, None, None],
            ["NSE Symbol", "Company name", "Average of All Exchanges (Rs. Cr.)", "ISIN"],
            ["RELIANCE", "Reliance Industries", "1800000", "INE002A01018"],
            ["-", "Abbott India", "50000", "INE358A01014"],  # missing NSE symbol, should resolve via ISIN
            ["BADVAL", "Bad Co", "-10", "INE000000000"],      # negative mcap, dropped
            ["-", "No Symbol Co", "1200", "-"],           # no symbol and no isin, dropped
            ["DUP", "Dup 1", "2000", "INE111A01010"],         # duplicate symbol
            ["DUP", "Dup 2", "2500", "INE111A01020"],         # duplicate symbol
        ]
    )
    raw_df.to_excel(xlsx_path, header=False, index=False)

    isin_map = {
        "INE358A01014": "ABBOTINDIA",
        "INE111A01020": "DUP",  # active ISIN for DUP
    }

    out = process_amfi_data(xlsx_path, as_of="2026-06-30", source="test", isin_to_symbol=isin_map)
    assert len(out) == 3  # RELIANCE, ABBOTINDIA, DUP
    assert set(out["symbol"]) == {"RELIANCE", "ABBOTINDIA", "DUP"}
    dup_row = out[out["symbol"] == "DUP"].iloc[0]
    assert dup_row["market_cap_cr"] == 2500.0  # active ISIN matched row kept



# ---- Universe filter ------------------------------------------------------------
def test_universe_keeps_only_nse_cash_equity():
    raw = pd.DataFrame(
        [
            {"instrument_token": 1, "tradingsymbol": "RELIANCE", "name": "RELIANCE INDUSTRIES", "exchange": "NSE", "segment": "NSE", "instrument_type": "EQ"},
            {"instrument_token": 2, "tradingsymbol": "XYZ-BE", "name": "XYZ", "exchange": "NSE", "segment": "NSE", "instrument_type": "EQ"},
            {"instrument_token": 3, "tradingsymbol": "SGBJAN29-GB", "name": "SGB", "exchange": "NSE", "segment": "NSE", "instrument_type": "EQ"},
            {"instrument_token": 4, "tradingsymbol": "NIFTY 50", "name": "NIFTY 50", "exchange": "NSE", "segment": "INDICES", "instrument_type": "EQ"},
            {"instrument_token": 5, "tradingsymbol": "RELIANCE26OCTFUT", "name": "RELIANCE", "exchange": "NFO", "segment": "NFO-FUT", "instrument_type": "FUT"},
            {"instrument_token": 6, "tradingsymbol": "TWIN", "name": "T1", "exchange": "NSE", "segment": "NSE", "instrument_type": "EQ"},
            {"instrument_token": 7, "tradingsymbol": "TWIN", "name": "T2", "exchange": "NSE", "segment": "NSE", "instrument_type": "EQ"},
        ]
    )
    df, stats = filter_equity_universe(raw)
    assert df["tradingsymbol"].tolist() == ["RELIANCE"]
    assert stats["duplicates_dropped"] == 2
    df2, _ = filter_equity_universe(raw, ["BE"])
    assert set(df2["tradingsymbol"]) == {"RELIANCE", "XYZ-BE"}


def test_parse_quote_handles_missing_fields():
    q = parse_quote({"last_price": 101.5, "volume": 1200, "ohlc": {"close": 100}, "timestamp": datetime(2026, 10, 12, 11, 0)})
    assert q.last_price == 101.5 and q.prev_close == 100 and q.last_trade_time is None
    assert parse_quote(None) is None
    assert parse_quote({"last_price": "bad"}).last_price is None


# ---- 10. Credentials and API failures -------------------------------------------
def test_expired_token_raises_auth_error_without_retry():
    calls = []

    def fn():
        calls.append(1)
        raise kex.TokenException("Incorrect `api_key` or `access_token`.", code=403)

    with pytest.raises(AuthError):
        call_with_retry(fn, FAST, sleep=no_sleep)
    assert len(calls) == 1


def test_permission_error_is_reported():
    def fn():
        raise kex.PermissionException("Insufficient permission", code=403)

    with pytest.raises(DataPermissionError):
        call_with_retry(fn, FAST, sleep=no_sleep)


def test_network_errors_are_retried_then_succeed():
    attempts = iter([kex.NetworkException("Too many requests", code=429), kex.NetworkException("x", code=502)])

    def fn():
        exc = next(attempts, None)
        if exc:
            raise exc
        return "ok"

    assert call_with_retry(fn, FAST, sleep=no_sleep) == "ok"


def test_network_errors_give_up_after_attempts():
    def fn():
        raise kex.NetworkException("down", code=503)

    with pytest.raises(DataUnavailableError):
        call_with_retry(fn, FAST, attempts=3, sleep=no_sleep)


def test_missing_credentials():
    with pytest.raises(AuthError):
        make_client(Settings(api_key="", access_token=""))
    with pytest.raises(AuthError):
        make_client(Settings(api_key="k", access_token=""))


def test_token_expiry_rule():
    created = "2026-10-12T08:30:00+05:30"
    assert not token_probably_expired(created, datetime(2026, 10, 12, 15, 0, tzinfo=IST))
    assert not token_probably_expired(created, datetime(2026, 10, 13, 5, 59, tzinfo=IST))
    assert token_probably_expired(created, datetime(2026, 10, 13, 6, 0, tzinfo=IST))
    assert token_probably_expired("2026-10-12T03:00:00+05:30", datetime(2026, 10, 12, 6, 1, tzinfo=IST))
    assert not token_probably_expired("", datetime(2026, 10, 13, 9, 0, tzinfo=IST))


def test_extract_request_token():
    assert extract_request_token("https://127.0.0.1/?action=login&status=success&request_token=abc123") == "abc123"
    assert extract_request_token("  abc123 ") == "abc123"
    with pytest.raises(ValueError):
        extract_request_token("https://127.0.0.1/?status=fail")


# ---- History cache with a fake Kite client ----------------------------------------
class FakeKite:
    def __init__(self, days, close=100.0, fail_tokens=(), token_error=False):
        self.days = days
        self.close = close
        self.fail_tokens = set(fail_tokens)
        self.token_error = token_error
        self.calls = []

    def historical_data(self, token, from_dt, to_dt, interval):
        self.calls.append((token, from_dt.date(), to_dt.date()))
        if self.token_error:
            raise kex.TokenException("expired", code=403)
        if token in self.fail_tokens:
            raise kex.InputException("invalid token", code=400)
        return [
            {"date": datetime(d.year, d.month, d.day, tzinfo=IST), "open": self.close, "high": self.close,
             "low": self.close, "close": self.close, "volume": 1000}
            for d in self.days
            if from_dt.date() <= d <= to_dt.date()
        ]


@pytest.fixture(autouse=True)
def fast_limits(monkeypatch):
    import kite_data

    monkeypatch.setattr(kite_data, "HIST_LIMITER", FAST)


def test_history_cache_incremental_and_skip(tmp_path):
    days = trading_days_before(date(2026, 10, 13), 100)
    kite = FakeKite(days)
    cache = HistoryCache(tmp_path)
    rep = cache.refresh(kite, [11], LAST_COMPLETED, 140)
    assert rep.fetched == 1
    df = cache.load(11)
    assert df["date"].max() == LAST_COMPLETED  # never beyond the last completed session
    rep2 = cache.refresh(kite, [11], LAST_COMPLETED, 140)
    assert rep2.skipped_fresh == 1 and len(kite.calls) == 1
    nxt = date(2026, 10, 12)
    cache.refresh(kite, [11], nxt, 140)
    assert kite.calls[-1][1] >= LAST_COMPLETED - timedelta(days=10)  # incremental window
    assert cache.load(11)["date"].max() == nxt


def test_history_cache_detects_split_adjustment(tmp_path):
    days = trading_days_before(date(2026, 10, 13), 100)
    cache = HistoryCache(tmp_path)
    cache.refresh(FakeKite(days, close=100.0), [11], LAST_COMPLETED, 140)
    rep = cache.refresh(FakeKite(days, close=50.0), [11], date(2026, 10, 12), 140)
    assert rep.full_refetch == 1
    assert set(cache.load(11)["close"]) == {50.0}


def test_history_failure_marks_stock_as_data_issue(tmp_path, ctx, params, mcap):
    days = trading_days_before(date(2026, 10, 13), 100)
    cache = HistoryCache(tmp_path)
    rep = cache.refresh(FakeKite(days, fail_tokens={99}), [99], LAST_COMPLETED, 140)
    assert 99 in rep.errors and cache.error(99)
    s = StockInput(99, "GOOD", "Good", make_quote(), cache.load(99), history_error=cache.error(99))
    r = evaluate_stock(s, ctx, params, mcap)
    assert r["status"] == DATA_ISSUE and r["c1_volume"] == NO_DATA and r["c4_sma"] == NO_DATA


def test_history_refresh_stops_on_expired_token(tmp_path):
    days = trading_days_before(date(2026, 10, 13), 100)
    with pytest.raises(AuthError):
        HistoryCache(tmp_path).refresh(FakeKite(days, token_error=True), [1, 2, 3], LAST_COMPLETED, 140)


def test_stale_history_cache_is_data_issue(ctx, params, mcap):
    days = trading_days_before(date(2026, 10, 6), 70)  # ends 5 Oct, but 9 Oct is required
    from conftest import make_candles

    s = StockInput(1, "GOOD", "Good", make_quote(), make_candles(days, 100.0, 1000))
    r = evaluate_stock(s, ctx, params, mcap)
    assert r["history_status"] == "ISSUE"
    assert r["c1_volume"] == NO_DATA and r["c4_sma"] == NO_DATA
    assert "refresh history cache" in r["data_issues"]


def test_quote_batching_splits_across_batch_size():
    from kite_data import fetch_quotes

    class BatchFakeKite:
        def __init__(self):
            self.calls = []

        def quote(self, keys):
            self.calls.append(keys)
            return {k: {"last_price": 100.0, "volume": 1000, "ohlc": {"close": 99.0}, "timestamp": datetime(2026, 10, 12, 11, 0)} for k in keys}

    kite = BatchFakeKite()
    symbols = [f"SYM{i}" for i in range(1050)]
    quotes = fetch_quotes(kite, symbols)
    assert len(quotes) == 1050
    assert len(kite.calls) == 3
    assert len(kite.calls[0]) == 500
    assert len(kite.calls[1]) == 500
    assert len(kite.calls[2]) == 50


def test_history_cache_error_metadata_persisted(tmp_path):
    days = trading_days_before(date(2026, 10, 13), 100)
    cache = HistoryCache(tmp_path)
    cache.refresh(FakeKite(days, fail_tokens={55}), [55], LAST_COMPLETED, 140)
    assert cache.error(55) != ""

    # Re-open cache from same directory: error should still be present
    reopened = HistoryCache(tmp_path)
    assert reopened.error(55) != ""


def test_config_bounds_and_invalid_env(monkeypatch):
    from config import load_settings

    monkeypatch.setenv("REFRESH_INTERVAL_SECONDS", "5")  # below minimum
    monkeypatch.setenv("MIN_MARKET_CAP_CR", "-50")
    s = load_settings()
    assert s.refresh_interval_seconds == 15  # clamped to minimum
    assert s.min_market_cap_cr == 0.0

    monkeypatch.setenv("REFRESH_INTERVAL_SECONDS", "invalid_text")
    with pytest.raises(ValueError, match="must be a number"):
        load_settings()


# ---- Historical data retrieval, sanitization, persistence & sync counts --------
def test_sanitize_error_masks_credentials():
    from kite_data import sanitize_error

    err1 = "Request failed for url https://kite.trade/history?api_key=secretKey123&access_token=tokenVal456"
    clean1 = sanitize_error(err1)
    assert "secretKey123" not in clean1
    assert "tokenVal456" not in clean1
    assert "api_key=***" in clean1
    assert "access_token=***" in clean1

    err2 = "Unauthorized: Bearer mySecretTokenString789"
    clean2 = sanitize_error(err2)
    assert "mySecretTokenString789" not in clean2
    assert "Bearer ***" in clean2

    err3 = "Error with token: 'xyz123abc' and secret: 'sec456'"
    clean3 = sanitize_error(err3)
    assert "xyz123abc" not in clean3
    assert "sec456" not in clean3


def test_history_cache_atomic_write_and_reload(tmp_path):
    days = trading_days_before(date(2026, 10, 13), 100)
    kite = FakeKite(days)
    cache = HistoryCache(tmp_path)

    # Initial fetch
    rep = cache.refresh(kite, [101, 102], date(2026, 10, 12), 140)
    assert rep.fetched == 2
    assert cache.fetched_through(101) == date(2026, 10, 12)
    assert cache.fetched_through(102) == date(2026, 10, 12)

    # CSV file exists and has proper format
    csv_file = cache._path(101)
    assert csv_file.exists()
    df = cache.load(101)
    assert df is not None and not df.empty
    assert df["date"].max() == date(2026, 10, 12)

    # Re-instantiate HistoryCache from disk and verify persistence
    reloaded = HistoryCache(tmp_path)
    assert reloaded.fetched_through(101) == date(2026, 10, 12)
    assert reloaded.fetched_through(102) == date(2026, 10, 12)
    assert reloaded.error(101) == ""


def test_synchronization_count_calculation(tmp_path):
    cache = HistoryCache(tmp_path)
    target = date(2026, 10, 12)

    # Token 1: Fresh through target
    cache.meta["1"] = {"through": "2026-10-12", "fetched_at": "2026-10-12T22:00:00+05:30"}
    # Token 2: Stale (only through 2026-10-09)
    cache.meta["2"] = {"through": "2026-10-09", "fetched_at": "2026-10-12T22:00:00+05:30"}
    # Token 3: Through 2026-10-12, but has an active error
    cache.meta["3"] = {"through": "2026-10-12", "error": "timeout", "fetched_at": "2026-10-12T22:00:00+05:30"}
    # Token 4: Never fetched (missing from meta)

    tokens = [1, 2, 3, 4]

    # Calculate synchronization count for target session 2026-10-12
    fresh_count_12 = sum(
        1
        for t in tokens
        if (cache.fetched_through(t) or date.min) >= target and not cache.error(t)
    )
    # Only token 1 qualifies!
    assert fresh_count_12 == 1

    # Calculate synchronization count for target session 2026-10-09
    fresh_count_09 = sum(
        1
        for t in tokens
        if (cache.fetched_through(t) or date.min) >= date(2026, 10, 9) and not cache.error(t)
    )
    # Tokens 1 and 2 qualify (token 3 has error, token 4 is missing)
    assert fresh_count_09 == 2

