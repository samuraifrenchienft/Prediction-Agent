from __future__ import annotations

import importlib
import time

from .adapters import AdapterMarket, MarketAdapter
from .catalyst_engine import CatalystDetectionEngine
from .models import Catalyst, MarketSnapshot

# Lazy import — avoids hard dependency on requests at import time
_injury_mod = importlib.import_module(".dat-ingestion.injury_api", "edge_agent")
InjuryAPIClient = _injury_mod.InjuryAPIClient
detect_sport = _injury_mod.detect_sport

_CATALYST_CACHE_TTL = 7200  # 2 hours — refresh news catalysts every 2 hours

# Theme-aware news queries
_THEME_QUERIES: dict[str, str] = {
    "politics": "US politics election government",
    "macro":    "Federal Reserve interest rates inflation GDP",
    "sports":   "NFL NBA MLB sports championship",
    "crypto":   "Bitcoin Ethereum cryptocurrency markets",
    "general":  "breaking news markets",
}


class EdgeScanner:
    """Builds normalized scan inputs from one or more market adapters."""

    def __init__(self, adapters: list[MarketAdapter]) -> None:
        self.adapters = adapters
        self.catalyst_engine = CatalystDetectionEngine()
        self.injury_client = InjuryAPIClient()
        self._catalyst_cache: dict[str, list[Catalyst]] = {}
        self._catalyst_cache_at: dict[str, float] = {}  # theme → timestamp

    def fetch_markets(self) -> list[AdapterMarket]:
        """Fetches markets from all adapters."""
        all_markets: list[AdapterMarket] = []
        for adapter in self.adapters:
            try:
                all_markets.extend(adapter.fetch_markets())
            except Exception as e:
                print(f"[{adapter.__class__.__name__}] error: {e}")
        return all_markets

    def collect(
        self,
        markets: list[AdapterMarket] | None = None,
        catalysts: list[Catalyst] | None = None,
    ) -> list[tuple]:
        """
        Processes markets into scan inputs.
        If markets not provided, fetches from all adapters.
        If catalysts not provided, fetches theme-aware news catalysts + injury
        catalysts per market. Sports markets get both news AND injury data.
        """
        if markets is None:
            markets = self.fetch_markets()

        collected: list[tuple] = []
        for market in markets:
            if catalysts is not None:
                all_catalysts = market.catalysts + catalysts
            else:
                # ── News catalysts (theme-aware, 2-hour TTL) ──────────────
                theme = market.theme
                now = time.time()
                cache_stale = (
                    theme not in self._catalyst_cache
                    or now - self._catalyst_cache_at.get(theme, 0) > _CATALYST_CACHE_TTL
                )
                if cache_stale:
                    query = _THEME_QUERIES.get(theme, _THEME_QUERIES["general"])
                    self._catalyst_cache[theme] = self.catalyst_engine.detect_catalysts(query)
                    self._catalyst_cache_at[theme] = now

                all_catalysts = list(market.catalysts) + self._catalyst_cache[theme]

                # ── Injury catalysts (sports markets only, 30-min TTL via client cache) ──
                if theme == "sports":
                    question = getattr(market.snapshot, "question", "") or market.snapshot.market_id
                    sport = detect_sport(question)
                    try:
                        injury_dicts = self.injury_client.build_injury_catalysts(question, sport)
                        for ic in injury_dicts:
                            all_catalysts.append(Catalyst(
                                source=ic["source"],
                                direction=ic["direction"],
                                confidence=ic["confidence"],
                                quality=ic["quality"],
                            ))
                    except Exception as e:
                        print(f"[InjuryAPI] skipped for {market.snapshot.market_id}: {e}")

            collected.append((market.snapshot, all_catalysts, market.theme))

        return collected
