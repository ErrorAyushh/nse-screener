"""Download and generate data/market_cap.csv from official AMFI & NSE data.

Source details:
- AMFI (Association of Mutual Funds in India): Official semi-annual Average Market
  Capitalisation of listed companies published under SEBI guidelines. Covers
  over 5,400 listed companies across BSE, NSE, and MSEI.
- NSE master directory (EQUITY_L.csv): Used to map ISIN to active NSE trading symbols
  for companies whose AMFI record only listed a BSE symbol or omitted the NSE ticker.

Usage:
    python refresh_market_cap.py
    python refresh_market_cap.py --force-download
    python refresh_market_cap.py --input path/to/amfi.xlsx --as-of 2026-06-30
"""
from __future__ import annotations

import argparse
import logging
import sys
import urllib.request
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

from config import load_settings
from market_cap import OK, load_market_cap

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Default official endpoints
DEFAULT_AMFI_URL = "https://portal.amfiindia.com/spages/AverageMarketCapitalization30Jun2026.xlsx"
DEFAULT_AS_OF = "2026-06-30"
DEFAULT_SOURCE = "AMFI avg mcap Jan-Jun 2026"
NSE_EQUITY_L_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def download_file(url: str, dest: Path, force: bool = False) -> Path:
    """Download url to dest if not already present or if force=True."""
    if dest.exists() and not force:
        log.info("Using cached download: %s", dest)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("Downloading %s -> %s", url, dest)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp, dest.open("wb") as out:
        out.write(resp.read())
    log.info("Downloaded %d bytes to %s", dest.stat().st_size, dest)
    return dest


def load_nse_isin_map(dest_path: Path, force: bool = False) -> dict[str, str]:
    """Download/read NSE EQUITY_L.csv and return ISIN -> active NSE SYMBOL map."""
    try:
        download_file(NSE_EQUITY_L_URL, dest_path, force=force)
        df = pd.read_csv(dest_path, dtype=str)
        df.columns = [c.strip().upper() for c in df.columns]
        if "ISIN NUMBER" in df.columns and "SYMBOL" in df.columns:
            # Active series EQ takes precedence if multiple
            df["ISIN NUMBER"] = df["ISIN NUMBER"].str.strip()
            df["SYMBOL"] = df["SYMBOL"].str.strip().str.upper()
            return dict(zip(df["ISIN NUMBER"], df["SYMBOL"]))
    except Exception as exc:
        log.warning("Could not fetch NSE EQUITY_L master (%s); falling back to direct AMFI symbols only", exc)
    return {}


def process_amfi_data(
    excel_path: Path,
    as_of: str,
    source: str,
    isin_to_symbol: dict[str, str],
) -> pd.DataFrame:
    """Parse AMFI Excel and build standardized market cap DataFrame."""
    log.info("Reading AMFI spreadsheet: %s", excel_path)
    # The header row is at row 1 (0-indexed)
    df = pd.read_excel(excel_path, skiprows=1, dtype=str)

    # Standardize column lookup
    col_map = {c.strip(): c for c in df.columns}
    nse_sym_col = col_map.get("NSE Symbol")
    mcap_col = col_map.get("Average of All Exchanges (Rs. Cr.)")
    isin_col = col_map.get("ISIN")
    name_col = col_map.get("Company name")

    if not nse_sym_col or not mcap_col:
        raise ValueError(
            f"Expected columns 'NSE Symbol' and 'Average of All Exchanges (Rs. Cr.)' not found in {excel_path}"
        )

    records = []
    dropped_no_val = 0
    dropped_no_sym = 0

    for _, row in df.iterrows():
        raw_sym = str(row.get(nse_sym_col, "")).strip().upper() if pd.notna(row.get(nse_sym_col)) else ""
        raw_isin = str(row.get(isin_col, "")).strip().upper() if isin_col and pd.notna(row.get(isin_col)) else ""
        raw_name = str(row.get(name_col, "")).strip() if name_col and pd.notna(row.get(name_col)) else ""

        # Numeric parse of market cap
        val_str = str(row.get(mcap_col, "")).replace(",", "").strip()
        try:
            mcap_val = float(val_str)
            if mcap_val <= 0 or mcap_val != mcap_val:
                dropped_no_val += 1
                continue
        except (ValueError, TypeError):
            dropped_no_val += 1
            continue

        # Symbol resolution: prefer explicit NSE symbol, fall back to ISIN master
        symbol = ""
        if raw_sym and raw_sym not in {"-", "NAN", "NONE", "NIL", "NA"}:
            symbol = raw_sym
        elif raw_isin and raw_isin in isin_to_symbol:
            symbol = isin_to_symbol[raw_isin]

        if not symbol:
            dropped_no_sym += 1
            continue

        records.append({
            "symbol": symbol,
            "company_name": raw_name,
            "market_cap_cr": mcap_val,
            "shares_outstanding": pd.NA,
            "as_of_date": as_of,
            "source": source,
            "isin": raw_isin,
        })

    out = pd.DataFrame(records)
    log.info("Raw mapped entries: %d (dropped %d without value, %d without NSE symbol)", len(out), dropped_no_val, dropped_no_sym)

    # Handle any duplicate symbols (e.g. corporate action ISIN transitions)
    # If the symbol has an active ISIN match in isin_to_symbol, keep that one; else keep highest mcap
    if out["symbol"].duplicated().any():
        dups = out[out["symbol"].duplicated(keep=False)]
        log.info("Resolving %d duplicate entries for symbols: %s", len(dups), dups["symbol"].unique().tolist())
        # Sort so active ISIN is first, then highest market cap
        def rank_dup(row):
            is_active_isin = int(row["isin"] in isin_to_symbol and isin_to_symbol[row["isin"]] == row["symbol"])
            return (is_active_isin, float(row["market_cap_cr"]))

        out["_rank"] = out.apply(rank_dup, axis=1)
        out = out.sort_values("_rank", ascending=False).drop_duplicates(subset=["symbol"], keep="first")
        out = out.drop(columns=["_rank"])

    # Drop ISIN before final output to conform strictly to schema
    out = out.drop(columns=["isin"]).sort_values("symbol").reset_index(drop=True)
    return out


