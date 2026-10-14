"""Daily Kite Connect login helper (run once each morning).

Flow (https://kite.trade/docs/connect/v3/user/):
1. Opens https://kite.zerodha.com/connect/login?v=3&api_key=<key> in your browser.
2. You log in on Zerodha's own page (password + 2FA). This script never sees it.
3. Zerodha redirects to the Redirect URL registered for your app, with
   ``?request_token=...&action=login&status=success`` in the address bar.
4. Paste that full redirect URL (or just the request_token) here.
5. The script exchanges it (checksum = SHA-256 of api_key + request_token + api_secret,
   computed by the official client) for an access token, and writes it to .env
   as KITE_ACCESS_TOKEN together with KITE_ACCESS_TOKEN_CREATED_AT.

The access token is never printed. It expires at 06:00 IST the next day; then
run this script again. The request_token is single-use and valid for minutes.
"""
from __future__ import annotations

import sys
import webbrowser
from urllib.parse import parse_qs, urlparse

from dotenv import set_key

from calendar_utils import now_ist
from config import ENV_PATH, load_settings


def extract_request_token(text: str) -> str:
    text = text.strip()
    if "request_token=" in text:
        qs = parse_qs(urlparse(text).query)
        vals = qs.get("request_token") or []
        if not vals:
            raise ValueError("No request_token found in that URL")
        return vals[0].strip()
    if not text or any(c in text for c in " /?&="):
        raise ValueError("That does not look like a request_token or redirect URL")
    return text


def main() -> int:
    settings = load_settings()
    if not settings.api_key or not settings.api_secret:
        print("KITE_API_KEY and KITE_API_SECRET must be set in .env first (see .env.example).")
        return 1
    if not ENV_PATH.exists():
        print(f"{ENV_PATH.name} not found. Copy .env.example to .env and fill in your API key and secret.")
        return 1

    from kiteconnect import KiteConnect
    from kiteconnect import exceptions as kex

    kite = KiteConnect(api_key=settings.api_key)
    url = kite.login_url()
    print("\nOpening the Zerodha login page in your browser:")
    print(f"  {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    raw = input("After logging in, paste the full redirect URL (or the request_token): ")
    try:
        request_token = extract_request_token(raw)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    try:
        session = kite.generate_session(request_token, api_secret=settings.api_secret)
    except kex.TokenException:
        print("Zerodha rejected the request_token (expired or already used). Log in again and retry quickly.")
        return 1
    except kex.KiteException as exc:
        print(f"Login failed: {type(exc).__name__}. Check your API key/secret and redirect URL.")
        return 1

    token = session.get("access_token", "")
    if not token:
        print("Login response did not include an access token.")
        return 1
    set_key(str(ENV_PATH), "KITE_ACCESS_TOKEN", token, quote_mode="never")
    set_key(str(ENV_PATH), "KITE_ACCESS_TOKEN_CREATED_AT", now_ist().isoformat(timespec="seconds"), quote_mode="never")
    print(f"\nLogged in as {session.get('user_id', '?')}. Access token saved to .env (not shown).")
    print("It expires at 06:00 IST tomorrow. Restart the dashboard or press 'Reconnect' to pick it up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
