"""
Win Probability — Injury Impact to Probability Shift Converter
==============================================================

Converts player injury data into direct win-probability shifts for use in
the prediction market edge calculation pipeline.

Method (as used by sharp prediction market traders):
  1. Injury status  → probability of playing (Out=0%, Doubtful=15%, etc.)
  2. Player lookup  → expected scoring impact above replacement (per game)
  3. Effective loss = full_impact × (1 - play_probability)
  4. Baseline win%  → implied scoring differential (logistic inverse)
  5. Adjusted diff  = baseline_diff - effective_loss
  6. Shift          = win_prob(adjusted_diff) - win_prob(baseline_diff)

Logistic model references (calibrated to real data):
  NHL: Win% = 1 / (1 + e^(-1.25 × goal_diff))      → +1.0 goal ≈ 74% win
  NBA: Win% = 1 / (1 + e^(-0.11 × pt_diff))        → +10 pts  ≈ 67% win
  NFL: Win% = 1 / (1 + e^(-0.14 × pt_diff))        → +3 pts   ≈ 60% win

Output is a signed probability shift (always ≤ 0 for injuries) that feeds
directly into the Catalyst.direction field — no scaling needed.
"""
from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Logistic model parameters per sport
# ---------------------------------------------------------------------------

_K: dict[str, float] = {
    "nhl": 1.25,   # 1.0 goal  diff → 74% win (source: NHL GAR analytics)
    "nba": 0.11,   # 10.0 pt   diff → 67% win (source: basketball-reference)
    "nfl": 0.14,   # 3.0  pt   diff → 60% win (source: NFLfastR calibration)
}

# ---------------------------------------------------------------------------
# Injury status → probability of actually playing
# ---------------------------------------------------------------------------

_PLAY_PROB: dict[str, float] = {
    "Out":             0.00,
    "Injured Reserve": 0.00,
    "Suspension":      0.00,
    "Doubtful":        0.15,   # Historically ~10-20% play
    "Questionable":    0.50,   # By definition ~50% play
    "Day-To-Day":      0.80,   # Minor limitation, usually plays
}

# ---------------------------------------------------------------------------
# Named player impact database — net scoring impact above replacement per game
# ---------------------------------------------------------------------------
# Units: NHL = goals/game, NBA = points/game, NFL = points/game
# Source: public GAR/WAR databases, FiveThirtyEight, BBall-Index, PFF

