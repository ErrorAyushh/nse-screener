"""The four screening conditions (pure logic, no API calls).

Each condition resolves to one of three states:

* ``PASS``    – data present and the strict inequality holds
* ``FAIL``    – data present and the inequality does not hold
* ``NO_DATA`` – required data missing, stale or inconsistent (with a reason)

Overall status per stock:

* ``MATCH``       – all four conditions PASS
* ``FILTER_FAIL`` – clean data, but at least one condition definitively FAILs
* ``DATA_ISSUE``  – incomplete, stale, or inconsistent data prevents clean evaluation

Missing data is never treated as zero and never treated as a pass.

Previous-Close Discrepancy Check (C3 Cross-Check):
Compares Kite live quote `ohlc.close` against cached historical close of the last completed session.
If divergence exceeds `prev_close_tolerance_pct` (default 1.0%), C3 returns NO_DATA.
Limitations:
1. It compares only the single previous session close (`ohlc.close` vs last completed daily bar).
2. It does not detect corporate actions with price impact <= tolerance (e.g. small cash dividends).
3. If the historical cache has already been refreshed with post-adjustment data from Kite, both quote and history match, so no discrepancy is flagged.
4. Not all discrepancies are corporate actions (e.g. post-market closing price revisions or auction prints).
5. It is a defensive sanity cross-check, NOT a comprehensive corporate action detector.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

import pandas as pd

from calendar_utils import to_ist
from indicators import (
    calculate_linear_session_pace,
    calculate_time_adjusted_volume,
    completed_candles,
    last_completed_close,
    previous_week_volume,
    sma_completed,
)
from market_cap import MarketCapTable

PASS = "PASS"
FAIL = "FAIL"
NO_DATA = "NO_DATA"

MATCH = "MATCH"
FILTER_FAIL = "FILTER_FAIL"
DATA_ISSUE = "DATA_ISSUE"


@dataclass(frozen=True)
class Quote:
    last_price: float | None
    prev_close: float | None  # Kite quote ohlc.close = previous trading day's close
    volume: float | None  # cumulative traded volume for the day
    timestamp: datetime | None  # exchange timestamp of the quote packet
    last_trade_time: datetime | None


@dataclass(frozen=True)
class ScreenParams:
    min_market_cap_cr: float = 1000.0
    volume_multiplier: float = 4.0
    sma_window: int = 50
    market_cap_max_age_days: int = 190
    quote_stale_seconds: int = 300
    prev_close_tolerance_pct: float = 1.0


@dataclass(frozen=True)
class ScreenContext:
    now: datetime
    session: date  # the trading session being screened
    expected_last_completed: date  # trading session immediately before `session`
    market_open: bool
    trading_calendar: list[date] = field(default_factory=list)


@dataclass
class StockInput:
    instrument_token: int
    symbol: str
    name: str
    quote: Quote | None
    candles: pd.DataFrame | None
    history_error: str = ""
    hist_intraday_volume: float | None = None



@dataclass
class ConditionResult:
    state: str
    reason: str = ""


def _valid_price(x: float | None) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def assess_quote(q: Quote | None, ctx: ScreenContext, params: ScreenParams) -> tuple[bool, bool, str]:
    """Return (quote_usable, traded_this_session, reason)."""
    if q is None:
        return False, False, "no quote returned"
    if not _valid_price(q.last_price):
        return False, False, "quote has no valid last price"
    if q.timestamp is None:
        return False, False, "quote has no exchange timestamp"
    ts = to_ist(q.timestamp)
    if ts.date() != ctx.session:
        return False, False, f"quote is from {ts.date()}, not session {ctx.session}"
    if ctx.market_open:
        age = (to_ist(ctx.now) - ts).total_seconds()
        if age > params.quote_stale_seconds:
            return False, False, f"quote stale ({int(age)}s old)"
    traded = q.last_trade_time is not None and to_ist(q.last_trade_time).date() == ctx.session
    return True, traded, "" if traded else "no trade yet this session"


def evaluate_stock(
    stock: StockInput,
    ctx: ScreenContext,
    params: ScreenParams,
    mcap_table: MarketCapTable,
) -> dict:
    q = stock.quote
    quote_ok, traded, quote_reason = assess_quote(q, ctx, params)
    ltp = q.last_price if (q and quote_ok) else None

    # ---- History freshness --------------------------------------------------
    hist_ok = False
    hist_reason = stock.history_error or ""
    last_hist_date: date | None = None
    if not stock.history_error:
        done = completed_candles(stock.candles, ctx.session)
        if done.empty:
            hist_reason = "no historical candles"
        else:
            last_hist_date = done["date"].iloc[-1]
            if last_hist_date < ctx.expected_last_completed:
                hist_reason = (
                    f"history ends {last_hist_date}, expected {ctx.expected_last_completed} "
                    "(refresh history cache)"
                )
            else:
                hist_ok = True

    # ---- Condition 1: volume expansion -------------------------------------
    pw = previous_week_volume(stock.candles, ctx.session, ctx.trading_calendar) if hist_ok else None
    today_vol = q.volume if (q and quote_ok and q.volume is not None and q.volume >= 0) else None
    if not quote_ok:
        c1 = ConditionResult(NO_DATA, quote_reason)
    elif today_vol is None:
        c1 = ConditionResult(NO_DATA, "quote has no volume")
    elif not hist_ok:
        c1 = ConditionResult(NO_DATA, hist_reason)
    elif not pw.ok:
        c1 = ConditionResult(NO_DATA, f"{pw.reason}: {pw.detail}")
    elif pw.value <= 0:
        c1 = ConditionResult(NO_DATA, "previous week volume is zero")
    else:
        c1 = ConditionResult(PASS if today_vol > params.volume_multiplier * pw.value else FAIL)

    # ---- Condition 2: market capitalisation --------------------------------
    mc = mcap_table.lookup(stock.symbol, ltp, ctx.session, params.market_cap_max_age_days)
    if mc.ok:
        c2 = ConditionResult(PASS if mc.value_cr > params.min_market_cap_cr else FAIL)
    else:
        c2 = ConditionResult(NO_DATA, f"market cap {mc.status}")

    # ---- Condition 3: price above previous close ---------------------------
    prev_close = q.prev_close if q is not None else None
    hist_close = last_completed_close(stock.candles, ctx.session) if hist_ok else None
    if not quote_ok:
        c3 = ConditionResult(NO_DATA, quote_reason)
    elif not traded:
        c3 = ConditionResult(NO_DATA, quote_reason)
    elif not _valid_price(prev_close):
        c3 = ConditionResult(NO_DATA, "quote has no previous close")
    elif (
        hist_close is not None
        and hist_close.ok
        and hist_close.value > 0
        and abs(prev_close - hist_close.value) / hist_close.value * 100 > params.prev_close_tolerance_pct
    ):
        c3 = ConditionResult(
            NO_DATA,
            f"quote prev close {prev_close} differs from history close {hist_close.value} "
            f"(quote vs history close divergence > {params.prev_close_tolerance_pct}%; sanity check only, does not detect all corporate actions)",
        )
    else:
        c3 = ConditionResult(PASS if ltp > prev_close else FAIL)

    # ---- Condition 4: price above 50-day SMA -------------------------------
    sma = sma_completed(stock.candles, ctx.session, params.sma_window) if hist_ok else None
    if not quote_ok:
        c4 = ConditionResult(NO_DATA, quote_reason)
    elif not traded:
        c4 = ConditionResult(NO_DATA, quote_reason)
    elif not hist_ok:
        c4 = ConditionResult(NO_DATA, hist_reason)
    elif not sma.ok:
        c4 = ConditionResult(NO_DATA, f"{sma.reason}: {sma.detail}")
    else:
        c4 = ConditionResult(PASS if ltp > sma.value else FAIL)

    conds = [c1, c2, c3, c4]

    # ---- Independent Underlying Data Issues ---------------------------------
    # Every data issue is recorded independently regardless of primary category.
    # A stock missing both market cap and history records both issues here.
    underlying_issues: list[str] = []
    if not quote_ok:
        underlying_issues.append("STALE_QUOTE")
    elif not traded:
        underlying_issues.append("NO_TRADES")

    if not mc.ok:
        underlying_issues.append("MISSING_MARKET_CAP")

    if not hist_ok or (sma is not None and not sma.ok) or (pw is not None and not pw.ok) or (pw is not None and pw.ok and pw.value <= 0):
        underlying_issues.append("INCOMPLETE_HISTORY")

    if c3.state == NO_DATA and "differs" in c3.reason:
        underlying_issues.append("PRICE_DISCREPANCY")

    if any(c.state == NO_DATA for c in conds) and not underlying_issues:
        underlying_issues.append("OTHER_DATA_ISSUE")

    # ---- Mutually Exclusive Primary Exception Hierarchy ---------------------
    # Assigns exactly one top-level category for mutual exclusivity:
    primary_exception: str | None = None
    if not quote_ok:
        primary_exception = "STALE_QUOTE"
    elif not traded:
        primary_exception = "NO_TRADES"
    elif not mc.ok:
        primary_exception = "MISSING_MARKET_CAP"
    elif not hist_ok or (sma is not None and not sma.ok) or (pw is not None and not pw.ok) or (pw is not None and pw.ok and pw.value <= 0):
        primary_exception = "INCOMPLETE_HISTORY"
    elif c3.state == NO_DATA and "differs" in c3.reason:
        primary_exception = "PRICE_DISCREPANCY"
    elif any(c.state == NO_DATA for c in conds):
        primary_exception = "OTHER_DATA_ISSUE"

    # Status Precedence:
    # 1. Incomplete/unreliable data -> strictly DATA_ISSUE (never legitimate filter failure)
    # 2. Complete data & all 4 pass -> MATCH
    # 3. Complete data & any fail   -> FILTER_FAIL
    if primary_exception is not None:
        overall = DATA_ISSUE
    elif all(c.state == PASS for c in conds):
        overall = MATCH
    else:
        overall = FILTER_FAIL

    pw_val = pw.value if (pw is not None and pw.ok) else None
    vol_multiple = None
    if today_vol is not None and pw_val is not None and pw_val > 0:
        vol_multiple = round(today_vol / pw_val, 2)
    vol_formula = (
        f"{today_vol:,.0f} / {pw_val:,.0f}"
        if (today_vol is not None and pw_val is not None and pw_val > 0)
        else "N/A"
    )

    # Time-adjusted volume against comparable historical intraday data
    time_adj_mult, time_adj_desc = calculate_time_adjusted_volume(
        today_vol, getattr(stock, "hist_intraday_volume", None)
    )

    # Separate linear session pace heuristic (informational only)
    linear_pace, linear_desc, sess_frac = calculate_linear_session_pace(
        today_vol, pw_val, ctx.now, ctx.market_open
    )

    pct_change = None
    if ltp is not None and _valid_price(prev_close):
        pct_change = (ltp - prev_close) / prev_close * 100

    sma_val = sma.value if (sma is not None and sma.ok) else None
    sma_dist_pct = (
        (ltp - sma_val) / sma_val * 100
        if (ltp is not None and sma_val is not None and sma_val > 0)
        else None
    )

    candle_count = len(done) if (not stock.history_error and stock.candles is not None) else 0
    quote_age_secs = (
        (to_ist(ctx.now) - to_ist(q.timestamp)).total_seconds()
        if (q and q.timestamp)
        else None
    )

    reasons = "; ".join(
        f"C{i}: {c.reason}" for i, c in enumerate(conds, start=1) if c.state == NO_DATA and c.reason
    )
    return {
        "instrument_token": stock.instrument_token,
        "symbol": stock.symbol,
        "company_name": stock.name,
        "status": overall,
        "primary_exception": primary_exception,
        "underlying_issues": underlying_issues,
        "underlying_issues_str": ", ".join(underlying_issues) if underlying_issues else "NONE",
        "ltp": ltp,
        "prev_close": prev_close if _valid_price(prev_close) else None,
        "pct_change": pct_change,
        "market_cap_cr": mc.value_cr,
        "market_cap_status": mc.status,
        "market_cap_as_of": mc.as_of,
        "market_cap_method": mc.method,
        "market_cap_source": mc.source,
        "today_volume": today_vol,
        "prev_week_volume": pw_val,
        "prev_week_range": pw.detail if pw is not None else "",
        "volume_multiple": vol_multiple,
        "volume_multiple_formula": vol_formula,
        "time_adjusted_volume_multiplier": time_adj_mult,
        "time_adjusted_formula": time_adj_desc,
        "linear_session_pace": linear_pace,
        "linear_session_pace_formula": linear_desc,
        "session_fraction": sess_frac,
        "sma50": sma_val,
        "sma50_distance_pct": sma_dist_pct,
        "candle_count": candle_count,
        "quote_time": to_ist(q.timestamp) if (q and q.timestamp) else None,
        "quote_age_seconds": quote_age_secs,
        "last_trade_time": to_ist(q.last_trade_time) if (q and q.last_trade_time) else None,
        "history_last_date": last_hist_date,
        "history_status": "OK" if hist_ok else "ISSUE",
        "c1_volume": c1.state,
        "c1_reason": c1.reason,
        "c2_mcap": c2.state,
        "c2_reason": c2.reason,
        "c3_prev_close": c3.state,
        "c3_reason": c3.reason,
        "c4_sma": c4.state,
        "c4_reason": c4.reason,
        "data_issues": reasons,
        "active_session": ctx.session,
        "expected_last_completed": ctx.expected_last_completed,
    }


def screen_universe(
    stocks: Iterable[StockInput],
    ctx: ScreenContext,
    params: ScreenParams,
    mcap_table: MarketCapTable,
) -> pd.DataFrame:
    rows = [evaluate_stock(s, ctx, params, mcap_table) for s in stocks]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("volume_multiple", ascending=False, na_position="last").reset_index(drop=True)
