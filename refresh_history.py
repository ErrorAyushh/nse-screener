"""Download / update the daily-candle cache from the command line.

Run once per trading day before (or soon after) the market opens:

    python refresh_history.py            # incremental (fast after first run)
    python refresh_history.py --force    # re-download the full lookback

The first run requests one instrument per call at under 3 requests/second,
so ~2,000 instruments take roughly 12-15 minutes. Later runs only fetch what
is missing.
"""
from __future__ import annotations

import argparse
import logging
import sys

from calendar_utils import now_ist
from config import load_settings
from kite_data import AuthError, DataPermissionError, DataUnavailableError, make_client, verify_session
from market_cap import load_market_cap
from pipeline import get_universe, history_tokens, refresh_history, session_info


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="re-download full lookback for every instrument")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = load_settings()
    now = now_ist()
    info = session_info(settings, now)
    try:
        kite = make_client(settings)
        user = verify_session(kite)
        print(f"Authenticated as {user}. Session {info.session}; caching completed candles through {info.last_completed}.")
        universe, stats = get_universe(kite, settings, now.date())
        mcap = load_market_cap(settings.market_cap_csv)
        if mcap.load_error:
            print(f"Market-cap warning: {mcap.load_error}")
        n = len(history_tokens(universe, mcap, settings.history_only_with_market_cap))
        print(f"Eligible NSE equities: {stats['eligible']}. Instruments to cache: {n}.")

        last = [-1]

        def progress(frac: float, label: str) -> None:
            pct = int(frac * 100)
            if pct != last[0] and pct % 5 == 0:
                last[0] = pct
                print(f"  {pct:3d}%  ({label})", flush=True)

        rep = refresh_history(kite, settings, universe, mcap, info, force=args.force, progress=progress)
    except AuthError as exc:
        print(f"Authentication problem: {exc}")
        return 2
    except DataPermissionError as exc:
        print(f"Permission problem: {exc}")
        return 3
    except DataUnavailableError as exc:
        print(f"Data problem: {exc}")
        return 4
    print(
        f"Done. fetched={rep.fetched} already_fresh={rep.skipped_fresh} "
        f"full_refetch(adjusted)={rep.full_refetch} errors={len(rep.errors)}"
    )
    for tok, err in list(rep.errors.items())[:20]:
        print(f"  token {tok}: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
