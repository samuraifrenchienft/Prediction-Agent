"""
User Profile Store
==================
Persistent, long-term memory for each Telegram user.

Stores:
  - Identity: Telegram user_id, first_name, username
  - Favorite teams (per sport) + rival/hated teams
  - Favorite players — extracted from natural language
  - Personal facts: family, city/timezone, platforms, risk style
  - Conversation highlights: memorable moments the AI should recall
  - Onboarding state: has the bot asked their sport/team/location yet?

Facts accumulate over time, never expire, and are injected into the AI
system prompt so EDGE feels genuinely personal — not just session-aware.

Extraction is regex + keyword matching — no LLM call needed.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent / "data" / "user_profiles.db"


# ── Team lists ─────────────────────────────────────────────────────────────────

_NBA_TEAMS = (
    "warriors|lakers|bulls|heat|celtics|nets|knicks|suns|nuggets|bucks|"
    "clippers|spurs|rockets|mavs|mavericks|hawks|hornets|pacers|pistons|"
    "wizards|magic|raptors|76ers|sixers|thunder|trail blazers|grizzlies|"
    "pelicans|jazz|kings|timberwolves|cavaliers|cavs"
)
_NFL_TEAMS = (
    "chiefs|eagles|cowboys|packers|bills|49ers|niners|ravens|broncos|"
    "patriots|rams|seahawks|steelers|bears|giants|jets|saints|buccaneers|"
    "falcons|panthers|cardinals|chargers|raiders|colts|titans|browns|"
    "texans|jaguars|lions|vikings|commanders|dolphins"
)
_MLB_TEAMS = (
    "yankees|red sox|dodgers|cubs|mets|giants|cardinals|braves|astros|"
    "phillies|nationals|padres|brewers|reds|pirates|tigers|white sox|"
    "guardians|twins|royals|athletics|mariners|angels|rangers|"
    "blue jays|rays|orioles|marlins|rockies|diamondbacks"
)
_NHL_TEAMS = (
    "bruins|sabres|flames|hurricanes|blackhawks|avalanche|blue jackets|"
    "stars|red wings|oilers|panthers|kings|predators|canadiens|devils|"
    "islanders|rangers|senators|flyers|penguins|blues|sharks|lightning|"
    "maple leafs|canucks|golden knights|capitals|jets|coyotes|kraken|ducks"
)

_ALL_TEAMS = f"{_NBA_TEAMS}|{_NFL_TEAMS}|{_MLB_TEAMS}|{_NHL_TEAMS}"

# ── City → timezone mapping ────────────────────────────────────────────────────

_CITY_TIMEZONE: dict[str, str] = {
    "new york": "America/New_York",
    "brooklyn": "America/New_York",
    "boston": "America/New_York",
    "miami": "America/New_York",
    "orlando": "America/New_York",
    "charlotte": "America/New_York",
    "philadelphia": "America/New_York",
    "cleveland": "America/New_York",
    "detroit": "America/New_York",
    "atlanta": "America/New_York",
    "washington": "America/New_York",
    "chicago": "America/Chicago",
    "houston": "America/Chicago",
    "dallas": "America/Chicago",
    "minneapolis": "America/Chicago",
    "memphis": "America/Chicago",
    "new orleans": "America/Chicago",
    "oklahoma city": "America/Chicago",
    "milwaukee": "America/Chicago",
    "indiana": "America/Indiana/Indianapolis",
    "san antonio": "America/Chicago",
    "denver": "America/Denver",
    "phoenix": "America/Phoenix",
    "los angeles": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "sacramento": "America/Los_Angeles",
    "portland": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    "toronto": "America/Toronto",
}

# ── Fact extraction patterns ───────────────────────────────────────────────────
# Each tuple: (regex, fact_key, value_extractor_fn | literal_string)

_FACT_PATTERNS: list[tuple[str, str, Any]] = [

    # ── Family ────────────────────────────────────────────────────────────────
    (r"\bmy (daughter|girl)\b",           "family",        lambda m: "daughter"),
    (r"\bmy (son|boy)\b",                 "family",        lambda m: "son"),
    (r"\bmy (wife|girlfriend|partner)\b", "family",        lambda m: m.group(1)),
    (r"\bmy (husband|boyfriend)\b",       "family",        lambda m: m.group(1)),
    (r"\bmy (kid|kids|children|child)\b", "family",        lambda m: "kids"),
    (r"\bmy (mom|mother|dad|father)\b",   "family",        lambda m: m.group(1)),
    (r"\bmy (brother|sister|sibling)\b",  "family",        lambda m: m.group(1)),

    # ── Favorite teams (affirmative signals) ──────────────────────────────────
    (
        r"\b(?:my (?:team|squad|guys?)|i(?:'m| am) (?:a |an )?|love the?|follow the?|"
        r"root(?:ing)? for(?: the)?|fan of(?: the)?|go )\s*"
        rf"({_NBA_TEAMS})\b",
        "fav_nba_teams",
        lambda m: m.group(1).title(),
    ),
    (
        r"\b(?:my (?:team|squad|guys?)|i(?:'m| am) (?:a |an )?|love the?|follow the?|"
        r"root(?:ing)? for(?: the)?|fan of(?: the)?|go )\s*"
        rf"({_NFL_TEAMS})\b",
        "fav_nfl_teams",
        lambda m: m.group(1).title(),
    ),
    (
        r"\b(?:my (?:team|squad|guys?)|i(?:'m| am) (?:a |an )?|love the?|follow the?|"
        r"root(?:ing)? for(?: the)?|fan of(?: the)?|go )\s*"
        rf"({_MLB_TEAMS})\b",
        "fav_mlb_teams",
        lambda m: m.group(1).title(),
    ),
    (
        r"\b(?:my (?:team|squad|guys?)|i(?:'m| am) (?:a |an )?|love the?|follow the?|"
        r"root(?:ing)? for(?: the)?|fan of(?: the)?|go )\s*"
        rf"({_NHL_TEAMS})\b",
        "fav_nhl_teams",
        lambda m: m.group(1).title(),
    ),

    # ── Passively mentioned teams (less strong signal, still track) ───────────
    (rf"\b({_NBA_TEAMS})\b",  "nba_teams",  lambda m: m.group(1).title()),
    (rf"\b({_NFL_TEAMS})\b",  "nfl_teams",  lambda m: m.group(1).title()),
    (rf"\b({_MLB_TEAMS})\b",  "mlb_teams",  lambda m: m.group(1).title()),
    (rf"\b({_NHL_TEAMS})\b",  "nhl_teams",  lambda m: m.group(1).title()),

    # ── Rival / hated teams ───────────────────────────────────────────────────
    (
        r"(?:hate|can'?t stand|dislike|despise|least fav|worst team|"
        r"can'?t watch|enemy|rivals?).{0,25}"
        rf"({_ALL_TEAMS})\b",
        "rival_teams",
        lambda m: m.group(1).title(),
    ),
    (
        rf"({_ALL_TEAMS})\b.{{0,20}}"
        r"(?:suck|are? terrible|are? trash|are? the worst|i hate)",
        "rival_teams",
        lambda m: m.group(1).title(),
    ),

    # ── Favorite players ──────────────────────────────────────────────────────
    # Patterns like "my guy Steph", "love watching Curry", "Lebron is my GOAT"
    (
        r"(?:my (?:guy|player|fav(?:orite)?|goat)|love (?:watching|following)|"
        r"big fan of)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        "fav_players",
        lambda m: m.group(1).strip(),
    ),
    (
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+is (?:my |the )?"
        r"(?:guy|goat|favorite|fav|player|idol|hero)",
        "fav_players",
        lambda m: m.group(1).strip(),
    ),
    # Common star name shorthand used passionately: "steph", "bron", "giannis", etc.
    (
        r"\b(steph|curry|lebron|bron|giannis|jokic|luka|doncic|kd|durant|"
        r"tatum|embiid|wemby|wembanyama|sga|kawhi|jimmy|butler|ant|"
        r"mahomes|lamar|jackson|burrow|hurts|allen|prescott|"
        r"ohtani|judge|soto|trout|acuna)\b",
        "fav_players",
        lambda m: m.group(1).title(),
    ),

    # ── Location / city ───────────────────────────────────────────────────────
    (
        r"\b(new york|brooklyn|los angeles|chicago|houston|phoenix|dallas|"
        r"san francisco|miami|boston|seattle|denver|atlanta|"
        r"philadelphia|toronto|portland|minneapolis|oklahoma city|"
        r"memphis|new orleans|sacramento|san antonio|orlando|"
        r"charlotte|detroit|cleveland|milwaukee|indiana|washington)\b",
        "city",
        lambda m: m.group(1).title(),
    ),

    # ── Platform preferences ──────────────────────────────────────────────────
    (r"\b(polymarket)\b",                 "platforms",     lambda m: "Polymarket"),
    (r"\b(kalshi)\b",                     "platforms",     lambda m: "Kalshi"),

    # ── Risk / trading style ──────────────────────────────────────────────────
    (r"\b(conservative|low.?risk)\b",     "risk_style",    "conservative"),
    (r"\b(aggressive|high.?risk)\b",      "risk_style",    "aggressive"),
    (r"\b(swing|long.?term)\b",           "risk_style",    "long-term"),

    # ── Sports interests ──────────────────────────────────────────────────────
    (r"\b(nba|basketball)\b",             "sports",        lambda m: "NBA"),
    (r"\b(nfl|football)\b",               "sports",        lambda m: "NFL"),
    (r"\b(mlb|baseball)\b",               "sports",        lambda m: "MLB"),
    (r"\b(nhl|hockey)\b",                 "sports",        lambda m: "NHL"),
    (r"\b(soccer|mls|premier league)\b",  "sports",        lambda m: "Soccer"),
    (r"\b(politics|election)\b",          "interests",     lambda m: "Politics"),
    (r"\b(crypto|bitcoin|ethereum|btc)\b","interests",     lambda m: "Crypto"),
]

# ── Memorable moment patterns ─────────────────────────────────────────────────

_MOMENT_PATTERNS = [
    r"taking my (?:daughter|son|kid|wife|husband|partner|family|mom|dad).{0,60}(?:game|match|show|concert|event)",
    r"(?:going|went|heading|drove|fly(?:ing)?).{0,40}(?:game|match|stadium|arena|concert)",
    r"(?:won|lost|made|hit).{0,30}(?:\$[\d,]+|\d+ bucks|\d+ dollars)",
    r"(?:just|finally|today|yesterday).{0,40}(?:signed up|joined|started|opened).{0,30}(?:polymarket|kalshi|account)",
    r"(?:my (?:guy|player|goat)).{0,40}(?:injured|out|hurt|done for the season|returned|back)",
]


# ── Helper functions ───────────────────────────────────────────────────────────

def _extract_facts(text: str) -> dict[str, list[str]]:
    """Scan a message for personal facts. Returns fact_key → [values]."""
    found: dict[str, list[str]] = {}
    for pattern, key, extractor in _FACT_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            value = extractor(m) if callable(extractor) else extractor
            if value and len(value) > 1:  # skip single-char noise
                found.setdefault(key, [])
                if value not in found[key]:
                    found[key].append(value)
    return found


def _extract_moments(text: str) -> list[str]:
    """Extract memorable phrases worth recalling in future sessions."""
    moments = []
    for pattern in _MOMENT_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            start   = max(0, m.start() - 10)
            end     = min(len(text), m.end() + 20)
            snippet = text[start:end].strip()
            if snippet not in moments:
                moments.append(snippet)
    return moments


def _tz_from_city(facts: dict) -> str | None:
    """Infer IANA timezone string from the most recent stored city."""
    cities = facts.get("city", [])
    if not cities:
        return None
    return _CITY_TIMEZONE.get(cities[-1].lower())


def is_new_user(profile: dict) -> bool:
    """True if we haven't asked onboarding questions yet (< 6 messages)."""
    return profile.get("message_count", 0) <= 5


