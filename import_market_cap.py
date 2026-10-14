"""Convert a market-cap or shares-outstanding spreadsheet into data/market_cap.csv.

You supply a file you obtained legitimately (see README "Market-cap data") and
tell the script which columns hold the symbol and the value. Nothing is
estimated or filled in: rows without a usable number are dropped and reported.

Examples
--------
Average market cap in ₹ crore (e.g. AMFI half-yearly list saved as .xlsx/.csv):

    python import_market_cap.py amfi.xlsx --symbol-col "NSE Symbol" \
        --mcap-col "Average of All Exchanges (Rs. Cr.)" --unit crore \
        --as-of 2026-06-30 --source "AMFI avg mcap Jan-Jun 2026"

Shares outstanding (market cap is then computed live as shares x LTP):

    python import_market_cap.py shares.csv --symbol-col Symbol \
        --shares-col "Issued Shares" --as-of 2026-09-30 --source "NSE/BSE filings Sep-2026"

Use --list-columns first to see the column names in your file.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from config import load_settings
from market_cap import parse_market_cap_frame

UNIT_TO_CRORE = {"crore": 1.0, "lakh": 0.01, "million": 0.1, "rupees": 1e-7}


def read_any(path: Path, sheet: str | None, skiprows: int) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path, sheet_name=sheet or 0, dtype=str, skiprows=skiprows)
    return pd.read_csv(path, dtype=str, skiprows=skiprows)


def to_number(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False).str.strip(), errors="coerce")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path)
    ap.add_argument("--list-columns", action="store_true")
    ap.add_argument("--sheet", default=None)
    ap.add_argument("--skiprows", type=int, default=0)
    ap.add_argument("--symbol-col")
    ap.add_argument("--name-col")
    ap.add_argument("--mcap-col")
    ap.add_argument("--unit", choices=sorted(UNIT_TO_CRORE), default="crore")
    ap.add_argument("--shares-col")
    ap.add_argument("--as-of", required=False, help="YYYY-MM-DD date the figures refer to")
    ap.add_argument("--source", default="")
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument("--yes", action="store_true", help="overwrite existing output without asking")
    a = ap.parse_args()

    df = read_any(a.input, a.sheet, a.skiprows)
    if a.list_columns:
        for c in df.columns:
            print(repr(c))
        return 0
    if not a.symbol_col or not a.as_of or not a.source:
        ap.error("--symbol-col, --as-of and --source are required")
    if bool(a.mcap_col) == bool(a.shares_col):
        ap.error("give exactly one of --mcap-col or --shares-col")
    date.fromisoformat(a.as_of)

    out = pd.DataFrame({"symbol": df[a.symbol_col].astype(str).str.strip().str.upper()})
    out["company_name"] = df[a.name_col].astype(str).str.strip() if a.name_col else ""
    if a.mcap_col:
        out["market_cap_cr"] = to_number(df[a.mcap_col]) * UNIT_TO_CRORE[a.unit]
        out["shares_outstanding"] = pd.NA
    else:
        out["market_cap_cr"] = pd.NA
        out["shares_outstanding"] = to_number(df[a.shares_col])
    out["as_of_date"] = a.as_of
    out["source"] = a.source

    value_col = "market_cap_cr" if a.mcap_col else "shares_outstanding"
    bad = out[out["symbol"].isin(["", "NAN", "NONE", "-"]) | out[value_col].isna() | (out[value_col] <= 0)]
    out = out.drop(bad.index)
    table = parse_market_cap_frame(out)
    dups = [s for s, why in table.problems.items() if why == "DUPLICATE"]

    settings = load_settings()
    dest = a.output or settings.market_cap_csv
    if dest.exists() and not a.yes:
        if input(f"{dest} exists. Overwrite? [y/N] ").strip().lower() != "y":
            print("Aborted; nothing written.")
            return 1
    with dest.open("w", encoding="utf-8", newline="") as fh:
        fh.write(f"# source: {a.source} | as_of: {a.as_of} | imported from: {a.input.name}\n")
        out.to_csv(fh, index=False)
    print(f"Wrote {len(out)} rows to {dest}")
    print(f"Dropped {len(bad)} rows without a usable symbol/value.")
    if dups:
        print(f"WARNING: {len(dups)} duplicate symbols will be treated as unusable: {', '.join(dups[:15])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
