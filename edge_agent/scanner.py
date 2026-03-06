from __future__ import annotations

from .adapters import AdapterMarket, MarketAdapter


from .catalyst_engine import CatalystDetectionEngine


class EdgeScanner:
    """Builds normalized scan inputs from one or more market adapters."""

    def __init__(self, adapters: list[MarketAdapter]) -> None:
        self.adapters = adapters
        self.catalyst_engine = CatalystDetectionEngine()

    def fetch_markets(self) -> list[AdapterMarket]:
        """Fetches markets from all adapters."""
        all_markets: list[AdapterMarket] = []
        for adapter in self.adapters:
            try:
                all_markets.extend(adapter.fetch_markets())
            except Exception as e:
                print(f"[{adapter.__class__.__name__}] error: {e}")
        return all_markets

    def collect(self, markets: list[AdapterMarket], catalysts: list[AIAnalysis]) -> list[tuple]:
        """Processes a list of markets and catalysts."""
        collected: list[tuple] = []
        for market in markets:
            collected.append((market.snapshot, catalysts, market.theme))
        return collected
