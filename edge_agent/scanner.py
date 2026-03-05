from __future__ import annotations

from .adapters import AdapterMarket, MarketAdapter
from .catalyst_engine import CatalystDetectionEngine


# Markets outside this probability range are already fully priced —
# no edge possible, skip expensive news fetch.
_MIN_PROB_FOR_NEWS = 0.08
_MAX_PROB_FOR_NEWS = 0.92

# Markets below this 24h volume get news anyway (small markets can still
# have lag), but we reduce page_size to save API quota.
_LOW_VOLUME_THRESHOLD = 5_000


class EdgeScanner:
    """Builds normalized scan inputs from one or more market adapters."""

    def __init__(self, adapters: list[MarketAdapter]) -> None:
        self.adapters = adapters
        self.catalyst_engine = CatalystDetectionEngine()

    def collect(self) -> list[tuple]:
        collected: list[tuple] = []
        for adapter in self.adapters:
            markets: list[AdapterMarket] = adapter.fetch_markets()
            for market in markets:
                prob = market.snapshot.market_prob

                # Skip news for markets that are already priced to near-certainty.
                # These have no exploitable edge and waste NewsAPI quota.
                if not (_MIN_PROB_FOR_NEWS <= prob <= _MAX_PROB_FOR_NEWS):
                    collected.append((market.snapshot, [], market.theme))
                    continue

                query = self._build_query(market)
                # Reduce page_size for low-volume markets to conserve API quota
                page_size = 3 if market.snapshot.volume_24h_usd < _LOW_VOLUME_THRESHOLD else 5
                catalysts = self.catalyst_engine.detect_catalysts(query, page_size=page_size)
                collected.append((market.snapshot, catalysts, market.theme))

        return collected

    def _build_query(self, market: AdapterMarket) -> str:
        """Build a focused news search query from the market's human-readable title."""
        title = market.title.strip()
        if title:
            # Strip common prediction market boilerplate to sharpen the query
            for strip in ("Will ", "will ", "?", " by end of", " in 2025", " in 2026"):
                title = title.replace(strip, "")
            return title.strip()[:120]  # NewsAPI query max ~120 chars for best results
        # Fallback: use theme
        return market.theme if market.theme != "other" else market.snapshot.market_id
