"""
Reddit sentiment scanner — crowd signal for prediction markets.
===============================================================
Uses PRAW (Python Reddit API Wrapper, free, 100 req/min OAuth).

Monitors betting/prediction subreddits relevant to the topic and returns
a sentiment summary: bullish/bearish/mixed + top post titles + velocity.

Requires in .env:
  REDDIT_CLIENT_ID=...
  REDDIT_CLIENT_SECRET=...
  REDDIT_USER_AGENT=PredictionAgent/1.0   (optional, has default)

Get free credentials at: https://www.reddit.com/prefs/apps
  → Create app → type: script → redirect URI: http://localhost:8080
"""
from __future__ import annotations

import logging
import os
import time

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True) or find_dotenv())

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Subreddit map — which communities to check per sport/topic
# ---------------------------------------------------------------------------
_SUBREDDIT_MAP: dict[str, list[str]] = {
    "nba":      ["sportsbook", "nba"],
    "nfl":      ["sportsbook", "nfl"],
    "nhl":      ["sportsbook", "hockey"],
    "mlb":      ["sportsbook", "baseball"],
    "nascar":   ["sportsbook", "NASCAR"],
    "ufc":      ["sportsbook", "ufc", "mmafighting"],
    "boxing":   ["sportsbook", "boxing"],
    "tennis":   ["sportsbook", "tennis"],
    "golf":     ["sportsbook", "golf"],
    "soccer":   ["sportsbook", "soccer"],
    "politics": ["politics", "PredictIt", "Polymarket"],
    "crypto":   ["CryptoCurrency", "Bitcoin", "ethtrader"],
    "general":  ["sportsbook", "Polymarket", "PredictIt"],
}

# Keywords that indicate bullish/bearish sentiment in betting contexts
_BULL_KW = {
    "win", "wins", "winning", "cover", "covers", "over", "yes", "lock",
    "hammer", "crushing", "dominating", "confident", "strong", "easy",
    "surge", "rally", "moon", "calls", "buying", "long",
}
_BEAR_KW = {
    "loss", "lose", "losing", "fade", "fading", "under", "no", "avoid",
    "injured", "injury", "out", "questionable", "doubtful", "concern",
    "collapse", "drop", "puts", "selling", "short", "risky",
}

# Cache — key: (topic_lower, sport) → (result_str, expires_at)
_cache: dict[tuple, tuple[str, float]] = {}
_CACHE_TTL = 1800   # 30 minutes

# Lazy-loaded PRAW instance
_reddit = None


def _get_reddit():
    """Lazy-init PRAW Reddit client. Returns None if keys missing."""
    global _reddit
    if _reddit is not None:
        return _reddit

    client_id = os.environ.get("REDDIT_CLIENT_ID", "").strip()
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None

    try:
        import praw
        user_agent = os.environ.get("REDDIT_USER_AGENT", "PredictionAgent/1.0")
        _reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=user_agent,
        )
        # Verify connection (read-only, no auth needed for public subreddits)
        _reddit.read_only = True
        log.info("[reddit] PRAW client initialized (read-only)")
        return _reddit
    except Exception as exc:
        log.warning("[reddit] PRAW init failed: %s", exc)
        return None


def get_reddit_sentiment(topic: str, sport: str | None = None) -> str:
    """
    Search recent posts mentioning `topic` in relevant subreddits.
    Returns a [Reddit Sentiment] block or "" if no signal / keys missing.

    Args:
        topic: team name, player name, or market topic (e.g. "Lakers", "Trump")
        sport: sport key for subreddit selection (e.g. "nba", "nfl", "politics")
    """
    key = (topic.strip().lower()[:60], sport)
    now = time.time()

    cached = _cache.get(key)
    if cached:
        result, expires = cached
        if now < expires:
            log.debug("[reddit] Cache hit for %s", key)
            return result

    result = _fetch_reddit(topic, sport)
    _cache[key] = (result, now + _CACHE_TTL)
    return result


