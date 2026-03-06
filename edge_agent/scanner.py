from __future__ import annotations

from .adapters import AdapterMarket, MarketAdapter
from .catalyst_engine import CatalystDetectionEngine
from .models import Catalyst, MarketSnapshot

# Map themes to relevant news search queries
_THEME_QUERIES: dict[str, str] = {
    "politics": "US politics election government",
    "macro": "Federal Reserve interest rates inflation GDP",
    "sports": "NFL NBA MLB sports championship",
    "crypto": "Bitcoin Ethereum cryptocurrency markets",
    "general": "breaking news markets",
}


class EdgeScanner:
    """Builds normalized scan inputs from one or more market adapters."""

    def __init__(self, adapters: list[MarketAdapter]) -> None:
        self.adapters = adapters
        self.catalyst_engine = CatalystDetectionEngine()
        self._catalyst_cache: dict[str, list[Catalyst]] = {}

    def collect(self) -> list[tuple[MarketSnapshot, list[Catalyst], str]]:
        collected: list[tuple] = []

        for adapter in self.adapters:
            markets: list[AdapterMarket] = adapter.fetch_markets()
            for market in markets:
                theme = market.theme
                # Cache catalysts per theme so we don't hit the news API repeatedly
                if theme not in self._catalyst_cache:
                    query = _THEME_QUERIES.get(theme, _THEME_QUERIES["general"])
                    self._catalyst_cache[theme] = self.catalyst_engine.detect_catalysts(query)

                # Merge adapter-provided catalysts with news-sourced catalysts
                all_catalysts = market.catalysts + self._catalyst_cache[theme]
                collected.append((market.snapshot, all_catalysts, theme))

        return collected