def needs_onboarding(profile: dict) -> bool:
    """True if key profile fields are still empty."""
    facts = profile.get("facts", {})
    has_sport  = bool(facts.get("sports") or facts.get("fav_nba_teams")
                      or facts.get("fav_nfl_teams") or facts.get("nba_teams")
                      or facts.get("nfl_teams"))
    has_player = bool(facts.get("fav_players"))
    has_city   = bool(facts.get("city"))
    return not (has_sport and has_player and has_city)


# ── DB setup ───────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id       INTEGER PRIMARY KEY,
            first_name    TEXT,
            username      TEXT,
            facts         TEXT NOT NULL DEFAULT '{}',
            moments       TEXT NOT NULL DEFAULT '[]',
            trading_prefs TEXT NOT NULL DEFAULT '{}',
            created_at    REAL NOT NULL,
            last_seen     REAL NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()


# ── Public API ─────────────────────────────────────────────────────────────────

class UserProfileStore:
    def __init__(self) -> None:
        self._conn = _connect()
        _init_db(self._conn)

    # ── Identity ──────────────────────────────────────────────────────────────

    def get_or_create(
        self,
        user_id: int,
        first_name: str | None = None,
        username: str | None = None,
    ) -> dict[str, Any]:
        """Return existing profile or create a fresh one."""
        now = time.time()
        row = self._conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
        ).fetchone()

        if row:
            with self._conn:
                self._conn.execute(
                    """UPDATE user_profiles
                       SET first_name     = COALESCE(?, first_name),
                           username       = COALESCE(?, username),
                           last_seen      = ?,
                           message_count  = message_count + 1
                       WHERE user_id = ?""",
                    (first_name, username, now, user_id),
                )
            return self._row_to_dict(
                self._conn.execute(
                    "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
                ).fetchone()
            )

        with self._conn:
            self._conn.execute(
                """INSERT INTO user_profiles
                   (user_id, first_name, username, created_at, last_seen)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, first_name, username, now, now),
            )
        return self._row_to_dict(
            self._conn.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
        )

    # ── Ingestion ─────────────────────────────────────────────────────────────

    def ingest_message(
        self,
        user_id: int,
        message: str,
        first_name: str | None = None,
        username: str | None = None,
    ) -> dict[str, list[str]]:
        """
        Extract facts + moments from a user message and merge into profile.
        Returns dict of newly discovered facts (empty if nothing new).
        """
        profile = self.get_or_create(user_id, first_name, username)
        facts   = profile["facts"]
        moments = profile["moments"]

        new_facts   = _extract_facts(message)
        new_moments = _extract_moments(message)
        changed     = False

        for key, values in new_facts.items():
            existing = facts.get(key, [])
            for v in values:
                if v not in existing:
                    existing.append(v)
                    changed = True
            facts[key] = existing

        for m in new_moments:
            if m not in moments:
                moments.append(m)
                changed = True
        moments = moments[-20:]

        if changed:
            with self._conn:
                self._conn.execute(
                    "UPDATE user_profiles SET facts = ?, moments = ? WHERE user_id = ?",
                    (json.dumps(facts), json.dumps(moments), user_id),
                )

        return new_facts if changed else {}

    def set_trading_pref(self, user_id: int, key: str, value: Any) -> None:
        """Store a trading preference (bankroll, risk_level, platform)."""
        profile = self.get_or_create(user_id)
        prefs   = profile["trading_prefs"]
        prefs[key] = value
        with self._conn:
            self._conn.execute(
                "UPDATE user_profiles SET trading_prefs = ? WHERE user_id = ?",
                (json.dumps(prefs), user_id),
            )

    # ── Context for AI prompt ─────────────────────────────────────────────────

    def get_profile_context(self, user_id: int) -> str:
        """
        Return a formatted block to inject into the AI system prompt.
        Covers: favorites, rivals, players, family, location/tz, prefs, moments.
        Empty string if nothing known yet.
        """
        row = self._conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return ""

        profile = self._row_to_dict(row)
        facts   = profile["facts"]
        moments = profile["moments"]
        prefs   = profile["trading_prefs"]
        name    = profile.get("first_name") or "this user"

        lines: list[str] = []

        # Favorite teams (strong signal)
        for key, label in [
            ("fav_nba_teams", "NBA"), ("fav_nfl_teams", "NFL"),
            ("fav_mlb_teams", "MLB"), ("fav_nhl_teams", "NHL"),
        ]:
            teams = facts.get(key, [])
            if teams:
                lines.append(f"❤️ Favorite {label} team(s): {', '.join(teams)}")

        # Passively mentioned teams (weaker signal)
        for key, label in [
            ("nba_teams", "NBA"), ("nfl_teams", "NFL"),
            ("mlb_teams", "MLB"), ("nhl_teams", "NHL"),
        ]:
            # Only show if no strong fav already captured for that sport
            fav_key = f"fav_{key}"
            if not facts.get(fav_key) and facts.get(key):
                lines.append(f"Follows {label}: {', '.join(facts[key])}")

        # Rival / hated teams
        rivals = facts.get("rival_teams", [])
        if rivals:
            lines.append(f"😤 Rival/hated teams: {', '.join(rivals)}")

        # Favorite players
        players = facts.get("fav_players", [])
        if players:
            lines.append(f"⭐ Favorite player(s): {', '.join(players)}")

        # Sports + interests
        sports = facts.get("sports", [])
        if sports:
            lines.append(f"Sports interests: {', '.join(sports)}")
        interests = facts.get("interests", [])
        if interests:
            lines.append(f"Market interests: {', '.join(interests)}")

        # Family
        family = facts.get("family", [])
        if family:
            lines.append(f"Family: {', '.join(family)}")

        # Location + timezone
        cities = facts.get("city", [])
        if cities:
            city = cities[-1]
            tz   = _tz_from_city(facts)
            tz_note = f" (timezone: {tz})" if tz else ""
            lines.append(f"Location: {city}{tz_note}")

        # Platforms
        platforms = facts.get("platforms", [])
        if platforms:
            lines.append(f"Uses: {', '.join(platforms)}")

        # Risk style + bankroll
        risk = facts.get("risk_style", [])
        if risk:
            lines.append(f"Trading style: {risk[-1]}")
        if prefs.get("bankroll"):
            lines.append(f"Bankroll: ${prefs['bankroll']}")

        # Memorable moments (most recent 3)
        if moments:
            lines.append("Past moments to recall:")
            for m in moments[-3:]:
                lines.append(f'  • "{m}"')

        if not lines:
            return ""

        # Onboarding hint for AI
        onboard_hint = ""
        if needs_onboarding(profile) and profile.get("message_count", 0) <= 10:
            missing = []
            if not players:
                missing.append("favorite player")
            if not cities:
                missing.append("their city/location")
            if not (facts.get("fav_nba_teams") or facts.get("fav_nfl_teams")):
                missing.append("favorite team")
            if missing:
                onboard_hint = (
                    f"\nSTILL UNKNOWN: {', '.join(missing)}. "
                    "Work these into conversation naturally — one at a time, not all at once."
                )

        return (
            f"\n\n[What you know about {name}]\n"
            + "\n".join(lines)
            + "\nReference these naturally, like a knowledgeable friend would — "
            "not like reading from a file. Express genuine emotion when relevant "
            "(concern for their fav player's injury, excitement for their team's win)."
            + onboard_hint
        )

    def get_onboarding_prompt(self, user_id: int) -> str:
        """
        Return an AI instruction string to gather missing profile info
        for new users. Empty string if profile is complete or user is not new.
        """
        row = self._conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return ""

        profile = self._row_to_dict(row)
        if not is_new_user(profile):
            return ""

        facts   = profile["facts"]
        missing = []

        if not (facts.get("sports") or facts.get("fav_nba_teams") or facts.get("fav_nfl_teams")):
            missing.append("what sports they follow")
        if not (facts.get("fav_nba_teams") or facts.get("fav_nfl_teams")
                or facts.get("fav_mlb_teams") or facts.get("fav_nhl_teams")):
            missing.append("their favorite team")
        if not facts.get("fav_players"):
            missing.append("a favorite player")
        if not facts.get("city"):
            missing.append("their location (city) for timezone-accurate alerts")

        if not missing:
            return ""

        return (
            "\nNEW USER ONBOARDING: This is an early interaction. "
            "Casually work into the conversation — one question at a time — "
            f"to learn: {', '.join(missing)}. "
            "Do it naturally, not like a form. If they mention a sport, ask about their team. "
            "If they mention a team, ask who their favorite player is. "
            "If they mention a city event, note their location."
        )

    # ── Alert personalization ─────────────────────────────────────────────────

    def get_alert_tone(
        self,
        user_id: int,
        player_name: str | None = None,
        team_name: str | None = None,
        event: str = "injury",   # "injury" | "return" | "win" | "loss"
    ) -> str:
        """
        Return a tone instruction string for the AI when sending a sports alert.
        Empty string if the player/team isn't relevant to this user.
        """
        row = self._conn.execute(
            "SELECT facts FROM user_profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return ""

        facts      = json.loads(row["facts"] or "{}")
        fav_players = [p.lower() for p in facts.get("fav_players", [])]
        fav_teams   = [
            t.lower() for t in (
                facts.get("fav_nba_teams", []) + facts.get("fav_nfl_teams", []) +
                facts.get("fav_mlb_teams", []) + facts.get("fav_nhl_teams", [])
            )
        ]
        rival_teams = [t.lower() for t in facts.get("rival_teams", [])]

        p_lower = (player_name or "").lower()
        t_lower = (team_name or "").lower()

        is_fav_player = any(p_lower in fp or fp in p_lower for fp in fav_players if p_lower)
        is_fav_team   = any(t_lower in ft or ft in t_lower for ft in fav_teams  if t_lower)
        is_rival_team = any(t_lower in rt or rt in t_lower for rt in rival_teams if t_lower)

        if event == "injury":
            if is_fav_player:
                return (
                    f"PERSONALIZATION: {player_name} is this user's favorite player. "
                    "Express genuine concern and sympathy — this is bad news for them personally."
                )
            if is_fav_team:
                return (
                    f"PERSONALIZATION: {team_name} is this user's favorite team. "
                    "Acknowledge the bad news for their team with empathy."
                )
            if is_rival_team:
                return (
                    f"PERSONALIZATION: {team_name} is this user's rival team. "
                    "Deliver the injury news factually — don't celebrate, but note the market impact."
                )

        elif event == "return":
            if is_fav_player:
                return (
                    f"PERSONALIZATION: {player_name} is this user's favorite player "
                    "and they're returning from injury. Show genuine excitement — "
                    "this is great news for them!"
                )
            if is_fav_team:
                return (
                    f"PERSONALIZATION: {player_name} returning is great news for "
                    f"{team_name}, this user's favorite team. Share in the excitement."
                )

        return ""

    def get_users_for_player(self, player_name: str) -> list[int]:
        """Return user_ids of all users who have this player as a favorite."""
        p = player_name.lower()
        rows = self._conn.execute(
            "SELECT user_id, facts FROM user_profiles"
        ).fetchall()
        result = []
        for row in rows:
            facts   = json.loads(row["facts"] or "{}")
            players = [fp.lower() for fp in facts.get("fav_players", [])]
            if any(p in fp or fp in p for fp in players):
                result.append(row["user_id"])
        return result

    def get_users_for_team(self, team_name: str) -> list[int]:
        """Return user_ids of all users who have this team as a favorite."""
        t = team_name.lower()
        rows = self._conn.execute(
            "SELECT user_id, facts FROM user_profiles"
        ).fetchall()
        result = []
        for row in rows:
            facts     = json.loads(row["facts"] or "{}")
            fav_teams = [
                ft.lower() for ft in (
                    facts.get("fav_nba_teams", []) + facts.get("fav_nfl_teams", []) +
                    facts.get("fav_mlb_teams", []) + facts.get("fav_nhl_teams", [])
                )
            ]
            if any(t in ft or ft in t for ft in fav_teams):
                result.append(row["user_id"])
        return result

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["facts"]         = json.loads(d.get("facts", "{}") or "{}")
        d["moments"]       = json.loads(d.get("moments", "[]") or "[]")
        d["trading_prefs"] = json.loads(d.get("trading_prefs", "{}") or "{}")
        return d
