"""
Polygon on-chain wallet signals for fresh-wallet vetting.
==========================================================

Inspired by / simplified from:
  https://github.com/pselamy/polymarket-insider-tracker

Key idea (from that repo's FreshWalletDetector):
  A wallet with very few on-chain transactions (low nonce) that places a
  large bet is suspicious — it's likely a throwaway account created
  specifically for insider trading or coordinated manipulation.

Differences from the original:
  - Sync (no asyncio), no Redis, no web3 library required.
  - Direct JSON-RPC over HTTP — just needs `requests`.
  - In-process dict cache with a 1-hour TTL.
  - Penalty value returned for direct subtraction from trust score.
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public Polygon RPC endpoints — no API key needed (tried in order)
# ---------------------------------------------------------------------------
_RPC_URLS = [
    "https://polygon-rpc.com",
    "https://polygon-bor.publicnode.com",
    "https://rpc.ankr.com/polygon",
]

# Wallets with fewer than this many txs are considered "fresh"
_FRESH_NONCE_THRESHOLD = 10

# In-process cache: address → (nonce, fetched_unix_ts)
_CACHE: dict[str, tuple[int, float]] = {}
_CACHE_TTL = 3600  # 1 hour — nonces only go up, stale=safe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rpc_nonce(address: str) -> int:
    """
    Call eth_getTransactionCount on Polygon via raw JSON-RPC.
    Returns the integer nonce, or -1 if all endpoints fail.
    """
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getTransactionCount",
        "params": [address, "latest"],
        "id": 1,
    }
    for url in _RPC_URLS:
        try:
            r = requests.post(url, json=payload, timeout=8)
            r.raise_for_status()
            hex_val = r.json().get("result", "0x0")
            return int(hex_val, 16)
        except Exception as exc:
            log.debug("RPC %s failed for %s: %s", url, address[:10], exc)
    return -1  # all endpoints failed


def _build_signals(nonce: int) -> dict:
    """Convert a nonce into human-readable vetting signals."""
    if nonce < 0:
        # RPC unavailable — be neutral, don't penalize
        return {"nonce": -1, "is_fresh": False, "fresh_penalty": 0.0}

    is_fresh = nonce < _FRESH_NONCE_THRESHOLD

    if not is_fresh:
        penalty = 0.0
    elif nonce == 0:
        penalty = 0.25  # brand-new wallet — maximum suspicion
    elif nonce < 3:
        penalty = 0.20
    elif nonce < 5:
        penalty = 0.15
    else:
        penalty = 0.08  # few txs but some history

    return {
        "nonce":         nonce,
        "is_fresh":      is_fresh,
        "fresh_penalty": penalty,   # subtract from final trust score
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def wallet_chain_signals(address: str) -> dict:
    """
    Return on-chain vetting signals for a Polygon wallet address.

    Returns a dict:
        nonce         (int)   — total Polygon transaction count
        is_fresh      (bool)  — nonce < threshold → likely throwaway/insider
        fresh_penalty (float) — 0.0–0.25 penalty to deduct from trust score

    Results are cached in-process for 1 hour.
    Uses only public Polygon RPC endpoints — no API key required.
    """
    address = address.lower()

    # In-process cache check
    cached = _CACHE.get(address)
    if cached is not None:
        nonce, ts = cached
        if time.time() - ts < _CACHE_TTL:
            log.debug("wallet_chain_signals: cache hit for %s…", address[:10])
            return _build_signals(nonce)

    nonce = _rpc_nonce(address)
    if nonce >= 0:
        _CACHE[address] = (nonce, time.time())

    return _build_signals(nonce)
