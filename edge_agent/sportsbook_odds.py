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

# ── Line movement tracking ─────────────────────────────────────────────────────
# Stores previous moneyline/spread snapshots per game_id so we can detect shifts
# Format: {game_id: {"home_ml": int, "away_ml": int, "home_line": float, "total": float, "ts": float}}
_LINE_HISTORY: dict[str, dict] = {}

# Recent detected movements — (game_label, description, detected_ts)
_MOVEMENTS: list[tuple[str, str, float]] = []
_MOVEMENT_WINDOW = 3600  # keep movements for 1 hour
_ML_MOVE_THRESHOLD = 10   # min American-odds change to flag moneyline shift
_LINE_MOVE_THRESHOLD = 0.5  # min points to flag spread/total shift


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


# ── Odds format converters (public) ───────────────────────────────────────────

def decimal_to_implied(decimal_odds: float) -> float:
    """
    Convert decimal odds (e.g. 2.50) to implied probability.
    Decimal 2.50 → 1/2.50 = 40% implied.
    """
    if decimal_odds <= 1:
        return 0.0
    return 1.0 / decimal_odds


def fractional_to_implied(numerator: float, denominator: float) -> float:
    """
    Convert fractional odds (e.g. 3/2) to implied probability.
    3/2 → 2 / (3 + 2) = 40% implied.
    """
    if denominator <= 0 or (numerator + denominator) <= 0:
        return 0.0
    return denominator / (numerator + denominator)


def american_to_implied_public(odds: int) -> float:
    """Public wrapper — American odds to implied probability (0-1)."""
    return _american_to_implied(odds)


def convert_odds(raw: str) -> dict | None:
    """
    Parse any common odds format and return a conversion dict.
    Handles: American (+150, -200), Decimal (2.50), Fractional (3/2, 5-2).

    Returns:
        {
            "format": "american" | "decimal" | "fractional",
            "input": original string,
            "implied_pct": float (0-100),
            "american": str,
            "decimal": float,
            "fractional": str,
        }
    or None if parsing fails.
    """
    import re as _re

    raw = raw.strip()

    # American: +150 or -200
    m = _re.fullmatch(r"([+-]\d{2,4})", raw)
    if m:
        val = int(m.group(1))
        prob = _american_to_implied(val)
        decimal = round(1 / prob, 3) if prob > 0 else 0
        # Approx fractional: numerator / denominator
        if val > 0:
            frac = f"{val}/100"
        else:
            frac = f"100/{abs(val)}"
        return {
            "format": "american",
            "input": raw,
            "implied_pct": round(prob * 100, 1),
            "american": raw,
            "decimal": decimal,
            "fractional": frac,
        }

    # Decimal: 2.50 or 1.91
    m = _re.fullmatch(r"(\d+\.\d+)", raw)
    if m:
        val = float(m.group(1))
        prob = decimal_to_implied(val)
        if prob <= 0:
            return None
        american = _implied_to_american(prob)
        # Approx fractional
        numerator = round((1 / prob) - 1, 2)
        frac = f"{numerator:.2f}/1" if numerator != int(numerator) else f"{int(numerator)}/1"
        return {
            "format": "decimal",
            "input": raw,
            "implied_pct": round(prob * 100, 1),
            "american": american,
            "decimal": val,
            "fractional": frac,
        }

    # Fractional: 3/2 or 5-2 or 7/4
    m = _re.fullmatch(r"(\d+)[/\-](\d+)", raw)
    if m:
        num, den = float(m.group(1)), float(m.group(2))
        prob = fractional_to_implied(num, den)
        if prob <= 0:
            return None
        american = _implied_to_american(prob)
        decimal_val = round(num / den + 1, 3)
        return {
            "format": "fractional",
            "input": raw,
            "implied_pct": round(prob * 100, 1),
            "american": american,
            "decimal": decimal_val,
            "fractional": f"{int(num)}/{int(den)}",
        }

    return None


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
            _snapshot_and_detect(data, now)
            return data
    except Exception as exc:
        log.debug("[odds_api] Fetch failed for %s: %s", sport, exc)

    return []


