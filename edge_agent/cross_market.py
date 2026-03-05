from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Catalyst, MarketSnapshot


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# A market must have at least this much volume to be a "primary" reference
_PRIMARY_VOLUME_THRESHOLD = 10_000

# Minimum probability discrepancy (pp) to flag a cross-market correlation lag
_DISCREPANCY_THRESHOLD = 0.05

# Synthetic catalyst injected into lagging secondary markets
_SYNTHETIC_DIRECTION = 0.65     # directional confidence toward closing the gap
_SYNTHETIC_QUALITY   = 0.75     # source quality (high — derived from market data, not news)
_SYNTHETIC_CONFIDENCE = 0.70    # confidence the correlation is real

# Minimum character length for extracted entity keywords (filters out "a", "in", etc.)
_MIN_KEYWORD_LEN = 4

# Stop-words to exclude from entity extraction
_STOP_WORDS = {
    "will", "this", "that", "they", "them", "from", "with", "have", "been",
    "more", "than", "each", "into", "some", "when", "what", "over", "also",
    "most", "their", "which", "there", "these", "those", "then", "than",
    "next", "last", "first", "second", "third", "does", "make", "take",
    "year", "2024", "2025", "2026", "season", "game", "match", "week",
    "round", "title", "series", "event", "would", "should", "could",
    "before", "after", "during", "between", "against", "reach", "place",
    "finish", "score", "points", "wins", "loses", "beat", "advance",
}


# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------

def _extract_entities(question: str) -> set[str]:
    """Extract significant keyword tokens from a market question.

    Returns capitalized words and compound proper nouns (e.g., "Lakers",
    "Federal Reserve", "Donald") that can be used to group related markets.
    """
    # Normalize
    text = question.strip()

    # Extract all capitalized sequences (likely proper nouns / team names)
    capitalized = re.findall(r"\b[A-Z][a-zA-Z]+\b", text)
    # Also extract all words ≥ min_len that aren't stop-words
    all_words = re.findall(r"\b[a-zA-Z]{%d,}\b" % _MIN_KEYWORD_LEN, text.lower())

    entities: set[str] = set()
    for w in capitalized:
        if w.lower() not in _STOP_WORDS and len(w) >= _MIN_KEYWORD_LEN:
            entities.add(w.lower())
    for w in all_words:
        if w not in _STOP_WORDS:
            entities.add(w)

    return entities


def _overlap_score(a: set[str], b: set[str]) -> int:
    """Number of shared entity keywords between two questions."""
    return len(a & b)


# ---------------------------------------------------------------------------
# Correlator
# ---------------------------------------------------------------------------

@dataclass
class CorrelationAlert:
    primary_market_id: str
    primary_question: str
    primary_prob: float
    secondary_market_id: str
    secondary_question: str
    secondary_prob: float
    discrepancy: float          # positive = secondary is underpriced vs primary


