import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Simple in-memory cache — avoids re-hitting NewsAPI for the same query
# within a single scan cycle or across back-to-back scans.
# TTL: 60 minutes (free plan = 100 req/day; 15-min scans = ~4 scans/hr)
# ---------------------------------------------------------------------------
_CACHE: dict[str, tuple[list[dict], float]] = {}  # query → (articles, timestamp)
_CACHE_TTL_SECONDS = 3600  # 60 minutes


class NewsAPIClient:
    """A client for fetching news from the NewsAPI with caching and 429 handling."""

    def __init__(self):
        self.api_key = os.environ.get("NEWS_API_KEY")
        if not self.api_key:
            raise ValueError("NEWS_API_KEY not found in .env file")
        self.base_url = "https://newsapi.org/v2/everything"

    def get_top_headlines(self, query: str, page_size: int = 5) -> list[dict]:
        """Gets news articles for a query, with caching and graceful 429 handling.

        Returns an empty list (rather than raising) if the API is rate-limited
        or unavailable — the scan continues without news for that market.
        """
        cache_key = f"{query}:{page_size}"

        # Return cached result if still fresh
        if cache_key in _CACHE:
            articles, ts = _CACHE[cache_key]
            if time.time() - ts < _CACHE_TTL_SECONDS:
                return articles

        try:
            params = {
                "q": query,
                "pageSize": page_size,
                "sortBy": "publishedAt",
                "apiKey": self.api_key,
            }
            response = requests.get(self.base_url, params=params, timeout=8)

            if response.status_code == 429:
                print(f"[NewsAPI] Rate limited (429) — skipping news for '{query[:50]}'")
                return []

            response.raise_for_status()
            articles = response.json().get("articles", [])
            _CACHE[cache_key] = (articles, time.time())
            return articles

        except requests.exceptions.Timeout:
            print(f"[NewsAPI] Timeout for '{query[:50]}'")
            return []
        except Exception as e:
            print(f"[NewsAPI] Error for '{query[:50]}': {e}")
            return []