_NAMED_IMPACT: dict[str, dict] = {
    # ── NHL Skaters ──────────────────────────────────────────────────────────
    "connor mcdavid":      {"sport": "nhl", "impact": 0.35},  # ~#1 all-time rate
    "nathan mackinnon":    {"sport": "nhl", "impact": 0.32},
    "auston matthews":     {"sport": "nhl", "impact": 0.28},
    "leon draisaitl":      {"sport": "nhl", "impact": 0.26},
    "david pastrnak":      {"sport": "nhl", "impact": 0.22},
    "nikita kucherov":     {"sport": "nhl", "impact": 0.22},
    "cale makar":          {"sport": "nhl", "impact": 0.24},  # elite D-man
    "matthew tkachuk":     {"sport": "nhl", "impact": 0.20},
    "aleksander barkov":   {"sport": "nhl", "impact": 0.20},
    "adam fox":            {"sport": "nhl", "impact": 0.20},
    "tage thompson":       {"sport": "nhl", "impact": 0.18},
    "jack hughes":         {"sport": "nhl", "impact": 0.18},
    "roman josi":          {"sport": "nhl", "impact": 0.18},
    "kirill kaprizov":     {"sport": "nhl", "impact": 0.17},
    "mitchell marner":     {"sport": "nhl", "impact": 0.16},
    "brady tkachuk":       {"sport": "nhl", "impact": 0.16},
    "tim stutzle":         {"sport": "nhl", "impact": 0.16},
    "william nylander":    {"sport": "nhl", "impact": 0.14},
    "trevor zegras":       {"sport": "nhl", "impact": 0.13},
    # ── NHL Goalies (measured in goals saved above replacement / game) ─────
    "connor hellebuyck":   {"sport": "nhl", "impact": 0.22},
    "igor shesterkin":     {"sport": "nhl", "impact": 0.22},
    "andrei vasilevskiy":  {"sport": "nhl", "impact": 0.18},
    "thatcher demko":      {"sport": "nhl", "impact": 0.16},
    "jacob markstrom":     {"sport": "nhl", "impact": 0.15},
    "juuse saros":         {"sport": "nhl", "impact": 0.15},
    "ilya sorokin":        {"sport": "nhl", "impact": 0.15},
    "sergei bobrovsky":    {"sport": "nhl", "impact": 0.13},
    "jake oettinger":      {"sport": "nhl", "impact": 0.13},
    "linus ullmark":       {"sport": "nhl", "impact": 0.13},
    "adin hill":           {"sport": "nhl", "impact": 0.12},
    "marc-andre fleury":   {"sport": "nhl", "impact": 0.11},
    # ── NBA ──────────────────────────────────────────────────────────────────
    "nikola jokic":            {"sport": "nba", "impact": 12.0},
    "giannis antetokounmpo":   {"sport": "nba", "impact": 11.5},
    "lebron james":            {"sport": "nba", "impact": 10.5},
    "luka doncic":             {"sport": "nba", "impact": 10.0},
    "shai gilgeous-alexander": {"sport": "nba", "impact": 10.0},
    "stephen curry":           {"sport": "nba", "impact":  9.5},
    "steph curry":             {"sport": "nba", "impact":  9.5},
    "joel embiid":             {"sport": "nba", "impact": 10.0},
    "victor wembanyama":       {"sport": "nba", "impact":  9.5},
    "kevin durant":            {"sport": "nba", "impact":  9.0},
    "jayson tatum":            {"sport": "nba", "impact":  8.0},
    "damian lillard":          {"sport": "nba", "impact":  7.5},
    "anthony davis":           {"sport": "nba", "impact":  8.0},
    "ja morant":               {"sport": "nba", "impact":  7.5},
    "tyrese haliburton":       {"sport": "nba", "impact":  6.5},
    "donovan mitchell":        {"sport": "nba", "impact":  6.5},
    "devin booker":            {"sport": "nba", "impact":  6.5},
    "jimmy butler":            {"sport": "nba", "impact":  6.5},
    "paolo banchero":          {"sport": "nba", "impact":  5.5},
    "bam adebayo":             {"sport": "nba", "impact":  5.5},
    # ── NFL QBs ───────────────────────────────────────────────────────────────
    "patrick mahomes":         {"sport": "nfl", "impact": 8.0},
    "josh allen":              {"sport": "nfl", "impact": 7.5},
    "lamar jackson":           {"sport": "nfl", "impact": 7.0},
    "jalen hurts":             {"sport": "nfl", "impact": 6.5},
    "joe burrow":              {"sport": "nfl", "impact": 6.0},
    "trevor lawrence":         {"sport": "nfl", "impact": 4.5},
    "justin herbert":          {"sport": "nfl", "impact": 4.5},
    "c.j. stroud":             {"sport": "nfl", "impact": 4.5},
    "cj stroud":               {"sport": "nfl", "impact": 4.5},
    "tua tagovailoa":          {"sport": "nfl", "impact": 4.0},
    # ── NFL Skill Players ─────────────────────────────────────────────────────
    "christian mccaffrey":     {"sport": "nfl", "impact": 3.5},
    "ceedee lamb":             {"sport": "nfl", "impact": 2.5},
    "justin jefferson":        {"sport": "nfl", "impact": 2.5},
    "tyreek hill":             {"sport": "nfl", "impact": 2.5},
    "travis kelce":            {"sport": "nfl", "impact": 2.0},
    "davante adams":           {"sport": "nfl", "impact": 2.0},
    "cooper kupp":             {"sport": "nfl", "impact": 2.0},
    "derrick henry":           {"sport": "nfl", "impact": 2.5},
    "stefon diggs":            {"sport": "nfl", "impact": 1.8},
}

