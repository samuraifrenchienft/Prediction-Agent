"""
Injury Report Client — Two Free Sources + SQLite persistence
=============================================================

Source 1  ESPN Unofficial API  (NBA + NFL, JSON, ~1hr freshness, no auth)
  https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries
  https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries

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
"""
from __future__ import annotations

import importlib
import logging
import re
import time
import io
from datetime import datetime, timedelta
from typing import Any

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy import of InjuryCache — avoids circular import at module load time
# ---------------------------------------------------------------------------

def _get_injury_cache():
    mod = importlib.import_module("edge_agent.memory.injury_cache")
    return mod.InjuryCache()


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

_NBA_CDN = (
    "https://ak-static.cms.nba.com/referee/injury/"
    "Injury-Report_{date}_{hh}_{mm}{ampm}.pdf"
)

# Statuses that represent a player who may not play
_INJURED_STATUSES = {"Out", "Doubtful", "Questionable", "Day-To-Day", "Suspension"}
_PDF_STATUSES     = ["Out", "Doubtful", "Questionable", "Day-To-Day", "Suspension"]

# Catalyst direction/confidence/quality per status severity
_SEVERITY: dict[str, dict[str, float]] = {
    "Out":         {"direction": -0.90, "confidence": 0.92, "quality": 0.90},
    "Suspension":  {"direction": -0.80, "confidence": 0.88, "quality": 0.88},
    "Doubtful":    {"direction": -0.65, "confidence": 0.78, "quality": 0.80},
    "Questionable":{"direction": -0.40, "confidence": 0.62, "quality": 0.72},
    "Day-To-Day":  {"direction": -0.25, "confidence": 0.50, "quality": 0.60},
}

# Only flag positions whose absence moves team win probability
_KEY_NBA = {"PG", "SG", "SF", "PF", "C"}
_KEY_NFL = {"QB", "RB", "WR", "TE"}

# Hot-path in-memory cache TTL (30 min) — feeds build_injury_catalysts() between
# scheduled refreshes without SQLite round-trips
_HOT_TTL = 1800

# NBA team keywords for sport detection
_NBA_KW = {
    "lakers","celtics","warriors","bucks","heat","nets","knicks","bulls","suns",
    "nuggets","clippers","sixers","76ers","raptors","mavericks","mavs","spurs",
    "pacers","pistons","hawks","hornets","magic","thunder","blazers","jazz",
    "grizzlies","pelicans","wolves","timberwolves","kings","rockets","cavaliers",
    "cavs","wizards","nba","basketball",
}
_NFL_KW = {
    "chiefs","eagles","cowboys","patriots","bengals","ravens","dolphins","bills",
    "jets","steelers","browns","titans","colts","texans","jaguars","broncos",
    "raiders","chargers","seahawks","49ers","rams","cardinals","falcons","saints",
    "panthers","buccaneers","packers","bears","lions","vikings","giants",
    "commanders","football","nfl",
}


