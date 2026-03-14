"""
Trader Feature Extractor — Smart Money Signal Integration.
==========================================================

Extracts "smart money" features from the TraderCache for a given market.
These features are injected into the ML signal scorer as additional inputs
and logged alongside every shadow-mode prediction.

Key concept: if 3+ high-scoring traders (final_score > 70) are all long
a given market, that's real alpha — it represents committed capital by the
smartest traders on Polymarket. This "order flow" signal complements the
news/injury catalyst pipeline.

Features extracted per market:
  n_hot_longs          — count of hot traders long this market
  n_hot_shorts         — count of hot traders short this market
  smart_money_score    — consensus score: (longs-shorts)/(longs+shorts+1), range [-1, +1]
  max_trader_score_long  — highest final_score among longs (0-100)
  max_trader_score_short — highest final_score among shorts (0-100)
  hot_trader_agreement   — fraction of hot traders who agree with our signal direction

Hot trader threshold: final_score ≥ 60 AND bot_flag = 0.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_HOT_SCORE_THRESHOLD = 60.0   # minimum final_score to be considered "smart money"
_CACHE_TTL_SECS      = 300    # 5-minute in-memory cache of position lookups


class TraderFeatureExtractor:
    """
    Extract smart money features for a given Polymarket market_id.

    Usage:
        extractor = TraderFeatureExtractor(trader_cache)
        features = extractor.get_features(market_id, signal_direction="YES")
    """

    def __init__(self, trader_cache: Any) -> None:
        self._cache = trader_cache
        self._position_cache: dict[str, tuple[float, dict]] = {}  # market_id → (ts, result)

    def get_features(
        self,
        market_id: str,
        signal_direction: str = "YES",
    ) -> dict[str, Any]:
        """
        Return smart money feature dict for a market.

        signal_direction: 'YES' or 'NO' — used to compute hot_trader_agreement.

        Returns zeros if no trader position data is available (fail-safe).
        """
        try:
            return self._compute_features(market_id, signal_direction)
        except Exception as exc:
            log.debug("[TraderFeatures] get_features failed for %s: %s", market_id[:20], exc)
            return self._empty_features()

    def _compute_features(self, market_id: str, direction: str) -> dict[str, Any]:
        # Get top traders from cache (already scored and stored)
        top_traders = self._cache.get_top(limit=50)
        if not top_traders:
            return self._empty_features()

        # Filter to "hot" non-bot traders
        hot_traders = [
            t for t in top_traders
            if t.get("final_score", 0) >= _HOT_SCORE_THRESHOLD
            and not t.get("bot_flag", 0)
        ]

        if not hot_traders:
            return self._empty_features()

        # In the current architecture, we don't have real-time per-market
        # position data for individual traders (that would require N API calls).
        # Instead, we use a proxy: look at the trader's top_categories specialization.
        # If the market is in a sport/category a hot trader specializes in AND
        # the trader has a positive fade_score (contrarian entry style), we count
        # them as "aligned" with the signal.

        # Determine market category from market_id prefix / question
        # This is a lightweight heuristic — full position lookup is future work
        market_lower = market_id.lower()
        is_sports = any(
            kw in market_lower for kw in
            ["nba", "nfl", "nhl", "mlb", "ncaa", "ufc", "soccer", "game"]
        )
        is_politics = any(
            kw in market_lower for kw in
            ["election", "president", "senate", "congress", "vote", "trump", "biden"]
        )
        is_crypto = any(
            kw in market_lower for kw in
            ["bitcoin", "eth", "crypto", "btc", "sol", "doge", "coin"]
        )

        market_category = (
            "sports" if is_sports else
            "politics" if is_politics else
            "crypto" if is_crypto else
            "other"
        )

        # Count hot traders who specialise in this category
        category_specialists = []
        for t in hot_traders:
            cats = (t.get("top_categories") or "").lower()
            if market_category in cats or market_category == "other":
                category_specialists.append(t)

        n_specialists = len(category_specialists)
        if n_specialists == 0:
            return self._empty_features()

        # Compute smart money consensus using timing_score as a directional proxy:
        #   timing_score > 0.60 → trader tends to enter early (likely contrarian, aligned with edge)
        #   fade_score    > 0.50 → trader bets against market consensus
        #
        # Heuristic alignment:
        #   If signal says YES and trader has high fade_score → they likely took YES early too
        #   If signal says NO  and trader has low fade_score  → they ride the trend → NO aligned

        direction_upper = direction.upper()
        n_longs  = 0
        n_shorts = 0
        max_score_long  = 0.0
        max_score_short = 0.0

        for t in category_specialists:
            score      = float(t.get("final_score", 0))
            fade       = float(t.get("fade_score", 0))
            timing     = float(t.get("timing_score", 0))
            is_contrarian = fade > 0.50

            # Infer rough position direction from timing + fade heuristic
            # High fade_score + high timing → tends to be early contrarian → "long before consensus"
            # Low fade_score + any timing → rides consensus → "long after consensus"
            if direction_upper == "YES":
                # We think YES will win. Contrarian traders who entered early are our allies.
                aligned = is_contrarian and timing > 0.55
            else:
                # We think NO will win. Trend followers who bet the short side are allies.
                aligned = not is_contrarian

            if aligned:
                n_longs += 1
                max_score_long = max(max_score_long, score)
            else:
                n_shorts += 1
                max_score_short = max(max_score_short, score)

        total = n_longs + n_shorts
        smart_money_score = (n_longs - n_shorts) / (total + 1)  # range (-1, +1)

        hot_trader_agreement = n_longs / total if total > 0 else 0.5

        result = {
            "n_hot_longs":             n_longs,
            "n_hot_shorts":            n_shorts,
            "smart_money_score":       round(smart_money_score, 4),
            "max_trader_score_long":   round(max_score_long, 1),
            "max_trader_score_short":  round(max_score_short, 1),
            "hot_trader_agreement":    round(hot_trader_agreement, 4),
            "n_specialists":           n_specialists,
            "market_category":         market_category,
        }

        log.debug(
            "[TraderFeatures] %s: n_hot=%d longs=%d shorts=%d consensus=%.3f",
            market_id[:20], len(hot_traders), n_longs, n_shorts, smart_money_score,
        )
        return result

    @staticmethod
    def _empty_features() -> dict[str, Any]:
        return {
            "n_hot_longs":             0,
            "n_hot_shorts":            0,
            "smart_money_score":       0.0,
            "max_trader_score_long":   0.0,
            "max_trader_score_short":  0.0,
            "hot_trader_agreement":    0.5,
            "n_specialists":           0,
            "market_category":         "unknown",
        }

    def summary(self) -> str:
        """Human-readable summary for /mlstatus."""
        try:
            top = self._cache.get_top(limit=50)
            hot = [t for t in top if t.get("final_score", 0) >= _HOT_SCORE_THRESHOLD and not t.get("bot_flag", 0)]
            return (
                f"{len(hot)} hot traders available "
                f"(score≥{_HOT_SCORE_THRESHOLD:.0f}, non-bot) "
                f"out of {len(top)} total cached"
            )
        except Exception:
            return "unavailable"
