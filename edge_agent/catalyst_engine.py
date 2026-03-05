import importlib
NewsAPIClient = importlib.import_module(".dat-ingestion.news_api", "edge_agent").NewsAPIClient
from .ai_service import get_ai_response
from .models import Catalyst

class CatalystDetectionEngine:
    """An engine for detecting catalysts from news headlines."""

    def __init__(self):
        self.news_client = NewsAPIClient()

    def detect_catalysts(self, query: str, page_size: int = 5) -> list[Catalyst]:
        """Detects catalysts for a given query."""
        articles = self.news_client.get_top_headlines(query, page_size=page_size)
        catalysts = []
        for article in articles:
            catalyst = self._create_catalyst_from_article(article)
            if catalyst:
                catalysts.append(catalyst)
        return catalysts

    def _create_catalyst_from_article(self, article: dict) -> Catalyst | None:
        """Creates a catalyst from a news article."""
        system_prompt = (
            "You are a news analyst. Your task is to analyze a news headline and return a structured JSON object "
            "with your analysis. The JSON object should conform to the following schema: "
            '{"quality": float, "direction": float, "confidence": float}'
        )
        prompt = f"Analyze the following news headline: {article['title']}"

        ai_analysis = get_ai_response(prompt, task_type="complex", system_prompt=system_prompt)

        if ai_analysis:
            # Include headline in source so injury keyword detection works
            source_name = article.get("source", {}).get("name", "unknown")
            headline = article.get("title", "")
            return Catalyst(
                source=f"{source_name}: {headline}"[:200],
                quality=ai_analysis.get("quality", 0.5),
                direction=ai_analysis.get("direction", 0.0),
                confidence=ai_analysis.get("confidence", 0.5),
            )
        return None