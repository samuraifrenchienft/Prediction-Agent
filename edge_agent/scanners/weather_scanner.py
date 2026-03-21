"""
Weather Scanner — Open-Meteo + Kalshi/Polymarket weather market gap detector.
=============================================================================

Flow:
  1. Fetch active weather-themed markets from Kalshi API (keyword search)
  2. Parse market title → extract city + weather condition + threshold
  3. Query Open-Meteo free forecast API (no API key needed)
  4. Convert forecast into a model probability for the market condition
  5. Compare model prob vs market price — gaps > MIN_GAP_PP fire alerts

Open-Meteo docs : https://open-meteo.com/en/docs
Kalshi weather  : temperature, snowfall, precipitation, storm markets
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# City coordinate lookup — lat/lon for Open-Meteo API
# ---------------------------------------------------------------------------

_CITY_COORDS: dict[str, tuple[float, float]] = {
    "new york":      (40.71, -74.01),
    "nyc":           (40.71, -74.01),
    "manhattan":     (40.78, -73.97),
    "chicago":       (41.88, -87.63),
    "los angeles":   (34.05, -118.24),
    "la":            (34.05, -118.24),
    "miami":         (25.77, -80.19),
    "boston":        (42.36, -71.06),
    "dallas":        (32.78, -96.80),
    "houston":       (29.76, -95.37),
    "seattle":       (47.61, -122.33),
    "denver":        (39.74, -104.98),
    "phoenix":       (33.45, -112.07),
    "atlanta":       (33.75, -84.39),
    "minneapolis":   (44.98, -93.27),
    "detroit":       (42.33, -83.05),
    "philadelphia":  (39.95, -75.17),
    "philly":        (39.95, -75.17),
    "washington":    (38.91, -77.04),
    "dc":            (38.91, -77.04),
    "san francisco": (37.77, -122.42),
    "sf":            (37.77, -122.42),
    "las vegas":     (36.17, -115.14),
    "portland":      (45.52, -122.68),
    "nashville":     (36.17, -86.78),
    "charlotte":     (35.23, -80.84),
    "raleigh":       (35.78, -78.64),
    "salt lake":     (40.76, -111.89),
    "kansas city":   (39.10, -94.58),
    "new orleans":   (29.95, -90.07),
    "tampa":         (27.97, -82.46),
    "orlando":       (28.54, -81.38),
    "pittsburgh":    (40.44, -79.99),
    "cleveland":     (41.50, -81.69),
    "indianapolis":  (39.77, -86.16),
    "columbus":      (39.96, -82.99),
    "memphis":       (35.15, -90.05),
    "baltimore":     (39.29, -76.61),
    "milwaukee":     (43.04, -87.91),
    "oklahoma city": (35.47, -97.51),
    "louisville":    (38.25, -85.76),
    "richmond":      (37.54, -77.44),
    "jacksonville":  (30.33, -81.66),
    "austin":        (30.27, -97.74),
    "san antonio":   (29.42, -98.49),
    "san diego":     (32.72, -117.16),
    "sacramento":    (38.58, -121.49),
    "buffalo":       (42.89, -78.88),
    "hartford":      (41.76, -72.68),
    "omaha":         (41.26, -95.94),
    "tucson":        (32.22, -110.97),
    "albuquerque":   (35.08, -106.65),
    "boise":         (43.62, -116.20),
    "anchorage":     (61.22, -149.90),
    "honolulu":      (21.31, -157.86),
}

# Minimum edge gap in pp to fire an alert
_MIN_GAP_PP = 15.0

# In-process cache: cache_key → (data, fetched_unix_ts)
_FORECAST_CACHE: dict[tuple, tuple] = {}
_FORECAST_TTL = 3600  # 1 hour


# ---------------------------------------------------------------------------
# Data class for results
# ---------------------------------------------------------------------------

@dataclass
class WeatherGap:
    title:       str
    ticker:      str
    venue:       str
    market_prob: float   # current YES price (0.0–1.0)
    model_prob:  float   # Open-Meteo model estimate (0.0–1.0)
    gap_pp:      float   # model − market, signed, in percentage points
    action:      str     # "BUY YES" or "BUY NO"
    city:        str
    condition:   str     # "temp_above" | "temp_below" | "snow" | "rain"
    forecast_summary: str  # human-readable forecast snippet


# ---------------------------------------------------------------------------
# Open-Meteo helpers
# ---------------------------------------------------------------------------

def _fetch_open_meteo(lat: float, lon: float, days: int = 7) -> Optional[dict]:
    """
    Fetch hourly temperature + precipitation from Open-Meteo (free, no key).
    Returns parsed JSON dict or None on failure.
    """
    cache_key = (round(lat, 2), round(lon, 2), days)
    cached = _FORECAST_CACHE.get(cache_key)
    if cached:
        data, ts = cached
        if time.time() - ts < _FORECAST_TTL:
            return data

    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude":           lat,
                "longitude":          lon,
                "hourly":             "temperature_2m,precipitation_probability,snowfall",
                "temperature_unit":   "fahrenheit",
                "precipitation_unit": "inch",
                "forecast_days":      days,
                "timezone":           "auto",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        _FORECAST_CACHE[cache_key] = (data, time.time())
        log.debug("[WeatherScan] Fetched Open-Meteo: (%.2f, %.2f) %d days", lat, lon, days)
        return data
    except Exception as exc:
        log.debug("[WeatherScan] Open-Meteo fetch failed (%.2f, %.2f): %s", lat, lon, exc)
        return None


def _daily_max_temps(forecast: dict) -> list[float]:
    """Extract daily high temperatures (°F) from hourly data."""
    temps = forecast.get("hourly", {}).get("temperature_2m", [])
    times = forecast.get("hourly", {}).get("time", [])
    if not temps or not times:
        return []
    daily: dict[str, list[float]] = {}
    for t, temp in zip(times, temps):
        if temp is not None:
            daily.setdefault(t[:10], []).append(float(temp))
    return [max(v) for v in daily.values() if v]


def _daily_precip_prob(forecast: dict) -> list[float]:
    """Extract daily max precipitation probability (0.0–1.0) from hourly data."""
    probs = forecast.get("hourly", {}).get("precipitation_probability", [])
    times = forecast.get("hourly", {}).get("time", [])
    if not probs or not times:
        return []
    daily: dict[str, list[float]] = {}
    for t, p in zip(times, probs):
        if p is not None:
            daily.setdefault(t[:10], []).append(float(p))
    return [max(v) / 100.0 for v in daily.values() if v]


def _daily_snow_inches(forecast: dict) -> list[float]:
    """Extract daily total snowfall (inches) from hourly data."""
    snow  = forecast.get("hourly", {}).get("snowfall", [])
    times = forecast.get("hourly", {}).get("time", [])
    if not snow or not times:
        return []
    daily: dict[str, list[float]] = {}
    for t, s in zip(times, snow):
        if s is not None:
            daily.setdefault(t[:10], []).append(float(s))
    return [sum(v) for v in daily.values() if v]


# ---------------------------------------------------------------------------
# Market title parser
# ---------------------------------------------------------------------------

_TEMP_ABOVE_RE = re.compile(
    r"(?:reach|hit|exceed|above|over|at least)\s+(\d+)\s*(?:degrees?\s*)?°?\s*f",
    re.IGNORECASE,
)
_TEMP_BELOW_RE = re.compile(
    r"(?:below|under|drop(?:s)?\s+(?:to\s+)?below|not\s+reach)\s+(\d+)\s*(?:degrees?\s*)?°?\s*f",
    re.IGNORECASE,
)
_SNOW_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[+]?\s*(?:or\s+more\s+)?inch(?:es)?\s*(?:of\s+snow(?:fall)?)?",
    re.IGNORECASE,
)
_RAIN_RE = re.compile(
    r"\brain(?:fall|y)?\b|\bprecipitation\b|\brainy\b",
    re.IGNORECASE,
)


def _parse_weather_market(title: str) -> Optional[dict]:
    """
    Parse a market title into a weather condition spec.

    Returns dict with keys: type, city, lat, lon, threshold
    or None if the title can't be parsed into a known weather condition.
    """
    t = title.lower()

    # Find city — longest match wins
    city_match = None
    coords     = None
    for city, c in sorted(_CITY_COORDS.items(), key=lambda x: -len(x[0])):
        if city in t:
            city_match = city
            coords     = c
            break

    if not coords:
        return None

    lat, lon = coords

    m = _TEMP_ABOVE_RE.search(title)
    if m:
        return {"type": "temp_above", "city": city_match,
                "lat": lat, "lon": lon, "threshold": float(m.group(1))}

    m = _TEMP_BELOW_RE.search(title)
    if m:
        return {"type": "temp_below", "city": city_match,
                "lat": lat, "lon": lon, "threshold": float(m.group(1))}

    m = _SNOW_RE.search(title)
    if m:
        return {"type": "snow", "city": city_match,
                "lat": lat, "lon": lon, "threshold": float(m.group(1))}

    if _RAIN_RE.search(title):
        return {"type": "rain", "city": city_match,
                "lat": lat, "lon": lon, "threshold": 0.0}

    return None


# ---------------------------------------------------------------------------
# Model probability estimator
# ---------------------------------------------------------------------------

def _estimate_model_prob(spec: dict, forecast: dict) -> tuple[Optional[float], str]:
    """
    Estimate the YES probability for a weather market condition.

    Returns (probability, human_readable_summary) or (None, "").
    """
    condition = spec["type"]
    thr       = spec["threshold"]

    if condition == "temp_above":
        highs = _daily_max_temps(forecast)
        if not highs:
            return None, ""
        hits = sum(1 for t in highs if t >= thr)
        prob = hits / len(highs)
        avg  = sum(highs) / len(highs)
        peak = max(highs)
        summary = f"7-day highs: avg {avg:.0f}°F, peak {peak:.0f}°F (threshold {thr:.0f}°F)"
        return prob, summary

    elif condition == "temp_below":
        highs = _daily_max_temps(forecast)
        if not highs:
            return None, ""
        hits = sum(1 for t in highs if t < thr)
        prob = hits / len(highs)
        avg  = sum(highs) / len(highs)
        low  = min(highs)
        summary = f"7-day highs: avg {avg:.0f}°F, low {low:.0f}°F (threshold {thr:.0f}°F)"
        return prob, summary

    elif condition == "snow":
        totals = _daily_snow_inches(forecast)
        if not totals:
            return None, ""
        if thr <= 0:
            # Just asking if it snows at all
            prob = sum(1 for s in totals if s > 0.1) / len(totals)
            summary = f"Snow forecast: {max(totals):.1f}\" max daily, {sum(totals):.1f}\" total 7d"
        else:
            hits = sum(1 for s in totals if s >= thr)
            prob = min(hits / len(totals), 1.0)
            summary = (
                f"Snow forecast: {max(totals):.1f}\" max daily, {sum(totals):.1f}\" total 7d "
                f"(need {thr:.1f}\")"
            )
        return prob, summary

    elif condition == "rain":
        probs = _daily_precip_prob(forecast)
        if not probs:
            return None, ""
        prob    = max(probs)
        avg_p   = sum(probs) / len(probs)
        summary = f"Precip probability: {prob:.0%} peak, {avg_p:.0%} avg over 7 days"
        return prob, summary

    return None, ""


# ---------------------------------------------------------------------------
# Keyword filter — which markets are weather-related
# ---------------------------------------------------------------------------

_WEATHER_KEYWORDS = [
    "temperature", "degrees", "fahrenheit", "celsius",
    "snow", "snowfall", "snowstorm", "blizzard",
    "rain", "rainfall", "precipitation",
    "hurricane", "tropical storm", "tornado",
    "heat wave", "freeze", "frost",
    "weather", "climate",
    "high of", "low of", "above freezing",
]


def _is_weather_market(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _WEATHER_KEYWORDS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_weather_markets(markets: list[dict]) -> list[WeatherGap]:
    """
    Scan a list of market dicts for weather mispricings.

    Each market dict must contain:
        title / question  (str)   — the market question
        price / yes_price (float) — current YES price 0.0–1.0
        ticker / id       (str)   — unique identifier
        venue             (str)   — "kalshi" or "polymarket"

    Returns a sorted list of WeatherGap objects (largest gap first).
    """
    gaps: list[WeatherGap] = []
    n_checked = 0

    for mkt in markets:
        title  = mkt.get("title") or mkt.get("question", "")
        price  = float(mkt.get("price") or mkt.get("yes_price") or 0.5)
        ticker = mkt.get("ticker") or mkt.get("id", "")
        venue  = mkt.get("venue", "kalshi")

        if not _is_weather_market(title):
            continue

        spec = _parse_weather_market(title)
        if not spec:
            log.debug("[WeatherScan] Could not parse: '%s'", title[:60])
            continue

        n_checked += 1
        forecast = _fetch_open_meteo(spec["lat"], spec["lon"])
        if not forecast:
            continue

        model_prob, summary = _estimate_model_prob(spec, forecast)
        if model_prob is None:
            continue

        gap_pp = (model_prob - price) * 100
        if abs(gap_pp) < _MIN_GAP_PP:
            continue

        action = "BUY YES" if gap_pp > 0 else "BUY NO"
        log.info(
            "[WeatherScan] Gap: '%s' | mkt=%.0f%% model=%.0f%% gap=%+.0fpp → %s",
            title[:60], price * 100, model_prob * 100, gap_pp, action,
        )
        gaps.append(WeatherGap(
            title            = title,
            ticker           = ticker,
            venue            = venue,
            market_prob      = round(price, 3),
            model_prob       = round(model_prob, 3),
            gap_pp           = round(gap_pp, 1),
            action           = action,
            city             = spec["city"].title(),
            condition        = spec["type"],
            forecast_summary = summary,
        ))

    log.info("[WeatherScan] Checked %d weather markets, found %d gaps", n_checked, len(gaps))
    return sorted(gaps, key=lambda g: -abs(g.gap_pp))


def fetch_weather_markets_from_kalshi(kalshi_api) -> list[dict]:
    """
    Convenience: pull weather-related markets from the Kalshi API client.
    Searches by keyword and returns a normalised list of market dicts.
    """
    markets: list[dict] = []
    for kw in ("temperature", "snow", "rain", "weather", "degrees"):
        try:
            raw = kalshi_api.get_markets(keyword=kw, status="open", limit=50)
            for m in raw:
                markets.append({
                    "title":  m.get("title", m.get("question", "")),
                    "price":  float(m.get("yes_bid", m.get("last_price", 0.5)) or 0.5),
                    "ticker": m.get("ticker", m.get("id", "")),
                    "venue":  "kalshi",
                })
        except Exception as exc:
            log.debug("[WeatherScan] Kalshi keyword '%s' fetch failed: %s", kw, exc)

    # De-duplicate by ticker
    seen: set[str] = set()
    deduped: list[dict] = []
    for m in markets:
        t = m["ticker"]
        if t not in seen:
            seen.add(t)
            deduped.append(m)
    return deduped
