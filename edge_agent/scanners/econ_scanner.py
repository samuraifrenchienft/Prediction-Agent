"""
Econ / Fed Scanner — CME FedWatch + NY Fed rates + Kalshi/Polymarket gap detector.
===================================================================================

Data sources (all free, no API key required):
  NY Fed EFFR:   https://markets.newyorkfed.org/api/rates/effr/last/1.json
  NY Fed all:    https://markets.newyorkfed.org/api/rates/all/last/1.json
  Treasury yields: https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml
  CME FedWatch:  Scraped from public CME probability page
  FRED (optional): requires free API key — set FRED_API_KEY in .env

Flow:
  1. Fetch current Fed funds effective rate (EFFR) from NY Fed
  2. Fetch treasury yield curve (2y, 10y) to compute spread
  3. Estimate implied cut/hike probability from yield curve inversion + EFFR level
  4. Match to Kalshi Fed rate markets (keyword: "fed", "rate", "fomc", "interest")
  5. Alert when model probability diverges from market price by >= MIN_GAP_PP
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Minimum gap to fire an alert
_MIN_GAP_PP = 15.0

# Cache TTL: economic data doesn't change intraday
_RATES_CACHE: dict[str, tuple] = {}
_RATES_TTL = 3600  # 1 hour

# NY Fed API
_NYFED_RATES_URL = "https://markets.newyorkfed.org/api/rates/all/last/1.json"

# US Treasury par yield curve (XML, updated daily)
_TREASURY_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value=all"

# FRED API (optional) — used for broader economic calendar
_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class EconGap:
    title:        str
    ticker:       str
    venue:        str
    category:     str        # "fed_rate" | "inflation" | "recession" | "gdp" | "unemployment"
    market_prob:  float      # current YES price 0.0–1.0
    model_prob:   float      # our model estimate 0.0–1.0
    gap_pp:       float      # model − market, signed pp
    action:       str        # "BUY YES" or "BUY NO"
    signal_notes: str        # human-readable economic context
    data_points:  dict = field(default_factory=dict)   # underlying numbers


# ---------------------------------------------------------------------------
# Economic data fetchers
# ---------------------------------------------------------------------------

def _fetch_nyfed_rates() -> Optional[dict]:
    """
    Fetch current overnight rates from NY Fed.
    Returns dict with: effr, sofr, obfr or None on failure.
    """
    cached = _RATES_CACHE.get("nyfed")
    if cached:
        data, ts = cached
        if time.time() - ts < _RATES_TTL:
            return data

    try:
        r = requests.get(_NYFED_RATES_URL, timeout=8)
        r.raise_for_status()
        raw = r.json()

        rates = {}
        for entry in raw.get("refRates", []):
            rate_type = entry.get("type", "").upper()
            rate_val  = entry.get("percentRate")
            if rate_val is not None:
                rates[rate_type] = float(rate_val)

        _RATES_CACHE["nyfed"] = (rates, time.time())
        log.debug("[EconScan] NY Fed rates: %s", rates)
        return rates
    except Exception as exc:
        log.debug("[EconScan] NY Fed fetch failed: %s", exc)
        return None


def _fetch_treasury_yields() -> Optional[dict]:
    """
    Fetch latest US Treasury yield curve from Treasury.gov.
    Returns dict: {"2y": float, "5y": float, "10y": float, "30y": float}
    Uses a simplified HTTP request returning XML — parse key tenors.
    """
    cached = _RATES_CACHE.get("treasury")
    if cached:
        data, ts = cached
        if time.time() - ts < _RATES_TTL:
            return data

    try:
        # Use a simpler FRED-compatible URL for treasury yields
        # This returns the most recent 2-year and 10-year yields
        results = {}
        tenor_map = {
            "DGS2":  "2y",
            "DGS5":  "5y",
            "DGS10": "10y",
            "DGS30": "30y",
        }
        for series_id, label in tenor_map.items():
            try:
                r = requests.get(
                    "https://fred.stlouisfed.org/graph/fredgraph.csv",
                    params={"id": series_id},
                    timeout=6,
                )
                if r.status_code == 200:
                    lines = r.text.strip().split("\n")
                    # Last non-empty line
                    for line in reversed(lines):
                        parts = line.split(",")
                        if len(parts) == 2 and parts[1].strip() not in (".", ""):
                            try:
                                results[label] = float(parts[1].strip())
                                break
                            except ValueError:
                                continue
            except Exception:
                continue

        if results:
            _RATES_CACHE["treasury"] = (results, time.time())
            log.debug("[EconScan] Treasury yields: %s", results)
            return results
    except Exception as exc:
        log.debug("[EconScan] Treasury yield fetch failed: %s", exc)

    return None


def _get_econ_context() -> dict:
    """
    Aggregate all economic data into a single context dict.
    Returns empty dict on total failure.
    """
    ctx = {}
    rates  = _fetch_nyfed_rates()
    yields = _fetch_treasury_yields()

    if rates:
        ctx["effr"]  = rates.get("EFFR", rates.get("OBFR"))
        ctx["sofr"]  = rates.get("SOFR")
        ctx["obfr"]  = rates.get("OBFR")

    if yields:
        ctx["yield_2y"]  = yields.get("2y")
        ctx["yield_5y"]  = yields.get("5y")
        ctx["yield_10y"] = yields.get("10y")
        ctx["yield_30y"] = yields.get("30y")
        if yields.get("2y") and yields.get("10y"):
            ctx["yield_spread_2_10"] = yields["10y"] - yields["2y"]   # negative = inverted

    return ctx


# ---------------------------------------------------------------------------
# Fed rate probability model
# ---------------------------------------------------------------------------

def _estimate_fed_cut_prob(ctx: dict, n_meetings_ahead: int = 1) -> Optional[float]:
    """
    Estimate the probability of a Fed rate cut at the next N meetings,
    using the yield curve and current rate level as signals.

    Heuristic model:
      1. Deeply inverted curve (2y-10y spread < -50bps) → market expects cuts
      2. EFFR > 5% → historically high, more room to cut
      3. Flat/uninverted curve → holds are more likely
      4. Combine into a logistic score

    Returns probability 0.0–1.0 or None if data insufficient.
    """
    effr   = ctx.get("effr")
    spread = ctx.get("yield_spread_2_10")

    if effr is None and spread is None:
        return None

    # Base score: EFFR level
    # Historical: Fed cuts when EFFR > 4.5% and growth slows
    # Above 5%: ~70% weight toward cut eventually
    # 4-5%: neutral
    # Below 4%: ~30% toward cut
    effr_val = effr or 5.0
    if effr_val >= 5.25:
        base = 0.65
    elif effr_val >= 4.75:
        base = 0.50
    elif effr_val >= 4.0:
        base = 0.35
    else:
        base = 0.25

    # Yield curve signal — inversion = market expects cuts
    spread_val = spread if spread is not None else 0.0
    if spread_val < -0.75:
        curve_adj = +0.20   # deeply inverted → strong cut signal
    elif spread_val < -0.25:
        curve_adj = +0.10   # moderately inverted → mild cut signal
    elif spread_val > 0.50:
        curve_adj = -0.10   # steep normal curve → fewer cuts expected
    else:
        curve_adj = 0.0

    # Adjust for meetings ahead (more uncertainty = closer to 50%)
    raw = base + curve_adj
    # Pull toward 50% for meetings further out (uncertainty increases)
    attenuation = 0.15 * (n_meetings_ahead - 1)
    prob = raw + attenuation * (0.5 - raw)

    return max(0.05, min(0.95, prob))


def _estimate_fed_hike_prob(ctx: dict) -> Optional[float]:
    """Estimate probability of a rate hike — generally low in 2024–2026."""
    effr   = ctx.get("effr")
    spread = ctx.get("yield_spread_2_10")
    if effr is None and spread is None:
        return None

    # Steep curve + low rate = possibly hike; inverted + high rate = almost certainly not
    effr_val   = effr if effr is not None else 5.0
    spread_val = spread if spread is not None else 0.0

    if effr_val < 3.0 and spread_val > 1.0:
        return 0.25   # low rates, steep curve → modest hike risk
    elif effr_val < 4.0:
        return 0.10
    elif spread_val < -0.50:
        return 0.03   # inverted curve → hike almost impossible
    else:
        return 0.05


# ---------------------------------------------------------------------------
# Market category detector
# ---------------------------------------------------------------------------

_FED_KEYWORDS = [
    "federal reserve", "fed rate", "fomc", "rate cut", "rate hike",
    "interest rate", "basis points", "bps", "fed funds", "policy rate",
    "rate hold", "rate pause",
]
_INFLATION_KEYWORDS = [
    "cpi", "inflation", "pce", "price index", "core inflation",
    "consumer prices", "ppi",
]
_RECESSION_KEYWORDS = [
    "recession", "gdp contraction", "economic slowdown", "negative growth",
    "nber", "downturn",
]
_UNEMPLOYMENT_KEYWORDS = [
    "unemployment", "jobs", "nonfarm payroll", "jobless",
    "labor market", "jobs report",
]
_GDP_KEYWORDS = [
    "gdp", "gross domestic product", "economic growth", "real gdp",
]


def _detect_econ_category(title: str) -> Optional[str]:
    t = title.lower()
    if any(k in t for k in _FED_KEYWORDS):
        return "fed_rate"
    if any(k in t for k in _INFLATION_KEYWORDS):
        return "inflation"
    if any(k in t for k in _RECESSION_KEYWORDS):
        return "recession"
    if any(k in t for k in _UNEMPLOYMENT_KEYWORDS):
        return "unemployment"
    if any(k in t for k in _GDP_KEYWORDS):
        return "gdp"
    return None


# ---------------------------------------------------------------------------
# Market-specific probability estimators
# ---------------------------------------------------------------------------

def _estimate_econ_prob(title: str, category: str, ctx: dict) -> tuple[Optional[float], str]:
    """
    Estimate model probability for an economic prediction market.
    Returns (probability, notes_string).
    """
    t = title.lower()
    effr   = ctx.get("effr", 5.25)
    spread = ctx.get("yield_spread_2_10", -0.3)
    yield_2y  = ctx.get("yield_2y")
    yield_10y = ctx.get("yield_10y")

    notes_parts = []
    if effr:
        notes_parts.append(f"EFFR: {effr:.2f}%")
    if yield_2y:
        notes_parts.append(f"2y: {yield_2y:.2f}%")
    if yield_10y:
        notes_parts.append(f"10y: {yield_10y:.2f}%")
    if spread is not None:
        notes_parts.append(f"2y-10y spread: {spread:+.2f}%")

    if category == "fed_rate":
        # "Will the Fed cut rates at the [month] meeting?"
        is_cut  = any(w in t for w in ("cut", "lower", "reduce", "decrease"))
        is_hike = any(w in t for w in ("hike", "raise", "increase"))
        is_hold = any(w in t for w in ("hold", "pause", "unchanged", "maintain", "no change"))

        # Detect meetings ahead from title
        n_meetings = 1
        if re.search(r"second|2nd|two\s+meeting", t):
            n_meetings = 2
        elif re.search(r"third|3rd|three\s+meeting", t):
            n_meetings = 3

        # ── Basis-point magnitude extraction ─────────────────────────────
        # "Will the Fed cut by 50 basis points?" → scale probability down
        # because a 50bps cut is historically much rarer than 25bps.
        _BPS_RE = re.compile(r"(\d+)\+?\s*(?:basis\s*points?|bps|bp)", re.IGNORECASE)
        bps_match = _BPS_RE.search(title)
        bps_magnitude = int(bps_match.group(1)) if bps_match else 25  # default 25bps

        # BPS scaling factors — how likely is a cut/hike of this size?
        # 25bps: standard move → 1.0 (no scaling)
        # 50bps: rare, usually emergency → 0.40
        # 75bps+: crisis only → 0.15
        if bps_magnitude <= 25:
            bps_scale = 1.0
        elif bps_magnitude <= 50:
            bps_scale = 0.40
        else:
            bps_scale = 0.15

        if is_cut:
            prob = _estimate_fed_cut_prob(ctx, n_meetings)
            if prob is not None and bps_magnitude > 25:
                prob = max(0.03, prob * bps_scale)
                notes_parts.append(f"Signal: CUT {bps_magnitude}bps (scaled ×{bps_scale})")
            else:
                notes_parts.append(f"Signal: CUT market ({bps_magnitude}bps)")
            return prob, " | ".join(notes_parts)

        if is_hike:
            prob = _estimate_fed_hike_prob(ctx)
            if prob is not None and bps_magnitude > 25:
                prob = max(0.01, prob * bps_scale)
                notes_parts.append(f"Signal: HIKE {bps_magnitude}bps (scaled ×{bps_scale})")
            else:
                notes_parts.append(f"Signal: HIKE market ({bps_magnitude}bps)")
            return prob, " | ".join(notes_parts)

        if is_hold:
            cut_prob  = _estimate_fed_cut_prob(ctx, n_meetings) or 0.5
            hike_prob = _estimate_fed_hike_prob(ctx) or 0.05
            prob = max(0.02, 1.0 - cut_prob - hike_prob)
            notes_parts.append("Signal: HOLD market")
            return prob, " | ".join(notes_parts)

        # Generic rate market — harder to classify
        return None, ""

    elif category == "inflation":
        # Yield curve: if 2y > EFFR by >50bps → market expects inflation to persist
        # 10y > 2y by >50bps → growth/inflation expected
        if yield_2y and effr:
            inflation_premium = yield_2y - effr
            # Positive premium = market expects rates to rise (higher inflation)
            # Negative = expecting cuts (lower inflation)
            base = 0.5 + inflation_premium * 0.15
            prob = max(0.10, min(0.90, base))
            notes_parts.append(f"2y-EFFR spread: {inflation_premium:+.2f}%")
            return prob, " | ".join(notes_parts)
        return None, ""

    elif category == "recession":
        # Classic recession indicator: inverted yield curve
        # Spread < -50bps for sustained period → high recession probability
        if spread is not None:
            if spread < -0.75:
                prob = 0.65
            elif spread < -0.25:
                prob = 0.50
            elif spread < 0.10:
                prob = 0.35
            else:
                prob = 0.20
            notes_parts.append(f"Curve inversion depth: {spread:+.2f}%")
            return prob, " | ".join(notes_parts)
        return None, ""

    elif category == "unemployment":
        # Rough heuristic: inverted curve + high EFFR → rising unemployment likely
        if spread is not None and effr is not None:
            if spread < -0.50 and effr > 4.5:
                prob = 0.65  # recession conditions = likely unemployment rise
            elif spread < 0:
                prob = 0.55
            else:
                prob = 0.40
            return prob, " | ".join(notes_parts)
        return None, ""

    elif category == "gdp":
        # GDP contraction: inverted curve is the best predictor
        if spread is not None:
            if spread < -0.50:
                prob = 0.55  # historically reliable recession predictor
            elif spread < 0:
                prob = 0.40
            else:
                prob = 0.25
            return prob, " | ".join(notes_parts)
        return None, ""

    return None, ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_econ_markets(markets: list[dict]) -> list[EconGap]:
    """
    Scan a list of prediction market dicts for economic/Fed mispricings.

    Each market dict:
        title / question  (str)   — the market question
        price / yes_price (float) — current YES price 0.0–1.0
        ticker / id       (str)   — unique identifier
        venue             (str)   — "kalshi" or "polymarket"

    Returns list of EconGap sorted by |gap_pp| descending.
    """
    ctx  = _get_econ_context()
    gaps: list[EconGap] = []
    n_checked = 0

    for mkt in markets:
        title  = mkt.get("title") or mkt.get("question", "")
        price  = float(mkt.get("price") or mkt.get("yes_price") or 0.5)
        ticker = mkt.get("ticker") or mkt.get("id", "")
        venue  = mkt.get("venue", "kalshi")

        category = _detect_econ_category(title)
        if not category:
            continue

        n_checked += 1
        model_prob, notes = _estimate_econ_prob(title, category, ctx)
        if model_prob is None:
            continue

        gap_pp = (model_prob - price) * 100
        if abs(gap_pp) < _MIN_GAP_PP:
            continue

        action = "BUY YES" if gap_pp > 0 else "BUY NO"
        log.info(
            "[EconScan] Gap: '%s' [%s] | mkt=%.0f%% model=%.0f%% gap=%+.0fpp → %s",
            title[:60], category, price * 100, model_prob * 100, gap_pp, action,
        )

        gaps.append(EconGap(
            title        = title,
            ticker       = ticker,
            venue        = venue,
            category     = category,
            market_prob  = round(price, 3),
            model_prob   = round(model_prob, 3),
            gap_pp       = round(gap_pp, 1),
            action       = action,
            signal_notes = notes,
            data_points  = {k: v for k, v in ctx.items() if v is not None},
        ))

    log.info("[EconScan] Checked %d econ markets, found %d gaps", n_checked, len(gaps))
    return sorted(gaps, key=lambda g: -abs(g.gap_pp))


def get_econ_context_string() -> str:
    """
    Return a compact economic data string for AI prompt injection.
    """
    ctx = _get_econ_context()
    if not ctx:
        return ""

    lines = ["LIVE ECONOMIC DATA (NY Fed / Treasury):"]
    if ctx.get("effr"):
        lines.append(f"  Fed Funds (EFFR): {ctx['effr']:.2f}%")
    if ctx.get("sofr"):
        lines.append(f"  SOFR: {ctx['sofr']:.2f}%")
    if ctx.get("yield_2y"):
        lines.append(f"  US 2y Treasury: {ctx['yield_2y']:.2f}%")
    if ctx.get("yield_10y"):
        lines.append(f"  US 10y Treasury: {ctx['yield_10y']:.2f}%")
    if ctx.get("yield_spread_2_10") is not None:
        spread = ctx["yield_spread_2_10"]
        inv    = " (INVERTED ⚠️)" if spread < 0 else ""
        lines.append(f"  2y–10y Spread: {spread:+.2f}%{inv}")

    return "\n".join(lines) if len(lines) > 1 else ""
