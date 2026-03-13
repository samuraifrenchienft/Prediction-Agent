"""
User Profile Store
==================
Persistent, long-term memory for each Telegram user.

Stores:
  - Identity: Telegram user_id, first_name, username
  - Personal facts: family, teams, city, hobbies, platforms — extracted
    passively from conversation without asking the user
  - Trading prefs: risk level, bankroll, preferred markets
  - Conversation highlights: memorable moments the AI should recall
  - Last seen timestamp

Facts accumulate over time and are injected into the AI system prompt
so EDGE can be genuinely personalized — not just session-aware.

The extraction is lightweight regex + keyword matching. No LLM call
needed — we catch the most useful signals cheaply.
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


# ── Fact extraction patterns ───────────────────────────────────────────────────
# Each tuple: (regex pattern, fact_key, value_extractor)
# value_extractor: None = boolean flag, else a function(match) → str

_NBA_TEAMS = (
    "warriors|lakers|bulls|heat|celtics|nets|knicks|suns|nuggets|bucks|"
    "clippers|spurs|rockets|mavs|mavericks|hawks|hornets|pacers|pistons|"
    "wizards|magic|raptors|76ers|sixers|thunder|trail blazers|grizzlies|"
    "pelicans|jazz|kings|timberwolves|cavaliers"
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
    "indians|guardians|twins|royals|athletics|mariners|angels|rangers|"
    "blue jays|rays|orioles|marlins|rockies|diamondbacks"
)

_FACT_PATTERNS: list[tuple[str, str, Any]] = [
    # ── Family ────────────────────────────────────────────────────────────────
    (r"\bmy (daughter|girl)\b",          "family",    lambda m: "daughter"),
    (r"\bmy (son|boy)\b",                "family",    lambda m: "son"),
    (r"\bmy (wife|girlfriend|partner)\b","family",    lambda m: m.group(1)),
    (r"\bmy (husband|boyfriend)\b",      "family",    lambda m: m.group(1)),
    (r"\bmy (kid|kids|children|child)\b","family",    lambda m: "kids"),
    (r"\bmy (mom|mother|dad|father)\b",  "family",    lambda m: m.group(1)),
    (r"\bmy (brother|sister|sibling)\b", "family",    lambda m: m.group(1)),

    # ── NBA teams ─────────────────────────────────────────────────────────────
    (rf"\b({_NBA_TEAMS})\b",             "nba_teams", lambda m: m.group(1).title()),

    # ── NFL teams ─────────────────────────────────────────────────────────────
    (rf"\b({_NFL_TEAMS})\b",             "nfl_teams", lambda m: m.group(1).title()),

    # ── MLB teams ─────────────────────────────────────────────────────────────
    (rf"\b({_MLB_TEAMS})\b",             "mlb_teams", lambda m: m.group(1).title()),

    # ── City / location ───────────────────────────────────────────────────────
    (
        r"\b(new york|los angeles|chicago|houston|phoenix|dallas|"
        r"san francisco|miami|boston|seattle|denver|atlanta|"
        r"philadelphia|toronto|portland|minneapolis|oklahoma city|"
        r"memphis|new orleans|sacramento|san antonio|orlando|"
        r"charlotte|detroit|cleveland|milwaukee|indiana|brooklyn)\b",
        "city",
        lambda m: m.group(1).title(),
    ),

    # ── Platform preferences ──────────────────────────────────────────────────
    (r"\b(polymarket)\b",                "platforms", lambda m: "Polymarket"),
    (r"\b(kalshi)\b",                    "platforms", lambda m: "Kalshi"),

    # ── Risk / bankroll signals ───────────────────────────────────────────────
    (r"\b(conservative|low.?risk)\b",    "risk_style","conservative"),
    (r"\b(aggressive|high.?risk)\b",     "risk_style","aggressive"),
    (r"\b(swing|long.?term)\b",          "risk_style","long-term"),

    # ── Sports interest ───────────────────────────────────────────────────────
    (r"\b(nba|basketball)\b",            "sports",    lambda m: "NBA"),
    (r"\b(nfl|football)\b",              "sports",    lambda m: "NFL"),
    (r"\b(mlb|baseball)\b",              "sports",    lambda m: "MLB"),
    (r"\b(nhl|hockey)\b",                "sports",    lambda m: "NHL"),
    (r"\b(soccer|mls|premier league)\b", "sports",    lambda m: "Soccer"),
    (r"\b(politics|election)\b",         "interests", lambda m: "Politics"),
    (r"\b(crypto|bitcoin|ethereum|btc)\b","interests",lambda m: "Crypto"),
]

# Memorable moment patterns — longer phrases worth storing verbatim
_MOMENT_PATTERNS = [
    r"taking my (daughter|son|kid|wife|husband|partner|family|mom|dad).{0,60}(game|match|show|concert|event)",
    r"(going|went|heading|drove|fly(ing)?).{0,40}(game|match|stadium|arena|concert)",
    r"(won|lost|made|hit).{0,30}(\$[\d,]+|\d+ bucks|\d+ dollars)",
    r"(just|finally|today|yesterday).{0,40}(signed up|joined|started|opened).{0,30}(polymarket|kalshi|account)",
]


def _extract_facts(text: str) -> dict[str, list[str]]:
    """
    Scan a message for personal facts. Returns a dict of fact_key → [values].
    Only new values need to be stored — caller merges with existing profile.
    """
    text_lower = text.lower()
    found: dict[str, list[str]] = {}

    for pattern, key, extractor in _FACT_PATTERNS:
        for m in re.finditer(pattern, text_lower, re.IGNORECASE):
            value = extractor(m) if callable(extractor) else extractor
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
            # Store a clean trimmed snippet
            start = max(0, m.start() - 10)
            end   = min(len(text), m.end() + 20)
            snippet = text[start:end].strip()
            if snippet not in moments:
                moments.append(snippet)
    return moments


# ── DB setup ──────────────────────────────────────────────────────────────────

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
            # Update identity fields and last_seen
            with self._conn:
                self._conn.execute(
                    """UPDATE user_profiles
                       SET first_name = COALESCE(?, first_name),
                           username   = COALESCE(?, username),
                           last_seen  = ?,
                           message_count = message_count + 1
                       WHERE user_id = ?""",
                    (first_name, username, now, user_id),
                )
            return self._row_to_dict(
                self._conn.execute(
                    "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
                ).fetchone()
            )

        # New user
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

    def ingest_message(
        self,
        user_id: int,
        message: str,
        first_name: str | None = None,
        username: str | None = None,
    ) -> dict[str, list[str]]:
        """
        Extract facts + moments from a user message and merge into profile.
        Returns dict of newly discovered facts (empty if nothing new found).
        """
        profile  = self.get_or_create(user_id, first_name, username)
        facts    = profile["facts"]
        moments  = profile["moments"]

        new_facts   = _extract_facts(message)
        new_moments = _extract_moments(message)

        changed = False

        # Merge facts — add new values to existing lists
        for key, values in new_facts.items():
            existing = facts.get(key, [])
            for v in values:
                if v not in existing:
                    existing.append(v)
                    changed = True
            facts[key] = existing

        # Merge moments — keep last 20, no duplicates
        for m in new_moments:
            if m not in moments:
                moments.append(m)
                changed = True
        moments = moments[-20:]  # cap

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

    def get_profile_context(self, user_id: int) -> str:
        """
        Return a formatted string to inject into the AI system prompt.
        Includes known personal facts, moments, and trading prefs.
        Empty string if nothing is known yet.
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

        # Family
        family = facts.get("family", [])
        if family:
            lines.append(f"Family: {', '.join(family)}")

        # Sports teams
        for key, label in [("nba_teams", "NBA"), ("nfl_teams", "NFL"),
                            ("mlb_teams", "MLB"), ("soccer_teams", "Soccer")]:
            teams = facts.get(key, [])
            if teams:
                lines.append(f"Follows {label}: {', '.join(teams)}")

        # Sports & interests
        sports = facts.get("sports", [])
        if sports:
            lines.append(f"Sports interests: {', '.join(sports)}")

        interests = facts.get("interests", [])
        if interests:
            lines.append(f"Market interests: {', '.join(interests)}")

        # Location
        cities = facts.get("city", [])
        if cities:
            lines.append(f"Location: {cities[-1]}")

        # Platforms
        platforms = facts.get("platforms", [])
        if platforms:
            lines.append(f"Uses: {', '.join(platforms)}")

        # Risk style
        risk = facts.get("risk_style", [])
        if risk:
            lines.append(f"Trading style: {risk[-1]}")

        # Trading prefs
        if prefs.get("bankroll"):
            lines.append(f"Bankroll: ${prefs['bankroll']}")

        # Memorable moments — most recent 3
        if moments:
            lines.append("Past moments to recall:")
            for m in moments[-3:]:
                lines.append(f'  • "{m}"')

        if not lines:
            return ""

        return (
            f"\n\n[What you know about {name}]\n"
            + "\n".join(lines)
            + "\nUse these naturally when relevant — reference them like a friend would, "
            "not like reading from a file. Don't force it in every reply."
        )

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["facts"]         = json.loads(d.get("facts", "{}") or "{}")
        d["moments"]       = json.loads(d.get("moments", "[]") or "[]")
        d["trading_prefs"] = json.loads(d.get("trading_prefs", "{}") or "{}")
        return d
