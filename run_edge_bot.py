"""
EDGE Telegram Bot
=================
Runs the EDGE agent as an interactive Telegram bot.

Setup (one-time):
  1. Message @BotFather on Telegram → /newbot → copy the token
  2. Add to .env:  TELEGRAM_BOT_TOKEN=<your token>
  3. Send any message to your new bot, then run:
       python -c "
       import os, requests
       from dotenv import load_dotenv
       load_dotenv()
       r = requests.get(f'https://api.telegram.org/bot{os.environ[\"TELEGRAM_BOT_TOKEN\"]}/getUpdates')
       print(r.json())
       "
     Find your user_id in the output and add to .env:
     TELEGRAM_OWNER_ID=<your user_id>
     ALLOWED_USER_IDS=<comma-separated user_ids of people you want to allow>
     (legacy: TELEGRAM_CHAT_ID still works for a single group channel)
  4. pip install python-telegram-bot
  5. python run_edge_bot.py
  6. Each allowed user DMs the bot and sends /start — they get their own channel

Injury refresh schedule (Pacific time):
  09:00 PT — morning check (overnight changes, NHL morning skate, NFL Wed report)
  13:30 PT — mid-day (NBA official PDF window, NFL Thu/Fri report)
  16:30 PT — pre-game final (last-minute scratches, lineup confirmations)
  + startup warmup 60s after boot

Injury sources:
  NBA: ESPN + official NBA PDF + Sleeper API cross-ref + star player news check
  NFL: ESPN + Sleeper API cross-ref + star player news check
  NHL: ESPN + star player news check

Commands in Telegram:
  /scan              — run a full market scan immediately
  /injuries          — injury cache summary (count + freshness)
  /injuries nba      — full NBA player list sorted by severity
  /injuries nfl      — full NFL player list sorted by severity
  /injuries nhl      — full NHL player list sorted by severity
  /injuries cfb      — College Football injury list
  /injuries cbb      — College Basketball (men's) injury list
  /injuries wnba     — WNBA injury list
  /injuries ncaaw    — Women's College Basketball injury list
  /injuries nba lakers — filter NBA to Lakers only
  /injuries nhl oilers — filter NHL to Oilers only
  /tracking          — show the injury game tracking list
  /top               — show top 3 opportunities from last scan
  /status            — show last scan summary
  /help              — command list

  Or just send any message to chat with EDGE about markets.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import signal
import time
from datetime import datetime, timezone
from datetime import time as dt_time
from pathlib import Path

try:
    from zoneinfo import ZoneInfo

    _PACIFIC = ZoneInfo("America/Los_Angeles")
except ImportError:
    import datetime as _dt

    _PACIFIC = _dt.timezone(_dt.timedelta(hours=-8))  # PST fallback (no DST)

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import importlib

from edge_agent import (
    EdgeEngine,
    EdgeScanner,
    EdgeService,
    KalshiAdapter,
    PolymarketAdapter,
    PortfolioState,
)
from edge_agent.ai_service import get_chat_response
from edge_agent.memory import KnowledgeBase, SessionMemory
from edge_agent.game_tracker import TrackedGame
from edge_agent.models import Recommendation

_kalshi_api = importlib.import_module(".dat-ingestion.kalshi_api", "edge_agent")
_injury_mod = importlib.import_module(".dat-ingestion.injury_api", "edge_agent")
_InjuryClient = _injury_mod.InjuryAPIClient
_standings_mod = importlib.import_module(".dat-ingestion.standings_api", "edge_agent")
_StandingsClient = _standings_mod.StandingsClient
_standings_client = _StandingsClient()  # singleton

_trader_mod = importlib.import_module(".dat-ingestion.trader_api", "edge_agent")
_TraderClient = _trader_mod.TraderAPIClient

# ---------------------------------------------------------------------------
# Specialist scanners — weather, crypto, fed/econ
# ---------------------------------------------------------------------------
from edge_agent.scanners.weather_scanner import (
    scan_weather_markets,
    fetch_weather_markets_from_kalshi as _fetch_weather_mkts,
    WeatherGap,
)
from edge_agent.scanners.crypto_scanner import (
    scan_crypto_markets,
    get_crypto_price_context,
    CryptoGap,
)
from edge_agent.scanners.econ_scanner import (
    scan_econ_markets,
    get_econ_context_string,
    EconGap,
)

# Scanner alert dedup keys — prevents re-alerting same gap within cooldown window
_WEATHER_ALERTED: dict[str, float] = {}  # ticker → last_alerted unix ts
_CRYPTO_ALERTED: dict[str, float] = {}
_ECON_ALERTED: dict[str, float] = {}
_SPECIALIST_ALERT_COOLDOWN = 14400  # 4 hours — same gap won't re-fire

# Per-sport on-demand refresh rate limiter (unix timestamp of last trigger)
_ONDEMAND_REFRESH_COOLDOWN: dict[str, float] = {}

# ---------------------------------------------------------------------------
# Topic keyword → Polymarket tag_slug mapping (module-level so reusable)
# Used in Priority 3 market search AND the topic news search block.
# ---------------------------------------------------------------------------
_TOPIC_TAGS: dict[str, str] = {
    # Crypto
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
    "crypto": "crypto",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "solana": "solana",
    "xrp": "xrp",
    "dogecoin": "dogecoin",
    "doge": "dogecoin",
    # Politics / World
    "trump": "trump",
    "biden": "biden",
    "election": "elections",
    "democrat": "elections",
    "republican": "elections",
    "congress": "congress",
    "senate": "senate",
    "tariff": "tariffs",
    "ukraine": "ukraine",
    "russia": "russia",
    "china": "china",
    "israel": "israel",
    "iran": "iran",
    "gaza": "gaza",
    "nato": "nato",
    "war": "geopolitics",
    # Economics
    "fed": "fed-funds-rate",
    "fomc": "fed-funds-rate",
    "fed chair": "fed-chair",
    "inflation": "inflation",
    "recession": "recession",
    "gdp": "gdp",
    # Tech / Business
    "tesla": "tesla",
    "elon": "elon-musk",
    "musk": "elon-musk",
    "spacex": "spacex",
    "apple": "apple",
    "nvidia": "nvidia",
    "amazon": "amazon",
    "google": "google",
    "meta": "meta",
    "microsoft": "microsoft",
    "openai": "openai",
    "artificial intelligence": "ai",
    "ipo": "ipo",
    "stock market": "stocks",
    # Entertainment / Awards
    "oscar": "oscars",
    "oscars": "oscars",
    "academy award": "oscars",
    "emmy": "emmys",
    "grammy": "grammys",
    "golden globe": "golden-globes",
    "box office": "movies",
    "movie": "movies",
    "film": "movies",
    "celebrity": "celebrities",
    "taylor swift": "taylor-swift",
    # Sports expanded
    "nhl": "nhl",
    "hockey": "nhl",
    "stanley cup": "stanley-cup",
    "nfl": "nfl",
    "mlb": "mlb",
    "baseball": "mlb",
    "ufc": "ufc",
    "mma": "ufc",
    "boxing": "boxing",
    "golf": "golf",
    "pga": "golf",
    "tennis": "tennis",
    "soccer": "soccer",
    "mls": "mls",
    "champions league": "champions-league",
    "premier league": "premier-league",
    "march madness": "march-madness",
    "ncaa": "ncaa",
    "super bowl": "super-bowl",
    "world cup": "world-cup",
    "olympics": "olympics",
    "formula 1": "formula-1",
    "f1": "formula-1",
}

# Topic → optimized Tavily/Serper search query.
# Each query is tuned for the kind of info users actually need per topic.
# Falls back to "{tag} latest news today" for any unregistered tag.
_TOPIC_NEWS_QUERIES: dict[str, str] = {
    # Awards — users need current nominees, frontrunners, critic consensus
    "oscars": "2026 Academy Awards Best Picture nominees odds favorite who will win",
    "emmys": "Emmy Awards 2026 nominees predictions odds",
    "grammys": "Grammy Awards 2026 nominees predictions odds",
    "golden-globes": "Golden Globes 2026 nominees odds predictions",
    "taylor-swift": "Taylor Swift latest news tour album 2026",
    # Tech / Business — stock price + breaking news
    "tesla": "Tesla stock news latest today 2026",
    "elon-musk": "Elon Musk latest news today DOGE Twitter Tesla",
    "spacex": "SpaceX launch news latest 2026",
    "apple": "Apple stock earnings news latest today",
    "nvidia": "Nvidia stock AI chip news latest today",
    "openai": "OpenAI ChatGPT GPT latest news 2026",
    "amazon": "Amazon stock AWS news latest today",
    "google": "Google Alphabet stock AI news latest today",
    "meta": "Meta Facebook Instagram stock news latest today",
    "microsoft": "Microsoft stock Azure AI news latest today",
    "ai": "artificial intelligence AI news latest today 2026",
    "ipo": "IPO market latest filings upcoming IPOs 2026",
    "stocks": "stock market news today S&P 500 latest",
    "fed-chair": "Federal Reserve chair Powell latest news statements 2026",
    # Politics / World — breaking news critical
    "trump": "Trump latest news today executive order policy 2026",
    "biden": "Biden latest news today 2026",
    "elections": "US 2026 elections midterm latest polls news",
    "congress": "US Congress legislation bills latest news today 2026",
    "senate": "US Senate vote legislation latest news today 2026",
    "tariffs": "US tariffs trade war Canada Mexico China latest news today",
    "ukraine": "Ukraine Russia war ceasefire latest news today",
    "israel": "Israel Gaza ceasefire latest news today",
    "gaza": "Gaza ceasefire humanitarian latest news today 2026",
    "nato": "NATO alliance defense latest news today 2026",
    "china": "China US trade relations Taiwan latest news today",
    "russia": "Russia Ukraine latest news today",
    "iran": "Iran nuclear deal sanctions latest news today",
    "geopolitics": "global geopolitics latest breaking news today",
    # Economics — data releases + Fed decisions
    "fed-funds-rate": "Federal Reserve FOMC interest rate decision cut hike latest news",
    "inflation": "US CPI inflation report latest news today 2026",
    "recession": "US economy recession risk GDP latest news today",
    "gdp": "US GDP growth report latest news today",
    # Sports leagues — standings + injury news + results
    "nhl": "NHL standings results injury news latest today 2026",
    "stanley-cup": "NHL Stanley Cup playoffs bracket odds latest 2026",
    "nfl": "NFL draft free agency news latest today 2026",
    "mlb": "MLB standings results injury news latest today 2026",
    "ufc": "UFC fight results card news latest tonight 2026",
    "boxing": "boxing fight results card news latest 2026",
    "golf": "PGA Tour golf results leaderboard news latest today",
    "tennis": "tennis ATP WTA results news latest today",
    "formula-1": "Formula 1 F1 race results standings news latest today",
    "champions-league": "UEFA Champions League results standings news latest",
    "premier-league": "Premier League standings results injury news latest today",
    "march-madness": "NCAA March Madness tournament bracket results today 2026",
    "ncaa": "NCAA basketball tournament results bracket today",
    "super-bowl": "Super Bowl 2026 odds predictions latest news",
    "world-cup": "FIFA World Cup 2026 qualifying results news latest",
    "olympics": "Olympics 2026 news results latest",
    "mls": "MLS soccer standings results news latest today",
    "soccer": "soccer football results news latest today",
    # Crypto — price action + on-chain news
    "bitcoin": "Bitcoin BTC price news today latest 2026",
    "ethereum": "Ethereum ETH price news latest today 2026",
    "solana": "Solana SOL price news latest today",
    "xrp": "XRP Ripple price SEC news latest today",
    "dogecoin": "Dogecoin DOGE price news Elon latest today",
    "crypto": "crypto market Bitcoin Ethereum price news today",
    # Movies
    "movies": "box office results weekend latest movie news 2026",
    "celebrities": "celebrity entertainment news latest today",
}

load_dotenv()

# ---------------------------------------------------------------------------
# Tavily real-time web search
# ---------------------------------------------------------------------------


def _tavily_search(query: str, max_results: int = 5) -> str:
    """
    Fire a Tavily search and return a compact summary block for prompt injection.
    Returns "" if TAVILY_API_KEY is not set or the call fails.
    """
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        return ""
    try:
        from tavily import TavilyClient

        client = TavilyClient(api_key=api_key)
        response = client.search(
            query=query,
            search_depth="basic",
            max_results=max_results,
            include_answer=True,
        )
        lines = ["\n[Live web search results]"]
        # Top-level AI answer when available
        answer = response.get("answer") or ""
        if answer:
            lines.append(f"Summary: {answer.strip()}")
        # Individual results
        for r in response.get("results", [])[:max_results]:
            title = r.get("title", "").strip()
            content = r.get("content", "").strip()[:200]
            url = r.get("url", "")
            lines.append(f"• {title}: {content}  [{url}]")
        lines.append("[End web search]")
        return "\n".join(lines)
    except Exception as exc:
        log.debug("Tavily search failed: %s", exc)
        return ""


def _serper_search(query: str, max_results: int = 5) -> str:
    """
    Serper.dev Google search — fallback when Tavily quota is exhausted.
    Returns "" if SERPER_API_KEY is not set or the call fails.
    Free tier: 2,500 searches/month, no credit card required.
    Sign up: https://serper.dev
    """
    api_key = os.getenv("SERPER_API_KEY", "").strip()
    if not api_key:
        return ""
    try:
        import requests as _req

        resp = _req.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": max_results},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        lines = ["\n[Live web search results]"]
        if ans := data.get("answerBox", {}).get("answer"):
            lines.append(f"Summary: {ans}")
        for r in data.get("organic", [])[:max_results]:
            title = r.get("title", "").strip()
            snippet = r.get("snippet", "").strip()[:200]
            url = r.get("link", "")
            lines.append(f"• {title}: {snippet}  [{url}]")
        lines.append("[End web search]")
        return "\n".join(lines)
    except Exception as exc:
        log.debug("Serper search failed: %s", exc)
        return ""


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # silence getUpdates poll noise
log = logging.getLogger("edge_bot")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
OWNER_ID = os.environ.get(
    "TELEGRAM_OWNER_ID", ""
)  # your personal user ID (from @userinfobot)
ALERT_CHANNEL_ID = os.environ.get(
    "ALERT_CHANNEL_ID", CHAT_ID
)  # dedicated copy-trade alert channel
SCAN_INTERVAL_MIN = int(
    os.environ.get("SCAN_INTERVAL_MINUTES", "180")
)  # default 3 hours
INJURY_REFRESH_MIN = int(
    os.environ.get("INJURY_REFRESH_MINUTES", "240")
)  # default 4 hours
BANKROLL_USD = float(os.environ.get("BANKROLL_USD", "10000"))

# BallDontLie — NBA game schedule (free tier: /v1/games endpoint)
_BALLDONTLIE_API = os.environ.get("BALLDONTLIE_API", "")

# ---------------------------------------------------------------------------
# Approved signals — persisted across restarts
# ---------------------------------------------------------------------------

_APPROVALS_FILE = Path("edge_agent/memory/data/approvals.json")


def _load_approved_signals() -> set[str]:
    """Load persisted approved signal types from disk."""
    try:
        if _APPROVALS_FILE.exists():
            return set(json.loads(_APPROVALS_FILE.read_text()))
    except Exception:
        pass
    return set()


def _save_approved_signals(signals: set[str]) -> None:
    """Persist approved signal types to disk."""
    try:
        _APPROVALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _APPROVALS_FILE.write_text(json.dumps(sorted(signals)))
    except Exception as e:
        log.warning("Could not save approved signals: %s", e)


# ---------------------------------------------------------------------------
# Global state (shared across handlers)
# ---------------------------------------------------------------------------

_service: EdgeService | None = None
_scanner: EdgeScanner | None = None
_portfolio = PortfolioState(bankroll_usd=BANKROLL_USD)
_kb = KnowledgeBase()
_mem = SessionMemory()

# ---------------------------------------------------------------------------
# Platform docs — loaded once at startup for AI onboarding context
# ---------------------------------------------------------------------------
_DOCS_DIR = Path(__file__).parent / "docs"


def _load_platform_doc(filename: str) -> str:
    """Load a markdown doc from the docs/ folder. Returns '' on failure."""
    try:
        return (_DOCS_DIR / filename).read_text(encoding="utf-8")
    except Exception:
        return ""


_POLYMARKET_DOC = _load_platform_doc("polymarket_guide.md")
_KALSHI_DOC = _load_platform_doc("kalshi_guide.md")

_ONBOARD_KEYWORDS = {
    "sign up",
    "signup",
    "register",
    "how to start",
    "getting started",
    "deposit",
    "withdraw",
    "usdc",
    "how do i",
    "how to use",
    "what is polymarket",
    "what is kalshi",
    "how does",
    "fees",
    "wallet",
    "account",
    "polygon",
    "matic",
    "bridge",
    "swap",
    "coinbase",
    "how to buy",
    "how to trade",
    "new to",
    "beginner",
    "first time",
    "set up",
    "setup",
    "onboard",
}


def _get_platform_doc_context(user_msg: str) -> str:
    """Return relevant platform doc snippets when user asks onboarding questions."""
    q = user_msg.lower()
    if not any(kw in q for kw in _ONBOARD_KEYWORDS):
        return ""

    ctx = "\n\n[Platform Setup Reference]\n"
    if "kalshi" in q and _KALSHI_DOC:
        ctx += _KALSHI_DOC
    elif "polymarket" in q and _POLYMARKET_DOC:
        ctx += _POLYMARKET_DOC
    else:
        # Generic onboarding question — include both, trimmed
        if _POLYMARKET_DOC:
            ctx += "=== POLYMARKET ===\n" + _POLYMARKET_DOC[:1500]
        if _KALSHI_DOC:
            ctx += "\n\n=== KALSHI ===\n" + _KALSHI_DOC[:1500]
    return ctx


# Tracks already-alerted market keys to avoid duplicate alerts per scan cycle.
# Dict format: {key: first_alerted_timestamp} — entries expire after 24h so
# duplicate suppression survives restarts (re-alerted after 24h is fine) and
# the dict doesn't grow unbounded over long-running sessions.
#
# PERSISTED to disk — survives bot restarts so users don't get re-alerted on
# the same markets after every reboot.
_ALERTED_KEYS_FILE = os.path.join(
    os.path.dirname(__file__), "edge_agent", "memory", "data", "alerted_keys.json"
)
_ALERTED_KEYS_TTL = 86400  # 24h


def _load_alerted_keys() -> dict[str, float]:
    """Load persisted alerted keys from disk. Returns empty dict on any error."""
    try:
        if os.path.exists(_ALERTED_KEYS_FILE):
            with open(_ALERTED_KEYS_FILE, "r") as f:
                data = json.load(f)
            # Prune expired on load
            cutoff = time.time() - _ALERTED_KEYS_TTL
            return {k: ts for k, ts in data.items() if ts >= cutoff}
    except Exception as exc:
        log.warning("[alerted_keys] Failed to load from disk: %s", exc)
    return {}


def _save_alerted_keys() -> None:
    """Persist alerted keys to disk. Fire-and-forget — never raises."""
    try:
        with open(_ALERTED_KEYS_FILE, "w") as f:
            json.dump(_alerted_keys, f)
    except Exception as exc:
        log.warning("[alerted_keys] Failed to save to disk: %s", exc)


_alerted_keys: dict[str, float] = _load_alerted_keys()


def _prune_alerted_keys() -> None:
    """Remove alert-key entries older than _ALERTED_KEYS_TTL. Call before reads/writes."""
    cutoff = time.time() - _ALERTED_KEYS_TTL
    expired = [k for k, ts in _alerted_keys.items() if ts < cutoff]
    for k in expired:
        del _alerted_keys[k]
    if expired:
        _save_alerted_keys()  # persist after cleanup


# Approved signal types — only markets matching these signals will trigger alerts.
# Empty set means "alert on all" (bootstrapping mode until user approves something).
_approved_signals: set[str] = _load_approved_signals()

# Outcome tracker — resolution engine + paper trading DB
from edge_agent.memory.outcome_tracker import OutcomeTracker as _OutcomeTracker

_ot = _OutcomeTracker()

# ── ML overlay singletons ─────────────────────────────────────────────────────
# All ML modules are lazy-loaded and fail-safe: if xgboost/sklearn are not
# installed the system degrades gracefully to pure rule-based mode.
from edge_agent.ml.ml_store import MLStore as _MLStore
from edge_agent.ml.confidence_calibrator import (
    ConfidenceCalibrator as _ConfidenceCalibrator,
)
from edge_agent.ml.signal_scorer import SignalScorer as _SignalScorer
from edge_agent.ml.trader_features import (
    TraderFeatureExtractor as _TraderFeatureExtractor,
)
from edge_agent.ml.regime_detector import RegimeDetector as _RegimeDetector
import edge_agent.nodes as _nodes_mod

_ml_store = _MLStore()
_calibrator = _ConfidenceCalibrator(_ml_store)
_scorer = _SignalScorer()
_regime = _RegimeDetector(_ml_store)

# Load saved calibration + model from disk (no-ops if not yet trained)
_calibrator.load()
_scorer.load()
_nodes_mod.set_calibrator(_calibrator)  # inject into probability_node

# ── Decision log + Prompt registry ───────────────────────────────────────────
# decision_log audits every AI call: model used, prompt version, latency, context blocks
# prompt_registry provides versioned templates so we can trace why the AI said what it said
from edge_agent.memory.decision_log import DecisionLog as _DecisionLog
from edge_agent.prompt_registry import get_registry as _get_prompt_registry
import edge_agent.ai_service as _ai_svc_mod

_decision_log = _DecisionLog()
_prompt_registry = _get_prompt_registry()

# Wire DecisionLog into ai_service so every call is automatically audited
_ai_svc_mod.set_decision_log(_decision_log)

# TraderCache singleton — shared connection across all commands to avoid
# spawning a new DB connection on every /traders, /wallet, /watch invocation
from edge_agent.memory.trader_cache import TraderCache as _TraderCache
from edge_agent.insider_alerts import InsiderAlertEngine as _InsiderAlertEngine

_trader_cache: "_TraderCache | None" = None

# Smart money positions cache — refreshed every 30 min by background job.
# Stores open positions from top-scored watchlist wallets so the AI can
# reference what real traders are currently betting on without per-message latency.
_sm_positions_cache: dict = {
    "lines": [],
    "fetched_at": 0.0,
    "position_keys": set(),  # "addr:condId:side" keys seen in last cycle (for new-position diff)
    "alertable": [],  # list[dict] of new positions that passed quality filters this cycle
}
_sm_alerted_24h: dict[
    str, float
] = {}  # "addr:condId" → Unix timestamp of last alert sent
_SM_CACHE_TTL = 1800  # 30 minutes

# ---------------------------------------------------------------------------
# Insider alert engine — singleton, initialised lazily in main()
# ---------------------------------------------------------------------------
_insider_engine: "_InsiderAlertEngine | None" = None


def _get_insider_engine() -> "_InsiderAlertEngine":
    global _insider_engine
    if _insider_engine is None:
        # Pass the best available search function so AI research works immediately
        def _search_wrapper(query: str, max_results: int = 4) -> str:
            result = _tavily_search(query, max_results=max_results)
            if not result:
                result = _serper_search(query, max_results=max_results)
            return result

        _insider_engine = _InsiderAlertEngine(search_fn=_search_wrapper)
    return _insider_engine


def _get_trader_cache() -> "_TraderCache":
    """Return the module-level TraderCache singleton, creating it if needed."""
    global _trader_cache
    if _trader_cache is None:
        _trader_cache = _TraderCache()
    return _trader_cache


# Long-term per-user profile store (facts, moments, trading prefs)
from edge_agent.memory.user_profile import UserProfileStore as _UserProfileStore

_profiles = _UserProfileStore()

# Per-user SessionMemory instances — keyed by Telegram user_id
# Initialized on first message from each user, reused within the process lifetime
_user_sessions: dict[int, "SessionMemory"] = {}


def _get_session(user_id: int) -> "SessionMemory":
    """Return (or create) a per-user SessionMemory instance."""
    if user_id not in _user_sessions:
        _user_sessions[user_id] = SessionMemory(user_id=user_id)
    return _user_sessions[user_id]


# Per-user conversation history for multi-turn AI chat {user_id: [messages]}
_chat_history: dict[int, list[dict]] = {}

# Last scan summary text for /status
_last_status: str = "No scan run yet."

# Short-key store for inline keyboard callbacks (avoids 64-byte Telegram limit).
# Telegram callback_data max = 64 bytes; market IDs alone can exceed that.
# We map slot_id (short int string) → Recommendation so buttons stay tiny.
_rec_store: dict[str, "Recommendation"] = {}
_rec_slot_counter: int = 0


def _store_rec(rec: "Recommendation") -> str:
    """Store a recommendation and return a short slot key for callback_data."""
    global _rec_slot_counter
    _rec_slot_counter = (_rec_slot_counter + 1) % 10_000  # recycle after 10k
    slot = str(_rec_slot_counter)
    _rec_store[slot] = rec
    return slot


def _get_service() -> EdgeService:
    global _service
    if _service is None:
        engine = EdgeEngine()
        _service = EdgeService(engine=engine)
    return _service


def _get_scanner() -> EdgeScanner:
    global _scanner
    if _scanner is None:
        _scanner = EdgeScanner(
            adapters=[
                KalshiAdapter(),
                PolymarketAdapter(),
            ]
        )
    return _scanner


# ---------------------------------------------------------------------------
# Alert formatting
# ---------------------------------------------------------------------------

_SIGNAL_EMOJI = {
    "INJURY_MOMENTUM_REVERSAL": "🔥",
    "PRE_GAME_INJURY_LAG": "🏥",
    "NEWS_LAG": "📰",
    "FAVORITE_LONGSHOT_BIAS": "📈",
    "NONE": "📊",
}

_QUAL_EMOJI = {
    "qualified": "🟢",
    "watchlist": "🟡",
    "rejected": "🔴",
}


def _e(text: str) -> str:
    """Escape text for Telegram HTML mode — safe with any market question content."""
    return html.escape(str(text))


_TG_MAX = 4000  # conservative limit below Telegram's 4096-char hard cap


async def _send_chunked(
    reply_fn,
    text: str,
    parse_mode: str = ParseMode.HTML,
    **kwargs,
) -> None:
    """
    Send a potentially long HTML message as multiple ≤4000-char parts.
    Splits on newline boundaries so HTML tags within a single line are
    never broken mid-tag. Each chunk is sent as a separate message.
    """
    if len(text) <= _TG_MAX:
        await reply_fn(text, parse_mode=parse_mode, **kwargs)
        return

    lines = text.split("\n")
    chunk = ""
    for line in lines:
        candidate = chunk + line + "\n"
        if len(candidate) > _TG_MAX:
            if chunk.strip():
                await reply_fn(chunk.rstrip(), parse_mode=parse_mode, **kwargs)
            chunk = line + "\n"
        else:
            chunk = candidate
    if chunk.strip():
        await reply_fn(chunk.rstrip(), parse_mode=parse_mode, **kwargs)


def _fmt_alert(rec: Recommendation) -> str:
    signal = rec.metadata.get("signal", "NONE")
    sem = _SIGNAL_EMOJI.get(signal, "📊")
    qem = _QUAL_EMOJI.get(rec.qualification_state.value, "")
    question = _e(rec.metadata.get("question") or rec.market_id)

    lines = [
        f"{sem} <b>{_e(signal)}</b>  {qem} {_e(rec.action)}",
        f"<i>{question[:90]}</i>",
        f"Venue: {_e(rec.venue.value)}",
        "",
        f"Market: {rec.market_prob:.1%}  →  Agent: {rec.agent_prob:.1%}",
        f"Edge: <code>{rec.edge:+.1%}</code>  |  EV net: <code>{rec.ev_net:+.2%}</code>",
        f"Confidence: {rec.confidence:.0%}",
    ]
    if rec.thesis:
        lines += ["", f"<i>{_e(rec.thesis[0][:120])}</i>"]
    lines += [
        "",
        f"📍 ID: <code>{_e(rec.market_id)}</code>",
    ]
    return "\n".join(lines)


def _fmt_details(rec: Recommendation) -> str:
    signal = rec.metadata.get("signal", "NONE")
    question = _e(rec.metadata.get("question") or rec.market_id)
    ttr = rec.metadata.get("time_to_resolution_hours", 0)
    ttr_str = f"{ttr:.1f}h" if isinstance(ttr, (int, float)) else "?"
    lines = [
        f"<b>Full Details — {_e(signal)}</b>",
        f"<i>{question[:100]}</i>",
        "",
        f"Market prob:  {rec.market_prob:.3f}",
        f"Agent prob:   {rec.agent_prob:.3f}",
        f"Uncertainty:  [{rec.uncertainty_band[0]:.2f}, {rec.uncertainty_band[1]:.2f}]",
        f"Edge:         {rec.edge:+.3f}",
        f"EV gross:     {rec.ev_gross:+.3f}",
        f"Fees:         {rec.fees:.4f}",
        f"Slippage:     {rec.slippage_cost:.4f}",
        f"EV net:       {rec.ev_net:+.3f}",
        f"Confidence:   {rec.confidence:.3f}",
        f"TTR:          {ttr_str}",
        "",
        "<b>Thesis:</b>",
    ]
    for t in rec.thesis:
        lines.append(f"• {_e(t)}")
    lines += ["", "<b>Disconfirming evidence:</b>"]
    for d in rec.disconfirming_evidence:
        lines.append(f"• {_e(d)}")
    lines += ["", "<b>Invalidation conditions:</b>"]
    for inv in rec.invalidation:
        lines.append(f"• {_e(inv)}")
    return "\n".join(lines)


def _fmt_game(g: TrackedGame) -> str:
    drop = g.current_drop
    flag = "🔥 TRIGGERED" if g.triggered else f"drop {drop:+.1%}"
    reg_type = getattr(g, "registration_type", "pre_game_lag")
    if g.triggered:
        type_icon = "🔥"
    elif reg_type == "pre_game_lag":
        type_icon = "📌"  # pre-game lag watch (market was underpricing)
    else:
        type_icon = "👁"  # proactive injury watch
    type_label = "Pre-game lag" if reg_type == "pre_game_lag" else "Star injury watch"
    return (
        f"{type_icon} <b>[{_e(g.phase.value)}]</b> <i>{type_label}</i>\n"
        f"<code>{_e(g.question[:65])}</code>\n"
        f"  Pre-game: {g.reference_prob:.1%} → Now: {g.last_market_prob:.1%}  ({_e(flag)})"
    )


# ---------------------------------------------------------------------------
# Broadcast helper — sends to the single dev/testing channel
# ---------------------------------------------------------------------------


async def _broadcast(bot, text: str, **kwargs) -> None:
    """Send a message to the configured Telegram dev channel."""
    if not CHAT_ID:
        log.warning("_broadcast: TELEGRAM_CHAT_ID not set")
        return
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, **kwargs)
    except Exception as exc:
        log.warning("_broadcast: send failed — %s", exc)


# ---------------------------------------------------------------------------
# Proactive injury game registration
# ---------------------------------------------------------------------------


def _proactive_injury_registration(game_tracker, inputs: list) -> int:
    """
    Pre-populate the game tracker with tonight's game markets where a star
    player is Out/Doubtful on one side, regardless of whether the market has
    already priced the injury.

    This covers the case PRE_GAME_INJURY_LAG misses:
      - Market correctly priced injury pre-game → no lag signal fires
      - But the injured team can still outperform in Q1/Q2
      - Creating a buy window on the healthy/favored team at improved odds

    Runs BEFORE svc.run_scan() so game_tracker.update() immediately monitors
    these games during the same scan cycle.

    Returns the number of new games registered.
    """
    try:
        from edge_agent.memory.injury_cache import InjuryCache

        icache = InjuryCache()
        n_new = 0

        # Collect teams with significant injuries tonight
        injured_teams: set[str] = set()
        for sport in ("nba", "nfl", "nhl", "cfb", "cbb", "wnba", "ncaaw"):
            for record in icache.get_all(sport):
                if record.get("status", "") in ("Out", "Doubtful"):
                    team = (record.get("team") or "").strip()
                    if team:
                        injured_teams.add(team.lower())

        if not injured_teams:
            return 0

        for item in inputs:
            # inputs from scanner.collect() are tuples: (snapshot, catalysts, theme)
            snapshot = item[0] if isinstance(item, (list, tuple)) else item

            # Skip if already tracked (PRE_GAME_INJURY_LAG may have registered it)
            if game_tracker.get_game(snapshot.venue, snapshot.market_id):
                continue
            # Only care about markets that are live or near-live (TTR 0.5–12h)
            ttr = getattr(snapshot, "time_to_resolution_hours", 0) or 0
            if not (0.5 <= ttr <= 12.0):
                continue
            title = (getattr(snapshot, "question", "") or "").lower()
            if not title:
                continue
            # Match injured team name against market title
            matched = next((t for t in injured_teams if t in title), None)
            if not matched:
                continue

            game_tracker.register(
                snapshot=snapshot,
                catalysts=[f"injury_cache:{matched}"],
                theme="sports",
                registration_type="proactive_injury",
            )
            n_new += 1

        return n_new

    except Exception as exc:
        log.debug("[InjuryTracker] Proactive registration error: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Scan helpers
# ---------------------------------------------------------------------------


async def _run_scan(bot, notify: bool = True) -> str:
    global _last_status, _alerted_keys
    _prune_alerted_keys()  # expire stale dedup entries before processing

    svc = _get_service()
    scanner = _get_scanner()
    loop = asyncio.get_running_loop()

    try:
        # ── Run all blocking I/O in a thread pool so the bot stays responsive ──
        # scanner.collect() hits Kalshi/Polymarket HTTP APIs (can take 10-30s)
        inputs = await loop.run_in_executor(None, scanner.collect)

        # ── Proactive injury game registration ─────────────────────────────────
        # Register ANY game market where a star is Out/Doubtful (per injury cache),
        # even when the market has already correctly priced the injury.
        # This enables Q1/Q2 monitoring for early-game price swings where the
        # injured team outperforms, creating a buy window on the healthy team.
        # (PRE_GAME_INJURY_LAG path still runs alongside — both coexist.)
        n_proactive = await loop.run_in_executor(
            None, _proactive_injury_registration, svc.engine.game_tracker, inputs
        )
        if n_proactive:
            log.info(
                "[InjuryTracker] Proactively registered %d game(s) from injury cache",
                n_proactive,
            )

        # svc.run_scan() processes all markets synchronously
        recs, summary = await loop.run_in_executor(
            None, lambda: svc.run_scan(inputs, portfolio=_portfolio)
        )

        new_alerts = 0
        for rec in recs:
            if rec.qualification_state.value != "qualified":
                continue
            key = f"{rec.venue.value}:{rec.market_id}"
            if key in _alerted_keys:
                continue

            # Filter by approved signals — if user has approved any signals,
            # only alert on those. Empty set = show all (bootstrapping mode).
            signal = rec.metadata.get("signal", "NONE")
            if _approved_signals and signal not in _approved_signals:
                continue

            _alerted_keys[key] = time.time()
            new_alerts += 1

            if notify and bot:
                slot = _store_rec(rec)
                # callback_data max = 64 bytes — use short slot key, not raw market_id
                # Row 1: paper trade picks  |  Row 2: signal management
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "📈 YES", callback_data=f"pt:YES:{slot}"
                            ),
                            InlineKeyboardButton(
                                "📉 NO", callback_data=f"pt:NO:{slot}"
                            ),
                            InlineKeyboardButton("🔄 Fade", callback_data=f"f:{slot}"),
                        ],
                        [
                            InlineKeyboardButton(
                                "✅ Approve", callback_data=f"a:{slot}"
                            ),
                            InlineKeyboardButton("❌ Skip", callback_data=f"s:{slot}"),
                            InlineKeyboardButton(
                                "ℹ️ Details", callback_data=f"d:{slot}"
                            ),
                        ],
                    ]
                )
                await _broadcast(
                    bot,
                    _fmt_alert(rec),
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )

        # Check for GameTracker triggers and notify
        triggered = svc.engine.game_tracker.triggered_games()
        for game in triggered:
            tkey = f"trigger:{game.venue.value}:{game.market_id}"
            if tkey not in _alerted_keys:
                _alerted_keys[tkey] = time.time()
                if notify and bot:
                    # Label the alert based on how this game was originally registered
                    reg_type = getattr(game, "registration_type", "pre_game_lag")
                    if reg_type == "pre_game_lag":
                        reg_label = (
                            "📊 <b>PRE-GAME EDGE</b> — market was underpricing this "
                            "injury before tip-off. Now it's correcting."
                        )
                        signal_tag = "INJURY_MOMENTUM_REVERSAL (lag confirmed)"
                    else:
                        reg_label = (
                            "🏈 <b>INJURY FADE WINDOW</b> — star player is Out on one "
                            "side. The injured team outperformed early — healthy team "
                            "odds have improved beyond fair value."
                        )
                        signal_tag = "INJURY_MOMENTUM_REVERSAL (proactive injury watch)"

                    await _broadcast(
                        bot,
                        (
                            f"🔥 <b>GAME TRACKER TRIGGER FIRED</b>\n"
                            f"<i>{_e(game.question[:80])}</i>\n\n"
                            f"{reg_label}\n\n"
                            f"Phase: <code>{_e(game.phase.value)}</code>\n"
                            f"Pre-game: {game.reference_prob:.1%} → Now: {game.trigger_prob:.1%}\n"
                            f"Drop: {game.reference_prob - game.trigger_prob:.1%}\n\n"
                            f"Signal: <code>{signal_tag}</code>"
                        ),
                        parse_mode=ParseMode.HTML,
                    )

        # Persist alerted keys after processing all alerts this cycle
        if new_alerts > 0:
            _save_alerted_keys()

        tracker_text = svc.game_tracker_summary()

        # Build injury alert block — independent of qualification pipeline
        # (calls BallDontLie HTTP + injury cache, so run off the event loop)
        injury_alert_block = await loop.run_in_executor(
            None, _build_tonight_injury_alerts
        )

        # Fetch sportsbook lines for any sport that has alerts (1 search per sport)
        # Each call hits Tavily/Serper HTTP — run in executor to avoid blocking
        book_lines_block = ""
        if injury_alert_block:
            for _sp in ("nba", "nfl", "nhl"):
                if _sp in injury_alert_block.lower():
                    _lines = await loop.run_in_executor(
                        None, _fetch_sportsbook_lines, _sp
                    )
                    if _lines:
                        book_lines_block += f"\n\n📊 <b>{_sp.upper()} Sportsbook Lines:</b>\n{html.escape(_lines)}"

        # ── Persist scan results to scan_log for /performance ───────────────
        try:
            from edge_agent.memory.scan_log import ScanLog

            _sl = ScanLog()
            _run_id = _sl.log_scan(
                total=summary.total_markets,
                qualified=summary.qualified,
                watchlist=summary.watchlist,
                rejected=summary.rejected,
                new_alerts=new_alerts,
            )
            for _rec in recs:
                if _rec.qualification_state.value == "qualified":
                    _sig_id = _sl.log_signal(
                        scan_run_id=_run_id,
                        market_id=_rec.market_id,
                        venue=_rec.venue.value,
                        signal_type=_rec.metadata.get("signal"),
                        ev_net=_rec.ev_net,
                        confidence=_rec.confidence,
                        action=_rec.action,
                        market_prob=_rec.market_prob,
                    )
                    # Register with outcome tracker so resolution is checked later
                    if _sig_id:
                        import re as _re

                        _side_match = _re.search(
                            r"\b(YES|NO)\b", (_rec.action or "").upper()
                        )
                        _target_side = _side_match.group(1) if _side_match else "YES"
                        _ot.register_signal(
                            signal_id=_sig_id,
                            market_id=_rec.market_id,
                            venue=_rec.venue.value,
                            target_side=_target_side,
                            entry_prob=_rec.market_prob or 0.5,
                            question=getattr(_rec, "question", None) or _rec.market_id,
                        )

                        # ── ML Shadow Mode: log prediction features ────────────────
                        # Runs in shadow mode — prediction is logged but NEVER affects
                        # qualification state or alert delivery in Phase 1.
                        try:
                            _tf_extractor = _TraderFeatureExtractor(_get_trader_cache())
                            _tf = _tf_extractor.get_features(
                                _rec.market_id, signal_direction=_target_side
                            )
                            _raw_conf = getattr(_rec, "raw_confidence", _rec.confidence)
                            _cat_str = getattr(_rec, "catalyst_strength", 0.0)
                            _xgb_prob = None
                            _cal_conf = None
                            if _regime.is_ml_safe:
                                _xgb_prob = _scorer.predict(
                                    {
                                        "raw_confidence": _raw_conf,
                                        "ev_net": _rec.ev_net,
                                        "market_prob": _rec.market_prob or 0.5,
                                        "depth_usd": getattr(_rec, "depth_usd", 0),
                                        "spread_bps": getattr(_rec, "spread_bps", 0),
                                        "ttr_hours": _rec.metadata.get(
                                            "time_to_resolution_hours", 0
                                        ),
                                        "catalyst_strength": _cat_str,
                                        "smart_money_score": _tf.get(
                                            "smart_money_score", 0
                                        ),
                                        "n_hot_longs": _tf.get("n_hot_longs", 0),
                                        "n_hot_shorts": _tf.get("n_hot_shorts", 0),
                                        "signal_type": _rec.metadata.get(
                                            "signal", "UNKNOWN"
                                        ),
                                    }
                                )
                                _cal_conf = (
                                    _calibrator.calibrate(_raw_conf)
                                    if _calibrator._active
                                    else None
                                )

                            _ml_store.log_prediction(
                                signal_id=_sig_id,
                                market_id=_rec.market_id,
                                venue=_rec.venue.value,
                                signal_type=_rec.metadata.get("signal", "UNKNOWN"),
                                raw_confidence=_raw_conf,
                                ev_net=_rec.ev_net,
                                market_prob=_rec.market_prob or 0.5,
                                depth_usd=getattr(_rec, "depth_usd", 0),
                                spread_bps=getattr(_rec, "spread_bps", 0),
                                ttr_hours=_rec.metadata.get(
                                    "time_to_resolution_hours", 0
                                ),
                                catalyst_strength=_cat_str,
                                smart_money_score=_tf.get("smart_money_score", 0),
                                n_hot_longs=_tf.get("n_hot_longs", 0),
                                n_hot_shorts=_tf.get("n_hot_shorts", 0),
                                xgb_win_prob=_xgb_prob,
                                calibrated_conf=_cal_conf,
                            )
                        except Exception as _ml_exc:
                            log.debug("[ML shadow] logging failed: %s", _ml_exc)

        except Exception as _log_exc:
            log.debug("scan_log write failed: %s", _log_exc)

        _last_status = (
            f"Scan @ {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
            f"Markets: {summary.total_markets} | "
            f"Qualified: {summary.qualified} | "
            f"Watchlist: {summary.watchlist} | "
            f"Rejected: {summary.rejected}\n"
            f"New alerts: {new_alerts}\n\n"
            f"{tracker_text}"
            f"{injury_alert_block}"
            f"{book_lines_block}"
        )
        return _last_status

    except Exception as e:
        log.error("Scan error: %s", e, exc_info=True)
        return f"Scan error: {e}"


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    approved_count = len(_approved_signals)
    filter_note = (
        f"🔒 Alerting on {approved_count} approved signal type(s)."
        if approved_count
        else "🔓 Alerting on all qualified signals (approve an alert to filter)."
    )
    await update.message.reply_text(
        "👋 <b>EDGE — Prediction Market Intelligence Agent</b>\n\n"
        "I scan Polymarket and Kalshi for mispriced markets, vet smart money wallets, "
        "track injuries, and help you understand prediction market trading.\n\n"
        "<b>🔍 Market Analysis</b>\n"
        "/scan — run live market scan for edge opportunities\n"
        "/top — top 3 highest-EV opportunities right now\n"
        "/status — last scan summary\n"
        "/performance — signal history, EDGE win rate + your paper P&amp;L\n\n"
        "<b>📊 Paper Trading</b>\n"
        "/mytrades — your open picks + settled history + P&amp;L\n"
        "<i>Tap 📈 YES / 📉 NO on any alert to paper trade it</i>\n\n"
        "<b>👛 Trader Intel</b>\n"
        "/traders — top smart money traders (auto-cached)\n"
        "/traders politics — filter by category (sports/crypto/politics)\n"
        "/wallet 0x… — deep vet any Polymarket wallet address\n\n"
        "<b>🏥 Injury Tracking</b>\n"
        "/injuries — injury cache summary\n"
        "/injuries nba|nfl|nhl|cfb|cbb|wnba|ncaaw — full league injury list\n"
        "/injuries nfl chiefs — filter by team\n"
        "/tracking — injury game tracking list\n\n"
        "<b>📊 Standings &amp; Odds</b>\n"
        "/standings — championship favorites (all sports, Polymarket odds)\n"
        "/standings nba|nfl|mlb|nhl|wnba|cfb|cbb|ncaaw — full table + odds\n"
        "/standings mls|epl|laliga|bundesliga|seriea|ligue1|ucl — soccer tables\n"
        "/standings f1 — F1 driver + constructor standings\n"
        "/standings pga — PGA Tour current leaderboard\n\n"
        "<b>🌤️ Specialist Scanners</b>\n"
        "/weatherscan — weather market gaps vs Open-Meteo 7-day forecast\n"
        "/cryptoscan — crypto market gaps vs Binance lognormal model\n"
        "/fedscan — Fed/econ market gaps vs NY Fed + Treasury yield curve\n\n"
        "<b>🔍 Insider Alerts</b>\n"
        "/insider — recent insider alert log (fresh wallet + large bet signals)\n\n"
        "<b>⚙️ Settings</b>\n"
        "/profile — see what EDGE knows about you\n"
        "/forget &lt;key&gt; — remove stored info (e.g. /forget city)\n"
        "/approvals — manage alert signal filters\n"
        "/help — show this message\n\n"
        f"{filter_note}\n"
        f"⏱ Auto-scan every {SCAN_INTERVAL_MIN // 60}h | "
        "Injury refresh: 9am, 1:30pm, 4:30pm PT | Specialist scan: every 4h\n\n"
        "💬 <b>Chat with me anytime</b> — ask about markets, platform setup, "
        "how to deposit USDC, Kalshi fees, or anything prediction market related.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)


async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show what EDGE knows about the user."""
    user_id = update.effective_user.id
    facts = _profiles.get_facts(user_id)
    if not facts:
        await update.message.reply_text("I don't have any stored info about you yet.")
        return
    lines = ["🧠 <b>What I know about you:</b>\n"]
    # Friendly key labels
    _LABELS = {
        "fav_nba_teams": "❤️ Fav NBA team(s)",
        "fav_nfl_teams": "❤️ Fav NFL team(s)",
        "fav_mlb_teams": "❤️ Fav MLB team(s)",
        "fav_nhl_teams": "❤️ Fav NHL team(s)",
        "fav_cfb_teams": "❤️ Fav CFB team(s)",
        "fav_cbb_teams": "❤️ Fav CBB team(s)",
        "fav_mls_teams": "❤️ Fav MLS team(s)",
        "fav_players": "⭐ Fav player(s)",
        "city": "📍 City",
        "rival_teams": "😤 Rival team(s)",
        "rival_players": "😠 Rival player(s)",
        "sports": "🏀 Sports",
        "interests": "📊 Interests",
        "platforms": "💻 Platforms",
        "risk_style": "📈 Trading style",
        "experience_level": "🎓 Experience",
        "family": "👨‍👩‍👧 Family",
        "market_prefs": "🎯 Market prefs",
        "alert_threshold": "🔔 Alert pref",
        "plays_fantasy": "🏈 Fantasy/DFS",
    }
    for key, values in sorted(facts.items()):
        label = _LABELS.get(key, key)
        val_str = ", ".join(values) if isinstance(values, list) else str(values)
        lines.append(f"  {label}: {val_str}")
    lines.append(
        "\nUse <code>/forget &lt;key&gt;</code> to remove something "
        "(e.g. <code>/forget city</code>)"
    )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Let the user remove a specific stored fact."""
    user_id = update.effective_user.id
    args = (update.message.text or "").split(maxsplit=1)
    if len(args) < 2:
        facts = _profiles.get_facts(user_id)
        if not facts:
            await update.message.reply_text("Nothing stored — nothing to forget!")
            return
        keys = ", ".join(f"<code>{k}</code>" for k in sorted(facts.keys()))
        await update.message.reply_text(
            f"Usage: <code>/forget &lt;key&gt;</code>\n\n"
            f"Available keys: {keys}\n\n"
            f"Example: <code>/forget city</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    fact_key = args[1].strip().lower()
    removed = _profiles.remove_fact(user_id, fact_key)
    if removed:
        await update.message.reply_text(f"✅ Done — forgot your '{fact_key}' info.")
    else:
        await update.message.reply_text(
            f"I don't have any '{fact_key}' stored for you. "
            f"Use /profile to see what I know."
        )


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔍 Running scan...")
    result = await _run_scan(ctx.bot, notify=True)
    if "Scan error" in result:
        await update.message.reply_text(f"⚠️ {result}")
    else:
        await _send_chunked(update.message.reply_text, f"✅ Scan complete.\n\n{result}")


async def cmd_tracking(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _get_service()
    games = svc.engine.game_tracker.active_games()
    if not games:
        await update.message.reply_text(
            "👁 No games currently in the injury tracking list."
        )
        return
    lines = [f"<b>Injury Tracking List</b> — {len(games)} game(s)\n"]
    for g in games:
        lines.append(_fmt_game(g))
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from edge_agent.models import QualificationState

    svc = _get_service()
    top = svc.engine.top_opportunities(limit=3)
    if top:
        for rec in top:
            await update.message.reply_text(_fmt_alert(rec), parse_mode=ParseMode.HTML)
        return

    # No fully qualified markets — fall back to watchlist
    wl_records = svc.engine.repository.list_by_state(QualificationState.WATCHLIST)
    wl = sorted(
        [r.recommendation for r in wl_records],
        key=lambda r: r.ev_net * r.confidence,
        reverse=True,
    )[:3]

    if wl:
        await update.message.reply_text(
            "📋 <b>No fully qualified opportunities.</b>\n"
            "Top watchlist items — close but didn't clear all thresholds "
            "(volume, depth, or EV margin):",
            parse_mode=ParseMode.HTML,
        )
        for rec in wl:
            await update.message.reply_text(_fmt_alert(rec), parse_mode=ParseMode.HTML)
    else:
        if not svc.engine.repository.list_all():
            await update.message.reply_text("No scan data yet — run /scan first.")
        else:
            await update.message.reply_text(
                "No opportunities in last scan.\n"
                "All markets were priced efficiently or below depth/volume thresholds.\n"
                "Try again after more markets open."
            )


async def cmd_traders(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /traders [category]
    Show top 20 Polymarket smart money traders. Reads from pre-warmed cache (instant).
    Falls back to live scoring (~30s) only if cache is empty.
    Category options: OVERALL (default), SPORTS, POLITICS, CRYPTO, CULTURE.
    """
    args = (update.message.text or "").split()
    category = args[1].upper() if len(args) > 1 else "OVERALL"
    valid_cats = {
        "OVERALL",
        "SPORTS",
        "POLITICS",
        "CRYPTO",
        "CULTURE",
        "ECONOMICS",
        "FINANCE",
        "TECH",
    }
    if category not in valid_cats:
        category = "OVERALL"

    _TraderScore = _trader_mod.TraderScore  # already imported via importlib at top

    # ── Cache-first: pre-warmed by daily job, instant response ──────────────
    cache = _get_trader_cache()
    cache_rows = cache.get_top(20)

    if cache_rows:
        # Convert SQLite dicts → TraderScore objects for uniform display
        _fields = _TraderScore.__dataclass_fields__
        scores = [
            _TraderScore(**{k: v for k, v in r.items() if k in _fields})
            for r in cache_rows
        ]
        st = cache.stats()

        # How many extra are in cache but filtered out as bots?
        bot_filtered = max(0, st["count"] - len(scores))
        bot_note = f" · {bot_filtered} bot-filtered" if bot_filtered else ""

        source_note = (
            f"<i>Smart money cache — {len(scores)} legit traders{bot_note} | "
            f"Updated: {st['last_fetch']}</i>"
        )

        # If the legit pool is thin (< 5), kick off a background live rescore
        # so the next /traders call has a richer cache
        if len(scores) < 5:

            async def _background_rescore():
                try:
                    client = _TraderClient()
                    fresh = await asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda: client.get_hot_traders(limit=20, category=category),
                    )
                    log.info(
                        "Background rescore complete — %d traders cached.", len(fresh)
                    )
                except Exception as exc:
                    log.warning("Background rescore failed: %s", exc)

            asyncio.ensure_future(_background_rescore())
            source_note += (
                "\n<i>⚙️ Refreshing cache in background — more traders soon.</i>"
            )
    else:
        # Cache empty — score live (happens on first boot before warmup job runs)
        await update.message.reply_text(
            f"⏳ Cache empty — scoring top Polymarket traders ({category}) live (~30s)…"
        )
        try:
            client = _TraderClient()
            scores = await asyncio.get_running_loop().run_in_executor(
                None, lambda: client.get_hot_traders(limit=20, category=category)
            )
        except Exception as exc:
            await update.message.reply_text(f"❌ Trader scan failed: {exc}")
            return
        if not scores:
            await update.message.reply_text(
                "No trader data available right now. Try again in a few minutes."
            )
            return
        source_note = f"<i>Live scored — {len(scores)} traders</i>"

    lines = [f"<b>🏆 Smart Money — Polymarket {category} (Top {len(scores)})</b>"]
    for i, ts in enumerate(scores, 1):
        name = _e(ts.display_name or ts.wallet_address[:10] + "…")
        badge = " ✅" if ts.verified else ""
        score = int(ts.final_score * 100)

        # Alltime PnL from leaderboard (authoritative)
        pnl_all = (
            f"+${ts.pnl_alltime:,.0f}"
            if ts.pnl_alltime >= 0
            else f"-${abs(ts.pnl_alltime):,.0f}"
        )
        # Alltime volume — format as $Xk or $XM
        vol = ts.volume_alltime
        if vol >= 1_000_000:
            vol_str = f"${vol / 1_000_000:.1f}M"
        elif vol >= 1_000:
            vol_str = f"${vol / 1_000:.0f}k"
        else:
            vol_str = f"${vol:.0f}"

        # Win rate from positions (best available source)
        wr_all = f"{ts.win_rate_alltime:.0%}" if ts.win_rate_alltime > 0 else "—"
        risk = f" ⚠️{ts.unsettled_count} open" if ts.unsettled_count else ""

        if score >= 75:
            verdict = "✅"
        elif score >= 55:
            verdict = "🟡"
        else:
            verdict = "🔴"

        specialty = f"   📌 {_e(ts.top_categories)}\n" if ts.top_categories else ""
        lines.append(
            f"\n{verdict} <b>#{i} {name}</b>{badge}  <code>{score}/100</code>\n"
            f"{specialty}"
            f"   PnL: {pnl_all}  ·  Vol: {vol_str}  ·  WR: {wr_all}{risk}"
        )

    lines.append(f"\n{source_note}")

    # ── Your Watchlist — always shown, regardless of leaderboard rank ────────
    wl_rows = cache.watchlist_list()
    if wl_rows:
        lines.append("\n👀 <b>Your Watchlist</b>")
        for w in wl_rows:
            addr = w.get("wallet_address", "")
            disp = w.get("display_name") or addr[:10] + "…"
            raw_score = w.get("current_score") or w.get("latest_score") or 0
            last_vetted_at = w.get("last_vetted_at") or 0
            # Guard: if score was stored on old 0.0–1.0 scale, upscale it
            if 0 < raw_score <= 1.0:
                raw_score = raw_score * 100
            if last_vetted_at == 0:
                score_str = "<i>pending vet</i>"
            elif raw_score:
                score_str = f"<code>{int(raw_score)}/100</code>"
            else:
                score_str = "<code>0/100</code> <i>(no data)</i>"
            bot_warn = " ⚠️ Bot" if w.get("current_bot_flag") else ""
            note = f" — {_e(w['note'])}" if w.get("note") else ""
            lines.append(f"  • <b>{_e(disp)}</b> {score_str}{bot_warn}{note}")
        lines.append("<i>Run /wallet 0x… to force a fresh vet on any address.</i>")
    else:
        lines.append("<i>Use /wallet 0x… to deep-dive any trader.</i>")

    await _send_chunked(
        update.message.reply_text, "\n".join(lines), parse_mode=ParseMode.HTML
    )


