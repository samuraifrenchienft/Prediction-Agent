"""
Kalshi authenticated REST API client.

Authentication uses RSA-SHA256 signing:
  - KALSHI_ACCESS_KEY   → your API key UUID from kalshi.com/profile/api
  - KALSHI_PRIVATE_KEY_PATH → path to your .pem private key file (downloaded from same page)

The access signature and access timestamp are generated automatically per-request.
Docs: https://trading-api.readme.io/reference/getmarkets
"""

from __future__ import annotations

import base64
import os
import time

import requests
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True) or find_dotenv())

KALSHI_BASE = "https://trading-api.kalshi.com/trade-api/v2"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "EdgeAgent/1.0"})


def _get_auth_headers(method: str, path: str) -> dict:
    """
    Build Kalshi auth headers for a request.
    Requires KALSHI_ACCESS_KEY and KALSHI_PRIVATE_KEY_PATH in .env
    Returns empty dict if credentials are not configured.
    """
    access_key = os.environ.get("KALSHI_ACCESS_KEY", "").strip()
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()

    if not access_key or not key_path:
        return {}

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        # Timestamp in milliseconds as string
        timestamp_ms = str(int(time.time() * 1000))

        # Message to sign: timestamp + METHOD + /trade-api/v2/path
        message = f"{timestamp_ms}{method.upper()}{path}"

        with open(key_path, "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)

        signature = base64.b64encode(
            private_key.sign(message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        ).decode("utf-8")

        return {
            "KALSHI-ACCESS-KEY": access_key,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    except FileNotFoundError:
        print(f"[KalshiAPI] Private key file not found: {key_path}")
        return {}
    except Exception as e:
        print(f"[KalshiAPI] Auth error: {e}")
        return {}


def get_markets(
    limit: int = 20,
    status: str = "open",
    series_ticker: str | None = None,
    min_volume: float = 0,
) -> list[dict]:
    """
    Fetch open markets from Kalshi, ordered by volume descending.
    Automatically uses authentication if KALSHI_ACCESS_KEY + KALSHI_PRIVATE_KEY_PATH are set.
    """
    path = "/trade-api/v2/markets"
    params: dict = {"limit": limit, "status": status}
    if series_ticker:
        params["series_ticker"] = series_ticker

    headers = _get_auth_headers("GET", path)
    resp = _SESSION.get(f"{KALSHI_BASE}/markets", params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    markets = resp.json().get("markets", [])

    # Exclude multivariate cross-category markets (no standard binary pricing)
    markets = [
        m for m in markets
        if not m.get("ticker", "").startswith("KXMVECROSSCATEGORY")
        and not m.get("ticker", "").startswith("KXMVESPORTS")
    ]

    # Sort by total volume descending; filter out zero-activity if requested
    markets.sort(key=lambda m: float(m.get("volume", 0) or 0), reverse=True)
    if min_volume > 0:
        markets = [m for m in markets if float(m.get("volume", 0) or 0) >= min_volume]

    return markets


def get_portfolio_balance() -> dict | None:
    """Fetch account balance — requires authentication."""
    path = "/trade-api/v2/portfolio/balance"
    headers = _get_auth_headers("GET", path)
    if not headers:
        print("[KalshiAPI] Auth headers missing — set KALSHI_ACCESS_KEY + KALSHI_PRIVATE_KEY_PATH")
        return None
    try:
        resp = _SESSION.get(f"{KALSHI_BASE}/portfolio/balance", headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[KalshiAPI] Portfolio fetch error: {e}")
        return None


def is_authenticated() -> bool:
    """Check whether Kalshi credentials are configured."""
    return bool(
        os.environ.get("KALSHI_ACCESS_KEY")
        and os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    )


# ── Parse helpers ─────────────────────────────────────────────────────────────

def parse_market_prob(market: dict) -> float:
    """Mid-price from yes_bid/yes_ask, falling back to last_price."""
    try:
        bid = market.get("yes_bid", 0) or 0
        ask = market.get("yes_ask", 0) or 0
        if bid > 0 and ask > 0:
            return round(((bid / 100.0) + (ask / 100.0)) / 2, 4)
        if ask > 0:
            return round(ask / 100.0, 4)
        last = market.get("last_price", 0) or 0
        if last > 0:
            return round(last / 100.0, 4)
    except (TypeError, ZeroDivisionError):
        pass
    return 0.5


def parse_spread_bps(market: dict) -> float:
    try:
        bid = market.get("yes_bid", 0) or 0
        ask = market.get("yes_ask", 0) or 0
        if bid and ask:
            return round((ask - bid) * 100, 1)
    except TypeError:
        pass
    return 100.0


def parse_volume(market: dict) -> float:
    try:
        v = market.get("volume_24h") or market.get("volume") or 0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_liquidity(market: dict) -> float:
    try:
        return float(market.get("liquidity", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
