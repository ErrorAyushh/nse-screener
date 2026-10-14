# Validation checklist

Work through this on a normal trading day (not the first session after a holiday) with real credentials, before trusting the dashboard. Pick 3–5 symbols: at least one **MATCH**, one **FAIL**, one **DATA_ISSUE**, and one stock that had a split/bonus or holiday in the last two weeks if possible.

Use the **All screened** tab and its CSV, and compare against Kite web/app charts or a Python shell:

```powershell
python
>>> from config import load_settings; from kite_data import make_client
>>> kite = make_client(load_settings())
>>> q = kite.quote(["NSE:RELIANCE"])["NSE:RELIANCE"]
>>> q["last_price"], q["ohlc"]["close"], q["volume"], q["timestamp"], q["last_trade_time"]
```

## Setup and connection
- [ ] `python -m pytest -q` → all tests pass.
- [ ] `python auth_login.py` saves a token; it is **not** printed anywhere; `.env` is not tracked by git (`git status`).
- [ ] Dashboard shows your Kite user id under *Data freshness details* and never shows the token.
- [ ] Next day before logging in: dashboard reports the token as expired and shows login steps.

## Universe
- [ ] *Eligible NSE equities* is in the expected range (low-to-mid thousands) and the universe-filter stats look sensible.
- [ ] No index (e.g. `NIFTY 50`), futures/options, or `-BE`/`-SM`/`-GB` symbols appear (unless you allowed those series).
- [ ] A well-known small company not in Nifty 500 appears in *All screened*.

## Condition 1 – volume
- [ ] *Today vol* equals `q["volume"]` (within one refresh) and matches the day's volume on Kite.
- [ ] *Prev week* shows last Monday–Friday (not a rolling window). On Kite's daily chart, add up the volume bars for exactly those days — equals *Prev-week vol*.
- [ ] Run once in a week following a holiday: the holiday is absent and the session count is one fewer.
- [ ] *Vol ×* = Today vol ÷ Prev-week vol (stocks with zero previous-week volume cannot demonstrate volume expansion and are reported under *Data issues*).

## Condition 2 – market cap
- [ ] For each sample stock, *Mcap ₹ cr* equals the value in `data/market_cap.csv` (or shares × LTP ÷ 1e7) and agrees roughly with a public source.
- [ ] Remove one symbol from the CSV → it moves to *Data issues* with `market cap MISSING`.
- [ ] Set its `as_of_date` older than the max age → `STALE`.

## Condition 3 – previous close
- [ ] *Prev close* equals the previous session's official close on Kite (daily chart close / quote `ohlc.close`).
- [ ] *% chg* agrees with Kite's day change.
- [ ] Check once shortly after 09:15 and once before 09:15: before the open the screen is labelled as the previous session's snapshot.
- [ ] *Previous-Close Discrepancy Cross-Check:* Verify that when quote `ohlc.close` diverges from the cached historical close by >1%, the stock is routed to `DATA_ISSUE`. *Limitations:* Note that this cross-check compares only the single previous session close against local cache; it does not detect adjustments <= 1%, does not detect adjustments when both feeds were already updated, and can be triggered by non-corporate events (e.g. post-market exchange closing price adjustments). It is a defensive sanity check, not a comprehensive corporate action detector.

## Condition 4 – SMA 50
- [ ] On Kite's daily chart add a 50-period SMA. **Yesterday's** SMA value (last completed candle) equals *SMA 50* (± rounding). Today's live SMA on the chart will differ because it includes today's candle — that difference is expected.
- [ ] Independent check:
  ```python
  >>> from datetime import date, timedelta
  >>> c = kite.historical_data(TOKEN, date.today()-timedelta(days=120), date.today()-timedelta(days=1), "day")
  >>> sum(x["close"] for x in c[-50:]) / 50
  ```

## Freshness and failure handling
- [ ] *History through* shows the previous trading day for every OK stock after `python refresh_history.py`.
- [ ] Skip the history refresh one morning → banner warns, affected stocks are data issues, none appear as matches.
- [ ] Disconnect the network during market hours → error banner; after 2× interval the status shows **STALE** and the Matches table is hidden.
- [ ] Quote timestamps in the table are within a minute or two of the refresh time during market hours.
- [ ] A stock with a corporate action in the last fortnight: history was re-downloaded (refresh report shows *re-downloaded after adjustment*) and SMA matches Kite's adjusted chart.

## Matches
- [ ] For every row in **Matches**, all four inequalities hold when re-computed by hand from the numbers shown.
- [ ] Sorting is by *Vol ×* descending; the CSV download has the same rows.
- [ ] Raise the volume multiplier or market-cap threshold, refresh → the match list shrinks accordingly.
