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

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

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
        # Kalshi requires RSA-PSS padding (NOT PKCS1v15)
        signature = private_key.sign(
            msg.encode("utf-8"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
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


# High-volume series to query (covers crypto, macro, politics, sports)
_DEFAULT_SERIES = ["KXBTC", "KXETH", "KXINFL", "KXFED", "KXGDP", "KXPRES", "KXHIGHNY", "KXNFL", "KXNBA"]


def get_markets(
    limit: int = 20,
    status: str = "open",
    series_ticker: str | None = None,
    min_volume: float = 0,
) -> list[dict]:
    """
    Fetch open markets from Kalshi, ordered by volume descending.
    Queries popular series by default to avoid zero-volume cross-category markets.
    """
    path = "/trade-api/v2/markets"
    headers = _build_signed_headers("GET", path)

    markets: list[dict] = []

    series_list = [series_ticker] if series_ticker else _DEFAULT_SERIES
    for series in series_list:
        try:
            params: dict = {"limit": min(limit, 20), "status": status, "series_ticker": series}
            resp = _SESSION.get(f"{KALSHI_BASE}/markets", params=params, headers=headers, timeout=10)
            if resp.ok:
                markets.extend(resp.json().get("markets", []))
        except Exception:
            pass

    # Exclude multivariate cross-category markets (no standard binary pricing)
    markets = [m for m in markets if not m.get("ticker", "").startswith("KXMVE")]

    # Sort by 24h volume — this surfaces actively-traded markets, not dead ones
    # with large lifetime volumes from months ago.
    markets.sort(key=lambda m: float(m.get("volume_24h", 0) or 0), reverse=True)

    # Filter: require at least min_volume 24h contracts AND a real price spread
    if min_volume > 0:
        markets = [
            m for m in markets
            if float(m.get("volume_24h", 0) or 0) >= min_volume
            and (m.get("yes_bid", 0) or m.get("yes_ask", 0) or m.get("last_price", 0))
        ]

    return markets[:limit]


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
    """
    Best-available YES probability (0-1).
    Priority: bid/ask mid → last_price → ask-only.
    Kalshi prices are in cents (0-99 integer).

    When only ask is available (bid=0), a one-sided quote is unreliable —
    we prefer last_price which reflects an actual executed trade.
    """
    try:
        bid  = int(market.get("yes_bid", 0) or 0)
        ask  = int(market.get("yes_ask", 0) or 0)
        last = int(market.get("last_price", 0) or 0)

        if bid > 0 and ask > 0:
            # Two-sided market: use mid
            return round((bid + ask) / 200.0, 4)
        if last > 0:
            # Prefer last executed trade over one-sided quote
            return round(last / 100.0, 4)
        if ask > 0:
            return round(ask / 100.0, 4)
    except (TypeError, ValueError, ZeroDivisionError):
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
    """
    Approximate 24h dollar volume.
    Kalshi volume fields are in NUMBER OF CONTRACTS (each worth $1 at expiry).
    Dollar value ≈ contracts × avg_price. We use last_price as a proxy.
    """
    try:
        contracts = float(market.get("volume_24h") or market.get("volume") or 0)
        last_price_cents = float(market.get("last_price") or 50)  # default 50¢ if unknown
        avg_price = max(1.0, min(99.0, last_price_cents)) / 100.0
        return round(contracts * avg_price, 2)
    except (TypeError, ValueError):
        return 0.0


def parse_liquidity(market: dict) -> float:
    """Available order book depth in dollars. Kalshi reports this in cents."""
    try:
        cents = float(market.get("liquidity", 0) or 0)
        return round(cents / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0
