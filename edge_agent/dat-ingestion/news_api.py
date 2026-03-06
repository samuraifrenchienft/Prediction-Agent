import os
import requests
from dotenv import load_dotenv

load_dotenv()

import json
import os
import requests
from time import time

CACHE_DIR = ".cache"
if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

class NewsAPIClient:
    """A client for fetching news from the NewsAPI."""

    def __init__(self):
        self.api_key = os.environ.get("NEWS_API_KEY")
        if not self.api_key:
            raise ValueError("NEWS_API_KEY not found in .env file")
        self.base_url = "https://newsapi.org/v2/everything"

    def get_top_headlines(self, query: str, page_size: int = 10, ttl_seconds: int = 3600) -> list[dict]:
        """Gets the top headlines for a given query, with file-based caching."""
        cache_key = f"{query}_{page_size}.json"
        cache_file = os.path.join(CACHE_DIR, cache_key)

        if os.path.exists(cache_file):
            with open(cache_file, "r") as f:
                data = json.load(f)
                if time() - data["timestamp"] < ttl_seconds:
                    return data["response"]

        params = {
            "q": query,
            "pageSize": page_size,
            "apiKey": self.api_key,
        }
        response = requests.get(self.base_url, params=params)
        response.raise_for_status()
        articles = response.json().get("articles", [])

        with open(cache_file, "w") as f:
            json.dump({"timestamp": time(), "response": articles}, f)

        return articles
        """Gets the top headlines for a given query."""
        params = {
            "q": query,
            "pageSize": page_size,
            "apiKey": self.api_key,
        }
        response = requests.get(self.base_url, params=params)
        response.raise_for_status()
        return articles