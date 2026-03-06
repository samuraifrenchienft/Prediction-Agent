from __future__ import annotations

from .adapters import AdapterMarket, MarketAdapter
from .catalyst_engine import CatalystDetectionEngine
from .models import Catalyst, MarketSnapshot

# Theme-aware news queries
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

    def fetch_markets(self) -> list[AdapterMarket]:
        """Fetches markets from all adapters."""
        all_markets: list[AdapterMarket] = []
        for adapter in self.adapters:
            try:
                all_markets.extend(adapter.fetch_markets())
            except Exception as e:
                print(f"[{adapter.__class__.__name__}] error: {e}")
        return all_markets

    def collect(self, markets: list[AdapterMarket] | None = None, catalysts: list[Catalyst] | None = None) -> list[tuple]:
        """
        Processes markets into scan inputs.
        If markets not provided, fetches from all adapters.
        If catalysts not provided, fetches theme-aware news catalysts per market.
        """
        if markets is None:
            markets = self.fetch_markets()

        collected: list[tuple] = []
        for market in markets:
            if catalysts is not None:
                all_catalysts = market.catalysts + catalysts
            else:
                # Theme-aware catalyst fetch with caching
                theme = market.theme
                if theme not in self._catalyst_cache:
                    query = _THEME_QUERIES.get(theme, _THEME_QUERIES["general"])
                    self._catalyst_cache[theme] = self.catalyst_engine.detect_catalysts(query)
                all_catalysts = market.catalysts + self._catalyst_cache[theme]

            collected.append((market.snapshot, all_catalysts, market.theme))

        return collected
