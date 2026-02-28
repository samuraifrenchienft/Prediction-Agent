from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .models import QualificationState, Recommendation


@dataclass
class WatchlistEntry:
    market_id: str
    venue: str
    state: QualificationState
    reason_codes: list[str]
    updated_at: datetime


class WatchlistStore:
    """In-memory watchlist for proposal-first pipeline integration."""

    def __init__(self) -> None:
        self._entries: dict[str, WatchlistEntry] = {}

    def update(self, recommendation: Recommendation) -> None:
        key = f"{recommendation.venue.value}:{recommendation.market_id}"
        if recommendation.qualification_state == QualificationState.QUALIFIED:
            self._entries.pop(key, None)
            return

        if recommendation.qualification_state == QualificationState.WATCHLIST:
            self._entries[key] = WatchlistEntry(
                market_id=recommendation.market_id,
                venue=recommendation.venue.value,
                state=recommendation.qualification_state,
                reason_codes=recommendation.reject_reason_codes,
                updated_at=datetime.now(timezone.utc),
            )
            return

        # Rejected markets are removed from active watchlist.
        self._entries.pop(key, None)

    def list_entries(self) -> list[WatchlistEntry]:
        return sorted(self._entries.values(), key=lambda entry: (entry.venue, entry.market_id))
