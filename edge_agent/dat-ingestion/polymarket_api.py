"""
Polymarket public Gamma API client — no API key required.
Docs: https://docs.polymarket.com/#introduction
Gamma (market search): https://gamma-api.polymarket.com
CLOB (order book):     https://clob.polymarket.com
"""

from __future__ import annotations

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "EdgeAgent/1.0"})


def get_active_markets(limit: int = 20, offset: int = 0) -> list[dict]:
    """
    Fetch active binary markets from Polymarket Gamma API, ordered by volume.
    Key fields available: conditionId, question, outcomePrices, lastTradePrice,
    spread, volumeNum, volume24hrClob, liquidityClob, endDateIso, groupItemTitle
    """
    params = {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "offset": offset,
        "order": "volumeNum",
        "ascending": "false",
    }
    resp = _SESSION.get(f"{GAMMA_BASE}/markets", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_market_orderbook(token_id: str) -> dict:
    """Fetch raw order book for a CLOB token ID."""
    resp = _SESSION.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def parse_market_prob(market: dict) -> float:
    """
    YES probability (0-1) from current CLOB token price.

    outcomePrices[0] is the current YES token price — always reflects the live
    CLOB state. lastTradePrice is the last *executed trade* which can be hours
    or days old in illiquid markets and causes stale probability readings.
    """
    try:
        prices = market.get("outcomePrices", [])
        if prices:
            return round(float(prices[0]), 4)
        # Fallback only if outcomePrices missing entirely
        last = market.get("lastTradePrice")
        if last is not None:
            return round(float(last), 4)
    except (ValueError, TypeError):
        pass
    return 0.5


def parse_spread_bps(market: dict) -> float:
    """Spread in basis points — prefer 'spread' field, fall back to outcomePrices."""
    try:
        spread = market.get("spread")
        if spread is not None:
            return round(float(spread) * 10000, 1)
        prices = market.get("outcomePrices", [])
        if len(prices) >= 2:
            spread_raw = abs(1.0 - (float(prices[0]) + float(prices[1])))
            return round(spread_raw * 10000, 1)
    except (ValueError, TypeError):
        pass
    return 150.0


def parse_volume_24h(market: dict) -> float:
    """24-hour CLOB volume."""
    try:
        v = market.get("volume24hrClob") or market.get("volume24hr") or market.get("volumeNum") or 0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_liquidity(market: dict) -> float:
    try:
        v = market.get("liquidityClob") or market.get("liquidityNum") or market.get("liquidity") or 0
        return float(v)
    except (TypeError, ValueError):
        return 0.0
