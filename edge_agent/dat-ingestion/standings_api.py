"""
Standings API
=============
Fetches current league standings and top-25 rankings from ESPN's public API.
Fetches championship market odds from Polymarket Gamma API.
Fetches F1 standings via Jolpica (Ergast mirror) — free, no auth.
Fetches PGA Tour leaderboard via PGA Tour public CDN — free, no auth.

In-memory cache with 6-hour TTL — no DB needed, standings survive restarts
are not critical enough to warrant persistence.

Supported sports:
  nfl, nba, mlb, nhl    — full standings by conference/division
  cfb, cbb              — AP Poll top-25 rankings
  ncaaw                 — Women's CBB AP Poll top-25 rankings
  mls, epl              — top of table standings
  wnba                  — WNBA conference standings
  laliga, bundesliga, seriea, ligue1, ucl — European soccer standings
  f1                    — F1 Driver + Constructor standings (Jolpica/Ergast)
  pga                   — PGA Tour current tournament leaderboard

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
    "nfl":        "https://site.api.espn.com/apis/v2/sports/football/nfl/standings",
    "nba":        "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings",
    "mlb":        "https://site.api.espn.com/apis/v2/sports/baseball/mlb/standings",
    "nhl":        "https://site.api.espn.com/apis/v2/sports/hockey/nhl/standings",
    "wnba":       "https://site.api.espn.com/apis/v2/sports/basketball/wnba/standings",
    "mls":        "https://site.api.espn.com/apis/v2/sports/soccer/usa.1/standings",
    "epl":        "https://site.api.espn.com/apis/v2/sports/soccer/eng.1/standings",
    "laliga":     "https://site.api.espn.com/apis/v2/sports/soccer/esp.1/standings",
    "bundesliga": "https://site.api.espn.com/apis/v2/sports/soccer/ger.1/standings",
    "seriea":     "https://site.api.espn.com/apis/v2/sports/soccer/ita.1/standings",
    "ligue1":     "https://site.api.espn.com/apis/v2/sports/soccer/fra.1/standings",
    "ucl":        "https://site.api.espn.com/apis/v2/sports/soccer/uefa.champions/standings",
}

_ESPN_RANKINGS: dict[str, str] = {
    "cfb":   "https://site.api.espn.com/apis/v2/sports/football/college-football/rankings",
    "cbb":   "https://site.api.espn.com/apis/v2/sports/basketball/mens-college-basketball/rankings",
    "ncaaw": "https://site.api.espn.com/apis/v2/sports/basketball/womens-college-basketball/rankings",
}

# ── External free APIs ─────────────────────────────────────────────────────────

# Jolpica — community Ergast mirror (Ergast shut down Dec 2024)
_JOLPICA_F1_DRIVERS      = "https://api.jolpi.ca/ergast/f1/current/driverStandings.json"
_JOLPICA_F1_CONSTRUCTORS = "https://api.jolpi.ca/ergast/f1/current/constructorStandings.json"

# PGA Tour public CDN — current tournament leaderboard (no auth)
_PGA_LEADERBOARD = "https://statdata.pgatour.com/r/current/leaderboard-v2mini.json"

# ── Polymarket championship search keywords ────────────────────────────────────

_CHAMP_SEARCHES: dict[str, str] = {
    "nba":        "nba champion",
    "nfl":        "super bowl",
    "mlb":        "world series winner",
    "nhl":        "stanley cup",
    "wnba":       "wnba champion",
    "epl":        "premier league winner",
    "laliga":     "la liga winner",
    "bundesliga": "bundesliga winner",
    "seriea":     "serie a winner",
    "ligue1":     "ligue 1 winner",
    "ucl":        "champions league winner",
    "cfb":        "college football playoff",
    "cbb":        "march madness",
    "ncaaw":      "women's march madness",
    "mls":        "mls cup",
    "f1":         "formula 1 world champion",
    "pga":        "masters winner",
}

_SPORT_EMOJI: dict[str, str] = {
    "nfl": "🏈", "nba": "🏀", "mlb": "⚾", "nhl": "🏒",
    "cfb": "🎓🏈", "cbb": "🎓🏀", "ncaaw": "🎓🏀♀️",
    "mls": "⚽", "epl": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "wnba": "🏀♀️",
    "laliga": "🇪🇸⚽", "bundesliga": "🇩🇪⚽", "seriea": "🇮🇹⚽",
    "ligue1": "🇫🇷⚽", "ucl": "🌟⚽",
    "f1": "🏎️", "pga": "⛳",
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

        _SOCCER_SPORTS = {"mls", "epl", "laliga", "bundesliga", "seriea", "ligue1", "ucl"}
        is_soccer = sport in _SOCCER_SPORTS

        result: list[dict] = []
        for group in raw.get("standings", []):
            group_name = group.get("name", "")
            for idx, entry in enumerate(group.get("standings", []), start=1):
                team_info = entry.get("team", {})
                team_name = team_info.get("displayName", team_info.get("name", "?"))
                stats = {s["name"]: s.get("displayValue", s.get("value", "")) for s in entry.get("stats", [])}

                if is_soccer:
                    # Soccer metrics: table points, W-D-L, goal difference
                    pts = stats.get("points", stats.get("pts", ""))
                    gp  = stats.get("gamesPlayed", stats.get("gp", ""))
                    w   = stats.get("wins",   stats.get("w", ""))
                    d   = stats.get("ties",   stats.get("d", stats.get("draws", "")))
                    l   = stats.get("losses", stats.get("l", ""))
                    gd  = stats.get("pointDifferential", stats.get("goalDifference", stats.get("gd", "")))
                    wdl = f"{w}-{d}-{l}" if all([w, d, l]) else stats.get("overall", "")
                    record = f"{pts}pts  {wdl}" if pts else wdl
                    pct = f"GD:{gd}" if gd else ""
                    gb  = f"GP:{gp}" if gp else ""
                else:
                    record = stats.get("overall",    stats.get("playoffSeed", ""))
                    pct    = stats.get("winPercent", stats.get("pointsFor",   ""))
                    gb     = stats.get("gamesBack",  "")

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

    # ── F1 standings (Jolpica / Ergast mirror) ───────────────────────────────

    def _fetch_f1_standings(self) -> dict[str, list[dict]]:
        """
        Returns {'drivers': [...], 'constructors': [...]}
        Each driver: {rank, driver, nationality, team, points}
        Each constructor: {rank, team, points}
        """
        cached = self._get("f1_standings")
        if cached is not None:
            return cached

        result: dict[str, list[dict]] = {"drivers": [], "constructors": []}

        # Driver standings
        try:
            resp = requests.get(_JOLPICA_F1_DRIVERS, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            raw = resp.json()
            standings_list = (
                raw.get("MRData", {})
                   .get("StandingsTable", {})
                   .get("StandingsLists", [])
            )
            if standings_list:
                for entry in standings_list[0].get("DriverStandings", [])[:20]:
                    driver = entry.get("Driver", {})
                    constructor = (entry.get("Constructors") or [{}])[0]
                    result["drivers"].append({
                        "rank":        int(entry.get("position", 0)),
                        "driver":      f"{driver.get('givenName','')} {driver.get('familyName','')}".strip(),
                        "nationality": driver.get("nationality", ""),
                        "team":        constructor.get("name", ""),
                        "points":      entry.get("points", "0"),
                    })
        except Exception as exc:
            log.warning("[Standings] F1 driver standings failed: %s", exc)

        # Constructor standings
        try:
            resp = requests.get(_JOLPICA_F1_CONSTRUCTORS, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            raw = resp.json()
            standings_list = (
                raw.get("MRData", {})
                   .get("StandingsTable", {})
                   .get("StandingsLists", [])
            )
            if standings_list:
                for entry in standings_list[0].get("ConstructorStandings", [])[:10]:
                    constructor = entry.get("Constructor", {})
                    result["constructors"].append({
                        "rank":   int(entry.get("position", 0)),
                        "team":   constructor.get("name", ""),
                        "points": entry.get("points", "0"),
                    })
        except Exception as exc:
            log.warning("[Standings] F1 constructor standings failed: %s", exc)

        if result["drivers"] or result["constructors"]:
            self._set("f1_standings", result)
        return result

    # ── PGA Tour leaderboard ──────────────────────────────────────────────────

    def _fetch_pga_leaderboard(self) -> dict:
        """
        Returns {'tournament': str, 'round': str, 'players': [...]}
        Each player: {rank, name, total, today, thru}
        Uses PGA Tour public CDN — no auth required.
        """
        cached = self._get("pga_leaderboard")
        if cached is not None:
            return cached

        result: dict = {"tournament": "No active tournament", "round": "", "players": []}

        try:
            resp = requests.get(_PGA_LEADERBOARD, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            raw = resp.json()

            leaderboard = raw.get("leaderboard", {})
            result["tournament"] = leaderboard.get("tournament_name", "Current Tournament")
            result["round"]      = f"Round {leaderboard.get('current_round', '?')}"

            for row in leaderboard.get("players", [])[:20]:
                player = row.get("player_bio", {})
                result["players"].append({
                    "rank":   row.get("current_position", "?"),
                    "name":   f"{player.get('first_name','')} {player.get('last_name','')}".strip(),
                    "total":  row.get("total", "E"),
                    "today":  row.get("today", "E"),
                    "thru":   row.get("thru", "F"),
                })

            self._set("pga_leaderboard", result)
        except Exception as exc:
            log.warning("[Standings] PGA leaderboard fetch failed: %s", exc)

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

        # ── F1 ────────────────────────────────────────────────────────────────
        if sport == "f1":
            data = self._fetch_f1_standings()
            drivers = data.get("drivers", [])
            constructors = data.get("constructors", [])
            if not drivers and not constructors:
                return f"{emoji} F1 standings unavailable right now."
            lines = [f"{emoji} <b>Formula 1 — Current Season Standings</b>\n"]
            if drivers:
                lines.append("<b>Driver Championship</b>")
                for d in drivers[:10]:
                    lines.append(f"  {d['rank']:>2}. {d['driver']} ({d['team']}) — {d['points']} pts")
            if constructors:
                lines.append("\n<b>Constructor Championship</b>")
                for c in constructors[:8]:
                    lines.append(f"  {c['rank']:>2}. {c['team']} — {c['points']} pts")
            odds = self.get_championship_odds("f1")
            if odds:
                lines.append("\n🏆 <b>Championship Odds (Polymarket)</b>")
                for team, prob in odds[:5]:
                    lines.append(f"  • {team}: {prob:.0%}")
            return "\n".join(lines)

        # ── PGA Tour ──────────────────────────────────────────────────────────
        if sport == "pga":
            data = self._fetch_pga_leaderboard()
            players = data.get("players", [])
            tournament = data.get("tournament", "PGA Tour")
            rnd = data.get("round", "")
            if not players:
                return f"{emoji} PGA leaderboard unavailable right now (no active tournament)."
            lines = [f"{emoji} <b>{tournament}</b> — {rnd}\n"]
            for p in players[:15]:
                thru = f" ({p['thru']})" if p.get("thru") else ""
                today = f"  Today: {p['today']}" if p.get("today") and p["today"] not in ("E", "0") else ""
                lines.append(f"  {str(p['rank']):>3}. {p['name']}  {p['total']}{thru}{today}")
            odds = self.get_championship_odds("pga")
            if odds:
                lines.append("\n🏆 <b>Major Winner Odds (Polymarket)</b>")
                for name, prob in odds[:5]:
                    lines.append(f"  • {name}: {prob:.0%}")
            return "\n".join(lines)

        # ── Rankings sports (CFB / CBB / NCAAW) ──────────────────────────────
        if sport in _ESPN_RANKINGS:
            entries = self._fetch_rankings_espn(sport)
            if not entries:
                return f"{emoji} {sport.upper()} rankings unavailable right now."
            poll = entries[0].get("poll", "AP Poll") if entries else "Poll"
            label = {
                "cfb":   "College Football",
                "cbb":   "College Basketball (Men's)",
                "ncaaw": "College Basketball (Women's)",
            }.get(sport, sport.upper())
            lines = [f"{emoji} <b>{label} — {poll} Top 25</b>\n"]
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
        all_known = set(_ESPN_STANDINGS) | {"f1", "pga"} | set(_ESPN_RANKINGS)
        if sport not in _ESPN_STANDINGS:
            valid_str = ", ".join(sorted(all_known))
            return f"❌ Unknown sport: {sport}. Try: {valid_str}"

        entries = self._fetch_standings_espn(sport)
        if not entries:
            return f"{emoji} {sport.upper()} standings unavailable right now."

        # Group by conference/division
        groups: dict[str, list[dict]] = {}
        for e in entries:
            groups.setdefault(e["group"], []).append(e)

        _SOCCER_SPORTS = {"mls", "epl", "laliga", "bundesliga", "seriea", "ligue1", "ucl"}
        is_soccer = sport in _SOCCER_SPORTS

        sport_label = {
            "nfl": "NFL", "nba": "NBA", "mlb": "MLB", "nhl": "NHL",
            "wnba": "WNBA", "mls": "MLS", "epl": "Premier League",
            "laliga": "La Liga", "bundesliga": "Bundesliga",
            "seriea": "Serie A", "ligue1": "Ligue 1", "ucl": "Champions League",
        }.get(sport, sport.upper())

        lines = [f"{emoji} <b>{sport_label} Standings</b>\n"]

        if is_soccer:
            # Soccer-specific header (no "games back" concept; show GD instead)
            lines.append("<code>Pos  Team                Pts   W-D-L   GD  GP</code>")

        for group_name, group_entries in list(groups.items())[:6]:   # max 6 groups
            if group_name:
                lines.append(f"<b>{group_name}</b>")
            for e in group_entries[:8]:  # top 8 per group
                if is_soccer:
                    # record = "67pts  10-7-1", pct = "GD:+23", gb = "GP:38"
                    gd_str = f"  {e['pct']}" if e.get("pct") and e["pct"] not in ("", "GD:") else ""
                    gp_str = f"  {e['gb']}"  if e.get("gb")  and e["gb"]  not in ("", "GP:") else ""
                    lines.append(f"  {e['rank']:>2}. {e['team']:<22} {e['record']}{gd_str}{gp_str}")
                else:
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
