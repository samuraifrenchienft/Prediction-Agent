from __future__ import annotations

from .adapters import AdapterMarket, MarketAdapter
from .catalyst_engine import CatalystDetectionEngine


class EdgeScanner:
    """Builds normalized scan inputs from one or more market adapters.

    Pass a BrandDNA instance to drive the catalyst query from Strategy DNA
    and enable domain-specific relevance filtering. Without one, the scanner
    falls back to a generic 'US politics' query with no relevance filtering.
    """

    def __init__(self, adapters: list[MarketAdapter], brand_dna=None) -> None:
        self.adapters = adapters
        self.brand_dna = brand_dna
        strategy_dna = brand_dna.strategy if brand_dna else None
        self.catalyst_engine = CatalystDetectionEngine(strategy_dna=strategy_dna)

    def collect(self) -> list[tuple]:
        collected: list[tuple] = []
        # Query is derived from Strategy DNA when available; falls back to generic query.
        catalysts = self.catalyst_engine.detect_catalysts()

        for adapter in self.adapters:
            markets: list[AdapterMarket] = adapter.fetch_markets()
            for market in markets:
                collected.append((market.snapshot, catalysts, market.theme))
        return collected
