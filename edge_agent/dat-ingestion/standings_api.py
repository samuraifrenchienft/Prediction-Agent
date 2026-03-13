"""
Standings API
=============
Fetches current league standings and top-25 rankings from ESPN's public API.
Fetches championship market odds from Polymarket Gamma API.

In-memory cache with 6-hour TTL — no DB needed, standings survive restarts
are not critical enough to warrant persistence.

Supported sports:
  nfl, nba, mlb, nhl    — full standings by conference/division
  cfb, cbb              — AP Poll / Coaches Poll top-25 rankings
  mls, epl              — top of table standings

Usage:
    client = StandingsClient()
    text   = client.format_standings("nba")          # Telegram-ready block
    odds   = client.get_championship_odds("nba")     # [(team, pct), ...]
    prompt = client.get_standings_context()           # AI system-prompt snippet
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "EDGE-Bot/1.0 standings-fetcher"}
_CACHE_TTL = 6 * 3600   # 6 hours

# ── ESPN endpoint map ──────────────────────────────────────────────────────────

_ESPN_STANDINGS: dict[str, str] = {
    "nfl": "https://site.api.espn.com/apis/v2/sports/football/nfl/standings",
    "nba": "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings",
    "mlb": "https://site.api.espn.com/apis/v2/sports/baseball/mlb/standings",
    "nhl": "https://site.api.espn.com/apis/v2/sports/hockey/nhl/standings",
    "mls": "https://site.api.espn.com/apis/v2/sports/soccer/usa.1/standings",
    "epl": "https://site.api.espn.com/apis/v2/sports/soccer/eng.1/standings",
}

_ESPN_RANKINGS: dict[str, str] = {
    "cfb": "https://site.api.espn.com/apis/v2/sports/football/college-football/rankings",
    "cbb": "https://site.api.espn.com/apis/v2/sports/basketball/mens-college-basketball/rankings",
}

# ── Polymarket championship search keywords ────────────────────────────────────

_CHAMP_SEARCHES: dict[str, str] = {
    "nba":  "nba champion",
    "nfl":  "super bowl",
    "mlb":  "world series winner",
    "nhl":  "stanley cup",
    "epl":  "premier league winner",
    "cfb":  "college football playoff",
    "cbb":  "march madness",
    "mls":  "mls cup",
}

_SPORT_EMOJI: dict[str, str] = {
    "nfl": "🏈", "nba": "🏀", "mlb": "⚾", "nhl": "🏒",
    "cfb": "🎓🏈", "cbb": "🎓🏀", "mls": "⚽", "epl": "🏴󠁧󠁢󠁥󠁮󠁧󠁿",
}

_GAMMA_BASE = "https://gamma-api.polymarket.com/markets"


class StandingsClient:
    """
    Standings and championship odds fetcher.
    All data is cached in memory — call clear_cache() to force a refresh.
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, Any]] = {}   # key → (ts, data)

    # ── Cache helpers ─────────────────────────────────────────────────────────

    def _get(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry and (time.time() - entry[0]) < _CACHE_TTL:
            return entry[1]
        return None

    def _set(self, key: str, value: Any) -> None:
        self._cache[key] = (time.time(), value)

    def clear_cache(self) -> None:
        self._cache.clear()

    # ── ESPN standings (NFL / NBA / MLB / NHL / MLS / EPL) ────────────────────

    def _fetch_standings_espn(self, sport: str) -> list[dict]:
        """
        Returns list of dicts: {group, rank, team, record, pct, games_back, playoff_seed}
        group = conference/division name
        """
        cached = self._get(f"standings_{sport}")
        if cached is not None:
            return cached

        url = _ESPN_STANDINGS.get(sport)
        if not url:
            return []

        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            raw = resp.json()
        except Exception as exc:
            log.warning("[Standings] ESPN %s fetch failed: %s", sport.upper(), exc)
            return []

        result: list[dict] = []
        for group in raw.get("standings", []):
            group_name = group.get("name", "")
            for idx, entry in enumerate(group.get("standings", []), start=1):
                team_info = entry.get("team", {})
                team_name = team_info.get("displayName", team_info.get("name", "?"))
                stats = {s["name"]: s.get("displayValue", s.get("value", "")) for s in entry.get("stats", [])}
                record   = stats.get("overall",    stats.get("playoffSeed", ""))
                pct      = stats.get("winPercent", stats.get("pointsFor",   ""))
                gb       = stats.get("gamesBack",  "")
                result.append({
                    "group":  group_name,
                    "rank":   idx,
                    "team":   team_name,
                    "record": str(record),
                    "pct":    str(pct),
                    "gb":     str(gb),
                })

        self._set(f"standings_{sport}", result)
        log.info("[Standings] ESPN %s: %d entries cached", sport.upper(), len(result))
        return result

    # ── ESPN rankings (CFB / CBB) ──────────────────────────────────────────────

    def _fetch_rankings_espn(self, sport: str) -> list[dict]:
        """
        Returns list of dicts: {rank, team, record, poll}
        Uses the AP Poll by default; falls back to first available poll.
        """
        cached = self._get(f"rankings_{sport}")
        if cached is not None:
            return cached

        url = _ESPN_RANKINGS.get(sport)
        if not url:
            return []

        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            raw = resp.json()
        except Exception as exc:
            log.warning("[Standings] ESPN %s rankings fetch failed: %s", sport.upper(), exc)
            return []

        polls = raw.get("rankings", [])
        # Prefer AP Poll
        ap_poll = next((p for p in polls if "AP" in p.get("name", "")), None)
        poll = ap_poll or (polls[0] if polls else None)
        if not poll:
            return []

        poll_name = poll.get("name", "Poll")
        result: list[dict] = []
        for entry in poll.get("ranks", [])[:25]:
            team_info = entry.get("team", {})
            result.append({
                "rank":   entry.get("current", 0),
                "team":   team_info.get("displayName", team_info.get("location", "?")) + " " + team_info.get("nickname", ""),
                "record": entry.get("recordSummary", ""),
                "poll":   poll_name,
            })

        self._set(f"rankings_{sport}", result)
        log.info("[Standings] %s %s: %d teams", sport.upper(), poll_name, len(result))
        return result

    # ── Polymarket championship odds ──────────────────────────────────────────

    def get_championship_odds(self, sport: str) -> list[tuple[str, float]]:
        """
        Query Polymarket Gamma API for the championship market for this sport.
        Returns list of (team_name, probability) tuples sorted by prob desc.
        Top 6 only. Returns [] on failure.
        """
        cached = self._get(f"champ_{sport}")
        if cached is not None:
            return cached

        keyword = _CHAMP_SEARCHES.get(sport)
        if not keyword:
            return []

        try:
            resp = requests.get(
                _GAMMA_BASE,
                params={"search": keyword, "active": "true", "closed": "false", "limit": 5},
                headers=_HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            markets = resp.json()
        except Exception as exc:
            log.warning("[Standings] Polymarket champ search (%s) failed: %s", keyword, exc)
            return []

        import json as _json
        result: list[tuple[str, float]] = []
        for mkt in markets:
            try:
                outcomes      = _json.loads(mkt.get("outcomes", "[]"))
                outcome_prices = _json.loads(mkt.get("outcomePrices", "[]"))
                if not outcomes or not outcome_prices:
                    continue
                for name, price in zip(outcomes, outcome_prices):
                    prob = float(price)
                    if name.lower() not in ("yes", "no") and prob > 0.01:
                        result.append((name, prob))
            except Exception:
                continue
            if result:
                break   # use first market that has named outcomes

        result.sort(key=lambda x: x[1], reverse=True)
        result = result[:6]
        self._set(f"champ_{sport}", result)
        return result

    # ── Formatted output ──────────────────────────────────────────────────────

    def format_standings(self, sport: str) -> str:
        """
        Return a Telegram-ready standings/rankings block for the given sport.
        """
        sport = sport.lower()
        emoji = _SPORT_EMOJI.get(sport, "📊")

        # ── Rankings sports (CFB / CBB) ───────────────────────────────────────
        if sport in _ESPN_RANKINGS:
            entries = self._fetch_rankings_espn(sport)
            if not entries:
                return f"{emoji} {sport.upper()} rankings unavailable right now."
            poll    = entries[0].get("poll", "AP Poll") if entries else "Poll"
            label   = "College Football" if sport == "cfb" else "College Basketball"
            lines   = [f"{emoji} <b>{label} — {poll} Top 25</b>\n"]
            for e in entries[:15]:   # show top 15 in Telegram
                rec = f" ({e['record']})" if e.get("record") else ""
                lines.append(f"  {e['rank']:>2}. {e['team']}{rec}")
            # Championship odds
            odds = self.get_championship_odds(sport)
            if odds:
                lines.append("\n🏆 <b>Championship Odds (Polymarket)</b>")
                for team, prob in odds[:5]:
                    lines.append(f"  • {team}: {prob:.0%}")
            return "\n".join(lines)

        # ── Standings sports ──────────────────────────────────────────────────
        if sport not in _ESPN_STANDINGS:
            return f"❌ Unknown sport: {sport}. Try: nfl, nba, mlb, nhl, cfb, cbb, mls, epl"

        entries = self._fetch_standings_espn(sport)
        if not entries:
            return f"{emoji} {sport.upper()} standings unavailable right now."

        # Group by conference/division
        groups: dict[str, list[dict]] = {}
        for e in entries:
            groups.setdefault(e["group"], []).append(e)

        sport_label = {
            "nfl": "NFL", "nba": "NBA", "mlb": "MLB", "nhl": "NHL",
            "mls": "MLS", "epl": "Premier League",
        }.get(sport, sport.upper())

        lines = [f"{emoji} <b>{sport_label} Standings</b>\n"]
        for group_name, group_entries in list(groups.items())[:6]:   # max 6 groups
            lines.append(f"<b>{group_name}</b>")
            for e in group_entries[:8]:  # top 8 per group
                gb_str = f"  GB: {e['gb']}" if e.get("gb") and e["gb"] not in ("", "0", "0.0") else ""
                lines.append(f"  {e['rank']:>2}. {e['team']}  {e['record']}{gb_str}")
            lines.append("")

        # Championship odds
        odds = self.get_championship_odds(sport)
        if odds:
            lines.append("🏆 <b>Championship Odds (Polymarket)</b>")
            for team, prob in odds[:5]:
                lines.append(f"  • {team}: {prob:.0%}")

        return "\n".join(lines)

    def get_standings_context(self, sports: list[str] | None = None) -> str:
        """
        Return a compact standings summary for AI system-prompt injection.
        Covers the top 3 teams per conference + championship leader.
        """
        if sports is None:
            sports = ["nba", "nfl", "mlb", "nhl"]

        lines: list[str] = []
        for sport in sports:
            emoji = _SPORT_EMOJI.get(sport, "")
            # Championship leader only
            odds = self.get_championship_odds(sport)
            if odds:
                leader, prob = odds[0]
                lines.append(f"{emoji} {sport.upper()} champion favorite: {leader} ({prob:.0%} on Polymarket)")
        if lines:
            return "\n[Current Championship Favorites]\n" + "\n".join(lines)
        return ""
