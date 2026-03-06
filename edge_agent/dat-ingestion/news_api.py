import json
import os
from time import time

import requests
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True) or find_dotenv())

# File-based cache to avoid burning free API quota on repeated calls
_CACHE_DIR = ".cache"
os.makedirs(_CACHE_DIR, exist_ok=True)


class NewsAPIClient:
    """
    Fetches news articles. Uses GNews (free tier) by default.
    Get a free key at: https://gnews.io  (100 req/day, no credit card)

    Falls back to NewsAPI.org if GNEWS_API_KEY is not set but NEWS_API_KEY is.
    """

    def __init__(self):
        self.gnews_key = os.environ.get("GNEWS_API_KEY")
        self.newsapi_key = os.environ.get("NEWS_API_KEY")

        if not self.gnews_key and not self.newsapi_key:
            raise ValueError(
                "No news API key found. Set GNEWS_API_KEY in .env "
                "(free at gnews.io — 100 req/day, no credit card needed)"
            )

    def get_top_headlines(self, query: str, page_size: int = 10, ttl_seconds: int = 3600) -> list[dict]:
        """Returns articles for a query, cached for ttl_seconds to save quota."""
        cache_file = os.path.join(_CACHE_DIR, f"{query.replace(' ', '_')}_{page_size}.json")

        if os.path.exists(cache_file):
            with open(cache_file, "r") as f:
                data = json.load(f)
                if time() - data["timestamp"] < ttl_seconds:
                    return data["articles"]

        articles = (
            self._fetch_gnews(query, page_size)
            if self.gnews_key
            else self._fetch_newsapi(query, page_size)
        )

        with open(cache_file, "w") as f:
            json.dump({"timestamp": time(), "articles": articles}, f)

        return articles

    def _fetch_gnews(self, query: str, max_results: int) -> list[dict]:
        params = {
            "q": query,
            "max": min(max_results, 10),
            "lang": "en",
            "token": self.gnews_key,
        }
        response = requests.get("https://gnews.io/api/v4/search", params=params, timeout=10)
        response.raise_for_status()
        articles = response.json().get("articles", [])
        return [
            {
                "title": a.get("title", ""),
                "source": {"name": a.get("source", {}).get("name", "unknown")},
            }
            for a in articles
        ]

    def _fetch_newsapi(self, query: str, page_size: int) -> list[dict]:
        params = {
            "q": query,
            "pageSize": page_size,
            "apiKey": self.newsapi_key,
        }
        response = requests.get("https://newsapi.org/v2/everything", params=params, timeout=10)
        response.raise_for_status()
        return response.json().get("articles", [])
