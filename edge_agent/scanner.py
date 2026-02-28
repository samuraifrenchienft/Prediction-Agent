from __future__ import annotations

from .adapters import AdapterMarket, MarketAdapter


class EdgeScanner:
    """Builds normalized scan inputs from one or more market adapters."""

    def __init__(self, adapters: list[MarketAdapter]) -> None:
        self.adapters = adapters

    def collect(self) -> list[tuple]:
        collected: list[tuple] = []
        for adapter in self.adapters:
            markets: list[AdapterMarket] = adapter.fetch_markets()
            for market in markets:
                collected.append((market.snapshot, market.catalysts, market.theme))
        return collected
