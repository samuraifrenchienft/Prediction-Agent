"""
Injury Report Client — ESPN + NBA Official PDF + SQLite persistence
====================================================================

Sports covered: NBA, NFL, NHL

Source 1  ESPN Unofficial API  (NBA + NFL + NHL, JSON, ~1hr freshness, no auth)
  https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries
  https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries
  https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries

Source 2  NBA Official CDN PDF  (NBA only, league-mandated, 15-min intervals)
  https://ak-static.cms.nba.com/referee/injury/Injury-Report_{date}_{HH}_{MM}{AM/PM}.pdf
  Parsed with pdfplumber (pip install pdfplumber).
  Used to verify / upgrade ESPN statuses for NBA markets.
  Silently skipped if pdfplumber is not installed.

Architecture
------------
• The *refresh job* in run_edge_bot.py calls fetch_and_store() every 4 hours.
  That is the ONLY place that makes live HTTP calls to injury APIs.
• Market scans call build_injury_catalysts() which reads from the SQLite cache.
  Zero HTTP calls happen during scans.
• The in-memory _cache dict provides a 30-minute hot-path for repeated calls
  within the same refresh cycle (avoids hitting SQLite on every market).
• Change detection: fetch_and_store() compares new records against previous
  cache and stores proactive alerts when a player's status worsens.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import re
import time
import io
from datetime import datetime, timedelta
from typing import Any

import requests

# File cache dir shared with news_api.py
_CACHE_DIR = ".cache"
os.makedirs(_CACHE_DIR, exist_ok=True)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy import of InjuryCache — avoids circular import at module load time
# ---------------------------------------------------------------------------

def _get_injury_cache():
    mod = importlib.import_module("edge_agent.memory.injury_cache")
    return mod.InjuryCache()


def _get_news_client():
    """Lazy import of NewsAPIClient — same module dir, avoids circular import."""
    mod = importlib.import_module(".dat-ingestion.news_api", "edge_agent")
    return mod.NewsAPIClient()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_ESPN_NBA = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
_ESPN_NFL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
_ESPN_NHL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries"

_NBA_CDN = (
    "https://ak-static.cms.nba.com/referee/injury/"
    "Injury-Report_{date}_{hh}_{mm}{ampm}.pdf"
)

# Statuses that represent a player who may not play
_INJURED_STATUSES = {
    "Out", "Doubtful", "Questionable", "Day-To-Day", "Suspension",
    "Injured Reserve",   # NHL-specific — definite miss, 7+ day minimum
}
_PDF_STATUSES = ["Out", "Doubtful", "Questionable", "Day-To-Day", "Suspension"]

# Catalyst direction/confidence/quality per status severity.
# Index 0 of _SEVERITY_ORDER = most severe (used for change detection ordering).
_SEVERITY: dict[str, dict[str, float]] = {
    "Out":             {"direction": -0.90, "confidence": 0.92, "quality": 0.90},
    "Injured Reserve": {"direction": -0.90, "confidence": 0.95, "quality": 0.92},  # NHL IR = certain miss
    "Suspension":      {"direction": -0.80, "confidence": 0.88, "quality": 0.88},
    "Doubtful":        {"direction": -0.65, "confidence": 0.78, "quality": 0.80},
    "Questionable":    {"direction": -0.40, "confidence": 0.62, "quality": 0.72},
    "Day-To-Day":      {"direction": -0.25, "confidence": 0.50, "quality": 0.60},
}
_SEVERITY_ORDER = list(_SEVERITY.keys())  # Out=0 (most severe) … Day-To-Day=5

# Key positions whose absence moves team win probability
_KEY_NBA = {"PG", "SG", "SF", "PF", "C"}
_KEY_NFL = {"QB", "RB", "WR", "TE"}
_KEY_NHL = {"C", "LW", "RW", "D", "G"}   # all positions matter in hockey; G = goalie (critical)

# Hot-path in-memory cache TTL (30 min)
_HOT_TTL = 1800

# ---------------------------------------------------------------------------
# Sleeper API — free, no auth, real-time NFL/NBA injury statuses
# Used as secondary cross-reference source for NFL and NBA.
# Docs: https://docs.sleeper.com/#get-all-players
# ---------------------------------------------------------------------------

_SLEEPER_NFL = "https://api.sleeper.app/v1/players/nfl"
_SLEEPER_NBA = "https://api.sleeper.app/v1/players/nba"
_SLEEPER_TTL = 21600  # 6h file cache — response is large (~8MB), avoid hammering

# Sleeper uses different status labels — map them to our standard set
_SLEEPER_STATUS_MAP: dict[str, str] = {
    "Out":          "Out",
    "Doubtful":     "Doubtful",
    "Questionable": "Questionable",
    "IR":           "Injured Reserve",   # NFL placed on IR
    "DNR":          "Out",               # Did Not Return (game-time scratch)
    "NF":           "Suspension",        # Non-football related (suspension)
    "COV":          "Out",               # COVID protocols (legacy)
    "PUP":          "Out",               # Physically unable to perform
    "NFI":          "Out",               # Non-football injury (camp)
}

# ---------------------------------------------------------------------------
# News headline keywords for star player spot-check
# Only ~10-20 star players get checked per refresh — quota-safe on 100/day tier
# ---------------------------------------------------------------------------

_NEWS_RETURN_KW = frozenset({
    "return", "cleared", "back at practice", "off injury report",
    "upgraded", "probable", "removed from", "no longer listed",
    "available", "participat", "full practice", "expected to play",
    "ready to play", "cleared to play",
})
_NEWS_CONFIRM_KW = frozenset({
    "out", "ruled out", "injured reserve", "placed on ir",
    "questionable", "doubtful", "miss", "injured", "day-to-day", "dtd",
    "will not play", "won't play", "sits out", "sidelined", "listed as out",
})

# ---------------------------------------------------------------------------
# Star player registry — direction & quality multiplier for marquee players
# ---------------------------------------------------------------------------
# Keys are lowercase name fragments; first match wins.
# Goalies receive high multipliers since one goalie = the entire position.

_STAR_MULTIPLIERS: dict[str, float] = {
    # ── NBA ──────────────────────────────────────────────────────────────────
    "lebron james":            1.20,
    "giannis antetokounmpo":   1.18,
    "nikola jokic":            1.18,
    "stephen curry":           1.15,
    "steph curry":             1.15,
    "kevin durant":            1.15,
    "joel embiid":             1.15,
    "luka doncic":             1.15,
    "shai gilgeous-alexander": 1.15,
    "victor wembanyama":       1.15,
    "jayson tatum":            1.12,
    "damian lillard":          1.12,
    "anthony davis":           1.12,
    "ja morant":               1.12,
    "tyrese haliburton":       1.10,
    "donovan mitchell":        1.10,
    "devin booker":            1.10,
    "jimmy butler":            1.10,
    "paolo banchero":          1.08,
    "bam adebayo":             1.08,
    # ── NFL ──────────────────────────────────────────────────────────────────
    "patrick mahomes":         1.25,
    "josh allen":              1.22,
    "lamar jackson":           1.22,
    "joe burrow":              1.18,
    "jalen hurts":             1.18,
    "christian mccaffrey":     1.15,
    "trevor lawrence":         1.12,
    "justin herbert":          1.12,
    "c.j. stroud":             1.12,
    "cj stroud":               1.12,
    "tua tagovailoa":          1.12,
    "ceedee lamb":             1.10,
    "justin jefferson":        1.10,
    "tyreek hill":             1.10,
    "travis kelce":            1.10,
    "davante adams":           1.08,
    "cooper kupp":             1.08,
    "stefon diggs":            1.08,
    "derrick henry":           1.08,
    # ── NHL — Skaters ────────────────────────────────────────────────────────
    "connor mcdavid":          1.28,
    "nathan mackinnon":        1.25,
    "auston matthews":         1.22,
    "leon draisaitl":          1.20,
    "david pastrnak":          1.18,
    "nikita kucherov":         1.18,
    "cale makar":              1.18,
    "matthew tkachuk":         1.15,
    "aleksander barkov":       1.15,
    "adam fox":                1.15,
    "tage thompson":           1.12,
    "jack hughes":             1.12,
    "tim stutzle":             1.12,
    "brady tkachuk":           1.12,
    "trevor zegras":           1.10,
    "roman josi":              1.12,
    "kirill kaprizov":         1.12,
    "mitchell marner":         1.10,
    "william nylander":        1.10,
    # ── NHL — Goalies (starting caliber) — very high multiplier ──────────────
    "connor hellebuyck":       1.25,
    "igor shesterkin":         1.25,
    "andrei vasilevskiy":      1.22,
    "thatcher demko":          1.18,
    "jacob markstrom":         1.18,
    "juuse saros":             1.18,
    "ilya sorokin":            1.18,
    "sergei bobrovsky":        1.15,
    "jake oettinger":          1.15,
    "linus ullmark":           1.15,
    "adin hill":               1.15,
    "marc-andre fleury":       1.12,
}

# ---------------------------------------------------------------------------
# Team alias maps — ESPN full display name → question keywords
# ---------------------------------------------------------------------------

_NBA_TEAM_ALIASES: dict[str, list[str]] = {
    "atlanta hawks":           ["hawks", "atl", "atlanta hawks", "atlanta"],
    "boston celtics":          ["celtics", "bos", "boston celtics", "boston"],
    "brooklyn nets":           ["nets", "bkn", "brooklyn nets", "brooklyn"],
    "charlotte hornets":       ["hornets", "cha", "charlotte hornets", "charlotte"],
    "chicago bulls":           ["bulls", "chi", "chicago bulls", "chicago"],
    "cleveland cavaliers":     ["cavaliers", "cavs", "cle", "cleveland"],
    "dallas mavericks":        ["mavericks", "mavs", "dal", "dallas"],
    "denver nuggets":          ["nuggets", "den", "denver"],
    "detroit pistons":         ["pistons", "det", "detroit"],
    "golden state warriors":   ["warriors", "gsw", "golden state", "golden state warriors"],
    "houston rockets":         ["rockets", "hou", "houston"],
    "indiana pacers":          ["pacers", "ind", "indiana"],
    "los angeles clippers":    ["clippers", "lac", "la clippers", "los angeles clippers"],
    "los angeles lakers":      ["lakers", "lal", "la lakers", "los angeles lakers"],
    "memphis grizzlies":       ["grizzlies", "mem", "memphis"],
    "miami heat":              ["heat", "mia", "miami heat", "miami"],
    "milwaukee bucks":         ["bucks", "mil", "milwaukee"],
    "minnesota timberwolves":  ["timberwolves", "wolves", "min", "minnesota"],
    "new orleans pelicans":    ["pelicans", "nop", "new orleans pelicans", "new orleans"],
    "new york knicks":         ["knicks", "nyk", "new york knicks", "new york"],
    "oklahoma city thunder":   ["thunder", "okc", "oklahoma city thunder", "oklahoma"],
    "orlando magic":           ["magic", "orl", "orlando"],
    "philadelphia 76ers":      ["76ers", "sixers", "phi", "philadelphia 76ers", "philadelphia"],
    "phoenix suns":            ["suns", "phx", "phoenix"],
    "portland trail blazers":  ["blazers", "trail blazers", "por", "portland"],
    "sacramento kings":        ["kings", "sac", "sacramento"],
    "san antonio spurs":       ["spurs", "sas", "san antonio"],
    "toronto raptors":         ["raptors", "tor", "toronto"],
    "utah jazz":               ["jazz", "uta", "utah"],
    "washington wizards":      ["wizards", "was", "washington wizards", "washington"],
}

_NFL_TEAM_ALIASES: dict[str, list[str]] = {
    "arizona cardinals":       ["cardinals", "ari", "arizona cardinals", "arizona"],
    "atlanta falcons":         ["falcons", "atl", "atlanta falcons", "atlanta"],
    "baltimore ravens":        ["ravens", "bal", "baltimore"],
    "buffalo bills":           ["bills", "buf", "buffalo"],
    "carolina panthers":       ["panthers", "car", "carolina"],
    "chicago bears":           ["bears", "chi", "chicago bears", "chicago"],
    "cincinnati bengals":      ["bengals", "cin", "cincinnati"],
    "cleveland browns":        ["browns", "cle", "cleveland"],
    "dallas cowboys":          ["cowboys", "dal", "dallas cowboys", "dallas"],
    "denver broncos":          ["broncos", "den", "denver"],
    "detroit lions":           ["lions", "det", "detroit"],
    "green bay packers":       ["packers", "gnb", "green bay", "gb packers"],
    "houston texans":          ["texans", "hou", "houston texans", "houston"],
    "indianapolis colts":      ["colts", "ind", "indianapolis"],
    "jacksonville jaguars":    ["jaguars", "jags", "jax", "jacksonville"],
    "kansas city chiefs":      ["chiefs", "kan", "kc chiefs", "kansas city"],
    "las vegas raiders":       ["raiders", "lv", "las vegas raiders", "las vegas"],
    "los angeles chargers":    ["chargers", "lac", "la chargers", "los angeles chargers"],
    "los angeles rams":        ["rams", "lar", "la rams", "los angeles rams"],
    "miami dolphins":          ["dolphins", "mia", "miami dolphins", "miami"],
    "minnesota vikings":       ["vikings", "min", "minnesota vikings", "minnesota"],
    "new england patriots":    ["patriots", "pats", "ne patriots", "new england"],
    "new orleans saints":      ["saints", "nol", "new orleans saints", "new orleans"],
    "new york giants":         ["giants", "nyg", "ny giants", "new york giants"],
    "new york jets":           ["jets", "nyj", "ny jets", "new york jets"],
    "philadelphia eagles":     ["eagles", "phi", "philadelphia eagles", "philadelphia"],
    "pittsburgh steelers":     ["steelers", "pit", "pittsburgh"],
    "san francisco 49ers":     ["49ers", "sf", "san francisco", "niners"],
    "seattle seahawks":        ["seahawks", "sea", "seattle"],
    "tampa bay buccaneers":    ["buccaneers", "bucs", "tb", "tampa bay", "tampa"],
    "tennessee titans":        ["titans", "ten", "tennessee"],
    "washington commanders":   ["commanders", "was", "washington commanders", "washington"],
}

_NHL_TEAM_ALIASES: dict[str, list[str]] = {
    "anaheim ducks":           ["ducks", "ana", "anaheim"],
    "boston bruins":           ["bruins", "bos", "boston"],
    "buffalo sabres":          ["sabres", "buf", "buffalo"],
    "calgary flames":          ["flames", "cgy", "calgary"],
    "carolina hurricanes":     ["hurricanes", "canes", "car", "carolina"],
    "chicago blackhawks":      ["blackhawks", "hawks", "chi", "chicago"],
    "colorado avalanche":      ["avalanche", "avs", "col", "colorado"],
    "columbus blue jackets":   ["blue jackets", "cbj", "columbus"],
    "dallas stars":            ["stars", "dal", "dallas"],
    "detroit red wings":       ["red wings", "det", "detroit"],
    "edmonton oilers":         ["oilers", "edm", "edmonton"],
    "florida panthers":        ["florida panthers", "fla", "florida"],
    "los angeles kings":       ["kings", "lak", "la kings", "los angeles kings"],
    "minnesota wild":          ["wild", "min", "minnesota wild", "minnesota"],
    "montreal canadiens":      ["canadiens", "habs", "mtl", "montreal"],
    "nashville predators":     ["predators", "preds", "nsh", "nashville"],
    "new jersey devils":       ["devils", "njd", "new jersey"],
    "new york islanders":      ["islanders", "nyi", "new york islanders"],
    "new york rangers":        ["rangers", "nyr", "new york rangers"],
    "ottawa senators":         ["senators", "sens", "ott", "ottawa"],
    "philadelphia flyers":     ["flyers", "phi", "philadelphia flyers"],
    "pittsburgh penguins":     ["penguins", "pens", "pit", "pittsburgh"],
    "san jose sharks":         ["sharks", "sjs", "san jose"],
    "seattle kraken":          ["kraken", "sea", "seattle"],
    "st. louis blues":         ["blues", "stl", "st louis", "st. louis"],
    "tampa bay lightning":     ["lightning", "bolts", "tbl", "tampa bay", "tampa"],
    "toronto maple leafs":     ["maple leafs", "leafs", "tor", "toronto"],
    "utah hockey club":        ["utah hockey", "utah hc", "utah"],
    "vancouver canucks":       ["canucks", "van", "vancouver"],
    "vegas golden knights":    ["golden knights", "knights", "vgk", "vegas"],
    "washington capitals":     ["capitals", "caps", "wsh", "washington capitals"],
    "winnipeg jets":           ["jets", "wpg", "winnipeg"],
}

# ---------------------------------------------------------------------------
# Sport keyword detection
# ---------------------------------------------------------------------------

_NBA_KW = {
    "lakers", "celtics", "warriors", "bucks", "heat", "nets", "knicks", "bulls", "suns",
    "nuggets", "clippers", "sixers", "76ers", "raptors", "mavericks", "mavs", "spurs",
    "pacers", "pistons", "hawks", "hornets", "magic", "thunder", "blazers", "jazz",
    "grizzlies", "pelicans", "wolves", "timberwolves", "kings", "rockets", "cavaliers",
    "cavs", "wizards", "nba", "basketball",
}
_NFL_KW = {
    "chiefs", "eagles", "cowboys", "patriots", "bengals", "ravens", "dolphins", "bills",
    "steelers", "browns", "titans", "colts", "texans", "jaguars", "broncos",
    "raiders", "chargers", "seahawks", "49ers", "rams", "falcons", "saints",
    "buccaneers", "packers", "bears", "lions", "vikings", "giants",
    "commanders", "football", "nfl",
    # "cardinals", "jets", "panthers" omitted — too ambiguous with NHL
}
_NHL_KW = {
    "nhl", "hockey", "stanley cup", "stanley",
    "oilers", "flames", "canucks", "maple leafs", "leafs", "senators", "canadiens", "habs",
    "bruins", "sabres", "rangers", "islanders", "devils", "flyers", "penguins", "capitals",
    "hurricanes", "canes", "blue jackets", "red wings", "blackhawks", "predators", "preds", "blues",
    "avalanche", "avs", "wild", "ducks", "sharks", "golden knights", "kraken",
    "lightning", "bolts",
    "goalie", "goalkeeper", "powerplay", "power play",
}


def detect_sport(text: str) -> str:
    """Return 'nba', 'nfl', or 'nhl' based on keywords in a market question."""
    t = text.lower()
    nba_score = sum(1 for k in _NBA_KW if k in t)
    nfl_score = sum(1 for k in _NFL_KW if k in t)
    nhl_score = sum(1 for k in _NHL_KW if k in t)
    best = max(nba_score, nfl_score, nhl_score)
    if best == 0:
        return "nba"  # safe default
    if nhl_score == best:
        return "nhl"
    if nfl_score == best:
        return "nfl"
    return "nba"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class InjuryAPIClient:
    """
    Multi-sport injury client with SQLite persistence.

    Normal flow:
        # Run by the refresh job every 4 hours:
        client.fetch_and_store("nba")
        client.fetch_and_store("nfl")
        client.fetch_and_store("nhl")

        # Run by scanner.collect() per sports market:
        cats = client.build_injury_catalysts("Will the Oilers win tonight?")
    """

    # Hot-path in-memory cache: sport → (timestamp, records)
    _hot_cache: dict[str, tuple[float, list[dict]]] = {}

    # Shared SQLite cache instance (created lazily)
    _db: Any = None

    def _get_db(self):
        if self._db is None:
            try:
                self._db = _get_injury_cache()
            except Exception as exc:
                log.warning("[InjuryAPI] Could not open injury cache DB: %s", exc)
        return self._db

    # ── Hot-path read ────────────────────────────────────────────────────────

    def _hot_get(self, sport: str) -> list[dict] | None:
        ts, data = self._hot_cache.get(sport, (0.0, []))
        if time.time() - ts < _HOT_TTL:
            return data
        return None

    def _hot_set(self, sport: str, records: list[dict]) -> None:
        self._hot_cache[sport] = (time.time(), records)

    # ── Source 1: ESPN ───────────────────────────────────────────────────────

    def _fetch_espn(self, sport: str) -> list[dict]:
        """ESPN unofficial injury API. NBA, NFL, and NHL all supported."""
        sport_lower = sport.lower()
        if sport_lower == "nba":
            url = _ESPN_NBA
        elif sport_lower == "nhl":
            url = _ESPN_NHL
        else:
            url = _ESPN_NFL

        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        raw = resp.json()

        records: list[dict] = []
        for team_block in raw.get("injuries", []):
            team = team_block.get("displayName", "")
            for inj in team_block.get("injuries", []):
                status = inj.get("status", "")
                if status not in _INJURED_STATUSES:
                    continue
                athlete = inj.get("athlete", {})
                details = inj.get("details", {})
                pos_raw = athlete.get("position", {})
                pos = pos_raw.get("abbreviation", "") if isinstance(pos_raw, dict) else str(pos_raw)
                records.append({
                    "player_name":   athlete.get("displayName", ""),
                    "team":          team,
                    "position":      pos.upper(),
                    "status":        status,
                    "injury_type":   details.get("type", ""),
                    "injury_detail": details.get("detail", ""),
                    "return_date":   details.get("returnDate", ""),
                    "comment":       inj.get("shortComment", ""),
                    "source_api":    "espn",
                    "sport":         sport_lower.upper(),
                })
        log.info("[InjuryAPI] ESPN %s: %d active injuries", sport.upper(), len(records))
        return records

    # ── Source 2: NBA Official CDN PDF ───────────────────────────────────────

    def _fetch_nba_official(self) -> dict[str, str]:
        try:
            import pdfplumber  # noqa
        except ImportError:
            log.debug("[InjuryAPI] pdfplumber not installed — skipping NBA official PDF")
            return {}
        records = self._fetch_nba_pdf()
        return {r["player_name_lower"]: r["status"] for r in records}

    def _fetch_nba_pdf(self) -> list[dict]:
        now = datetime.now()
        for mins_back in range(0, 300, 15):
            dt = now - timedelta(minutes=(now.minute % 15) + mins_back)
            dt = dt.replace(second=0, microsecond=0)
            url = _NBA_CDN.format(
                date=dt.strftime("%Y-%m-%d"),
                hh=f"{(dt.hour % 12) or 12:02d}",
                mm=f"{(dt.minute // 15) * 15:02d}",
                ampm="AM" if dt.hour < 12 else "PM",
            )
            try:
                resp = requests.get(url, headers=_HEADERS, timeout=10)
                if resp.status_code == 200:
                    log.debug("[InjuryAPI] NBA PDF found: %s", url)
                    return self._parse_nba_pdf(resp.content)
            except Exception:
                pass
        log.debug("[InjuryAPI] NBA CDN: no recent report found")
        return []

    @staticmethod
    def _parse_nba_pdf(pdf_bytes: bytes) -> list[dict]:
        import pdfplumber

        records: list[dict] = []
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    for line in text.splitlines():
                        line = line.strip()
                        for status in _PDF_STATUSES:
                            if status not in line:
                                continue
                            parts = line.split(status, 1)
                            raw_name = parts[0].strip()
                            reason   = parts[1].strip() if len(parts) > 1 else ""

                            match = re.search(
                                r"([A-Z][a-z]+(?:II|III|IV|Jr|Sr)?,\s*[A-Z][a-z]+)",
                                raw_name,
                            )
                            if not match:
                                continue
                            last_first = match.group(1)
                            parts2 = last_first.split(",", 1)
                            if len(parts2) == 2:
                                normalized = f"{parts2[1].strip()} {parts2[0].strip()}"
                            else:
                                normalized = last_first

                            records.append({
                                "player_name":       normalized,
                                "player_name_lower": normalized.lower(),
                                "status":            status,
                                "reason":            reason,
                            })
                            break
        except Exception as exc:
            log.warning("[InjuryAPI] NBA PDF parse error: %s", exc)

        return records

    # ── Secondary source: Sleeper API (NFL/NBA) ──────────────────────────────

    def _fetch_sleeper(self, sport: str) -> dict[str, dict]:
        """
        Return {full_name_lower: {"status": str, "depth_chart_order": int|None}}
        from the Sleeper player API.
        File-cached for 6 hours (response is ~8MB — we don't want to re-fetch
        on every scan cycle).  Returns {} for NHL (Sleeper doesn't cover it)
        or if the request fails.
        depth_chart_order == 1 means the player is the starter at their position.
        """
        sport_lower = sport.lower()
        if sport_lower not in ("nba", "nfl"):
            return {}

        cache_file = os.path.join(_CACHE_DIR, f"sleeper_{sport_lower}.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file) as f:
                    cached = json.load(f)
                if time.time() - cached.get("ts", 0) < _SLEEPER_TTL:
                    return cached.get("data", {})
            except Exception:
                pass

        url = _SLEEPER_NFL if sport_lower == "nfl" else _SLEEPER_NBA
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=25)
            resp.raise_for_status()
            raw = resp.json()
        except Exception as exc:
            log.warning("[InjuryAPI] Sleeper %s fetch failed: %s", sport.upper(), exc)
            return {}

        result: dict[str, dict] = {}
        for player_data in raw.values():
            if not isinstance(player_data, dict):
                continue
            full_name = player_data.get("full_name") or ""
            if not full_name:
                continue
            inj_status  = player_data.get("injury_status") or ""
            depth_order = player_data.get("depth_chart_order")
            # Normalise depth_chart_order to int or None
            try:
                depth_order = int(depth_order) if depth_order is not None else None
            except (TypeError, ValueError):
                depth_order = None
            normalized = _SLEEPER_STATUS_MAP.get(inj_status) if inj_status else None
            # Store every player that has injury status OR depth_chart_order == 1
            if normalized or depth_order == 1:
                result[full_name.lower()] = {
                    "status":             normalized,
                    "depth_chart_order":  depth_order,
                }

        try:
            with open(cache_file, "w") as f:
                json.dump({"ts": time.time(), "data": result}, f)
        except Exception:
            pass

        starters  = sum(1 for v in result.values() if v.get("depth_chart_order") == 1)
        log.info("[InjuryAPI] Sleeper %s: %d players cached (%d starters)",
                 sport.upper(), len(result), starters)
        return result

    # ── Star player news spot-check ───────────────────────────────────────────

    def _verify_stars_with_news(self, records: list[dict], sport: str) -> None:
        """
        Headline spot-check for star players only (those in _STAR_MULTIPLIERS).

        Queries GNews/NewsAPI for "{player} injury" and scans the headlines
        for return-signal vs confirm-signal keywords.  Mutates source_api
        in-place to flag conflicts or confirmation.

        Quota-safe: at most ~10-20 star players are ever injured at once,
        and each result is cached 2 hours, so this stays well under 100/day.
        Silently skips if news API is not configured.
        """
        try:
            news = _get_news_client()
        except Exception:
            return  # no news API key configured — skip silently

        for rec in records:
            player = rec.get("player_name", "")
            player_lower = player.lower()

            # Only check named star players — skip role players
            if not any(k in player_lower for k in _STAR_MULTIPLIERS):
                continue

            status = rec.get("status", "")

            # Per-player file cache — 2h TTL
            safe_key = re.sub(r"[^a-z0-9]", "_", player_lower)
            cache_file = os.path.join(_CACHE_DIR, f"news_inj_{safe_key}.json")

            headlines: list[str] = []
            if os.path.exists(cache_file):
                try:
                    with open(cache_file) as f:
                        cached = json.load(f)
                    if time.time() - cached.get("ts", 0) < 7200:
                        headlines = cached.get("headlines", [])
                except Exception:
                    pass

            if not headlines:
                try:
                    articles = news.get_top_headlines(
                        f"{player} injury", page_size=5, ttl_seconds=7200
                    )
                    headlines = [
                        (a.get("title", "") + " " + a.get("description", "")).lower()
                        for a in articles
                    ]
                    with open(cache_file, "w") as f:
                        json.dump({"ts": time.time(), "headlines": headlines}, f)
                except Exception as exc:
                    log.debug("[InjuryAPI] News check skipped for %s: %s", player, exc)
                    continue

            if not headlines:
                continue

            combined = " ".join(headlines)
            return_score  = sum(1 for kw in _NEWS_RETURN_KW  if kw in combined)
            confirm_score = sum(1 for kw in _NEWS_CONFIRM_KW if kw in combined)

            cur_src = rec.get("source_api", "espn")
            if return_score > confirm_score and status in ("Out", "Injured Reserve", "Doubtful"):
                # Headlines lean toward player returning — flag as conflicting
                rec["source_api"] = cur_src + "+news⚠️"
                log.info(
                    "[InjuryAPI] News conflict: %s ESPN=%s but headlines suggest return",
                    player, status,
                )
            elif confirm_score > 0:
                # At least one headline confirms the injury
                rec["source_api"] = cur_src + "+news✓"
                log.debug("[InjuryAPI] News confirmed: %s %s", player, status)

    # ── Scheduled refresh entry point ────────────────────────────────────────

    def fetch_and_store(self, sport: str) -> int:
        """
        Fetch fresh injury data from all sources for *sport* and persist to
        SQLite. Supports 'nba', 'nfl', and 'nhl'.

        Change detection: if any player's status worsens vs previous snapshot,
        the change is stored as a pending alert for Telegram dispatch.

        Returns the number of records stored.
        """
        sport = sport.lower()
        log.info("[InjuryAPI] Refreshing %s injury data...", sport.upper())

        try:
            records = self._fetch_espn(sport)
        except Exception as exc:
            log.warning("[InjuryAPI] ESPN %s fetch failed: %s", sport.upper(), exc)
            records = []

        # NBA only: overlay official status from the CDN PDF
        if sport == "nba":
            official = self._fetch_nba_official()
            if official:
                for r in records:
                    player_lower = r["player_name"].lower()
                    off_status = official.get(player_lower)
                    if off_status:
                        try:
                            if _SEVERITY_ORDER.index(off_status) < _SEVERITY_ORDER.index(r["status"]):
                                r["status"] = off_status
                                r["source_api"] = "nba_official"
                        except ValueError:
                            pass

        # NFL/NBA: cross-reference with Sleeper (status + depth_chart_order)
        if sport in ("nfl", "nba"):
            sleeper = self._fetch_sleeper(sport)
            if sleeper:
                confirmed = upgraded = conflicted = starters_found = 0
                for r in records:
                    name_lower = r["player_name"].lower()
                    sl_entry   = sleeper.get(name_lower)
                    # ── Starter flag from depth_chart_order ──────────────────
                    if sl_entry and sl_entry.get("depth_chart_order") == 1:
                        r["is_starter"] = 1
                        starters_found += 1
                    # ── Status cross-reference ────────────────────────────────
                    sl_status = sl_entry.get("status") if sl_entry else None
                    if not sl_status:
                        continue
                    cur_src = r.get("source_api", "espn")
                    try:
                        espn_idx = _SEVERITY_ORDER.index(r["status"])
                        sl_idx   = _SEVERITY_ORDER.index(sl_status)
                        if sl_idx < espn_idx:
                            r["status"]     = sl_status
                            r["source_api"] = cur_src + "+sleeper↑"
                            upgraded += 1
                        elif sl_idx == espn_idx:
                            r["source_api"] = cur_src + "+sleeper✓"
                            confirmed += 1
                        else:
                            r["source_api"] = cur_src + "+sleeper⚠️"
                            conflicted += 1
                    except ValueError:
                        pass
                log.info(
                    "[InjuryAPI] Sleeper %s: %d confirmed | %d upgraded | %d conflicted | %d starters flagged",
                    sport.upper(), confirmed, upgraded, conflicted, starters_found,
                )

        # All sports: flag starters via _STAR_MULTIPLIERS (covers NHL + fills gaps)
        for r in records:
            if r.get("is_starter"):
                continue  # already set by Sleeper
            player_lower = r["player_name"].lower()
            for name_key, mult in _STAR_MULTIPLIERS.items():
                if name_key in player_lower and mult >= 1.05:
                    r["is_starter"] = 1
                    break

        # All sports: headline spot-check for named star players only
        self._verify_stars_with_news(records, sport)

        db = self._get_db()

        # ── Change detection ──────────────────────────────────────────────────
        change_alerts: list[dict] = []
        if db is not None and records:
            prev_records = db.get(sport)
            if prev_records:
                prev_statuses: dict[str, str] = {
                    r["player_name"]: r["status"] for r in prev_records
                }
                new_by_name: dict[str, dict] = {
                    r["player_name"]: r for r in records
                }
                for player, new_rec in new_by_name.items():
                    old_status = prev_statuses.get(player)
                    new_status = new_rec["status"]
                    if old_status and old_status != new_status:
                        try:
                            old_idx = _SEVERITY_ORDER.index(old_status)
                            new_idx = _SEVERITY_ORDER.index(new_status)
                            if new_idx < old_idx:  # lower index = more severe
                                change_alerts.append({
                                    "sport":       sport,
                                    "player_name": player,
                                    "team":        new_rec.get("team", ""),
                                    "position":    new_rec.get("position", ""),
                                    "old_status":  old_status,
                                    "new_status":  new_status,
                                })
                                log.info(
                                    "[InjuryAPI] Status worsened: %s %s → %s",
                                    player, old_status, new_status,
                                )
                        except ValueError:
                            pass

        if db is not None:
            db.store(sport, records)
            if change_alerts:
                db.store_change_alerts(change_alerts)
        else:
            log.warning("[InjuryAPI] DB unavailable — using hot cache only")

        self._hot_set(sport, records)
        return len(records)

    # ── Catalyst Builder (scan-time, reads from cache) ───────────────────────

    def build_injury_catalysts(
        self,
        market_question: str,
        sport: str | None = None,
        market_prob: float = 0.50,
    ) -> list[dict[str, Any]]:
        """
        Given a prediction market question, return Catalyst-compatible dicts
        for any injured players whose team is mentioned in the question.

        Supports NBA, NFL, and NHL markets.

        Direction values are computed win-probability shifts using sport-specific
        logistic models (e.g. McDavid Out from a 60% favourite → -14pp shift),
        not flat sentiment scores. Falls back to static severity values for
        unknown players.

        market_prob: current market win probability for the team in question.
                     Used as the logistic baseline so shifts are calibrated to
                     the actual game context, not a neutral 50% assumption.

        Reads from hot-path cache first, then SQLite. No live HTTP calls.
        """
        if not sport:
            sport = detect_sport(market_question)
        sport = sport.lower()

        # 1. Try hot-path cache
        records = self._hot_get(sport)

        # 2. Fall back to SQLite
        if records is None:
            db = self._get_db()
            if db is not None:
                records = db.get(sport)
                if records:
                    self._hot_set(sport, records)
                    log.debug("[InjuryAPI] Loaded %d %s records from DB", len(records), sport.upper())

        if not records:
            return []

        # Sport-specific lookup tables
        if sport == "nba":
            key_positions = _KEY_NBA
            alias_map = _NBA_TEAM_ALIASES
        elif sport == "nhl":
            key_positions = _KEY_NHL
            alias_map = _NHL_TEAM_ALIASES
        else:
            key_positions = _KEY_NFL
            alias_map = _NFL_TEAM_ALIASES

        q = market_question.lower()
        catalyst_dicts: list[dict[str, Any]] = []

        for inj in records:
            team = inj.get("team", "")
            if not team:
                continue

            # ── Team matching ────────────────────────────────────────────────
            team_lower = team.lower()
            aliases = alias_map.get(team_lower)
            if aliases is None:
                aliases = [w for w in team_lower.split() if len(w) >= 4]

            if not any(alias in q for alias in aliases):
                continue

            # Skip non-key positions
            pos = inj.get("position", "")
            if pos and pos not in key_positions:
                continue

            final_status = inj.get("status", "Questionable")
            sev = dict(_SEVERITY.get(final_status, _SEVERITY["Questionable"]))
            src = inj.get("source_api", "espn")

            # ── Cross-reference confidence adjustment ─────────────────────────
            if "⚠️" in src:
                sev["confidence"] = max(0.30, sev["confidence"] - 0.20)
            elif "+sleeper✓" in src or "news✓" in src or "nba_official" in src:
                sev["confidence"] = min(0.98, sev["confidence"] + 0.05)

            # ── Star player lookup (for logistic model and goalie floor) ──────
            player       = inj.get("player_name", "Unknown")
            player_lower = player.lower()
            multiplier   = 1.0
            for name_key, mult in _STAR_MULTIPLIERS.items():
                if name_key in player_lower:
                    multiplier = mult
                    break

            if sport == "nhl" and pos == "G" and multiplier < 1.12:
                multiplier = 1.12  # any starting goalie is high-impact

            # ── Logistic win-probability shift (prediction-market method) ─────
            # Try to compute an actual probability shift via the sport-specific
            # logistic model and player impact database.  If the player is not
            # in the database, fall back to the static severity direction.
            try:
                from edge_agent import win_probability as _wp
                shift, eff_impact, wp_explanation = _wp.injury_win_prob_shift(
                    player_name    = player,
                    position       = pos,
                    status         = final_status,
                    sport          = sport,
                    base_win_prob  = market_prob,
                    star_multiplier= multiplier,
                )
                if shift != 0.0:
                    # Use computed shift as direction — already in probability space
                    sev["direction"] = max(-0.99, shift)
                    # Quality reflects how well-calibrated the impact estimate is
                    if multiplier >= 1.15:
                        sev["quality"] = min(0.98, sev["quality"] + 0.05)
                else:
                    # Unknown player — use static severity, apply star multiplier
                    if multiplier > 1.0:
                        sev["direction"] = max(-1.0, sev["direction"] * multiplier)
                        sev["quality"]   = min(1.0, sev["quality"] * (1.0 + (multiplier - 1.0) * 0.5))
                    wp_explanation = ""
            except Exception:
                # win_probability module unavailable — fall back gracefully
                if multiplier > 1.0:
                    sev["direction"] = max(-1.0, sev["direction"] * multiplier)
                    sev["quality"]   = min(1.0, sev["quality"] * (1.0 + (multiplier - 1.0) * 0.5))
                wp_explanation = ""

            # ── Build catalyst label (prediction-market language) ─────────────
            team_disp  = inj.get("team", "")
            inj_type   = inj.get("injury_type", "")
            inj_detail = inj.get("injury_detail", "")

            detail_str = (
                f"{inj_type}" + (f" - {inj_detail}" if inj_detail else "")
                if inj_type else ""
            )

            pos_badge = "🥅" if (sport == "nhl" and pos == "G") else ""
            label = f"INJURY:{player} ({team_disp}) {final_status}"
            if detail_str:
                label += f" [{detail_str}]"
            if pos_badge:
                label += f" {pos_badge}"
            if wp_explanation:
                # Embed the win-prob derivation so it surfaces in the thesis
                label += f" | {wp_explanation}"
            elif multiplier > 1.0:
                label += " ⭐"
            if "nba_official" in src:
                label += " [confirmed official]"

            catalyst_dicts.append({
                "source":     label,
                "direction":  sev["direction"],
                "confidence": sev["confidence"],
                "quality":    sev["quality"],
            })
            log.debug(
                "[InjuryAPI] Catalyst: %s | dir=%+.3f conf=%.2f",
                player, sev["direction"], sev["confidence"],
            )

        return catalyst_dicts