# ---------------------------------------------------------------------------
# Position + tier fallback when player is not in _NAMED_IMPACT
# Tier is inferred from star_multiplier passed in from injury_api.py
# ---------------------------------------------------------------------------

_POSITION_DEFAULTS: dict[str, dict[str, dict[str, float]]] = {
    "nhl": {
        "C":  {"elite": 0.28, "star": 0.18, "starter": 0.10, "depth": 0.05},
        "LW": {"elite": 0.22, "star": 0.15, "starter": 0.08, "depth": 0.04},
        "RW": {"elite": 0.22, "star": 0.15, "starter": 0.08, "depth": 0.04},
        "D":  {"elite": 0.22, "star": 0.16, "starter": 0.09, "depth": 0.04},
        "G":  {"elite": 0.20, "star": 0.14, "starter": 0.10, "depth": 0.05},
    },
    "nba": {
        "PG": {"elite": 10.0, "star": 7.0, "starter": 4.0, "depth": 2.0},
        "SG": {"elite":  9.0, "star": 6.0, "starter": 3.5, "depth": 1.5},
        "SF": {"elite":  9.5, "star": 6.5, "starter": 3.5, "depth": 1.5},
        "PF": {"elite":  9.0, "star": 6.0, "starter": 3.0, "depth": 1.5},
        "C":  {"elite": 10.0, "star": 7.0, "starter": 4.0, "depth": 2.0},
    },
    "nfl": {
        "QB": {"elite": 7.5, "star": 5.0, "starter": 3.0, "depth": 1.0},
        "RB": {"elite": 3.5, "star": 2.5, "starter": 1.5, "depth": 0.5},
        "WR": {"elite": 3.0, "star": 2.0, "starter": 1.0, "depth": 0.3},
        "TE": {"elite": 2.5, "star": 1.5, "starter": 0.8, "depth": 0.2},
    },
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def win_prob(diff: float, sport: str) -> float:
    """
    Logistic win probability from expected scoring differential.

    NHL: diff = goals/game advantage
    NBA: diff = points/game advantage
    NFL: diff = points/game advantage
    """
    k = _K.get(sport, 1.0)
    return 1.0 / (1.0 + math.exp(-k * diff))


def goal_diff_from_win_prob(p: float, sport: str) -> float:
    """
    Logistic inverse — convert win probability back to implied scoring differential.
    Used to anchor the injury shift calculation to a specific baseline win%.
    """
    p = max(0.001, min(0.999, p))
    k = _K.get(sport, 1.0)
    return math.log(p / (1.0 - p)) / k


def _infer_tier(star_multiplier: float) -> str:
    """Map star multiplier to a tier label for position-default lookup."""
    if star_multiplier >= 1.15:
        return "elite"
    if star_multiplier >= 1.10:
        return "star"
    if star_multiplier >= 1.05:
        return "starter"
    return "depth"


def player_goal_impact(
    player_name: str,
    position: str,
    sport: str,
    star_multiplier: float = 1.0,
) -> float:
    """
    Return the player's expected scoring impact above replacement per game.

    Checks the named player database first (exact fragment match on lowercase
    name), then falls back to position + tier defaults using the star multiplier
    inherited from injury_api._STAR_MULTIPLIERS.

    Returns 0.0 if no data available (catalyst falls back to static severity).
    """
    name_lower = player_name.lower()

    # 1. Named player database — fragment match
    for key, data in _NAMED_IMPACT.items():
        if key in name_lower and data.get("sport") == sport:
            return data["impact"]

    # 2. Position + tier defaults
    pos_defaults = _POSITION_DEFAULTS.get(sport, {}).get(position, {})
    if pos_defaults:
        tier = _infer_tier(star_multiplier)
        return pos_defaults.get(tier, 0.0)

    return 0.0


def injury_win_prob_shift(
    player_name: str,
    position: str,
    status: str,
    sport: str,
    base_win_prob: float = 0.50,
    star_multiplier: float = 1.0,
) -> tuple[float, float, str]:
    """
    Compute the expected win-probability shift caused by a player injury.

    Args:
        player_name:    Full player name (used for named DB lookup)
        position:       Position abbreviation (QB, C, PG, etc.)
        status:         Injury status string (Out, Doubtful, Questionable, etc.)
        sport:          Sport identifier (nhl, nba, nfl)
        base_win_prob:  Team's baseline win probability (default 50% = neutral)
        star_multiplier: From injury_api._STAR_MULTIPLIERS for tier inference

    Returns:
        (shift, effective_impact, explanation)
        shift:            Win-probability shift (≤ 0 for injuries)
        effective_impact: Expected scoring loss per game
        explanation:      Human-readable derivation string for thesis output

    Example:
        Connor McDavid, Out, NHL, base=65%
        → player impact = 0.35 goals/gm
        → play probability = 0%
        → effective loss = 0.35 goals
        → baseline diff = logit(0.65)/1.25 = +0.37 goals
        → adjusted diff = +0.37 - 0.35 = +0.02 goals
        → adjusted win% = 50.5%
        → shift = 50.5% - 65% = -14.5pp
    """
    play_prob   = _PLAY_PROB.get(status, 0.50)
    goal_impact = player_goal_impact(player_name, position, sport, star_multiplier)

    if goal_impact == 0.0:
        # No data available — caller falls back to static severity values
        return (0.0, 0.0, "")

    # Effective scoring loss = full impact × fraction of games missed
    effective_loss = goal_impact * (1.0 - play_prob)

    if effective_loss < 0.001:
        # Player is expected to play (status probably Day-To-Day with minor effect)
        return (0.0, 0.0, "")

    # Anchor to the team's actual baseline win probability
    baseline_diff    = goal_diff_from_win_prob(base_win_prob, sport)
    adjusted_diff    = baseline_diff - effective_loss
    adjusted_win_prob = win_prob(adjusted_diff, sport)

    shift = adjusted_win_prob - base_win_prob  # always ≤ 0

    # Build human-readable derivation for catalyst label and thesis
    unit       = "goals/gm" if sport == "nhl" else "pts/gm"
    play_pct   = play_prob * 100
    miss_pct   = (1.0 - play_prob) * 100
    explanation = (
        f"{player_name} | {goal_impact:.2f} {unit} impact | "
        f"{status} ({miss_pct:.0f}% miss prob) | "
        f"eff. loss {effective_loss:.2f} {unit} | "
        f"win-prob shift {shift:+.1%} from {base_win_prob:.0%} baseline"
    )

    return (shift, effective_loss, explanation)


# ---------------------------------------------------------------------------
# Quick reference table (for logging / debugging)
# ---------------------------------------------------------------------------

def shift_table(sport: str = "nhl") -> str:
    """Print a reference table of win-prob shifts at 50% baseline."""
    unit = "goals" if sport == "nhl" else "pts"
    rows = [f"{'Impact':>8}  {'WinProb':>8}  {'Shift':>8}"]
    rows.append("-" * 30)
    impacts = [0.35, 0.28, 0.22, 0.18, 0.14, 0.10, 0.06]
    for imp in impacts:
        diff  = 0.0 - imp          # removing impact from neutral game
        wp    = win_prob(diff, sport)
        shift = wp - 0.50
        rows.append(f"{imp:>6.2f} {unit}  {wp:>7.1%}  {shift:>+7.1%}")
    return "\n".join(rows)