async def cmd_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /wallet {address}
    Full vet of a specific Polymarket wallet address.
    """
    parts = (update.message.text or "").split()
    address = parts[1].strip() if len(parts) > 1 else ""

    if not re.match(r"^0x[0-9a-fA-F]{40}$", address):
        await update.message.reply_text(
            "Usage: /wallet <b>0x…address</b>\n"
            "Provide a valid 42-character Ethereum address.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(f"⏳ Vetting wallet {address[:10]}…{address[-4:]}…")
    try:
        client = _TraderClient()
        ts = await asyncio.get_running_loop().run_in_executor(
            None, lambda: client.score_trader(address)
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Wallet vet failed: {exc}")
        return

    score = int(ts.final_score * 100)
    ab = int(ts.anti_bot_score * 100)
    pf = int(ts.performance_score * 100)
    rl = int(ts.reliability_score * 100)

    if ts.bot_flag:
        verdict = "⚠️ LIKELY BOT"
    elif score >= 75:
        verdict = "✅ STRONG TRADER"
    elif score >= 55:
        verdict = "🟡 LEGIT TRADER"
    else:
        verdict = "🔴 WEAK RECORD"

    rl_tag = " ⚠️" if rl < 70 else ""
    timing = int(ts.timing_score * 100)
    consist = int(ts.consistency_score * 100)
    fade = int(ts.fade_score * 100)
    sizing = int(ts.sizing_discipline * 100)

    timing_label = (
        "Early/contrarian"
        if timing >= 60
        else ("Late to market" if timing < 35 else "Average timing")
    )
    consist_label = (
        "Steady earner"
        if consist >= 60
        else ("One-hit wonder?" if consist < 35 else "Moderate variance")
    )
    fade_label = (
        "Contrarian"
        if fade >= 60
        else ("Follows crowd" if fade < 35 else "Mixed style")
    )
    sizing_label = (
        "Sizes up on edge"
        if sizing >= 60
        else ("Flat/undisciplined" if sizing < 35 else "Moderate")
    )

    lines = [
        f"<b>🔍 Wallet Vet: {_e(ts.wallet_address[:10])}…{_e(ts.wallet_address[-4:])}</b>",
        f"Score: <b>{score}/100</b> — {verdict}",
        f"Anti-bot: {ab}  |  Perf: {pf}  |  Reliability: {rl}{rl_tag}",
        "",
        f"🕐 Timing:      {timing}/100  {timing_label}",
        f"📊 Consistency: {consist}/100  {consist_label}",
        f"🔄 Style:       {fade}/100 contrarian  ({fade_label})",
        f"💰 Sizing:      {sizing}/100  {sizing_label}",
        "",
    ]

    def _fmt_pnl(v: float) -> str:
        return f"+${v:,.0f}" if v >= 0 else f"-${abs(v):,.0f}"

    if ts.trades_alltime:
        adj_note = (
            f" (adj: {_fmt_pnl(ts.pnl_alltime_adj)})"
            if ts.hidden_loss_exposure > 0
            else ""
        )
        lines += [
            f"All-time: {ts.win_rate_alltime:.0%} | {_fmt_pnl(ts.pnl_alltime)}{adj_note}",
            f"30-day:   {ts.win_rate_30d:.0%} | {_fmt_pnl(ts.pnl_30d)}",
            f"7-day:    {ts.win_rate_7d:.0%} | {_fmt_pnl(ts.pnl_7d)}",
            f"Streak:   🔥{ts.current_streak}W now | {ts.max_streak_50}W best (last 50)",
        ]
    else:
        lines.append("Insufficient trade history to score.")

    if ts.top_categories:
        lines += ["", f"📌 Specializes in: {_e(ts.top_categories)}"]

    if ts.hidden_loss_exposure > 0:
        lines += [
            "",
            f"⚠️ Hidden loss exposure: {_fmt_pnl(-ts.hidden_loss_exposure)}",
            f"   {ts.unsettled_count} position(s) in ended markets priced near $0",
            "   Adjusted PnL reflects likely unrealized losses.",
        ]

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_performance(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /performance [days]
    Show scan performance summary: qualified signals, signal breakdown, avg EV.
    Defaults to last 30 days. Use /performance 7 for last 7 days.
    """
    parts = (update.message.text or "").split()
    try:
        days = int(parts[1]) if len(parts) > 1 else 30
        days = max(1, min(days, 365))
    except ValueError:
        days = 30

    try:
        from edge_agent.memory.scan_log import ScanLog

        data = await asyncio.get_running_loop().run_in_executor(
            None, lambda: ScanLog().get_summary(days=days)
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Performance data unavailable: {exc}")
        return

    scans = data["scans"]
    qual = data["total_qualified"]
    watch = data["total_watchlist"]
    alerts = data["total_alerts"]
    avg_q = data["avg_qual_per_scan"]

    if scans == 0:
        await update.message.reply_text(
            f"📊 No scan data yet for the last {days} days.\n"
            "Run /scan to start building a performance history."
        )
        return

    lines = [
        f"<b>📊 EDGE Performance — Last {days} Days</b>\n",
        f"Scans run:         <b>{scans}</b>",
        f"Markets evaluated: <b>{data['total_markets']:,}</b>",
        f"Qualified signals: <b>{qual}</b> (avg {avg_q:.2f}/scan)",
        f"Watchlist entries: <b>{watch}</b>",
        f"Alerts sent:       <b>{alerts}</b>",
    ]

    breakdown = data.get("signal_breakdown", [])
    if breakdown:
        lines.append("\n<b>Signal Breakdown:</b>")
        for sig in breakdown:
            ev_pct = f"{sig['avg_ev'] * 100:+.1f}%"
            lines.append(
                f"  <code>{_e(sig['signal'])}</code>: "
                f"<b>{sig['count']}</b> signals | "
                f"Avg EV: {ev_pct} | "
                f"Avg conf: {sig['avg_conf']:.0%}"
            )

    best = data.get("best_signal")
    if best:
        lines.append(
            f"\n🏆 <b>Best signal found:</b>\n"
            f"  <code>{_e(best['market_id'][:40])}</code> @ {_e(best['venue'])}\n"
            f"  Signal: {_e(best['signal_type'])} | "
            f"EV: <b>{best['ev_net'] * 100:+.1f}%</b> | "
            f"Conf: {best['confidence']:.0%}\n"
            f"  Found: {best['ts_str']}"
        )

    # Smart money cache stats
    try:
        st = _get_trader_cache().stats()
        if st["count"]:
            lines.append(
                f"\n📈 <b>Smart Money Cache:</b> "
                f"{st['count']} traders | "
                f"Avg score: {st['avg_score']:.0f} | "
                f"Updated: {st['last_fetch']}"
            )
    except Exception:
        pass

    # ── EDGE Accuracy (actual resolution outcomes) ────────────────────────
    try:
        acc = _ot.edge_accuracy(days=days)
        settled = acc.get("settled", 0)
        pending = acc.get("pending", 0)
        if settled or pending:
            lines.append("\n<b>🎯 EDGE Accuracy (actual outcomes):</b>")
            if settled:
                wr = acc.get("win_rate")
                wr_str = f"{wr:.0%}" if wr is not None else "n/a"
                lines.append(
                    f"  Win rate: <b>{wr_str}</b> "
                    f"({acc['wins']}W / {acc['losses']}L / {acc.get('voids', 0)} void)"
                )
            if pending:
                lines.append(f"  ⏳ {pending} signals still pending resolution")
    except Exception as _acc_exc:
        log.debug("accuracy block failed: %s", _acc_exc)

    # ── User paper P&L ────────────────────────────────────────────────────
    try:
        user_id = update.effective_user.id
        pnl = _ot.user_pnl(user_id=user_id, days=days)
        if pnl.get("total_picks", 0):
            settled_u = pnl.get("settled", 0)
            pnl_val = pnl.get("total_pnl", 0.0)
            roi = pnl.get("roi")
            wr_u = pnl.get("win_rate")
            lines.append("\n<b>📊 Your Paper Trading:</b>")
            lines.append(
                f"  Picks: {pnl['total_picks']} | "
                f"Settled: {settled_u} | "
                f"Pending: {pnl.get('pending', 0)}"
            )
            if settled_u:
                wr_str = f"{wr_u:.0%}" if wr_u is not None else "n/a"
                roi_str = f"{roi:+.1%}" if roi is not None else "n/a"
                pnl_sign = "+" if pnl_val >= 0 else ""
                lines.append(
                    f"  Win rate: <b>{wr_str}</b> | "
                    f"Paper P&L: <b>{pnl_sign}${pnl_val:.2f}</b> | "
                    f"ROI: {roi_str}"
                )
    except Exception as _pnl_exc:
        log.debug("user pnl block failed: %s", _pnl_exc)

    await _send_chunked(
        update.message.reply_text, "\n".join(lines), parse_mode=ParseMode.HTML
    )


# ── Dev Tracker commands ──────────────────────────────────────────────────


async def outcome_resolution_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Every 2h — check pending signals against Polymarket/Kalshi APIs and resolve."""
    log.info("Outcome resolution job triggered.")
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: _ot.resolve_pending(limit=50))
        log.info(
            "Outcome resolution: %d resolved, %d pending, %d backoff-skipped, "
            "%d unresolvable, %d errors.",
            result["resolved"],
            result["still_pending"],
            result.get("skipped_backoff", 0),
            result.get("unresolvable", 0),
            result["errors"],
        )
        # Periodic 180-day cleanup of old resolved signals
        await loop.run_in_executor(None, lambda: _ot.cleanup(resolved_max_age_days=180))

        # Propagate resolved outcomes → ML store shadow predictions
        try:
            _recent = _ot.recent_resolved(days=3, limit=200)
            for _res in _recent:
                _ml_store.update_prediction_outcome(
                    signal_id=_res.get("signal_id", 0) or 0,
                    outcome=_res.get("outcome", "VOID"),
                )
        except Exception as _ml_prop_exc:
            log.debug("[ML] outcome propagation failed: %s", _ml_prop_exc)

        # Propagate resolved outcomes → insider alert engine (auto-watchlist winners)
        try:
            _recent_all = _ot.recent_resolved(days=3, limit=200)
            engine = _get_insider_engine()
            tc = _get_trader_cache()
            for _res in _recent_all:
                cid = _res.get("condition_id") or _res.get("market_id") or ""
                outcome = _res.get("outcome", "VOID")
                if not cid or outcome == "VOID":
                    continue
                resolved_yes = outcome == "WIN"
                winning_wallets = await loop.run_in_executor(
                    None, lambda c=cid, r=resolved_yes: engine.record_outcome(c, r)
                )
                # Auto-add confirmed insider wallets (bet paid off) to watchlist
                for addr in winning_wallets:
                    try:
                        await loop.run_in_executor(
                            None,
                            lambda a=addr: tc.watchlist_add(
                                a,
                                added_by="insider_engine",
                                note="Auto-added: insider alert confirmed (bet resolved YES)",
                                vet_interval_sec=21600,  # 6h
                            ),
                        )
                        log.info(
                            "[insider] Auto-watchlisted confirmed wallet: %s", addr[:10]
                        )
                    except Exception as _wl_exc:
                        log.debug(
                            "[insider] Watchlist add failed for %s: %s",
                            addr[:10],
                            _wl_exc,
                        )
        except Exception as _ins_exc:
            log.debug(
                "[insider] outcome propagation to insider engine failed: %s", _ins_exc
            )

    except Exception as exc:
        log.warning("Outcome resolution job failed: %s", exc)


async def ml_calibration_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Weekly — retrain confidence calibrator and XGBoost shadow scorer on
    the latest labeled outcome data.  Runs drift check after training.
    Safe no-op if insufficient data (< 150 labeled signals).
    """
    log.info("[ml_calibration_job] Starting ML calibration refresh.")
    try:
        loop = asyncio.get_running_loop()

        def _run_calibration():
            labeled = _ml_store.get_labeled_features(min_samples=0, days=180)
            n = len(labeled)
            log.info("[ml_calibration_job] Found %d labeled signals.", n)

            # 1. Retrain confidence calibrator
            cal_ok = _calibrator.train(labeled)
            if cal_ok:
                _nodes_mod.set_calibrator(_calibrator)
                log.info("[ml_calibration_job] Confidence calibrator updated.")

            # 2. Retrain XGBoost scorer (Phase 2 threshold: 400 samples)
            score_ok = _scorer.train(labeled)
            if score_ok:
                log.info(
                    "[ml_calibration_job] XGBoost scorer updated (phase=%d).",
                    _scorer._phase,
                )

            # 3. Update regime detector baseline if training succeeded
            if cal_ok or score_ok:
                _regime.set_baseline(labeled)

            # 4. Run drift check on recent 14-day window
            recent = _ml_store.get_labeled_features(min_samples=0, days=14)
            drifted = _regime.check(recent)
            if drifted:
                log.warning(
                    "[ml_calibration_job] Regime drift detected — ML overlay disabled."
                )

            # 5. Cleanup old ML store rows
            _ml_store.cleanup(max_age_days=180)

            return {
                "n_labeled": n,
                "cal_ok": cal_ok,
                "score_ok": score_ok,
                "drifted": drifted,
            }

        result = await loop.run_in_executor(None, _run_calibration)
        log.info(
            "[ml_calibration_job] Complete — n=%d cal=%s xgb=%s drift=%s",
            result["n_labeled"],
            result["cal_ok"],
            result["score_ok"],
            result["drifted"],
        )
    except Exception as exc:
        log.warning("[ml_calibration_job] Failed: %s", exc)


async def maintenance_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Weekly Sunday 3am PT — vacuum all SQLite DBs, archive old scan logs,
    and purge stale .cache/ JSON files.
    Keeps DB files compact and prevents unbounded disk growth.
    """
    import glob as _glob
    import os as _os
    from pathlib import Path as _Path

    log.info("[maintenance_job] Weekly maintenance starting.")

    # ── 1. VACUUM all SQLite databases ────────────────────────────────────
    db_dir = _Path(__file__).parent / "edge_agent" / "memory" / "data"
    vacuumed = []
    for db_file in sorted(db_dir.glob("*.db")):
        try:
            import sqlite3 as _sqlite3

            conn = _sqlite3.connect(str(db_file))
            conn.execute("VACUUM")
            conn.close()
            vacuumed.append(db_file.name)
        except Exception as exc:
            log.warning("[maintenance_job] VACUUM failed for %s: %s", db_file.name, exc)
    log.info("[maintenance_job] VACUUMed: %s", ", ".join(vacuumed))

    # ── 2. Archive old scan log entries ───────────────────────────────────
    try:
        from edge_agent.memory.scan_log import ScanLog as _ScanLog

        sl = _ScanLog()
        result = sl.cleanup(max_age_days=90)
        log.info(
            "[maintenance_job] scan_log: %d runs + %d signals archived.",
            result["runs_deleted"],
            result["signals_deleted"],
        )
    except Exception as exc:
        log.warning("[maintenance_job] scan_log cleanup failed: %s", exc)

    # ── 3. Purge stale .cache/ JSON files (>48h old) ──────────────────────
    import time as _time

    cache_dir = _Path(__file__).parent / ".cache"
    cutoff = _time.time() - (48 * 3600)  # 48 hours
    removed = 0
    errors = 0
    if cache_dir.exists():
        for fpath in cache_dir.glob("*.json"):
            try:
                if fpath.stat().st_mtime < cutoff:
                    fpath.unlink()
                    removed += 1
            except Exception:
                errors += 1
    log.info(
        "[maintenance_job] .cache/ cleanup: %d stale files removed (%d errors).",
        removed,
        errors,
    )

    # ── 4. Purge old decision log entries (>30 days) ──────────────────────
    try:
        deleted = _decision_log.cleanup(retain_days=30)
        log.info("[maintenance_job] decision_log: %d old entries purged.", deleted)
    except Exception as exc:
        log.warning("[maintenance_job] decision_log cleanup failed: %s", exc)

    # ── 5. Prune in-memory alerted_keys (already time-keyed, just force prune) ─
    _prune_alerted_keys()

    log.info("[maintenance_job] Weekly maintenance complete.")


async def trader_refresh_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily 8am PT — warm the trader cache with full top-100 leaderboard scores."""
    log.info("Trader refresh triggered.")
    try:
        loop = asyncio.get_running_loop()
        client = _TraderClient()
        scores = await loop.run_in_executor(
            None, lambda: client.get_hot_traders(limit=100, category="OVERALL")
        )
        log.info("Trader refresh complete — %d traders scored.", len(scores))
    except Exception as exc:
        log.warning("Trader refresh failed: %s", exc)


async def discovery_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Hourly — sweep 4 leaderboard categories (OVERALL/SPORTS/CRYPTO/POLITICS,
    ~400 wallets), fast-score each with zero per-wallet API calls, populate
    discovery_pool.  Then graduate the top candidates (fast_score >= 40) to
    Tier-2 full vet (max 15/cycle).
    """
    log.info("[discovery_job] Starting multi-category sweep.")
    try:
        loop = asyncio.get_running_loop()
        client = _TraderClient()
        summary = await loop.run_in_executor(
            None,
            lambda: client.discovery_sweep(per_category=100, fast_score_threshold=30.0),
        )
        log.info(
            "[discovery_job] Sweep complete — %d unique wallets discovered: %s",
            summary.get("total_unique", 0),
            {k: v for k, v in summary.items() if k != "total_unique"},
        )

        # Graduate top candidates to Tier-2 full vet (up to 15 per cycle)
        cache = _get_trader_cache()
        queue = cache.pool_get_vet_queue(
            limit=15, min_fast_score=40.0, exclude_done=True
        )
        if queue:
            log.info("[discovery_job] Tier-2 vetting %d top candidates.", len(queue))
            scored = 0
            for entry in queue:
                try:
                    addr = entry["wallet_address"]
                    await loop.run_in_executor(
                        None,
                        lambda a=addr, e=entry: client.score_trader(
                            a,
                            {
                                "pnl": e.get("pnl_alltime", 0),
                                "vol": e.get("volume_alltime", 0),
                                "userName": e.get("display_name", ""),
                            },
                        ),
                    )
                    scored += 1
                except Exception as exc:
                    log.debug(
                        "[discovery_job] Vet failed for %s: %s",
                        entry.get("wallet_address", "?")[:10],
                        exc,
                    )
            log.info(
                "[discovery_job] Tier-2 complete — %d/%d vetted.", scored, len(queue)
            )
        else:
            log.info(
                "[discovery_job] No new candidates met Tier-2 threshold this cycle."
            )

    except Exception as exc:
        log.warning("[discovery_job] Failed: %s", exc)


async def watchlist_vet_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Every 6h — re-vet watchlist wallets whose interval has elapsed.
    Sends a Telegram alert when score changes ≥ 10 pts or bot flag appears.
    """
    log.info("[watchlist_vet_job] Checking due wallets.")
    try:
        cache = _get_trader_cache()
        due = cache.watchlist_due_for_vet()
        if not due:
            log.info("[watchlist_vet_job] No wallets due for re-vet.")
            return

        log.info("[watchlist_vet_job] %d wallet(s) due for re-vet.", len(due))
        loop = asyncio.get_running_loop()
        client = _TraderClient()
        alerts: list[str] = []

        for entry in due:
            addr = entry["wallet_address"]
            old_score = float(entry.get("latest_score") or 0)
            old_bot = int(entry.get("latest_bot_flag") or 0)
            name = entry.get("display_name") or addr[:10] + "…"
            try:
                ts = await loop.run_in_executor(
                    None, lambda a=addr: client.score_trader(a)
                )
                cache.watchlist_mark_vetted(
                    addr, score=ts.final_score * 100, bot_flag=ts.bot_flag
                )
                new_score = round(ts.final_score * 100, 1)
                delta = new_score - old_score

                if ts.bot_flag and not old_bot:
                    alerts.append(
                        f"🚨 <b>Watched wallet flagged as bot:</b>\n"
                        f"  {_e(name)} (<code>{_e(addr[:14])}…</code>)\n"
                        f"  Score: {old_score:.0f} → <b>{new_score:.0f}</b>"
                    )
                elif abs(delta) >= 10:
                    arrow = "📈" if delta > 0 else "📉"
                    alerts.append(
                        f"{arrow} <b>Watchlist score change:</b>\n"
                        f"  {_e(name)} (<code>{_e(addr[:14])}…</code>)\n"
                        f"  {old_score:.0f} → <b>{new_score:.0f}</b> ({delta:+.0f} pts)"
                    )
                log.info("[watchlist_vet_job] %s → %.1f/100", addr[:12], new_score)

            except Exception as exc:
                log.debug("[watchlist_vet_job] Vet failed for %s: %s", addr[:10], exc)

        if alerts and CHAT_ID:
            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text="👀 <b>WATCHLIST UPDATE</b>\n\n" + "\n\n".join(alerts),
                parse_mode=ParseMode.HTML,
            )

    except Exception as exc:
        log.warning("[watchlist_vet_job] Failed: %s", exc)


