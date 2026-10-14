# NSE Intraday Screening Dashboard

A local Streamlit dashboard that scans NSE cash-equity stocks with Zerodha Kite Connect market data and lists those passing **all four** conditions:

| # | Condition | Rule (all strict `>`) |
|---|-----------|------------------------|
| C1 | Volume expansion | today's cumulative volume `>` 4 × total volume of the previous completed Mon–Fri trading week |
| C2 | Market cap | market capitalisation `>` ₹1,000 crore |
| C3 | Price vs previous close | latest traded price `>` previous session's close |
| C4 | Price vs 50-day SMA | latest traded price `>` SMA of the last 50 *completed* daily closes |

> **Scope.** Market-data screening and analytics only. It never places, modifies or cancels orders, never manages portfolios and makes no buy/sell recommendations. No order-related API is called anywhere in the code.

---

## Contents

1. [How it works](#how-it-works)
2. [Windows PowerShell setup](#windows-powershell-setup)
3. [Kite Connect credentials and daily login](#kite-connect-credentials-and-daily-login)
4. [Market-cap data](#market-cap-data)
5. [Running](#running)
6. [Tests](#tests)
7. [API costs, permissions, limits and refresh behaviour](#api-costs-permissions-limits-and-refresh-behaviour)
8. [Data-correctness rules](#data-correctness-rules)
9. [Troubleshooting](#troubleshooting)
10. [Known limitations and what still needs real-world verification](#known-limitations-and-what-still-needs-real-world-verification)

Validation checklist against Kite data: see **[VALIDATION.md](VALIDATION.md)**.

---

## How it works

```
app.py               Streamlit UI (presentation only)
pipeline.py          Glue: session info, universe, history refresh, scan
kite_data.py         All Kite API access: auth, rate limits, retries, instruments, quotes, history cache
screener.py          The four conditions (pure functions)          ─┐
indicators.py        SMA 50 and previous-week volume (pure)         ├─ tested offline
calendar_utils.py    Session date, holidays, trading weeks (pure)   │
market_cap.py        Market-cap CSV loading/validation (pure)      ─┘
config.py            Settings from .env
auth_login.py        Daily login: request_token → access_token (written to .env, never printed)
refresh_history.py   CLI: build/update the daily-candle cache
import_market_cap.py CLI: convert your market-cap/share-count file to data/market_cap.csv
data/nse_holidays.csv     NSE equity trading holidays (2026 included; add future years)
data/market_cap.template.csv  Schema for data/market_cap.csv
tests/               63 unit tests, no Zerodha account needed
```

Per refresh:

1. **Universe** – the NSE instrument dump (`kite.instruments("NSE")`) is downloaded once per day and cached to `data/cache/`. Kept: `exchange=NSE`, `segment=NSE`, `instrument_type=EQ`, symbols without a series suffix (EQ series). Indices, F&O, currency, `-BE`/`-SM`/`-GB`/… series and duplicate symbols are dropped (add series via `ALLOWED_SERIES_SUFFIXES`).
2. **History** – daily candles are cached per instrument on disk (`data/cache/history/`) and refreshed **once a day** through the last *completed* session only. Today's candle is never requested.
3. **Quotes** – `kite.quote()` in batches of 500 at under 1 request/second (~5 calls for ~2,000 stocks) gives last price, cumulative day volume, previous close (`ohlc.close`), and timestamps.
4. **Screen** – each stock gets PASS / FAIL / NO_DATA per condition and an overall **MATCH / FAIL / DATA_ISSUE**.

**Why quote polling instead of WebSocket?** Streamlit re-runs scripts and does not suit a long-lived socket thread, and the screen only needs a snapshot every ~minute. Polling the full universe costs about five quote calls per refresh, comfortably inside Kite's limits, and is far simpler to make reliable (no reconnection state, no missed-tick bookkeeping). WebSocket `full` mode would be the upgrade path for sub-10-second updates (3,000 instruments per connection, 3 connections per key).

---

## Windows PowerShell setup

Requires **Python 3.11 or newer** ([python.org](https://www.python.org/downloads/windows/), tick "Add python.exe to PATH").

```powershell
# 1. Go to the project folder (where you extracted the zip)
cd C:\path\to\nse-screener

# 2. Create and activate a virtual environment
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
# If activation is blocked:  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   (then re-run the line above)

# 3. Install dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt

# 4. Create your .env from the template and edit it
Copy-Item .env.example .env
notepad .env          # fill KITE_API_KEY and KITE_API_SECRET, save

# 5. Run the tests (no Zerodha account needed)
python -m pytest -q

# 6. Daily login (writes the access token into .env)
python auth_login.py

# 7. Build/update market-cap data (automated download and mapping)
python refresh_market_cap.py
# (Or manually import a custom file via: python import_market_cap.py .\file.xlsx ...)

# 8. Build the history cache (first run ~12–15 minutes; later runs are quick)
python refresh_history.py

# 9. Start the dashboard (opens http://localhost:8501)
streamlit run app.py
```

Every following trading day: activate the venv, `python auth_login.py`, `python refresh_history.py`, `streamlit run app.py`.

---

## Kite Connect credentials and daily login

1. Sign in at **https://developers.kite.trade** with your Zerodha account and create an app on the **Connect** plan (market data requires it; see costs below).
2. Set the app's **Redirect URL**. For local use `http://127.0.0.1:5000/` works — nothing needs to be running there; you only copy the address from the browser bar.
3. Put the app's **API key** and **API secret** into `.env`.
4. Each day run `python auth_login.py`:
   - it opens `https://kite.zerodha.com/connect/login?v=3&api_key=…`;
   - you log in on Zerodha's own page (password + 2FA — the script never sees them);
   - Zerodha redirects to your Redirect URL with `?request_token=…&status=success`; the page may show "can't connect", which is fine;
   - copy the whole address and paste it into the script;
   - the official client exchanges it (checksum = SHA-256 of `api_key + request_token + api_secret`) for an **access token**, which is written to `.env` with a timestamp. It is never printed or shown in the dashboard.
5. In a running dashboard press **Reconnect (reload .env after login)**.

**Expiry.** Access tokens expire at **06:00 IST the next day** (regulatory) and also on a master logout from Kite web. The dashboard checks the saved login time and also handles `TokenException` from any call: it stops, shows the login instructions, and never retries with a dead token. The `request_token` is single-use and valid only for a few minutes, so paste it promptly.

**Security.** `.env` is git-ignored. Logs and error messages never include the API secret or access token; the app keys its client cache on a SHA-256 fingerprint, not the token.

---

## Market-cap data

Kite Connect does not provide market capitalisation, so it is read from **`data/market_cap.csv`**, which you build from a source you are entitled to use. **No value is ever estimated, defaulted or set to zero.**

### File format

| column | meaning |
|---|---|
| `symbol` | NSE trading symbol exactly as in Kite (e.g. `RELIANCE`, `M&M`, `BAJAJ-AUTO`) |
| `company_name` | optional |
| `market_cap_cr` | market cap in **₹ crore** — *or* |
| `shares_outstanding` | total issued equity shares (then market cap = shares × live LTP ÷ 10⁷) |
| `as_of_date` | `YYYY-MM-DD` the figure refers to |
| `source` | where it came from |

Each row must have exactly one of `market_cap_cr` / `shares_outstanding`. Lines starting with `#` are comments.

### Status rules (only `OK` can pass Condition 2)

| status | when |
|---|---|
| `OK` | valid row, not older than `MARKET_CAP_MAX_AGE_DAYS` (default 190) |
| `MISSING` | symbol not in the file |
| `STALE` | `as_of_date` older than the max age |
| `DUPLICATE` | symbol appears more than once (ambiguous) |
| `INVALID` | non-numeric, non-positive, both/neither value columns, or bad date |
| `NO_PRICE` | shares-based row but no valid live price |

Non-OK makes C2 `NO_DATA` and the stock a **DATA_ISSUE**, never a match.

### Automated refresh script

Run `python refresh_market_cap.py` to download and build `data/market_cap.csv` automatically:
```powershell
# Automatically downloads the latest official AMFI report and NSE EQUITY_L master
python refresh_market_cap.py

# Optional flags:
#   --force-download     re-download source files from AMFI and NSE
#   --input <path>       use a locally downloaded AMFI spreadsheet
#   --as-of <YYYY-MM-DD> customize as-of date (defaults to period end date)
#   --source <text>      customize source attribution
```
The script performs automated validation:
* Maps explicit NSE symbols from AMFI and cross-references ISINs against NSE's master directory (`EQUITY_L.csv`) to resolve dual-listed stocks (recovering over 200+ tickers that lacked an explicit NSE symbol in AMFI).
* Resolves duplicate records based on current active ISIN.
* Validates positive numeric figures and writes compliant `data/market_cap.csv`.
* Prints a coverage breakdown against the active NSE equity universe.

### Manual import (alternative)

If you have a custom spreadsheet, use `import_market_cap.py`:
1. **AMFI half-yearly "Average Market Capitalisation of listed companies"** — free, public, published every January and July at [amfiindia.com](https://www.amfiindia.com) (Research & Information → other data), covers every listed company with the NSE symbol and average market cap in **₹ crore**. Caveat: it is a **six-month average**, not today's value, so stocks near ₹1,000 crore can be misclassified. That is why the default max age is 190 days.
   ```powershell
   python import_market_cap.py .\amfi.xlsx --list-columns
   python import_market_cap.py .\amfi.xlsx --symbol-col "NSE Symbol" `
       --mcap-col "Average of All Exchanges (Rs. Cr.)" --unit crore `
       --as-of 2026-06-30 --source "AMFI avg mcap Jan-Jun 2026"
   ```
2. **Shares outstanding × live price (most accurate).** Issued share capital comes from quarterly shareholding-pattern filings on NSE/BSE or company disclosures. Put share counts in `shares_outstanding` and market cap tracks the live price:
   ```powershell
   python import_market_cap.py .\shares.csv --symbol-col Symbol --shares-col "Issued Shares" `
       --as-of 2026-09-30 --source "Shareholding pattern Sep-2026"
   ```
   Share counts change with buybacks, splits, bonuses and new issues — refresh quarterly and after corporate actions.
3. **A licensed data vendor** export, imported the same way.

### Symbol mapping

Rows match Kite's `tradingsymbol` case-insensitively. Symbols that changed after a rename, or AMFI rows for companies listed only on BSE, will show as `MISSING` — check the **Data issues** tab and fix the CSV.

---

## Running

```powershell
streamlit run app.py
```

**Sidebar controls**

- *Minimum market cap* (default ₹1,000 cr) and *volume multiplier* (default 4) — both strict `>`. Changing them takes effect on the next refresh.
- *Auto-refresh* with *refresh interval* (minimum 15 s; also never shorter than the time a full quote sweep needs). Auto-refresh only runs during market hours (09:15–15:30 IST on trading days).
- *Refresh quotes now* — manual scan at any time.
- *Refresh daily history cache* (optionally *force full re-download*). Blocks the page while running; for the first build prefer `python refresh_history.py` in a separate window.
- *Reload instrument list*, *Reconnect*.

**Tabs**

- **Matches** — stocks passing all four conditions, sorted by volume multiple (descending), with CSV download.
- **Data issues** — no condition failed but some data was missing/stale/inconsistent, with the reason. Not matches.
- **All screened** — every stock with per-condition PASS/FAIL/NO_DATA.

**Status indicator**

- *Live* — market open and last successful refresh is recent.
- *STALE* — market open but last success is older than 2× the interval (min 2 minutes). Matches are **hidden** until a refresh succeeds.
- *Market closed · snapshot of DATE* — outside market hours, results are the last session's snapshot and labelled as such.

---

## Tests

```powershell
python -m pytest -q
```

55 tests, all offline, covering: all-pass; each condition failing alone; volume exactly 4×; market cap exactly ₹1,000 cr (reported and share-based); price equal to previous close; price equal to SMA; missing/stale/duplicate market cap; missing and insufficient candles; a missing day inside the previous week; stale, wrong-session and missing quotes; no trades yet; previous-close mismatch; weekends; exchange holidays (e.g. 2 Oct); pre-open; Monday/Wednesday/Friday sessions; fully closed weeks; calendar week vs rolling 7 days; today's partial candle excluded from SMA, previous close and weekly volume; history download failures, stale cache, split re-adjustment; expired tokens (no retry), permission errors, network retries and give-up; token-expiry timing; universe filtering; request-token parsing.

---

## API costs, permissions, limits and refresh behaviour

Verified against Zerodha's documentation in October 2026 — re-check before relying on it:

| Item | Value |
|---|---|
| Plan needed | **Kite Connect "Connect"** — ₹500/month per app; includes WebSocket streaming and historical candles. The free "Personal" plan is listed for orders/portfolio APIs, not market data. |
| Account | An active Zerodha trading account; you log in with it daily. |
| `/quote` | up to **500** instruments per call, **1 request/second** |
| `/quote/ohlc`, `/quote/ltp` | up to 1,000 instruments per call (not used: they lack volume) |
| Historical candles | **3 requests/second**, one instrument per request |
| Other endpoints | 10 requests/second |
| WebSocket | 3,000 instruments/connection, 3 connections/key (not used) |
| Access token | expires 06:00 IST next day |

How the code respects them: separate limiters run at 85% of each documented rate; retryable errors (`NetworkException` incl. HTTP 429, `DataException`, `GeneralException`, connection/timeouts) back off exponentially (1, 2, 4, 8 s); `TokenException` stops immediately; `PermissionException` is reported as a subscription problem; `InputException` for one instrument is recorded and that instrument becomes a data issue.

**Refresh cost.** One full scan ≈ ⌈N/500⌉ quote calls (≈5 calls, ~6 s for ~2,000 stocks). The first history build is one call per instrument (~2,000 calls ≈ 12–15 min at <3/s); afterwards only instruments not yet current are fetched, with a 10-day overlap that detects split/bonus re-adjustment and re-downloads that instrument in full. By default history is only fetched for symbols present in your market-cap file (`HISTORY_ONLY_WITH_MARKET_CAP=true`), since others can never pass C2.

---

## Data-correctness rules

- **Session date.** On a trading day from 09:15 IST the session is *today*; before that, on weekends and holidays it is the previous trading day (from `data/nse_holidays.csv`).
- **Previous completed week.** The completed trading week before the week containing the session date — not a rolling 7 days. The days that count are those the exchange actually traded, derived from the cached candles (a day is a session if ≥50% of cached instruments have a candle). This includes special trading sessions (e.g. Diwali Muhurat trading or Saturday disaster-recovery trading sessions) recorded in the trading calendar. If that week was entirely closed, the previous week with sessions is used. If the stock lacks a candle (or volume) for **any** session in that week, C1 is `NO_DATA`. If total volume in that week was zero, C1 is `NO_DATA` ("previous week volume is zero") to prevent zero-volume division anomalies and false matches.
- **SMA 50.** Mean of the 50 most recent daily closes strictly before the session date. Fewer than 50 or non-positive closes → `NO_DATA`.
- **History freshness.** The stock's last completed candle must be the trading session immediately before the session date without unhandled fetch errors; otherwise C1 and C4 are `NO_DATA` ("refresh history cache").
- **Previous close (C3).** Kite quote `ohlc.close` (documented as the last trading day's close), cross-checked against the cached historical close of that session; a gap above `PREV_CLOSE_TOLERANCE_PCT` (1%) makes C3 `NO_DATA`. *Limitations:* This is a sanity cross-check between the quote feed and candle cache. It compares only the single previous session close, does not detect adjustments below the 1% tolerance (e.g. minor dividends), does not trigger if both feeds were already updated post-event, and may be triggered by non-corporate events like exchange post-market closing revisions. It does not claim to detect all corporate actions.
- **Market cap (C2).** Clearly differentiates between `shares_x_ltp` (live, calculated from issued shares × LTP) and `reported` (static from CSV, such as AMFI 6-month averages). Both the method and source are displayed in tables and CSV downloads. Strict boundary: ₹999.9 Cr fails, ₹1,000.0 Cr fails, and ₹1,000.1 Cr passes.
- **Quotes.** Missing quote, missing/invalid price, quote timestamp not on the session date, or (during market hours) older than `QUOTE_STALE_SECONDS` (300 s) → `NO_DATA`. If the stock has not traded yet this session, C3/C4 are `NO_DATA` and C1 is evaluated on its real (zero) volume.
- **Overall status.** Reconciles all eligible instruments into exactly one top-level status: `MATCH` (all four conditions pass), `FILTER_FAIL` (clean data, but at least one condition fails), or `DATA_ISSUE` (incomplete or inconsistent data prevents clean evaluation). Overlapping underlying data issues (e.g., missing market cap and missing history) are tracked independently without double-counting the top-level universe. Missing data is never zero and never a pass.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No Kite access token" / "token has expired" / `TokenException` | `python auth_login.py`, then **Reconnect** |
| "request_token expired or already used" | Log in again and paste within a minute or two |
| `PermissionException` / "subscription" | Your app is not on the Connect plan, or it lapsed |
| Everything is a data issue with "refresh history cache" | Run `python refresh_history.py` (needed every trading day) |
| "Market-cap file not found" | Create `data/market_cap.csv` (see above) |
| Many `MISSING` market caps | Symbol mismatch or ETFs/new listings not in your source; check Data issues tab |
| Quotes stale during market hours | Network/API trouble; check the error banner, press *Refresh quotes now* |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| Holiday not recognised | Add the date to `data/nse_holidays.csv` (needed for every new year) |

---

## Known limitations and what still needs real-world verification

This is a verified local engine with comprehensive unit tests, **not yet production-verified against live Kite API keys in market hours**. Before relying on it:

1. **Real credentials + live market test.** The Kite layer was tested against fakes that follow the documented response shapes; run it against your account during market hours and work through [VALIDATION.md](VALIDATION.md).
2. **Market-cap data.** You must supply `data/market_cap.csv`. No real figures are bundled. AMFI averages are approximations near the threshold (clearly labeled as `reported` in the UI).
3. **Connect subscription.** Quotes, history and streaming need the paid Connect plan; confirm in your developer console.
4. **Quote field behaviour around the open.** Kite documents `ohlc.close` as the previous trading day's close; the exact moment it rolls over in the morning was not verifiable offline. The 1% cross-check against history guards against using a rolled value.
5. **Historical adjustments.** Kite adjusts past candles for splits/bonuses; the cache re-downloads when it detects a change, but verify around corporate actions.
6. **Instrument universe.** Kite's dump has no "series" or ETF flag. ETFs/InvITs/REITs with `EQ` type stay in the universe but usually lack market-cap rows and show as data issues.
7. **Holidays.** `data/nse_holidays.csv` contains the 2026 NSE equity holidays and must be extended each year.
8. **History refresh in the UI blocks the page**; use the CLI for the initial build.
9. Kite's Python client is v5 (`kiteconnect>=5`); the v4 docs linked in the brief describe the same calls used here (`instruments`, `quote`, `historical_data`, `generate_session`, `profile`).
