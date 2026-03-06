"""
Kalshi REST API client.

Public markets endpoint: https://api.elections.kalshi.com/trade-api/v2/markets (no auth needed)
Authenticated endpoints (portfolio, etc.) require RSA-SHA256 signing — Bearer token alone is not
supported by Kalshi's trading API.

Docs: https://trading-api.readme.io/reference/getmarkets
"""

from __future__ import annotations

import os

import requests
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True) or find_dotenv())

# Public endpoint (no auth required for market listing)
KALSHI_PUBLIC_BASE = "https://api.elections.kalshi.com/trade-api/v2"
# Authenticated endpoint (requires RSA-SHA256 signed requests, not just Bearer token)
KALSHI_BASE = "https://trading-api.kalshi.com/trade-api/v2"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "EdgeAgent/1.0"})


def _get_auth_headers() -> dict:
    """Returns Bearer auth header if KALSHI_ACCESS_KEY is set."""
    access_key = os.environ.get("KALSHI_ACCESS_KEY", "").strip()
    if access_key and access_key not in ("paste_your_access_key_here", "your_kalshi_access_key_here"):
        return {"Authorization": f"Bearer {access_key}"}
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
    params: dict = {"limit": limit, "status": status}
    if series_ticker:
        params["series_ticker"] = series_ticker

    # Use public endpoint (no auth needed for market listing)
    resp = _SESSION.get(f"{KALSHI_PUBLIC_BASE}/markets", params=params, timeout=10)
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
    """Fetch account balance — requires RSA-SHA256 signed request (not supported in free mode)."""
    print("[KalshiAPI] Portfolio requires RSA-SHA256 signing — not available without private key")
    return None


def is_authenticated() -> bool:
    return bool(_get_auth_headers())


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
