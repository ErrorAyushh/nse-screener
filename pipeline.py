"""Glue between the Kite data layer and the pure screener.

Used by both the Streamlit app and the command-line history refresh, so the
dashboard and scheduled jobs follow exactly the same rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

import pandas as pd

from calendar_utils import (
    derive_trading_calendar,
    is_market_open,
    last_completed_trading_day,
    load_holidays,
    previous_trading_day,
    session_date,
)
from config import SMA_WINDOW, Settings
from kite_data import HistoryCache, HistoryRefreshReport, fetch_quotes, filter_equity_universe, load_instruments
from market_cap import MarketCapTable
from screener import ScreenContext, ScreenParams, StockInput, screen_universe


@dataclass
class SessionInfo:
    now: datetime
    session: date
    last_completed: date
    market_open: bool
    prior_session: date | None = None


def session_info(settings: Settings, now: datetime) -> SessionInfo:
    holidays = load_holidays(settings.holidays_csv)
    s = session_date(now, holidays)
    last_comp = last_completed_trading_day(now, holidays)
    prior = previous_trading_day(s, holidays)
    return SessionInfo(now, s, last_comp, is_market_open(now, holidays), prior_session=prior)


def get_universe(kite, settings: Settings, today: date, force: bool = False) -> tuple[pd.DataFrame, dict]:
    raw = load_instruments(kite, settings.cache_dir, today, force=force)
    return filter_equity_universe(raw, settings.allowed_series_suffixes)


def history_tokens(universe: pd.DataFrame, mcap: MarketCapTable, only_with_mcap: bool) -> list[int]:
    if not only_with_mcap:
        return universe["instrument_token"].astype(int).tolist()
    mask = universe["tradingsymbol"].str.upper().isin(set(mcap.records.index))
    return universe.loc[mask, "instrument_token"].astype(int).tolist()


def refresh_history(
    kite,
    settings: Settings,
    universe: pd.DataFrame,
    mcap: MarketCapTable,
    info: SessionInfo,
    force: bool = False,
    progress: Callable[[float, str], None] | None = None,
) -> HistoryRefreshReport:
    cache = HistoryCache(settings.cache_dir)
    tokens = history_tokens(universe, mcap, settings.history_only_with_market_cap)
    return cache.refresh(kite, tokens, info.last_completed, settings.history_lookback_days, force, progress)


def load_histories(settings: Settings, tokens: list[int]) -> tuple[dict[int, pd.DataFrame | None], dict[int, str]]:
    cache = HistoryCache(settings.cache_dir)
    hist = {t: cache.load(t) for t in tokens}
    errs = {t: cache.error(t) for t in tokens if cache.error(t)}
    return hist, errs


def build_context(info: SessionInfo, histories: dict[int, pd.DataFrame | None]) -> ScreenContext:
    calendar = derive_trading_calendar(
        (h["date"].tolist() for h in histories.values() if h is not None and not h.empty)
    )
    expected_last = info.prior_session if info.prior_session is not None else info.last_completed
    return ScreenContext(
        now=info.now,
        session=info.session,
        expected_last_completed=expected_last,
        market_open=info.market_open,
        trading_calendar=calendar,
    )


def params_from(settings: Settings, min_mcap: float, multiplier: float) -> ScreenParams:
    return ScreenParams(
        min_market_cap_cr=min_mcap,
        volume_multiplier=multiplier,
        sma_window=SMA_WINDOW,
        market_cap_max_age_days=settings.market_cap_max_age_days,
        quote_stale_seconds=settings.quote_stale_seconds,
        prev_close_tolerance_pct=settings.prev_close_tolerance_pct,
    )


def run_scan(
    kite,
    universe: pd.DataFrame,
    histories: dict[int, pd.DataFrame | None],
    history_errors: dict[int, str],
    ctx: ScreenContext,
    params: ScreenParams,
    mcap: MarketCapTable,
    only_with_mcap: bool,
    progress: Callable[[float], None] | None = None,
) -> pd.DataFrame:
    symbols = universe["tradingsymbol"].tolist()
    quotes = fetch_quotes(kite, symbols, progress)
    stocks = []
    for row in universe.itertuples(index=False):
        tok = int(row.instrument_token)
        err = history_errors.get(tok, "")
        if tok not in histories and only_with_mcap:
            err = err or "history not downloaded (no market-cap record)"
        stocks.append(
            StockInput(
                instrument_token=tok,
                symbol=row.tradingsymbol,
                name=row.name,
                quote=quotes.get(row.tradingsymbol),
                candles=histories.get(tok),
                history_error=err,
            )
        )
    return screen_universe(stocks, ctx, params, mcap)
