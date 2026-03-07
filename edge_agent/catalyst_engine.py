import importlib
import json

from .ai_service import get_ai_response
from .models import Catalyst

# dat-ingestion has a hyphen so it can't be a normal import — use importlib
NewsAPIClient = importlib.import_module(".dat-ingestion.news_api", "edge_agent").NewsAPIClient


class CatalystDetectionEngine:
    """
    Detects catalysts from live news headlines and scores them with AI.

    All articles for a theme are sent in a SINGLE AI call (batch scoring)
    to keep OpenRouter usage to 1 call per theme per scan cycle instead of
    1 call per article (which was 4× more expensive).
    """

    def __init__(self):
        self.news_client = NewsAPIClient()

    @staticmethod
    def _safe_float(value, default: float) -> float:
        """Convert AI response value to float, stripping any junk characters."""
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            import re
            match = re.search(r"-?\d+\.?\d*", value)
            if match:
                return float(match.group())
        return default

    def detect_catalysts(self, query: str) -> list[Catalyst]:
        """
        Fetch up to 4 headlines for the query and score ALL of them in one
        AI call. Returns a list of Catalyst objects (empty if no articles or
        if the AI call fails).
        """
        articles = self.news_client.get_top_headlines(query)[:4]
        if not articles:
            return []

        # Build a compact numbered list so the AI can score them all at once
        headline_block = "\n".join(
            f"{i+1}. {a['title']}" for i, a in enumerate(articles)
        )

        system_prompt = (
            "You are a financial news analyst scoring headlines for prediction markets. "
            f"Score each of the {len(articles)} headlines below.\n"
            "Return ONLY a valid JSON array with one object per headline, in order:\n"
            '[{"quality":0.7,"direction":0.3,"confidence":0.6}, ...]\n'
            "quality=0.0-1.0 information value. "
            "direction=-1.0(bearish) to 1.0(bullish). "
            "confidence=0.0-1.0. Plain numbers only, no text in values."
        )
        prompt = f"Headlines:\n{headline_block}"

        catalysts: list[Catalyst] = []
        try:
            result = get_ai_response(prompt, task_type="simple", system_prompt=system_prompt)

            # result may be a list directly or wrapped under a key
            if isinstance(result, list):
                scores = result
            elif isinstance(result, dict):
                # Sometimes the model wraps it: {"scores": [...]} or {"data": [...]}
                scores = next(
                    (v for v in result.values() if isinstance(v, list)),
                    None,
                )
                if scores is None:
                    # Single-article response mistakenly returned as dict
                    scores = [result]
            else:
                scores = []

            for i, score in enumerate(scores):
                if i >= len(articles):
                    break
                article = articles[i]
                if not isinstance(score, dict):
                    continue
                catalysts.append(Catalyst(
                    source=article.get("source", {}).get("name", "news"),
                    quality=self._safe_float(score.get("quality"), 0.5),
                    direction=self._safe_float(score.get("direction"), 0.0),
                    confidence=self._safe_float(score.get("confidence"), 0.5),
                ))
        except Exception:
            pass

        return catalysts
