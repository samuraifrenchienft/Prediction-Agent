"""
Sportsbook Odds — The Odds API free tier integration.
======================================================

Fetches live lines from real sportsbooks (DraftKings, FanDuel, BetMGM, etc.)
and converts them to implied probabilities for comparison against Polymarket.

API key: free tier at https://the-odds-api.com
  - 500 requests/month (no credit card required)
  - Set THE_ODDS_API_KEY in .env

The real value here is EDGE DETECTION:
  DraftKings: Celtics -200 (67% implied)
  Polymarket: Celtics YES at 55¢
  → 12pp gap — buy Celtics YES on Polymarket

All results are cached 30 minutes to preserve the free quota.
"""
from __future__ import annotations

import logging
import os
import time

import requests
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True) or find_dotenv())

log = logging.getLogger(__name__)

_API_KEY = os.environ.get("THE_ODDS_API_KEY", "")
_BASE = "https://api.the-odds-api.com/v4"

# ── Sport key map: our canonical name → The Odds API sport key ──────────────
_SPORT_KEYS: dict[str, str] = {
    "nba":     "basketball_nba",
    "nhl":     "icehockey_nhl",
    "nfl":     "americanfootball_nfl",
    "mlb":     "baseball_mlb",
    "ncaa":    "basketball_ncaab",
    "wnba":    "basketball_wnba",
    "ufc":     "mma_mixed_martial_arts",
    "soccer":  "soccer_epl",          # Premier League by default
    "epl":     "soccer_epl",
    "mls":     "soccer_usa_mls",
}

# ── Cache: sport_key → (data, fetched_ts) ────────────────────────────────────
_CACHE: dict[str, tuple[list, float]] = {}
_CACHE_TTL = 1800  # 30 min — preserve free quota

# Priority bookmakers to display (used in order, show first available)
_PREF_BOOKS = ["draftkings", "fanduel", "betmgm", "pointsbetus", "bovada"]


# ── Implied probability helpers ───────────────────────────────────────────────

def _american_to_implied(odds: int) -> float:
    """Convert American odds to implied probability (0-1, vig not removed)."""
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)


def _implied_to_american(prob: float) -> str:
    """Convert implied probability to American odds string (e.g. '-150' or '+130')."""
    if prob <= 0 or prob >= 1:
        return "N/A"
    if prob >= 0.5:
        return f"-{round(prob / (1 - prob) * 100)}"
    return f"+{round((1 - prob) / prob * 100)}"


def _vig_free(p_a: float, p_b: float) -> tuple[float, float]:
    """Remove vig from a two-outcome market to get true implied probabilities."""
    total = p_a + p_b
    if total <= 0:
        return 0.5, 0.5
    return p_a / total, p_b / total


# ── Core fetch ────────────────────────────────────────────────────────────────

def fetch_odds(sport: str) -> list[dict]:
    """
    Fetch live odds for a sport from The Odds API.
    Returns list of game dicts (each has teams, commence_time, bookmakers).
    Cached 30 minutes. Returns [] if no API key or fetch fails.
    """
    if not _API_KEY:
        return []

    sport_key = _SPORT_KEYS.get(sport.lower(), "")
    if not sport_key:
        return []

    now = time.time()
    cached = _CACHE.get(sport_key)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    try:
        r = requests.get(
            f"{_BASE}/sports/{sport_key}/odds",
            params={
                "apiKey":     _API_KEY,
                "regions":    "us",
                "markets":    "h2h,spreads,totals",
                "oddsFormat": "american",
            },
            timeout=10,
        )
        remaining = r.headers.get("x-requests-remaining", "?")
        log.debug("[odds_api] %s — HTTP %d, %s requests remaining", sport_key, r.status_code, remaining)

        if r.status_code == 401:
            log.warning("[odds_api] Invalid API key")
            return []
        if r.status_code == 422:
            log.warning("[odds_api] Sport key not supported: %s", sport_key)
            return []
        r.raise_for_status()

        data = r.json()
        if isinstance(data, list):
            _CACHE[sport_key] = (data, now)
            return data
    except Exception as exc:
        log.debug("[odds_api] Fetch failed for %s: %s", sport, exc)

    return []


# ── Per-game lookup ────────────────────────────────────────────────────────────

def find_game_odds(team_a: str, team_b: str | None, sport: str) -> dict | None:
    """
    Find odds for a specific game by team name.
    team_b may be None for single-team queries (returns first game containing team_a).
    Returns a structured dict with lines from preferred bookmakers, or None.
    """
    games = fetch_odds(sport)
    if not games:
        return None

    a_lower = team_a.lower()
    b_lower = team_b.lower() if team_b else None

    for game in games:
        home = (game.get("home_team") or "").lower()
        away = (game.get("away_team") or "").lower()
        teams_lower = {home, away}

        if a_lower not in home and a_lower not in away:
            continue
        if b_lower and b_lower not in home and b_lower not in away:
            continue

        return _parse_game(game)

    return None


