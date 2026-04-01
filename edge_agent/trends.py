"""
Google Trends context — attention spike detection.
===================================================
Uses pytrends (unofficial Google Trends API, no key required).

Returns a [Trending] context block when search volume for a topic
spikes > 2x its 7-day average — a signal that public attention is
outpacing current market prices.

Rate limiting: 1-hour cache per query + 15s between live calls.
"""
from __future__ import annotations

import logging
import time
from threading import Lock

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache — keyed by normalized query string
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[str, float]] = {}   # query → (result, expires_at)
_CACHE_TTL = 3600   # 1 hour
_last_call_at: float = 0.0
_MIN_INTERVAL = 15.0   # seconds between live API calls (pytrends rate limit)
_lock = Lock()


def get_trends_context(query: str) -> str:
    """
    Check Google Trends for `query` over the past 7 days.
    Returns a [Trending] block string if search volume spiked 2x+ above
    the prior 6-day average in the last 24h. Returns "" if flat or on error.

    Thread-safe with a 1-hour cache and 15s minimum interval between calls.
    """
    global _last_call_at

    key = query.strip().lower()[:80]
    now = time.time()

    # Serve from cache if fresh
    cached = _cache.get(key)
    if cached:
        result, expires = cached
        if now < expires:
            log.debug("[trends] Cache hit for '%s'", key)
            return result

    with _lock:
        # Re-check cache inside lock
        cached = _cache.get(key)
        if cached:
            result, expires = cached
            if now < expires:
                return result

        # Rate limit — don't hammer pytrends
        elapsed = now - _last_call_at
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)

        result = _fetch_trends(query)
        _last_call_at = time.time()
        _cache[key] = (result, time.time() + _CACHE_TTL)
        return result


def _fetch_trends(query: str) -> str:
    """
    Live pytrends fetch. Returns formatted block or "".
    """
    try:
        from pytrends.request import TrendReq

        pt = TrendReq(hl="en-US", tz=0, timeout=(10, 25))
        pt.build_payload([query], timeframe="now 7-d")
        df = pt.interest_over_time()

        if df.empty or query not in df.columns:
            return ""

        series = df[query]
        if len(series) < 2:
            return ""

        # Split into "last 24h" vs "prior 6 days"
        last_24h = series.iloc[-8:]   # ~8 data points per day (hourly-ish)
        prior = series.iloc[:-8]

        if prior.empty or prior.mean() == 0:
            return ""

        last_avg = last_24h.mean()
        prior_avg = prior.mean()
        ratio = last_avg / prior_avg if prior_avg > 0 else 0

        if ratio < 1.5:
            return ""   # not a meaningful spike

        peak = int(series.max())
        current = int(last_24h.iloc[-1])

        if ratio >= 3.0:
            level = "MAJOR spike"
            signal = "strong public attention surge — odds may lag"
        elif ratio >= 2.0:
            level = "Notable spike"
            signal = "above-average interest — watch for price movement"
        else:
            level = "Mild uptick"
            signal = "slightly elevated search interest"

        return (
            f"\n\n[Google Trends — {query}]\n"
            f"  Status: {level} ({ratio:.1f}x above prior 6-day avg)\n"
            f"  Current interest index: {current}/100  |  Peak this week: {peak}/100\n"
            f"  Signal: {signal}\n"
            "[End Trends]"
        )

    except Exception as exc:
        log.debug("[trends] Fetch failed for '%s': %s", query, exc)
        return ""
