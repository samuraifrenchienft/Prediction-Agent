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

# ── College Football (CFB) ─────────────────────────────────────────────────────
_CFB_TEAMS = (
    # SEC
    "alabama|crimson tide|georgia bulldogs|georgia dawgs|lsu tigers|lsu|"
    "tennessee vols|volunteers|auburn tigers|auburn|florida gators|gators|"
    "ole miss|rebels|mississippi state|razorbacks|gamecocks|"
    "commodores|texas a&m|aggies|texas longhorns|longhorns|"
    "oklahoma sooners|sooners|"
    # Big Ten
    "ohio state|buckeyes|michigan wolverines|wolverines|penn state|nittany lions|"
    "michigan state|iowa hawkeyes|hawkeyes|minnesota gophers|gophers|"
    "wisconsin badgers|badgers|cornhuskers|northwestern wildcats|"
    "boilermakers|fighting illini|rutgers|hoosiers|oregon ducks|"
    "washington huskies|usc trojans|ucla bruins|"
    # Big 12
    "kansas state|jayhawks|horned frogs|baylor bears|oklahoma state cowboys|"
    "mountaineers|cyclones|byu cougars|utah utes|utes|sun devils|colorado buffs|"
    # ACC
    "clemson tigers|clemson|seminoles|miami hurricanes|wolfpack|tar heels|hokies|"
    "demon deacons|duke blue devils|georgia tech|notre dame|fighting irish|"
    "louisville cardinals|pitt panthers|cavaliers|syracuse orange"
)

# ── College Basketball (CBB / NCAAB) ─────────────────────────────────────────
_CBB_TEAMS = (
    "duke blue devils|duke|kentucky wildcats|kansas jayhawks|north carolina tar heels|"
    "unc tar heels|gonzaga bulldogs|gonzaga|villanova wildcats|"
    "michigan state spartans|uconn huskies|uconn|connecticut huskies|"
    "indiana hoosiers|iowa hawkeyes|louisville cardinals|memphis tigers|"
    "arizona wildcats|ucla bruins|houston cougars|baylor bears|purdue boilermakers|"
    "tennessee volunteers|auburn tigers|alabama crimson tide|"
    "illinois fighting illini|ohio state buckeyes|michigan wolverines|"
    "arkansas razorbacks|creighton bluejays|xavier musketeers|"
    "marquette golden eagles|marquette|st johns|florida gators|"
    "oregon ducks|texas tech red raiders|texas tech|san diego state aztecs|"
    "saint mary|colorado state|drake bulldogs|dayton flyers"
)

# ── MLS (Major League Soccer) ─────────────────────────────────────────────────
_MLS_TEAMS = (
    "la galaxy|galaxy|lafc|seattle sounders|sounders|portland timbers|timbers|"
    "atlanta united|new york city fc|nycfc|new york red bulls|red bulls|"
    "philadelphia union|union|new england revolution|revolution|dc united|"
    "toronto fc|cf montreal|orlando city|inter miami|miami fc|"
    "colorado rapids|rapids|real salt lake|minnesota united|fc dallas|"
    "houston dynamo|dynamo|sporting kansas city|sporting kc|chicago fire|"
    "columbus crew|crew|nashville sc|austin fc|charlotte fc|"
    "st louis city|san jose earthquakes|earthquakes|vancouver whitecaps|whitecaps|"
    "cincinnati fc|fc cincinnati"
)

# ── Soccer clubs (EPL + top European + international) ────────────────────────
_SOCCER_CLUBS = (
    # EPL
    "manchester city|man city|liverpool fc|arsenal fc|chelsea fc|"
    "tottenham hotspur|spurs|manchester united|man united|man utd|"
    "aston villa|newcastle united|newcastle|west ham united|west ham|"
    "brighton|brentford|fulham fc|crystal palace|everton fc|"
    "nottingham forest|bournemouth|wolverhampton|wolves|leicester city|"
    # La Liga
    "real madrid|barcelona|barca|atletico madrid|atletico|sevilla fc|"
    "real sociedad|villarreal|athletic bilbao|real betis|betis|"
    # Bundesliga
    "bayern munich|bayern|borussia dortmund|dortmund|bvb|bayer leverkusen|leverkusen|"
    "rb leipzig|eintracht frankfurt|"
    # Serie A
    "inter milan|ac milan|juventus|juve|napoli|lazio|roma|atalanta|"
    # Ligue 1 / Other
    "psg|paris saint.germain|ajax|porto|benfica|celtic fc"
)

