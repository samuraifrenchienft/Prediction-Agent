from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .models import QualificationState, Recommendation


@dataclass
class RecommendationRecord:
    recommendation: Recommendation
    recorded_at: datetime


class RecommendationRepository:
    """In-memory repository for recommendations and rejection analytics."""

    def __init__(self) -> None:
        self._records: list[RecommendationRecord] = []

    def add(self, recommendation: Recommendation) -> None:
        self._records.append(
            RecommendationRecord(
                recommendation=recommendation,
                recorded_at=datetime.now(timezone.utc),
            )
        )

    def list_all(self) -> list[RecommendationRecord]:
        return list(self._records)

    def list_by_state(self, state: QualificationState) -> list[RecommendationRecord]:
        return [record for record in self._records if record.recommendation.qualification_state == state]

    def top_opportunities(self, limit: int = 3) -> list[Recommendation]:
        qualified = [
            record.recommendation
            for record in self._records
            if record.recommendation.qualification_state == QualificationState.QUALIFIED
        ]
        ranked = sorted(qualified, key=lambda rec: rec.ev_net * rec.confidence, reverse=True)
        return ranked[:limit]

    def reject_reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self._records:
            for reason in record.recommendation.reject_reason_codes:
                counts[reason] = counts.get(reason, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))

    def venue_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self._records:
            venue = record.recommendation.venue.value
            counts[venue] = counts.get(venue, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: kv[0]))