def _fetch_reddit(topic: str, sport: str | None) -> str:
    """Live PRAW fetch. Returns formatted block or ""."""
    reddit = _get_reddit()
    if reddit is None:
        return ""

    # Determine subreddits to search
    sport_key = (sport or "general").lower()
    subreddits = _SUBREDDIT_MAP.get(sport_key, _SUBREDDIT_MAP["general"])

    all_posts: list[dict] = []
    cutoff_2h = time.time() - 7200
    cutoff_24h = time.time() - 86400

    try:
        for sub_name in subreddits[:2]:   # max 2 subreddits to stay within rate limits
            try:
                sub = reddit.subreddit(sub_name)
                # Search recent posts mentioning the topic
                for post in sub.search(topic, sort="new", time_filter="day", limit=20):
                    title_lower = post.title.lower()
                    if topic.lower() not in title_lower:
                        # Also check selftext for shorter topics
                        if len(topic) > 4 and topic.lower() not in (post.selftext or "").lower():
                            continue

                    all_posts.append({
                        "title": post.title,
                        "score": post.score,
                        "upvote_ratio": post.upvote_ratio,
                        "num_comments": post.num_comments,
                        "created_utc": post.created_utc,
                        "url": f"https://reddit.com{post.permalink}",
                    })
            except Exception as exc:
                log.debug("[reddit] r/%s search failed: %s", sub_name, exc)
                continue

    except Exception as exc:
        log.warning("[reddit] Fetch failed for '%s': %s", topic, exc)
        return ""

    if not all_posts:
        return ""

    # Sort by score descending
    all_posts.sort(key=lambda p: p["score"], reverse=True)

    # Velocity: posts in last 2h vs last 24h
    recent_2h = [p for p in all_posts if p["created_utc"] > cutoff_2h]
    recent_24h = [p for p in all_posts if p["created_utc"] > cutoff_24h]

    # Sentiment scoring from titles
    bull_count = 0
    bear_count = 0
    for post in all_posts:
        words = set(post["title"].lower().split())
        if words & _BULL_KW:
            bull_count += 1
        if words & _BEAR_KW:
            bear_count += 1

    total = bull_count + bear_count
    if total > 0:
        bull_pct = bull_count / total
        if bull_pct >= 0.65:
            sentiment = "BULLISH"
            sentiment_note = "community leaning YES/WIN"
        elif bull_pct <= 0.35:
            sentiment = "BEARISH"
            sentiment_note = "community leaning NO/FADE"
        else:
            sentiment = "MIXED"
            sentiment_note = "divided community opinion"
    else:
        sentiment = "NEUTRAL"
        sentiment_note = "no strong directional signal"

    # Top 3 posts
    top_posts = all_posts[:3]
    top_titles = "\n".join(
        f"  • {p['title'][:100]} ({p['score']} pts, {p['num_comments']} comments)"
        for p in top_posts
    )

    # Velocity context
    velocity = ""
    if len(recent_2h) >= 3:
        velocity = f"  Velocity: {len(recent_2h)} posts in last 2h (active discussion)"
    elif len(recent_24h) >= 5:
        velocity = f"  Velocity: {len(recent_24h)} posts in last 24h"

    return (
        f"\n\n[Reddit Sentiment — {topic}]\n"
        f"  Sentiment: {sentiment} ({sentiment_note})\n"
        f"  Posts found: {len(all_posts)} across r/{', r/'.join(subreddits[:2])}\n"
        + (f"{velocity}\n" if velocity else "")
        + f"  Top posts:\n{top_titles}\n"
        "[End Reddit Sentiment]"
    )


def get_reddit_buzz_score(topic: str, sport: str | None = None) -> float:
    """
    Returns a 0.0–1.0 buzz score for use in insider alert suspicion scoring.
    0.0 = no mention, 1.0 = high velocity + strong sentiment signal.
    """
    reddit = _get_reddit()
    if reddit is None:
        return 0.0

    sport_key = (sport or "general").lower()
    subreddits = _SUBREDDIT_MAP.get(sport_key, _SUBREDDIT_MAP["general"])
    cutoff_2h = time.time() - 7200
    count_2h = 0

    try:
        sub = reddit.subreddit(subreddits[0])
        for post in sub.search(topic, sort="new", time_filter="hour", limit=10):
            if topic.lower() in post.title.lower():
                if post.created_utc > cutoff_2h:
                    count_2h += 1
    except Exception:
        return 0.0

    if count_2h >= 5:
        return 0.9
    elif count_2h >= 3:
        return 0.7
    elif count_2h >= 1:
        return 0.5
    return 0.0