# ── WNBA teams ────────────────────────────────────────────────────────────────
_WNBA_TEAMS = (
    "las vegas aces|aces|indiana fever|fever|new york liberty|liberty|"
    "chicago sky|sky|seattle storm|storm|connecticut sun|sun|"
    "minnesota lynx|lynx|washington mystics|mystics|los angeles sparks|sparks|"
    "phoenix mercury|mercury|dallas wings|wings|atlanta dream|dream|"
    "golden state valkyries|valkyries"
)

# ── Women's College Basketball (NCAAW) ────────────────────────────────────────
_NCAAW_TEAMS = (
    "uconn women|south carolina gamecocks women|iowa hawkeyes women|"
    "lsu tigers women|stanford cardinal|notre dame women|"
    "texas longhorns women|duke women|nc state women|ohio state women|"
    "baylor women|virginia tech women|utah women|indiana women|"
    "tennessee lady vols|lady vols|kansas women|louisville women|"
    "kentucky women|oregon women|caitlin clark"  # Clark as she's a brand
)

# ── F1 constructor teams ──────────────────────────────────────────────────────
_F1_TEAMS = (
    "red bull racing|red bull f1|mercedes amg f1|mercedes f1|ferrari f1|scuderia ferrari|"
    "mclaren f1|aston martin f1|alpine f1|williams f1|racing bulls|alphatauri|"
    "kick sauber|sauber f1|haas f1"
)

# ── F1 drivers ────────────────────────────────────────────────────────────────
_F1_DRIVERS = (
    "verstappen|lewis hamilton|hamilton f1|charles leclerc|leclerc|"
    "lando norris|norris|carlos sainz|sainz|fernando alonso|alonso|"
    "george russell|russell f1|oscar piastri|piastri|sergio perez|checo perez|"
    "lance stroll|pierre gasly|gasly|esteban ocon|valtteri bottas|"
    "nico hulkenberg|kevin magnussen|yuki tsunoda|alexander albon|"
    "franco colapinto|liam lawson|oliver bearman"
)

# ── PGA Tour golfers ──────────────────────────────────────────────────────────
_PGA_GOLFERS = (
    "scottie scheffler|rory mcilroy|jon rahm|viktor hovland|"
    "xander schauffele|collin morikawa|patrick cantlay|brooks koepka|"
    "justin thomas|jordan spieth|dustin johnson|tony finau|"
    "hideki matsuyama|matt fitzpatrick|tommy fleetwood|tyrrell hatton|"
    "shane lowry|will zalatoris|sam burns|cameron smith|"
    "bryson dechambeau|tiger woods|tiger|phil mickelson|ryder cup"
)