def _snapshot_and_detect(games: list[dict], now: float) -> None:
    """
    Compare fresh Odds API data against previous snapshots.
    Appends notable line movements to _MOVEMENTS.
    """
    global _MOVEMENTS
    # Prune stale movements
    _MOVEMENTS = [(g, d, t) for g, d, t in _MOVEMENTS if now - t < _MOVEMENT_WINDOW]

    for game in games:
        game_id = game.get("id", "")
        if not game_id:
            continue

        home = game.get("home_team", "")
        away = game.get("away_team", "")
        label = f"{away} @ {home}"

        # Extract best moneyline/spread/total from this snapshot
        snap: dict = {"ts": now}
        for book in sorted(game.get("bookmakers") or [], key=lambda b: _PREF_BOOKS.index(b["key"]) if b.get("key") in _PREF_BOOKS else 99):
            for market in book.get("markets") or []:
                mkey = market.get("key", "")
                outcomes = market.get("outcomes") or []
                if mkey == "h2h" and "home_ml" not in snap:
                    for o in outcomes:
                        if home in o.get("name", ""):
                            snap["home_ml"] = o.get("price", 0)
                        else:
                            snap["away_ml"] = o.get("price", 0)
                elif mkey == "spreads" and "home_line" not in snap:
                    for o in outcomes:
                        if home in o.get("name", ""):
                            snap["home_line"] = o.get("point", 0.0)
                elif mkey == "totals" and "total" not in snap:
                    for o in outcomes:
                        if "over" in (o.get("name") or "").lower():
                            snap["total"] = o.get("point", 0.0)

        prev = _LINE_HISTORY.get(game_id)
        if prev:
            moves: list[str] = []
            # Moneyline movement
            if "home_ml" in snap and "home_ml" in prev:
                delta = snap["home_ml"] - prev["home_ml"]
                if abs(delta) >= _ML_MOVE_THRESHOLD:
                    direction = "shortened" if delta < 0 else "lengthened"
                    sign = "+" if snap["home_ml"] > 0 else ""
                    moves.append(
                        f"{home} ML {direction}: {prev['home_ml']:+d} → {sign}{snap['home_ml']} "
                        f"({delta:+d})"
                    )
            if "away_ml" in snap and "away_ml" in prev:
                delta = snap["away_ml"] - prev["away_ml"]
                if abs(delta) >= _ML_MOVE_THRESHOLD:
                    direction = "shortened" if delta < 0 else "lengthened"
                    sign = "+" if snap["away_ml"] > 0 else ""
                    moves.append(
                        f"{away} ML {direction}: {prev['away_ml']:+d} → {sign}{snap['away_ml']} "
                        f"({delta:+d})"
                    )
            # Spread movement
            if "home_line" in snap and "home_line" in prev:
                delta = snap["home_line"] - prev["home_line"]
                if abs(delta) >= _LINE_MOVE_THRESHOLD:
                    sign = "+" if snap["home_line"] > 0 else ""
                    moves.append(
                        f"Spread moved: {home} {prev['home_line']:+.1f} → {sign}{snap['home_line']:.1f} "
                        f"({delta:+.1f} pts)"
                    )
            # Total movement
            if "total" in snap and "total" in prev:
                delta = snap["total"] - prev["total"]
                if abs(delta) >= _LINE_MOVE_THRESHOLD:
                    moves.append(
                        f"Total moved: O/U {prev['total']} → O/U {snap['total']} ({delta:+.1f})"
                    )
            for m in moves:
                log.info("[line_move] %s — %s", label, m)
                _MOVEMENTS.append((label, m, now))

        _LINE_HISTORY[game_id] = snap


def get_line_movement(sport: str) -> str:
    """
    Return a formatted string of recent line movements for the given sport.
    Returns "" if no movements detected since last fetch.
    """
    now = time.time()
    # Ensure we have fresh data (uses cache if recent)
    fetch_odds(sport)

    relevant = [(g, d, t) for g, d, t in _MOVEMENTS if now - t < _MOVEMENT_WINDOW]
    if not relevant:
        return ""

    lines = ["📊 <b>Line Movement (last 60 min)</b>"]
    for game_label, desc, ts in relevant:
        mins_ago = int((now - ts) / 60)
        ago_str = "just now" if mins_ago < 2 else f"{mins_ago}m ago"
        lines.append(f"  <b>{game_label}</b>  [{ago_str}]")
        lines.append(f"    • {desc}")
    return "\n".join(lines)


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


# ── Multi-book side-by-side comparison ────────────────────────────────────────

