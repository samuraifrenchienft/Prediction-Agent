from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .service import EdgeService


@dataclass
class EdgeDashboard:
    summary: dict[str, Any]
    top_opportunities: list[dict[str, Any]]
    watchlist: list[dict[str, Any]]


class EdgeReporter:
    """Builds UI-ready dashboard payloads from EDGE service state."""

    def __init__(self, service: EdgeService) -> None:
        self.service = service

    def build_dashboard(self, top_n: int = 3) -> EdgeDashboard:
        summary = self.service.get_scan_summary()
        top = [rec.to_dict() for rec in self.service.engine.top_opportunities(limit=top_n)]
        watchlist = self.service.list_watchlist()
        return EdgeDashboard(
            summary={
                "total_markets": summary.total_markets,
                "qualified": summary.qualified,
                "watchlist": summary.watchlist,
                "rejected": summary.rejected,
                "top_market_ids": summary.top_market_ids,
                "reject_reason_counts": summary.reject_reason_counts,
                "venue_counts": self.service.engine.repository.venue_counts(),
            },
            top_opportunities=top,
            watchlist=watchlist,
        )
