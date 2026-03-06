"""
Kalshi REST API client with RSA-SHA256 signing.

Set in .env:
  KALSHI_ACCESS_KEY=<your UUID access key>
  KALSHI_PRIVATE_KEY_PATH=kalshi_private_key.pem  (path to PEM file)

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


def _load_private_key():
    """Load RSA private key from PEM file."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend

        pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
        if not pem_path:
            return None

        # Resolve relative to project root (walk up from cwd)
        if not os.path.isabs(pem_path):
            # Try cwd and parent dirs
            search_dir = os.getcwd()
            for _ in range(5):
                candidate = os.path.join(search_dir, pem_path)
                if os.path.exists(candidate):
                    pem_path = candidate
                    break
                search_dir = os.path.dirname(search_dir)

        if not os.path.exists(pem_path):
            return None

        with open(pem_path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
    except Exception as e:
        print(f"[KalshiAPI] Could not load private key: {e}")
        return None


def _build_signed_headers(method: str, path: str) -> dict:
    """Build RSA-SHA256 signed headers for Kalshi API requests."""
    access_key = os.environ.get("KALSHI_ACCESS_KEY", "").strip()
    placeholder_keys = ("paste_your_access_key_here", "your_kalshi_access_key_here", "")
    if not access_key or access_key in placeholder_keys:
        return {}

    private_key = _load_private_key()
    if private_key is None:
        return {}

    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        timestamp_ms = str(int(time.time() * 1000))
        # Kalshi signing string: timestamp + method + path (no separators, no query string)
        msg = f"{timestamp_ms}{method.upper()}{path}"
        signature = private_key.sign(msg.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        sig_b64 = base64.b64encode(signature).decode("utf-8")

        return {
            "KALSHI-ACCESS-KEY": access_key,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
        }
    except Exception as e:
        print(f"[KalshiAPI] Signing error: {e}")
        return {}


def is_authenticated() -> bool:
    """Returns True if both access key and private key are available."""
    access_key = os.environ.get("KALSHI_ACCESS_KEY", "").strip()
    placeholder_keys = ("paste_your_access_key_here", "your_kalshi_access_key_here", "")
    if not access_key or access_key in placeholder_keys:
        return False
    return _load_private_key() is not None


def get_markets(
    limit: int = 20,
    status: str = "open",
    series_ticker: str | None = None,
    min_volume: float = 0,
) -> list[dict]:
    """
    Fetch open markets from Kalshi, ordered by volume descending.
    Uses RSA-SHA256 signed requests if KALSHI_ACCESS_KEY + KALSHI_PRIVATE_KEY_PATH are set.
    """
    params: dict = {"limit": limit, "status": status}
    if series_ticker:
        params["series_ticker"] = series_ticker

    path = "/trade-api/v2/markets"
    headers = _build_signed_headers("GET", path)

    resp = _SESSION.get(f"{KALSHI_BASE}/markets", params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    markets = resp.json().get("markets", [])

    # Exclude multivariate cross-category markets (no standard binary pricing)
    markets = [
        m for m in markets
        if not m.get("ticker", "").startswith("KXMVE")
    ]

    # Sort by total volume descending; filter out zero-activity if requested
    markets.sort(key=lambda m: float(m.get("volume", 0) or 0), reverse=True)
    if min_volume > 0:
        markets = [m for m in markets if float(m.get("volume", 0) or 0) >= min_volume]

    return markets


def get_portfolio_balance() -> dict | None:
    """Fetch account balance — requires RSA-SHA256 signed request."""
    path = "/trade-api/v2/portfolio/balance"
    headers = _build_signed_headers("GET", path)
    if not headers:
        print("[KalshiAPI] Set KALSHI_ACCESS_KEY + KALSHI_PRIVATE_KEY_PATH in .env")
        return None
    try:
        resp = _SESSION.get(f"{KALSHI_BASE}/portfolio/balance", headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[KalshiAPI] Portfolio fetch error: {e}")
        return None


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
