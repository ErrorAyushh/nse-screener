"""Market-capitalisation reference data: loading, validation and freshness.

Kite Connect does not supply market capitalisation, so it comes from a local,
user-maintained CSV (``data/market_cap.csv``). See README "Market-cap data".

CSV schema (header row required; UTF-8):

    symbol            NSE trading symbol, e.g. RELIANCE (EQ series, no suffix)
    company_name      optional
    market_cap_cr     market cap in ₹ crore           } exactly one of these
    shares_outstanding total issued equity shares      } two per row
    as_of_date        YYYY-MM-DD the figure refers to
    source            free text, e.g. "AMFI H1-2026 avg mcap" or "NSE filing"

If ``shares_outstanding`` is given, market cap is computed live as
``shares_outstanding * latest_traded_price / 1e7`` (₹ crore). This is the most
accurate option. If ``market_cap_cr`` is given it is used as-is.

Rules:
* No value is ever invented or defaulted. A symbol absent from the CSV is MISSING.
* A record older than ``max_age_days`` is STALE.
* A symbol appearing more than once is DUPLICATE (ambiguous) and unusable.
* Non-numeric or non-positive values are INVALID.
Only status OK can satisfy Condition 2.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

CRORE = 1e7

OK = "OK"
MISSING = "MISSING"
STALE = "STALE"
INVALID = "INVALID"
DUPLICATE = "DUPLICATE"
NO_PRICE = "NO_PRICE"

REQUIRED_COLUMNS = {"symbol", "as_of_date", "source"}


@dataclass(frozen=True)
class MarketCapInfo:
    value_cr: float | None
    status: str
    as_of: date | None = None
    source: str = ""
    method: str = ""  # "reported" or "shares_x_ltp"

    @property
    def ok(self) -> bool:
        return self.status == OK and self.value_cr is not None


@dataclass
class MarketCapTable:
    records: pd.DataFrame  # indexed by symbol, validated rows only
    problems: dict[str, str]  # symbol -> INVALID/DUPLICATE
    path: Path | None = None
    load_error: str = ""

    @property
    def latest_as_of(self) -> date | None:
        if self.records.empty:
            return None
        return max(self.records["as_of_date"])

    @property
    def oldest_as_of(self) -> date | None:
        if self.records.empty:
            return None
        return min(self.records["as_of_date"])

    def lookup(self, symbol: str, ltp: float | None, today: date, max_age_days: int) -> MarketCapInfo:
        sym = symbol.strip().upper()
        if sym in self.problems:
            return MarketCapInfo(None, self.problems[sym])
        if sym not in self.records.index:
            return MarketCapInfo(None, MISSING)
        row = self.records.loc[sym]
        as_of: date = row["as_of_date"]
        source = str(row["source"])
        if (today - as_of).days > max_age_days:
            return MarketCapInfo(None, STALE, as_of, source)
        has_sh = pd.notna(row["shares_outstanding"]) and row["shares_outstanding"] > 0
        has_mc = pd.notna(row["market_cap_cr"]) and row["market_cap_cr"] > 0

        # Preferred valuation: live calculation from shares_outstanding and LTP
        if has_sh and ltp is not None and ltp > 0:
            return MarketCapInfo(float(row["shares_outstanding"]) * ltp / CRORE, OK, as_of, source, "shares_x_ltp")
        # Fallback to reported market cap if available (e.g. pre-market, or when only market_cap_cr is present)
        if has_mc:
            return MarketCapInfo(float(row["market_cap_cr"]), OK, as_of, source, "reported")
        # If only shares_outstanding is available but LTP is missing/invalid
        if has_sh:
            return MarketCapInfo(None, NO_PRICE, as_of, source, "shares_x_ltp")
        return MarketCapInfo(None, INVALID, as_of, source)


def empty_table(error: str = "", path: Path | None = None) -> MarketCapTable:
    cols = ["company_name", "market_cap_cr", "shares_outstanding", "as_of_date", "source"]
    return MarketCapTable(pd.DataFrame(columns=cols), {}, path, error)


def parse_market_cap_frame(raw: pd.DataFrame, path: Path | None = None) -> MarketCapTable:
    df = raw.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    missing_cols = REQUIRED_COLUMNS - set(df.columns)
    if "market_cap_cr" not in df.columns and "shares_outstanding" not in df.columns:
        missing_cols.add("market_cap_cr or shares_outstanding")
    if missing_cols:
        return empty_table(f"Market-cap CSV missing columns: {sorted(missing_cols)}", path)
    for col in ("company_name", "market_cap_cr", "shares_outstanding"):
        if col not in df.columns:
            df[col] = pd.NA

    df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
    df = df[df["symbol"].ne("") & df["symbol"].ne("NAN")]

    problems: dict[str, str] = {}
    dup_mask = df["symbol"].duplicated(keep=False)
    for sym in df.loc[dup_mask, "symbol"].unique():
        problems[sym] = DUPLICATE
    df = df[~dup_mask]

    df["market_cap_cr"] = pd.to_numeric(df["market_cap_cr"], errors="coerce")
    df["shares_outstanding"] = pd.to_numeric(df["shares_outstanding"], errors="coerce")
    df["as_of_date"] = pd.to_datetime(df["as_of_date"], errors="coerce", format="%Y-%m-%d").dt.date

    has_mc = df["market_cap_cr"].notna()
    has_sh = df["shares_outstanding"].notna()
    valid = (
        df["as_of_date"].notna()
        & (has_mc | has_sh)
        & (~has_mc | (df["market_cap_cr"] > 0))
        & (~has_sh | (df["shares_outstanding"] > 0))
    )
    for sym in df.loc[~valid, "symbol"]:
        problems[sym] = INVALID
    df = df[valid].set_index("symbol")
    df["source"] = df["source"].fillna("").astype(str)
    return MarketCapTable(df, problems, path)


def load_market_cap(path: Path) -> MarketCapTable:
    if not path.exists():
        return empty_table(f"Market-cap file not found: {path.name}. See README 'Market-cap data'.", path)
    try:
        raw = pd.read_csv(path, dtype=str, comment="#", encoding="utf-8")
    except Exception as exc:  # malformed file
        return empty_table(f"Could not read market-cap file {path.name}: {exc}", path)
    return parse_market_cap_frame(raw, path)
