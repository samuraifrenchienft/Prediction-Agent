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
        articles = self.news_client.get_top_headlines(query)[:4]  # cap at 4 to limit AI calls
        catalysts = []
        for article in articles:
            catalyst = self._create_catalyst_from_article(article)
            if catalyst:
                catalysts.append(catalyst)
        return catalysts

    @staticmethod
    def _safe_float(value, default: float) -> float:
        """Convert AI response value to float, stripping any junk characters."""
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            import re
            # Extract first number (including negatives and decimals) from string
            match = re.search(r"-?\d+\.?\d*", value)
            if match:
                return float(match.group())
        return default

    def _create_catalyst_from_article(self, article: dict) -> Catalyst | None:
        system_prompt = (
            "You are a financial news analyst scoring a headline for a prediction market. "
            "Return ONLY a valid JSON object with exactly these three numeric fields:\n"
            '{"quality": 0.7, "direction": 0.3, "confidence": 0.6}\n'
            "quality = 0.0-1.0 information value. direction = -1.0 to 1.0 bearish/bullish. "
            "confidence = 0.0-1.0. Use plain numbers only, no text in values."
        )
        prompt = f"Headline: {article['title']}"

        try:
            result = get_ai_response(prompt, task_type="simple", system_prompt=system_prompt)
            if result:
                return Catalyst(
                    source=article.get("source", {}).get("name", "news"),
                    quality=self._safe_float(result.get("quality"), 0.5),
                    direction=self._safe_float(result.get("direction"), 0.0),
                    confidence=self._safe_float(result.get("confidence"), 0.5),
                )
        except Exception:
            pass
        return None
