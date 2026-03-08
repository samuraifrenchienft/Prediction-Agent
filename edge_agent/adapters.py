from __future__ import annotations

import importlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .models import Catalyst, MarketSnapshot, Venue

logger = logging.getLogger(__name__)

# Import from hyphenated package via importlib
_ingestion = importlib.import_module(".dat-ingestion", "edge_agent")
_poly_api = importlib.import_module(".dat-ingestion.polymarket_api", "edge_agent")
_kalshi_api = importlib.import_module(".dat-ingestion.kalshi_api", "edge_agent")

# TTL cache: don't re-hit APIs within this window (seconds)
_CACHE_TTL = 300  # 5 minutes


@dataclass
class AdapterMarket:
    snapshot: MarketSnapshot
    catalysts: list[Catalyst]
    theme: str


class MarketAdapter(Protocol):
    venue: Venue

    def fetch_markets(self) -> list[AdapterMarket]:
        """Return normalized market candidates for EDGE evaluation."""


# ---------------------------------------------------------------------------
# Kalshi — live RSA-signed requests. Returns empty list if auth fails.
# ---------------------------------------------------------------------------

class KalshiAdapter:
    venue = Venue.KALSHI
    _cache: list[AdapterMarket] = []
    _cache_at: float = 0.0

    def fetch_markets(self) -> list[AdapterMarket]:
        if self._cache and (time.time() - self._cache_at) < _CACHE_TTL:
            logger.debug("Kalshi: returning cached %d markets", len(self._cache))
            return self._cache

        try:
            markets = _kalshi_api.get_markets(limit=30, min_volume=1)
            result = []
            for m in markets:
                prob = _kalshi_api.parse_market_prob(m)
                spread = _kalshi_api.parse_spread_bps(m)
                volume = _kalshi_api.parse_volume(m)
                liquidity = _kalshi_api.parse_liquidity(m)

                # Kalshi is market-maker driven — the `liquidity` field is
                # often 0 even for active markets. Use 20% of 24h dollar
                # volume as a conservative depth proxy when liquidity=0.
                effective_depth = liquidity if liquidity > 0 else volume * 0.20

                result.append(
                    AdapterMarket(
                        snapshot=MarketSnapshot(
                            market_id=m.get("ticker", "kalshi_unknown"),
                            venue=self.venue,
                            market_prob=prob,
                            spread_bps=spread,
                            depth_usd=effective_depth,
                            volume_24h_usd=volume,
                            time_to_resolution_hours=self._hours_to_close(m),
                            updated_at=datetime.now(timezone.utc),
                            question=m.get("title") or m.get("subtitle") or m.get("ticker"),
                        ),
                        catalysts=[],
                        theme=self._infer_theme(m),
                    )
                )
            KalshiAdapter._cache = result
            KalshiAdapter._cache_at = time.time()
            logger.info("Kalshi: fetched %d live markets", len(result))
            return result
        except Exception as exc:
            logger.error("Kalshi live fetch failed: %s", exc)
            print(f"[Kalshi] Live fetch failed: {exc}")
            return []

    @staticmethod
    def _hours_to_close(market: dict) -> float:
        close_time = market.get("close_time")
        if close_time:
            try:
                import dateutil.parser
                dt = dateutil.parser.parse(close_time)
                delta = dt - datetime.now(timezone.utc)
                return max(delta.total_seconds() / 3600, 0.1)
            except Exception:
                pass
        return 48.0

    @staticmethod
    def _infer_theme(market: dict) -> str:
        title = (market.get("title") or market.get("ticker") or "").lower()
        if any(w in title for w in ("fed", "rate", "cpi", "gdp", "inflation")):
            return "macro"
        if any(w in title for w in ("election", "president", "senate", "house", "vote")):
            return "politics"
        if any(w in title for w in ("nfl", "nba", "mlb", "nhl", "soccer", "sport")):
            return "sports"
        if any(w in title for w in ("btc", "eth", "crypto", "bitcoin")):
            return "crypto"
        return "general"


# ---------------------------------------------------------------------------
# Polymarket — live public API, no auth needed.
# ---------------------------------------------------------------------------

class PolymarketAdapter:
    venue = Venue.POLYMARKET
    _cache: list[AdapterMarket] = []
    _cache_at: float = 0.0

    def fetch_markets(self) -> list[AdapterMarket]:
        if self._cache and (time.time() - self._cache_at) < _CACHE_TTL:
            logger.debug("Polymarket: returning cached %d markets", len(self._cache))
            return self._cache

        try:
            markets = _poly_api.get_active_markets(limit=15)
            result = []
            for m in markets:
                prob = _poly_api.parse_market_prob(m)
                spread = _poly_api.parse_spread_bps(m)

                result.append(
                    AdapterMarket(
                        snapshot=MarketSnapshot(
                            market_id=m.get("conditionId", "poly_unknown"),
                            venue=self.venue,
                            market_prob=prob,
                            spread_bps=spread,
                            depth_usd=_poly_api.parse_liquidity(m),
                            volume_24h_usd=_poly_api.parse_volume_24h(m),
                            time_to_resolution_hours=self._hours_to_end(m),
                            updated_at=datetime.now(timezone.utc),
                            question=m.get("question") or m.get("groupItemTitle"),
                        ),
                        catalysts=[],
                        theme=self._infer_theme(m),
                    )
                )
            PolymarketAdapter._cache = result
            PolymarketAdapter._cache_at = time.time()
            logger.info("Polymarket: fetched %d live markets", len(result))
            return result
        except Exception as exc:
            logger.error("Polymarket live fetch failed: %s", exc)
            print(f"[Polymarket] Live fetch failed: {exc}")
            return []

    @staticmethod
    def _hours_to_end(market: dict) -> float:
        end_date = market.get("endDate") or market.get("end_date_iso")
        if end_date:
            try:
                import dateutil.parser
                dt = dateutil.parser.parse(end_date)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                delta = dt - datetime.now(timezone.utc)
                return max(delta.total_seconds() / 3600, 0.1)
            except Exception:
                pass
        return 72.0

    @staticmethod
    def _infer_theme(market: dict) -> str:
        question = (market.get("question") or market.get("groupItemTitle") or "").lower()
        if any(w in question for w in ("election", "president", "senate", "congress", "vote", "biden", "trump")):
            return "politics"
        if any(w in question for w in ("fed", "rate", "cpi", "gdp", "inflation", "recession")):
            return "macro"
        if any(w in question for w in ("nfl", "nba", "mlb", "nhl", "soccer", "super bowl", "champion")):
            return "sports"
        if any(w in question for w in ("bitcoin", "eth", "crypto", "btc", "sol")):
            return "crypto"
        return "general"