def build_multibook_context(teams: list[str], sport: str) -> str:
    """
    Show moneyline odds side-by-side across all available bookmakers for a game.
    Highlights the best line for each side and notes implied probability spread
    across books (useful for spotting book-specific mispricing).

    Returns "" if no API key or game not found.
    """
    if not _API_KEY or not teams:
        return ""

    sport_key = _SPORT_KEYS.get(sport.lower(), "")
    if not sport_key:
        return ""

    games = fetch_odds(sport)
    if not games:
        return ""

    a_lower = teams[0].lower()
    b_lower = teams[1].lower() if len(teams) >= 2 else None

    target_game: dict | None = None
    for g in games:
        home = (g.get("home_team") or "").lower()
        away = (g.get("away_team") or "").lower()
        if a_lower not in home and a_lower not in away:
            continue
        if b_lower and b_lower not in home and b_lower not in away:
            continue
        target_game = g
        break

    if not target_game:
        return ""

    home_team = target_game.get("home_team", "")
    away_team = target_game.get("away_team", "")
    bookmakers = target_game.get("bookmakers") or []

    # Collect h2h from every book
    book_lines: list[tuple[str, int, int]] = []  # (book_name, home_ml, away_ml)
    for book in bookmakers:
        book_name = book.get("title") or book.get("key", "Unknown")
        for market in book.get("markets") or []:
            if market.get("key") != "h2h":
                continue
            home_ml = away_ml = None
            for o in market.get("outcomes") or []:
                name = o.get("name", "")
                price = o.get("price", 0)
                if home_team in name:
                    home_ml = price
                else:
                    away_ml = price
            if home_ml is not None and away_ml is not None:
                book_lines.append((book_name, home_ml, away_ml))

    if not book_lines:
        return ""

    # Sort preferred books first
    def _rank(item: tuple) -> int:
        k = item[0].lower().replace(" ", "")
        for i, pb in enumerate(_PREF_BOOKS):
            if pb in k:
                return i
        return 99

    book_lines.sort(key=_rank)

    # Find best lines
    best_home = max(book_lines, key=lambda x: x[1])
    best_away = max(book_lines, key=lambda x: x[2])

    out: list[str] = [f"\n📚 <b>Multi-Book Moneyline — {away_team} @ {home_team}</b>"]
    out.append(f"{'Book':<18} {'Away':>8} {'Home':>8}")
    out.append("─" * 36)

    for book_name, home_ml, away_ml in book_lines:
        home_str = f"{home_ml:+d}"
        away_str = f"{away_ml:+d}"
        # Mark best line with ★
        if (book_name, home_ml, away_ml) == best_home:
            home_str += " ★"
        if (book_name, home_ml, away_ml) == best_away:
            away_str += " ★"
        out.append(f"{book_name:<18} {away_str:>9} {home_str:>9}")

    # Implied probability range across books
    home_probs = [_american_to_implied(ml) for _, ml, _ in book_lines]
    away_probs = [_american_to_implied(ml) for _, _, ml in book_lines]
    h_lo, h_hi = min(home_probs) * 100, max(home_probs) * 100
    a_lo, a_hi = min(away_probs) * 100, max(away_probs) * 100
    out.append("─" * 36)
    out.append(f"Implied range:  {away_team[:12]}: {a_lo:.1f}–{a_hi:.1f}%  |  {home_team[:12]}: {h_lo:.1f}–{h_hi:.1f}%")
    out.append("★ = best line available")

    return "\n".join(out)


# ── Player prop bets ───────────────────────────────────────────────────────────

# Prop market keys available via The Odds API (varies by sport and season)
_PROP_MARKETS: dict[str, list[str]] = {
    "basketball_nba": [
        "player_points", "player_rebounds", "player_assists",
        "player_threes", "player_blocks", "player_steals",
    ],
    "americanfootball_nfl": [
        "player_pass_tds", "player_pass_yds", "player_rush_yds",
        "player_reception_yds", "player_receptions",
    ],
    "baseball_mlb": [
        "player_total_bases", "player_hits", "player_home_runs",
        "player_strikeouts_thrown",
    ],
    "icehockey_nhl": [
        "player_points", "player_goals", "player_assists",
    ],
}

# Cache: (sport_key, event_id) → (props_list, fetched_ts)
_PROPS_CACHE: dict[tuple[str, str], tuple[list, float]] = {}
_PROPS_CACHE_TTL = 900  # 15 min — props change more frequently than game lines