_ALL_TEAMS = (
    f"{_NBA_TEAMS}|{_NFL_TEAMS}|{_MLB_TEAMS}|{_NHL_TEAMS}|"
    f"{_CFB_TEAMS}|{_CBB_TEAMS}|{_MLS_TEAMS}|{_SOCCER_CLUBS}|"
    f"{_WNBA_TEAMS}|{_NCAAW_TEAMS}|{_F1_TEAMS}|{_F1_DRIVERS}|{_PGA_GOLFERS}"
)

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

    # ── Favorite CFB teams ────────────────────────────────────────────────────
    (
        r"\b(?:my (?:team|squad|guys?)|i(?:'m| am) (?:a |an )?|love the?|follow the?|"
        r"root(?:ing)? for(?: the)?|fan of(?: the)?|go )\s*"
        rf"({_CFB_TEAMS})\b",
        "fav_cfb_teams",
        lambda m: m.group(1).title(),
    ),

    # ── Favorite CBB teams ────────────────────────────────────────────────────
    (
        r"\b(?:my (?:team|squad|guys?)|i(?:'m| am) (?:a |an )?|love the?|follow the?|"
        r"root(?:ing)? for(?: the)?|fan of(?: the)?|go )\s*"
        rf"({_CBB_TEAMS})\b",
        "fav_cbb_teams",
        lambda m: m.group(1).title(),
    ),

    # ── Favorite MLS teams ────────────────────────────────────────────────────
    (
        r"\b(?:my (?:team|squad|guys?)|i(?:'m| am) (?:a |an )?|love the?|follow the?|"
        r"root(?:ing)? for(?: the)?|fan of(?: the)?|go )\s*"
        rf"({_MLS_TEAMS})\b",
        "fav_mls_teams",
        lambda m: m.group(1).title(),
    ),

    # ── Favorite soccer clubs (EPL / European / international) ───────────────
    (
        r"\b(?:my (?:team|squad|club|guys?)|i(?:'m| am) (?:a |an )?|love the?|"
        r"follow the?|root(?:ing)? for(?: the)?|fan of(?: the)?|support(?:s|ing)? )\s*"
        rf"({_SOCCER_CLUBS})\b",
        "fav_soccer_clubs",
        lambda m: m.group(1).title(),
    ),

    # ── Favorite WNBA teams ───────────────────────────────────────────────────
    (
        r"\b(?:my (?:team|squad|girls?)|i(?:'m| am) (?:a |an )?|love the?|follow the?|"
        r"root(?:ing)? for(?: the)?|fan of(?: the)?|go )\s*"
        rf"({_WNBA_TEAMS})\b",
        "fav_wnba_teams",
        lambda m: m.group(1).title(),
    ),

    # ── Favorite NCAAW teams ──────────────────────────────────────────────────
    (
        r"\b(?:my (?:team|squad|girls?)|i(?:'m| am) (?:a |an )?|love the?|follow the?|"
        r"root(?:ing)? for(?: the)?|fan of(?: the)?|go )\s*"
        rf"({_NCAAW_TEAMS})\b",
        "fav_ncaaw_teams",
        lambda m: m.group(1).title(),
    ),

    # ── Favorite F1 team/driver ───────────────────────────────────────────────
    (
        r"\b(?:my (?:team|driver|guy)|i(?:'m| am) (?:a |an )?|love|follow|"
        r"root(?:ing)? for|fan of|support(?:s|ing)? )\s*"
        rf"({_F1_TEAMS}|{_F1_DRIVERS})\b",
        "fav_f1",
        lambda m: m.group(1).title(),
    ),

    # ── Favorite PGA golfer ───────────────────────────────────────────────────
    (
        r"\b(?:my (?:guy|golfer|player|favorite)|love (?:watching|following)|"
        r"big fan of|rooting for)\s*"
        rf"({_PGA_GOLFERS})\b",
        "fav_golfers",
        lambda m: m.group(1).title(),
    ),

    # ── Passively mentioned teams (less strong signal, still track) ───────────
    (rf"\b({_NBA_TEAMS})\b",     "nba_teams",     lambda m: m.group(1).title()),
    (rf"\b({_NFL_TEAMS})\b",     "nfl_teams",     lambda m: m.group(1).title()),
    (rf"\b({_MLB_TEAMS})\b",     "mlb_teams",     lambda m: m.group(1).title()),
    (rf"\b({_NHL_TEAMS})\b",     "nhl_teams",     lambda m: m.group(1).title()),
    (rf"\b({_CFB_TEAMS})\b",     "cfb_teams",     lambda m: m.group(1).title()),
    (rf"\b({_CBB_TEAMS})\b",     "cbb_teams",     lambda m: m.group(1).title()),
    (rf"\b({_MLS_TEAMS})\b",     "mls_teams",     lambda m: m.group(1).title()),
    (rf"\b({_SOCCER_CLUBS})\b",  "soccer_clubs",  lambda m: m.group(1).title()),
    (rf"\b({_WNBA_TEAMS})\b",    "wnba_teams",    lambda m: m.group(1).title()),
    (rf"\b({_NCAAW_TEAMS})\b",   "ncaaw_teams",   lambda m: m.group(1).title()),
    (rf"\b({_F1_TEAMS}|{_F1_DRIVERS})\b", "f1_teams", lambda m: m.group(1).title()),
    (rf"\b({_PGA_GOLFERS})\b",   "pga_golfers",   lambda m: m.group(1).title()),

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
    (r"\b(nba|basketball)\b",                                       "sports", lambda m: "NBA"),
    (r"\b(nfl|football)\b",                                         "sports", lambda m: "NFL"),
    (r"\b(mlb|baseball)\b",                                         "sports", lambda m: "MLB"),
    (r"\b(nhl|hockey)\b",                                           "sports", lambda m: "NHL"),
    (r"\b(college football|cfb|ncaaf)\b",                           "sports", lambda m: "CFB"),
    (r"\b(college basketball|cbb|ncaab|march madness)\b",           "sports", lambda m: "CBB"),
    (r"\b(wnba|women(?:'s)? basketball|women(?:'s)? nba)\b",        "sports", lambda m: "WNBA"),
    (r"\b(ncaaw|women(?:'s)? college basketball|womens march madness)\b", "sports", lambda m: "NCAAW"),
    (r"\b(soccer|mls|premier league|epl|futbol)\b",                 "sports", lambda m: "Soccer"),
    (r"\b(champions league|ucl|europa league|la liga|bundesliga|serie a)\b", "sports", lambda m: "Soccer"),
    (r"\b(formula.?1|f1|formula one|grand prix|gp racing)\b",       "sports", lambda m: "F1"),
    (r"\b(pga|golf|masters|us open golf|the open championship|ryder cup)\b", "sports", lambda m: "Golf"),
    (r"\b(politics|election)\b",                                     "interests", lambda m: "Politics"),
    (r"\b(crypto|bitcoin|ethereum|btc)\b",                           "interests", lambda m: "Crypto"),

    # ── Experience level ──────────────────────────────────────────────────────
    (
        r"\b(?:i(?:'m| am) (?:new|a newbie|just starting|a beginner)|"
        r"never (?:used|tried|done) (?:prediction markets?|polymarket|kalshi)|"
        r"just (?:signed up|joined|started))\b",
        "experience_level", "beginner",
    ),
    (
        r"\b(?:i(?:'ve| have) (?:been|traded|used).{0,30}(?:polymarket|kalshi|prediction markets?)|"
        r"i(?:'m| am) (?:experienced|a trader|familiar with)|"
        r"(?:traded|been trading).{0,20}(?:for|since))\b",
        "experience_level", "experienced",
    ),

    # ── Market category preferences ───────────────────────────────────────────
    (r"\b(?:prefer|like|into|mostly|mainly)\s+(?:sports|nba|nfl|mlb|nhl)\b",
     "market_prefs", lambda m: "Sports"),
    (r"\b(?:prefer|like|into|mostly|mainly)\s+politics\b",
     "market_prefs", lambda m: "Politics"),
    (r"\b(?:prefer|like|into|mostly|mainly)\s+crypto\b",
     "market_prefs", lambda m: "Crypto"),
    (r"\b(?:prefer|like|into|mostly|mainly)\s+economics?\b",
     "market_prefs", lambda m: "Economics"),

    # ── Alert threshold preference ─────────────────────────────────────────────
    (
        r"\b(?:only (?:send|show|alert|notify) (?:me )?(?:high.?confidence|strong|best|top)|"
        r"(?:don(?:'t| not) (?:want|need)|skip) (?:weak|low.?confidence|marginal) alerts?)\b",
        "alert_threshold", "high-confidence-only",
    ),
    (
        r"\b(?:(?:send|show|give) (?:me )?(?:all|every|any)|"
        r"i(?:'ll| will) (?:filter|decide)|don(?:'t| not) miss (?:any|anything))\b",
        "alert_threshold", "all-signals",
    ),

    # ── Fantasy / DFS ─────────────────────────────────────────────────────────
    (
        r"\b(?:play(?:ing|s)?|do|into|love)\s+(?:fantasy|dfs|draftkings|fanduel|"
        r"daily fantasy|fantasy (?:sports?|basketball|football|baseball|hockey))\b",
        "plays_fantasy", "yes",
    ),
    (
        r"\b(?:don(?:'t| not) (?:play|do)|not into|no) (?:fantasy|dfs)\b",
        "plays_fantasy", "no",
    ),

    # ── Rival player (player they love to see lose) ───────────────────────────
    (
        r"(?:hate|can'?t stand|dislike|despise|love (?:to )?(?:see|watch).{0,10}(?:lose|fail)|"
        r"biggest (?:villain|enemy)|least fav(?:orite)? player)\s+"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        "rival_players",
        lambda m: m.group(1).strip(),
    ),
    (
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:is|makes?)\s+(?:my )?(?:least fav|"
        r"most hated|annoying|overrated|can'?t stand)",
        "rival_players",
        lambda m: m.group(1).strip(),
    ),

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
    has_sport  = bool(
        facts.get("sports") or facts.get("fav_nba_teams") or facts.get("fav_nfl_teams")
        or facts.get("fav_mlb_teams") or facts.get("fav_nhl_teams")
        or facts.get("fav_cfb_teams") or facts.get("fav_cbb_teams")
        or facts.get("fav_mls_teams") or facts.get("fav_soccer_clubs")
        or facts.get("fav_wnba_teams") or facts.get("fav_ncaaw_teams")
        or facts.get("fav_f1") or facts.get("fav_golfers")
        or facts.get("nba_teams") or facts.get("nfl_teams")
    )
    has_player = bool(facts.get("fav_players") or facts.get("fav_f1") or facts.get("fav_golfers"))
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
            user_id          INTEGER PRIMARY KEY,
            first_name       TEXT,
            username         TEXT,
            facts            TEXT NOT NULL DEFAULT '{}',
            moments          TEXT NOT NULL DEFAULT '[]',
            trading_prefs    TEXT NOT NULL DEFAULT '{}',
            onboarding_asked TEXT NOT NULL DEFAULT '[]',
            created_at       REAL NOT NULL,
            last_seen        REAL NOT NULL,
            message_count    INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Migrate existing DBs that predate the onboarding_asked column
    try:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN onboarding_asked TEXT NOT NULL DEFAULT '[]'")
    except Exception:
        pass  # column already exists — fine
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

        # Favorite teams (strong signal) — pro sports
        for key, label in [
            ("fav_nba_teams", "NBA"), ("fav_nfl_teams", "NFL"),
            ("fav_mlb_teams", "MLB"), ("fav_nhl_teams", "NHL"),
        ]:
            teams = facts.get(key, [])
            if teams:
                lines.append(f"❤️ Favorite {label} team(s): {', '.join(teams)}")

        # Favorite teams — college sports
        for key, label in [
            ("fav_cfb_teams", "College Football"), ("fav_cbb_teams", "College Basketball"),
        ]:
            teams = facts.get(key, [])
            if teams:
                lines.append(f"❤️ Favorite {label} team(s): {', '.join(teams)}")

        # Favorite teams — soccer
        for key, label in [
            ("fav_mls_teams", "MLS"), ("fav_soccer_clubs", "Soccer Club"),
        ]:
            teams = facts.get(key, [])
            if teams:
                lines.append(f"❤️ Favorite {label}: {', '.join(teams)}")

        # Favorite teams — WNBA / NCAAW
        for key, label in [
            ("fav_wnba_teams", "WNBA"), ("fav_ncaaw_teams", "Women's CBB"),
        ]:
            teams = facts.get(key, [])
            if teams:
                lines.append(f"❤️ Favorite {label} team(s): {', '.join(teams)}")

        # Favorite F1 / Golf
        fav_f1 = facts.get("fav_f1", [])
        if fav_f1:
            lines.append(f"🏎️ Favorite F1 team/driver: {', '.join(fav_f1)}")
        fav_golfers = facts.get("fav_golfers", [])
        if fav_golfers:
            lines.append(f"⛳ Favorite golfer(s): {', '.join(fav_golfers)}")

        # Passively mentioned teams (weaker signal)
        for key, label in [
            ("nba_teams", "NBA"), ("nfl_teams", "NFL"),
            ("mlb_teams", "MLB"), ("nhl_teams", "NHL"),
            ("cfb_teams", "CFB"), ("cbb_teams", "CBB"),
            ("mls_teams", "MLS"), ("soccer_clubs", "Soccer"),
            ("wnba_teams", "WNBA"), ("ncaaw_teams", "NCAAW"),
            ("f1_teams", "F1"), ("pga_golfers", "PGA"),
        ]:
            fav_key_map = {
                "soccer_clubs": "fav_soccer_clubs",
                "f1_teams":     "fav_f1",
                "pga_golfers":  "fav_golfers",
                "wnba_teams":   "fav_wnba_teams",
                "ncaaw_teams":  "fav_ncaaw_teams",
            }
            fav_key = fav_key_map.get(key, f"fav_{key}")
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

        # Rival players
        rival_players = facts.get("rival_players", [])
        if rival_players:
            lines.append(f"😠 Rival player(s) (loves to see lose): {', '.join(rival_players)}")

        # Market category preferences
        mkt_prefs = facts.get("market_prefs", [])
        if mkt_prefs:
            lines.append(f"Preferred market types: {', '.join(mkt_prefs)}")

        # Platforms
        platforms = facts.get("platforms", [])
        if platforms:
            lines.append(f"Uses: {', '.join(platforms)}")

        # Experience level
        exp = facts.get("experience_level", [])
        if exp:
            lines.append(f"Prediction market experience: {exp[-1]}")

        # Fantasy / DFS
        fantasy = facts.get("plays_fantasy", [])
        if fantasy and fantasy[-1] == "yes":
            lines.append("Plays fantasy/DFS: yes — injury alerts are extra important")

        # Alert threshold preference
        thresh = facts.get("alert_threshold", [])
        if thresh:
            lines.append(f"Alert preference: {thresh[-1]}")

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

        # Onboarding hint for AI (only show fields not yet captured AND not yet asked)
        onboard_hint = ""
        if needs_onboarding(profile) and profile.get("message_count", 0) <= 10:
            asked   = set(profile.get("onboarding_asked", []))
            missing = []
            if not players and "fav_player" not in asked:
                missing.append("favorite player")
            if not cities and "city" not in asked:
                missing.append("their city/location")
            if not (facts.get("fav_nba_teams") or facts.get("fav_nfl_teams")) \
               and "fav_team" not in asked:
                missing.append("favorite team")
            if missing:
                onboard_hint = (
                    f"\nSTILL UNKNOWN (not yet asked): {', '.join(missing)}. "
                    "get_onboarding_prompt() will surface these one at a time — "
                    "do not ask them all at once."
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
        Return an AI instruction string to gather the NEXT missing profile field
        for new users — one question at a time, never repeating a question already asked.

        Each call marks the returned question key as asked so the same question
        is never surfaced again (even if the user doesn't answer).

        Returns empty string if:
          - User is not new (message_count > 5)
          - All 11 questions have been asked or answered
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
        asked   = set(profile.get("onboarding_asked", []))  # keys already surfaced

        # ── Ordered list of 11 onboarding questions ──────────────────────────
        # Each entry: (key, check_fn, ai_instruction)
        #   key          — unique identifier; stored in onboarding_asked once surfaced
        #   check_fn     — returns True if fact already captured (skip question)
        #   ai_instruction — what to tell the AI to work into conversation naturally

        questions: list[tuple[str, Any, str]] = [
            # 1. Sport interests (highest priority — gates everything else)
            (
                "sports",
                lambda f: bool(f.get("sports") or f.get("fav_nba_teams") or
                               f.get("fav_nfl_teams") or f.get("fav_mlb_teams") or
                               f.get("fav_nhl_teams")),
                "Casually ask which sports they follow or care about — "
                "make it feel like natural curiosity, not a form field.",
            ),
            # 2. Favorite team
            (
                "fav_team",
                lambda f: bool(
                    f.get("fav_nba_teams") or f.get("fav_nfl_teams") or
                    f.get("fav_mlb_teams") or f.get("fav_nhl_teams") or
                    f.get("fav_cfb_teams") or f.get("fav_cbb_teams") or
                    f.get("fav_mls_teams") or f.get("fav_soccer_clubs")
                ),
                "Weave in a question about their favorite team — "
                "'who are you rooting for?' or similar, context-appropriate.",
            ),
            # 3. Favorite player
            (
                "fav_player",
                lambda f: bool(f.get("fav_players")),
                "Find a natural moment to ask who their favorite player is — "
                "maybe after mentioning a team or a current game.",
            ),
            # 4. City / location (for timezone-accurate alerts)
            (
                "city",
                lambda f: bool(f.get("city")),
                "Casually ask where they're based or what city they're in — "
                "frame it around giving them time-accurate game alerts.",
            ),
            # 5. Experience level with prediction markets
            (
                "experience",
                lambda f: bool(f.get("experience_level")),
                "Gauge their familiarity with prediction markets — are they new "
                "to Polymarket/Kalshi or already experienced? Keep it casual.",
            ),
            # 6. Rival / hated team
            (
                "rival_team",
                lambda f: bool(f.get("rival_teams")),
                "In a playful way, ask if there's a team they can't stand or love "
                "to see lose — helps personalize rivalry alerts.",
            ),
            # 7. Plays fantasy / DFS
            (
                "fantasy",
                lambda f: bool(f.get("plays_fantasy")),
                "Ask if they play fantasy sports or DFS — injury alerts become "
                "much more urgent if so. Keep it conversational.",
            ),
            # 8. Market category preferences
            (
                "market_prefs",
                lambda f: bool(f.get("market_prefs")),
                "Ask what types of markets they're most interested in — sports, "
                "politics, crypto, economics — to tailor signal alerts.",
            ),
            # 9. Alert preference (all signals vs. high-confidence only)
            (
                "alert_threshold",
                lambda f: bool(f.get("alert_threshold")),
                "Ask whether they want every signal EDGE finds, or only the "
                "highest-confidence ones — frame it as a filter preference.",
            ),
            # 10. Rival player (player they love to see lose)
            (
                "rival_player",
                lambda f: bool(f.get("rival_players")),
                "Ask if there's a player they love to see lose or find overrated — "
                "keeps injury/return alerts personalized on both sides.",
            ),
        ]

        # Find the first question not yet answered AND not yet asked
        for key, check_fn, instruction in questions:
            if check_fn(facts):
                continue   # already answered — skip silently
            if key in asked:
                continue   # already asked but not answered — move on, don't repeat

            # Mark this question as asked NOW so it won't repeat even if user ignores it
            asked.add(key)
            with self._conn:
                self._conn.execute(
                    "UPDATE user_profiles SET onboarding_asked = ? WHERE user_id = ?",
                    (json.dumps(sorted(asked)), user_id),
                )

            return (
                "\nNEW USER ONBOARDING: "
                + instruction
                + " One question only — do NOT ask multiple things at once. "
                "If the conversation doesn't have a natural opening, wait for the next message."
            )

        return ""  # all questions asked or answered

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
                facts.get("fav_mlb_teams", []) + facts.get("fav_nhl_teams", []) +
                facts.get("fav_cfb_teams", []) + facts.get("fav_cbb_teams", []) +
                facts.get("fav_mls_teams", []) + facts.get("fav_soccer_clubs", []) +
                facts.get("fav_wnba_teams", []) + facts.get("fav_ncaaw_teams", []) +
                facts.get("fav_f1", []) + facts.get("fav_golfers", [])
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
                    facts.get("fav_mlb_teams", []) + facts.get("fav_nhl_teams", []) +
                    facts.get("fav_cfb_teams", []) + facts.get("fav_cbb_teams", []) +
                    facts.get("fav_mls_teams", []) + facts.get("fav_soccer_clubs", []) +
                    facts.get("fav_wnba_teams", []) + facts.get("fav_ncaaw_teams", []) +
                    facts.get("fav_f1", []) + facts.get("fav_golfers", [])
                )
            ]
            if any(t in ft or ft in t for ft in fav_teams):
                result.append(row["user_id"])
        return result

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["facts"]            = json.loads(d.get("facts", "{}") or "{}")
        d["moments"]          = json.loads(d.get("moments", "[]") or "[]")
        d["trading_prefs"]    = json.loads(d.get("trading_prefs", "{}") or "{}")
        d["onboarding_asked"] = json.loads(d.get("onboarding_asked", "[]") or "[]")
        return d
