import importlib

from .ai_service import get_ai_response
from .models import Catalyst

# dat-ingestion has a hyphen so it can't be a normal import — use importlib
NewsAPIClient = importlib.import_module(".dat-ingestion.news_api", "edge_agent").NewsAPIClient


class CatalystDetectionEngine:
    """Detects catalysts from live news headlines and scores them with AI."""

    def __init__(self):
        self.news_client = NewsAPIClient()

    def detect_catalysts(self, query: str) -> list[Catalyst]:
        articles = self.news_client.get_top_headlines(query)
        catalysts = []
        for article in articles:
            catalyst = self._create_catalyst_from_article(article)
            if catalyst:
                catalysts.append(catalyst)
        return catalysts

    def _create_catalyst_from_article(self, article: dict) -> Catalyst | None:
        system_prompt = (
            "You are a financial news analyst scoring a headline for a prediction market. "
            "Return ONLY a JSON object with these three float fields:\n"
            '{"quality": <0.0-1.0>, "direction": <-1.0 to 1.0>, "confidence": <0.0-1.0>}\n'
            "quality = information value, direction = bullish/bearish signal, "
            "confidence = how certain you are."
        )
        prompt = f"Headline: {article['title']}"

        result = get_ai_response(prompt, task_type="simple", system_prompt=system_prompt)
        if result:
            return Catalyst(
                source=article["source"]["name"],
                quality=float(result.get("quality", 0.5)),
                direction=float(result.get("direction", 0.0)),
                confidence=float(result.get("confidence", 0.5)),
            )
        return None
