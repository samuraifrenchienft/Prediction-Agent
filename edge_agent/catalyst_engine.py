from __future__ import annotations

import importlib

from .ai_service import get_ai_response
from .models import Catalyst

NewsAPIClient = importlib.import_module(".dat-ingestion.news_api", "edge_agent").NewsAPIClient

_DEFAULT_GATEKEEPER_PROMPT = (
    "You are a financial news analyst. Analyze the provided headline and return ONLY a JSON object "
    "with exactly these fields: "
    '{"relevance": <int 0-100>, "quality": <float 0-1, signal quality>, '
    '"direction": <float -1 to 1, bearish to bullish>, '
    '"confidence": <float 0-1, confidence in assessment>}. '
    "No extra keys, no markdown, no explanation outside the JSON object."
)
_DEFAULT_RELEVANCE_THRESHOLD = 0  # no filtering when no StrategyDNA is provided


class CatalystDetectionEngine:
    """An engine for detecting catalysts from news headlines.

    Pass a StrategyDNA instance to enable domain-specific relevance filtering
    and gatekeeper AI prompting. Without one, all articles above confidence
    threshold are accepted.
    """

    def __init__(self, strategy_dna=None) -> None:
        self.news_client = NewsAPIClient()
        self.strategy_dna = strategy_dna

    def detect_catalysts(self, query: str | None = None) -> list[Catalyst]:
        """Detects catalysts for a given query.

        If no query is provided and a StrategyDNA is configured, the DNA's
        keyword list drives the NewsAPI query automatically.
        """
        if query is None:
            query = (
                self.strategy_dna.build_news_query()
                if self.strategy_dna
                else "US politics"
            )

        articles = self.news_client.get_top_headlines(query)
        catalysts = []
        for article in articles:
            catalyst = self._create_catalyst_from_article(article)
            if catalyst:
                catalysts.append(catalyst)
        return catalysts

    def _create_catalyst_from_article(self, article: dict) -> Catalyst | None:
        """Creates a catalyst from a news article using the gatekeeper AI.

        Uses StrategyDNA's system prompt when available, which scores relevance
        alongside quality/direction/confidence and filters out off-topic articles.
        """
        system_prompt = (
            self.strategy_dna.to_system_prompt()
            if self.strategy_dna
            else _DEFAULT_GATEKEEPER_PROMPT
        )
        relevance_threshold = (
            self.strategy_dna.relevance_threshold
            if self.strategy_dna
            else _DEFAULT_RELEVANCE_THRESHOLD
        )

        prompt = f"Analyze this news headline: {article['title']}"
        ai_analysis = get_ai_response(prompt, task_type="complex", system_prompt=system_prompt)

        if not ai_analysis or "quality" not in ai_analysis:
            return None

        relevance = int(ai_analysis.get("relevance", 100))
        if relevance < relevance_threshold:
            return None

        source = article.get("source", {}).get("name", "unknown")
        return Catalyst(
            source=source,
            quality=float(ai_analysis.get("quality", 0.5)),
            direction=float(ai_analysis.get("direction", 0.0)),
            confidence=float(ai_analysis.get("confidence", 0.5)),
        )