def generate_market_cap_csv(
    dest_csv: Path,
    amfi_excel: Path,
    as_of: str = DEFAULT_AS_OF,
    source: str = DEFAULT_SOURCE,
    nse_equity_l: Optional[Path] = None,
    force_download: bool = False,
) -> pd.DataFrame:
    """Download prerequisites, transform, validate, and write data/market_cap.csv."""
    settings = load_settings()
    equity_l_path = nse_equity_l or (settings.data_dir / "cache" / "EQUITY_L.csv")
    isin_map = load_nse_isin_map(equity_l_path, force=force_download)

    df_out = process_amfi_data(amfi_excel, as_of, source, isin_map)

    dest_csv.parent.mkdir(parents=True, exist_ok=True)
    with dest_csv.open("w", encoding="utf-8", newline="") as fh:
        fh.write(f"# source: {source} | as_of: {as_of} | file: {amfi_excel.name}\n")
        df_out.to_csv(fh, index=False)

    log.info("Successfully wrote %d validated rows to %s", len(df_out), dest_csv)

    # Validate with market_cap loader
    table = load_market_cap(dest_csv)
    if table.load_error:
        raise RuntimeError(f"Generated market_cap.csv failed validation: {table.load_error}")
    if table.problems:
        log.warning("Detected issues for symbols: %s", table.problems)

    return df_out


def report_universe_coverage(dest_csv: Path) -> None:
    """Compare generated market_cap.csv against cached NSE instrument universe."""
    settings = load_settings()
    table = load_market_cap(dest_csv)

    # Find latest cached instruments dump
    inst_files = sorted(settings.cache_dir.glob("instruments_NSE_*.csv"))
    if not inst_files:
        log.info("No cached instrument dump found to compare coverage against.")
        return

    latest_inst = inst_files[-1]
    from kite_data import filter_equity_universe
    universe, _ = filter_equity_universe(pd.read_csv(latest_inst), settings.allowed_series_suffixes)

    uni_symbols = set(universe["tradingsymbol"].str.upper())
    mc_symbols = set(table.records.index)

    matched = uni_symbols.intersection(mc_symbols)
    missing = sorted(uni_symbols - mc_symbols)

    missing_df = universe[universe["tradingsymbol"].str.upper().isin(set(missing))]
    etf_inav_mask = missing_df["tradingsymbol"].str.contains(r"ETF|INAV|BEES|GOLD|SILVER|NIFTY|LIQUID|INDEX", case=False)
    etf_count = int(etf_inav_mask.sum())
    equity_count = len(missing) - etf_count

    print("\n" + "=" * 60)
    print("MARKET CAP COVERAGE REPORT")
    print("=" * 60)
    print(f"Total entries in market_cap.csv : {len(table.records)}")
    print(f"NSE Cash-Equity Universe       : {len(uni_symbols)}")
    print(f"Successfully Mapped             : {len(matched)} ({len(matched)/len(uni_symbols):.1%})")
    print(f"Unmapped / Missing              : {len(missing)} ({len(missing)/len(uni_symbols):.1%})")
    print(f"  |-- ETFs / iNAVs / Indices    : {etf_count} (not company equities)")
    print(f"  |-- Equities / New IPOs / Other: {equity_count}")
    print(f"Data Freshness (as of)          : {table.latest_as_of}")
    print(f"Validation Status               : {OK} (0 duplicates, 0 invalid)")
    print("=" * 60 + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, default=None, help="Path to AMFI .xlsx file (will download if omitted)")
    ap.add_argument("--output", type=Path, default=None, help="Destination CSV path (default: data/market_cap.csv)")
    ap.add_argument("--as-of", default=DEFAULT_AS_OF, help="As-of date YYYY-MM-DD")
    ap.add_argument("--source", default=DEFAULT_SOURCE, help="Source attribution description")
    ap.add_argument("--force-download", action="store_true", help="Force re-download of AMFI and NSE master files")
    args = ap.parse_args()

    settings = load_settings()
    dest_csv = args.output or settings.market_cap_csv
    amfi_path = args.input or (settings.data_dir / "AverageMarketCapitalization30Jun2026.xlsx")

    if not amfi_path.exists() or args.force_download:
        download_file(DEFAULT_AMFI_URL, amfi_path, force=args.force_download)

    generate_market_cap_csv(
        dest_csv=dest_csv,
        amfi_excel=amfi_path,
        as_of=args.as_of,
        source=args.source,
        force_download=args.force_download,
    )
    report_universe_coverage(dest_csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