async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /watch {address} [note]
    Add a Polymarket wallet to the watchlist for automatic re-vetting every 6h.
    """
    args = ctx.args or []
    if not args:
        await update.message.reply_text(
            "Usage: <code>/watch 0xADDRESS [optional note]</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    addr = args[0].strip().lower()
    note = " ".join(args[1:]) if len(args) > 1 else ""

    if not addr.startswith("0x") or len(addr) < 10:
        await update.message.reply_text(
            "⚠️ That doesn't look like a valid wallet address."
        )
        return

    cache = _get_trader_cache()

    # Check if already watched
    existing = cache.watchlist_get(addr)
    if existing:
        await update.message.reply_text(
            f"👀 Already watching <code>{_e(addr[:14])}…</code>\n"
            f"Last vetted: {_fmt_ts(existing.get('last_vetted_at', 0))}",
            parse_mode=ParseMode.HTML,
        )
        return

    cache.watchlist_add(
        address=addr,
        display_name="",
        added_by=str(update.effective_user.id),
        note=note,
    )

    await update.message.reply_text(
        f"✅ Added <code>{_e(addr[:14])}…</code> to watchlist.\n"
        f"Full vet will run within 6h. Use /watchlist to see all watched wallets.",
        parse_mode=ParseMode.HTML,
    )

    # Kick off an immediate background vet so first score appears quickly
    loop = asyncio.get_running_loop()
    client = _TraderClient()
    try:
        ts = await loop.run_in_executor(None, lambda: client.score_trader(addr))
        cache.watchlist_mark_vetted(
            addr, score=ts.final_score * 100, bot_flag=ts.bot_flag
        )
        score_str = f"{ts.final_score * 100:.0f}/100"
        bot_str = " 🚨 <b>BOT FLAGGED</b>" if ts.bot_flag else ""
        await update.message.reply_text(
            f"⚡ Quick vet done: <b>{score_str}</b>{bot_str}\n"
            f"PnL: <b>${ts.pnl_alltime:,.0f}</b> | "
            f"Win rate: <b>{ts.win_rate_alltime:.0%}</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        log.debug("cmd_watch immediate vet failed: %s", exc)


async def cmd_unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /unwatch {address}
    Remove a wallet from the watchlist.
    """
    args = ctx.args or []
    if not args:
        await update.message.reply_text(
            "Usage: <code>/unwatch 0xADDRESS</code>", parse_mode=ParseMode.HTML
        )
        return

    addr = args[0].strip().lower()
    removed = _get_trader_cache().watchlist_remove(addr)

    if removed:
        await update.message.reply_text(
            f"🗑️ Removed <code>{_e(addr[:14])}…</code> from watchlist.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"❌ <code>{_e(addr[:14])}…</code> wasn't in your watchlist.",
            parse_mode=ParseMode.HTML,
        )


async def cmd_watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /watchlist
    Show all watched wallets with their latest scores and last-vet time.
    """
    cache = _get_trader_cache()
    entries = cache.watchlist_list()

    if not entries:
        await update.message.reply_text(
            "📋 Watchlist is empty.\n\nUse <code>/watch 0xADDRESS</code> to add a wallet.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [
        f"👀 <b>WATCHLIST</b> ({len(entries)} wallet{'s' if len(entries) != 1 else ''})\n"
    ]

    for e in entries:
        addr = e.get("wallet_address", "")
        name = e.get("display_name") or addr[:10] + "…"
        score = float(e.get("current_score") or e.get("latest_score") or 0)
        bot_flag = int(e.get("current_bot_flag") or e.get("latest_bot_flag") or 0)
        last_vet = _fmt_ts(e.get("last_vetted_at", 0))
        note = e.get("note", "")
        pnl = float(e.get("tp_pnl") or 0)
        wr = float(e.get("tp_win_rate") or 0)

        if bot_flag:
            badge = "🚨 BOT"
        elif score >= 70:
            badge = "✅ STRONG"
        elif score >= 50:
            badge = "🟡 LEGIT"
        else:
            badge = "🔴 WEAK"

        pnl_str = f" | PnL: ${pnl:,.0f}" if pnl else ""
        wr_str = f" | WR: {wr:.0%}" if wr else ""
        note_str = f"\n    📝 {_e(note)}" if note else ""

        lines.append(
            f"<b>{_e(name)}</b> {badge}\n"
            f"  Score: <b>{score:.0f}/100</b>{pnl_str}{wr_str}\n"
            f"  <code>{_e(addr[:14])}…</code> | Last vet: {last_vet}"
            f"{note_str}"
        )

    pool_stats = cache.pool_stats()
    lines.append(
        f"\n<i>Discovery pool: {pool_stats['total']} wallets "
        f"({pool_stats['vetted']} fully vetted) | "
        f"Avg fast score: {pool_stats['avg_fast_score']:.0f}</i>"
    )

    await _send_chunked(
        update.message.reply_text, "\n\n".join(lines), parse_mode=ParseMode.HTML
    )


def _fmt_ts(ts: float | None) -> str:
    """Format a unix timestamp as a human-readable relative time or UTC clock."""
    if not ts:
        return "never"
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        ago = time.time() - float(ts)
        if ago < 3600:
            return f"{int(ago // 60)}m ago"
        if ago < 86400:
            return f"{int(ago // 3600)}h ago"
        return dt.strftime("%b %d")
    except Exception:
        return "unknown"


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_chunked(update.message.reply_text, _last_status)


async def cmd_mytrades(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /mytrades — show the user's paper trade picks (open + recent settled).
    """
    user_id = update.effective_user.id
    picks = _ot.get_user_picks(user_id=user_id, limit=30)

    if not picks:
        await update.message.reply_text(
            "📋 <b>No paper trades yet.</b>\n\n"
            "When EDGE fires a signal alert, tap <b>📈 YES</b> or <b>📉 NO</b> "
            "to paper trade it. Your picks and P&amp;L will appear here.",
            parse_mode=ParseMode.HTML,
        )
        return

    open_picks = [p for p in picks if p["pick_outcome"] == "PENDING"]
    settled_picks = [p for p in picks if p["pick_outcome"] != "PENDING"]

    lines: list[str] = ["<b>📊 My Paper Trades</b>"]

    # ── Open positions ────────────────────────────────────────────────────────
    if open_picks:
        lines.append(f"\n<b>🟡 Open ({len(open_picks)})</b>")
        for p in open_picks:
            side_em = "📈" if p["side"] == "YES" else "📉"
            prob = p["entry_prob"] or 0.5
            # Potential payout if this side wins
            payout = round(p["paper_stake"] * (1 / max(prob, 0.01) - 1), 2)
            venue = (p["venue"] or "").upper()
            venue_tag = f"[{venue[:4]}]" if venue else ""

            # Market title — truncate to keep it readable
            q = p["question"] or p["market_id"] or "Unknown market"
            q_short = (q[:55] + "…") if len(q) > 55 else q

            lines.append(
                f"{side_em} <b>{p['side']}</b>  @{prob:.0%}  "
                f"·  win +${payout:.2f} / lose -${p['paper_stake']:.0f}\n"
                f"   <i>{_e(q_short)}</i>  <code>{venue_tag}</code>"
            )
    else:
        lines.append("\n<i>No open picks right now.</i>")

    # ── Settled history ───────────────────────────────────────────────────────
    if settled_picks:
        total_pnl = sum(p["paper_pnl"] or 0 for p in settled_picks)
        wins = sum(1 for p in settled_picks if p["pick_outcome"] == "WIN")
        losses = sum(1 for p in settled_picks if p["pick_outcome"] == "LOSS")
        voids = sum(1 for p in settled_picks if p["pick_outcome"] == "VOID")
        settled_ct = wins + losses
        wr_str = f"{wins / settled_ct:.0%}" if settled_ct else "n/a"
        pnl_sign = "+" if total_pnl >= 0 else ""
        pnl_em = "🟢" if total_pnl >= 0 else "🔴"

        lines.append(
            f"\n<b>📁 Settled ({len(settled_picks)})</b>  "
            f"{pnl_em} <b>{pnl_sign}${total_pnl:.2f}</b>  ·  "
            f"Win rate: <b>{wr_str}</b>  ({wins}W / {losses}L"
            + (f" / {voids} void" if voids else "")
            + ")"
        )

        # Show last 5 settled picks detail
        for p in settled_picks[:5]:
            outcome_em = {"WIN": "✅", "LOSS": "❌", "VOID": "⬜"}.get(
                p["pick_outcome"], "⬜"
            )
            pnl_val = p["paper_pnl"] or 0
            pnl_str = f"+${pnl_val:.2f}" if pnl_val >= 0 else f"-${abs(pnl_val):.2f}"
            q = p["question"] or p["market_id"] or "Unknown market"
            q_short = (q[:50] + "…") if len(q) > 50 else q
            lines.append(
                f"{outcome_em} {p['side']}  <b>{pnl_str}</b>  <i>{_e(q_short)}</i>"
            )
        if len(settled_picks) > 5:
            lines.append(
                f"<i>… and {len(settled_picks) - 5} more. See /performance for full stats.</i>"
            )

    lines.append("\n<i>Run /performance for full win rate + ROI breakdown.</i>")
    await _send_chunked(
        update.message.reply_text, "\n".join(lines), parse_mode=ParseMode.HTML
    )


