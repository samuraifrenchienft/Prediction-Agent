from __future__ import annotations

from dataclasses import dataclass

from .engine import EdgeEngine
from .game_tracker import TrackedGame
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

    def list_tracked_games(self) -> list[dict]:
        """Return all games being monitored by the GameTracker (injury tracking list)."""
        return [
            {
                "market_id": g.market_id,
                "venue": g.venue.value,
                "question": g.question,
                "theme": g.theme,
                "phase": g.phase.value,
                "pre_game_prob": round(g.reference_prob, 4),
                "current_prob": round(g.last_market_prob, 4),
                "price_drop": round(g.current_drop, 4),
                "injury_catalysts": g.injury_catalysts,
                "triggered": g.triggered,
                "trigger_phase": g.trigger_phase.value if g.trigger_phase else None,
                "trigger_prob": round(g.trigger_prob, 4) if g.triggered else None,
                "registered_at": g.registered_at.isoformat(),
                "last_updated": g.last_updated.isoformat(),
            }
            for g in self.engine.game_tracker.active_games()
        ]

    def game_tracker_summary(self) -> str:
        """Human-readable summary of the GameTracker state."""
        return self.engine.game_tracker.summary()