def fetch_player_props(sport: str, team: str) -> list[dict]:
    """
    Fetch player prop bets for the first game matching `team` in the given sport.
    Returns a list of prop dicts:
        {player, market, line, over_odds, under_odds, book}
    Returns [] if no API key, no matching game, or no props available.
    """
    if not _API_KEY:
        return []

    sport_key = _SPORT_KEYS.get(sport.lower(), "")
    if not sport_key or sport_key not in _PROP_MARKETS:
        return []

    # Find the matching event_id from game cache
    games = fetch_odds(sport)
    team_lower = team.lower()
    event_id = ""
    for g in games:
        home = (g.get("home_team") or "").lower()
        away = (g.get("away_team") or "").lower()
        if team_lower in home or team_lower in away:
            event_id = g.get("id", "")
            break

    if not event_id:
        return []

    cache_key = (sport_key, event_id)
    now = time.time()
    cached = _PROPS_CACHE.get(cache_key)
    if cached and (now - cached[1]) < _PROPS_CACHE_TTL:
        return cached[0]

    markets_param = ",".join(_PROP_MARKETS[sport_key])
    try:
        r = requests.get(
            f"{_BASE}/sports/{sport_key}/events/{event_id}/odds",
            params={
                "apiKey":     _API_KEY,
                "regions":    "us",
                "markets":    markets_param,
                "oddsFormat": "american",
            },
            timeout=12,
        )
        remaining = r.headers.get("x-requests-remaining", "?")
        log.debug("[props] %s %s — HTTP %d, %s remaining", sport_key, event_id, r.status_code, remaining)

        if r.status_code in (401, 404, 422):
            log.warning("[props] HTTP %d for %s %s", r.status_code, sport_key, event_id)
            return []
        r.raise_for_status()

        data = r.json()
        props = _parse_props(data)
        _PROPS_CACHE[cache_key] = (props, now)
        return props

    except Exception as exc:
        log.debug("[props] Fetch failed for %s %s: %s", sport, event_id, exc)
        return []


def _parse_props(event_data: dict) -> list[dict]:
    """
    Parse raw Odds API event/odds response into flat list of player prop lines.
    Picks the first available bookmaker for each player+market combo.
    """
    results: dict[tuple[str, str], dict] = {}  # (player, market) → best prop

    bookmakers = event_data.get("bookmakers") or []
    # Sort preferred books first
    bookmakers = sorted(
        bookmakers,
        key=lambda b: _PREF_BOOKS.index(b.get("key", "")) if b.get("key") in _PREF_BOOKS else 99,
    )

    for book in bookmakers:
        book_key = book.get("key", book.get("title", "unknown"))
        for market in book.get("markets") or []:
            mkey = market.get("key", "")
            # Friendly display name
            market_label = (
                mkey.replace("player_", "")
                .replace("_", " ")
                .title()
            )
            outcomes = market.get("outcomes") or []

            # Group outcomes by player name (description field or name)
            player_data: dict[str, dict] = {}
            for o in outcomes:
                player = o.get("description") or o.get("name") or "Unknown"
                side = (o.get("name") or "").lower()  # "Over" or "Under"
                price = o.get("price", 0)
                line = o.get("point", 0.0)

                if player not in player_data:
                    player_data[player] = {"line": line, "book": book_key}
                if "over" in side:
                    player_data[player]["over_odds"] = price
                elif "under" in side:
                    player_data[player]["under_odds"] = price
                if line:
                    player_data[player]["line"] = line

            for player, pdata in player_data.items():
                key = (player, market_label)
                if key not in results:  # first (preferred) book wins
                    results[key] = {
                        "player": player,
                        "market": market_label,
                        "line": pdata.get("line", 0.0),
                        "over_odds": pdata.get("over_odds", 0),
                        "under_odds": pdata.get("under_odds", 0),
                        "book": book_key,
                    }

    return list(results.values())


def format_props(props: list[dict], player_filter: str = "", limit: int = 15) -> str:
    """
    Format player props list into a readable Telegram-safe string.
    Optionally filter to a specific player name substring.
    """
    if not props:
        return ""

    if player_filter:
        pf = player_filter.lower()
        props = [p for p in props if pf in p["player"].lower()]

    if not props:
        return f"No props found for player matching '{player_filter}'."

    props = props[:limit]
    lines = []
    current_market = ""
    for p in sorted(props, key=lambda x: (x["market"], x["player"])):
        if p["market"] != current_market:
            current_market = p["market"]
            lines.append(f"\n<b>{current_market}</b>")
        over_str = f"{p['over_odds']:+d}" if p["over_odds"] else "—"
        under_str = f"{p['under_odds']:+d}" if p["under_odds"] else "—"
        lines.append(
            f"  {p['player'][:22]:<22}  O{p['line']}  "
            f"Over {over_str}  /  Under {under_str}  [{p['book']}]"
        )
    return "\n".join(lines)