async def cmd_approvals(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show (and optionally clear) the approved signal filter list."""
    arg = (update.message.text or "").strip().lower()

    if "clear" in arg:
        _approved_signals.clear()
        _save_approved_signals(_approved_signals)
        await update.message.reply_text(
            "🔓 Approval filter cleared — bot will alert on <b>all</b> qualified signals again.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not _approved_signals:
        await update.message.reply_text(
            "📋 <b>No approved signals yet.</b>\n\n"
            "The bot is in <i>alert-all</i> mode.\n"
            "When you click <b>✅ Approve</b> on an alert, its signal type is added here "
            "and future alerts will only fire for those types.\n\n"
            "Send <code>/approvals clear</code> to reset back to alert-all.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [f"<b>Approved signal types</b> ({len(_approved_signals)}):\n"]
    for sig in sorted(_approved_signals):
        emoji = _SIGNAL_EMOJI.get(sig, "📊")
        lines.append(f"{emoji} <code>{_e(sig)}</code>")
    lines.append("\nOnly markets matching these signals will trigger alerts.")
    lines.append("Send <code>/approvals clear</code> to reset to alert-all mode.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Inline keyboard callbacks (Approve / Skip / Details)
# ---------------------------------------------------------------------------


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("a:"):
        slot = data[2:]
        rec = _rec_store.get(slot)
        label = rec.market_id if rec else slot

        # Save the approved signal type so future scans filter to these only
        sig_added = ""
        if rec:
            sig = rec.metadata.get("signal", "NONE")
            if sig and sig != "NONE" and sig not in _approved_signals:
                _approved_signals.add(sig)
                _save_approved_signals(_approved_signals)
                sig_added = (
                    f"\n📌 Signal type <code>{_e(sig)}</code> added to approved list."
                )

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"✅ <b>Approved:</b> <code>{_e(label)}</code>\n"
            f"<i>Proposal recorded. No live trade placed — this is a proposal-only system.</i>"
            f"{sig_added}",
            parse_mode=ParseMode.HTML,
        )

    elif data.startswith("s:"):
        slot = data[2:]
        rec = _rec_store.get(slot)
        label = rec.market_id if rec else slot
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"❌ Skipped: <code>{_e(label)}</code>", parse_mode=ParseMode.HTML
        )

    elif data.startswith("d:"):
        slot = data[2:]
        rec = _rec_store.get(slot)
        if rec:
            await query.message.reply_text(
                _fmt_details(rec),
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.message.reply_text(
                f"⚠️ Details expired (slot <code>{_e(slot)}</code> no longer cached).",
                parse_mode=ParseMode.HTML,
            )

    elif data.startswith("f:"):
        # Fade — paper trade the OPPOSITE of the bot's recommendation
        slot = data[2:]
        rec = _rec_store.get(slot)
        if not rec:
            await query.answer("⚠️ Signal expired — can't record fade.", show_alert=True)
            return
        bot_side = "YES" if "YES" in rec.action.upper() else "NO"
        fade_side = "NO" if bot_side == "YES" else "YES"
        # Re-use the pt: handler logic by rewriting data and falling through
        data = f"pt:{fade_side}:{slot}"
        fade_label = f"🔄 Fading bot's {bot_side} → your pick: {fade_side}"
        # Inline answer before falling through so user sees the fade label
        # Store fade tag in side field so /mytrades can show it distinctly
        user_id = update.effective_user.id
        try:
            sig_row = _ot._conn.execute(
                "SELECT signal_id, entry_prob FROM signal_outcomes WHERE market_id = ? ORDER BY created_at DESC LIMIT 1",
                (rec.market_id,),
            ).fetchone()
            if not sig_row:
                await query.answer(
                    "⚠️ Signal not registered yet — try again in a moment.",
                    show_alert=True,
                )
                return
            signal_id = sig_row["signal_id"]
            entry_prob = sig_row["entry_prob"]
            recorded = _ot.record_user_pick(
                signal_id=signal_id,
                market_id=rec.market_id,
                user_id=user_id,
                side=f"FADE_{fade_side}",  # tagged as fade in DB
            )
            if not recorded:
                await query.answer("You already picked this one.", show_alert=True)
                return
            prob = entry_prob or rec.market_prob or 0.5
            f_prob = (1 - prob) if fade_side == "YES" else prob
            payout = round(10 * (1 / max(f_prob, 0.01) - 1), 2)
            await query.answer(
                f"{fade_label}\nPaper $10 @ {f_prob:.0%} — Win = +${payout:.2f} | Loss = -$10.00\n"
                "EDGE will track resolution automatically.",
                show_alert=True,
            )
        except Exception as exc:
            log.warning("Fade pick failed: %s", exc)
            await query.answer("⚠️ Could not save fade — try again.", show_alert=True)
        return

    elif data.startswith("pt:"):
        # Paper trade pick — "pt:YES:{slot}" or "pt:NO:{slot}"
        parts = data.split(":", 2)
        if len(parts) != 3:
            return
        _, side, slot = parts
        rec = _rec_store.get(slot)
        user_id = update.effective_user.id

        if not rec:
            await query.answer("⚠️ Signal expired — can't record pick.", show_alert=True)
            return

        # Find the signal_id from outcome tracker by market_id
        # We store it keyed by market_id; outcome tracker has it registered
        try:
            # Look up signal_id — outcome tracker stores market_id → signal_id
            sig_row = _ot._conn.execute(
                "SELECT signal_id, entry_prob FROM signal_outcomes WHERE market_id = ? ORDER BY created_at DESC LIMIT 1",
                (rec.market_id,),
            ).fetchone()

            if not sig_row:
                await query.answer(
                    "⚠️ Signal not registered yet — try again in a moment.",
                    show_alert=True,
                )
                return

            signal_id = sig_row["signal_id"]
            entry_prob = sig_row["entry_prob"]

            recorded = _ot.record_user_pick(
                signal_id=signal_id,
                market_id=rec.market_id,
                user_id=user_id,
                side=side,
            )

            if not recorded:
                await query.answer("You already picked this one.", show_alert=True)
                return

            # Show confirmation with implied payout
            prob = entry_prob or rec.market_prob or 0.5
            payout = (
                round(10 * (1 / max(prob, 0.01) - 1), 2)
                if side.upper() == "YES"
                else round(10 * (1 / max(1 - prob, 0.01) - 1), 2)
            )
            side_emoji = "📈" if side.upper() == "YES" else "📉"
            await query.answer(
                f"{side_emoji} Picked {side.upper()} — paper $10 @ {prob:.0%}\n"
                f"Win = +${payout:.2f} | Loss = -$10.00\n"
                "EDGE will track resolution automatically.",
                show_alert=True,
            )
        except Exception as exc:
            log.warning("Paper trade pick failed: %s", exc)
            await query.answer("⚠️ Could not save pick — try again.", show_alert=True)


# ---------------------------------------------------------------------------
# Injury context builder for free-form chat
# ---------------------------------------------------------------------------


async def _maybe_refresh_injury_cache(sport: str) -> None:
    """
    On-demand freshness gate. If the cache for *sport* is empty or older than
    2 hours, fires a synchronous fetch_and_store() in a background thread so
    the very next _build_injury_context() call returns real data instead of
    nothing.  Rate-limited to once per 30 min per sport.
    """
    import time as _t

    now = _t.time()
    if now - _ONDEMAND_REFRESH_COOLDOWN.get(sport, 0) < 1800:
        return  # refreshed recently
    try:
        from edge_agent.memory.injury_cache import InjuryCache

        records = InjuryCache().get_all(sport)
        if records:
            newest = max(r.get("fetched_at", 0) for r in records)
            if (now - newest) / 3600 < 2.0:
                return  # fresh enough
        _ONDEMAND_REFRESH_COOLDOWN[sport] = now
        log.info("On-demand cache refresh triggered for %s", sport.upper())
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _InjuryClient().fetch_and_store, sport)
        log.info("On-demand cache refresh complete for %s", sport.upper())
    except Exception as exc:
        log.debug("On-demand cache refresh failed for %s: %s", sport, exc)


def _build_injury_context(query: str) -> str:
    """
    Check whether the user's message mentions a sport, team, or player.
    If it does, pull the relevant rows from the verified injury cache and
    return a formatted context block for injection into the AI prompt.

    This grounds the AI's answer in real-time data (ESPN + Sleeper + news)
    instead of stale training knowledge.

    Returns "" if no sports content is detected or the cache is empty.
    """
    try:
        from edge_agent.memory.injury_cache import InjuryCache

        _injury_detect = importlib.import_module(
            ".dat-ingestion.injury_api", "edge_agent"
        )
        detect_sport = _injury_detect.detect_sport
        _star_keys = set(_injury_detect._STAR_MULTIPLIERS.keys())

        # ── Detect sport ──────────────────────────────────────────────────────
        # Quick bail-out: if none of the sport-indicator words appear, skip.
        _SPORT_TRIGGERS = {
            "nba": {
                "nba",
                "basketball",
                "lakers",
                "celtics",
                "warriors",
                "bucks",
                "heat",
                "nets",
                "knicks",
                "nuggets",
                "suns",
                "sixers",
                "raptors",
                "mavericks",
                "mavs",
                "spurs",
                "thunder",
                "grizzlies",
                "pelicans",
            },
            "nfl": {
                "nfl",
                "football",
                "chiefs",
                "eagles",
                "cowboys",
                "ravens",
                "bills",
                "bengals",
                "dolphins",
                "steelers",
                "49ers",
                "rams",
                "seahawks",
                "patriots",
                "packers",
                "bears",
                "giants",
                "saints",
                "buccaneers",
                "chargers",
                "raiders",
                "broncos",
                "texans",
            },
            "nhl": {
                "nhl",
                "hockey",
                "oilers",
                "bruins",
                "rangers",
                "leafs",
                "canadiens",
                "penguins",
                "capitals",
                "lightning",
                "golden knights",
                "kraken",
                "avalanche",
                "flames",
                "canucks",
                "senators",
                "sabres",
            },
        }
        q = query.lower()
        matched_sport = None
        for sport, triggers in _SPORT_TRIGGERS.items():
            if any(t in q for t in triggers):
                matched_sport = sport
                break

        # Also check for player name mentions (covers "is LeBron playing?")
        player_mentioned = next((k for k in _star_keys if k in q), None)
        if player_mentioned and not matched_sport:
            matched_sport = detect_sport(q)  # let keyword scorer decide

        if not matched_sport:
            return ""

        # ── Pull from cache ───────────────────────────────────────────────────
        cache = InjuryCache()
        all_records = cache.get_all(matched_sport)
        if not all_records:
            return f"\n[Injury cache for {matched_sport.upper()} is empty — refresh pending]"

        # If a specific player was mentioned, show just that player + team.
        # Otherwise try to match a team from the query, then fall back to top-10.
        if player_mentioned:
            relevant = [
                r
                for r in all_records
                if player_mentioned in r.get("player_name", "").lower()
            ]
        else:
            # Try substring team match
            relevant = [
                r
                for r in all_records
                if any(w in q for w in r.get("team", "").lower().split())
            ]

        # Fallback: show the most-severe players (top 10) for the detected sport
        if not relevant:
            relevant = all_records[:10]

        # ── Format — split starters vs role players ───────────────────────────
        _SEV_TAG = {
            "Out": "OUT",
            "Injured Reserve": "OUT(IR)",
            "Suspension": "SUSP",
            "Doubtful": "DOUBTFUL",
            "Questionable": "QUEST",
            "Day-To-Day": "DTD",
        }
        src_note = {
            "nba_official": "(official)",
            "+sleeper✓": "(confirmed)",
            "⚠️": "(⚠️ conflicting)",
        }

        def _fmt_row(r: dict) -> str:
            status = r.get("status", "")
            tag = _SEV_TAG.get(status, status)
            player = r.get("player_name", "")
            team = r.get("team", "")
            pos = r.get("position", "")
            inj_type = r.get("injury_type", "")
            src = r.get("source_api", "espn")
            src_tag = next((v for k, v in src_note.items() if k in src), "")
            detail = f" [{inj_type}]" if inj_type else ""
            pos_s = f" ({pos})" if pos else ""
            return f"  {tag}: {player}{pos_s} — {team}{detail}{src_tag}"

        starters = [r for r in relevant if r.get("is_starter")]
        role_players = [r for r in relevant if not r.get("is_starter")]

        lines = [f"\n[Live {matched_sport.upper()} injury data from verified cache]"]
        if starters:
            lines.append("⭐ STARTERS:")
            for r in starters[:10]:
                lines.append(_fmt_row(r))
        if role_players:
            lines.append("ROLE PLAYERS:")
            for r in role_players[:5]:  # condensed — less critical
                lines.append(_fmt_row(r))

        import time as _t

        newest_ts = max((r.get("fetched_at", 0) for r in relevant), default=0)
        if newest_ts:
            age_min = int((_t.time() - newest_ts) / 60)
            age_str = (
                f"{age_min}m ago"
                if age_min < 60
                else f"{age_min // 60}h {age_min % 60}m ago"
            )
        else:
            age_str = "unknown"
        sources = "ESPN"
        if matched_sport == "nba":
            sources += " + NBA official PDF"
        if matched_sport in ("nba", "nfl"):
            sources += " + Sleeper cross-ref"
        lines.append(
            f"[Source: {sources}. Last updated {age_str}. "
            f"Use /injuries {matched_sport} to force-refresh.]"
        )
        return "\n".join(lines)

    except Exception as exc:
        log.debug("Could not build injury context for chat: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Smart Money context builder
# ---------------------------------------------------------------------------


def _derive_strategy_tag(tw: dict) -> str:
    """
    Derive a human-readable strategy label from stored wallet signals.
    Called in smart money context builder — no extra DB query needed.
    """
    top_cats = tw.get("top_categories", "")
    timing = float(tw.get("timing_score", 0.0) or 0.0)
    fade = float(tw.get("fade_score", 0.0) or 0.0)
    # Rough avg-entry proxy from timing_score: timing = 1 - (avg - 0.10)/0.70
    avg_entry = 0.10 + (1.0 - timing) * 0.70 if timing > 0 else 0.5

    # Specialist: top category contains a single sport/domain name
    if top_cats:
        first_cat = top_cats.split(",")[0].strip()
        if first_cat:
            return f"{first_cat} Specialist"

    if fade > 0.55:
        return "Contrarian"
    if timing > 0.65 and avg_entry < 0.40:
        return "Value Hunter"
    if timing > 0.60 and fade < 0.35:
        return "Momentum"
    return "Generalist"


def _build_smart_money_context(
    force_refresh: bool = False, sport_filter: str = ""
) -> str:
    """
    Return a compact [Smart Money] context block showing what top-scored
    watchlist wallets are currently betting on.

    sport_filter — if set (e.g. "NBA"), surface specialist wallets for that
    sport first; non-specialists still appear but after specialists.

    Uses a 30-minute in-memory cache so the AI gets fresh data without
    adding API latency to every message.  Returns "" if no data available.
    """
    global _sm_positions_cache

    now = time.time()
    if not force_refresh and (now - _sm_positions_cache["fetched_at"]) < _SM_CACHE_TTL:
        lines = _sm_positions_cache["lines"]
        if lines:
            age_min = int((now - _sm_positions_cache["fetched_at"]) / 60)
            return (
                f"\n[Smart Money — top tracked wallets, refreshed {age_min}m ago]\n"
                + "\n".join(lines)
            )
        return ""

    # ── Refresh: pull top-scored non-bot wallets from cache, fetch positions ──
    try:
        cache = _get_trader_cache()
        # Top 8 non-bot wallets by final_score — sport specialists may be ranked lower
        top = cache.get_top(limit=8)
        client = _TraderClient()
        new_lines: list[str] = []
        new_pos_keys: set[str] = set()
        new_alertable: list[dict] = []

        prev_keys = _sm_positions_cache.get("position_keys", set())

        # Sort: specialists for the requested sport first, then by score
        if sport_filter:
            sf = sport_filter.upper()
            top = sorted(
                top,
                key=lambda w: (
                    0 if sf in (w.get("top_categories") or "").upper() else 1,
                    -float(w.get("final_score", 0) or 0),
                ),
            )

        shown = 0
        for tw in top:
            if shown >= 5:
                break
            addr = tw.get("wallet_address", "")
            score = int(float(tw.get("final_score", 0) or 0) * 100)
            streak = int(tw.get("current_streak", 0) or 0)
            strat = _derive_strategy_tag(tw)
            pnl_all = float(tw.get("pnl_alltime", 0) or 0)
            win_rate = float(tw.get("win_rate_alltime", 0) or 0)
            if not addr:
                continue
            try:
                positions = client.fetch_wallet_positions(addr)
            except Exception:
                continue

            # Filter: significant open positions ($100+ size), active markets only
            sig = []
            _now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            for p in positions:
                if float(p.get("size", p.get("currentValue", 0)) or 0) < 100:
                    continue
                # Skip expired markets (endDate in the past)
                _end = p.get("endDate", p.get("end_date_iso", ""))
                if _end and _end[:10] < _now_iso:
                    continue
                # Skip spread/prop/total markets — only keep moneyline-equivalent
                _ptitle = (p.get("title") or p.get("market", "")).lower()
                if any(
                    kw in _ptitle
                    for kw in (
                        "spread",
                        "o/u",
                        "over/under",
                        "total",
                        "points",
                        "(+",
                        "(-",
                        "rebounds",
                        "assists",
                        "1h",
                        "2h",
                        "quarter",
                    )
                ):
                    continue
                sig.append(p)
            if not sig:
                continue

            # Streak badge: show only meaningful streaks (|streak| >= 3)
            streak_badge = ""
            if streak >= 5:
                streak_badge = f" 🔥{streak}W"
            elif streak >= 3:
                streak_badge = f" +{streak}W streak"
            elif streak <= -3:
                streak_badge = f" -{abs(streak)}L skid"

            wallet_header = f"  [{score}/100]{streak_badge} {addr[:8]}... | {strat}"
            new_lines.append(wallet_header)

            for pos in sig[:3]:  # max 3 positions per wallet
                title = (pos.get("title") or pos.get("market", "Unknown market"))[:55]
                side = "YES" if pos.get("outcomeIndex", 0) == 0 else "NO"
                size = float(pos.get("size", pos.get("currentValue", 0)) or 0)
                cond_id = pos.get("conditionId", pos.get("market", title[:20]))

                # Fetch real current price from CLOB API instead of defaulting to 0.5
                token_id = pos.get("asset") or pos.get("tokenId", "")
                cur_pct = 0.5  # fallback
                if token_id:
                    try:
                        cur_pct = client._fetch_token_price(token_id)
                    except Exception:
                        pass

                pos_key = f"{addr}:{cond_id}:{side}"
                new_pos_keys.add(pos_key)

                # Detect new positions (not seen in previous cycle) for alert candidates
                if pos_key not in prev_keys:
                    new_alertable.append(
                        {
                            "addr": addr,
                            "score": score,
                            "streak": streak,
                            "strat": strat,
                            "pnl_all": pnl_all,
                            "win_rate": win_rate,
                            "title": title,
                            "side": side,
                            "size": size,
                            "cur_pct": cur_pct,
                            "cond_id": cond_id,
                        }
                    )

                new_lines.append(
                    f"    → {side} on '{title}' ${size:,.0f} @ {cur_pct:.0%}"
                )
            shown += 1

        _sm_positions_cache["lines"] = new_lines
        _sm_positions_cache["fetched_at"] = now
        _sm_positions_cache["position_keys"] = new_pos_keys
        _sm_positions_cache["alertable"] = new_alertable

        if new_lines:
            return (
                "\n[Smart Money — top tracked wallets, just refreshed]\n"
                + "\n".join(new_lines)
            )
    except Exception as exc:
        log.debug("[SmartMoney] Position refresh failed: %s", exc)

    return ""


# ---------------------------------------------------------------------------
# Copy-trade alert quality filter + channel notifier
# ---------------------------------------------------------------------------

_SM_ALERT_MIN_SCORE = 40  # wallets below this score are too low-quality
_SM_ALERT_MIN_SIZE = 200  # positions below $200 are noise / test trades
_SM_ALERT_PRICE_LOW = 0.15  # below 15% → near-certain NO, no useful entry window
_SM_ALERT_PRICE_HIGH = (
    0.75  # above 75% → mostly played out, follower gets little upside
)
_SM_ALERT_DCA_WINDOW = 86400  # 24 hours — suppress follow-on buys into same market


def _copy_trade_quality_check(pos: dict) -> tuple[bool, str]:
    """
    Return (passes, reason_if_rejected) for a candidate copy-trade position.

    Filters:
      1. Wallet score gate (too low quality)
      2. Entry window — price too high (mostly played out) or too low (near-certain)
      3. Position size gate (test trade / noise)
      4. DCA suppression — same wallet already alerted on this market in last 24h
      5. Stale price check — if cur_pct is exactly 0.5, price fetch likely failed
    """
    score = pos["score"]
    cur_pct = pos["cur_pct"]
    size = pos["size"]
    key_24h = f"{pos['addr']}:{pos['cond_id']}"

    if score < _SM_ALERT_MIN_SCORE:
        return False, f"score {score}/100 below minimum {_SM_ALERT_MIN_SCORE}"

    if size < _SM_ALERT_MIN_SIZE:
        return False, f"position ${size:.0f} below minimum ${_SM_ALERT_MIN_SIZE}"

    # If price is exactly 0.5, the CLOB fetch likely failed — don't alert with bad data
    if cur_pct == 0.5:
        return False, "price exactly 50% — likely stale/unfetched, skipping"

    if cur_pct < _SM_ALERT_PRICE_LOW:
        return False, f"price {cur_pct:.0%} — near-certain NO, no entry window"

    if cur_pct > _SM_ALERT_PRICE_HIGH:
        return False, f"price {cur_pct:.0%} — market mostly resolved, poor upside"

    last_alerted = _sm_alerted_24h.get(key_24h, 0.0)
    if time.time() - last_alerted < _SM_ALERT_DCA_WINDOW:
        return False, "DCA follow-on — same market alerted in last 24h"

    return True, "ok"


async def _send_copy_trade_alerts(bot) -> int:
    """
    Read new alertable positions from _sm_positions_cache, apply quality filters,
    and send passing alerts to ALERT_CHANNEL_ID.

    Called by the async smart money refresh job after _build_smart_money_context().
    Returns the number of alerts sent.
    """
    if not ALERT_CHANNEL_ID:
        return 0

    candidates = _sm_positions_cache.get("alertable", [])
    if not candidates:
        return 0

    # First-run guard: if prev_keys was empty, this is a cold start — don't
    # alert everything in the cache at once (they aren't new, we just don't know).
    # We detect cold start by checking if prev_keys was empty before the refresh.
    # The cache now has the populated keys, so we check if alertable count equals
    # total positions (which means prev_keys was empty = cold start).
    total_pos = len(_sm_positions_cache.get("position_keys", set()))
    if len(candidates) == total_pos and total_pos > 0:
        log.info(
            "[CopyAlert] Cold start — skipping %d positions (no previous baseline)",
            total_pos,
        )
        return 0

    sent = 0
    for pos in candidates:
        ok, reason = _copy_trade_quality_check(pos)
        if not ok:
            log.debug("[CopyAlert] Filtered out '%s': %s", pos["title"][:40], reason)
            continue

        score = pos["score"]
        streak = pos["streak"]
        strat = pos["strat"]
        addr = pos["addr"]
        title = pos["title"]
        side = pos["side"]
        size = pos["size"]
        cur_pct = pos["cur_pct"]
        pnl_all = pos["pnl_all"]
        wr = pos["win_rate"]

        # Derive position-specific category from the market title
        _tl = title.lower()
        _pos_cat = strat  # default to wallet-level strategy tag
        _POSITION_CATS = [
            (
                [
                    "nba",
                    "lakers",
                    "celtics",
                    "warriors",
                    "bucks",
                    "nets",
                    "knicks",
                    "nuggets",
                    "suns",
                    "76ers",
                    "heat",
                    "mavericks",
                    "thunder",
                    "grizzlies",
                    "clippers",
                    "rockets",
                    "kings",
                    "bulls",
                    "hawks",
                    "cavaliers",
                    "pacers",
                    "pistons",
                    "hornets",
                    "magic",
                    "jazz",
                    "timberwolves",
                    "spurs",
                    "pelicans",
                    "trail blazers",
                    "wizards",
                    "raptors",
                ],
                "NBA",
            ),
            (
                [
                    "nfl",
                    "chiefs",
                    "eagles",
                    "cowboys",
                    "ravens",
                    "bills",
                    "bengals",
                    "dolphins",
                    "steelers",
                    "49ers",
                    "rams",
                    "seahawks",
                    "packers",
                    "lions",
                    "bears",
                    "vikings",
                    "giants",
                    "saints",
                    "buccaneers",
                    "chargers",
                    "raiders",
                    "broncos",
                    "texans",
                    "colts",
                    "titans",
                    "jaguars",
                    "browns",
                    "falcons",
                    "panthers",
                    "cardinals",
                    "commanders",
                ],
                "NFL",
            ),
            (
                [
                    "nhl",
                    "bruins",
                    "rangers",
                    "oilers",
                    "flames",
                    "canucks",
                    "maple leafs",
                    "canadiens",
                    "lightning",
                    "penguins",
                    "capitals",
                    "avalanche",
                    "golden knights",
                    "red wings",
                    "blackhawks",
                    "blues",
                    "flyers",
                ],
                "NHL",
            ),
            (
                [
                    "fc ",
                    "united",
                    "city",
                    "arsenal",
                    "chelsea",
                    "liverpool",
                    "barcelona",
                    "madrid",
                    "bayern",
                    "juventus",
                    "psg",
                    "paris saint",
                    "inter milan",
                    "ac milan",
                    "dortmund",
                    "atletico",
                    "tottenham",
                    "man utd",
                    "man city",
                    "la liga",
                    "premier league",
                    "serie a",
                    "bundesliga",
                    "ligue 1",
                    "champions league",
                    "europa",
                    "soccer",
                    "football",
                ],
                "Soccer",
            ),
            (
                [
                    "bitcoin",
                    "btc",
                    "ethereum",
                    "eth",
                    "crypto",
                    "solana",
                    "token",
                    "coin",
                    "defi",
                ],
                "Crypto",
            ),
            (
                [
                    "election",
                    "president",
                    "trump",
                    "biden",
                    "senate",
                    "congress",
                    "governor",
                    "vote",
                    "ballot",
                    "democrat",
                    "republican",
                    "by-election",
                    "parliament",
                ],
                "Politics",
            ),
        ]
        for _keywords, _cat_label in _POSITION_CATS:
            if any(kw in _tl for kw in _keywords):
                _pos_cat = _cat_label
                break

        # Upside/downside remaining
        upside_pp = (
            round((1.0 - cur_pct) * 100, 1)
            if side == "YES"
            else round(cur_pct * 100, 1)
        )
        downside_pp = (
            round(cur_pct * 100, 1)
            if side == "YES"
            else round((1.0 - cur_pct) * 100, 1)
        )

        streak_line = ""
        if streak >= 5:
            streak_line = f"\n🔥 On a <b>{streak}-game win streak</b>"
        elif streak >= 3:
            streak_line = f"\n📈 +{streak}W streak"
        elif streak <= -3:
            streak_line = f"\n⚠️ On a {abs(streak)}-game losing skid"

        pnl_str = f"+${pnl_all:,.0f}" if pnl_all >= 0 else f"-${abs(pnl_all):,.0f}"
        confidence = (
            "High conviction"
            if score >= 60
            else "Moderate signal"
            if score >= 40
            else "Weak signal"
        )

        text = (
            f"🔔 <b>COPY TRADE SIGNAL</b>\n\n"
            f"<b>{_e(title)}</b>\n"
            f"→ <b>{side}</b> @ <b>{cur_pct:.0%}</b> | ${size:,.0f} position\n"
            f"📊 Upside: +{upside_pp}pp | Risk: -{downside_pp}pp\n\n"
            f"<b>Wallet:</b> {addr[:10]}... [{score}/100] — {_e(_pos_cat)}"
            f"{streak_line}\n"
            f"All-time: {wr:.0%} win rate | {pnl_str} PnL\n"
            f"<i>{confidence} — entry window still open</i>\n\n"
            f"<i>Search this market on Polymarket or Kalshi to follow.</i>"
        )

        try:
            await bot.send_message(
                chat_id=ALERT_CHANNEL_ID,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            # Mark this wallet+market as alerted so DCA follow-ons are suppressed
            _sm_alerted_24h[f"{addr}:{pos['cond_id']}"] = time.time()
            sent += 1
            log.info(
                "[CopyAlert] Sent: %s %s on '%s' @ %.0f%% (score=%d)",
                addr[:10],
                side,
                title[:40],
                cur_pct * 100,
                score,
            )
        except Exception as exc:
            log.warning("[CopyAlert] Failed to send alert: %s", exc)

    # Prune _sm_alerted_24h — remove entries older than 24h to prevent unbounded growth
    cutoff = time.time() - _SM_ALERT_DCA_WINDOW
    expired = [k for k, ts in _sm_alerted_24h.items() if ts < cutoff]
    for k in expired:
        del _sm_alerted_24h[k]

    return sent


# ---------------------------------------------------------------------------
# Game Assessment Context Builder
# ---------------------------------------------------------------------------

_GAME_ASSESSMENT_KEYWORDS = {
    "analysis",
    "assess",
    "break down",
    "preview",
    "prediction",
    "prediction",
    "who wins",
    "win chance",
    "win probability",
    "odds",
    "outlook",
    "outlook",
    "how will",
    "should i bet",
    "bet on",
    "recap",
    "previous game",
    "last game",
    "h2h",
    "head to head",
    "matchup",
    "versus",
    "vs ",
    "standings",
    "record",
}


def _build_game_assessment_context(teams: list[str], sport: str, query: str) -> str:
    """
    Build a comprehensive game assessment context for a matchup.
    Includes: standings/records, head-to-head, coaching, all-stars, recent form.
    Returns "" if no game assessment keywords detected.
    """
    if len(teams) < 2:
        return ""

    if not any(kw in query.lower() for kw in _GAME_ASSESSMENT_KEYWORDS):
        return ""

    from datetime import date

    today = date.today().strftime("%B %d, %Y")

    team1, team2 = teams[0], teams[1]

    assessment_lines = [f"\n[Game Assessment — {team1} vs {team2}]", f"Today: {today}"]

    assessment_lines.append(f"\nWeb search results for matchup analysis:")

    search_query = f"{team1} vs {team2} {sport.upper()} game preview analysis standings record {today}"
    try:
        import requests as _req

        resp = _req.get(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": os.getenv("SERPER_API_KEY", "").strip()},
            json={"q": search_query, "num": 5},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if ans := data.get("answerBox", {}).get("answer"):
                assessment_lines.append(f"Summary: {ans}")
            for r in data.get("organic", [])[:5]:
                title = r.get("title", "").strip()
                snippet = r.get("snippet", "").strip()
                if len(snippet) > 200:
                    snippet = snippet[:200].rsplit(" ", 1)[0] + "..."
                assessment_lines.append(f"• {title}: {snippet}")
    except Exception:
        pass

    h2h_query = f"{team1} vs {team2} {sport.upper()} head to head results this season"
    try:
        resp = _req.get(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": os.getenv("SERPER_API_KEY", "").strip()},
            json={"q": h2h_query, "num": 3},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("organic"):
                assessment_lines.append(f"\nHead-to-head / this season results:")
                for r in data.get("organic", [])[:3]:
                    title = r.get("title", "").strip()
                    snippet = r.get("snippet", "").strip()
                    if len(snippet) > 200:
                        snippet = snippet[:200].rsplit(" ", 1)[0] + "..."
                    assessment_lines.append(f"• {title}: {snippet}")
    except Exception:
        pass

    return "\n".join(assessment_lines)


def _build_todays_games_context(user_msg: str) -> str:
    """
    Return a context block listing today's games when user asks about them.
    Supports NBA (BallDontLie) and NHL (web search).
    Returns "" if no 'today's games' intent detected.
    """
    _TODAYS_GAMES_TRIGGERS = {
        "games today",
        "games tonight",
        "todays games",
        "tonights games",
        "what games",
        "schedule today",
        "schedule tonight",
        "games on today",
        "games on tonight",
        "whats playing",
        "whats on today",
    }
    q = user_msg.lower().strip()

    if not any(t in q for t in _TODAYS_GAMES_TRIGGERS):
        return ""

    from datetime import date, timedelta

    today = date.today().strftime("%B %d, %Y")
    lines = [f"\n[Today's Games — {today}]"]

    nba_games = _get_tonight_nba_games()
    if nba_games:
        lines.append(f"\n🏀 NBA Tonight/Tomorrow: {', '.join(sorted(nba_games)[:20])}")

    sport = (
        "NHL"
        if any(
            t in q for t in {"hockey", "nhl", "ducks", "flyers", "rangers", "islanders"}
        )
        else None
    )
    if sport is None:
        if any(t in q for t in {"nba", "basketball", "lakers", "celtics", "warriors"}):
            sport = "NBA"
        elif any(t in q for t in {"mlb", "baseball", "yankees", "dodgers", "mets"}):
            sport = "MLB"

    if sport:
        search_query = (
            f"{sport} games schedule today {date.today().strftime('%B %d %Y')}"
        )
        try:
            resp = _req.get(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": os.getenv("SERPER_API_KEY", "").strip()},
                json={"q": search_query, "num": 8},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if ans := data.get("answerBox", {}).get("answer"):
                    lines.append(f"\n{ans}")
                for r in data.get("organic", [])[:6]:
                    title = r.get("title", "").strip()
                    snippet = r.get("snippet", "").strip()
                    if len(snippet) > 200:
                        snippet = snippet[:200].rsplit(" ", 1)[0] + "..."
                    if sport.lower() in (title + snippet).lower():
                        lines.append(f"• {title}: {snippet}")
        except Exception:
            pass

    if len(lines) == 1:
        return ""

    lines.append("\n[End today's games]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Free-form AI chat
# ---------------------------------------------------------------------------


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    first_name = update.effective_user.first_name or None
    username = update.effective_user.username or None
    user_msg = update.message.text

    if not user_msg:
        return

    await update.message.chat.send_action("typing")

    # ── Per-user session + profile ────────────────────────────────────────────
    _mem_user = _get_session(user_id)

    # Passively extract personal facts from every message and store long-term
    try:
        _profiles.ingest_message(
            user_id=user_id,
            message=user_msg,
            first_name=first_name,
            username=username,
        )
    except Exception:
        pass  # never let profile writes crash the bot

    # Long-term profile context (what EDGE knows about this person across all sessions)
    profile_context = _profiles.get_profile_context(user_id)

    # New-user onboarding hint — tells AI to naturally ask missing profile questions
    onboarding_hint = _profiles.get_onboarding_prompt(user_id)

    # ── Correction detection ──────────────────────────────────────────────────
    _CORRECTION_TRIGGERS = {
        # explicit corrections
        "wrong",
        "you're wrong",
        "youre wrong",
        "that's wrong",
        "thats wrong",
        "that was wrong",
        "bad answer",
        "wrong answer",
        "incorrect",
        "not right",
        "still wrong",
        "no that",
        # stale/outdated data
        "old data",
        "stale",
        "outdated",
        "your data is",
        "data is wrong",
        "data is old",
        "data is outdated",
        "data is off",
        "data is stale",
        "not current",
        "not live",
        "not accurate",
        "not up to date",
        # retry requests
        "try again",
        "retry",
        "search again",
        "look again",
        "check again",
        "check current",
        "check polymarket",
        "check the price",
        "pull the price",
        "get the price",
        "get current",
        "get live",
        "pull live",
        "pull current",
        "different answer",
        "try harder",
        "redo",
        # direct challenges
        "that's not",
        "thats not",
        "you said",
        "actually",
        "no the price",
        "the price is",
        "it's actually",
        "its actually",
        "are you sure",
        "are you certain",
        "double check",
        "double-check",
        "verify that",
    }
    _is_correction = any(t in user_msg.lower() for t in _CORRECTION_TRIGGERS)

    # ── Memory correction — auto-remove wrong facts ───────────────────────
    # Detect "I don't live in X" or "I'm not from X" and remove the wrong city.
    # Also detect generic "remove my city/team" requests.
    _city_deny = re.search(
        r"(?:i )?(?:don'?t|do not) live in\s+([\w\s]{2,25})",
        user_msg,
        re.IGNORECASE,
    )
    if _city_deny:
        _denied_city = _city_deny.group(1).strip().title()
        try:
            if _profiles.remove_fact(user_id, "city", _denied_city):
                log.info(
                    "[profile] Auto-removed city '%s' for user %s",
                    _denied_city,
                    user_id,
                )
        except Exception:
            pass

    _not_from = re.search(
        r"i(?:'m| am) not (?:from|in|based in)\s+([\w\s]{2,25})",
        user_msg,
        re.IGNORECASE,
    )
    if _not_from:
        _denied_city = _not_from.group(1).strip().title()
        try:
            if _profiles.remove_fact(user_id, "city", _denied_city):
                log.info(
                    "[profile] Auto-removed city '%s' for user %s",
                    _denied_city,
                    user_id,
                )
        except Exception:
            pass

    # Generic "forget my city/location/team" via chat (alternative to /forget command)
    _forget_chat = re.search(
        r"(?:remove|delete|forget|clear)\s+(?:my\s+)?(?:city|location)",
        user_msg,
        re.IGNORECASE,
    )
    if _forget_chat:
        try:
            if _profiles.remove_fact(user_id, "city"):
                log.info(
                    "[profile] Cleared all city data for user %s via chat", user_id
                )
        except Exception:
            pass

    # "that's not my city", "wrong city", "incorrect location"
    _wrong_city = re.search(
        r"(?:wrong|not my|incorrect|that'?s not)\s+(?:my\s+)?(?:city|location|hometown)",
        user_msg,
        re.IGNORECASE,
    )
    if _wrong_city:
        try:
            if _profiles.remove_fact(user_id, "city"):
                log.info(
                    "[profile] Removed city for user %s (wrong city correction)",
                    user_id,
                )
        except Exception:
            pass

    # "remove X from my profile/memory" — try to match a stored fact value
    _forget_profile = re.search(
        r"(?:remove|delete|clear|erase)\s+(.{1,40}?)\s+(?:from|in)\s+(?:my\s+)?(?:profile|memory|data)",
        user_msg,
        re.IGNORECASE,
    )
    if _forget_profile:
        _target = _forget_profile.group(1).strip().lower()
        try:
            facts = _profiles.get_facts(user_id)
            for key, values in facts.items():
                if isinstance(values, list):
                    for v in values:
                        if v.lower() in _target or _target in v.lower():
                            _profiles.remove_fact(user_id, key, v)
                            log.info(
                                "[profile] Removed %s='%s' for user %s via chat",
                                key,
                                v,
                                user_id,
                            )
                            break
        except Exception:
            pass

    # 1. Knowledge base context
    kb_context = _kb.get_context_for_question(user_msg)

    # 1b. Platform docs context — injected for onboarding/setup questions
    platform_doc_context = _get_platform_doc_context(user_msg)

    # 2. Session memory context (today's conversation, per user)
    session_context = _mem_user.get_session_context(max_exchanges=4)

    # 2b. Detect short affirmative replies ("yes", "yeah", "sure") and inject
    #     continuation context so the AI follows up on its own question.
    _SHORT_AFFIRM = {
        "yes",
        "yeah",
        "yep",
        "yea",
        "sure",
        "ok",
        "okay",
        "definitely",
        "absolutely",
        "do it",
        "go ahead",
        "please",
    }
    if user_msg.strip().lower().rstrip("!.") in _SHORT_AFFIRM:
        _last_q = _mem_user.get_last_bot_question()
        if _last_q:
            user_msg = (
                f"(User replied '{user_msg}' to your previous message: "
                f'"{_last_q[:300]}")\n'
                f"Follow up on YOUR question — do not change topics."
            )

    # 3. Live market context (on-demand, only for market questions)
    # ── Team name → canonical search tokens (Polymarket uses full city names) ──
    _TEAM_ALIASES: dict[str, str] = {
        # NBA
        "warriors": "Warriors",
        "golden state": "Warriors",
        "timberwolves": "Timberwolves",
        "wolves": "Timberwolves",
        "lakers": "Lakers",
        "celtics": "Celtics",
        "bucks": "Bucks",
        "heat": "Heat",
        "nets": "Nets",
        "knicks": "Knicks",
        "nuggets": "Nuggets",
        "suns": "Suns",
        "sixers": "76ers",
        "raptors": "Raptors",
        "mavericks": "Mavericks",
        "mavs": "Mavericks",
        "spurs": "Spurs",
        "thunder": "Thunder",
        "grizzlies": "Grizzlies",
        "pelicans": "Pelicans",
        "kings": "Kings",
        "bulls": "Bulls",
        "rockets": "Rockets",
        "jazz": "Jazz",
        "clippers": "Clippers",
        "pistons": "Pistons",
        "hornets": "Hornets",
        "magic": "Magic",
        "hawks": "Hawks",
        "pacers": "Pacers",
        "cavaliers": "Cavaliers",
        "cavs": "Cavaliers",
        "wizards": "Wizards",
        "blazers": "Trail Blazers",
        "trailblazers": "Trail Blazers",
        "trail blazers": "Trail Blazers",
        "portland": "Trail Blazers",
        "okc": "Thunder",
        "brooklyn": "Nets",
        "minnesota": "Timberwolves",
        # NFL
        "chiefs": "Chiefs",
        "eagles": "Eagles",
        "cowboys": "Cowboys",
        "ravens": "Ravens",
        "bills": "Bills",
        "bengals": "Bengals",
        "dolphins": "Dolphins",
        "steelers": "Steelers",
        "49ers": "49ers",
        "niners": "49ers",
        "rams": "Rams",
        "seahawks": "Seahawks",
        "packers": "Packers",
        "lions": "Lions",
        "bears": "Bears",
        "vikings": "Vikings",
        "giants": "Giants",
        "commanders": "Commanders",
        "saints": "Saints",
        "falcons": "Falcons",
        "panthers": "Panthers",
        "buccaneers": "Buccaneers",
        "bucs": "Buccaneers",
        "texans": "Texans",
        "colts": "Colts",
        "jaguars": "Jaguars",
        "titans": "Titans",
        "broncos": "Broncos",
        "raiders": "Raiders",
        "chargers": "Chargers",
        # NHL
        "bruins": "Bruins",
        "maple leafs": "Maple Leafs",
        "leafs": "Maple Leafs",
        "canadiens": "Canadiens",
        "habs": "Canadiens",
        "lightning": "Lightning",
        "florida panthers": "Panthers",
        "capitals": "Capitals",
        "rangers": "Rangers",
        "flyers": "Flyers",
        "ducks": "Ducks",
        "penguins": "Penguins",
        "red wings": "Red Wings",
        "blackhawks": "Blackhawks",
        "blues": "Blues",
        "avalanche": "Avalanche",
        "golden knights": "Golden Knights",
        "oilers": "Oilers",
        "flames": "Flames",
        "canucks": "Canucks",
        # MLB
        "yankees": "Yankees",
        "red sox": "Red Sox",
        "dodgers": "Dodgers",
        "cubs": "Cubs",
        "cardinals": "Cardinals",
        "braves": "Braves",
        "mets": "Mets",
        "astros": "Astros",
        "phillies": "Phillies",
        "padres": "Padres",
        "sf giants": "Giants",
        "mariners": "Mariners",
        "blue jays": "Blue Jays",
        "rays": "Rays",
        "orioles": "Orioles",
    }

    def _find_team_mentions(text: str) -> list[str]:
        """Return up to 2 canonical team names found in the message."""
        found, seen = [], set()
        tl = text.lower()
        # Sort by length descending so "red sox" matches before "sox"
        for alias, canonical in sorted(_TEAM_ALIASES.items(), key=lambda x: -len(x[0])):
            if alias in tl and canonical not in seen:
                found.append(canonical)
                seen.add(canonical)
            if len(found) == 2:
                break
        return found

    # Team name → Polymarket slug abbreviation
    _SLUG_ABBR: dict[str, str] = {
        # NBA
        "Warriors": "gsw",
        "Timberwolves": "min",
        "Lakers": "lal",
        "Celtics": "bos",
        "Bucks": "mil",
        "Heat": "mia",
        "Nets": "bkn",
        "Knicks": "nyk",
        "Nuggets": "den",
        "Suns": "phx",
        "76ers": "phi",
        "Raptors": "tor",
        "Mavericks": "dal",
        "Spurs": "sas",
        "Thunder": "okc",
        "Grizzlies": "mem",
        "Pelicans": "nop",
        "Kings": "sac",
        "Bulls": "chi",
        "Rockets": "hou",
        "Jazz": "uta",
        "Clippers": "lac",
        "Pistons": "det",
        "Hornets": "cha",
        "Magic": "orl",
        "Hawks": "atl",
        "Pacers": "ind",
        "Cavaliers": "cle",
        "Wizards": "was",
        "Trail Blazers": "por",
        # NHL
        "Bruins": "bos",
        "Maple Leafs": "tor",
        "Canadiens": "mtl",
        "Lightning": "tbl",
        "Capitals": "wsh",
        "Rangers": "nyr",
        "Flyers": "phi",
        "Ducks": "ana",
        "Penguins": "pit",
        "Red Wings": "det",
        "Blackhawks": "chi",
        "Blues": "stl",
        "Avalanche": "col",
        "Golden Knights": "vgk",
        "Oilers": "edm",
        "Flames": "cgy",
        "Canucks": "van",
        # NFL (off-season — slug lookup still useful for futures)
        "Chiefs": "kc",
        "Eagles": "phi",
        "Cowboys": "dal",
        "Ravens": "bal",
        "Bills": "buf",
        "Bengals": "cin",
        "Dolphins": "mia",
        "Steelers": "pit",
        "49ers": "sf",
        "Rams": "lar",
        "Seahawks": "sea",
        "Packers": "gb",
        "Lions": "det",
        "Bears": "chi",
        "Vikings": "min",
        "Giants": "nyg",
        "Commanders": "was",
        "Saints": "no",
        "Falcons": "atl",
        "Panthers": "car",
        "Buccaneers": "tb",
        "Texans": "hou",
        "Colts": "ind",
        "Jaguars": "jax",
        "Titans": "ten",
        "Broncos": "den",
        "Raiders": "lv",
        "Chargers": "lac",
    }

    def _search_polymarket_game(teams: list[str]) -> str:
        """
        Slug-based Polymarket event lookup for a specific game.
        Tries slug patterns (nba-{away}-{home}-{date}) for today ± 2 days.
        Falls back to title-search within today's active NBA/NHL events.
        Returns a formatted context string with real prices, or "" on failure.
        """
        import requests as _req
        from datetime import date, timedelta

        GAMMA = "https://gamma-api.polymarket.com"

        def _sport_prefix(t1: str, t2: str) -> str:
            nba = {
                "Warriors",
                "Timberwolves",
                "Lakers",
                "Celtics",
                "Bucks",
                "Heat",
                "Nets",
                "Knicks",
                "Nuggets",
                "Suns",
                "76ers",
                "Raptors",
                "Mavericks",
                "Spurs",
                "Thunder",
                "Grizzlies",
                "Pelicans",
                "Kings",
                "Bulls",
                "Rockets",
                "Jazz",
                "Clippers",
                "Pistons",
                "Hornets",
                "Magic",
                "Hawks",
                "Pacers",
                "Cavaliers",
                "Wizards",
                "Trail Blazers",
            }
            nhl = {
                "Bruins",
                "Maple Leafs",
                "Canadiens",
                "Lightning",
                "Capitals",
                "Rangers",
                "Flyers",
                "Ducks",
                "Penguins",
                "Red Wings",
                "Blackhawks",
                "Blues",
                "Avalanche",
                "Golden Knights",
                "Oilers",
                "Flames",
                "Canucks",
            }
            nfl = {
                "Chiefs",
                "Eagles",
                "Cowboys",
                "Ravens",
                "Bills",
                "Bengals",
                "Dolphins",
                "Steelers",
                "49ers",
                "Rams",
                "Seahawks",
                "Packers",
                "Lions",
                "Bears",
                "Vikings",
                "Giants",
                "Commanders",
                "Saints",
                "Falcons",
                "Panthers",
                "Buccaneers",
                "Texans",
                "Colts",
                "Jaguars",
                "Titans",
                "Broncos",
                "Raiders",
                "Chargers",
            }
            for t in (t1, t2):
                if t in nba:
                    return "nba"
                if t in nhl:
                    return "nhl"
                if t in nfl:
                    return "nfl"
            return "nba"

        def _fmt_market(title: str, markets: list[dict]) -> str:
            """Pick the moneyline (highest-volume non-total market) and format it."""
            # Filter out totals (O/U) and prop bets; pick highest volume remainder
            non_total = [
                m
                for m in markets
                if not any(
                    kw in (m.get("question") or "").lower()
                    for kw in (
                        "o/u",
                        "over/under",
                        "total",
                        "spread",
                        "points",
                        "rebounds",
                        "assists",
                        "1h",
                        "2h",
                        "quarter",
                        "period",
                    )
                )
            ]
            candidates = non_total if non_total else markets
            best = max(
                candidates,
                key=lambda m: float(m.get("volumeNum", 0) or 0),
                default=None,
            )
            if not best:
                return ""
            prices = best.get("outcomePrices") or []
            # outcomePrices may be a JSON string like '["0.5","0.5"]' — parse it
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except (json.JSONDecodeError, ValueError):
                    return ""
            if len(prices) < 2:
                return ""
            try:
                p0 = round(float(prices[0]) * 100, 1)
                p1 = round(float(prices[1]) * 100, 1)
            except Exception:
                return ""
            vol = float(best.get("volumeNum", 0) or 0)
            vol_str = f"${vol / 1000:.0f}k vol" if vol >= 1000 else f"${vol:.0f} vol"
            accepting = best.get("acceptingOrders", False)
            status = "LIVE" if accepting else "RESOLVED"
            # Extract team labels from the event title (e.g. "Warriors vs. Pistons")
            _vs_parts = re.split(r"\s+vs\.?\s+", title, maxsplit=1)
            if len(_vs_parts) >= 2:
                label_a = _vs_parts[0].strip()
                label_b = _vs_parts[1].strip()
                return (
                    f"\n[Polymarket — {status}] {title}\n"
                    f"  {label_a} YES: {p0}¢  |  {label_b} YES: {p1}¢  |  {vol_str}"
                )
            else:
                # Non-sports market: show market question for context
                question = best.get("question", title)
                return (
                    f"\n[Polymarket — {status}] {title}\n"
                    f"  Q: {question}\n"
                    f"  YES: {p0}¢  |  NO: {p1}¢  |  {vol_str}"
                )

        # ── Try slug-based lookup (most accurate) ─────────────────────────────
        if len(teams) >= 2:
            a1 = _SLUG_ABBR.get(teams[0], teams[0].lower().replace(" ", ""))
            a2 = _SLUG_ABBR.get(teams[1], teams[1].lower().replace(" ", ""))
            prefix = _sport_prefix(teams[0], teams[1])
            today = date.today()
            for delta in (0, 1, -1, 2):
                d = (today + timedelta(days=delta)).isoformat()
                for slug in (f"{prefix}-{a1}-{a2}-{d}", f"{prefix}-{a2}-{a1}-{d}"):
                    try:
                        resp = _req.get(
                            f"{GAMMA}/events", params={"slug": slug}, timeout=8
                        )
                        items = resp.json() if resp.status_code == 200 else []
                        if items:
                            ev = items[0]
                            result = _fmt_market(
                                ev.get("title", " vs ".join(teams)),
                                ev.get("markets", []),
                            )
                            if result:
                                return result
                    except Exception as exc:
                        log.warning(
                            "[polymarket_game] Slug lookup failed for %s: %s", slug, exc
                        )
                        continue

        # ── Fallback: tag_slug search for the team's sport ────────────────────
        # tag_slug=nba/nhl/nfl returns actual games, not championship futures.
        _CITY_NAMES: dict[str, str] = {
            "Warriors": "golden state",
            "Lakers": "los angeles",
            "Clippers": "los angeles",
            "Celtics": "boston",
            "Bucks": "milwaukee",
            "Heat": "miami",
            "Nets": "brooklyn",
            "Knicks": "new york",
            "Nuggets": "denver",
            "Suns": "phoenix",
            "76ers": "philadelphia",
            "Raptors": "toronto",
            "Mavericks": "dallas",
            "Spurs": "san antonio",
            "Thunder": "oklahoma city",
            "Grizzlies": "memphis",
            "Pelicans": "new orleans",
            "Kings": "sacramento",
            "Bulls": "chicago",
            "Rockets": "houston",
            "Jazz": "utah",
            "Pistons": "detroit",
            "Hornets": "charlotte",
            "Magic": "orlando",
            "Hawks": "atlanta",
            "Pacers": "indiana",
            "Cavaliers": "cleveland",
            "Wizards": "washington",
            "Trail Blazers": "portland",
            "Timberwolves": "minnesota",
        }
        try:
            prefix = _sport_prefix(teams[0], teams[-1])
            resp = _req.get(
                f"{GAMMA}/events",
                params={
                    "tag_slug": prefix,
                    "active": "true",
                    "limit": 50,
                    "order": "startDate",
                    "ascending": "false",
                },
                timeout=10,
            )
            events = resp.json() if resp.status_code == 200 else []
            # Build lookup patterns: word-boundary regex for team names + city names
            _title_patterns = []
            _slug_tokens = set()
            for t in teams:
                _title_patterns.append(re.compile(r"\b" + re.escape(t.lower()) + r"\b"))
                city = _CITY_NAMES.get(t)
                if city:
                    _title_patterns.append(re.compile(r"\b" + re.escape(city) + r"\b"))
                abbr = _SLUG_ABBR.get(t)
                if abbr:
                    _slug_tokens.add(abbr)
            for ev in events:
                ev_title = (ev.get("title") or "").lower()
                ev_slug = (ev.get("slug") or "").lower()
                title_match = any(p.search(ev_title) for p in _title_patterns)
                slug_match = any(
                    f"-{tok}-" in ev_slug or ev_slug.endswith(f"-{tok}")
                    for tok in _slug_tokens
                )
                if title_match or slug_match:
                    result = _fmt_market(
                        ev.get("title", teams[0]), ev.get("markets", [])
                    )
                    if result:
                        return result
        except Exception as exc:
            log.warning("[polymarket_game] Tag-slug fallback search failed: %s", exc)

        # ── Tier 3: single-team search via /markets endpoint ───────────────
        # When slug and tag_slug fail, search individual markets by question text
        if len(teams) >= 1:
            try:
                resp = _req.get(
                    f"{GAMMA}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": 30,
                        "order": "volume24hrClob",
                        "ascending": "false",
                    },
                    timeout=8,
                )
                all_mkts = resp.json() if resp.status_code == 200 else []
                _team_pats = []
                for t in teams:
                    _team_pats.append(re.compile(r"\b" + re.escape(t.lower()) + r"\b"))
                    city = _CITY_NAMES.get(t)
                    if city:
                        _team_pats.append(re.compile(r"\b" + re.escape(city) + r"\b"))
                for m in all_mkts:
                    q_text = (m.get("question") or "").lower()
                    if any(p.search(q_text) for p in _team_pats):
                        prices = m.get("outcomePrices") or []
                        if isinstance(prices, str):
                            try:
                                prices = json.loads(prices)
                            except (json.JSONDecodeError, ValueError):
                                continue
                        if len(prices) >= 2:
                            p0 = round(float(prices[0]) * 100, 1)
                            p1 = round(float(prices[1]) * 100, 1)
                            vol = float(m.get("volumeNum", 0) or 0)
                            vol_str = (
                                f"${vol / 1000:.0f}k vol"
                                if vol >= 1000
                                else f"${vol:.0f} vol"
                            )
                            question = m.get("question", teams[0])
                            accepting = m.get("acceptingOrders", False)
                            status = "LIVE" if accepting else "RESOLVED"
                            return (
                                f"\n[Polymarket — {status}] {question}\n"
                                f"  YES: {p0}¢  |  NO: {p1}¢  |  {vol_str}"
                            )
            except Exception as exc:
                log.debug("[polymarket_game] Single-team market search failed: %s", exc)

        return ""

    _SERIES_MAP = [
        (["fed", "fomc", "rate"], "KXFED"),
        (["inflation", "cpi"], "KXINFL"),
        (["bitcoin", "btc"], "KXBTC"),
        (["nba", "basketball"], "KXNBA"),
        (["nfl", "football"], "KXNFL"),
        (["election", "president", "trump"], "KXPRES"),
    ]
    q = user_msg.lower()
    market_context = ""

    # ── Priority 1: specific game matchup detected → search Polymarket directly ──
    _mentioned_teams = _find_team_mentions(q)
    if len(_mentioned_teams) >= 2 or (
        len(_mentioned_teams) == 1
        and any(
            kw in q
            for kw in (
                "vs",
                "versus",
                "game",
                "tonight",
                "match",
                "beat",
                "win",
                "cover",
                "price",
                "market",
                "scan",
                "odds",
                "line",
                "bet",
                "chances",
                "probability",
                "playing",
                "play",
                "spread",
            )
        )
    ):
        market_context = _search_polymarket_game(_mentioned_teams)

    # ── Priority 2: generic sport topic → Kalshi series (championship / season markets) ──
    if not market_context:
        for keywords, series in _SERIES_MAP:
            if any(kw in q for kw in keywords):
                try:
                    markets = _kalshi_api.get_markets(
                        limit=3, series_ticker=series, min_volume=1
                    )
                    if markets:
                        lines = [
                            f"\nLive {series} Kalshi markets (season/championship):"
                        ]
                        for m in markets:
                            prob = _kalshi_api.parse_market_prob(m)
                            vol = _kalshi_api.parse_volume(m)
                            lines.append(
                                f"- {m.get('title', m.get('ticker'))}: {prob:.0%} yes | ${vol:,.0f} vol"
                            )
                        market_context = "\n".join(lines)
                except Exception:
                    pass
                break

    # ── Priority 3: topic-based Polymarket search ─────────────────────────
    # If the user asks about a specific topic (bitcoin, trump, oscars, etc.)
    # search Polymarket events by tag_slug. _TOPIC_TAGS is module-level.
    if not market_context:
        _matched_tag = None
        for keyword, tag in _TOPIC_TAGS.items():
            if keyword in q:
                _matched_tag = tag
                break
        if _matched_tag:
            try:
                import requests as _req

                # Try multiple tag formats — Polymarket is inconsistent:
                # "elon-musk" might be stored as "elon musk" or just "elon"
                events = []
                _tag_attempts = [
                    _matched_tag,
                    _matched_tag.replace("-", " "),
                    _matched_tag.split("-")[0],
                ]
                for _tag_try in dict.fromkeys(
                    _tag_attempts
                ):  # deduplicate, preserve order
                    resp = _req.get(
                        "https://gamma-api.polymarket.com/events",
                        params={
                            "tag_slug": _tag_try,
                            "active": "true",
                            "limit": 8,
                            "order": "volume",
                            "ascending": "false",
                        },
                        timeout=10,
                    )
                    events = resp.json() if resp.status_code == 200 else []
                    if events:
                        break
                if events:
                    lines = [f"\n[Polymarket — LIVE {_matched_tag.upper()} markets]"]
                    for ev in events[:5]:
                        title = ev.get("title", "?")
                        mkts = ev.get("markets", [])
                        if not mkts:
                            continue
                        best = max(
                            mkts,
                            key=lambda m: float(m.get("volumeNum", 0) or 0),
                            default=None,
                        )
                        if not best:
                            continue
                        prices = best.get("outcomePrices") or "[]"
                        if isinstance(prices, str):
                            try:
                                prices = json.loads(prices)
                            except (json.JSONDecodeError, ValueError):
                                continue
                        if len(prices) < 2:
                            continue
                        p0 = round(float(prices[0]) * 100, 1)
                        vol = float(best.get("volumeNum", 0) or 0)
                        vol_str = (
                            f"${vol / 1000:.0f}k" if vol >= 1000 else f"${vol:.0f}"
                        )
                        lines.append(f"  • {title[:70]} — YES: {p0}¢ | {vol_str} vol")
                    if len(lines) > 1:
                        market_context = "\n".join(lines)
            except Exception as exc:
                log.warning("[market_context] Topic tag search failed: %s", exc)

    # ── Priority 4: generic market question → top trending Polymarket markets ──
    # Catches "what's on Polymarket", "show me markets", "any good markets",
    # "what should I bet on", "odds", "prices", "prediction market"
    if not market_context:
        _GENERIC_MARKET_KW = {
            "polymarket",
            "prediction market",
            "what market",
            "which market",
            "show me market",
            "any market",
            "trending market",
            "hot market",
            "what should i bet",
            "what should i trade",
            "any good bet",
            "what's trading",
            "whats trading",
            "top market",
            "scan market",
            "show me odds",
            "what markets are",
            "any markets",
            "best market",
            "good bet",
            "what to trade",
            "active markets",
            "what's popular",
            "whats popular",
            "live markets",
            "live odds",
            "show me",
            "what can i bet",
            "give me markets",
            "find me a market",
        }
        if any(kw in q for kw in _GENERIC_MARKET_KW):
            try:
                import requests as _req

                resp = _req.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={
                        "active": "true",
                        "limit": 10,
                        "order": "volume24hrClob",
                        "ascending": "false",
                        "closed": "false",
                    },
                    timeout=10,
                )
                mkts = resp.json() if resp.status_code == 200 else []
                if mkts:
                    lines = ["\n[Polymarket — TOP TRENDING MARKETS (24h volume)]"]
                    for m in mkts[:8]:
                        question = m.get("question", "?")
                        prices = m.get("outcomePrices") or "[]"
                        if isinstance(prices, str):
                            try:
                                prices = json.loads(prices)
                            except (json.JSONDecodeError, ValueError):
                                continue
                        p0 = round(float(prices[0]) * 100, 1) if len(prices) >= 1 else 0
                        vol24 = float(m.get("volume24hrClob", 0) or 0)
                        vol_str = (
                            f"${vol24 / 1000:.0f}k"
                            if vol24 >= 1000
                            else f"${vol24:.0f}"
                        )
                        spread = float(m.get("spreadBps", 0) or 0) / 100
                        lines.append(
                            f"  • {question[:65]} — YES: {p0}¢ | "
                            f"24h vol: {vol_str} | spread: {spread:.1f}%"
                        )
                    if len(lines) > 1:
                        market_context = "\n".join(lines)
            except Exception as exc:
                log.warning("[market_context] Trending markets fetch failed: %s", exc)

    # ── Inject hard "NO LIVE DATA" block when lookup was attempted but failed ──
    # Without this, market_context is "" and the AI can silently ignore the lack
    # of data and hallucinate prices from training memory.
    _wanted_market_data = bool(_mentioned_teams)
    if _wanted_market_data and not market_context:
        market_context = (
            "\n\n[NO LIVE MARKET DATA AVAILABLE]\n"
            "All Polymarket and Kalshi lookups returned empty for this query.\n"
            "You MUST tell the user: 'I don't have live market data for that right now — "
            "check polymarket.com directly for current prices.'\n"
            "Do NOT guess, estimate, or recall prices from memory. Any price you state "
            "without a [Polymarket] data block above is a HALLUCINATION."
        )
        log.info("[market_context] No live data found for teams=%s", _mentioned_teams)

    # ── On-demand paper trade ─────────────────────────────────────────────────
    # Catches: "paper trade Warriors YES", "bet YES on Nets", "I'll take Lakers NO"
    # Must run AFTER _find_team_mentions / _SLUG_ABBR / _sport_prefix are defined.
    _PT_RE = re.compile(
        r"(?:paper\s*(?:trade|bet)|put\s+me\s+down|(?:i(?:'ll| will| want to| wanna)\s+(?:take|pick|bet|paper))|(?:^|\s)bet\b)"
        r"(?:"
        r"\s+(?:on\s+)?(.{2,40}?)\s+(yes|no)\b"  # A: "paper trade Warriors YES"
        r"|"
        r"\s+(yes|no)\s+(?:on\s+)?(.{2,40})"  # B: "paper trade NO on warriors"
        r")",
        re.IGNORECASE,
    )
    _pt_match = _PT_RE.search(user_msg)
    if _pt_match:
        if _pt_match.group(1):
            _pt_topic = _pt_match.group(1).strip()
            _pt_side = _pt_match.group(2).upper()
        else:
            _pt_side = _pt_match.group(3).upper()
            _pt_topic = _pt_match.group(4).strip()
        # Strip trailing noise: "today's warriors game" → "warriors"
        _pt_topic = re.sub(
            r"(?:today'?s?\s+|tonight'?s?\s+|the\s+)", "", _pt_topic, flags=re.I
        ).strip()
        _pt_topic = re.sub(
            r"\s+(?:game|match|market|tonight|today)$", "", _pt_topic, flags=re.I
        ).strip()
        # Strip betting jargon that pollutes search: "Warriors ML" → "Warriors"
        _pt_topic = re.sub(
            r"\b(?:ml|moneyline|money\s*line|spread|over|under|o/u|ats|pts|points|props?|parlay|alt)\b",
            "",
            _pt_topic,
            flags=re.I,
        ).strip()
        _pt_topic = re.sub(r"\s{2,}", " ", _pt_topic)  # collapse double spaces
        if _pt_topic and _pt_side in ("YES", "NO"):
            try:
                import requests as _ptreq
                from datetime import date as _ptdate, timedelta as _pttd

                _pt_teams = _find_team_mentions(_pt_topic.lower())
                _found_ev = None

                # Path A: team detected — try slug (2 teams) then tag_slug title match
                if _pt_teams:
                    _a1 = _SLUG_ABBR.get(
                        _pt_teams[0], _pt_teams[0].lower().replace(" ", "")
                    )
                    _pfx = _sport_prefix(_pt_teams[0], _pt_teams[-1])
                    _today = _ptdate.today()
                    # Slug lookup only works with 2 teams
                    if len(_pt_teams) >= 2:
                        _a2 = _SLUG_ABBR.get(
                            _pt_teams[1], _pt_teams[1].lower().replace(" ", "")
                        )
                        for _delta in (0, 1, -1, 2, 3, 4, 5, 6):
                            _d = (_today + _pttd(days=_delta)).isoformat()
                            for _sl in (
                                f"{_pfx}-{_a1}-{_a2}-{_d}",
                                f"{_pfx}-{_a2}-{_a1}-{_d}",
                            ):
                                _rr = _ptreq.get(
                                    "https://gamma-api.polymarket.com/events",
                                    params={"slug": _sl},
                                    timeout=6,
                                )
                                _items = _rr.json() if _rr.status_code == 200 else []
                                if _items:
                                    _found_ev = _items[0]
                                    break
                            if _found_ev:
                                break
                    # tag_slug fallback: search by sport, match team name in title
                    # Works for BOTH single-team ("Warriors YES") and 2-team queries
                    if not _found_ev:
                        _rr2 = _ptreq.get(
                            "https://gamma-api.polymarket.com/events",
                            params={
                                "tag_slug": _pfx,
                                "active": "true",
                                "limit": 50,
                                "order": "startDate",
                                "ascending": "false",
                            },
                            timeout=8,
                        )
                        _evs = _rr2.json() if _rr2.status_code == 200 else []
                        _pats = [
                            re.compile(r"\b" + re.escape(t.lower()) + r"\b")
                            for t in _pt_teams
                        ]
                        for _ev in _evs:
                            if any(
                                p.search((_ev.get("title") or "").lower())
                                for p in _pats
                            ):
                                _found_ev = _ev
                                break
                    # Text search fallback: search by team name directly in Gamma
                    if not _found_ev:
                        _rr2b = _ptreq.get(
                            "https://gamma-api.polymarket.com/events",
                            params={
                                "title": _pt_teams[0],
                                "active": "true",
                                "limit": 10,
                                "order": "startDate",
                                "ascending": "false",
                            },
                            timeout=8,
                        )
                        _evs2b = _rr2b.json() if _rr2b.status_code == 200 else []
                        for _ev in _evs2b:
                            _ev_title_lc = (_ev.get("title") or "").lower()
                            if _pt_teams[0].lower() in _ev_title_lc:
                                _found_ev = _ev
                                break

                # Path B: no team found — try topic tag (but VERIFY result matches)
                if not _found_ev:
                    _tag = _pt_topic.lower().replace(" ", "-")
                    for _tag_try in [_tag, _tag.split("-")[0]]:
                        _rr3 = _ptreq.get(
                            "https://gamma-api.polymarket.com/events",
                            params={
                                "tag_slug": _tag_try,
                                "active": "true",
                                "limit": 5,
                                "order": "volume",
                                "ascending": "false",
                            },
                            timeout=8,
                        )
                        _evs3 = _rr3.json() if _rr3.status_code == 200 else []
                        if _evs3:
                            # If we had teams, verify the result actually matches
                            if _pt_teams:
                                _ev_title_check = (_evs3[0].get("title") or "").lower()
                                if any(t.lower() in _ev_title_check for t in _pt_teams):
                                    _found_ev = _evs3[0]
                                    break
                                # else: SKIP — wrong market (prevents Trump tariff bug)
                            else:
                                _found_ev = _evs3[0]
                                break

                # Explicit "not found" — don't fall through to AI with wrong data
                if not _found_ev:
                    await update.message.reply_text(
                        f"❌ No live Polymarket market found for '{_pt_topic}' right now.\n"
                        f"The game may not be listed yet. Check polymarket.com or try later."
                    )
                    return

                if _found_ev:
                    _pt_title = _found_ev.get("title", _pt_topic)
                    _pt_mkts = _found_ev.get("markets", [])
                    _pt_best = max(
                        _pt_mkts,
                        key=lambda m: float(m.get("volumeNum", 0) or 0),
                        default=None,
                    )
                    if _pt_best:
                        _pt_prices = _pt_best.get("outcomePrices", "[]")
                        if isinstance(_pt_prices, str):
                            _pt_prices = json.loads(_pt_prices)
                        # YES = prices[0], NO = prices[1]
                        _entry_prob = (
                            float(_pt_prices[0])
                            if _pt_side == "YES"
                            else float(_pt_prices[1])
                            if len(_pt_prices) > 1
                            else 0.5
                        )
                        _pt_market_id = _found_ev.get(
                            "slug", _pt_topic.lower().replace(" ", "-")
                        )
                        _pt_signal_id = int(time.time() * 1000) % 2_000_000_000
                        _ot.register_signal(
                            signal_id=_pt_signal_id,
                            market_id=_pt_market_id,
                            venue="POLYMARKET",
                            target_side=_pt_side,
                            entry_prob=_entry_prob,
                            question=_pt_title,
                        )
                        _ot.record_user_pick(
                            _pt_signal_id, _pt_market_id, user_id, _pt_side
                        )
                        await update.message.reply_text(
                            f"✅ Paper trade logged!\n"
                            f"Market: {_pt_title[:60]}\n"
                            f"Side: {_pt_side} @ {_entry_prob * 100:.1f}¢  |  $10 virtual stake\n\n"
                            f"Use /mytrades to track it."
                        )
                        return
                else:
                    await update.message.reply_text(
                        f"Couldn't find a live Polymarket market for '{_pt_topic}'. "
                        f"Try a more specific name (e.g. 'paper trade Warriors YES')."
                    )
                    return
            except Exception as _pt_exc:
                log.warning("[paper_trade] On-demand paper trade failed: %s", _pt_exc)

    # 4. Recent scan opportunities as context
    svc = _get_service()
    top = svc.engine.top_opportunities(limit=3)
    scan_context = ""
    if top:
        lines = ["\nRecent scan opportunities:"]
        for r in top:
            lines.append(
                f"- {r.metadata.get('question', r.market_id)[:60]} "
                f"| market={r.market_prob:.0%} edge={r.edge:+.0%}"
            )
        scan_context = "\n".join(lines)

    # 5. Live injury context — injected when the message mentions a sport/team/player.
    #    Before building context, auto-refresh the cache if it is stale (>2h) or empty
    #    so the AI always sees real data rather than falling back to training knowledge.
    _SPORT_DETECT = {
        "nba": {
            "nba",
            "basketball",
            "lakers",
            "celtics",
            "warriors",
            "bucks",
            "heat",
            "nets",
            "knicks",
            "nuggets",
            "suns",
            "sixers",
            "raptors",
            "mavericks",
            "mavs",
            "spurs",
            "thunder",
            "grizzlies",
            "pelicans",
            "kings",
            "bulls",
            "rockets",
            "jazz",
            "clippers",
            "pistons",
            "hornets",
            "magic",
            "hawks",
            "pacers",
            "cavaliers",
            "wizards",
            "timberwolves",
            "trail blazers",
        },
        "nfl": {
            "nfl",
            "football",
            "chiefs",
            "eagles",
            "cowboys",
            "ravens",
            "bills",
            "bengals",
            "dolphins",
            "steelers",
            "49ers",
            "rams",
            "seahawks",
            "patriots",
            "packers",
            "bears",
            "giants",
            "saints",
            "buccaneers",
            "chargers",
            "raiders",
            "broncos",
            "texans",
            "colts",
            "titans",
            "jaguars",
            "browns",
            "falcons",
            "panthers",
            "cardinals",
            "vikings",
        },
        "nhl": {
            "nhl",
            "hockey",
            "oilers",
            "bruins",
            "rangers",
            "leafs",
            "canadiens",
            "penguins",
            "capitals",
            "lightning",
            "golden knights",
            "kraken",
            "avalanche",
            "flames",
            "canucks",
            "senators",
            "sabres",
            "coyotes",
            "sharks",
            "ducks",
            "kings",
            "blues",
            "predators",
            "wild",
            "jets",
            "red wings",
            "islanders",
            "devils",
            "flyers",
            "hurricanes",
        },
    }
    _chat_sport = None
    for _sp, _triggers in _SPORT_DETECT.items():
        if any(_t in q for _t in _triggers):
            _chat_sport = _sp
            break
    if _chat_sport:
        await _maybe_refresh_injury_cache(_chat_sport)

    injury_context = _build_injury_context(q)

    # 5b. Player-name detection — when user mentions a specific player by name,
    #     trigger a web search for their current team/status to avoid stale roster data.
    #     Pattern: 2+ capitalized words that aren't team names (e.g. "Klay Thompson",
    #     "LeBron James", "Steph Curry"). Only fires in sport context.
    _player_search_context = ""
    if _chat_sport:
        _player_pat = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", user_msg)
        # Filter out known team names, cities, and common non-player phrases
        _non_player = {
            "Golden Knights",
            "Red Wings",
            "Trail Blazers",
            "Red Sox",
            "White Sox",
            "Blue Jays",
            "Maple Leafs",
            "Golden State",
            "New York",
            "Los Angeles",
            "San Francisco",
            "San Antonio",
            "New Orleans",
            "Oklahoma City",
            "Kansas City",
            "Green Bay",
            "Tampa Bay",
            "Las Vegas",
            "Salt Lake",
            "Paper Trade",
            "March Madness",
            "Super Bowl",
            "World Series",
        }
        _player_names = [
            p for p in _player_pat if p not in _non_player and len(p.split()) <= 3
        ]
        if _player_names:
            _pname = _player_names[0]
            _p_query = f"{_pname} current team roster {_chat_sport.upper()} 2026"
            _ploop = asyncio.get_running_loop()
            _player_search_context = await _ploop.run_in_executor(
                None, _tavily_search, _p_query
            )
            if not _player_search_context:
                _player_search_context = await _ploop.run_in_executor(
                    None, _serper_search, _p_query
                )
            if _player_search_context:
                _player_search_context = (
                    f"\n[Live player lookup for {_pname} — use this for current team info, "
                    f"NOT your training data]\n" + _player_search_context
                )
                log.debug("[player_lookup] Searched for player: %s", _pname)

    # 6. Real-time data refresh — fires when sport detected OR user corrects us.
    #    On correction + team mention: bypass cache and re-query Polymarket live.
    #    On correction + sport only: force fresh web search with expanded query.
    #    On sport only (no correction): standard injury web search.
    search_context = ""
    if _is_correction and _mentioned_teams:
        # User says our price is wrong — re-hit Polymarket directly, bypass cache
        # Clear file cache for this query so we don't return the same stale result
        import glob as _glob, os as _os

        for _f in _glob.glob(".cache/trader_leaderboard*.json"):
            try:
                _os.remove(_f)
            except Exception:
                pass
        _fresh_market = _search_polymarket_game(_mentioned_teams)
        if _fresh_market:
            market_context = _fresh_market  # override with freshly fetched data
            search_context = "\n[Market data refreshed from Polymarket live feed]"
        else:
            search_context = "\n[No live Polymarket market found for that matchup]"

    elif _chat_sport or _is_correction:
        if _is_correction and _chat_sport:
            _query = f"{user_msg} {_chat_sport.upper()} latest update today"
        elif _is_correction:
            _query = f"{user_msg} latest confirmed data today"
        else:
            _query = f"{user_msg} {_chat_sport.upper()} injury report today"
        loop = asyncio.get_running_loop()
        search_context = await loop.run_in_executor(None, _tavily_search, _query)
        if not search_context:
            search_context = await loop.run_in_executor(None, _serper_search, _query)

    # Merge player lookup into search context so AI sees current roster data
    if _player_search_context:
        search_context = _player_search_context + (
            "\n\n" + search_context if search_context else ""
        )

    # 6b. Topic-based news search — fires for non-sport topics (Oscars, Tesla,
    #     UFC, NHL, politics, crypto, etc.) when no sport game is detected.
    #     Injects live news headlines so the AI has real context to reason from
    #     instead of relying on potentially stale training data.
    if not search_context:
        _topic_news_tag = None
        for _tkw, _ttag in _TOPIC_TAGS.items():
            if _tkw in q:
                _topic_news_tag = _ttag
                break
        if _topic_news_tag:
            _news_query = _TOPIC_NEWS_QUERIES.get(
                _topic_news_tag,
                f"{_topic_news_tag.replace('-', ' ')} latest news today 2026",
            )
            _tloop = asyncio.get_running_loop()
            search_context = await _tloop.run_in_executor(
                None, _tavily_search, _news_query
            )
            if not search_context:
                search_context = await _tloop.run_in_executor(
                    None, _serper_search, _news_query
                )
            log.debug(
                "[topic_news] tag=%s query=%r ctx_len=%d",
                _topic_news_tag,
                _news_query,
                len(search_context),
            )

    # 7. Win-probability impact context — injected when a sport is detected.
    #    Prepended to search_context so the AI reasons with actual shift math
    #    (e.g. "LeBron Out → -12.3% win-prob shift, 10.5 pts/gm impact") rather
    #    than generic statements about a player being injured.
    if _chat_sport and _chat_sport != "unknown":
        wp_context = _build_win_prob_context(_chat_sport)
        if wp_context:
            search_context = wp_context + (
                "\n\n" + search_context if search_context else ""
            )

    # 7b. Game Assessment context — when user asks for matchup analysis, preview,
    #     or prediction, fetch standings, records, H2H results, coaching info.
    game_assessment_context = ""
    if _mentioned_teams and len(_mentioned_teams) >= 2:
        sport_for_assessment = _chat_sport or "nba"
        game_assessment_context = _build_game_assessment_context(
            _mentioned_teams, sport_for_assessment, user_msg
        )
        if game_assessment_context:
            log.debug(
                "[game_assessment] Built context for %s vs %s",
                _mentioned_teams[0],
                _mentioned_teams[1],
            )

    # 7c. Today's Games context — when user asks "what games are today/tonight"
    todays_games_context = _build_todays_games_context(user_msg)

    # 8. Smart money positions — injected when user asks about trading or markets.
    #    Shows what top-scored vetted wallets are currently positioned on so the
    #    AI can surface copy-trade ideas and smart money alignment signals.
    _SMART_MONEY_TRIGGERS = {
        "trade",
        "buy",
        "bet",
        "position",
        "market",
        "edge",
        "wallet",
        "copy",
        "follow",
        "who",
        "smart money",
        "trader",
        "recommend",
        "should i",
        "worth",
        "call",
        "play",
        "play on",
        "long",
        "short",
        # streak / hot-hand queries
        "streak",
        "hot",
        "on fire",
        "killing it",
        "winning",
        # strategy / specialist queries
        "specialist",
        "expert",
        "best at",
        "who trades",
        "who bets",
        "nba trader",
        "nhl trader",
        "crypto trader",
        "sports trader",
        # copy-trade intent
        "copy trade",
        "who should",
        "top trader",
        "best trader",
    }
    # Detect sport context in the question for specialist routing
    _SPORT_KEYWORDS: dict[str, str] = {
        "nba": "NBA",
        "basketball": "NBA",
        "nhl": "NHL",
        "hockey": "NHL",
        "nfl": "NFL",
        "football": "NFL",
        "mlb": "MLB",
        "baseball": "MLB",
        "soccer": "Soccer",
        "football": "Soccer",
        "crypto": "Crypto",
        "bitcoin": "Crypto",
        "btc": "Crypto",
        "politics": "Politics",
        "election": "Politics",
    }
    sm_sport = next((label for kw, label in _SPORT_KEYWORDS.items() if kw in q), "")

    smart_money_context = ""
    if any(kw in q for kw in _SMART_MONEY_TRIGGERS) or market_context or scan_context:
        smart_money_context = _build_smart_money_context(sport_filter=sm_sport)

    # 8b. Live crypto prices — injected when the user asks about BTC, ETH, crypto.
    #     Uses Binance 15-min cache — gives AI real prices instead of guessing.
    _CRYPTO_TRIGGERS = {
        "bitcoin",
        "btc",
        "ethereum",
        "eth",
        "solana",
        "sol",
        "xrp",
        "crypto",
        "doge",
        "bnb",
        "coin",
        "token",
        "blockchain",
        "price",
        "dump",
        "pump",
        "rally",
        "altcoin",
        "defi",
        "nft",
        "bull",
        "bear",
        "market cap",
    }
    crypto_price_context = ""
    if any(kw in q for kw in _CRYPTO_TRIGGERS):
        try:
            crypto_price_context = (
                "\n\n" + get_crypto_price_context()
                if get_crypto_price_context()
                else ""
            )
        except Exception:
            pass

    # 8c. Live economic rates — injected when the user asks about the Fed, rates, inflation.
    #     Uses NY Fed API + Treasury yields — gives AI real rate context.
    _ECON_TRIGGERS = {
        "fed",
        "federal reserve",
        "rate",
        "fomc",
        "cut",
        "hike",
        "pause",
        "inflation",
        "cpi",
        "pce",
        "recession",
        "gdp",
        "yield",
        "treasury",
        "interest",
        "monetary",
        "hawkish",
        "dovish",
        "basis points",
        "bps",
        "unemployment",
        "jobs",
        "nonfarm",
        "payroll",
    }
    econ_rate_context = ""
    if any(kw in q for kw in _ECON_TRIGGERS):
        try:
            _ec = get_econ_context_string()
            econ_rate_context = "\n\n" + _ec if _ec else ""
        except Exception:
            pass

    # ── Correction mode instruction — prepended to system prompt ─────────────
    correction_instruction = (
        "CORRECTION MODE ACTIVE — the user told you your previous answer was wrong or stale.\n"
        "Rules:\n"
        "1. Start your reply by briefly acknowledging the mistake — be direct and natural "
        "(e.g. 'My bad, let me pull fresh data.' or 'You're right, that was off — here's "
        "what I'm seeing now:'). Keep the acknowledgment to ONE sentence.\n"
        "2. Use ONLY the [Polymarket] or [Market data refreshed] block below for prices — "
        "NEVER repeat the price you gave before.\n"
        "3. If the refreshed block shows different data, lead with that and explain the delta.\n"
        "4. If you still can't find accurate data, say honestly: 'I'm not finding a live feed "
        "for that right now — check polymarket.com directly.' Do NOT guess or hallucinate.\n"
        "5. Never be defensive or make excuses — just correct and move forward.\n\n"
        if _is_correction
        else ""
    )

    system_prompt = (
        correction_instruction
        + "You are EDGE, an AI prediction market analyst operating on Telegram. "
        "Your job: help users find and act on mispriced prediction markets on Polymarket and Kalshi. "
        "You scan markets for edge (mispriced probability), vet smart money traders, track injuries, "
        "and answer questions about trading, strategy, and platform setup.\n\n"
        "PLATFORMS YOU SUPPORT:\n"
        "• Polymarket — decentralized, USDC on Polygon, no KYC, 0% fees, global\n"
        "• Kalshi — US-regulated (CFTC), USD via bank/card, KYC required, ~7% fee on winnings\n\n"
        "ONBOARDING: If a [Platform Setup Reference] block is in the prompt, use it verbatim "
        "to answer setup/deposit/fee questions. Do not guess — if it's in the docs, cite it.\n\n"
        "PERSONALIZATION: A [What you know about {name}] block may appear in the prompt. "
        "Use it to be a knowledgeable friend — reference favorites, rivals, and past moments "
        "naturally and with genuine emotion. Express concern for injuries to their fav players, "
        "excitement for returns, empathy for their team's struggles. Never feel robotic or scripted.\n"
        "⚠️ CRITICAL: NEVER assume a user's location, city, or timezone from their favorite team. "
        "A fan of the Brooklyn Nets may live in Texas. A Lakers fan may live in Boston. "
        "Only reference their location if a 'Location:' field is explicitly listed in the profile block. "
        "Do NOT mention local weather, local events, or geographic references based on team fandom alone.\n"
        "MEMORY LIMITATIONS: You CANNOT directly edit, update, or modify user profile data. "
        "If a user asks you to remove or correct stored info, tell them to use /forget <key> "
        "(e.g., /forget city). NEVER claim you've 'updated', 'edited', or 'removed' anything "
        "from their profile — you do not have that ability. "
        "Only the /forget command and specific correction phrases ('I don't live in X') actually work.\n\n"
        + onboarding_hint
        + ("\n\n" if onboarding_hint else "")
        + "BREVITY IS MANDATORY. Telegram users want 1-3 sentences for simple questions, "
        "max 100 words for market analysis. No filler phrases, no emoji stories, no life "
        "analogies, no sports metaphors. Give the data, the edge, and the recommendation. "
        "Return plain text (no JSON).\n\n"
        "ROSTER KNOWLEDGE CUTOFF: Your training data for player rosters, trades, and "
        "team compositions may be STALE. Do NOT reference specific players as being on "
        "specific teams unless that info appears in a [Live injury data] block. "
        "Do NOT make analogies using player-team associations from memory.\n\n"
        "CONVERSATIONAL CONTINUITY: If a user sends a short affirmative reply ('yes', 'yeah', "
        "'sure') and the session context shows you just asked them a question, treat their "
        "reply as answering YOUR question. Follow up appropriately — do not change topics.\n\n"
        "LIVE MARKET DATA RULES:\n"
        "• You have LIVE access to Polymarket and Kalshi via API. Data blocks labeled "
        "[Polymarket] or [Polymarket — LIVE] contain real-time prices pulled seconds ago.\n"
        "• When a [Polymarket] block is in the prompt, use THOSE exact prices — no exceptions. "
        "outcomePrices[0] is Team A YES probability, outcomePrices[1] is Team B YES probability.\n"
        "• NEVER cite prices from training memory. They are always stale and wrong.\n"
        "• If the market block shows [RESOLVED], the game has already ended — say so.\n"
        "• If a [NO LIVE MARKET DATA AVAILABLE] block appears, you MUST NOT provide ANY price, "
        "probability, or odds. Saying 'I think it's around X%' or 'last I saw' is FORBIDDEN. "
        "Only say you don't have live data and suggest they ask about a specific team or topic.\n"
        "• Kalshi series data = season/championship futures — NOT individual game prices.\n"
        "• NEVER say 'I don't have a live Polymarket feed' unless a [NO LIVE MARKET DATA] "
        "block is explicitly present. If you see a [Polymarket] block, you DO have live data.\n"
        "• [Live web search results] blocks contain REAL-TIME news pulled seconds ago — "
        "use them to answer questions about Oscars nominees, tech stocks, UFC results, "
        "NHL standings, politics news, etc. Treat this as ground truth for current events.\n"
        "• When [Live web search results] is present: cite the headlines/facts naturally. "
        "Do NOT say 'I don't have access to current information' — you literally do.\n\n"
        f"IN-SEASON SPORTS (month {datetime.now(timezone.utc).month}):\n"
        "• NBA, NHL: IN SEASON — provide game prices and injury analysis.\n"
        "• NFL, CFB: OFF SEASON — do NOT show game lines or injury reports. "
        "If asked about NFL, say 'NFL is in the off-season (season starts September).' "
        "NFL futures/championship markets are still valid.\n"
        "• NCAA March Madness (CBB): IN SEASON March–April.\n\n"
        "INJURY DATA RULES — apply ONLY when the user's current message explicitly "
        "asks about injuries, player health, roster status, or a specific game matchup:\n"
        "• If injury data IS in [Live injury data] or [Live web search results]: cite it.\n"
        "• If the player/team is NOT in those blocks: say 'I don't have current data "
        "for [name] — use /injuries nba (or nfl/nhl) to refresh.'\n"
        "• NEVER invent or recall injury statuses from training memory.\n"
        "• ROSTER ACCURACY: NEVER cite a player's team affiliation from your training "
        "data — rosters change constantly (trades, free agency, waivers). Only reference "
        "a player's current team if it appears in [Live injury data] or [Live web search results] "
        "in this prompt. If unsure, say 'I'd need to check current rosters' rather than guessing.\n"
        "• For ALL other questions (scan results, market edges, strategy, commands, "
        "general chat): answer normally — do NOT mention injuries or data blocks "
        "unless the user directly asked about them.\n\n"
        "PAPER TRADING — THIS IS A BUILT-IN FEATURE, NOT A MISSING FEATURE:\n"
        "• ON-DEMAND: Users can paper trade ANY market instantly by saying "
        "'paper trade [team/topic] YES' or 'paper trade [team/topic] NO'. "
        "Example: 'paper trade Warriors YES' or 'bet NO on Lakers'. "
        "The bot logs it immediately at the live Polymarket price — NO buttons needed.\n"
        "• SCAN ALERTS also have 📈 YES / 📉 NO buttons for one-tap logging.\n"
        "• /mytrades — shows all open paper picks with potential payout, plus settled "
        "history (WIN/LOSS/VOID) with actual P&L.\n"
        "• /performance — shows EDGE bot win rate AND the user's personal paper P&L, "
        "win rate, and ROI across all their picks.\n"
        "• Picks auto-resolve when the underlying Polymarket/Kalshi market settles — "
        "no manual tracking required.\n"
        "• When a user says 'I want to put in a paper trade' or 'how do I bet', "
        "tell them: 'Just say paper trade [topic] YES or NO — I'll log it instantly!'\n"
        "• NEVER say paper trading is unavailable or that they need to find a scan alert first.\n\n"
        "CRITICAL — YOU ARE A PREDICTION MARKET ANALYST, NOT A SPORTSBOOK:\n"
        "• NEVER use sportsbook spread language: no '+3.5', '-7.5', 'moneyline', "
        "'ATS', 'cover', 'over/under', 'juice', '-110', or point spreads.\n"
        "• ALWAYS frame edges as probability: "
        "'Market: 61% | Model: 56% | Edge: -5pp — sell the favourite.'\n"
        "• For injury impact say: 'Mahomes out shifts KC win prob ~-7pp from 65% to 58%' "
        "not 'Chiefs are now -3 underdogs'.\n"
        "• Prices are probabilities (0-100%), positions are YES/NO contracts, "
        "not sides or totals."
    )

    # Save correction event to session memory so the AI's next response
    # has full context that a correction happened
    if _is_correction:
        _mem_user.add_exchange(
            "[USER CORRECTION]",
            "[Bot acknowledged correction — performed expanded search]",
        )

    # Dynamic guardrail: when live data IS present, prepend a hard reminder
    # so the AI never says "I don't have live data" when it literally does.
    _data_available_hint = ""
    _has_live = bool(
        market_context
        or scan_context
        or search_context
        or injury_context
        or game_assessment_context
        or todays_games_context
        or crypto_price_context
        or econ_rate_context
    )
    if _has_live:
        _blocks = []
        if market_context:
            _blocks.append("market prices")
        if scan_context:
            _blocks.append("scan opportunities")
        if search_context:
            _blocks.append("web search results")
        if injury_context:
            _blocks.append("injury data")
        if game_assessment_context:
            _blocks.append("game assessment data")
        if todays_games_context:
            _blocks.append("today's games schedule")
        if crypto_price_context:
            _blocks.append("crypto prices")
        if econ_rate_context:
            _blocks.append("economic rates")
        _data_available_hint = (
            f"\n\n⚡ LIVE DATA LOADED: You have real-time {', '.join(_blocks)} below. "
            "USE THIS DATA. Do NOT say 'I don't have live data' or 'I don't have access "
            "to current information' — the data is RIGHT HERE in this prompt.\n"
        )

    prompt = (
        user_msg
        + kb_context
        + platform_doc_context
        + profile_context  # long-term personal facts about this user
        + session_context  # today's conversation history (per user)
        + _data_available_hint
        + market_context
        + scan_context
        + smart_money_context  # top-scored wallet positions (copy-trade signal)
        + injury_context
        + search_context
        + game_assessment_context  # matchup analysis: records, H2H, coaching, all-stars
        + todays_games_context  # today's game schedule when asked
        + crypto_price_context  # live Binance prices (BTC, ETH, SOL)
        + econ_rate_context  # live NY Fed rates + Treasury yields
    )

    # Build context block list for decision_log (which blocks were non-empty)
    _active_ctx_blocks: list[str] = []
    if kb_context:
        _active_ctx_blocks.append("knowledge_base")
    if platform_doc_context:
        _active_ctx_blocks.append("platform_docs")
    if profile_context:
        _active_ctx_blocks.append("user_profile")
    if session_context:
        _active_ctx_blocks.append("session_history")
    if market_context:
        _active_ctx_blocks.append("market_data")
    if scan_context:
        _active_ctx_blocks.append("scan_results")
    if smart_money_context:
        _active_ctx_blocks.append("smart_money")
    if injury_context:
        _active_ctx_blocks.append("injuries")
    if search_context:
        _active_ctx_blocks.append("web_search")
    if game_assessment_context:
        _active_ctx_blocks.append("game_assessment")
    if todays_games_context:
        _active_ctx_blocks.append("todays_games")
    if crypto_price_context:
        _active_ctx_blocks.append("crypto_prices")
    if econ_rate_context:
        _active_ctx_blocks.append("econ_rates")

    reply = get_chat_response(
        prompt,
        task_type="creative",
        system_prompt=system_prompt,
        prompt_version="chat_system@2.7",
        context_blocks=_active_ctx_blocks,
        correction_mode=_is_correction,
        regime_safe=_regime.is_ml_safe,
        user_id=str(update.effective_user.id),
    )

    # Fallback: if AI failed, try a minimal retry with just the user message + basic market data
    if reply is None:
        log.warning("[handle_message] Primary AI call failed, attempting fallback...")
        fallback_prompt = (
            f"User asked: {user_msg}\n\n"
            f"{market_context or '[No market data available]'}\n\n"
            f"{injury_context or '[No injury data available]'}\n\n"
            f"{search_context or '[No web search results]'}"
        )
        fallback_system = (
            "You are EDGE, a helpful sports prediction market assistant. "
            "Answer the user's question based on the data provided above. "
            "If no data is available, say so and suggest what to check. "
            "Keep your reply under 150 words. Be concise and direct."
        )
        reply = get_chat_response(
            fallback_prompt,
            task_type="simple",
            system_prompt=fallback_system,
            prompt_version="fallback@1.0",
            user_id=str(update.effective_user.id),
            max_tokens=300,
        )

    reply = (
        reply
        or "Sorry, I couldn't generate a response right now. The AI service may be temporarily unavailable. Please try again in a few minutes."
    )

    # Save to per-user session memory
    if reply:
        _mem_user.add_exchange(user_msg, reply)

    # Telegram max message length is 4096 chars
    if len(reply) > 4000:
        reply = reply[:4000] + "\n\n(truncated)"

    await update.message.reply_text(reply)


# ---------------------------------------------------------------------------
# /injuries command — enhanced with player list and team filtering
# ---------------------------------------------------------------------------

_SEVERITY_EMOJI = {
    "Out": "🔴",
    "Injured Reserve": "🔴",  # NHL IR — confirmed miss, treated same as Out
    "Suspension": "🚫",
    "Doubtful": "🟠",
    "Questionable": "🟡",
    "Day-To-Day": "⚪",
}

# Max players shown per sport before truncation (keeps messages readable)
_INJURIES_MAX_PER_SPORT = 40

# Key positions per sport — only these count as impactful starters for alert purposes
_KEY_POSITIONS = {
    "nba": {"PG", "SG", "SF", "PF", "C"},
    "nfl": {"QB", "RB", "WR", "TE"},
    "nhl": {"C", "LW", "RW", "D", "G"},
}

# Statuses that count as "starter alert" material (not just minor bumps)
_ALERT_STATUSES = {"out", "doubtful", "suspension", "injured reserve"}


def _build_tonight_injury_alerts() -> str:
    """
    Reads injury cache for all 3 sports and returns a formatted alert block
    listing Out/Doubtful starters. Completely independent of the qualification
    pipeline — shows useful info even when Qualified: 0.

    Enhancements:
    • NBA alerts are filtered to teams playing tonight via BallDontLie API.
    • Each alert shows the win-probability shift calculated by injury_win_prob_shift()
      e.g. "🔴 LeBron James [SF] (Lakers) — Out ✓ → -12.3% win prob (10.5 pts/gm)"
    """
    try:
        from edge_agent.memory.injury_cache import InjuryCache
        from edge_agent.win_probability import injury_win_prob_shift

        db = InjuryCache()
        lines: list[str] = []

        # Fetch tonight's NBA game schedule once (cached 30 min)
        tonight_nba = _get_tonight_nba_games()  # frozenset of lowercase team tokens

        for sport in ("nba", "nfl", "nhl"):
            records = db.get_all(sport)
            if not records:
                continue
            key_pos = _KEY_POSITIONS.get(sport, set())
            alerts = [
                r
                for r in records
                if r.get("status", "").lower() in _ALERT_STATUSES
                and (not r.get("position") or r.get("position", "") in key_pos)
            ]
            if not alerts:
                continue

            # Header — tag NBA with "(Tonight's Games)" when schedule is available
            header_suffix = (
                " (Tonight & Tomorrow)" if sport == "nba" and tonight_nba else ""
            )
            lines.append(f"\n🏥 <b>{sport.upper()} Starter Alerts{header_suffix}:</b>")

            shown = 0
            for r in alerts:
                if shown >= 10:  # cap at 10 per sport to keep message compact
                    break
                name = r.get("player_name", "Unknown")
                team = r.get("team", "")
                status = r.get("status", "")
                src = r.get("source_api", "")
                pos = r.get("position", "")
                emoji = _SEVERITY_EMOJI.get(status, "⚪")

                # NBA: filter to teams playing tonight when schedule data is available
                if sport == "nba" and tonight_nba:
                    team_tokens = set(team.lower().split())
                    if not team_tokens.intersection(tonight_nba):
                        continue  # this team isn't playing tonight — skip

                # ── Win-probability shift (points-system math) ────────────────
                shift, eff_impact, _expl = injury_win_prob_shift(
                    player_name=name,
                    position=pos,
                    status=status,
                    sport=sport,
                    base_win_prob=0.50,
                    star_multiplier=1.0,
                )
                if shift != 0.0:
                    unit = "goals/gm" if sport == "nhl" else "pts/gm"
                    shift_str = (
                        f" → <b>{shift:+.1%}</b> win prob ({eff_impact:.1f} {unit})"
                    )
                else:
                    shift_str = ""

                # Source confidence badge
                if "⚠️" in src:
                    badge = " ⚠️"
                elif any(x in src for x in ("news✓", "official", "sleeper✓")):
                    badge = " ✓"
                else:
                    badge = ""

                pos_str = f" [{_e(pos)}]" if pos else ""
                lines.append(
                    f"  {emoji} <b>{_e(name)}</b>{pos_str} ({_e(team)}) "
                    f"— {_e(status)}{badge}{shift_str}"
                )
                shown += 1

        return "\n".join(lines) if lines else ""
    except Exception as exc:
        log.debug("_build_tonight_injury_alerts failed: %s", exc)
        return ""


def _fetch_sportsbook_lines(sport: str) -> str:
    """
    Search for tonight's sportsbook moneyline odds for the given sport.
    Tries Tavily first, Serper fallback. Returns "" if both fail or no
    sport keywords matched.
    Costs 1 search call per sport — only fired when injury alerts exist.
    """
    try:
        from datetime import date

        today = date.today().strftime("%B %d")
        query = f"{sport.upper()} games tonight moneyline odds spread {today}"
        result = _tavily_search(query, max_results=3)
        if not result:
            result = _serper_search(query, max_results=3)
        # Strip the wrapper tags and trim to keep scan message readable
        result = (
            result.replace("\n[Live web search results]\n", "")
            .replace("\n[End web search]", "")
            .strip()
        )
        return result[:700] if result else ""
    except Exception as exc:
        log.debug("_fetch_sportsbook_lines failed for %s: %s", sport, exc)
        return ""


# ---------------------------------------------------------------------------
# BallDontLie — tonight's NBA game schedule (free tier)
# ---------------------------------------------------------------------------

# In-process cache so we don't hammer the API on every scan (30-min TTL)
_bdl_game_cache: dict = {"teams": None, "fetched_at": 0.0}


def _get_tonight_nba_games() -> frozenset:
    """
    Fetch tonight's AND tomorrow's NBA game schedule from BallDontLie free tier.
    Returns a frozenset of lowercase team-name tokens so alerts can be filtered
    to teams playing in the next 48-hour window — useful for planning trades
    the night before a game.
    E.g. "Los Angeles Lakers" → {"los", "angeles", "lakers"}

    Caches result for 30 minutes. Returns frozenset() if API key is
    missing or the call fails — callers must treat empty set as "show all".
    """
    import time
    from datetime import timedelta
    import requests as _req

    if not _BALLDONTLIE_API:
        return frozenset()

    now = time.time()
    if (
        _bdl_game_cache["teams"] is not None
        and now - _bdl_game_cache["fetched_at"] < 1800
    ):
        return _bdl_game_cache["teams"]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        resp = _req.get(
            "https://api.balldontlie.io/v1/games",
            headers={"Authorization": _BALLDONTLIE_API},
            # Pass both dates in one request — BallDontLie supports repeated params
            params=[("dates[]", today), ("dates[]", tomorrow)],
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        tokens: set = set()
        for game in data:
            for side in ("home_team", "visitor_team"):
                full_name = game.get(side, {}).get("full_name", "")
                tokens.update(full_name.lower().split())
        result = frozenset(tokens)
        _bdl_game_cache["teams"] = result
        _bdl_game_cache["fetched_at"] = now
        log.info("[BALLDONTLIE] Tonight+Tomorrow NBA team tokens: %s", sorted(result))
        return result
    except Exception as exc:
        log.warning("[BALLDONTLIE] Game fetch failed: %s", exc)
        _bdl_game_cache["teams"] = frozenset()
        _bdl_game_cache["fetched_at"] = now
        return frozenset()


# ---------------------------------------------------------------------------
# Win-probability context builder for AI chat
# ---------------------------------------------------------------------------


def _build_win_prob_context(sport: str) -> str:
    """
    Build a compact win-prob impact summary from the injury cache.
    Injected into the AI chat system prompt so the AI can reason with
    real numbers instead of generic statements.
    Returns "" if cache is empty or sport is unknown/unsupported.
    """
    if not sport or sport == "unknown":
        return ""
    try:
        from edge_agent.memory.injury_cache import InjuryCache
        from edge_agent.win_probability import injury_win_prob_shift

        db = InjuryCache()
        records = db.get_all(sport)
        lines: list[str] = []
        for r in records:
            if r.get("status", "").lower() not in _ALERT_STATUSES:
                continue
            shift, impact, _expl = injury_win_prob_shift(
                player_name=r.get("player_name", ""),
                position=r.get("position", ""),
                status=r.get("status", ""),
                sport=sport,
            )
            if shift != 0.0:
                unit = "goals/gm" if sport == "nhl" else "pts/gm"
                lines.append(
                    f"- {r.get('player_name', '?')} ({r.get('team', '?')}) "
                    f"{r.get('status', '?')}: {shift:+.1%} win-prob shift "
                    f"({impact:.1f} {unit} impact)"
                )
        if not lines:
            return ""
        header = f"[INJURY WIN-PROB IMPACTS — {sport.upper()}]\n"
        return header + "\n".join(lines[:15])
    except Exception as exc:
        log.debug("_build_win_prob_context failed: %s", exc)
        return ""


async def cmd_injuries(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Show cached injury report.

    Usage:
      /injuries            — summary (count + fetch time per sport)
      /injuries nba        — full NBA player list sorted by severity
      /injuries nfl        — full NFL player list sorted by severity
      /injuries nhl        — full NHL player list sorted by severity
      /injuries cfb        — College Football injury list
      /injuries cbb        — College Basketball injury list
      /injuries nba lakers — NBA players for the Lakers only
      /injuries nfl chiefs — NFL players for the Chiefs only
    """
    args = ctx.args or []
    sport_filter = args[0].lower() if args else None
    team_filter = " ".join(args[1:]).lower() if len(args) > 1 else None

    _VALID_SPORTS = ("nba", "nfl", "nhl", "cfb", "cbb", "wnba", "ncaaw")

    try:
        from edge_agent.memory.injury_cache import InjuryCache

        cache = InjuryCache()

        # ── No sport arg: show summary ────────────────────────────────────────
        if not sport_filter or sport_filter not in _VALID_SPORTS:
            stats = cache.stats()
            if not stats:
                await update.message.reply_text(
                    "⚠️ No injury data cached yet.\n"
                    "Refreshes run at 9am, 1:30pm, and 4:30pm PT automatically.\n"
                    "Try <code>/injuries nba</code>, <code>/injuries nfl</code>, or "
                    "<code>/injuries nhl</code> after the first refresh completes.",
                    parse_mode=ParseMode.HTML,
                )
                return

            lines = ["<b>🏥 Injury Cache Summary</b>\n"]
            for sport, info in sorted(stats.items()):
                lines.append(
                    f"<b>{_e(sport)}</b>: {info['count']} injured players\n"
                    f"   Fetched: {_e(info['last_fetch'])} | Expires: {_e(info['expires'])}"
                )
            lines.append(
                "\n<i>Auto-refresh: 9am, 1:30pm, 4:30pm PT. Records expire after 24h.</i>\n"
                "Tip: <code>/injuries nba</code>, <code>/injuries nfl</code>, or "
                "<code>/injuries nhl</code> for full player lists."
            )
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
            return

        # ── Sport arg: show player list ──────────────────────────────────────
        sport = sport_filter  # "nba" or "nfl"
        records = cache.get_all(sport, team_filter=team_filter)

        if not records:
            label = sport.upper()
            if team_filter:
                msg = (
                    f"<b>🏥 {_e(label)} Injuries</b>\n"
                    f"No injured players found for team filter: <i>{_e(team_filter)}</i>\n"
                    f"Try the full team name, city, or abbreviation."
                )
            else:
                msg = (
                    f"<b>🏥 {_e(label)} Injuries</b>\n"
                    "No injury data cached yet. Next refresh at 9am, 1:30pm, or 4:30pm PT."
                )
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
            return

        # Build the player-list message — starters first, role players after
        sport_label = sport.upper()
        header = f"<b>🏥 {sport_label} Injuries</b>"
        if team_filter:
            header += f" — <i>{_e(team_filter.title())}</i>"
        starters_all = [r for r in records if r.get("is_starter")]
        role_all = [r for r in records if not r.get("is_starter")]
        header += f" ({len(starters_all)} starters · {len(role_all)} role)"

        def _player_line(r: dict) -> str:
            status = r.get("status", "")
            sem = _SEVERITY_EMOJI.get(status, "⚪")
            player = r.get("player_name", "")
            pos = r.get("position", "")
            inj_type = r.get("injury_type", "")
            src = r.get("source_api", "espn")
            pos_str = f" ({_e(pos)})" if pos else ""
            detail_str = f" — <i>{_e(inj_type)}</i>" if inj_type else ""
            if "nba_official" in src or "+sleeper✓" in src:
                src_badge = " ✅"
            elif "⚠️" in src:
                src_badge = " ⚠️"
            elif "news✓" in src:
                src_badge = " 📰"
            else:
                src_badge = ""
            return f"  {sem} <b>{_e(player)}</b>{pos_str}: {_e(status)}{detail_str}{src_badge}"

        lines = [header, ""]

        # ── STARTERS section ─────────────────────────────────────────────────
        if starters_all:
            lines.append("<b>⭐ STARTERS</b>")
            current_team = None
            for r in starters_all:
                team = r.get("team", "")
                if team != current_team:
                    if current_team is not None:
                        lines.append("")
                    current_team = team
                    lines.append(f"<b>{_e(team)}</b>")
                lines.append(_player_line(r))

        # ── ROLE PLAYERS section ─────────────────────────────────────────────
        if role_all:
            lines.append("\n<b>ROLE PLAYERS</b>")
            current_team = None
            shown = 0
            for r in role_all:
                if shown >= _INJURIES_MAX_PER_SPORT:
                    lines.append(
                        f"<i>... and {len(role_all) - shown} more. "
                        f"Use /injuries {sport} [team] for filtered view.</i>"
                    )
                    break
                team = r.get("team", "")
                if team != current_team:
                    if current_team is not None:
                        lines.append("")
                    current_team = team
                    lines.append(f"<b>{_e(team)}</b>")
                lines.append(_player_line(r))
                shown += 1

        lines.append(
            "\n<i>✅ multi-source confirmed | ⚠️ conflicting sources | 📰 news confirmed</i>"
        )

        msg = "\n".join(lines)
        # Telegram hard limit is 4096 chars
        if len(msg) > 3900:
            msg = msg[:3900] + (
                f"\n\n<i>(truncated — use /injuries {sport} [team] "
                "for a shorter filtered list)</i>"
            )

        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    except Exception as exc:
        await update.message.reply_text(
            f"⚠️ Injury cache error: {_e(str(exc))}", parse_mode=ParseMode.HTML
        )


# ---------------------------------------------------------------------------
# Background injury refresh job
# ---------------------------------------------------------------------------


async def injury_refresh_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fetches fresh injury data for NBA and NFL from ESPN + NBA official PDF,
    stores results in SQLite. The ONLY place that makes live HTTP calls to
    injury APIs — market scans read from the DB instead.

    After each refresh, checks for pending status-change alerts (e.g. a player
    upgraded from Questionable → Out) and fires proactive Telegram messages.
    """
    log.info("Injury refresh triggered.")
    client = _InjuryClient()
    results = {}

    # ── In-season filter — only refresh sports currently in regular season ────
    # Avoids pointless API calls + prevents the AI getting stale/wrong data
    # for sports that aren't playing (e.g. NFL lines in March = preseason noise).
    _now_month = datetime.now(timezone.utc).month
    _SEASON_MONTHS: dict[str, tuple[int, ...]] = {
        "nba": (10, 11, 12, 1, 2, 3, 4, 5, 6),  # Oct–Jun (inc. playoffs)
        "wnba": (5, 6, 7, 8, 9, 10),  # May–Oct
        "nfl": (9, 10, 11, 12, 1, 2),  # Sep–Feb (inc. playoffs)
        "cfb": (8, 9, 10, 11, 12, 1),  # Aug–Jan
        "nhl": (10, 11, 12, 1, 2, 3, 4, 5, 6),  # Oct–Jun
        "cbb": (11, 12, 1, 2, 3, 4),  # Nov–Apr (March Madness)
        "ncaaw": (11, 12, 1, 2, 3, 4),
    }

    for sport in ("nba", "nfl", "nhl", "cfb", "cbb", "wnba", "ncaaw"):
        active_months = _SEASON_MONTHS.get(sport, tuple(range(1, 13)))
        if _now_month not in active_months:
            results[sport.upper()] = "off-season (skipped)"
            log.info(
                "Injury refresh: %s is off-season (month=%d) — skipped",
                sport.upper(),
                _now_month,
            )
            continue
        try:
            count = client.fetch_and_store(sport)
            results[sport.upper()] = count
        except Exception as exc:
            log.warning("Injury refresh %s failed: %s", sport.upper(), exc)
            results[sport.upper()] = f"error: {exc}"

    summary = " | ".join(f"{s}: {v}" for s, v in results.items())
    log.info("Injury refresh complete — %s", summary)

    # ── Proactive status-change alerts ────────────────────────────────────────
    try:
        from edge_agent.memory.injury_cache import InjuryCache

        cache = InjuryCache()
        pending = cache.get_pending_change_alerts()  # all directions

        for alert in pending:
            player = alert.get("player_name", "")
            team = alert.get("team", "")
            pos = alert.get("position", "")
            old_s = alert.get("old_status", "")
            new_s = alert.get("new_status", "")
            sport = alert.get("sport", "").upper()
            direction = alert.get("direction", "worsening")

            pos_str = f" ({_e(pos)})" if pos else ""
            sport_emoji = {
                "NBA": "🏀",
                "WNBA": "🏀♀️",
                "NCAAW": "🎓🏀♀️",
                "NFL": "🏈",
                "CFB": "🎓🏈",
                "NHL": "🏒",
                "MLB": "⚾",
            }.get(sport, "🏅")

            # ── Worsening alert (existing behavior) ───────────────────────────
            if direction == "worsening":
                old_em = _SEVERITY_EMOJI.get(old_s, "⚪")
                new_em = _SEVERITY_EMOJI.get(new_s, "🔴")
                msg = (
                    f"🚨 <b>INJURY STATUS WORSENED</b>\n\n"
                    f"{sport_emoji} <b>{_e(player)}</b>{pos_str}\n"
                    f"<i>{_e(team)}</i> [{sport}]\n\n"
                    f"{old_em} {_e(old_s)} → {new_em} <b>{_e(new_s)}</b>\n\n"
                    f"<i>This may affect win-probability markets. "
                    f"Run /scan for updated signals.</i>"
                )
                await _broadcast(ctx.bot, msg, parse_mode=ParseMode.HTML)
                log.info(
                    "Proactive injury alert sent: %s %s → %s", player, old_s, new_s
                )

            # ── Return / clearance alert ───────────────────────────────────────
            elif direction == "return":
                if new_s == "Active":
                    status_line = (
                        f"🟢 <b>Cleared — off injury report</b> (was {_e(old_s)})"
                    )
                    headline = "🔓 <b>PLAYER CLEARED FOR RETURN</b>"
                else:
                    old_em = _SEVERITY_EMOJI.get(old_s, "⚪")
                    status_line = f"{old_em} {_e(old_s)} → 🟡 <b>{_e(new_s)}</b>"
                    headline = "📈 <b>INJURY STATUS IMPROVING</b>"

                # Broadcast to the main channel
                channel_msg = (
                    f"{headline}\n\n"
                    f"{sport_emoji} <b>{_e(player)}</b>{pos_str}\n"
                    f"<i>{_e(team)}</i> [{sport}]\n\n"
                    f"{status_line}\n\n"
                    f"<i>Market odds may not have adjusted yet — "
                    f"run /scan for updated win-probability signals.</i>"
                )
                await _broadcast(ctx.bot, channel_msg, parse_mode=ParseMode.HTML)

                # ── Personalized DMs to fans of this player / team ────────────
                try:
                    fan_ids: set[int] = set(
                        _profiles.get_users_for_player(player)
                    ) | set(_profiles.get_users_for_team(team))
                    for fan_id in fan_ids:
                        tone = _profiles.get_alert_tone(
                            fan_id, player_name=player, team_name=team, event="return"
                        )
                        if not tone:
                            continue  # shouldn't happen, but guard anyway
                        dm = (
                            f"{headline}\n\n"
                            f"{sport_emoji} <b>{_e(player)}</b>{pos_str} "
                            f"— <i>{_e(team)}</i>\n\n"
                            f"{status_line}\n\n"
                            f"🎯 <i>This player is on your watchlist. "
                            f"EDGE will scan for market opportunities "
                            f"on their next game automatically.</i>"
                        )
                        try:
                            await ctx.bot.send_message(
                                chat_id=fan_id,
                                text=dm,
                                parse_mode=ParseMode.HTML,
                            )
                            log.info(
                                "Personalized return alert → user %d for %s",
                                fan_id,
                                player,
                            )
                        except Exception as dm_exc:
                            log.warning(
                                "Could not send return DM to user %d: %s",
                                fan_id,
                                dm_exc,
                            )
                except Exception as fan_exc:
                    log.warning(
                        "Fan lookup failed for return alert (%s): %s", player, fan_exc
                    )

                log.info("Return alert sent: %s %s → %s", player, old_s, new_s)

    except Exception as exc:
        log.warning("Could not dispatch proactive injury alerts: %s", exc)


# ---------------------------------------------------------------------------
# /standings command
# ---------------------------------------------------------------------------


async def cmd_standings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Show current standings + Polymarket championship odds for any sport.

    Usage:
      /standings              — championship favorites across all major sports
      /standings nba          — NBA standings + championship odds
      /standings nfl          — NFL standings + Super Bowl odds
      /standings mlb          — MLB standings + World Series odds
      /standings nhl          — NHL standings + Stanley Cup odds
      /standings wnba         — WNBA standings + championship odds
      /standings cfb          — College Football top-25 + playoff odds
      /standings cbb          — College Basketball top-25 + March Madness odds
      /standings ncaaw        — Women's CBB top-25 + championship odds
      /standings mls          — MLS standings + MLS Cup odds
      /standings epl          — Premier League table + champions odds
      /standings laliga       — La Liga table + championship odds
      /standings bundesliga   — Bundesliga table + championship odds
      /standings seriea       — Serie A table + championship odds
      /standings ligue1       — Ligue 1 table + championship odds
      /standings ucl          — Champions League table + winner odds
      /standings f1           — F1 driver + constructor standings
      /standings pga          — PGA Tour current leaderboard
    """
    args = ctx.args or []
    sport = args[0].lower() if args else None

    _VALID = (
        "nfl",
        "nba",
        "mlb",
        "nhl",
        "wnba",
        "cfb",
        "cbb",
        "ncaaw",
        "mls",
        "epl",
        "laliga",
        "bundesliga",
        "seriea",
        "ligue1",
        "ucl",
        "f1",
        "pga",
    )

    await update.message.reply_text("🔍 Fetching standings…")

    try:
        if sport and sport in _VALID:
            # Single sport detailed view
            text = await asyncio.get_running_loop().run_in_executor(
                None, lambda: _standings_client.format_standings(sport)
            )
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        else:
            # No arg: championship favorites summary across all sports
            lines = ["🏆 <b>Championship Favorites (Polymarket)</b>\n"]
            sport_labels = {
                "nfl": "🏈 Super Bowl",
                "nba": "🏀 NBA Champion",
                "mlb": "⚾ World Series",
                "nhl": "🏒 Stanley Cup",
                "wnba": "🏀♀️ WNBA Champion",
                "cfb": "🎓🏈 CFB Playoff",
                "cbb": "🎓🏀 March Madness",
                "ncaaw": "🎓🏀♀️ Women's March Madness",
                "mls": "⚽ MLS Cup",
                "epl": "🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League",
                "ucl": "🌟⚽ Champions League",
                "f1": "🏎️ F1 World Champion",
                "pga": "⛳ Masters Winner",
            }
            for s, label in sport_labels.items():
                try:
                    odds = await asyncio.get_running_loop().run_in_executor(
                        None, lambda s=s: _standings_client.get_championship_odds(s)
                    )
                    if odds:
                        top3 = "  |  ".join(f"{t}: {p:.0%}" for t, p in odds[:3])
                        lines.append(f"<b>{label}</b>\n  {top3}")
                except Exception:
                    pass
            lines.append(
                "\n<i>Use /standings nba, /standings f1, /standings pga, etc. for full tables.\n"
                "Soccer: /standings laliga, /standings bundesliga, /standings seriea, "
                "/standings ligue1, /standings ucl</i>"
            )
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    except Exception as exc:
        log.warning("cmd_standings error: %s", exc)
        await update.message.reply_text(
            f"❌ Standings unavailable right now: {exc}\n"
            "ESPN or Polymarket may be temporarily unreachable."
        )


# ---------------------------------------------------------------------------
# /mlstatus — ML layer health, calibration state, regime, predictions
# ---------------------------------------------------------------------------


async def cmd_mlstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Show the current state of every ML component:
      • Confidence Calibrator (Platt scaling logistic regression)
      • XGBoost Signal Scorer (shadow mode → soft gate → full)
      • Feature Regime Detector (drift monitor)
      • Smart Money (trader feature extractor)
      • Prediction counts (total / resolved / pending)
      • Prompt Registry (all active prompt versions)
    """
    lines: list[str] = ["🤖 <b>ML &amp; AI System Status</b>\n"]

    # ── 1. Confidence Calibrator ──────────────────────────────────────────
    cal = _calibrator.status()
    lines.append("<b>📐 Confidence Calibrator (Platt Scaling)</b>")
    lines.append(
        f"  Status:   {'✅ Active' if cal['active'] else '⏳ Collecting data (passthrough)'}"
    )
    lines.append(f"  Samples:  {cal['n_samples']} / {cal['min_samples_needed']} needed")
    if cal["active"]:
        lines.append(f"  β₀={cal['intercept']}  β₁={cal['slope']}")
        lines.append(f"  Brier score: {cal['brier_score']} (threshold 0.25)")
        lines.append(f"  Trained: {cal['trained_at']}")
    lines.append("")

    # ── 2. XGBoost Signal Scorer ──────────────────────────────────────────
    sc = _scorer.status()
    phase_emoji = {1: "🔵", 2: "🟡", 3: "🟢"}.get(sc["phase"], "🔵")
    lines.append("<b>🌲 XGBoost Signal Scorer</b>")
    lines.append(f"  Phase: {phase_emoji} {sc['phase_label']} (Phase {sc['phase']}/3)")
    lines.append(f"  Samples: {sc['n_samples']} / {sc['min_train_samples']} needed")
    if sc["model_version"] != "untrained":
        lines.append(f"  Model v{sc['model_version']}")
        lines.append(
            f"  Val accuracy: {sc['val_accuracy']:.1%}  |  Logloss: {sc['val_logloss']}"
        )
        lines.append(f"  Trained: {sc['trained_at']}")
        if sc.get("feature_importance"):
            top3 = sorted(sc["feature_importance"].items(), key=lambda x: -x[1])[:3]
            imp_str = "  |  ".join(f"{k}={v:.3f}" for k, v in top3)
            lines.append(f"  Top features: {imp_str}")
        lines.append(
            f"  Promote threshold: {sc['promote_threshold']:.0%}  |  Demote: {sc['demote_threshold']:.0%}"
        )
    lines.append("")

    # ── 3. Regime Detector ────────────────────────────────────────────────
    reg = _regime.status()
    safe_emoji = "✅" if reg["ml_safe"] else "🔴"
    lines.append("<b>📊 Feature Regime Detector</b>")
    lines.append(
        f"  ML safe: {safe_emoji} {'Yes — no drift detected' if reg['ml_safe'] else 'NO — ML DISABLED (drift detected)'}"
    )
    if not reg["ml_safe"] and reg.get("drift_reasons"):
        for r in reg["drift_reasons"]:
            lines.append(f"  ⚠️ {r}")
        lines.append(f"  Recovery: {reg['recovery_needed']}")
    if reg.get("baseline"):
        b = reg["baseline"]
        lines.append(
            f"  Baseline: conf={b.get('confidence', 0):.3f}  ev={b.get('ev_net', 0):.4f}  prob={b.get('market_prob', 0):.3f}"
        )
    lines.append(f"  Last checked: {reg['last_checked']}")
    lines.append(
        f"  Thresholds: conf±{reg['thresholds']['confidence']}  ev±{reg['thresholds']['ev_net']}  prob±{reg['thresholds']['market_prob']}"
    )
    lines.append("")

    # ── 4. Prediction counts ──────────────────────────────────────────────
    pred = _ml_store.prediction_counts()
    total = pred.get("total") or 0
    wins = pred.get("wins") or 0
    losses = pred.get("losses") or 0
    pend = pred.get("pending") or 0
    resolved = wins + losses
    win_rate_str = f"{wins / resolved:.1%}" if resolved > 0 else "n/a"
    lines.append("<b>🎯 Shadow Predictions (all-time)</b>")
    lines.append(f"  Total: {total}  |  Resolved: {resolved}  |  Pending: {pend}")
    lines.append(f"  Wins: {wins}  |  Losses: {losses}  |  Win rate: {win_rate_str}")
    lines.append("")

    # ── 5. Smart money ────────────────────────────────────────────────────
    try:
        tf = _TraderFeatureExtractor(_get_trader_cache())
        lines.append("<b>💰 Smart Money</b>")
        lines.append(f"  {tf.summary()}")
        lines.append("")
    except Exception:
        pass

    # ── 6. Decision Log ───────────────────────────────────────────────────
    try:
        dec_summary = _decision_log.summary(days=7)
        lines.append("<b>📋 AI Decision Log (last 7 days)</b>")
        lines.append(f"  Total calls: {dec_summary['total_calls']}")
        lines.append(f"  Avg latency: {dec_summary['avg_latency_ms']}ms")
        lines.append(
            f"  Correction rate: {dec_summary['correction_rate']} ({dec_summary['correction_calls']} calls)"
        )
        lines.append(f"  User corrections: {dec_summary['user_corrections']}")
        model_stats = _decision_log.model_stats(days=7)
        if model_stats:
            lines.append("  Top models:")
            for ms in model_stats[:3]:
                lines.append(
                    f"    • {ms['model_used'].split('/')[-1]}: "
                    f"{ms['calls']} calls  {int(ms['avg_latency_ms'] or 0)}ms avg"
                )
        lines.append("")
    except Exception:
        pass

    # ── 7. Prompt Registry ────────────────────────────────────────────────
    try:
        prompts = _prompt_registry.list_prompts()
        lines.append("<b>📝 Prompt Registry</b>")
        for p in prompts:
            lines.append(
                f"  • <code>{p['version_id']}</code>  ~{p['tokens_est']} tokens  [{p['hash']}]"
            )
        lines.append("")
    except Exception:
        pass

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n(truncated)"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# /decisions — last N AI decisions with model, prompt version, context blocks
# ---------------------------------------------------------------------------


async def cmd_decisions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Show the last 10 AI decisions so you can debug why the bot said what it said.

    Each row shows:
      • Timestamp + model that answered
      • Prompt version used
      • Which context blocks were active (market data? injuries? scan?)
      • Latency
      • Whether correction mode was on
      • Whether the response was later corrected by the user

    Usage:
      /decisions          — last 10 decisions across all users
      /decisions chat     — filter to chat-type calls only
      /decisions me       — only my own decisions (your user_id)
    """
    args = ctx.args or []
    call_type_filter = None
    user_filter = None

    for arg in args:
        if arg.lower() == "chat":
            call_type_filter = "chat"
        elif arg.lower() == "structured":
            call_type_filter = "structured"
        elif arg.lower() == "me":
            user_filter = str(update.effective_user.id)

    try:
        decisions = _decision_log.get_recent(
            limit=10,
            user_id=user_filter,
            call_type=call_type_filter,
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Decision log unavailable: {exc}")
        return

    if not decisions:
        await update.message.reply_text(
            "📋 No AI decisions logged yet.\n"
            "Decisions are recorded after each chat message or catalyst score call."
        )
        return

    lines = ["📋 <b>Recent AI Decisions</b>\n"]

    for i, d in enumerate(decisions, 1):
        ctx_blocks = d.get("context_blocks") or []
        ctx_str = ", ".join(ctx_blocks) if ctx_blocks else "none"

        # Correction / outcome indicators
        flags = []
        if d.get("correction_mode"):
            flags.append("🔄 correction")
        if not d.get("regime_safe", 1):
            flags.append("⚠️ drift")
        if d.get("outcome") == "corrected_by_user":
            flags.append("❌ user corrected")
        flag_str = "  " + " | ".join(flags) if flags else ""

        model_short = (
            (d.get("model_used") or "unknown").split("/")[-1].replace(":free", "")
        )

        lines.append(
            f"<b>{i}. {d['ts_str']}</b>{flag_str}\n"
            f"  Model: <code>{model_short}</code>  |  {d.get('latency_ms', 0)}ms\n"
            f"  Prompt: <code>{d.get('prompt_version', 'unknown')}</code>\n"
            f"  Context: {ctx_str}\n"
            f"  Type: {d.get('call_type', '?')}"
        )
        if d.get("response_snippet"):
            snip = d["response_snippet"][:120].replace("\n", " ")
            lines.append(f"  Reply: <i>{snip}…</i>")
        lines.append("")

    # Summary stats at the bottom
    try:
        s = _decision_log.summary(days=7)
        lines.append(
            f"<i>7-day: {s['total_calls']} calls  |  "
            f"avg {s['avg_latency_ms']}ms  |  "
            f"correction rate {s['correction_rate']}</i>"
        )
    except Exception:
        pass

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n(truncated)"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# /insider command — show recent insider alerts
# ---------------------------------------------------------------------------


async def cmd_insider(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Show the last 10 insider alerts fired by the engine.

    Usage:
      /insider          — last 10 alerts
      /insider 20       — last 20 alerts

    Each alert shows: market, wallet, position size, suspicion score, outcome.
    """
    args = ctx.args or []
    limit = 10
    try:
        if args:
            limit = max(1, min(int(args[0]), 25))
    except ValueError:
        pass

    engine = _get_insider_engine()
    alerts = engine.get_recent_alerts(limit=limit)

    if not alerts:
        await update.message.reply_text(
            "No insider alerts fired yet.\n\n"
            "The engine scans every 5 minutes for fresh wallets placing large bets "
            "on niche markets. Alerts fire when suspicion score >= 45/100.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [f"<b>Insider Alerts — Last {len(alerts)}</b>\n"]
    for a in alerts:
        ts = datetime.fromtimestamp(a["fired_at"], tz=timezone.utc).strftime(
            "%m/%d %H:%M"
        )
        addr = a["wallet"][:6] + "..." + a["wallet"][-4:]
        q = _e(a["question"][:70])
        score = a["suspicion_score"]
        size = a["trade_size_usd"]
        price = int(a["current_price"] * 100)
        outcome_emoji = {"win": "✅", "loss": "❌", "pending": "⏳"}.get(
            a["outcome"], "⏳"
        )
        score_emoji = "🚨" if score >= 70 else "⚠️" if score >= 50 else "🔍"
        lines.append(
            f"{score_emoji} [{ts}] <b>{score}/100</b> — ${size:,.0f} @ {price}% YES\n"
            f"  <i>{q}</i>\n"
            f"  Wallet: <code>{_e(addr)}</code>  {outcome_emoji} {a['outcome'].upper()}\n"
        )

    lines.append(
        "\n<i>Scores: 70+ = HIGH suspicion | 50-69 = MEDIUM-HIGH | 45-49 = MEDIUM</i>"
    )
    await _send_chunked(update.message.reply_text, "\n".join(lines))


# ---------------------------------------------------------------------------
# Specialist scanner helpers — shared market fetcher + alert formatter
# ---------------------------------------------------------------------------


def _fetch_all_open_markets(limit: int = 200) -> list[dict]:
    """
    Pull open markets from Kalshi (and optionally Polymarket) for specialist scanners.
    Returns normalised list of market dicts with keys: title, price, ticker, venue.
    """
    markets: list[dict] = []
    try:
        ka = _kalshi_api.KalshiAPIClient()
        raw = ka.get_markets(status="open", limit=limit)
        for m in raw or []:
            markets.append(
                {
                    "title": m.get("title", m.get("question", "")),
                    "price": float(m.get("yes_bid", m.get("last_price", 0.5)) or 0.5),
                    "ticker": m.get("ticker", m.get("id", "")),
                    "venue": "kalshi",
                }
            )
    except Exception as exc:
        log.debug("[SpecialistScan] Kalshi market fetch failed: %s", exc)

    # Polymarket — if adapter available, add those markets too
    try:
        from edge_agent.dat_ingestion_polymarket_api import PolymarketAPIClient  # noqa: F401

        pa = PolymarketAPIClient()
        poly_raw = pa.get_markets(active=True, limit=100)
        for m in poly_raw or []:
            markets.append(
                {
                    "title": m.get("question", m.get("title", "")),
                    "price": float(
                        m.get("outcomePrices", [0.5])[0]
                        if isinstance(m.get("outcomePrices"), list)
                        else 0.5
                    ),
                    "ticker": m.get("conditionId", m.get("id", "")),
                    "venue": "polymarket",
                }
            )
    except Exception:
        pass  # Polymarket adapter optional

    return markets


def _fmt_weather_gap(g: WeatherGap) -> str:
    """Format a WeatherGap into a Telegram HTML alert string."""
    cond_emoji = {
        "temp_above": "🌡️",
        "temp_below": "🥶",
        "snow": "❄️",
        "rain": "🌧️",
    }.get(g.condition, "🌤️")
    action_emoji = "📈" if g.action == "BUY YES" else "📉"
    return (
        f"🌤️ <b>WEATHER MARKET SIGNAL</b>\n\n"
        f"{cond_emoji} <b>{_e(g.title[:80])}</b>\n\n"
        f"Market:  <b>{g.market_prob:.0%}</b>\n"
        f"Model:   <b>{g.model_prob:.0%}</b> (Open-Meteo)\n"
        f"Gap:     <b>{g.gap_pp:+.0f}pp</b>\n\n"
        f"📍 City: {_e(g.city)}\n"
        f"🔢 Forecast: {_e(g.forecast_summary)}\n\n"
        f"{action_emoji} <b>Signal: {g.action}</b>\n"
        f"<i>Venue: {g.venue.upper()} | {g.ticker}</i>"
    )


def _fmt_crypto_gap(g: CryptoGap) -> str:
    """Format a CryptoGap into a Telegram HTML alert string."""
    sym = g.symbol.replace("USDT", "")
    action_emoji = "📈" if g.action == "BUY YES" else "📉"
    c24_str = f"{g.change_24h:+.1f}%"
    c7d_str = f"{g.change_7d:+.1f}%"
    return (
        f"₿ <b>CRYPTO MARKET SIGNAL</b>\n\n"
        f"<b>{_e(g.title[:80])}</b>\n\n"
        f"Market:  <b>{g.market_prob:.0%}</b>\n"
        f"Model:   <b>{g.model_prob:.0%}</b> (lognormal)\n"
        f"Gap:     <b>{g.gap_pp:+.0f}pp</b>\n\n"
        f"📊 {sym}: ${g.current_price:,.2f} | 24h {c24_str} | 7d {c7d_str}\n"
        f"📉 Ann.vol: {g.daily_vol:.1f}%\n\n"
        f"{action_emoji} <b>Signal: {g.action}</b>\n"
        f"<i>Venue: {g.venue.upper()} | {g.ticker}</i>"
    )


def _fmt_econ_gap(g: EconGap) -> str:
    """Format an EconGap into a Telegram HTML alert string."""
    cat_emoji = {
        "fed_rate": "🏦",
        "inflation": "📈",
        "recession": "📉",
        "unemployment": "👷",
        "gdp": "📊",
    }.get(g.category, "🏛️")
    action_emoji = "📈" if g.action == "BUY YES" else "📉"
    return (
        f"🏛️ <b>ECON/FED MARKET SIGNAL</b>\n\n"
        f"{cat_emoji} <b>{_e(g.title[:80])}</b>\n\n"
        f"Market:  <b>{g.market_prob:.0%}</b>\n"
        f"Model:   <b>{g.model_prob:.0%}</b> (yield curve)\n"
        f"Gap:     <b>{g.gap_pp:+.0f}pp</b>\n\n"
        f"📡 {_e(g.signal_notes)}\n\n"
        f"{action_emoji} <b>Signal: {g.action}</b>\n"
        f"<i>Venue: {g.venue.upper()} | {g.ticker}</i>"
    )


async def _run_specialist_scans(bot, silent: bool = False) -> tuple[int, int, int]:
    """
    Run weather, crypto, and econ scanners against all open markets.
    Sends alerts to ALERT_CHANNEL_ID (or CHAT_ID) for new gaps.
    Returns (n_weather, n_crypto, n_econ) — number of alerts sent per scanner.
    """
    loop = asyncio.get_running_loop()
    markets = await loop.run_in_executor(None, _fetch_all_open_markets)

    if not markets:
        log.debug("[SpecialistScan] No markets fetched — skipping scan")
        return 0, 0, 0

    target = ALERT_CHANNEL_ID or CHAT_ID
    now = time.time()

    # ── Weather ──────────────────────────────────────────────────────────
    w_gaps = await loop.run_in_executor(None, scan_weather_markets, markets)
    n_w = 0
    for g in w_gaps:
        key = f"weather:{g.ticker}"
        if now - _WEATHER_ALERTED.get(key, 0) < _SPECIALIST_ALERT_COOLDOWN:
            continue
        _WEATHER_ALERTED[key] = now
        if not silent and target:
            try:
                await bot.send_message(
                    chat_id=target, text=_fmt_weather_gap(g), parse_mode=ParseMode.HTML
                )
                n_w += 1
            except Exception as exc:
                log.warning("[WeatherScan] Alert send failed: %s", exc)

    # ── Crypto ───────────────────────────────────────────────────────────
    c_gaps = await loop.run_in_executor(None, scan_crypto_markets, markets)
    n_c = 0
    for g in c_gaps:
        key = f"crypto:{g.ticker}"
        if now - _CRYPTO_ALERTED.get(key, 0) < _SPECIALIST_ALERT_COOLDOWN:
            continue
        _CRYPTO_ALERTED[key] = now
        if not silent and target:
            try:
                await bot.send_message(
                    chat_id=target, text=_fmt_crypto_gap(g), parse_mode=ParseMode.HTML
                )
                n_c += 1
            except Exception as exc:
                log.warning("[CryptoScan] Alert send failed: %s", exc)

    # ── Econ/Fed ─────────────────────────────────────────────────────────
    e_gaps = await loop.run_in_executor(None, scan_econ_markets, markets)
    n_e = 0
    for g in e_gaps:
        key = f"econ:{g.ticker}"
        if now - _ECON_ALERTED.get(key, 0) < _SPECIALIST_ALERT_COOLDOWN:
            continue
        _ECON_ALERTED[key] = now
        if not silent and target:
            try:
                await bot.send_message(
                    chat_id=target, text=_fmt_econ_gap(g), parse_mode=ParseMode.HTML
                )
                n_e += 1
            except Exception as exc:
                log.warning("[EconScan] Alert send failed: %s", exc)

    log.info(
        "[SpecialistScan] Gaps found — weather: %d, crypto: %d, econ: %d | alerts sent: %d/%d/%d",
        len(w_gaps),
        len(c_gaps),
        len(e_gaps),
        n_w,
        n_c,
        n_e,
    )
    return n_w, n_c, n_e


# ---------------------------------------------------------------------------
# /weatherscan command
# ---------------------------------------------------------------------------


async def cmd_weatherscan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /weatherscan — scan all open weather markets against Open-Meteo forecast.

    Shows any pricing gaps where the weather forecast contradicts the market price.
    """
    await update.message.reply_text("🌤️ Running weather market scan…")
    loop = asyncio.get_running_loop()
    markets = await loop.run_in_executor(None, _fetch_all_open_markets)
    gaps = await loop.run_in_executor(None, scan_weather_markets, markets)

    if not gaps:
        await update.message.reply_text(
            "✅ No weather market gaps detected.\n\n"
            "Either no active weather markets were found, or all markets "
            "are within 15pp of the Open-Meteo model forecast."
        )
        return

    lines = [f"🌤️ <b>WEATHER MARKET GAPS</b> — {len(gaps)} found\n"]
    for g in gaps[:5]:
        cond_emoji = {
            "temp_above": "🌡️",
            "temp_below": "🥶",
            "snow": "❄️",
            "rain": "🌧️",
        }.get(g.condition, "🌤️")
        action_emoji = "📈" if g.action == "BUY YES" else "📉"
        lines.append(
            f"{cond_emoji} <b>{_e(g.title[:65])}</b>\n"
            f"   Market {g.market_prob:.0%} → Model {g.model_prob:.0%} "
            f"({g.gap_pp:+.0f}pp) {action_emoji} <b>{g.action}</b>\n"
            f"   📍 {_e(g.city)} | {_e(g.forecast_summary)}\n"
        )

    if len(gaps) > 5:
        lines.append(f"<i>… and {len(gaps) - 5} more gaps</i>")

    lines.append("\n<i>Model: Open-Meteo 7-day forecast | Gaps ≥15pp shown</i>")
    await _send_chunked(update.message.reply_text, "\n".join(lines))


# ---------------------------------------------------------------------------
# /cryptoscan command
# ---------------------------------------------------------------------------


async def cmd_cryptoscan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /cryptoscan — scan crypto prediction markets against Binance price data.

    Uses a lognormal price model to estimate correct probabilities for
    "Will BTC exceed $X by [date]?" type markets.
    """
    await update.message.reply_text("₿ Running crypto market scan…")
    loop = asyncio.get_running_loop()
    markets = await loop.run_in_executor(None, _fetch_all_open_markets)
    gaps = await loop.run_in_executor(None, scan_crypto_markets, markets)

    if not gaps:
        await update.message.reply_text(
            "✅ No crypto market gaps detected.\n\n"
            "Either no active crypto prediction markets were found, or all "
            "markets are priced within 15pp of the lognormal model."
        )
        return

    lines = [f"₿ <b>CRYPTO MARKET GAPS</b> — {len(gaps)} found\n"]
    for g in gaps[:5]:
        sym = g.symbol.replace("USDT", "")
        action_emoji = "📈" if g.action == "BUY YES" else "📉"
        lines.append(
            f"<b>{_e(g.title[:65])}</b>\n"
            f"   {sym}: ${g.current_price:,.2f} | 24h {g.change_24h:+.1f}%\n"
            f"   Market {g.market_prob:.0%} → Model {g.model_prob:.0%} "
            f"({g.gap_pp:+.0f}pp) {action_emoji} <b>{g.action}</b>\n"
        )

    if len(gaps) > 5:
        lines.append(f"<i>… and {len(gaps) - 5} more gaps</i>")

    lines.append(
        "\n<i>Model: Binance lognormal | 15-min price cache | Gaps ≥15pp shown</i>"
    )
    await _send_chunked(update.message.reply_text, "\n".join(lines))


# ---------------------------------------------------------------------------
# /fedscan command
# ---------------------------------------------------------------------------


async def cmd_fedscan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /fedscan — scan Fed/econ prediction markets against yield curve + NY Fed data.

    Detects mispricings in FOMC rate decision markets, inflation markets, etc.
    """
    await update.message.reply_text("🏛️ Running Fed/econ market scan…")
    loop = asyncio.get_running_loop()

    # Show current rates first
    econ_ctx = await loop.run_in_executor(None, get_econ_context_string)

    markets = await loop.run_in_executor(None, _fetch_all_open_markets)
    gaps = await loop.run_in_executor(None, scan_econ_markets, markets)

    lines = [f"🏛️ <b>FED / ECON MARKET SCAN</b>\n"]
    if econ_ctx:
        lines.append(f"<code>{_e(econ_ctx)}</code>\n")

    if not gaps:
        lines.append(
            "✅ No econ market gaps detected.\n\n"
            "Either no active Fed/econ markets were found, or all markets "
            "are within 15pp of the yield curve model."
        )
        await _send_chunked(update.message.reply_text, "\n".join(lines))
        return

    lines.append(f"<b>{len(gaps)} gap(s) found:</b>\n")
    cat_emoji = {
        "fed_rate": "🏦",
        "inflation": "📈",
        "recession": "📉",
        "unemployment": "👷",
        "gdp": "📊",
    }
    for g in gaps[:5]:
        ce = cat_emoji.get(g.category, "🏛️")
        action_emoji = "📈" if g.action == "BUY YES" else "📉"
        lines.append(
            f"{ce} <b>{_e(g.title[:65])}</b>\n"
            f"   Market {g.market_prob:.0%} → Model {g.model_prob:.0%} "
            f"({g.gap_pp:+.0f}pp) {action_emoji} <b>{g.action}</b>\n"
            f"   <i>{_e(g.signal_notes[:80])}</i>\n"
        )

    if len(gaps) > 5:
        lines.append(f"<i>… and {len(gaps) - 5} more gaps</i>")

    lines.append("\n<i>Model: NY Fed EFFR + US Treasury yields | Gaps ≥15pp shown</i>")
    await _send_chunked(update.message.reply_text, "\n".join(lines))


# ---------------------------------------------------------------------------
# Background scan job
# ---------------------------------------------------------------------------


async def scan_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("Background scan triggered.")
    result = await _run_scan(ctx.bot, notify=True)
    if "Scan error" in result:
        await _broadcast(ctx.bot, f"⚠️ Scan error:\n{result}")
    else:
        log.info("Scan complete: %s", result.split("\n")[0])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_SEED_WALLET_FILE = (
    Path(__file__).parent / "edge_agent" / "memory" / "data" / "seed_wallets.json"
)


def _bootstrap_seed_wallets() -> None:
    """
    Load seed_wallets.json into the trader watchlist at startup.
    Skips wallets already present. Runs in O(n) — safe to call every restart.
    """
    if not _SEED_WALLET_FILE.exists():
        log.warning("[bootstrap] seed_wallets.json not found — skipping.")
        return
    try:
        data = json.loads(_SEED_WALLET_FILE.read_text(encoding="utf-8"))
        wallets = data.get("wallets", [])
        tc = _TraderCache()
        added = 0
        for w in wallets:
            addr = w.get("address", "").strip().lower()
            note = w.get("note", "Owner seed — priority vet")
            if not addr:
                continue
            ok = tc.watchlist_add(addr, added_by="seed_bootstrap", note=note)
            if ok:
                added += 1
        log.info(
            "[bootstrap] Seed wallets loaded: %d new / %d total in file.",
            added,
            len(wallets),
        )
    except Exception as exc:
        log.warning("[bootstrap] Failed to load seed wallets: %s", exc)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN not set in .env\n"
            "Create a bot at @BotFather and add the token to .env"
        )
    if not CHAT_ID:
        raise SystemExit(
            "TELEGRAM_CHAT_ID not set in .env\n"
            "See the setup instructions at the top of this file."
        )

    _bootstrap_seed_wallets()

    log.info(
        "Starting EDGE Telegram bot (scan every %d min, injury refresh every %d min)...",
        SCAN_INTERVAL_MIN,
        INJURY_REFRESH_MIN,
    )

    app = Application.builder().token(BOT_TOKEN).build()

    # ---------------------------------------------------------------------------
    # Access control — single dev/testing channel (Telegram is internal only)
    #   Layer 1: filters.Chat — only TELEGRAM_CHAT_ID group
    #   Layer 2: filters.User — only TELEGRAM_OWNER_ID within that chat
    # ---------------------------------------------------------------------------
    try:
        _chat_filter = filters.Chat(int(CHAT_ID))
        log.info("Chat filter active: only responding to chat_id=%s", CHAT_ID)
    except (ValueError, TypeError):
        _chat_filter = filters.ALL
        log.warning(
            "CHAT_ID=%r is not a valid integer — no chat filter applied", CHAT_ID
        )

    try:
        _user_filter = filters.User(int(OWNER_ID)) if OWNER_ID else filters.ALL
        if OWNER_ID:
            log.info("User filter active: only responding to user_id=%s", OWNER_ID)
        else:
            log.warning(
                "TELEGRAM_OWNER_ID not set — bot will respond to ALL users in the chat. "
                "Set TELEGRAM_OWNER_ID in .env to restrict to just your account."
            )
    except (ValueError, TypeError):
        _user_filter = filters.ALL
        log.warning(
            "OWNER_ID=%r is not a valid integer — no user filter applied", OWNER_ID
        )

    _auth_filter = _chat_filter & _user_filter

    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start, filters=_auth_filter))
    app.add_handler(CommandHandler("help", cmd_help, filters=_auth_filter))
    app.add_handler(CommandHandler("scan", cmd_scan, filters=_auth_filter))
    app.add_handler(CommandHandler("injuries", cmd_injuries, filters=_auth_filter))
    app.add_handler(
        CommandHandler("injurys", cmd_injuries, filters=_auth_filter)
    )  # typo alias
    app.add_handler(CommandHandler("tracking", cmd_tracking, filters=_auth_filter))
    app.add_handler(CommandHandler("top", cmd_top, filters=_auth_filter))
    app.add_handler(CommandHandler("traders", cmd_traders, filters=_auth_filter))
    app.add_handler(CommandHandler("wallet", cmd_wallet, filters=_auth_filter))
    app.add_handler(CommandHandler("watch", cmd_watch, filters=_auth_filter))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch, filters=_auth_filter))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist, filters=_auth_filter))
    app.add_handler(
        CommandHandler("performance", cmd_performance, filters=_auth_filter)
    )
    app.add_handler(CommandHandler("mytrades", cmd_mytrades, filters=_auth_filter))
    app.add_handler(CommandHandler("status", cmd_status, filters=_auth_filter))
    app.add_handler(CommandHandler("approvals", cmd_approvals, filters=_auth_filter))
    app.add_handler(CommandHandler("profile", cmd_profile, filters=_auth_filter))
    app.add_handler(CommandHandler("forget", cmd_forget, filters=_auth_filter))
    app.add_handler(CommandHandler("standings", cmd_standings, filters=_auth_filter))
    app.add_handler(CommandHandler("mlstatus", cmd_mlstatus, filters=_auth_filter))
    app.add_handler(CommandHandler("decisions", cmd_decisions, filters=_auth_filter))
    app.add_handler(CommandHandler("insider", cmd_insider, filters=_auth_filter))
    app.add_handler(
        CommandHandler("weatherscan", cmd_weatherscan, filters=_auth_filter)
    )
    app.add_handler(CommandHandler("cryptoscan", cmd_cryptoscan, filters=_auth_filter))
    app.add_handler(CommandHandler("fedscan", cmd_fedscan, filters=_auth_filter))
    # Inline keyboard (callback queries are always scoped to the chat they came from)
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Free-form AI chat — only in the authorized chat, must come last
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & _auth_filter,
            handle_message,
        )
    )

    # ---------------------------------------------------------------------------
    # Injury refresh — 3 targeted daily pulls aligned to game-prep windows
    # (Pacific time, handles PST/PDT automatically via ZoneInfo)
    #
    #   09:00 PT — morning check: overnight news, NHL morning skate, NFL Wed report
    #   13:30 PT — mid-day: NBA official PDF opens (5 PM ET), NFL Thu/Fri report
    #   16:30 PT — pre-game final: last-minute scratches + lineup confirmations
    #
    # This replaces the old dumb 4-hour timer — injury lists rarely change
    # mid-day but are most likely to update in these three windows.
    # ---------------------------------------------------------------------------
    for _pull_time in (
        dt_time(9, 0, tzinfo=_PACIFIC),  # morning
        dt_time(13, 30, tzinfo=_PACIFIC),  # mid-day / NBA PDF window
        dt_time(16, 30, tzinfo=_PACIFIC),  # pre-game final
    ):
        app.job_queue.run_daily(injury_refresh_job, time=_pull_time)

    # Startup warmup — populate cache 60s after boot regardless of time of day
    app.job_queue.run_once(injury_refresh_job, when=60)

    # Trader leaderboard — refresh daily at 8am PT, warm cache 2 min after boot
    app.job_queue.run_daily(trader_refresh_job, time=dt_time(8, 0, tzinfo=_PACIFIC))
    app.job_queue.run_once(trader_refresh_job, when=120)

    # Discovery sweep — multi-category fast-score, every 1h, first run 3 min after boot
    app.job_queue.run_repeating(discovery_job, interval=3600, first=180)

    # Watchlist re-vet — every 6h, first run 10 min after boot
    app.job_queue.run_repeating(watchlist_vet_job, interval=21600, first=30)

    # Smart money position refresh — every 30 min, first run 2 min after boot
    # Pre-warms the cache so the first user message doesn't trigger a live fetch
    async def _smart_money_refresh_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            _build_smart_money_context(force_refresh=True)
            n_lines = len(_sm_positions_cache["lines"])
            n_new = len(_sm_positions_cache.get("alertable", []))
            log.info(
                "[smart_money_job] Position cache refreshed — %d lines, %d new candidates",
                n_lines,
                n_new,
            )
            if n_new > 0:
                n_sent = await _send_copy_trade_alerts(ctx.bot)
                if n_sent:
                    log.info(
                        "[smart_money_job] Sent %d copy-trade alert(s) to channel",
                        n_sent,
                    )
        except Exception as exc:
            log.debug("[smart_money_job] Refresh failed: %s", exc)

    app.job_queue.run_repeating(_smart_money_refresh_job, interval=1800, first=120)

    # ---------------------------------------------------------------------------
    # Insider alert job — scans niche markets for price moves driven by unknown
    # fresh wallets placing large bets. Fires to ALERT_CHANNEL_ID.
    # Runs every 5 min; first run 3 min after boot (after price snapshot baseline).
    # ---------------------------------------------------------------------------
    async def _insider_scan_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        engine = _get_insider_engine()
        try:

            async def _send_to_alert_channel(msg: str) -> None:
                target = ALERT_CHANNEL_ID or CHAT_ID
                if not target:
                    return
                try:
                    await ctx.bot.send_message(
                        chat_id=target,
                        text=msg,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except Exception as exc:
                    log.warning("[insider_job] send failed: %s", exc)

            n = await engine.run_scan(send_alert_fn=_send_to_alert_channel)
            if n:
                log.info("[insider_job] %d insider alert(s) fired this cycle", n)

            # Cleanup old records weekly (piggyback on maintenance rhythm)
            import random

            if random.random() < 0.02:  # ~2% chance per run ≈ weekly at 5-min intervals
                engine.cleanup_old_records(days=30)

        except Exception as exc:
            log.warning("[insider_job] scan failed: %s", exc)

    app.job_queue.run_repeating(_insider_scan_job, interval=300, first=180)

    # ---------------------------------------------------------------------------
    # Specialist scanner job — weather, crypto, econ gap detection
    # Runs every 4h; first run 5 min after boot (after main markets are loaded)
    # ---------------------------------------------------------------------------
    async def _specialist_scan_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            n_w, n_c, n_e = await _run_specialist_scans(ctx.bot)
            total = n_w + n_c + n_e
            if total:
                log.info(
                    "[specialist_job] Alerts sent — weather:%d crypto:%d econ:%d",
                    n_w,
                    n_c,
                    n_e,
                )
        except Exception as exc:
            log.warning("[specialist_job] failed: %s", exc)

    app.job_queue.run_repeating(_specialist_scan_job, interval=14400, first=300)

    # Outcome resolution — check pending signals every 2h, first run 5 min after boot
    app.job_queue.run_repeating(outcome_resolution_job, interval=7200, first=300)

    # Weekly maintenance — VACUUM all DBs + archive scan_log + purge .cache/
    # Runs every Sunday at 3:00 AM PT (604800s = 7 days)
    app.job_queue.run_daily(
        maintenance_job,
        time=dt_time(3, 0, tzinfo=_PACIFIC),
        days=(6,),  # Sunday only (0=Mon … 6=Sun)
    )

    # ML calibration refresh — retrain confidence calibrator + XGBoost scorer weekly
    # Runs every Saturday at 2:00 AM PT (day before maintenance VACUUM)
    # No-op if < 150 labeled signals (safe passthrough maintained)
    app.job_queue.run_daily(
        ml_calibration_job,
        time=dt_time(2, 0, tzinfo=_PACIFIC),
        days=(5,),  # Saturday only
    )
    # Also run 15 min after boot to pick up any models saved before restart
    app.job_queue.run_once(ml_calibration_job, when=900)

    # Background market scan loop — reads from injury cache, no live injury API calls
    app.job_queue.run_repeating(
        scan_job,
        interval=SCAN_INTERVAL_MIN * 60,
        first=90,  # first scan after injury cache is warm
    )

    log.info("Bot running. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


_PID_FILE = Path(__file__).parent / ".edge_bot.pid"


def _acquire_instance_lock() -> None:
    """Kill any previous instance, then write our own PID."""
    if _PID_FILE.exists():
        try:
            old_pid = int(_PID_FILE.read_text().strip())
            if old_pid != os.getpid():
                try:
                    if os.name == "nt":
                        import subprocess

                        subprocess.run(
                            ["taskkill", "/F", "/PID", str(old_pid)],
                            capture_output=True,
                        )
                    else:
                        os.kill(old_pid, signal.SIGTERM)
                    log.info("Killed previous bot instance (PID %d).", old_pid)
                    time.sleep(2)  # give Telegram time to release the long-poll
                except (ProcessLookupError, PermissionError, OSError):
                    pass  # process already dead — fine
        except (ValueError, OSError):
            pass
    _PID_FILE.write_text(str(os.getpid()))


def _release_instance_lock() -> None:
    try:
        _PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


if __name__ == "__main__":
    _acquire_instance_lock()
    try:
        main()
    finally:
        _release_instance_lock()
