from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .models import AIAnalysis, MarketSnapshot, Venue


@dataclass
class AdapterMarket:
    snapshot: MarketSnapshot
    catalysts: list[AIAnalysis]
    theme: str


class MarketAdapter(Protocol):
    venue: Venue

    def fetch_markets(self) -> list[AdapterMarket]:
        """Return normalized market candidates for EDGE evaluation."""


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
                    AIAnalysis(source="official_injury_feed", quality=0.95, direction=0.03, confidence=0.86),
                    AIAnalysis(source="team_reporter", quality=0.7, direction=0.01, confidence=0.65),
                ],
                theme="sports",
            )
        ]


class KalshiAdapter:
    venue = Venue.KALSHI

    def fetch_markets(self) -> list[AdapterMarket]:
        return [
            AdapterMarket(
                snapshot=MarketSnapshot(
                    market_id="kalshi_fed_rate_cut",
                    venue=self.venue,
                    market_prob=0.53,
                    spread_bps=75,
                    depth_usd=17000,
                    volume_24h_usd=210000,
                    time_to_resolution_hours=60,
                    updated_at=datetime.now(timezone.utc),
                    ambiguity_score=0.2,
                ),
                catalysts=[
                    AIAnalysis(source="official_data", quality=0.9, direction=0.03, confidence=0.85),
                ],
                theme="macro",
            )
        ]


class PolymarketAdapter:
    venue = Venue.POLYMARKET

    def fetch_markets(self) -> list[AdapterMarket]:
        return [
            AdapterMarket(
                snapshot=MarketSnapshot(
                    market_id="poly_election_candidate_y",
                    venue=self.venue,
                    market_prob=0.58,
                    spread_bps=220,
                    depth_usd=1000,
                    volume_24h_usd=15000,
                    time_to_resolution_hours=36,
                    updated_at=datetime.now(timezone.utc),
                    volatility_entropy_score=0.82,
                ),
                catalysts=[
                    AIAnalysis(source="news", quality=0.7, direction=-0.02, confidence=0.6),
                ],
                theme="politics",
            )
        ]
