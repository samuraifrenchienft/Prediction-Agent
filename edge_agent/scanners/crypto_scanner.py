"""
Crypto Scanner — Binance price data + Polymarket/Kalshi crypto market gap detector.
====================================================================================

Flow:
  1. Fetch 30 daily candles from Binance public REST API (no key needed)
  2. Compute: current price, 24h/7d/30d change, daily volatility
  3. Match symbols to active prediction market questions (BTC, ETH, SOL, etc.)
  4. For price-threshold markets ("Will BTC exceed $X by [date]?"):
       • Use lognormal drift model to estimate probability
       • Compare vs market price — alert if gap > MIN_GAP_PP
  5. For trend-based markets ("Will BTC be higher at end of month?"):
       • Use momentum signal (RSI proxy + multi-timeframe trend)
       • Compare vs market price

Binance public API docs: https://binance-docs.github.io/apidocs/spot/en/
No API key required for public market data.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported assets — maps display names / ticker keywords to Binance symbols
# ---------------------------------------------------------------------------

_ASSET_MAP: dict[str, str] = {
    "btc":       "BTCUSDT",
    "bitcoin":   "BTCUSDT",
    "eth":       "ETHUSDT",
    "ethereum":  "ETHUSDT",
    "sol":       "SOLUSDT",
    "solana":    "SOLUSDT",
    "xrp":       "XRPUSDT",
    "ripple":    "XRPUSDT",
    "doge":      "DOGEUSDT",
    "dogecoin":  "DOGEUSDT",
    "bnb":       "BNBUSDT",
    "ada":       "ADAUSDT",
    "cardano":   "ADAUSDT",
    "avax":      "AVAXUSDT",
    "avalanche": "AVAXUSDT",
    "link":      "LINKUSDT",
    "chainlink": "LINKUSDT",
    "matic":     "MATICUSDT",
    "polygon":   "MATICUSDT",
    "dot":       "DOTUSDT",
    "polkadot":  "DOTUSDT",
    "ltc":       "LTCUSDT",
    "litecoin":  "LTCUSDT",
    "shib":      "SHIBUSDT",
    "pepe":      "PEPEUSDT",
    "ton":       "TONUSDT",
    "sui":       "SUIUSDT",
    "apt":       "APTUSDT",
    "aptos":     "APTUSDT",
    "arb":       "ARBUSDT",
    "arbitrum":  "ARBUSDT",
    "op":        "OPUSDT",
    "optimism":  "OPUSDT",
    "inj":       "INJUSDT",
    "sei":       "SEIUSDT",
    "wld":       "WLDUSDT",
    "worldcoin": "WLDUSDT",
}

# Minimum gap in pp to fire an alert
_MIN_GAP_PP = 15.0

# Cache: binance_symbol → (data, fetched_at)
_PRICE_CACHE: dict[str, tuple[dict, float]] = {}
_PRICE_TTL = 900   # 15 minutes — crypto moves fast, keep it fresh

# Binance API base
_BINANCE_BASE = "https://api.binance.com/api/v3"


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class CryptoGap:
    title:        str
    ticker:       str
    venue:        str
    symbol:       str        # e.g. "BTCUSDT"
    current_price: float
    market_prob:  float      # current YES price 0.0–1.0
    model_prob:   float      # our model estimate 0.0–1.0
    gap_pp:       float      # model − market, signed pp
    action:       str        # "BUY YES" or "BUY NO"
    change_24h:   float      # % change last 24h
    change_7d:    float      # % change last 7d
    daily_vol:    float      # annualised daily vol (σ)
    signal_notes: str        # human-readable signal summary


# ---------------------------------------------------------------------------
# Binance data fetchers
# ---------------------------------------------------------------------------

def _fetch_ticker_24h(symbol: str) -> Optional[dict]:
    """Fetch 24h stats from Binance (price, change, volume). Retries up to 3×."""
    for attempt in range(3):
        try:
            r = requests.get(
                f"{_BINANCE_BASE}/ticker/24hr",
                params={"symbol": symbol},
                timeout=8,
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            if attempt < 2:
                time.sleep(1 * (attempt + 1))
                log.debug("[CryptoScan] 24h ticker %s attempt %d failed: %s — retrying", symbol, attempt + 1, exc)
            else:
                log.debug("[CryptoScan] 24h ticker failed for %s after 3 attempts: %s", symbol, exc)
    return None


def _fetch_daily_klines(symbol: str, limit: int = 30) -> Optional[list]:
    """
    Fetch daily OHLCV candles from Binance.
    Returns list of [open_time, open, high, low, close, volume, ...] or None.
    """
    cached = _PRICE_CACHE.get(symbol)
    if cached:
        data, ts = cached
        if time.time() - ts < _PRICE_TTL:
            return data.get("klines")

    for attempt in range(3):
        try:
            r = requests.get(
                f"{_BINANCE_BASE}/klines",
                params={"symbol": symbol, "interval": "1d", "limit": limit},
                timeout=8,
            )
            r.raise_for_status()
            klines = r.json()

            # Fetch 24h ticker alongside
            ticker = _fetch_ticker_24h(symbol)

            payload = {"klines": klines, "ticker": ticker}
            _PRICE_CACHE[symbol] = (payload, time.time())
            return klines
        except Exception as exc:
            if attempt < 2:
                time.sleep(1 * (attempt + 1))
                log.debug("[CryptoScan] klines %s attempt %d failed: %s — retrying", symbol, attempt + 1, exc)
            else:
                log.debug("[CryptoScan] klines failed for %s after 3 attempts: %s", symbol, exc)
                # Return stale cache if available
                stale = _PRICE_CACHE.get(symbol)
                if stale:
                    log.debug("[CryptoScan] returning stale cache for %s", symbol)
                    return stale[0].get("klines")
    return None


def _get_price_data(symbol: str) -> Optional[dict]:
    """
    Return a summary dict for a Binance symbol:
        current_price, change_24h (%), change_7d (%), daily_vol (annualised σ)
    """
    cached = _PRICE_CACHE.get(symbol)
    if cached:
        data, ts = cached
        if time.time() - ts < _PRICE_TTL:
            return _summarise(data)

    # Cold fetch
    klines = _fetch_daily_klines(symbol)
    if not klines:
        return None

    cached = _PRICE_CACHE.get(symbol)
    if not cached:
        return None
    return _summarise(cached[0])


def _compute_rsi(closes: list[float], period: int = 14) -> float:
    """
    Compute RSI(period) from daily closes.
    Returns RSI 0–100 or 50.0 if insufficient data.
    """
    if len(closes) < period + 1:
        return 50.0
    gains  = [max(0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss < 0.001:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _summarise(data: dict) -> Optional[dict]:
    """Convert raw cache payload into a clean price summary."""
    klines = data.get("klines", [])
    ticker = data.get("ticker")
    if len(klines) < 2:
        return None

    try:
        closes = [float(k[4]) for k in klines]
        current = closes[-1]

        # 24h change from ticker (more accurate than daily candle)
        change_24h = float(ticker["priceChangePercent"]) / 100 if ticker else (closes[-1] - closes[-2]) / closes[-2]

        # 7d change
        close_7d_ago = closes[-7] if len(closes) >= 7 else closes[0]
        change_7d = (current - close_7d_ago) / close_7d_ago

        # Daily log-return volatility → annualise
        log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        if len(log_returns) >= 2:
            mean = sum(log_returns) / len(log_returns)
            variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
            daily_vol = math.sqrt(variance)
        else:
            daily_vol = 0.03  # 3% default if insufficient data

        # RSI(14) — sharper momentum signal for trend markets
        rsi = _compute_rsi(closes, 14)

        return {
            "current_price": current,
            "change_24h":    change_24h,
            "change_7d":     change_7d,
            "daily_vol":     daily_vol,
            "closes":        closes,
            "rsi_14":        round(rsi, 1),
        }
    except Exception as exc:
        log.debug("[CryptoScan] Summarise failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Market title parsing
# ---------------------------------------------------------------------------

import re

# Threshold price patterns: "$100,000", "$100k", "100000"
_PRICE_THRESH_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*([km])?",
    re.IGNORECASE,
)
# "above/exceed/reach/over $X"
_ABOVE_RE = re.compile(
    r"(?:above|exceed|reach|hit|over|higher than|more than)\s+\$?\s*([\d,]+(?:\.\d+)?)\s*([km])?",
    re.IGNORECASE,
)
# "below/under/drop below $X"
_BELOW_RE = re.compile(
    r"(?:below|under|drop(?:s)?\s+(?:to\s+)?below|less than|lower than)\s+\$?\s*([\d,]+(?:\.\d+)?)\s*([km])?",
    re.IGNORECASE,
)
# Days/date patterns for expiry estimation
_END_MONTH_RE = re.compile(r"end\s+of\s+(?:the\s+)?month|eom|by\s+(?:end\s+of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.IGNORECASE)
_DAYS_RE      = re.compile(r"(?:in|within)\s+(\d+)\s+days?", re.IGNORECASE)


def _parse_price_value(value_str: str, suffix: Optional[str]) -> float:
    """Convert parsed string + suffix to float. E.g. '100', 'k' → 100000."""
    val = float(value_str.replace(",", ""))
    if suffix:
        s = suffix.lower()
        if s == "k":
            val *= 1_000
        elif s == "m":
            val *= 1_000_000
    return val


def _detect_asset(title: str) -> Optional[str]:
    """Return the Binance symbol for the first asset mentioned in title."""
    t = title.lower()
    # Longest match first to avoid "eth" matching "ethereum" twice
    for keyword, symbol in sorted(_ASSET_MAP.items(), key=lambda x: -len(x[0])):
        if re.search(r"\b" + re.escape(keyword) + r"\b", t):
            return symbol
    return None


def _estimate_days_to_expiry(title: str) -> int:
    """Rough estimate of days remaining for a market, from title."""
    m = _DAYS_RE.search(title)
    if m:
        return int(m.group(1))
    if _END_MONTH_RE.search(title):
        # Approximate: ~15 days to end of month on average
        return 15
    # Default: assume 30-day market
    return 30


# ---------------------------------------------------------------------------
# Lognormal probability model
# ---------------------------------------------------------------------------

def _lognormal_prob_above(
    current: float,
    target:  float,
    days:    int,
    daily_vol: float,
    daily_drift: float = 0.0,
) -> float:
    """
    P(S_T > target) under a log-normal price process.

    Uses Black-Scholes-style formula:
        d = (ln(S/K) + (μ - σ²/2)·T) / (σ·√T)
        P(S_T > K) = N(d)

    Returns probability 0.0–1.0.
    """
    if current <= 0 or target <= 0 or days <= 0 or daily_vol <= 0:
        return 0.5

    T    = float(days)
    sigma = daily_vol
    mu    = daily_drift

    d = (math.log(current / target) + (mu - sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))

    # Normal CDF approximation (Abramowitz & Stegun 26.2.17)
    return _norm_cdf(d)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF, pure Python (no scipy dependency)."""
    return (1.0 + math.erf(x / math.sqrt(2))) / 2.0