def _parse_game(game: dict) -> dict:
    """
    Parse a raw Odds API game object into a clean structured result.

    Returns:
        home_team, away_team, commence_time,
        moneyline   — {book: {home: int, away: int}} from best available book
        spread      — {book: {home_line: float, home_odds: int, away_line: float, away_odds: int}}
        total       — {book: {line: float, over_odds: int, under_odds: int}}
        implied     — {home_true: float, away_true: float}  (vig-removed)
    """
    result: dict = {
        "home_team":    game.get("home_team", ""),
        "away_team":    game.get("away_team", ""),
        "commence_time": game.get("commence_time", ""),
        "moneyline": {},
        "spread": {},
        "total": {},
        "implied": {},
    }

    bookmakers = game.get("bookmakers") or []

    # Sort bookmakers by preference
    def _book_rank(b: dict) -> int:
        key = b.get("key", "")
        try:
            return _PREF_BOOKS.index(key)
        except ValueError:
            return 99

    bookmakers = sorted(bookmakers, key=_book_rank)

    for book in bookmakers:
        book_key = book.get("key", book.get("title", "unknown"))
        for market in book.get("markets") or []:
            mkey = market.get("key", "")
            outcomes = market.get("outcomes") or []

            if mkey == "h2h" and not result["moneyline"]:
                ml: dict = {}
                for o in outcomes:
                    name = o.get("name", "")
                    price = o.get("price", 0)
                    if result["home_team"] in name:
                        ml["home"] = price
                    else:
                        ml["away"] = price
                if "home" in ml and "away" in ml:
                    result["moneyline"] = {"book": book_key, **ml}
                    # Vig-free implied probabilities
                    p_home = _american_to_implied(ml["home"])
                    p_away = _american_to_implied(ml["away"])
                    true_h, true_a = _vig_free(p_home, p_away)
                    result["implied"] = {
                        "home_true": round(true_h, 4),
                        "away_true": round(true_a, 4),
                    }

            elif mkey == "spreads" and not result["spread"]:
                sp: dict = {}
                for o in outcomes:
                    name = o.get("name", "")
                    point = o.get("point", 0)
                    price = o.get("price", 0)
                    if result["home_team"] in name:
                        sp["home_line"] = point
                        sp["home_odds"] = price
                    else:
                        sp["away_line"] = point
                        sp["away_odds"] = price
                if "home_line" in sp:
                    result["spread"] = {"book": book_key, **sp}

            elif mkey == "totals" and not result["total"]:
                tot: dict = {}
                for o in outcomes:
                    name = (o.get("name") or "").lower()
                    if "over" in name:
                        tot["line"] = o.get("point", 0)
                        tot["over_odds"] = o.get("price", 0)
                    elif "under" in name:
                        tot["under_odds"] = o.get("price", 0)
                if "line" in tot:
                    result["total"] = {"book": book_key, **tot}

    return result


# ── Context builder for AI prompt ─────────────────────────────────────────────

def build_sportsbook_context(teams: list[str], sport: str) -> str:
    """
    Build a [Sportsbook Lines] context block for injection into the AI prompt.

    Shows moneyline, spread, total, and implied probability from real books.
    Also notes any edge vs a provided Polymarket price (polymarket_pct).

    Returns "" if no API key or no matching game found.
    """
    if not _API_KEY or not teams:
        return ""

    team_a = teams[0]
    team_b = teams[1] if len(teams) >= 2 else None

    game = find_game_odds(team_a, team_b, sport)
    if not game:
        return ""

    home = game["home_team"]
    away = game["away_team"]
    ml   = game.get("moneyline", {})
    sp   = game.get("spread", {})
    tot  = game.get("total", {})
    imp  = game.get("implied", {})

    lines: list[str] = [f"\n[Sportsbook Lines — {away} @ {home}]"]

    if ml:
        h_ml = ml.get("home", 0)
        a_ml = ml.get("away", 0)
        book = ml.get("book", "").upper()
        h_str = f"+{h_ml}" if h_ml > 0 else str(h_ml)
        a_str = f"+{a_ml}" if a_ml > 0 else str(a_ml)
        lines.append(f"  Moneyline ({book}):  {away} {a_str}  |  {home} {h_str}")

    if imp:
        h_pct = round(imp["home_true"] * 100, 1)
        a_pct = round(imp["away_true"] * 100, 1)
        lines.append(f"  Implied win%:        {away} {a_pct}%  |  {home} {h_pct}%  (vig removed)")

    if sp:
        book = sp.get("book", "").upper()
        h_line = sp.get("home_line", 0)
        a_line = sp.get("away_line", 0)
        h_odds = sp.get("home_odds", -110)
        a_odds = sp.get("away_odds", -110)
        h_line_str = f"+{h_line}" if h_line > 0 else str(h_line)
        a_line_str = f"+{a_line}" if a_line > 0 else str(a_line)
        h_o_str = f"+{h_odds}" if h_odds > 0 else str(h_odds)
        a_o_str = f"+{a_odds}" if a_odds > 0 else str(a_odds)
        lines.append(f"  Spread ({book}):      {away} {a_line_str} ({a_o_str})  |  {home} {h_line_str} ({h_o_str})")

    if tot:
        book  = tot.get("book", "").upper()
        line  = tot.get("line", 0)
        o_str = f"+{tot['over_odds']}" if tot.get("over_odds", 0) > 0 else str(tot.get("over_odds", -110))
        u_str = f"+{tot['under_odds']}" if tot.get("under_odds", 0) > 0 else str(tot.get("under_odds", -110))
        lines.append(f"  Total ({book}):       O/U {line}  |  Over {o_str}  /  Under {u_str}")

    lines.append(
        "  NOTE: Use implied win% ONLY to compare vs Polymarket probability for edge detection. "
        "Do NOT use spread/moneyline language in your reply — translate to probabilities."
    )

    return "\n".join(lines)
