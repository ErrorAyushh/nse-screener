"""NSE Intraday Screening Terminal (Streamlit).

Professional institutional market-data screening and analytics terminal.
Zero order-routing, execution, or portfolio management functionality.

Run:  streamlit run app.py
"""
from __future__ import annotations

import hashlib
import math
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import altair as alt

from calendar_utils import now_ist
from config import QUOTE_BATCH_SIZE, load_settings
from indicators import prepare_chart_data
from kite_data import (
    AuthError,
    DataPermissionError,
    DataUnavailableError,
    HistoryCache,
    make_client,
    token_probably_expired,
    verify_session,
)
from market_cap import load_market_cap
from pipeline import (
    build_context,
    get_universe,
    history_tokens,
    load_histories,
    params_from,
    refresh_history,
    run_scan,
    session_info,
)
from screener import DATA_ISSUE, FAIL, FILTER_FAIL, MATCH

st.set_page_config(
    page_title="NSE Intraday Screener | Terminal",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Institutional Financial Terminal CSS Theme
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap');

      /* Core Color Palette: Bloomberg / Koyfin Matte Charcoal & Deep Navy */
      :root {
        --bg-app: #080b11;
        --bg-panel: #0d121c;
        --bg-card: #121824;
        --bg-card-hover: #161e2e;
        --border-primary: #1b2333;
        --border-subtle: #242e42;
        --text-primary: #f1f5f9;
        --text-secondary: #94a3b8;
        --text-muted: #64748b;
        --accent-blue: #38bdf8;
        --accent-indigo: #6366f1;
        --bull-green: #10b981;
        --bull-green-bg: rgba(16, 185, 129, 0.08);
        --bear-red: #f43f5e;
        --bear-red-bg: rgba(244, 63, 94, 0.08);
        --warn-amber: #f59e0b;
        --warn-amber-bg: rgba(245, 158, 11, 0.08);
      }

      /* Global Layout & Typography */
      html, body, [class*="css"] {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        background-color: var(--bg-app);
        color: var(--text-primary);
        letter-spacing: -0.01em;
      }

      /* Streamlit Header Clearance */
      header[data-testid="stHeader"] {
        background: transparent !important;
        height: 2.5rem !important;
        z-index: 10 !important;
      }

      .block-container {
        padding-top: 3.5rem !important;
        padding-bottom: 2rem !important;
        padding-left: 1.5rem !important;
        padding-right: 1.5rem !important;
        max-width: 100% !important;
      }

      /* Institutional Top Ribbon */
      .pro-header {
        background: linear-gradient(180deg, #0f1523 0%, #0a0f1a 100%);
        border: 1px solid var(--border-primary);
        border-radius: 8px;
        padding: 14px 20px;
        margin-top: 0.2rem;
        margin-bottom: 12px;
        display: flex;
        flex-wrap: wrap;
        justify-content: space-between;
        align-items: center;
        gap: 12px;
        box-sizing: border-box;
      }

      .pro-title-wrap {
        display: flex;
        align-items: center;
        gap: 10px;
      }

      .pro-title {
        font-family: 'Inter', sans-serif;
        font-size: 1.25rem;
        font-weight: 700;
        letter-spacing: -0.01em;
        color: #ffffff;
        display: flex;
        align-items: center;
        line-height: 1.2;
        margin: 0;
        padding: 0;
      }

      .pro-tag {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.68rem;
        font-weight: 600;
        color: var(--accent-blue);
        background: rgba(56, 189, 248, 0.1);
        border: 1px solid rgba(56, 189, 248, 0.25);
        padding: 3px 8px;
        border-radius: 4px;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        line-height: 1.2;
      }

      .pro-pills {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 8px;
        line-height: 1;
      }

      .pro-pill {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.72rem;
        font-weight: 500;
        padding: 5px 10px;
        border-radius: 4px;
        border: 1px solid var(--border-subtle);
        background: var(--bg-card);
        color: var(--text-secondary);
        display: inline-flex;
        align-items: center;
        gap: 6px;
        line-height: 1.2;
      }

      .pro-pill-live {
        border-color: rgba(16, 185, 129, 0.3);
        background: var(--bull-green-bg);
        color: #34d399;
      }

      .pro-pill-closed {
        border-color: var(--border-subtle);
        background: #111724;
        color: #94a3b8;
      }

      .pro-pill-auth {
        border-color: rgba(56, 189, 248, 0.25);
        background: rgba(56, 189, 248, 0.08);
        color: var(--accent-blue);
      }

      .pro-pill-warn {
        border-color: rgba(245, 158, 11, 0.3);
        background: var(--warn-amber-bg);
        color: #fbbf24;
      }

      .dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        display: inline-block;
      }
      .dot-green { background-color: #10b981; box-shadow: 0 0 6px rgba(16, 185, 129, 0.6); }
      .dot-amber { background-color: #f59e0b; box-shadow: 0 0 6px rgba(245, 158, 11, 0.6); }
      .dot-slate { background-color: #64748b; }

      /* Technical Conditions Strip */
      .criteria-strip {
        display: flex;
        align-items: center;
        justify-content: space-between;
        flex-wrap: wrap;
        gap: 10px;
        padding: 9px 16px;
        background: #090e17;
        border: 1px solid var(--border-primary);
        border-radius: 6px;
        margin-bottom: 14px;
        font-size: 0.76rem;
      }
      .criteria-item {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        color: var(--text-secondary);
        white-space: nowrap;
      }
      .criteria-label {
        font-weight: 600;
        color: var(--accent-blue);
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.74rem;
      }
      .criteria-sep {
        color: #242e42;
        font-weight: 300;
        user-select: none;
      }

      /* KPI Summary Cards */
      div[data-testid="stMetric"] {
        background: var(--bg-panel) !important;
        border: 1px solid var(--border-primary) !important;
        border-radius: 6px !important;
        padding: 12px 16px !important;
        box-shadow: none !important;
        transition: border-color 0.15s ease;
      }

      div[data-testid="stMetric"]:hover {
        border-color: var(--border-subtle) !important;
      }

      div[data-testid="stMetricLabel"] {
        font-size: 0.7rem !important;
        font-weight: 600 !important;
        text-transform: uppercase !important;
        letter-spacing: 0.05em !important;
        color: var(--text-muted) !important;
      }

      div[data-testid="stMetricValue"] {
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 1.35rem !important;
        font-weight: 600 !important;
        color: #ffffff !important;
        letter-spacing: -0.02em !important;
      }

      /* Primary Action Button (Terminal Blue/Cyan instead of coral) */
      button[kind="primary"], .stButton > button[kind="primary"] {
        background: #0284c7 !important;
        border: 1px solid #38bdf8 !important;
        color: #ffffff !important;
        font-weight: 600 !important;
        font-size: 0.82rem !important;
        letter-spacing: 0.02em !important;
        box-shadow: 0 2px 6px rgba(2, 132, 199, 0.25) !important;
        transition: all 0.15s ease !important;
      }
      button[kind="primary"]:hover, .stButton > button[kind="primary"]:hover {
        background: #0369a1 !important;
        border-color: #7dd3fc !important;
      }

      /* Sidebar Customization */
      section[data-testid="stSidebar"] {
        background-color: #080c13 !important;
        border-right: 1px solid var(--border-primary) !important;
      }
      section[data-testid="stSidebar"] .block-container {
        padding-top: 1.5rem !important;
      }

      /* Professional Tab Strip */
      .stTabs [data-baseweb="tab-list"] {
        gap: 2px !important;
        background-color: #0b0f19 !important;
        border-radius: 6px !important;
        padding: 3px !important;
        border: 1px solid var(--border-primary) !important;
      }

      .stTabs [data-baseweb="tab"] {
        border-radius: 4px !important;
        padding: 6px 14px !important;
        font-size: 0.8rem !important;
        font-weight: 500 !important;
        color: var(--text-secondary) !important;
        border: none !important;
      }

      .stTabs [aria-selected="true"] {
        background-color: #171f2f !important;
        color: #ffffff !important;
        font-weight: 600 !important;
      }

      /* Alert and Notification Containers */
      .pro-alert {
        padding: 10px 14px;
        border-radius: 6px;
        margin-bottom: 12px;
        font-size: 0.82rem;
        display: flex;
        align-items: center;
        gap: 10px;
        border: 1px solid transparent;
      }

      .pro-alert-closed {
        background: #0d121c;
        border-color: var(--border-subtle);
        color: var(--text-secondary);
      }

      .pro-alert-warn {
        background: var(--warn-amber-bg);
        border-color: rgba(245, 158, 11, 0.25);
        color: #fbbf24;
      }

      /* Minimal Scrollbars */
      ::-webkit-scrollbar {
        width: 6px;
        height: 6px;
      }
      ::-webkit-scrollbar-track {
        background: #080b11;
      }
      ::-webkit-scrollbar-thumb {
        background: #1c2536;
        border-radius: 3px;
      }
      ::-webkit-scrollbar-thumb:hover {
        background: #2b3952;
      }

      .mono-text {
        font-family: 'JetBrains Mono', monospace;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

ss = st.session_state
ss.setdefault("results", None)
ss.setdefault("last_success", None)
ss.setdefault("last_attempt", None)
ss.setdefault("last_error", "")
ss.setdefault("scan_meta", {})
ss.setdefault("env_reload", 0)


# ---------------------------------------------------------------------------
# Cached Data Layer & Connectors
# ---------------------------------------------------------------------------
def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12] if token else ""


@st.cache_resource(show_spinner=False)
def get_kite(api_key: str, token_fp: str, _settings):
    kite = make_client(_settings)
    user = verify_session(kite)
    return kite, user


@st.cache_data(ttl=6 * 3600, show_spinner="Loading NSE instrument universe…")
def cached_universe(token_fp: str, day: str, _kite, _settings, force_key: int = 0):
    return get_universe(_kite, _settings, datetime.fromisoformat(day).date(), force=bool(force_key))


@st.cache_data(show_spinner=False)
def cached_mcap(path: str, mtime: float):
    return load_market_cap(Path(path))


@st.cache_data(show_spinner="Loading historical candle database…")
def cached_histories(meta_mtime: float, tokens: tuple[int, ...], _settings):
    return load_histories(_settings, list(tokens))


def mtime(p) -> float:
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return 0.0


settings = load_settings(override=ss.env_reload > 0)
fp = _token_fingerprint(settings.access_token)


# ---------------------------------------------------------------------------
# Sidebar: Professional Configuration & Execution Controls
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("#### Screening Parameters")
    min_mcap = st.number_input(
        "Min Market Cap (₹ Cr)",
        min_value=0.0,
        value=float(settings.min_market_cap_cr),
        step=100.0,
        help="Condition 2: Cash-equity market capitalisation must exceed this threshold.",
    )
    multiplier = st.number_input(
        "Volume Multiplier (vs Prev Week)",
        min_value=0.1,
        value=float(settings.volume_multiplier),
        step=0.5,
        help="Condition 1: Today's cumulative volume > multiplier × previous Mon-Fri week's total volume.",
    )

    st.markdown("---")
    st.markdown("#### Execution & Polling")
    auto = st.toggle("Auto-refresh during market hours", value=True)
    interval = st.number_input(
        "Refresh Interval (seconds)",
        min_value=settings.min_refresh_interval_seconds,
        max_value=3600,
        value=int(settings.refresh_interval_seconds),
        step=5,
        help="Quote sweeps execute in ~500-instrument batches adhering to Kite Connect rate caps.",
    )
    manual = st.button("Refresh Quotes Now", use_container_width=True, type="primary")

    st.markdown("---")
    st.markdown("#### Database Maintenance")
    force_hist = st.checkbox("Force full historical refetch", value=False)
    do_hist = st.button("Update Historical Candle Cache", use_container_width=True)
    reload_universe = st.button("Reload NSE Instrument Master", use_container_width=True)

    if st.button("Reconnect Kite Session", use_container_width=True):
        ss.env_reload += 1
        st.cache_resource.clear()
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    st.caption(
        "**Compliance & Scope:** Market-data analytical screening terminal. "
        "No orders are generated, routed, or placed. Zero financial advisory provided."
    )


# ---------------------------------------------------------------------------
# Session & Authentication Gatekeeper
# ---------------------------------------------------------------------------
def fail_page(msg: str, help_md: str = "") -> None:
    st.error(f"Authentication Error: {msg}")
    if help_md:
        st.markdown(help_md)
    st.stop()


LOGIN_HELP = (
    "**To renew authentication:**\n"
    "1. Run `python auth_login.py` in PowerShell.\n"
    "2. Complete Zerodha 2FA authentication in the browser.\n"
    "3. Paste the redirect URL into the terminal prompt.\n"
    "4. Click **Reconnect Kite Session** in the sidebar."
)

if not settings.api_key:
    fail_page("KITE_API_KEY is not configured.", "Create `.env` from `.env.example` and set your API credentials.")
if not settings.access_token:
    fail_page("Active Kite access token not found.", LOGIN_HELP)
if token_probably_expired(settings.access_token_created_at):
    fail_page("Kite access token expired at 06:00 IST.", LOGIN_HELP)

try:
    kite, user_id = get_kite(settings.api_key, fp, settings)
except AuthError as exc:
    get_kite.clear()
    fail_page(str(exc), LOGIN_HELP)
except (DataPermissionError, DataUnavailableError) as exc:
    get_kite.clear()
    fail_page(f"Connection failure: {exc}")

now = now_ist()
info = session_info(settings, now)

if reload_universe:
    cached_universe.clear()
try:
    universe, uni_stats = cached_universe(fp, now.date().isoformat(), kite, settings, int(reload_universe))
except (AuthError, DataPermissionError, DataUnavailableError) as exc:
    fail_page(f"Instrument universe failure: {exc}", LOGIN_HELP if isinstance(exc, AuthError) else "")

mcap = cached_mcap(str(settings.market_cap_csv), mtime(settings.market_cap_csv))
if mcap.load_error:
    st.warning(f"Market-cap reference unavailable: {mcap.load_error} Condition 2 evaluates to NO_DATA.")

hist_tokens = history_tokens(universe, mcap, settings.history_only_with_market_cap)
hcache = HistoryCache(settings.cache_dir)

# History Cache Refresh
if do_hist:
    bar = st.progress(0.0, text="Synchronizing daily historical candles…")
    try:
        rep = refresh_history(
            kite,
            settings,
            universe,
            mcap,
            info,
            force=force_hist,
            progress=lambda f, lbl: bar.progress(min(f, 1.0), text=f"Downloading candles… {lbl}"),
        )
        bar.empty()
        st.success(
            f"Historical cache updated through {info.last_completed}: "
            f"fetched {rep.fetched}, fresh {rep.skipped_fresh}, "
            f"corporate adjustment refetches {rep.full_refetch}, errors {len(rep.errors)}."
        )
        cached_histories.clear()
    except AuthError as exc:
        bar.empty()
        fail_page(str(exc), LOGIN_HELP)
    except DataPermissionError as exc:
        bar.empty()
        fail_page(str(exc))
    hcache = HistoryCache(settings.cache_dir)

fresh_hist = sum(
    1
    for t in hist_tokens
    if (hcache.fetched_through(t) or date.min) >= info.last_completed and not hcache.error(t)
)
if hist_tokens and fresh_hist < len(hist_tokens):
    st.warning(
        f"Historical candle database is synchronized for {fresh_hist:,} of {len(hist_tokens):,} instruments "
        f"(target session: {info.last_completed}). Stocks with incomplete history will be tagged as DATA_ISSUE."
    )

histories, hist_errors = cached_histories(mtime(hcache.meta_path), tuple(hist_tokens), settings)


# ---------------------------------------------------------------------------
# Universe Scan Logic
# ---------------------------------------------------------------------------
def do_scan() -> None:
    ss.last_attempt = now_ist()
    ctx = build_context(session_info(settings, now_ist()), histories)
    params = params_from(settings, min_mcap, multiplier)
    bar = st.progress(0.0, text="Executing market-wide quote sweep…")
    try:
        df = run_scan(
            kite,
            universe,
            histories,
            hist_errors,
            ctx,
            params,
            mcap,
            settings.history_only_with_market_cap,
            progress=lambda f: bar.progress(min(f, 1.0), text="Executing market-wide quote sweep…"),
        )
        ss.results = df
        ss.last_success = now_ist()
        ss.last_error = ""
        ss.scan_meta = {
            "session": ctx.session,
            "market_open": ctx.market_open,
            "min_mcap": min_mcap,
            "multiplier": multiplier,
            "calendar_days": len(ctx.trading_calendar),
        }
    except AuthError as exc:
        ss.last_error = f"{exc}"
        get_kite.clear()
    except (DataPermissionError, DataUnavailableError) as exc:
        ss.last_error = f"Quote polling failed: {exc}"
    finally:
        bar.empty()


def fmt_num(x, nd=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, nd)


# ---------------------------------------------------------------------------
# Presentation Rendering
# ---------------------------------------------------------------------------
def render(curr_info=None) -> None:
    if curr_info is None:
        curr_info = session_info(settings, now_ist())
    df: pd.DataFrame | None = ss.results
    meta = ss.scan_meta
    last = ss.last_success
    live_now = curr_info.market_open
    age = (now_ist() - last).total_seconds() if last else None
    stale_after = max(2 * int(interval), 120)
    is_stale = live_now and age is not None and age > stale_after
    params_changed = bool(meta) and (meta.get("min_mcap") != min_mcap or meta.get("multiplier") != multiplier)

    # Top Status Ribbon
    market_pill = (
        '<span class="pro-pill pro-pill-live"><span class="dot dot-green"></span>MARKET OPEN (LIVE)</span>'
        if live_now
        else f'<span class="pro-pill pro-pill-closed"><span class="dot dot-slate"></span>MARKET CLOSED (EOD SNAPSHOT: {curr_info.session:%d-%b-%Y})</span>'
    )
    user_pill = f'<span class="pro-pill pro-pill-auth">KITE: {user_id}</span>'
    refresh_pill = (
        f'<span class="pro-pill">LAST SWEEP: {last.strftime("%H:%M:%S IST")}</span>'
        if last
        else '<span class="pro-pill">AWAITING INITIAL SWEEP</span>'
    )
    stale_pill = (
        f'<span class="pro-pill pro-pill-warn"><span class="dot dot-amber"></span>QUOTE LATENCY: {int(age)}s</span>'
        if is_stale
        else ""
    )

    header_html = f"""
    <div class="pro-header">
      <div class="pro-title-wrap">
        <span class="pro-title">NSE INTRADAY SCREENER</span>
        <span class="pro-tag">CASH EQ</span>
      </div>
      <div class="pro-pills">
        {market_pill}
        {user_pill}
        {refresh_pill}
        {stale_pill}
      </div>
    </div>
    """
    st.markdown(header_html, unsafe_allow_html=True)

    # Technical Screening Conditions Bar
    criteria_bar = f"""
    <div class="criteria-strip">
      <div class="criteria-item"><span class="criteria-label">C1 [VOL EXPANSION]</span> Today Vol &gt; {multiplier:.1f}× Prev Week Mon–Fri</div>
      <div class="criteria-sep">|</div>
      <div class="criteria-item"><span class="criteria-label">C2 [MARKET CAP]</span> Capitalisation &gt; ₹{min_mcap:,.0f} Cr</div>
      <div class="criteria-sep">|</div>
      <div class="criteria-item"><span class="criteria-label">C3 [PRICE MOMENTUM]</span> LTP &gt; Prev Session Close</div>
      <div class="criteria-sep">|</div>
      <div class="criteria-item"><span class="criteria-label">C4 [TREND FILTER]</span> LTP &gt; 50-Session Daily SMA</div>
    </div>
    """
    st.markdown(criteria_bar, unsafe_allow_html=True)

    if ss.last_error:
        st.error(f"Execution Alert: {ss.last_error}")
        if "token" in ss.last_error.lower():
            st.markdown(LOGIN_HELP)

    # Institutional Metric Cards
    n_match = int((df["status"] == MATCH).sum()) if df is not None and not df.empty else 0
    n_issue = int((df["status"] == DATA_ISSUE).sum()) if df is not None and not df.empty else 0

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Eligible Universe", f"{uni_stats['eligible']:,}", help="NSE cash-equity mainboard securities after series filter")
    col2.metric("Screen Matches", f"{n_match:,}", help="Securities satisfying all four conditions simultaneously")
    col3.metric("Data Exceptions", f"{n_issue:,}", help="Instruments with missing history, stale quotes, or reference anomalies")
    col4.metric("Min Market Cap", f"> ₹{min_mcap:,.0f} Cr", help="Active minimum market capitalisation threshold")
    col5.metric("Volume Multiple", f"> {multiplier:.1f}×", help="Active weekly volume expansion multiple")

    # Advisory Banners
    if not live_now and last:
        st.markdown(
            f"""
            <div class="pro-alert pro-alert-closed">
              <span><strong>Notice:</strong> NSE cash market is closed. Displaying final verified EOD snapshot from the <strong>{meta.get('session', curr_info.session):%d-%b-%Y}</strong> trading session. Real-time intraday quote polling resumes at 09:15 IST.</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    elif is_stale:
        st.markdown(
            f"""
            <div class="pro-alert pro-alert-warn">
              <span><strong>Data Advisory:</strong> Quotes are currently {int(age)}s old (exceeding polling threshold). Click <strong>Refresh Quotes Now</strong> in the sidebar to synchronize.</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

    if params_changed:
        st.info("Screening parameters modified. Trigger **Refresh Quotes Now** to apply the updated thresholds.")

    # Technical Diagnostics Expander
    with st.expander("System & Database Diagnostics", expanded=False):
        c_diag1, c_diag2, c_diag3 = st.columns(3)
        with c_diag1:
            st.markdown("**Session Parameters**")
            st.write({
                "Active Trading Session": str(curr_info.session),
                "Last Completed Trading Day": str(curr_info.last_completed),
                "Derived Calendar Days": meta.get("calendar_days", "—"),
                "Eligible EQ Securities": f"{uni_stats['eligible']:,}",
            })
        with c_diag2:
            st.markdown("**Historical Candle Database**")
            st.write({
                "Completed History Required": str(curr_info.last_completed),
                "Synchronized Instruments": f"{fresh_hist:,} / {len(hist_tokens):,}",
                "Historical Lookback": f"{settings.history_lookback_days} calendar days",
                "Moving Average Window": "50 completed trading sessions",
            })
        with c_diag3:
            st.markdown("**Reference Capitalisation Data**")
            st.write({
                "Source Master": settings.market_cap_csv.name,
                "Valid Capitalisation Records": f"{len(mcap.records):,}",
                "As-of Window": f"{mcap.oldest_as_of} → {mcap.latest_as_of}",
                "Max Age Allowed": f"{settings.market_cap_max_age_days} days",
                "Unusable Records": len(mcap.problems),
            })

        if df is not None and not df.empty:
            st.markdown("---")
            st.markdown("###### Universe Coverage Reconciliation")
            n_eligible = uni_stats.get("eligible", len(df))
            n_mat = int((df["status"] == MATCH).sum())
            n_fail = int((df["status"] == FILTER_FAIL).sum())
            n_iss = int((df["status"] == DATA_ISSUE).sum())

            r_col1, r_col2, r_col3, r_col4 = st.columns(4)
            r_col1.metric("Eligible Universe", f"{n_eligible:,}", help="NSE mainboard EQ series securities")
            r_col2.metric("Qualified Matches", f"{n_mat:,}", help="Passes C1, C2, C3, and C4 simultaneously")
            r_col3.metric("Evaluated Filter Fails", f"{n_fail:,}", help="Clean data, failed one or more conditions")
            r_col4.metric("Data Exceptions", f"{n_iss:,}", help="Missing or incomplete reference data")

            reconciled = (n_mat + n_fail + n_iss == n_eligible)
            st.caption(
                f"Universe Reconciliation: {'✓ 100% RECONCILED' if reconciled else '⚠ DISCREPANCY'} "
                f"({n_mat} Matches + {n_fail} Filter Fails + {n_iss} Exceptions = {n_mat + n_fail + n_iss:,} / {n_eligible:,})"
            )

            # 1. Mutually Exclusive Primary Exceptions (sums strictly to n_iss)
            issues_sub = df[df["status"] == DATA_ISSUE]
            exc_counts = issues_sub["primary_exception"].value_counts().to_dict()

            st.markdown("**1. Primary Exception Classification (Mutually Exclusive — Sums to Total Exceptions)**")
            st.write({
                "Missing Market Cap (Unmapped AMFI)": f"{exc_counts.get('MISSING_MARKET_CAP', 0):,}",
                "Incomplete History (< 50 daily bars)": f"{exc_counts.get('INCOMPLETE_HISTORY', 0):,}",
                "Stale / Unavailable Quote": f"{exc_counts.get('STALE_QUOTE', 0):,}",
                "No Trades This Session": f"{exc_counts.get('NO_TRADES', 0):,}",
                "Prev Close Discrepancy": f"{exc_counts.get('PRICE_DISCREPANCY', 0):,}",
                "Other Reference Issue": f"{exc_counts.get('OTHER_DATA_ISSUE', 0):,}",
                "Total Reconciled Exceptions": f"{n_iss:,}",
            })

            # 2. Independent Overlapping Underlying Data Issues
            all_underlying = []
            if "underlying_issues" in issues_sub.columns:
                for item in issues_sub["underlying_issues"]:
                    if isinstance(item, list):
                        all_underlying.extend(item)
                    elif isinstance(item, str):
                        all_underlying.extend([x.strip() for x in item.split(",") if x.strip()])
            from collections import Counter
            underlying_counts = Counter(all_underlying)

            st.markdown("**2. Overlapping Underlying Data Issues (Independent Universe Counts)**")
            st.caption(
                "Securities may suffer from multiple data issues concurrently (e.g. missing both market cap and historical candles). "
                "These independent totals report every underlying issue across the universe without double-counting top-level status."
            )
            st.write({
                "Total Missing / Stale Market Cap Issues": f"{underlying_counts.get('MISSING_MARKET_CAP', 0):,}",
                "Total Incomplete / Missing History Issues": f"{underlying_counts.get('INCOMPLETE_HISTORY', 0):,}",
                "Total Stale / Unavailable Quote Issues": f"{underlying_counts.get('STALE_QUOTE', 0):,}",
                "Total Zero Session Trades": f"{underlying_counts.get('NO_TRADES', 0):,}",
                "Total Prev Close Discrepancy Issues": f"{underlying_counts.get('PRICE_DISCREPANCY', 0):,}",
            })

    if df is None:
        st.info("Terminal initialized. Select **Refresh Quotes Now** in the control panel to execute market scan.")
        return

    # Data transformation for presentation
    show = df.copy()
    for col in ("quote_time", "last_trade_time"):
        show[col] = show[col].map(lambda t: t.strftime("%d-%b %H:%M:%S") if isinstance(t, datetime) else None)
    show["history_last_date"] = show["history_last_date"].astype(str).replace({"None": None})
    show["market_cap_as_of"] = show["market_cap_as_of"].astype(str).replace({"None": None})
    show["volume_multiple"] = show["volume_multiple"].map(lambda v: None if v is None or pd.isna(v) else round(v, 2))
    if "time_adjusted_volume_multiplier" in show.columns:
        show["time_adjusted_volume_multiplier"] = show["time_adjusted_volume_multiplier"].map(
            lambda v: None if v is None or pd.isna(v) else round(v, 2)
        )
    for col in ("ltp", "prev_close", "pct_change", "market_cap_cr", "sma50", "sma50_distance_pct"):
        if col in show.columns:
            show[col] = show[col].map(fmt_num)

    table_cols = {
        "symbol": st.column_config.TextColumn("Symbol", width="small"),
        "company_name": st.column_config.TextColumn("Company", width="medium"),
        "ltp": st.column_config.NumberColumn("LTP", format="₹%.2f"),
        "pct_change": st.column_config.NumberColumn("Change %", format="%+.2f%%"),
        "prev_close": st.column_config.NumberColumn("Prev Close", format="₹%.2f"),
        "volume_multiple": st.column_config.NumberColumn("Weekly Vol Mult (C1)", format="%.2f×", help="Primary C1: Today Vol / Prev Completed Week Vol"),
        "time_adjusted_volume_multiplier": st.column_config.NumberColumn("Intraday Pace Mult", format="%.2f×", help="Time-adjusted volume multiple prorated by session progress"),
        "market_cap_cr": st.column_config.NumberColumn("Market Cap", format="₹%,.0f Cr"),
        "sma50": st.column_config.NumberColumn("50-SMA", format="₹%.2f"),
        "sma50_distance_pct": st.column_config.NumberColumn("SMA Dist %", format="%+.2f%%"),
        "today_volume": st.column_config.NumberColumn("Today Vol", format="%,d"),
        "prev_week_volume": st.column_config.NumberColumn("Prev Wk Vol", format="%,d"),
        "candle_count": st.column_config.NumberColumn("Candles", format="%d"),
        "market_cap_method": st.column_config.TextColumn("Mcap Type"),
        "market_cap_source": st.column_config.TextColumn("Citation"),
        "quote_time": st.column_config.TextColumn("Sweep Time"),
        "last_trade_time": st.column_config.TextColumn("Last Traded"),
        "history_last_date": st.column_config.TextColumn("Candles Through"),
        "history_status": st.column_config.TextColumn("Candles"),
        "market_cap_status": st.column_config.TextColumn("Mcap Data"),
        "market_cap_as_of": st.column_config.TextColumn("As-Of"),
        "prev_week_range": st.column_config.TextColumn("Prev Week Range"),
        "c1_volume": st.column_config.TextColumn("C1: Vol"),
        "c2_mcap": st.column_config.TextColumn("C2: Mcap"),
        "c3_prev_close": st.column_config.TextColumn("C3: >Prev"),
        "c4_sma": st.column_config.TextColumn("C4: >SMA"),
        "status": st.column_config.TextColumn("Status"),
        "primary_exception": st.column_config.TextColumn("Primary Category"),
        "underlying_issues_str": st.column_config.TextColumn("All Underlying Issues", width="medium"),
        "data_issues": st.column_config.TextColumn("Exception Details", width="large"),
    }

    # Institutional Tabs
    tab_match, tab_issue, tab_all, tab_inspect = st.tabs([
        f"Matches ({n_match})",
        f"Data Exceptions ({n_issue})",
        f"Screened Universe ({len(show):,})",
        "Stock Inspector",
    ])

    with tab_match:
        if is_stale:
            st.error("Market quotes are stale. Match display suspended pending synchronization.")
        else:
            match_cols = [
                "symbol", "company_name", "ltp", "pct_change", "prev_close",
                "volume_multiple", "time_adjusted_volume_multiplier", "market_cap_cr", "sma50",
                "today_volume", "prev_week_volume", "market_cap_method", "quote_time",
            ]
            valid_match_cols = [c for c in match_cols if c in show.columns]
            matches = show[show["status"] == MATCH]

            if matches.empty:
                st.info(
                    "Zero securities currently meet all four strict criteria:\n"
                    "1. Today's cumulative volume > multiplier × previous Mon–Fri week's total volume\n"
                    "2. Capitalisation > specified threshold (AMFI 6M Avg or Live)\n"
                    "3. Last traded price > previous session close\n"
                    "4. Last traded price > 50-session completed daily SMA"
                )
            else:
                m_ctrl1, m_ctrl2 = st.columns([3, 2])
                with m_ctrl1:
                    m_search = st.text_input("Filter Candidates", placeholder="Search symbol or company...", key="m_search_box")
                with m_ctrl2:
                    m_sort = st.selectbox(
                        "Sort By",
                        [
                            "Weekly Volume Multiple (Desc)",
                            "Intraday Pace Multiple (Desc)",
                            "Price Change % (Desc)",
                            "Market Cap (Desc)",
                            "Today's Volume (Desc)",
                        ],
                        key="m_sort_box",
                    )

                display_matches = matches.copy()
                if m_search:
                    mq = m_search.strip().upper()
                    display_matches = display_matches[
                        display_matches["symbol"].str.contains(mq, na=False) |
                        display_matches["company_name"].astype(str).str.upper().contains(mq, na=False)
                    ]

                if m_sort == "Weekly Volume Multiple (Desc)":
                    display_matches = display_matches.sort_values("volume_multiple", ascending=False)
                elif m_sort == "Intraday Pace Multiple (Desc)":
                    display_matches = display_matches.sort_values("time_adjusted_volume_multiplier", ascending=False)
                elif m_sort == "Price Change % (Desc)":
                    display_matches = display_matches.sort_values("pct_change", ascending=False)
                elif m_sort == "Market Cap (Desc)":
                    display_matches = display_matches.sort_values("market_cap_cr", ascending=False)
                elif m_sort == "Today's Volume (Desc)":
                    display_matches = display_matches.sort_values("today_volume", ascending=False)

                st.dataframe(display_matches[valid_match_cols], column_config=table_cols, hide_index=True, use_container_width=True)
                st.download_button(
                    "Export Matches Audit Trail (CSV)",
                    matches.to_csv(index=False).encode("utf-8"),
                    file_name=f"nse_matches_{meta.get('session', curr_info.session)}_{last:%H%M%S}.csv",
                    mime="text/csv",
                )
                st.caption(
                    "Primary criteria: C1 evaluates Today Cumulative Vol > multiplier × Previous Completed Week. "
                    "Intraday Pace indicates time-adjusted volume velocity prorated for session elapsed time."
                )

    with tab_issue:
        st.caption("Securities where screening was incomplete due to missing history, missing market-cap reference, stale quotes, or zero session trades.")
        EXC_LABELS = {
            "MISSING_MARKET_CAP": "Missing Market Cap (Unmapped AMFI)",
            "INCOMPLETE_HISTORY": "Incomplete History (< 50 daily bars)",
            "STALE_QUOTE": "Stale / Unavailable Quote",
            "NO_TRADES": "No Trades This Session",
            "PRICE_DISCREPANCY": "Prev Close Discrepancy",
            "OTHER_DATA_ISSUE": "Other Reference Issue",
        }

        issues = show[show["status"] == DATA_ISSUE].copy()
        if not issues.empty:
            issues["category"] = issues["primary_exception"].map(
                lambda x: EXC_LABELS.get(x, "Other Reference Issue")
            )
            cat_counts = issues["category"].value_counts().to_dict()

            c_pills = " &nbsp;|&nbsp; ".join(f"<strong>{k}:</strong> {v:,}" for k, v in cat_counts.items())
            st.markdown(f"<div style='font-size:0.83rem; color:#94a3b8; margin-bottom:8px;'>{c_pills}</div>", unsafe_allow_html=True)

            i_col1, i_col2 = st.columns([2, 3])
            with i_col1:
                cat_filter = st.selectbox("Filter Category", ["All Categories"] + sorted(cat_counts.keys()))
            with i_col2:
                issue_search = st.text_input("Search Exceptions", placeholder="Search symbol, company, or error...")

            filtered_issues = issues
            if cat_filter != "All Categories":
                filtered_issues = filtered_issues[filtered_issues["category"] == cat_filter]
            if issue_search:
                iq = issue_search.strip().upper()
                filtered_issues = filtered_issues[
                    filtered_issues["symbol"].str.contains(iq, na=False) |
                    filtered_issues["company_name"].astype(str).str.upper().contains(iq, na=False) |
                    filtered_issues["data_issues"].astype(str).str.upper().contains(iq, na=False) |
                    filtered_issues["underlying_issues_str"].astype(str).str.upper().contains(iq, na=False)
                ]

            issue_cols = [
                "symbol", "company_name", "category", "underlying_issues_str", "data_issues", "ltp", "pct_change", "prev_close",
                "volume_multiple", "market_cap_cr", "sma50", "c1_volume", "c2_mcap",
                "c3_prev_close", "c4_sma", "market_cap_status", "history_status",
            ]
            valid_issue_cols = [c for c in issue_cols if c in filtered_issues.columns]
            st.dataframe(filtered_issues[valid_issue_cols], column_config=table_cols, hide_index=True, use_container_width=True)
            st.download_button(
                "Export Exceptions (CSV)",
                issues.to_csv(index=False).encode("utf-8"),
                file_name="nse_data_issues.csv",
                mime="text/csv",
                disabled=issues.empty,
            )
        else:
            st.success("Zero data exceptions! All screened instruments have complete historical candles and market-cap records.")

    with tab_all:
        f_col1, f_col2 = st.columns([2, 5])
        with f_col1:
            status_filter = st.multiselect("Filter by Status", [MATCH, FILTER_FAIL, DATA_ISSUE], default=[MATCH, FILTER_FAIL, DATA_ISSUE])
        with f_col2:
            search_query = st.text_input("Filter Ticker / Company", placeholder="Filter by symbol e.g. INFY, RELIANCE, TCS...")

        all_cols = [
            "symbol", "company_name", "status", "primary_exception", "underlying_issues_str", "ltp", "pct_change", "prev_close",
            "volume_multiple", "time_adjusted_volume_multiplier", "market_cap_cr", "sma50", "today_volume", "prev_week_volume",
            "c1_volume", "c2_mcap", "c3_prev_close", "c4_sma", "data_issues",
        ]
        valid_all_cols = [c for c in all_cols if c in show.columns]
        view = show[show["status"].isin(status_filter)]
        if search_query:
            q = search_query.strip().upper()
            view = view[view["symbol"].str.contains(q, na=False) | view["company_name"].astype(str).str.upper().contains(q, na=False)]

        st.dataframe(view[valid_all_cols], column_config=table_cols, hide_index=True, use_container_width=True)
        st.download_button(
            "Export Full Universe (CSV)",
            view.to_csv(index=False).encode("utf-8"),
            file_name="nse_universe_screened.csv",
            mime="text/csv",
            disabled=view.empty,
        )

    with tab_inspect:
        st.markdown("##### Institutional Security Technical Audit & Candlestick Inspector")
        # Default to first match if matches exist, else first symbol
        match_syms = show[show["status"] == MATCH]["symbol"].tolist()
        inspect_options = match_syms + [s for s in show["symbol"].tolist() if s not in match_syms]

        selected_symbol = st.selectbox(
            "Select Security for Deep-Dive Audit",
            options=inspect_options,
            index=0 if inspect_options else None,
            help="Displays complete source data, audit trail, time-adjusted volume calculation, and historical chart",
        )
        if selected_symbol:
            row = show[show["symbol"] == selected_symbol].iloc[0]

            # Security Header Banner
            status_tag_color = "#10b981" if row["status"] == MATCH else ("#f43f5e" if row["status"] in (FILTER_FAIL, FAIL) else "#f59e0b")
            st.markdown(
                f"""
                <div style="background:#0f1523; border:1px solid #1b2333; border-radius:6px; padding:12px 16px; margin-bottom:12px; display:flex; justify-content:space-between; align-items:center;">
                  <div>
                    <span style="font-size:1.25rem; font-weight:700; color:#ffffff; font-family:'JetBrains Mono',monospace;">{row['symbol']}</span>
                    <span style="font-size:0.9rem; color:#94a3b8; margin-left:12px;">{row['company_name']}</span>
                    <span style="font-size:0.75rem; color:#38bdf8; background:rgba(56,189,248,0.1); border:1px solid rgba(56,189,248,0.3); padding:2px 6px; border-radius:4px; margin-left:8px;">NSE EQ #{row['instrument_token']}</span>
                  </div>
                  <div>
                    <span style="font-size:0.8rem; font-weight:700; color:{status_tag_color}; border:1px solid {status_tag_color}; padding:4px 10px; border-radius:4px; text-transform:uppercase;">{row['status']}</span>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # Key Statistics Grid
            kpi_c1, kpi_c2, kpi_c3, kpi_c4 = st.columns(4)
            with kpi_c1:
                ltp_val = f"₹{row['ltp']:,.2f}" if row['ltp'] else "—"
                pct_val = f"{row['pct_change']:+.2f}%" if row['pct_change'] else "—"
                st.metric("Last Traded Price", ltp_val, delta=pct_val)
            with kpi_c2:
                vol_m = f"{row['volume_multiple']}×" if row['volume_multiple'] else "—"
                st.metric("Weekly Volume Multiple (C1)", vol_m, help="Today Cumulative Vol / Prev Completed Week Vol")
            with kpi_c3:
                time_val = row.get("time_adjusted_volume_multiplier")
                time_m = f"{time_val}×" if (time_val is not None and not (isinstance(time_val, float) and math.isnan(time_val))) else "N/A"
                st.metric(
                    "Time-Adjusted Multiple",
                    time_m,
                    help="Requires comparable historical intraday candles. Returns N/A when only daily candles exist.",
                )
            with kpi_c4:
                mcap_val = f"₹{row['market_cap_cr']:,.0f} Cr" if row['market_cap_cr'] else "—"
                st.metric("Market Capitalisation", mcap_val, help=f"Method: {row['market_cap_method']}, Source: {row['market_cap_source']}")

            # Detailed 4 Conditions Evaluation
            st.markdown("###### Quantitative Screening Criteria Verification")
            c1_col, c2_col, c3_col, c4_col = st.columns(4)

            with c1_col:
                st.markdown(f"**C1: Volume Expansion — `{row['c1_volume']}`**")
                time_val = row.get("time_adjusted_volume_multiplier")
                st.write({
                    "Today Traded Volume": f"{row['today_volume']:,}" if row['today_volume'] else "—",
                    "Previous Week Total": f"{row['prev_week_volume']:,}" if row['prev_week_volume'] else "—",
                    "Weekly Multiple (C1)": f"{row['volume_multiple']}×" if row['volume_multiple'] else "—",
                    "Required Threshold": f"> {multiplier:.1f}×",
                    "Time-Adjusted Metric": f"{time_val}×" if (time_val is not None and not (isinstance(time_val, float) and math.isnan(time_val))) else "N/A",
                    "Time-Adjusted Formula": row.get("time_adjusted_formula", "N/A (Intraday history unavailable)"),
                    "Linear Pace (Heuristic)": f"{row.get('linear_session_pace')}×" if row.get("linear_session_pace") is not None else "N/A",
                })
            with c2_col:
                st.markdown(f"**C2: Market Cap — `{row['c2_mcap']}`**")
                st.write({
                    "Capitalisation": f"₹{row['market_cap_cr']:,.0f} Cr" if row['market_cap_cr'] else "—",
                    "Required Threshold": f"> ₹{min_mcap:,.0f} Cr",
                    "Valuation Basis": "Reported 6M Avg (AMFI)" if row["market_cap_method"] == "reported" else "Live (Shares × LTP)",
                    "As-Of Date": row["market_cap_as_of"],
                    "Citation": row["market_cap_source"],
                })
            with c3_col:
                st.markdown(f"**C3: Price vs Prev Close — `{row['c3_prev_close']}`**")
                st.write({
                    "LTP": f"₹{row['ltp']:,.2f}" if row['ltp'] else "—",
                    "Previous Close": f"₹{row['prev_close']:,.2f}" if row['prev_close'] else "—",
                    "Net Price Delta": f"{row['pct_change']:+.2f}%" if row['pct_change'] else "—",
                    "Target Rule": "LTP > Prev Close",
                    "Discrepancy Sanity Check": f"Max {settings.prev_close_tolerance_pct}% gap vs candle cache (does not detect all corporate actions)",
                })
            with c4_col:
                st.markdown(f"**C4: Trend vs 50-SMA — `{row['c4_sma']}`**")
                st.write({
                    "LTP": f"₹{row['ltp']:,.2f}" if row['ltp'] else "—",
                    "50-Session Daily SMA": f"₹{row['sma50']:,.2f}" if row['sma50'] else "—",
                    "Distance from SMA": f"{row.get('sma50_distance_pct'):+.2f}%" if row.get('sma50_distance_pct') else "—",
                    "Candles Synced Through": row["history_last_date"],
                    "Target Rule": "LTP > SMA 50",
                })

            if row.get("primary_exception") or row.get("data_issues"):
                st.warning(
                    f"**Reported Data Issues:** Primary Category: `{row.get('primary_exception')}` | "
                    f"Underlying Issues: `{row.get('underlying_issues_str', 'NONE')}`\n\n"
                    f"**Diagnostic Details:** {row.get('data_issues', 'None')}"
                )

            # Single Candidate Audit Export
            c_aud_col1, c_aud_col2 = st.columns([3, 1])
            with c_aud_col1:
                st.caption(
                    f"Quote Freshness: {row.get('quote_time', 'N/A')} "
                    f"({'LIVE FEED' if live_now else 'EOD SNAPSHOT'}) | "
                    f"Historical Daily Candles Synced: {row.get('candle_count', 0)} sessions"
                )
            with c_aud_col2:
                audit_df = pd.DataFrame([row.to_dict()])
                st.download_button(
                    f"Export Audit ({row['symbol']})",
                    audit_df.to_csv(index=False).encode("utf-8"),
                    file_name=f"audit_{row['symbol']}_{curr_info.session}_{now_ist():%H%M%S}.csv",
                    mime="text/csv",
                    use_container_width=True,
                )

            # Historical Daily Candlestick & Volume Chart with verified SMA50
            tok = int(row["instrument_token"])
            cand_df = histories.get(tok)
            if cand_df is not None and not cand_df.empty:
                st.markdown("###### Daily Price & Volume Trend (with 50-SMA)")
                c_plot = prepare_chart_data(cand_df, sma_window=50, display_tail=90, session=curr_info.session)

                price_chart = alt.Chart(c_plot).mark_line(color="#38bdf8", strokeWidth=2).encode(
                    x=alt.X("date:T", title=None, axis=alt.Axis(format="%d %b")),
                    y=alt.Y("close:Q", title="Close Price (₹)", scale=alt.Scale(zero=False)),
                    tooltip=[alt.Tooltip("date:T", format="%Y-%m-%d"), "close:Q", "open:Q", "high:Q", "low:Q"],
                )
                sma_chart = alt.Chart(c_plot.dropna(subset=["sma50"])).mark_line(color="#f59e0b", strokeWidth=1.5, strokeDash=[4, 4]).encode(
                    x="date:T",
                    y="sma50:Q",
                    tooltip=["sma50:Q"],
                )
                vol_chart = alt.Chart(c_plot).mark_bar(color="#10b981", opacity=0.6).encode(
                    x=alt.X("date:T", title="Date", axis=alt.Axis(format="%d %b")),
                    y=alt.Y("volume:Q", title="Volume"),
                    tooltip=[alt.Tooltip("date:T", format="%Y-%m-%d"), "volume:Q"],
                ).properties(height=100)

                combined_chart = alt.vconcat(
                    (price_chart + sma_chart).properties(height=260),
                    vol_chart
                ).configure_view(strokeWidth=0).configure_axis(
                    labelColor="#94a3b8", titleColor="#94a3b8", gridColor="#1b2333"
                )
                st.altair_chart(combined_chart, use_container_width=True)
                st.caption("Blue line: Daily Close | Amber dashed line: 50-Session SMA | Green bars: Daily Volume")

            # Research Disclaimer Box
            st.markdown(
                """
                <div style="background:rgba(245,158,11,0.05); border:1px solid rgba(245,158,11,0.2); border-radius:6px; padding:10px 14px; margin-top:16px;">
                  <span style="font-size:0.75rem; color:#f59e0b;">
                    <strong>Research Disclaimer:</strong> This intraday screener is a quantitative data-filtering tool and does not provide investment advice or order execution. All statistics are calculated from Zerodha Kite Connect market data feeds. Independent risk evaluation is mandatory before taking trading decisions.
                  </span>
                </div>
                """,
                unsafe_allow_html=True,
            )


min_scan_seconds = math.ceil(len(universe) / QUOTE_BATCH_SIZE) * 1.2
run_every = max(int(interval), int(min_scan_seconds) + 5) if (auto and info.market_open) else None


@st.fragment(run_every=run_every)
def live_panel() -> None:
    curr_now = now_ist()
    curr_info = session_info(settings, curr_now)
    first = ss.results is None
    last_run = ss.last_success or ss.last_attempt
    time_since = (curr_now - last_run).total_seconds() if last_run else None
    due = (
        run_every is not None
        and time_since is not None
        and time_since >= 0.8 * run_every
    )
    recently_failed = bool(ss.last_error) and time_since is not None and time_since < 15
    if (manual or (first and not recently_failed) or due) and not recently_failed:
        do_scan()
    render(curr_info)


live_panel()