class CrossMarketCorrelator:
    """
    Detects cross-market probability inconsistencies across a batch of markets.

    For every pair of related markets (same entity group), it checks whether
    a high-volume "primary" market has moved in a direction that implies a
    probability shift for a lower-volume "secondary" market that hasn't moved.

    When a discrepancy is found, a synthetic Catalyst is injected into the
    secondary market's catalyst list so that _classify_signal() in nodes.py
    can detect the CROSS_MARKET_CORRELATION signal and route it to the AI.

    The synthetic catalyst source format is:
        "CROSS_MARKET: <primary question> is at {prob:.0%}, implying {implied:.0%} here."
    """

    def __init__(
        self,
        min_shared_keywords: int = 2,
        primary_volume_threshold: float = _PRIMARY_VOLUME_THRESHOLD,
        discrepancy_threshold: float = _DISCREPANCY_THRESHOLD,
    ) -> None:
        self.min_shared_keywords = min_shared_keywords
        self.primary_volume_threshold = primary_volume_threshold
        self.discrepancy_threshold = discrepancy_threshold

    def enrich_batch(
        self,
        inputs: list[tuple[MarketSnapshot, list[Catalyst], str]],
    ) -> list[tuple[MarketSnapshot, list[Catalyst], str]]:
        """Inject synthetic cross-market catalysts where correlation lags are detected.

        Returns the same list with modified catalyst lists for affected markets.
        """
        if len(inputs) < 2:
            return inputs

        # Build entity sets for each market
        snapshots = [snap for snap, _, _ in inputs]
        entity_sets = [_extract_entities(s.question) for s in snapshots]

        # Find primary markets (high-volume reference points)
        primary_indices = {
            i for i, s in enumerate(snapshots)
            if s.volume_24h_usd >= self.primary_volume_threshold
        }

        # For each primary, find related secondaries and check for discrepancies
        alerts: dict[int, list[CorrelationAlert]] = {}  # secondary_idx → alerts

        for pri_idx in primary_indices:
            primary = snapshots[pri_idx]
            pri_entities = entity_sets[pri_idx]

            for sec_idx, secondary in enumerate(snapshots):
                if sec_idx == pri_idx:
                    continue
                if secondary.market_id == primary.market_id:
                    continue
                if secondary.volume_24h_usd >= self.primary_volume_threshold:
                    continue  # Both are primaries — skip (they're peers, not lagging)

                overlap = _overlap_score(pri_entities, entity_sets[sec_idx])
                if overlap < self.min_shared_keywords:
                    continue

                # Compute discrepancy: does the primary's prob imply the secondary is off?
                # Simple model: correlated markets should move in the same direction.
                # We flag when the absolute difference exceeds threshold.
                discrepancy = abs(primary.market_prob - secondary.market_prob)
                if discrepancy < self.discrepancy_threshold:
                    continue

                alert = CorrelationAlert(
                    primary_market_id=primary.market_id,
                    primary_question=primary.question,
                    primary_prob=primary.market_prob,
                    secondary_market_id=secondary.market_id,
                    secondary_question=secondary.question,
                    secondary_prob=secondary.market_prob,
                    discrepancy=discrepancy,
                )
                alerts.setdefault(sec_idx, []).append(alert)

        if not alerts:
            return inputs

        # Rebuild the inputs list, injecting synthetic catalysts for flagged secondaries
        enriched: list[tuple[MarketSnapshot, list[Catalyst], str]] = []
        for idx, (snapshot, catalysts, theme) in enumerate(inputs):
            if idx not in alerts:
                enriched.append((snapshot, catalysts, theme))
                continue

            # Pick the most significant alert (largest discrepancy)
            top_alert = max(alerts[idx], key=lambda a: a.discrepancy)
            synthetic_source = (
                f"CROSS_MARKET: '{top_alert.primary_question[:80]}' "
                f"is at {top_alert.primary_prob:.0%}, "
                f"but this related market is at {top_alert.secondary_prob:.0%} "
                f"(gap={top_alert.discrepancy:.0%})."
            )
            synthetic_catalyst = Catalyst(
                source=synthetic_source,
                quality=_SYNTHETIC_QUALITY,
                direction=_SYNTHETIC_DIRECTION if top_alert.primary_prob > top_alert.secondary_prob else -_SYNTHETIC_DIRECTION,
                confidence=_SYNTHETIC_CONFIDENCE,
            )
            new_catalysts = list(catalysts) + [synthetic_catalyst]
            enriched.append((snapshot, new_catalysts, theme))
            print(
                f"[CrossMarket] Flagged '{snapshot.question[:55]}' | "
                f"primary='{top_alert.primary_question[:40]}' {top_alert.primary_prob:.0%} | "
                f"gap={top_alert.discrepancy:.0%}"
            )

        return enriched

    def find_alerts(
        self,
        inputs: list[tuple[MarketSnapshot, list[Catalyst], str]],
    ) -> list[CorrelationAlert]:
        """Return all detected correlation alerts without modifying the batch.

        Useful for reporting / debugging.
        """
        snapshots = [snap for snap, _, _ in inputs]
        entity_sets = [_extract_entities(s.question) for s in snapshots]
        primary_indices = {
            i for i, s in enumerate(snapshots)
            if s.volume_24h_usd >= self.primary_volume_threshold
        }

        all_alerts: list[CorrelationAlert] = []
        for pri_idx in primary_indices:
            primary = snapshots[pri_idx]
            pri_entities = entity_sets[pri_idx]
            for sec_idx, secondary in enumerate(snapshots):
                if sec_idx == pri_idx:
                    continue
                if secondary.volume_24h_usd >= self.primary_volume_threshold:
                    continue
                overlap = _overlap_score(pri_entities, entity_sets[sec_idx])
                if overlap < self.min_shared_keywords:
                    continue
                discrepancy = abs(primary.market_prob - secondary.market_prob)
                if discrepancy < self.discrepancy_threshold:
                    continue
                all_alerts.append(CorrelationAlert(
                    primary_market_id=primary.market_id,
                    primary_question=primary.question,
                    primary_prob=primary.market_prob,
                    secondary_market_id=secondary.market_id,
                    secondary_question=secondary.question,
                    secondary_prob=secondary.market_prob,
                    discrepancy=discrepancy,
                ))
        return all_alerts
