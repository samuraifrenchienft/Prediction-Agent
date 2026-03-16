"""
Goldsky subgraph — deeper on-chain trade history for wallet vetting.
=====================================================================

Approach taken from:
  https://github.com/warproxxx/poly_data  (update_utils/update_goldsky.py)

Why this matters:
  Polymarket's Data API returns a capped trade window per wallet.
  Goldsky has the *full* on-chain orderbook history — every filled order
  on the CLOB going back to genesis.  This lets us:

    1. Cross-check trade counts — if Goldsky shows 5× more trades than
       the Polymarket API, the wallet likely has multiple aliases or the
       API is silently paginating.

    2. Velocity analysis — detect burst-trading patterns (20+ fills in
       one hour) that the Polymarket API sometimes misses because of how
       it aggregates positions.

    3. Extend history for newer wallets — a wallet might look thin on
       Polymarket's leaderboard window but have 200 on-chain fills.

Goldsky endpoint is public and free — no API key required.
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

_GOLDSKY_URL = (
    "https://api.goldsky.com/api/public"
    "/project_cl6mb8i9h0003e201j6li0diw"
    "/subgraphs/orderbook-subgraph/0.0.1/gn"
)

# In-process cache: address → (trades_list, fetched_ts)
_CACHE: dict[str, tuple[list, float]] = {}
_CACHE_TTL = 3600  # 1 hour

# GraphQL query — filter by maker address, most recent first
_QUERY = """
query WalletTrades($maker: String!, $limit: Int!) {
    orderFilledEvents(
        where:          { maker: $maker }
        orderBy:        timestamp
        orderDirection: desc
        first:          $limit
    ) {
        id
        timestamp
        maker
        taker
        makerAssetId
        takerAssetId
        makerAmountFilled
        takerAmountFilled
        transactionHash
    }
}
"""


def fetch_onchain_trades(address: str, limit: int = 500) -> list[dict]:
    """
    Fetch on-chain order-filled events for a maker wallet via Goldsky.

    Returns a list of raw event dicts. Falls back to [] on any error.
    Results are cached in-process for 1 hour.
    """
    address = address.lower()

    cached = _CACHE.get(address)
    if cached is not None:
        trades, ts = cached
        if time.time() - ts < _CACHE_TTL:
            return trades

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(
                _GOLDSKY_URL,
                json={
                    "query":     _QUERY,
                    "variables": {"maker": address, "limit": limit},
                },
                timeout=15,
            )
            r.raise_for_status()
            body = r.json()

            if "errors" in body:
                log.warning(
                    "Goldsky errors for %s…: %s", address[:10], body["errors"]
                )
                return []

            events = body.get("data", {}).get("orderFilledEvents", [])
            _CACHE[address] = (events, time.time())
            log.debug(
                "Goldsky: %d on-chain trades for %s…", len(events), address[:10]
            )
            return events

        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)  # 1s, 2s

    log.debug("Goldsky fetch failed for %s… after retries: %s", address[:10], last_exc)
    return []


def goldsky_summary(address: str) -> dict:
    """
    Return a lightweight summary of on-chain trade activity.

    Returns:
        onchain_count   (int)   — total fills found on Goldsky
        burst_flag      (bool)  — True if >20 fills found in any 1-hour window
        extended_data   (bool)  — True if Goldsky has significantly more trades
                                  than Polymarket's API might surface
    """
    trades = fetch_onchain_trades(address)
    count = len(trades)

    # Burst detection: any 1-hour bucket with >20 fills
    from collections import Counter
    if count >= 2:
        buckets = Counter(
            int(float(t.get("timestamp", 0))) // 3600
            for t in trades
            if t.get("timestamp")
        )
        burst_flag = max(buckets.values(), default=0) > 20
    else:
        burst_flag = False

    return {
        "onchain_count": count,
        "burst_flag":    burst_flag,
        "extended_data": count >= 100,  # signals we have a deep history
    }
