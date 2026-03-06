from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .models import Catalyst, MarketSnapshot, Venue

logger = logging.getLogger(__name__)

# Import from hyphenated package via importlib
_ingestion = importlib.import_module(".dat-ingestion", "edge_agent")
_poly_api = importlib.import_module(".dat-ingestion.polymarket_api", "edge_agent")
_kalshi_api = importlib.import_module(".dat-ingestion.kalshi_api", "edge_agent")


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
# Jupiter — no public REST API yet; keep mock data
# ---------------------------------------------------------------------------

class JupiterAdapter:
    venue = Venue.JUPITER_PREDICTION

    def fetch_markets(self) -> list[AdapterMarket]:
        return [
            AdapterMarket(
                snapshot=MarketSnapshot(
                    market_id="jup_nfl_team_a_playoffs",
                    venue=self.venue,
                    market_prob=0.44,
                    spread_bps=110,
                    depth_usd=9200,
                    volume_24h_usd=132000,
                    time_to_resolution_hours=96,
                    updated_at=datetime.now(timezone.utc),
                ),
                catalysts=[
                    Catalyst(source="official_injury_feed", quality=0.95, direction=0.03, confidence=0.86),
                    Catalyst(source="team_reporter", quality=0.7, direction=0.01, confidence=0.65),
                ],
                theme="sports",
            )
        ]


# ---------------------------------------------------------------------------
# Kalshi — live data requires a free Kalshi API key (KALSHI_API_KEY in .env).
# Falls back to realistic mock markets for testing without a key.
# Sign up free at: https://kalshi.com  → API Settings → Create key
# ---------------------------------------------------------------------------

def _kalshi_mock_markets() -> list[AdapterMarket]:
    now = datetime.now(timezone.utc)
    return [
        AdapterMarket(
            snapshot=MarketSnapshot(
                market_id="KXFED-25MAR-T425",
                venue=Venue.KALSHI,
                market_prob=0.53,
                spread_bps=75,
                depth_usd=17000,
                volume_24h_usd=210000,
                time_to_resolution_hours=60,
                updated_at=now,
                ambiguity_score=0.2,
            ),
            catalysts=[Catalyst(source="official_data", quality=0.9, direction=0.03, confidence=0.85)],
            theme="macro",
        ),
        AdapterMarket(
            snapshot=MarketSnapshot(
                market_id="KXINFL-25MAR-T30",
                venue=Venue.KALSHI,
                market_prob=0.41,
                spread_bps=90,
                depth_usd=12000,
                volume_24h_usd=95000,
                time_to_resolution_hours=120,
                updated_at=now,
                ambiguity_score=0.3,
            ),
            catalysts=[Catalyst(source="cpi_data", quality=0.85, direction=-0.05, confidence=0.78)],
            theme="macro",
        ),
        AdapterMarket(
            snapshot=MarketSnapshot(
                market_id="KXBTC-25MAR-B80K",
                venue=Venue.KALSHI,
                market_prob=0.62,
                spread_bps=120,
                depth_usd=8500,
                volume_24h_usd=148000,
                time_to_resolution_hours=48,
                updated_at=now,
            ),
            catalysts=[Catalyst(source="crypto_feed", quality=0.7, direction=0.08, confidence=0.65)],
            theme="crypto",
        ),
        AdapterMarket(
            snapshot=MarketSnapshot(
                market_id="KXPRES-26NOV-REP",
                venue=Venue.KALSHI,
                market_prob=0.57,
                spread_bps=100,
                depth_usd=22000,
                volume_24h_usd=310000,
                time_to_resolution_hours=6500,
                updated_at=now,
                ambiguity_score=0.25,
            ),
            catalysts=[Catalyst(source="polling_aggregate", quality=0.8, direction=0.04, confidence=0.72)],
            theme="politics",
        ),
        AdapterMarket(
            snapshot=MarketSnapshot(
                market_id="KXGDP-25Q1-ABOVE2",
                venue=Venue.KALSHI,
                market_prob=0.35,
                spread_bps=85,
                depth_usd=9800,
                volume_24h_usd=72000,
                time_to_resolution_hours=720,
                updated_at=now,
                ambiguity_score=0.35,
            ),
            catalysts=[Catalyst(source="nowcast_model", quality=0.75, direction=-0.06, confidence=0.68)],
            theme="macro",
        ),
    ]


class KalshiAdapter:
    venue = Venue.KALSHI

    def fetch_markets(self) -> list[AdapterMarket]:
        try:
            markets = _kalshi_api.get_markets(limit=30, min_volume=1)
            result = []
            for m in markets:
                prob = _kalshi_api.parse_market_prob(m)
                spread = _kalshi_api.parse_spread_bps(m)
                volume = _kalshi_api.parse_volume(m)
                liquidity = _kalshi_api.parse_liquidity(m)

                result.append(
                    AdapterMarket(
                        snapshot=MarketSnapshot(
                            market_id=m.get("ticker", "kalshi_unknown"),
                            venue=self.venue,
                            market_prob=prob,
                            spread_bps=spread,
                            depth_usd=liquidity,
                            volume_24h_usd=volume,
                            time_to_resolution_hours=self._hours_to_close(m),
                            updated_at=datetime.now(timezone.utc),
                        ),
                        catalysts=[],
                        theme=self._infer_theme(m),
                    )
                )
            if result:
                logger.info("Kalshi: fetched %d live markets", len(result))
                return result
        except Exception as exc:
            logger.warning("Kalshi live fetch failed (%s) — using fallback mock", exc)

        return _kalshi_mock_markets()

    @staticmethod
    def _hours_to_close(market: dict) -> float:
        close_time = market.get("close_time")
        if close_time:
            try:
                from datetime import datetime, timezone
                import dateutil.parser
                dt = dateutil.parser.parse(close_time)
                delta = dt - datetime.now(timezone.utc)
                return max(delta.total_seconds() / 3600, 0.1)
            except Exception:
                pass
        return 48.0

    @staticmethod
    def _infer_theme(market: dict) -> str:
        title = (market.get("title") or "").lower()
        if any(w in title for w in ("fed", "rate", "cpi", "gdp", "inflation")):
            return "macro"
        if any(w in title for w in ("election", "president", "senate", "house", "vote")):
            return "politics"
        if any(w in title for w in ("nfl", "nba", "mlb", "nhl", "soccer", "sport")):
            return "sports"
        return "general"


# ---------------------------------------------------------------------------
# Polymarket — live data, no auth needed
# ---------------------------------------------------------------------------

class PolymarketAdapter:
    venue = Venue.POLYMARKET
    _FALLBACK = [
        AdapterMarket(
            snapshot=MarketSnapshot(
                market_id="poly_election_candidate_y",
                venue=Venue.POLYMARKET,
                market_prob=0.58,
                spread_bps=220,
                depth_usd=1000,
                volume_24h_usd=15000,
                time_to_resolution_hours=36,
                updated_at=datetime.now(timezone.utc),
                volatility_entropy_score=0.82,
            ),
            catalysts=[
                Catalyst(source="news", quality=0.7, direction=-0.02, confidence=0.6),
            ],
            theme="politics",
        )
    ]

    def fetch_markets(self) -> list[AdapterMarket]:
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
                        ),
                        catalysts=[],
                        theme=self._infer_theme(m),
                    )
                )
            if result:
                logger.info("Polymarket: fetched %d live markets", len(result))
                return result
        except Exception as exc:
            logger.warning("Polymarket live fetch failed (%s) — using fallback mock", exc)

        return self._FALLBACK

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
