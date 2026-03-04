from __future__ import annotations

from .adapters import AdapterMarket, MarketAdapter


from .catalyst_engine import CatalystDetectionEngine


class EdgeScanner:
    """Builds normalized scan inputs from one or more market adapters."""

    def __init__(self, adapters: list[MarketAdapter]) -> None:
        self.adapters = adapters
        self.catalyst_engine = CatalystDetectionEngine()

    def collect(self) -> list[tuple]:
        collected: list[tuple] = []
        # For now, we'll use a generic query to get catalysts.
        # In the future, this could be tailored to each market.
        catalysts = self.catalyst_engine.detect_catalysts("US politics")

        for adapter in self.adapters:
            markets: list[AdapterMarket] = adapter.fetch_markets()
            for market in markets:
                collected.append((market.snapshot, catalysts, market.theme))
        return collected