def detect_sport(text: str) -> str:
    """Return 'nba' or 'nfl' based on keywords in a market question."""
    t = text.lower()
    return "nfl" if sum(1 for k in _NFL_KW if k in t) > sum(1 for k in _NBA_KW if k in t) else "nba"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class InjuryAPIClient:
    """
    Two-source injury client with SQLite persistence.

    Normal flow:
        # Run by the refresh job every 4 hours:
        client.fetch_and_store("nba")
        client.fetch_and_store("nfl")

        # Run by scanner.collect() per sports market:
        cats = client.build_injury_catalysts("Will the Lakers win tonight?")
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
        """Return cached records if still fresh, else None."""
        ts, data = self._hot_cache.get(sport, (0.0, []))
        if time.time() - ts < _HOT_TTL:
            return data
        return None

    def _hot_set(self, sport: str, records: list[dict]) -> None:
        self._hot_cache[sport] = (time.time(), records)

    # ── Source 1: ESPN ───────────────────────────────────────────────────────

    def _fetch_espn(self, sport: str) -> list[dict]:
        """
        ESPN unofficial injury API. No key. NBA and NFL.
        Returns active (non-healthy) players only.
        """
        url = _ESPN_NBA if sport.lower() == "nba" else _ESPN_NFL
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
                    "sport":         sport.upper(),
                })
        log.info("[InjuryAPI] ESPN %s: %d active injuries", sport.upper(), len(records))
        return records

    # ── Source 2: NBA Official CDN PDF ───────────────────────────────────────

    def _fetch_nba_official(self) -> dict[str, str]:
        """
        NBA official CDN PDF. No key. 15-min updates. NBA only.
        Returns {player_name_lower → status}.
        Silently returns {} if pdfplumber is missing or CDN unavailable.
        """
        try:
            import pdfplumber  # noqa
        except ImportError:
            log.debug("[InjuryAPI] pdfplumber not installed — skipping NBA official PDF")
            return {}
        records = self._fetch_nba_pdf()
        return {r["player_name_lower"]: r["status"] for r in records}

    def _fetch_nba_pdf(self) -> list[dict]:
        """Walk back through 15-min slots to find the latest published report."""
        now = datetime.now()
        for mins_back in range(0, 300, 15):   # look back up to 5 hours
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
        """
        Extract injury records from the NBA official report PDF.
        The PDF has no table borders so pdfplumber.extract_table() fails.
        We use line-by-line text extraction and scan for status keywords.

        Player names are stored as 'Lastname,Firstname' in the PDF.
        We normalize to 'Firstname Lastname' for ESPN matching.
        """
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

    # ── Scheduled refresh entry point ────────────────────────────────────────

    def fetch_and_store(self, sport: str) -> int:
        """
        Fetch fresh injury data from all sources for *sport* and persist to
        SQLite. Called by the 4-hour refresh job — NOT by scan-time code.

        Returns the number of records stored.
        """
        sport = sport.lower()
        log.info("[InjuryAPI] Refreshing %s injury data...", sport.upper())

        try:
            records = self._fetch_espn(sport)
        except Exception as exc:
            log.warning("[InjuryAPI] ESPN %s fetch failed: %s", sport.upper(), exc)
            records = []

        # NBA: overlay official status from the CDN PDF
        if sport == "nba":
            official = self._fetch_nba_official()
            if official:
                severity_order = list(_SEVERITY.keys())
                for r in records:
                    player_lower = r["player_name"].lower()
                    off_status = official.get(player_lower)
                    if off_status:
                        try:
                            if severity_order.index(off_status) < severity_order.index(r["status"]):
                                r["status"] = off_status
                                r["source_api"] = "nba_official"
                        except ValueError:
                            pass

        db = self._get_db()
        if db is not None:
            db.store(sport, records)
        else:
            # Fallback: just warm the hot cache so scans still work
            log.warning("[InjuryAPI] DB unavailable — using hot cache only")

        self._hot_set(sport, records)
        return len(records)

    # ── Catalyst Builder (scan-time, reads from cache) ───────────────────────

    def build_injury_catalysts(
        self,
        market_question: str,
        sport: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Given a prediction market question, return Catalyst-compatible dicts
        for any injured players whose team is mentioned in the question.

        Reads from the hot-path cache first, then SQLite. No live HTTP calls.
        Returns [] if no relevant injuries are found.
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

        key_positions = _KEY_NBA if sport == "nba" else _KEY_NFL
        q = market_question.lower()
        catalyst_dicts: list[dict[str, Any]] = []

        for inj in records:
            team = inj.get("team", "").lower()
            if not team:
                continue

            # Match team to question — use significant words (≥4 chars)
            team_words = [w for w in team.split() if len(w) >= 4]
            if not any(tw in q for tw in team_words):
                continue

            # Skip bench players — only key positions move markets
            pos = inj.get("position", "")
            if pos and pos not in key_positions:
                continue

            final_status = inj.get("status", "Questionable")
            sev = _SEVERITY.get(final_status, _SEVERITY["Questionable"])

            player      = inj.get("player_name", "Unknown")
            team_disp   = inj.get("team", "")
            inj_type    = inj.get("injury_type", "")
            inj_detail  = inj.get("injury_detail", "")
            source_api  = inj.get("source_api", "espn")

            detail_str = (
                f"{inj_type}" + (f" - {inj_detail}" if inj_detail else "")
                if inj_type else ""
            )
            label = f"INJURY:{player} ({team_disp}) {final_status}"
            if detail_str:
                label += f" [{detail_str}]"
            if source_api == "nba_official":
                label += " [confirmed official]"

            catalyst_dicts.append({
                "source":     label,
                "direction":  sev["direction"],
                "confidence": sev["confidence"],
                "quality":    sev["quality"],
            })
            log.debug("[InjuryAPI] Catalyst: %s", label)

        return catalyst_dicts