def _estimate_daily_drift(price_data: dict) -> float:
    """
    Estimate recent daily log-drift from 7d and 30d price changes.
    Weights recent momentum more heavily.
    """
    closes  = price_data.get("closes", [])
    if len(closes) < 7:
        return 0.0

    # 7d log-return → average daily log-drift over last week
    drift_7d = math.log(closes[-1] / closes[-7]) / 7 if closes[-7] > 0 else 0.0
    # 30d log-return → slower drift
    drift_30d = math.log(closes[-1] / closes[0]) / len(closes) if closes[0] > 0 else 0.0

    # Weight 60% recent (7d), 40% longer (30d)
    return drift_7d * 0.60 + drift_30d * 0.40


def _build_signal_notes(pd: dict, action_type: str, target: Optional[float] = None) -> str:
    """Build a human-readable signal summary."""
    price    = pd["current_price"]
    c24h     = pd["change_24h"] * 100
    c7d      = pd["change_7d"]  * 100
    vol_ann  = pd["daily_vol"]  * math.sqrt(365) * 100
    rsi      = pd.get("rsi_14", 50.0)

    parts = [
        f"Price: ${price:,.2f}",
        f"24h: {c24h:+.1f}%",
        f"7d: {c7d:+.1f}%",
        f"RSI(14): {rsi:.0f}",
        f"Ann.vol: {vol_ann:.0f}%",
    ]
    if target:
        dist = (target - price) / price * 100
        parts.append(f"Target ${target:,.0f} ({dist:+.1f}% away)")

    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_crypto_markets(markets: list[dict]) -> list[CryptoGap]:
    """
    Scan a list of prediction market dicts for crypto mispricings.

    Each market dict:
        title / question  (str)   — the market question
        price / yes_price (float) — current YES price 0.0–1.0
        ticker / id       (str)   — unique identifier
        venue             (str)   — "kalshi" or "polymarket"

    Returns list of CryptoGap sorted by |gap_pp| descending.
    """
    gaps: list[CryptoGap] = []
    n_checked = 0
    price_cache: dict[str, Optional[dict]] = {}   # local within this scan call

    for mkt in markets:
        title  = mkt.get("title") or mkt.get("question", "")
        price  = float(mkt.get("price") or mkt.get("yes_price") or 0.5)
        ticker = mkt.get("ticker") or mkt.get("id", "")
        venue  = mkt.get("venue", "kalshi")

        symbol = _detect_asset(title)
        if not symbol:
            continue

        # Fetch price data (cached per symbol within this call)
        if symbol not in price_cache:
            price_cache[symbol] = _get_price_data(symbol)
        pd = price_cache[symbol]
        if not pd:
            continue

        n_checked += 1
        current  = pd["current_price"]
        daily_vol = pd["daily_vol"]

        # ── Determine market type and estimate model probability ──────────

        model_prob:  Optional[float] = None
        action_type: str             = "threshold"
        target_price: Optional[float] = None

        # Check for price-threshold "above" market
        m_above = _ABOVE_RE.search(title)
        if m_above:
            target_price = _parse_price_value(m_above.group(1), m_above.group(2))
            days         = _estimate_days_to_expiry(title)
            drift        = _estimate_daily_drift(pd)
            model_prob   = _lognormal_prob_above(current, target_price, days, daily_vol, drift)
            action_type  = "above"

        # Check for price-threshold "below" market
        if model_prob is None:
            m_below = _BELOW_RE.search(title)
            if m_below:
                target_price = _parse_price_value(m_below.group(1), m_below.group(2))
                days         = _estimate_days_to_expiry(title)
                drift        = _estimate_daily_drift(pd)
                # P(below) = 1 - P(above)
                model_prob   = 1.0 - _lognormal_prob_above(current, target_price, days, daily_vol, drift)
                action_type  = "below"

        # Fallback: momentum-only signal for "higher/lower by X" markets
        if model_prob is None:
            t = title.lower()
            is_higher = any(w in t for w in ("higher", "up", "rise", "rally", "bull"))
            is_lower  = any(w in t for w in ("lower", "down", "drop", "fall", "bear"))
            if is_higher or is_lower:
                # Blended momentum: 24h price + 7d price + RSI(14) normalised
                # RSI/100 - 0.5 maps RSI 0→-0.5, 50→0.0, 100→+0.5
                rsi_norm = (pd.get("rsi_14", 50.0) / 100.0) - 0.5
                momentum = (pd["change_24h"] * 0.30
                            + pd["change_7d"] * 0.30
                            + rsi_norm * 0.40)
                # Logistic transform: momentum of +10% → ~73% prob
                model_prob  = 1.0 / (1.0 + math.exp(-momentum * 10))
                if is_lower:
                    model_prob = 1.0 - model_prob
                action_type = "trend"

        if model_prob is None:
            continue

        # Clamp
        model_prob = max(0.02, min(0.98, model_prob))

        gap_pp = (model_prob - price) * 100
        if abs(gap_pp) < _MIN_GAP_PP:
            continue

        action = "BUY YES" if gap_pp > 0 else "BUY NO"
        notes  = _build_signal_notes(pd, action_type, target_price)

        log.info(
            "[CryptoScan] Gap: '%s' | mkt=%.0f%% model=%.0f%% gap=%+.0fpp → %s",
            title[:60], price * 100, model_prob * 100, gap_pp, action,
        )

        gaps.append(CryptoGap(
            title         = title,
            ticker        = ticker,
            venue         = venue,
            symbol        = symbol,
            current_price = round(current, 4),
            market_prob   = round(price, 3),
            model_prob    = round(model_prob, 3),
            gap_pp        = round(gap_pp, 1),
            action        = action,
            change_24h    = round(pd["change_24h"] * 100, 2),
            change_7d     = round(pd["change_7d"]  * 100, 2),
            daily_vol     = round(pd["daily_vol"]  * 100, 4),
            signal_notes  = notes,
        ))

    log.info("[CryptoScan] Checked %d crypto markets, found %d gaps", n_checked, len(gaps))
    return sorted(gaps, key=lambda g: -abs(g.gap_pp))


# ---------------------------------------------------------------------------
# Quick price fetch for AI context injection
# ---------------------------------------------------------------------------

def get_crypto_price_context(symbols: Optional[list[str]] = None) -> str:
    """
    Return a compact price summary string for AI prompt injection.
    Defaults to BTC, ETH, SOL.
    """
    if symbols is None:
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    lines = ["LIVE CRYPTO PRICES (Binance, ~15min cache):"]
    for sym in symbols:
        pd = _get_price_data(sym)
        if not pd:
            continue
        name = sym.replace("USDT", "")
        c24  = pd["change_24h"] * 100
        c7d  = pd["change_7d"]  * 100
        rsi  = pd.get("rsi_14", 50.0)
        lines.append(
            f"  {name}: ${pd['current_price']:,.2f} | 24h {c24:+.1f}% | 7d {c7d:+.1f}% | RSI {rsi:.0f}"
        )

    return "\n".join(lines) if len(lines) > 1 else ""
