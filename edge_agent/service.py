from __future__ import annotations

from dataclasses import dataclass

from .engine import EdgeEngine
from .models import Catalyst, MarketSnapshot, PortfolioState, QualificationState, Recommendation


@dataclass
class ScanSummary:
    total_markets: int
    qualified: int
    watchlist: int
    rejected: int
    top_market_ids: list[str]
    reject_reason_counts: dict[str, int]
    venue_counts: dict[str, int]


class EdgeService:
    """Convenience service for backend/API integration around EdgeEngine."""

    def __init__(self, engine: EdgeEngine | None = None) -> None:
        self.engine = engine or EdgeEngine()

    def run_scan(
        self,
        inputs: list[tuple[MarketSnapshot, list[Catalyst], str]],
        portfolio: PortfolioState,
    ) -> tuple[list[Recommendation], ScanSummary]:
        recommendations = self.engine.evaluate_batch(inputs=inputs, portfolio=portfolio)
        summary = self.get_scan_summary()
        return recommendations, summary

    def get_scan_summary(self) -> ScanSummary:
        all_records = self.engine.repository.list_all()
        qualified = self.engine.repository.list_by_state(QualificationState.QUALIFIED)
        watchlist = self.engine.repository.list_by_state(QualificationState.WATCHLIST)
        rejected = self.engine.repository.list_by_state(QualificationState.REJECTED)
        top_ids = [rec.market_id for rec in self.engine.top_opportunities(limit=3)]

        return ScanSummary(
            total_markets=len(all_records),
            qualified=len(qualified),
            watchlist=len(watchlist),
            rejected=len(rejected),
            top_market_ids=top_ids,
            reject_reason_counts=self.engine.repository.reject_reason_counts(),
            venue_counts=self.engine.repository.venue_counts(),
        )

    def game_tracker_summary(self) -> str:
        """Delegated to engine — used by run_edge_bot.py for /status display."""
        return self.engine.game_tracker_summary()

    def list_watchlist(self) -> list[dict[str, str | list[str]]]:
        return [
            {
                "market_id": entry.market_id,
                "venue": entry.venue,
                "state": entry.state.value,
                "reason_codes": entry.reason_codes,
                "updated_at": entry.updated_at.isoformat(),
            }
            for entry in self.engine.watchlist.list_entries()
        ]
